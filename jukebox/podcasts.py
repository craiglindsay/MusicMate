"""Podcasts (2 Nov 2026): search, subscribe, listen, per person.

People. The household has no accounts, so a person is a name picked on each
device ("who's listening") and sent with every podcast call as X-MM-Who. A
device on the tailnet also carries Tailscale-User-Login; the first time a
person is picked there the login is remembered, so that device's next visit
picks them by itself. Each person has their own subscriptions, resume points
and played marks; everyone's shows are visible to everyone (the owner's
decision), so the household can see what the others follow.

Search is Apple's podcast directory (itunes.apple.com/search, no key); a show
is its RSS feed. Feeds are read every hour for any show at least one person
follows. The newest episode of each followed show is downloaded to the
podcast folder (D:/Podcasts, never the music library; JUKEBOX_PODCASTS
overrides), as is any episode someone is part-way through; everything else
streams from the publisher through this server, so the TV, phones and Sonos
all play the same URL.

An episode is a queue entry with id POD_BASE + its row id, marked pod: 1, like
a DJ link is marked dj: 1. Everything the music player does with a song id
(stream, /media for Sonos, /api/tracks) answers for it.

All of it lives in state.db: subscriptions and positions are the household's
own data and ride along in the daily backup. Shows and episodes are cached
there too, since the positions refer to them.
"""
import os, re, io, json, time, shutil, sqlite3, threading, email.utils, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

import state

HERE = Path(__file__).resolve().parent
FILES = Path(os.environ.get('JUKEBOX_PODCASTS') or r'D:\Podcasts')
ART = HERE / 'cache' / 'pod'
POD_BASE = 10 ** 12
UA = 'MusicMate/1.0 (home podcast player; https://github.com/craiglindsay/MusicMate)'
REFRESH = 3600
KEEP_ITEMS = 300                 # the newest this many episodes of a feed are listed
KEEP_PARTIAL_DAYS = 30           # a half-heard episode stays on disk this long
ITUNES = 'http://www.itunes.com/dtds/podcast-1.0.dtd'

SCHEMA = """
CREATE TABLE IF NOT EXISTS person (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE COLLATE NOCASE, login TEXT, created REAL);
CREATE TABLE IF NOT EXISTS pod_show (
  id INTEGER PRIMARY KEY AUTOINCREMENT, feed TEXT UNIQUE, title TEXT, author TEXT, description TEXT,
  image TEXT, link TEXT, checked REAL, error TEXT, added REAL, latest REAL);
CREATE TABLE IF NOT EXISTS pod_episode (
  id INTEGER PRIMARY KEY AUTOINCREMENT, show_id INTEGER, guid TEXT, title TEXT, published REAL,
  duration REAL, url TEXT, mime TEXT, size INTEGER, description TEXT, file TEXT,
  UNIQUE (show_id, guid));
CREATE INDEX IF NOT EXISTS ix_pod_ep ON pod_episode(show_id, published DESC);
CREATE TABLE IF NOT EXISTS pod_sub (
  person_id INTEGER, show_id INTEGER, since REAL, PRIMARY KEY (person_id, show_id));
CREATE TABLE IF NOT EXISTS pod_pos (
  person_id INTEGER, episode_id INTEGER, pos REAL, duration REAL, played INTEGER DEFAULT 0, updated REAL,
  PRIMARY KEY (person_id, episode_id));
"""

_lock = threading.Lock()          # one feed refresh / download pass at a time
STATUS = {'last': 0, 'running': False, 'error': None, 'downloading': None}


def db():
    c = state.db()
    c.executescript(SCHEMA)
    return c


def _rows(sql, *a):
    c = db()
    try:
        return [dict(r) for r in c.execute(sql, a)]
    finally:
        c.close()


def _one(sql, *a):
    r = _rows(sql, *a)
    return r[0] if r else None


def _exec(sql, *a):
    c = db()
    try:
        cur = c.execute(sql, a)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


# ------------------------------------------------------------------ people
def people():
    return _rows('SELECT id, name FROM person ORDER BY name')


def add_person(name, login=None):
    name = re.sub(r'\s+', ' ', name or '').strip()[:40]
    if not name:
        raise ValueError('a name, please')
    r = _one('SELECT id, name FROM person WHERE name=?', name)
    if r:
        return r
    pid = _exec('INSERT INTO person (name, login, created) VALUES (?,?,?)', name, login, time.time())
    return {'id': pid, 'name': name}


