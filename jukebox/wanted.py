"""Music the household does not have yet: find it on MusicBrainz, request it,
follow the request until it plays. (30 Oct 2026; docs/PLAN-requests-and-auto-intake.md)

  find     an artist's studio albums missing from the library (artist pages),
           and a MusicBrainz search for what the library has no match for. A
           song resolves to the studio album it first appeared on, because the
           pipeline and the library both work in whole albums.
  request  POST /api/requests runs UNPoller's request_album.py <rgid>, which
           owns the ledger and every Lidarr quirk. Anyone on the LAN may
           request (the owner's decision, 30 Oct 2026).
  follow   each open request's state, worked out afresh rather than stored:
             asking       request_album.py is talking to Lidarr
             searching    monitored, nothing grabbed yet (Lidarr keeps watching)
             not_found    nothing acceptable on the indexer; loose candidates
                          listed for a person, never grabbed
             found        grabbed; in Lidarr's queue, not moving yet
             downloading  in Lidarr's queue, with percent
             converting   every file in; waiting for conversion and release
             filing       released into staging; the intake loop files it next
             attention    ingest left it in staging (see the System page)
             library      in the read model; album_id links to it
             have / error

MusicBrainz asks for one request a second and a User-Agent that names the
application; both are honoured and every response is cached in requests.db.
"""
import os, re, sys, json, time, sqlite3, threading, subprocess, urllib.request, urllib.parse, urllib.error
from pathlib import Path

import intake

HERE = Path(__file__).resolve().parent
DB = HERE / 'requests.db'
JUKEBOX_DB = HERE / 'jukebox.db'
LIB_DB = HERE.parent / 'library' / 'library.db'
sys.path.insert(0, str(HERE.parent / 'library'))
import merge                                    # norm_artist / norm_album_safe, as ingest and etl use

MB = 'https://musicbrainz.org/ws/2/'
UA = 'MusicMate/1.0 ( https://github.com/craiglindsay/MusicMate )'
DAY = 86400
OPEN = ('asking', 'searching', 'not_found', 'found', 'downloading', 'converting', 'filing', 'attention', 'have')

SCHEMA = """
CREATE TABLE IF NOT EXISTS request (
  id INTEGER PRIMARY KEY, rgid TEXT UNIQUE, artist TEXT, album TEXT, year TEXT,
  asked REAL, who TEXT, state TEXT, percent INTEGER, message TEXT, lidarr_id INTEGER,
  candidates TEXT, album_id INTEGER, arrived REAL, updated REAL);
CREATE TABLE IF NOT EXISTS mb (url TEXT PRIMARY KEY, body TEXT, at REAL);
"""


def db():
    c = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


# ------------------------------------------------------------------ MusicBrainz
_mb_lock = threading.Lock()
_mb_last = [0.0]


def mb(path, params, ttl=7 * DAY):
    params = dict(params, fmt='json')
    url = MB + path + '?' + urllib.parse.urlencode(params)
    c = db()
    try:
        r = c.execute('SELECT body, at FROM mb WHERE url=?', (url,)).fetchone()
        if r and time.time() - r['at'] < ttl:
            return json.loads(r['body'])
        with _mb_lock:                              # one request a second, across threads
            wait = 1.1 - (time.time() - _mb_last[0])
            if wait > 0:
                time.sleep(wait)
            body = None
            for attempt in range(3):                # 503 is MusicBrainz saying "slower", even at 1/s
                try:
                    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        body = resp.read().decode('utf-8')
                    break
                except urllib.error.HTTPError as e:
                    if e.code != 503 or attempt == 2:
                        raise
                    time.sleep(2 + 2 * attempt)
                finally:
                    _mb_last[0] = time.time()
        c.execute('INSERT OR REPLACE INTO mb VALUES (?,?,?)', (url, body, time.time()))
        c.commit()
        return json.loads(body)
    except Exception:
        if r:                                       # stale beats nothing
            return json.loads(r['body'])
        raise
    finally:
        c.close()


def _studio(rg):
    return rg.get('primary-type') == 'Album' and not rg.get('secondary-types')


