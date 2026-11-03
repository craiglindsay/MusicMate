"""The DJ (31 Oct 2026): a station grown from one seed, with a voice between sets.

Pick a seed -- an artist, a song, an album, a genre shelf, an era or a whole
library (Christmas) -- and the DJ plays three to five well-known songs, talks
about what just played and what is coming, and carries on. Like Pandora, it
starts close to the seed and wanders: an artist seed begins with that artist
and the similar artists the library holds, and each set adds the similar
artists of what has played, so the station drifts outward a ring at a time.
A genre or era seed draws from that shelf or decade, with the odd jump to a
similar artist outside it.

"Popular" is the same ranking the artist Radio uses (Deezer's top songs,
matched to the copies you own, then your own play counts), so a set leans on
the songs people know.

The talk is a short MP3 that sits in the queue between sets like a song, so
it plays on a Sonos room as well as this device. Claude writes the script
from facts the library holds (years, albums, shelves, chart rank, the artist
bio) and a Microsoft neural voice (edge-tts, online) speaks it. Without an
API key, or when the call fails, a plainer script is filled from the same
facts; without the voice, the clip is left out and the music carries on.

A clip is a queue entry with a NEGATIVE id: /api/stream/-<n> and
/media/-<n>.mp3 serve it, /api/tracks hydrates it, and the player skips the
things a clip has no use for (hearts, play counts, lyrics).

Key: ANTHROPIC_API_KEY, or the repo-root file .anthropic_key (gitignored).
"""
import os, re, json, time, random, sqlite3, asyncio, threading, collections, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / 'jukebox.db'
CLIPS = HERE / 'cache' / 'dj'
KEYFILE = HERE.parent / '.anthropic_key'
MODEL = 'claude-opus-5-5'
VOICE = 'en-US-AndrewNeural'
VOICES = ['en-US-AndrewNeural', 'en-US-AvaNeural', 'en-US-GuyNeural', 'en-US-JennyNeural',
          'en-GB-RyanNeural', 'en-GB-SoniaNeural', 'en-AU-WilliamNeural', 'en-AU-NatashaNeural']
CLIP_TTL = 24 * 3600
SESSION_TTL = 12 * 3600

_lock = threading.Lock()
SESSIONS = {}
CLIPMETA = {}            # clip id (negative) -> {'path', 'text', 'duration', 'label', 'ready': Event}
_radio = None            # server.artist_radio, handed in by init()
_main_pool = 'VariousArtists'


def init(radio, main_pool):
    global _radio, _main_pool
    _radio, _main_pool = radio, main_pool
    CLIPS.mkdir(parents=True, exist_ok=True)
    cut = time.time() - CLIP_TTL
    for p in CLIPS.glob('*.mp3'):
        try:
            if p.stat().st_mtime < cut:
                p.unlink()
        except OSError:
            pass


def _db():
    c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _rows(sql, *args):
    c = _db()
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _key():
    k = os.environ.get('ANTHROPIC_API_KEY')
    if k:
        return k.strip()
    try:
        return KEYFILE.read_text(encoding='utf-8').strip() or None
    except OSError:
        return None