def remember_login(person_id, login):
    if login:
        _exec('UPDATE person SET login=NULL WHERE login=? AND id<>?', login, person_id)
        _exec('UPDATE person SET login=? WHERE id=?', login, person_id)


def by_login(login):
    return _one('SELECT id, name FROM person WHERE login=?', login) if login else None


def person(pid):
    try:
        return _one('SELECT id, name FROM person WHERE id=?', int(pid))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ the network
def _get(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers=dict({'User-Agent': UA}, **(headers or {})))
    return urllib.request.urlopen(req, timeout=timeout)


def search(q):
    """Apple's directory: shows with a feed, best match first."""
    url = 'https://itunes.apple.com/search?' + urllib.parse.urlencode(
        {'media': 'podcast', 'entity': 'podcast', 'term': q, 'limit': 30})
    with _get(url, 20) as r:
        d = json.loads(r.read().decode('utf-8', 'replace'))
    have = {s['feed']: s['id'] for s in _rows('SELECT id, feed FROM pod_show')}
    out = []
    for x in d.get('results') or []:
        feed = x.get('feedUrl')
        if not feed:
            continue
        out.append({'feed': feed, 'title': x.get('collectionName') or x.get('trackName') or '',
                    'author': x.get('artistName') or '', 'image': x.get('artworkUrl600') or x.get('artworkUrl100'),
                    'genre': x.get('primaryGenreName') or '', 'episodes': x.get('trackCount') or 0,
                    'show_id': have.get(feed)})
    return out


# ------------------------------------------------------------------ feeds
def _text(el, *paths):
    for p in paths:
        x = el.find(p)
        if x is not None and (x.text or '').strip():
            return x.text.strip()
    return None


def _clean(html):
    """show notes as plain text: tags out, paragraphs kept"""
    if not html:
        return ''
    s = re.sub(r'(?i)<br\s*/?>|</p>|</li>', '\n', html)
    s = re.sub(r'<[^>]+>', '', s)
    import html as _h
    s = _h.unescape(s)
    return re.sub(r'\n\s*\n+', '\n\n', s).strip()[:6000]


def _dur(s):
    if not s:
        return None
    try:
        parts = [float(p) for p in s.strip().split(':')]
    except ValueError:
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec or None


def _when(s):
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except Exception:
        return None


def parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    ch = root.find('channel')
    if ch is None:
        raise ValueError('not a podcast feed')
    img = ch.find('{%s}image' % ITUNES)
    show = {'title': _text(ch, 'title') or 'Untitled',
            'author': _text(ch, '{%s}author' % ITUNES, 'managingEditor') or '',
            'description': _clean(_text(ch, '{%s}summary' % ITUNES, 'description')),
            'image': (img.get('href') if img is not None else None) or _text(ch, 'image/url'),
            'link': _text(ch, 'link')}
    eps = []
    for it in ch.findall('item'):
        enc = it.find('enclosure')
        if enc is None or not enc.get('url'):
            continue
        mime = enc.get('type') or ''
        if mime and not (mime.startswith('audio') or mime in ('application/octet-stream',)):
            continue                                   # video podcasts are not for this player
        eps.append({'guid': _text(it, 'guid') or enc.get('url'), 'title': _text(it, 'title') or 'Episode',
                    'published': _when(_text(it, 'pubDate')), 'duration': _dur(_text(it, '{%s}duration' % ITUNES)),
                    'url': enc.get('url'), 'mime': mime or 'audio/mpeg',
                    'size': int(enc.get('length') or 0) if (enc.get('length') or '').isdigit() else None,
                    'description': _clean(_text(it, '{http://purl.org/rss/1.0/modules/content/}encoded',
                                                '{%s}summary' % ITUNES, 'description'))})
    eps.sort(key=lambda e: e['published'] or 0, reverse=True)
    return show, eps[:KEEP_ITEMS]