def discography(mbid):
    """The artist's studio albums, oldest first."""
    out, offset = [], 0
    while True:
        d = mb('release-group', {'artist': mbid, 'type': 'album', 'limit': 100, 'offset': offset})
        rgs = d.get('release-groups') or []
        out += [{'rgid': g['id'], 'title': g['title'], 'year': (g.get('first-release-date') or '')[:4]}
                for g in rgs if _studio(g)]
        offset += len(rgs)
        if not rgs or offset >= (d.get('release-group-count') or 0) or offset >= 500:
            break
    return sorted(out, key=lambda g: (g['year'] or '9999', g['title']))


def artist_mbid(name):
    norm = merge.norm_artist(name)
    try:
        c = sqlite3.connect(f'file:{LIB_DB}?mode=ro', uri=True, timeout=10)
        r = c.execute('SELECT mbid FROM mb_artist WHERE norm=? AND mbid IS NOT NULL ORDER BY score DESC LIMIT 1',
                      (norm,)).fetchone()
        c.close()
        if r:
            return r[0]
    except sqlite3.Error:
        pass
    d = mb('artist', {'query': f'artist:"{name}"', 'limit': 5})
    for a in d.get('artists') or []:
        if merge.norm_artist(a.get('name', '')) == norm and int(a.get('score') or 0) >= 90:
            return a['id']
    return None


# ------------------------------------------------------------------ what we have
def ledger():
    p = intake.PIPELINE / 'processed_albums.txt'
    try:
        return {ln.split(None, 1)[0] for ln in open(p, encoding='utf-8') if ln.strip()}
    except OSError:
        return set()