# ------------------------------------------------------------------ the seed
def describe_seed(kind, sid):
    """(label, pool) for a seed, or None when it names nothing."""
    if kind in ('artist',):
        r = _rows('SELECT name FROM artist WHERE id=?', sid)
        return r and (r[0]['name'], None)
    if kind == 'song':
        r = _rows('SELECT track.title, COALESCE(NULLIF(track.track_artist,\'\'), artist.name) artist, album.pool '
                  'FROM track JOIN album ON album.id=track.album_id JOIN artist ON artist.id=album.artist_id '
                  'WHERE track.id=?', sid)
        return r and ('%s by %s' % (r[0]['title'], r[0]['artist']), r[0]['pool'])
    if kind == 'album':
        r = _rows('SELECT album.title, artist.name, album.pool FROM album JOIN artist ON artist.id=album.artist_id '
                  'WHERE album.id=?', sid)
        return r and ('%s by %s' % (r[0]['title'], r[0]['name']), r[0]['pool'])
    if kind == 'genre':
        r = _rows('SELECT shelf FROM genre WHERE id=?', sid)
        return r and (r[0]['shelf'], None)
    if kind == 'era':
        return ('the %ds' % (int(sid) // 10 * 10), None) if str(sid).isdigit() and int(sid) >= 1900 else None
    if kind == 'lib':
        return (sid, sid) if _rows('SELECT 1 FROM album WHERE pool=? LIMIT 1', sid) else None
    return None


class Session:
    def __init__(self, kind, sid, voice):
        self.id = '%x' % random.getrandbits(48)
        self.kind, self.sid, self.voice = kind, sid, voice if voice in VOICES else VOICE
        label, pool = describe_seed(kind, sid) or (None, None)
        if not label:
            raise ValueError('no such seed')
        self.label = label
        self.touched = time.time()
        self.played = set()           # track ids
        self.titles = set()           # artist|title keys, so a song on two albums plays once
        self.recent = collections.deque(maxlen=7)   # artist ids, most recent last
        self.ring = {}                # artist id -> distance from the seed
        self.radios = {}              # artist id -> ranked songs (server.artist_radio)
        self.talks = []               # what the DJ has said, so it does not repeat itself
        self.known = {}               # artist id -> how many of its chart songs the library holds
        self.nset = 0
        self.pending = None           # the set after the one handed out: the talk teases it
        self.first = None             # a song seed opens with that song
        # Everything handed out, in order, and the speakers the station plays on
        # (2 Nov 2026). On a speaker the server keeps the station going itself
        # (server._dj_keeper), so it does not stop when the phone sleeps; a page
        # that wakes up catches up from the log instead of asking for a new set.
        self.log = []
        self.bound = None             # [ip, ...] of the Sonos targets, or None
        self.lock = threading.Lock()

        # which albums the station may play from
        if kind == 'lib':
            self.pool = sid
        elif pool and pool != _main_pool:
            self.pool = pool          # a Christmas song seeds a Christmas station
        else:
            self.pool = _main_pool
        self.albums = {r['id']: r for r in _rows(
            'SELECT id, year, decade, genre_id FROM album WHERE pool=?', self.pool)}
        self.inside = None            # the seed's own albums, for genre/era stations
        if kind == 'genre':
            self.inside = {a for a, r in self.albums.items() if r['genre_id'] == int(sid)}
        elif kind == 'era':
            dec = int(sid) // 10 * 10
            self.inside = {a for a, r in self.albums.items() if r['decade'] == dec or
                           (r['year'] and dec <= r['year'] < dec + 10)}

        if kind in ('artist', 'song', 'album'):
            if kind == 'artist':
                aid = int(sid)
            else:
                q = ('SELECT album.artist_id a FROM track JOIN album ON album.id=track.album_id WHERE track.id=?'
                     if kind == 'song' else 'SELECT artist_id a FROM album WHERE id=?')
                r = _rows(q, sid)
                aid = r[0]['a'] if r else None
            if aid is None:
                raise ValueError('no such seed')
            self.seed_artist = aid
            self.ring[aid] = 0
            self._grow([aid], 1)
            if kind == 'song':
                self.first = int(sid)
            elif kind == 'album':
                songs = [t for t in self._songs(aid) if t['album_id'] == int(sid)]
                if songs:
                    self.first = songs[0]['id']
        else:
            self.seed_artist = None
            album_ids = self.inside if self.inside is not None else set(self.albums)
            if not album_ids:
                raise ValueError('nothing to play there')
            ph = ','.join('?' * len(album_ids))
            for r in _rows(f'SELECT artist_id a, COUNT(*) n FROM album WHERE id IN ({ph}) GROUP BY artist_id',
                           *album_ids):
                self.ring[r['a']] = 0
            # a little room to wander from the start
            # until an artist's songs are looked at, having a chart list at all
            # is the guess at how well known it is
            charted = {r['artist_id'] for r in _rows('SELECT DISTINCT artist_id FROM artist_top')}
            for a in self.ring:
                self.known[a] = 3 if a in charted else 0
            some = random.sample(list(self.ring), min(25, len(self.ring)))
            self._grow(some, 2)

    # ---- the artist graph
    def _grow(self, artists, ring):
        """Add the similar artists the library holds, one ring further out."""
        if not artists:
            return
        ph = ','.join('?' * len(artists))
        for r in _rows('SELECT DISTINCT a.id FROM artist_similar s JOIN artist a ON a.norm=s.norm '
                       f'WHERE s.artist_id IN ({ph}) AND s.pos < 15', *artists):
            if r['id'] not in self.ring:
                self.ring[r['id']] = ring

    def _songs(self, aid):
        if aid not in self.radios:
            try:
                got = _radio(aid, 40)
            except Exception:
                got = []
            tops = {r['norm'] for r in _rows('SELECT norm FROM artist_top WHERE artist_id=?', aid)}
            keep = []
            for t in got:
                if t['album_id'] in self.albums:
                    t['top'] = _norm(t['title']) in tops
                    keep.append(t)
            self.radios[aid] = keep
            self.known[aid] = sum(1 for t in keep if t['top'])
        return self.radios[aid]

    # ---- choosing
    def _pick_song(self, aid, strict):
        songs = self._songs(aid)
        cands = []
        for rank, t in enumerate(songs[:18]):
            if t['id'] in self.played or _tkey(t) in self.titles:
                continue
            if strict and self.inside is not None and t['album_id'] not in self.inside:
                continue
            # the well-known ones first; a song off the chart list only now and then
            cands.append((t, (1.0 if t.get('top') else .12) / (1 + rank * .45)))
        if not cands:
            return None
        return _wchoice(cands)

    def _artist_weight(self, aid):
        ring = self.ring.get(aid, 3)
        if self.seed_artist is not None:
            # near the seed at first, further out as the station runs
            spread = min(1.0, self.nset / 6.0)
            w = [1.4 - .6 * spread, 1.0, .35 + .5 * spread, .15 + .4 * spread][min(ring, 3)]
        else:
            w = 1.0 if ring == 0 else .18
            # a shelf or a decade holds hundreds of artists, most of them
            # obscure: lean on the ones with songs people know
            k = self.known.get(aid)
            w *= .5 if k is None else (.06 + min(1.0, k / 6.0))
        if aid in self.recent:
            w *= .03 if list(self.recent)[-3:].count(aid) else .3
        return w

    def next_set(self):
        n = random.choice((3, 4, 4, 5))
        out, used = [], set()
        if self.first and self.first not in self.played:
            r = self._first_track()
            if r:
                out.append(r)
                used.add(r['artist_id'])
        # the seed artist opens the station and keeps coming back, less often
        # as it wanders (one artist among a dozen similar ones was rarely drawn)
        sa = self.seed_artist
        if sa is not None and not out and random.random() < max(.3, 1.0 - .15 * self.nset):
            t = self._pick_song(sa, False)
            if t:
                out.append(t)
                used.add(sa)
                self.played.add(t['id'])
                self.titles.add(_tkey(t))
        tries = 0
        while len(out) < n and tries < 60:
            tries += 1
            pool = [(a, self._artist_weight(a)) for a in self.ring if a not in used]
            if not pool:
                break
            aid = _wchoice(pool)
            strict = self.inside is not None and self.ring.get(aid, 3) == 0 and random.random() < .9
            t = self._pick_song(aid, strict)
            if not t:
                used.add(aid)
                continue
            used.add(aid)
            out.append(t)
            self.played.add(t['id'])
            self.titles.add(_tkey(t))
        for t in out:
            self.recent.append(t['artist_id'])
        # the station wanders: what just played brings its own neighbours
        self.nset += 1
        if self.seed_artist is not None or random.random() < .5:
            self._grow([t['artist_id'] for t in out], min(3, self.nset + 1))
        return out

    def _first_track(self):
        r = _rows('SELECT track.id, track.title, track.duration, album.id album_id, album.title album, album.year, '
                  "COALESCE(NULLIF(track.track_artist,''), artist.name) artist, album.artist_id "
                  'FROM track JOIN album ON album.id=track.album_id JOIN artist ON artist.id=album.artist_id '
                  'WHERE track.id=?', self.first)
        if not r:
            return None
        t = r[0]
        self.played.add(t['id'])
        self.titles.add(_tkey(t))
        return t


def _norm(title):
    return re.sub(r'[^a-z0-9]', '', re.sub(r'[\(\[].*?[\)\]]', ' ', (title or '').lower()))


def _tkey(t):
    s = re.sub(r'[\(\[].*?[\)\]]', ' ', (t.get('title') or '').lower())
    return '%s|%s' % (t.get('artist_id'), re.sub(r'[^a-z0-9]', '', s))


def _wchoice(pairs):
    tot = sum(w for _, w in pairs)
    x = random.random() * tot
    for v, w in pairs:
        x -= w
        if x <= 0:
            return v
    return pairs[-1][0]


# ------------------------------------------------------------------ facts
def _facts(tracks):
    if not tracks:
        return []
    ids = [t['id'] for t in tracks]
    ph = ','.join('?' * len(ids))
    meta = {r['id']: r for r in _rows(
        'SELECT track.id, track.title, album.title album, album.year, genre.shelf genre, album.subgenre, '
        "COALESCE(NULLIF(track.track_artist,''), artist.name) artist, album.artist_id, artist_info.bio "
        'FROM track JOIN album ON album.id=track.album_id JOIN artist ON artist.id=album.artist_id '
        'LEFT JOIN genre ON genre.id=album.genre_id LEFT JOIN artist_info ON artist_info.artist_id=album.artist_id '
        f'WHERE track.id IN ({ph})', *ids)}
    out = []
    for t in tracks:
        m = meta.get(t['id'])
        if not m:
            continue
        f = {'title': m['title'], 'artist': m['artist'], 'album': m['album'], 'year': m['year'],
             'genre': m['subgenre'] or m['genre']}
        top = _rows('SELECT pos FROM artist_top WHERE artist_id=? AND norm=? LIMIT 1', m['artist_id'],
                    re.sub(r'[^a-z0-9]', '', re.sub(r'[\(\[].*?[\)\]]', ' ', (m['title'] or '').lower())))
        if top:
            f['rank_among_artists_songs'] = top[0]['pos'] + 1
        if m['bio']:
            f['artist_bio'] = ' '.join(re.split(r'(?<=[.!?])\s+', m['bio'].strip())[:3])[:600]
        out.append(f)
    return out


SYSTEM = """You are the late-night DJ on MusicMate, a family's private radio station that plays only music from their own library. Between runs of songs you drop in briefly and get out of the way.

Write exactly what the DJ says next, as plain spoken English: no stage directions, no sound effects, no emoji, no markdown, no lists. Keep it short: 20 to 45 words, one breath, two or three sentences. Casual and easy, a little dry, like someone on the radio at midnight who loves this stuff; never a hype man, never a lecture.

Name the artists, not the songs: "That was AC/DC, Duran Duran and Depeche Mode" or just one of them, never a list of titles. Now and then (not every time) slip in one short tidbit about an artist that just played or is coming up: something genuinely interesting, one sentence, to the point. Sometimes skip the tidbit and just do a little DJ talk instead (the time of day you are given, the mood, keeping it rolling). Then point at what is next loosely: an artist, the decade, the feel ("more from the eighties"), not titles. Use the facts given; add general music knowledge only when you are certain it is true, and never invent chart positions, quotes or dates. Vary your openings and phrasing from what you said before. Do not mention being an AI, the library, files or algorithms. Do not greet the listener again after the first time."""


def _prompt(sess, played, coming, intro):
    now = datetime.datetime.now()
    part = ('morning' if now.hour < 12 else 'afternoon' if now.hour < 17 else 'evening' if now.hour < 22 else 'night')
    d = {'station': 'inspired by ' + sess.label, 'time': '%s %s' % (now.strftime('%A'), part),
         'just_played': _facts(played), 'coming_up': _facts(coming[:3])}
    if sess.talks:
        d['what_you_said_before'] = sess.talks[-3:]
    head = ('This is the opening of the station: a quick hello, what the station is built around, '
            'and who we start with.' if intro else
            'The songs in just_played have finished. Talk now.')
    return head + '\n\n' + json.dumps(d, ensure_ascii=False, indent=1)


def _write_claude(sess, played, coming, intro):
    key = _key()
    if not key:
        return None
    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=40.0, max_retries=1)
    r = client.beta.messages.create(
        model=MODEL, max_tokens=4000,
        output_config={'effort': 'low'},
        betas=['server-side-fallback-2026-07-01'], fallbacks='default',
        system=SYSTEM,
        messages=[{'role': 'user', 'content': _prompt(sess, played, coming, intro)}])
    if r.stop_reason == 'refusal':
        return None
    text = ' '.join(b.text for b in r.content if b.type == 'text').strip()
    return re.sub(r'[*_#`]', '', text) or None


_FORMED = re.compile(r'\b(?:formed|founded)\s+in\s+([A-Z][^.;()]*?)\s*,?\s+in\s+(\d{4})')


def _tidbit(facts, upcoming=()):
    """one short true thing from the facts: where a band started, how old a
    song is, a big one; None about half the time, which is the point"""
    out = []
    for f in list(facts) + list(upcoming):
        m = _FORMED.search(f.get('artist_bio') or '')
        if m:
            place = m.group(1).split(',')[0]
            out.append(random.choice(['%s got their start in %s, back in %s.', '%s first got together in %s in %s.'])
                       % (f['artist'], place, m.group(2)))
        if f in upcoming:
            continue
        y = f.get('year')
        if y and datetime.date.today().year - y >= 30:
            out.append(random.choice(['That %s one goes all the way back to %d.', '%s there, from %d.']) % (f['artist'], y))
        if f.get('rank_among_artists_songs') in (1, 2):
            out.append(random.choice(['That was one of the big ones from %s.', 'Still one of the best-loved %s songs.']) % f['artist'])
    return random.choice(out) if out and random.random() < .6 else None


def _write_plain(sess, played, coming, intro):
    """The script without Claude: short and casual, from the same facts,
    artists rather than titles."""
    def names(ts):
        seen = []
        for t in ts:
            if t['artist'] not in seen:
                seen.append(t['artist'])
        seen = seen[:3]
        return seen[0] if len(seen) == 1 else ', '.join(seen[:-1]) + ' and ' + seen[-1]
    hour = datetime.datetime.now().hour
    when = 'this morning' if 5 <= hour < 12 else 'this afternoon' if 12 <= hour < 17 else 'tonight'
    nxt = coming[0]['artist'] if coming else None
    if intro:
        s = random.choice(['Hey, welcome in.', "Hi there, it's your MusicMate DJ.", 'Pull up a chair.']) + \
            " %s, it's all about %s." % (when[0].upper() + when[1:], sess.label)
        if nxt:
            s += random.choice([' We start with %s.', ' Kicking off with %s.']) % nxt
        return s
    if not played:
        return None
    s = random.choice(['That was %s.', 'You just heard %s.', '%s, right there.', 'Some %s for you.']) % names(played)
    t = _tidbit(_facts(played), _facts(coming[:1]))
    if t:
        s += ' ' + t
    elif random.random() < .5:
        s += ' ' + random.choice(['Good company %s.' % when, "Let's keep it rolling.", 'Stay right where you are.',
                                  'Turn it up a little, nobody will mind.', 'No talking over this next bit, promise.'])
    if nxt:
        yrs = sorted(t['year'] for t in coming if t.get('year'))
        if yrs and random.random() < .4:
            s += ' Coming up, more from the %ds.' % (yrs[len(yrs) // 2] // 10 * 10)
        else:
            s += random.choice([' Coming up, %s.', ' Next, %s.', ' %s is on the way.', ' Here comes %s.']) % nxt
    return s


def _speak(text, voice, path):
    import edge_tts

    async def go():
        await edge_tts.Communicate(text, voice, rate='+8%').save(str(path))
    for attempt in range(3):          # edge-tts now and then answers with no audio; a retry voices it
        try:
            asyncio.run(go())
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))


def _duration(path):
    try:
        from mutagen.mp3 import MP3     # mutagen.File's result is falsy when the clip has no tags
        return float(MP3(str(path)).info.length)
    except Exception:
        return None


def _new_clip(label):
    with _lock:
        cid = -(int(time.time() * 1000) % 10 ** 11)
        while cid in CLIPMETA:
            cid -= 1
        CLIPMETA[cid] = {'path': CLIPS / ('%d.mp3' % -cid), 'text': None, 'duration': None,
                         'label': label, 'ready': threading.Event(), 'ok': False}
    return cid


def _make_clip(cid, sess, played, coming, intro):
    c = CLIPMETA[cid]
    try:
        text = None
        try:
            text = _write_claude(sess, played, coming, intro)
        except Exception as e:
            print('dj: claude failed:', type(e).__name__, str(e)[:200], flush=True)
        if not text:
            text = _write_plain(sess, played, coming, intro)
        if not text:
            return
        c['text'] = text
        sess.talks.append(text)
        _speak(text, sess.voice, c['path'])
        c['duration'] = _duration(c['path'])
        c['ok'] = c['path'].exists() and c['path'].stat().st_size > 1000
    except Exception as e:
        print('dj: clip failed:', type(e).__name__, str(e)[:200], flush=True)
    finally:
        c['ready'].set()


def _clip_item(cid, sess):
    c = CLIPMETA[cid]
    return {'id': cid, 'dj': 1, 'title': 'MusicMate DJ', 'artist': 'Inspired by ' + sess.label,
            'duration': c['duration'] or 25}


def _track_item(t):
    return {'id': t['id'], 'title': t['title'], 'duration': t['duration'], 'album_id': t['album_id'],
            'album': t['album'], 'artist': t['artist'], 'artist_id': t['artist_id']}


# ------------------------------------------------------------------ the API
def _prune():
    cut = time.time() - SESSION_TTL
    for k in [k for k, s in SESSIONS.items() if s.touched < cut]:
        SESSIONS.pop(k, None)


def start(kind, sid, voice=None, played=()):
    """A new station: an opening clip (waited for, briefly), the first set, and
    the talk after it, which is written while the first songs play."""
    _prune()
    sess = Session(kind, sid, voice)
    sess.played.update(int(x) for x in played if int(x) > 0)
    SESSIONS[sess.id] = sess
    first = sess.next_set()
    if not first:
        raise ValueError('nothing to play')
    sess.pending = sess.next_set()
    items = []
    if not played:
        cid = _new_clip(sess.label)
        th = threading.Thread(target=_make_clip, args=(cid, sess, [], first, True), daemon=True)
        th.start()
        CLIPMETA[cid]['ready'].wait(25)
        if CLIPMETA[cid]['ok']:
            items.append(_clip_item(cid, sess))
    items += [_track_item(t) for t in first]
    items += _talk_after(sess, first)
    sess.log = list(items)
    return {'session': sess.id, 'label': sess.label, 'items': items}


def _talk_after(sess, played):
    cid = _new_clip(sess.label)
    threading.Thread(target=_make_clip, args=(cid, sess, played, sess.pending or [], False), daemon=True).start()
    # the clip is listed now and generated by the time the set has played; its
    # duration is filled in when the player asks for it
    return [{'id': cid, 'dj': 1, 'title': 'MusicMate DJ', 'artist': 'Inspired by ' + sess.label, 'duration': None}]


_append = None            # server: (ips, track ids) -> adds them to those speakers' queues


def set_appender(fn):
    global _append
    _append = fn


def more(session_id, voice=None, have=None):
    """The next set (the one the last talk promised) and the talk after it.

    `have` is how many of the station's items the asker already holds: a page
    that slept while the server kept a speaker going gets what it missed rather
    than a new set. On a bound station the new set also goes onto the speakers
    here, and 'bound' tells the page not to add it again."""
    sess = SESSIONS.get(session_id)
    if not sess:
        return None
    sess.touched = time.time()
    if voice is not None:
        sess.voice = voice if voice in VOICES else VOICE     # changed in the preferences
    with sess.lock:
        out = {'session': sess.id, 'label': sess.label, 'bound': bool(sess.bound)}
        if have is not None and have < len(sess.log):
            out['items'] = sess.log[have:]
            return out
        cur = sess.pending or sess.next_set()
        sess.pending = sess.next_set()
        if not cur:
            out['items'] = []
            return out
        items = [_track_item(t) for t in cur] + _talk_after(sess, cur)
        sess.log.extend(items)
        if sess.bound and _append:
            try:
                _append(sess.bound, [i['id'] for i in items])
            except Exception as e:
                print('dj: could not add to the speaker:', type(e).__name__, str(e)[:200], flush=True)
        out['items'] = items
        return out


def bind(session_id, ips):
    """The station plays on these speakers now (or on none: the page's own output)."""
    sess = SESSIONS.get(session_id)
    if not sess:
        return False
    sess.touched = time.time()
    sess.bound = list(ips) or None
    return True


def clip(cid, wait=30):
    """(path, meta) of a clip once it is ready, or None."""
    c = CLIPMETA.get(cid)
    if not c:
        p = CLIPS / ('%d.mp3' % -cid)     # made before a restart
        return (p, {'text': None}) if p.exists() else None
    c['ready'].wait(wait)
    return (c['path'], c) if c['ok'] else None


def clip_info(cid):
    c = CLIPMETA.get(cid)
    p = CLIPS / ('%d.mp3' % -cid)
    if not c and not p.exists():
        return None
    return {'id': cid, 'dj': 1, 'title': 'MusicMate DJ', 'artist': (c and 'Inspired by ' + c['label']) or 'MusicMate DJ',
            'duration': (c and c['duration']) or _duration(p) or 25, 'text': c and c['text']}