def refresh_show(show_id=None, feed=None):
    """Read one feed; add new episodes. Returns the show id."""
    if show_id:
        feed = _one('SELECT feed FROM pod_show WHERE id=?', show_id)['feed']
    try:
        with _get(feed, 40) as r:
            body = r.read()
        show, eps = parse(body)
    except Exception as e:
        if show_id:
            _exec('UPDATE pod_show SET checked=?, error=? WHERE id=?', time.time(), str(e)[:200], show_id)
            return show_id
        raise
    c = db()
    try:
        r = c.execute('SELECT id FROM pod_show WHERE feed=?', (feed,)).fetchone()
        if r:
            show_id = r['id']
            c.execute('UPDATE pod_show SET title=?, author=?, description=?, image=?, link=?, checked=?, error=NULL '
                      'WHERE id=?', (show['title'], show['author'], show['description'], show['image'],
                                     show['link'], time.time(), show_id))
        else:
            show_id = c.execute('INSERT INTO pod_show (feed,title,author,description,image,link,checked,added) '
                                'VALUES (?,?,?,?,?,?,?,?)', (feed, show['title'], show['author'], show['description'],
                                                             show['image'], show['link'], time.time(), time.time())).lastrowid
        for e in eps:
            c.execute('INSERT INTO pod_episode (show_id,guid,title,published,duration,url,mime,size,description) '
                      'VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(show_id,guid) DO UPDATE SET title=excluded.title, '
                      'url=excluded.url, mime=excluded.mime, description=excluded.description, '
                      'duration=COALESCE(pod_episode.duration, excluded.duration)',
                      (show_id, e['guid'], e['title'], e['published'], e['duration'], e['url'], e['mime'],
                       e['size'], e['description']))
        latest = eps[0]['published'] if eps else None
        c.execute('UPDATE pod_show SET latest=? WHERE id=?', (latest, show_id))
        c.commit()
    finally:
        c.close()
    _art(show_id, show['image'])
    return show_id


def _art(show_id, url):
    p = ART / ('%d.jpg' % show_id)
    if p.exists() or not url:
        return
    try:
        from PIL import Image
        with _get(url, 30) as r:
            im = Image.open(io.BytesIO(r.read())).convert('RGB')
        im.thumbnail((600, 600))
        ART.mkdir(parents=True, exist_ok=True)
        im.save(p, 'JPEG', quality=86)
    except Exception as e:
        print('podcast art %d: %s' % (show_id, e), flush=True)


def art_path(show_id):
    p = ART / ('%d.jpg' % show_id)
    return p if p.exists() else None


# ------------------------------------------------------------------ downloads
def _ext(mime, url):
    m = re.search(r'\.(mp3|m4a|aac|mp4|ogg|opus|wav)(?:\?|$)', (url or '').lower())
    if m:
        return '.' + m.group(1)
    return {'audio/mp4': '.m4a', 'audio/x-m4a': '.m4a', 'audio/aac': '.aac', 'audio/ogg': '.ogg'}.get(mime, '.mp3')


def file_of(ep):
    if ep and ep.get('file'):
        p = FILES / ep['file']
        if p.exists():
            return p
    return None


def _download(ep):
    rel = '%d/%d%s' % (ep['show_id'], ep['id'], _ext(ep['mime'], ep['url']))
    dst = FILES / rel
    if dst.exists():
        _exec('UPDATE pod_episode SET file=? WHERE id=?', rel, ep['id'])
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(dst.suffix + '.part')
    STATUS['downloading'] = ep['title']
    try:
        with _get(ep['url'], 60) as r, open(part, 'wb') as f:
            shutil.copyfileobj(r, f, 1 << 20)
        os.replace(part, dst)
        _exec('UPDATE pod_episode SET file=? WHERE id=?', rel, ep['id'])
    finally:
        STATUS['downloading'] = None
        if part.exists():
            part.unlink()


def _keep_ids():
    """episodes that should be on disk: the newest of every followed show, and
    any someone is part-way through"""
    keep = {}
    for s in _rows('SELECT DISTINCT show_id FROM pod_sub'):
        e = _one('SELECT * FROM pod_episode WHERE show_id=? ORDER BY published DESC LIMIT 1', s['show_id'])
        if e:
            keep[e['id']] = e
    for e in _rows('SELECT pod_episode.* FROM pod_pos JOIN pod_episode ON pod_episode.id=pod_pos.episode_id '
                   'WHERE pod_pos.played=0 AND pod_pos.pos>30 AND pod_pos.updated>?',
                   time.time() - KEEP_PARTIAL_DAYS * 86400):
        keep[e['id']] = e
    return keep