def _jdb():
    c = sqlite3.connect(f'file:{JUKEBOX_DB}?mode=ro', uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


# The library often holds a reissue under its edition's name ("Harvest 50th
# Anniversary Edition", Neil Young, 31 Oct 2026) while MusicBrainz lists the
# album plainly, so ownership also compares titles with the edition wording cut.
_EDITION = re.compile(r"\s*[\(\[:-]?\s*\b(?:\d+(?:st|nd|rd|th)\s+anniversary|super\s+deluxe|deluxe|expanded|"
                      r"remaster(?:ed)?|anniversary|special|collector'?s|legacy|definitive|"
                      r"bonus\s+tracks?|(?:\d{4}\s+)?edition|reissue)\b.*$", re.I)


def core_title(title):
    """norm_album_safe of the title without its edition wording; the plain
    normalised title when cutting would leave nothing."""
    cut = _EDITION.sub('', title or '').strip()
    return merge.norm_album_safe(cut or title)


def library_album(artist, album, loose=False):
    """The read model's album for an artist + title, or None. Titles compare as
    etl and ingest normalise them. loose: an artist spelt differently (ingest's
    aliases) still matches when only one album in the library has that title;
    right for a request arriving, wrong for search (every band's Rumours)."""
    na, nt = merge.norm_artist(artist), merge.norm_album_safe(album)
    idx = _title_index()
    same = idx.get(nt) or idx.get(core_title(album)) or []
    for aid, norm in same:
        if norm == na:
            return aid
    return same[0][0] if (loose and len(same) == 1) else None


_titles = {'mtime': None, 'index': {}}


def _title_index():
    """normalised album title -> [(album id, artist norm)], rebuilt with the read model"""
    try:
        m = JUKEBOX_DB.stat().st_mtime
    except OSError:
        return {}
    if _titles['mtime'] != m:
        idx = {}
        c = _jdb()
        try:
            for r in c.execute('SELECT album.id, album.title, artist.norm FROM album '
                               'JOIN artist ON artist.id = album.artist_id'):
                idx.setdefault(merge.norm_album_safe(r['title']), []).append((r['id'], r['norm']))
                core = core_title(r['title'])
                if core != merge.norm_album_safe(r['title']):
                    idx.setdefault(core, []).append((r['id'], r['norm']))
        finally:
            c.close()
        _titles.update(mtime=m, index=idx)
    return _titles['index']


def owned_titles(artist_id):
    c = _jdb()
    try:
        out = {}
        for title, aid in c.execute('SELECT title, id FROM album WHERE artist_id=? ORDER BY id', (artist_id,)):
            out.setdefault(merge.norm_album_safe(title), aid)
            out.setdefault(core_title(title), aid)
        return out
    finally:
        c.close()


def _requested():
    c = db()
    try:
        return {r['rgid']: dict(r) for r in c.execute('SELECT * FROM request')}
    finally:
        c.close()


def mark(albums, owned=None, artist=None):
    """Give each MusicBrainz album a state: library (with album_id), have
    (in the ledger), requested (with the request's state), or missing."""
    led, req = ledger(), _requested()
    for g in albums:
        aid = (owned or {}).get(merge.norm_album_safe(g['title'])) or (owned or {}).get(core_title(g['title']))
        if aid is None and artist and owned is None:
            aid = library_album(artist, g['title'])
        if aid:
            g.update(state='library', album_id=aid)
        elif g['rgid'] in req:
            r = req[g['rgid']]
            g.update(state='requested', request=r['state'], album_id=r['album_id'])
        elif g['rgid'] in led:
            g['state'] = 'have'
        else:
            g['state'] = 'missing'
    return albums


def for_artist(artist_id):
    c = _jdb()
    try:
        ar = c.execute('SELECT id, name FROM artist WHERE id=?', (artist_id,)).fetchone()
    finally:
        c.close()
    if not ar:
        return None
    mbid = artist_mbid(ar['name'])
    if not mbid:
        return {'mbid': None, 'albums': []}
    albums = mark(discography(mbid), owned_titles(artist_id))
    # filed under another artist entry (a collaboration, a spelling): the
    # library-wide lookup by this artist's name still finds it
    for g in albums:
        if g['state'] != 'library':
            aid = library_album(ar['name'], g['title'])
            if aid:
                g.update(state='library', album_id=aid)
    return {'mbid': mbid, 'albums': albums}


def mb_artist(mbid):
    a = mb('artist/' + mbid, {})
    name = a.get('name') or ''
    albums = discography(mbid)
    # albums the library holds under this name, if the artist is in it at all
    c = _jdb()
    try:
        r = c.execute('SELECT id FROM artist WHERE norm=?', (merge.norm_artist(name),)).fetchone()
    finally:
        c.close()
    return {'mbid': mbid, 'name': name, 'disambiguation': a.get('disambiguation') or '',
            'artist_id': r['id'] if r else None,
            'albums': mark(albums, owned_titles(r['id']) if r else {})}


def _credit(ac):
    return ''.join((x.get('name') or '') + (x.get('joinphrase') or '') for x in (ac or []))


def _words(s):
    return re.findall(r'[a-z0-9]+', merge.strip_accents(s or '').lower())


def _split_artist(q, names):
    """If the query names an artist ("dreams fleetwood mac"), return (name,
    the rest). The longest name whose words appear in a row in the query wins."""
    qw = _words(q)
    best = None
    for n in names:
        nw = _words(n)
        if not nw or len(nw) > len(qw):
            continue
        for i in range(len(qw) - len(nw) + 1):
            if qw[i:i + len(nw)] == nw and (best is None or len(nw) > len(_words(best[0]))):
                rest = qw[:i] + qw[i + len(nw):]
                best = (n, ' '.join(rest))
    return best


def _q(s):
    return s.replace('"', ' ').replace('\\', ' ')      # quotes and backslashes are Lucene syntax


def search(q):
    """Artists, studio albums and songs on MusicBrainz, each marked against the
    library. Free text cannot say which words are the artist, so a query that
    contains a known artist's name is searched within that artist; that is
    what makes "dreams fleetwood mac" find Rumours rather than covers. Songs
    rank by how many releases carry the recording: MusicBrainz has no
    popularity, but a famous recording is on dozens of releases and a cover on
    one. Three requests, so about four seconds uncached."""
    q = q.strip()
    c = _jdb()
    try:
        lib_artists = {r['norm']: (r['id'], r['name']) for r in c.execute('SELECT id, name, norm FROM artist')}
    finally:
        c.close()
    artists = []
    for a in (mb('artist', {'query': _q(q), 'limit': 8}, ttl=DAY).get('artists') or [])[:8]:
        if int(a.get('score') or 0) < 70:
            continue
        lib = lib_artists.get(merge.norm_artist(a['name']))
        artists.append({'mbid': a['id'], 'name': a['name'], 'disambiguation': a.get('disambiguation') or '',
                        'artist_id': lib[0] if lib else None})
    # which artist, if any, the query names
    names = [v[1] for v in lib_artists.values()] + [a['name'] for a in artists[:3]]
    hit = _split_artist(q, names)
    arid, rest = None, q
    if hit and hit[1]:
        mbids = {a['name']: a['mbid'] for a in artists}
        arid = mbids.get(hit[0]) or artist_mbid(hit[0])
        rest = hit[1] if arid else q
    scope = f' AND arid:{arid}' if arid else ''
    albums = []
    # a bare name may be an artist AND a title (Rumours), so titles are searched either way
    for g in (mb('release-group', {'query': f'releasegroup:"{_q(rest)}"{scope} AND primarytype:album',
                                   'limit': 15}, ttl=DAY).get('release-groups') or []):
        if not _studio(g) or int(g.get('score') or 0) < 70:
            continue
        albums.append({'rgid': g['id'], 'title': g['title'], 'artist': _credit(g.get('artist-credit')),
                       'year': (g.get('first-release-date') or '')[:4], 'n': g.get('count') or 0})
    albums.sort(key=lambda a: -a['n'])          # releases in the group: the nearest thing to popularity
    groups = {}
    for rec in (mb('recording', {'query': f'recording:"{_q(rest)}"{scope} AND status:official',
                                 'limit': 100}, ttl=DAY).get('recordings') or []):
        if int(rec.get('score') or 0) < 70:
            continue
        artist = _credit(rec.get('artist-credit'))
        k = (merge.norm_artist(artist), re.sub(r'[^a-z0-9]', '', rec['title'].lower()))
        g = groups.setdefault(k, {'title': rec['title'], 'artist': artist, 'n': 0, 'best': None})
        for rel in rec.get('releases') or []:
            g['n'] += 1
            rg = rel.get('release-group') or {}
            d = rel.get('date') or '9999'
            if _studio(rg) and (g['best'] is None or d < g['best'][0]):
                g['best'] = (d, rg)
    songs, seen = [], set()
    for g in sorted(groups.values(), key=lambda g: -g['n']):
        if not g['best'] or g['best'][1]['id'] in seen:
            continue
        seen.add(g['best'][1]['id'])
        d, rg = g['best']
        songs.append({'title': g['title'], 'artist': g['artist'], 'rgid': rg['id'], 'album': rg.get('title'),
                      'year': d[:4] if d != '9999' else ''})
    for x in albums:
        mark([x], artist=x['artist'])
    for s_ in songs:
        g = {'rgid': s_['rgid'], 'title': s_['album']}
        mark([g], artist=s_['artist'])
        s_.update(state=g['state'], album_id=g.get('album_id'), request=g.get('request'))
    return {'artists': artists, 'albums': albums[:12], 'songs': songs[:10],
            'within': hit[0] if (hit and arid) else None}


# ------------------------------------------------------------------ requests
_ask = threading.Semaphore(2)


def _script(args, timeout):
    p = intake.PIPELINE / 'request_album.py'
    if not p.exists():
        return None
    r = subprocess.run([sys.executable, str(p)] + args, cwd=str(intake.PIPELINE), capture_output=True,
                       text=True, encoding='utf-8', errors='replace', timeout=timeout,
                       env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for line in reversed(r.stdout.splitlines()):
        if line.startswith('{'):
            return json.loads(line)
    return {'status': 'error', 'message': (r.stderr or r.stdout or 'no output')[-300:]}


def _update(rgid, **kw):
    kw['updated'] = time.time()
    c = db()
    try:
        c.execute('UPDATE request SET ' + ', '.join(k + '=?' for k in kw) + ' WHERE rgid=?',
                  list(kw.values()) + [rgid])
        c.commit()
    finally:
        c.close()


def _ask_run(rgid):
    with _ask:
        try:
            res = _script([rgid], timeout=900)
        except Exception as e:
            res = {'status': 'error', 'message': str(e)}
        if res is None:
            res = {'status': 'error', 'message': 'the download pipeline (request_album.py) is not installed'}
        _update(rgid, state=res.get('status') or 'error', message=res.get('message'),
                lidarr_id=res.get('lidarrAlbumId'),
                candidates=json.dumps(res.get('candidates') or []) or None)
        intake.log(f"request {rgid}: {res.get('status')} {res.get('message') or ''}")


def ask(rgid, artist, album, year='', who=''):
    if not re.match(r'^[0-9a-f-]{36}$', rgid or ''):
        raise ValueError('not a release-group id')
    c = db()
    try:
        r = c.execute('SELECT state FROM request WHERE rgid=?', (rgid,)).fetchone()
        if r and r['state'] not in ('error', 'not_found'):
            return False                            # already asked and going
        c.execute('INSERT OR REPLACE INTO request (rgid, artist, album, year, asked, who, state, message, updated) '
                  'VALUES (?,?,?,?,?,?,?,?,?)', (rgid, artist, album, year, time.time(), who, 'asking',
                                                 'asking the download pipeline', time.time()))
        c.commit()
    finally:
        c.close()
    intake.log(f'request from {who or "?"}: {artist} - {album} ({rgid})')
    threading.Thread(target=_ask_run, args=(rgid,), daemon=True, name='request').start()
    return True


_refresh = {'at': 0.0, 'busy': False}


def refresh(force=False):
    """Work out each open request's state. At most every 15 seconds."""
    if _refresh['busy'] or (not force and time.time() - _refresh['at'] < 15):
        return
    _refresh['busy'] = True
    try:
        rows = [r for r in _requested().values() if r['state'] in OPEN]
        if not rows:
            return
        led = ledger()
        stat = {}
        need = [r['rgid'] for r in rows]
        if need:
            try:
                stat = _script(['--status'] + need, timeout=120) or {}
            except Exception:
                stat = {}
        staged = {(merge.norm_album_safe(re.sub(r'\s*\(\d{4}\)\s*$', '', b))): (a, b)
                  for a, b in intake.staging_albums()}
        attention = {(x['artist'], x['album']): x for x in intake.STATE['attention']}
        for r in rows:
            g, now = r['rgid'], time.time()
            aid = library_album(r['artist'], r['album'], loose=True)
            if aid:
                _update(g, state='library', album_id=aid, arrived=r['arrived'] or now, percent=None,
                        message='in your library')
                continue
            s = stat.get(g) or {}
            if r['state'] == 'asking':
                # request_album.py is still minding Lidarr's monitoring (up to
                # four minutes), but the queue can already say it was grabbed
                q = s.get('queue')
                if q:
                    pct = q.get('percent') or 0
                    _update(g, state='downloading' if pct else 'found', percent=pct,
                            message=f"downloading ({q.get('status') or 'queued'})" if pct else 'found a release; queued')
                    continue
                if now - (r['asked'] or now) < 1200:
                    continue
                # asking for 20 minutes: its script died with a server restart;
                # Lidarr has the request either way, so judge it like the rest
            st = staged.get(merge.norm_album_safe(r['album']))
            if st and st in attention and attention[st].get('outcome'):
                _update(g, state='attention', message=attention[st]['why'])
            elif st or g in led or s.get('ledger'):
                _update(g, state='filing', message='downloaded; being filed into the library', percent=None)
            elif s.get('queue'):
                q = s['queue']
                pct = q.get('percent') or 0
                _update(g, state='downloading' if pct else 'found', percent=pct,
                        message=f"downloading ({q.get('status') or 'queued'})" if pct else 'found a release; queued')
            elif s.get('total') and s.get('files') == s.get('total'):
                _update(g, state='converting', percent=None, message='downloaded; converting')
            elif r['state'] in ('asking', 'found', 'downloading', 'converting', 'filing'):
                _update(g, state='searching', percent=None, message='waiting for a release')
    finally:
        _refresh.update(at=time.time(), busy=False)


def listing():
    c = db()
    try:
        rows = [dict(r) for r in c.execute('SELECT * FROM request ORDER BY asked DESC LIMIT 200')]
    finally:
        c.close()
    for r in rows:
        try:
            r['candidates'] = json.loads(r['candidates'] or '[]')
        except ValueError:
            r['candidates'] = []
    return rows