def sync_files():
    keep = _keep_ids()
    for e in keep.values():
        if not file_of(e):
            try:
                _download(e)
            except Exception as ex:
                print('podcast download %d: %s' % (e['id'], ex), flush=True)
    for e in _rows('SELECT id, file FROM pod_episode WHERE file IS NOT NULL'):
        if e['id'] not in keep:
            try:
                (FILES / e['file']).unlink()
            except OSError:
                pass
            _exec('UPDATE pod_episode SET file=NULL WHERE id=?', e['id'])


def refresh_all():
    if not _lock.acquire(blocking=False):
        return
    STATUS['running'] = True
    try:
        for s in _rows('SELECT DISTINCT show_id FROM pod_sub'):
            refresh_show(s['show_id'])
        sync_files()
        STATUS['error'] = None
    except Exception as e:
        STATUS['error'] = str(e)[:200]
        print('podcasts: %s' % e, flush=True)
    finally:
        STATUS['last'] = time.time()
        STATUS['running'] = False
        _lock.release()


def start():
    def loop():
        time.sleep(60)
        while True:
            refresh_all()
            time.sleep(REFRESH)
    threading.Thread(target=loop, daemon=True, name='podcasts').start()


def kick():
    threading.Thread(target=refresh_all, daemon=True).start()


# ------------------------------------------------------------------ listening
def subscribe(pid, feed=None, show_id=None):
    if not show_id:
        r = _one('SELECT id FROM pod_show WHERE feed=?', feed)
        show_id = r['id'] if r else refresh_show(feed=feed)
    _exec('INSERT OR IGNORE INTO pod_sub VALUES (?,?,?)', pid, show_id, time.time())
    kick()                                        # fetch its newest episode now
    return show_id


def unsubscribe(pid, show_id):
    _exec('DELETE FROM pod_sub WHERE person_id=? AND show_id=?', pid, show_id)


def _subscribers():
    out = {}
    for r in _rows('SELECT pod_sub.show_id, person.id, person.name FROM pod_sub '
                   'JOIN person ON person.id=pod_sub.person_id ORDER BY person.name'):
        out.setdefault(r['show_id'], []).append({'id': r['id'], 'name': r['name']})
    return out


def _show_card(s, subs, pid):
    who = subs.get(s['id'], [])
    return {'id': s['id'], 'title': s['title'], 'author': s['author'], 'latest': s['latest'],
            'followers': [w['name'] for w in who], 'mine': any(w['id'] == pid for w in who),
            'error': s.get('error')}


def item(e, show=None, pos=None):
    """an episode as a queue entry"""
    show = show or _one('SELECT title FROM pod_show WHERE id=?', e['show_id']) or {}
    d = {'id': POD_BASE + e['id'], 'pod': 1, 'episode_id': e['id'], 'show_id': e['show_id'],
         'title': e['title'], 'artist': show.get('title') or '', 'album': show.get('title') or '',
         'duration': e['duration'], 'published': e['published'], 'local': bool(e.get('file')),
         'mime': e['mime']}
    if pos:
        d.update(pos=pos['pos'] or 0, played=pos['played'])
    return d


def _positions(pid, ids):
    if not pid or not ids:
        return {}
    ph = ','.join('?' * len(ids))
    return {r['episode_id']: r for r in _rows(
        f'SELECT * FROM pod_pos WHERE person_id=? AND episode_id IN ({ph})', pid, *ids)}


def home(pid):
    subs = _subscribers()
    shows = _rows('SELECT * FROM pod_show WHERE id IN (SELECT show_id FROM pod_sub) ORDER BY latest DESC')
    cards = [_show_card(s, subs, pid) for s in shows]
    out = {'mine': [c for c in cards if c['mine']], 'others': [c for c in cards if not c['mine']],
           'continue': [], 'new': [], 'status': dict(STATUS)}
    if pid:
        names = {s['id']: s for s in shows}
        for r in _rows('SELECT pod_episode.*, pod_pos.pos, pod_pos.played FROM pod_pos '
                       'JOIN pod_episode ON pod_episode.id=pod_pos.episode_id '
                       'WHERE pod_pos.person_id=? AND pod_pos.played=0 AND pod_pos.pos>30 '
                       'ORDER BY pod_pos.updated DESC LIMIT 12', pid):
            out['continue'].append(item(r, names.get(r['show_id']), r))
        for r in _rows('SELECT pod_episode.*, pod_pos.pos, pod_pos.played FROM pod_episode '
                       'JOIN pod_sub ON pod_sub.show_id=pod_episode.show_id AND pod_sub.person_id=? '
                       'LEFT JOIN pod_pos ON pod_pos.episode_id=pod_episode.id AND pod_pos.person_id=? '
                       'WHERE pod_episode.published>? AND COALESCE(pod_pos.played,0)=0 AND COALESCE(pod_pos.pos,0)<=30 '
                       'ORDER BY pod_episode.published DESC LIMIT 20', pid, pid, time.time() - 14 * 86400):
            out['new'].append(item(r, names.get(r['show_id']), r))
    return out


def show(show_id, pid, limit=300):
    s = _one('SELECT * FROM pod_show WHERE id=?', show_id)
    if not s:
        return None
    subs = _subscribers()
    eps = _rows('SELECT * FROM pod_episode WHERE show_id=? ORDER BY published DESC LIMIT ?', show_id, limit)
    pos = _positions(pid, [e['id'] for e in eps])
    d = _show_card(s, subs, pid)
    d.update(description=s['description'], link=s['link'], feed=s['feed'], checked=s['checked'],
             episodes=[dict(item(e, s, pos.get(e['id'])), description=(e['description'] or '')[:400]) for e in eps])
    return d


def episode(eid):
    return _one('SELECT * FROM pod_episode WHERE id=?', eid)


def notes(eid):
    e = _one('SELECT pod_episode.title, pod_episode.description, pod_episode.published, pod_show.title show '
             'FROM pod_episode JOIN pod_show ON pod_show.id=pod_episode.show_id WHERE pod_episode.id=?', eid)
    return e


def queue(eid, pid):
    """play this episode, then the next unplayed ones of the show, oldest first
    after it (a show heard in order carries on in order)"""
    e = episode(eid)
    if not e:
        return []
    s = _one('SELECT title FROM pod_show WHERE id=?', e['show_id'])
    after = _rows('SELECT * FROM pod_episode WHERE show_id=? AND published>? ORDER BY published ASC LIMIT 40',
                  e['show_id'], e['published'] or 0)
    pos = _positions(pid, [e['id']] + [a['id'] for a in after])
    out = [item(e, s, pos.get(e['id']))]
    for a in after:
        p = pos.get(a['id'])
        if p and p['played']:
            continue
        out.append(item(a, s, p))
        if len(out) >= 12:
            break
    return out


def save_pos(pid, eid, pos, duration=None, played=None):
    if not pid:
        return None
    pos = max(0.0, float(pos or 0))
    if played is None:
        played = bool(duration and (pos >= duration - 45 or pos / duration > .97))
    c = db()
    try:
        c.execute('INSERT INTO pod_pos (person_id,episode_id,pos,duration,played,updated) VALUES (?,?,?,?,?,?) '
                  'ON CONFLICT(person_id,episode_id) DO UPDATE SET pos=excluded.pos, '
                  'duration=COALESCE(excluded.duration,pod_pos.duration), played=excluded.played, updated=excluded.updated',
                  (pid, eid, 0 if played else pos, duration, 1 if played else 0, time.time()))
        if duration:
            c.execute('UPDATE pod_episode SET duration=? WHERE id=? AND (duration IS NULL OR ABS(duration-?)>5)',
                      (duration, eid, duration))
        c.commit()
    finally:
        c.close()
    return {'played': bool(played)}


def hydrate(ids, pid=None):
    eids = [i - POD_BASE for i in ids]
    if not eids:
        return {}
    ph = ','.join('?' * len(eids))
    eps = _rows(f'SELECT pod_episode.*, pod_show.title show_title FROM pod_episode '
                f'JOIN pod_show ON pod_show.id=pod_episode.show_id WHERE pod_episode.id IN ({ph})', *eids)
    pos = _positions(pid, eids)
    return {POD_BASE + e['id']: item(e, {'title': e['show_title']}, pos.get(e['id'])) for e in eps}


def newest_for(pid):
    """the newest episode of each show this person follows (phones keep these)"""
    out = []
    for s in _rows('SELECT pod_show.id, pod_show.title FROM pod_sub JOIN pod_show ON pod_show.id=pod_sub.show_id '
                   'WHERE pod_sub.person_id=?', pid):
        e = _one('SELECT * FROM pod_episode WHERE show_id=? ORDER BY published DESC LIMIT 1', s['id'])
        if e:
            out.append(item(e, s))
    return out
