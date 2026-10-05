"""Lyrics lookup, cached permanently including misses.

LRCLIB is the source that matters: free, no key, and it returns TIME-SYNCED lyrics,
which is what lets a line highlight as it is sung. Genius cannot do that -- its API
deliberately returns no lyric text at all, only metadata and a link, which I verified
rather than assumed (lyrics_state says "complete", there is no lyrics field).

Note: lrclib.net is currently unreachable from this machine -- curl cannot complete a
TLS handshake with it while other hosts are fine, so something on the network is in
the way. The code is written to work when that clears; until then every lookup falls
back to Genius metadata plus a link, and the miss is cached with a short TTL so it
retries later rather than hammering a blocked host.
"""
import os, re, json, time, sqlite3, urllib.request, urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRATCH = HERE.parent
CACHE = HERE / 'lyrics.db'
UA = 'SignalJukebox/1.0 (personal music player)'
MISS_TTL = 7 * 86400          # retry a miss after a week, not on every play

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS lyric (
  skey     TEXT PRIMARY KEY,
  source   TEXT,
  synced   TEXT,       -- LRC format, timestamps included
  plain    TEXT,
  url      TEXT,       -- where a human can read them
  art      TEXT,       -- Genius song art, useful when the album has none
  status   TEXT,       -- 'ok' | 'none' | 'error'
  fetched  REAL
);
"""

def db():
    c = sqlite3.connect(CACHE, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c

def _get(url, headers=None, timeout=15):
    r = urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.load(resp)

# ---------------------------------------------------------------- sources
def from_lrclib(artist, title, album=None, duration=None):
    q = {'artist_name': artist, 'track_name': title}
    if album:
        q['album_name'] = album
    if duration:
        q['duration'] = int(duration)
    try:
        d = _get('https://lrclib.net/api/get?' + urllib.parse.urlencode(q))
    except Exception:
        # the exact-match endpoint 404s readily; the search endpoint is more forgiving
        try:
            res = _get('https://lrclib.net/api/search?' + urllib.parse.urlencode(
                {'track_name': title, 'artist_name': artist}))
        except Exception:
            return None
        if not res:
            return None
        # prefer the candidate closest in length -- that is what disambiguates
        # a studio cut from a live version of the same song
        if duration:
            res.sort(key=lambda r: abs((r.get('duration') or 0) - duration))
        d = res[0]
    if not d:
        return None
    return {'source': 'lrclib', 'synced': d.get('syncedLyrics'),
            'plain': d.get('plainLyrics'), 'url': None}

def genius_token():
    try:
        cid, sec = [l.strip() for l in open(SCRATCH / '.genius_client') if l.strip()]
    except Exception:
        return None
    try:
        data = urllib.parse.urlencode({'grant_type': 'client_credentials',
                                       'client_id': cid, 'client_secret': sec}).encode()
        req = urllib.request.Request('https://api.genius.com/oauth/token', data=data,
                                     headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)['access_token']
    except Exception:
        return None

_TOK = None
def from_genius(artist, title):
    """Metadata and a link only. The Genius API does not return lyric text."""
    global _TOK
    if _TOK is None:
        _TOK = genius_token() or ''
    if not _TOK:
        return None
    try:
        d = _get('https://api.genius.com/search?' + urllib.parse.urlencode(
            {'q': f'{artist} {title}'}), {'Authorization': 'Bearer ' + _TOK})
    except Exception:
        return None
    hits = d.get('response', {}).get('hits', [])
    na, nt = _norm(artist), _norm(title)
    for h in hits:
        r = h.get('result', {})
        if not r:
            continue
        ga = _norm(r.get('primary_artist', {}).get('name', ''))
        gt = _norm(r.get('title', ''))
        if (na in ga or ga in na) and (nt in gt or gt in nt):
            return {'source': 'genius', 'synced': None, 'plain': None,
                    'url': r.get('url'), 'art': r.get('song_art_image_url')}
    return None

def _norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())

# ---------------------------------------------------------------- api
def lookup(skey, artist, title, album=None, duration=None, force=False):
    c = db()
    try:
        row = c.execute('SELECT * FROM lyric WHERE skey=?', (skey,)).fetchone()
        if row and not force:
            fresh = row['status'] == 'ok' or (time.time() - (row['fetched'] or 0)) < MISS_TTL
            if fresh:
                return dict(row)

        got = None
        try:
            got = from_lrclib(artist, title, album, duration)
        except Exception:
            got = None
        gen = None
        if not got or not (got.get('synced') or got.get('plain')):
            gen = from_genius(artist, title)

        rec = {'skey': skey, 'source': None, 'synced': None, 'plain': None,
               'url': None, 'art': None, 'status': 'none', 'fetched': time.time()}
        if got and (got.get('synced') or got.get('plain')):
            rec.update(source='lrclib', synced=got.get('synced'),
                       plain=got.get('plain'), status='ok')
        if gen:
            rec['url'] = gen.get('url')
            rec['art'] = gen.get('art')
            if rec['status'] != 'ok':
                rec['source'] = 'genius'
        c.execute('INSERT OR REPLACE INTO lyric (skey,source,synced,plain,url,art,'
                  'status,fetched) VALUES (?,?,?,?,?,?,?,?)',
                  (rec['skey'], rec['source'], rec['synced'], rec['plain'],
                   rec['url'], rec['art'], rec['status'], rec['fetched']))
        c.commit()
        return rec
    finally:
        c.close()

LRC_LINE = re.compile(r'\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)')
LRC_TAG = re.compile(r'\[(\d+):(\d+(?:\.\d+)?)\][ \t]*')

def parse_synced(lrc):
    """LRC text -> [{t: seconds, line: str}] in time order, which is what lets a
    line highlight. A line may carry several timestamps -- the LRC shorthand
    for a line sung more than once, "[01:39.51][00:37.79]Can't hurt you now"
    -- and becomes one entry per time. Only the first was read, and the rest
    were shown as words on the karaoke screen (10,000 Maniacs, Because the
    Night, 23 Sep 2026). align.py uses this same function, so line indexes in
    words.json match the screen's."""
    out = []
    for raw in (lrc or '').splitlines():
        s = raw.strip()
        times, pos = [], 0
        while True:
            m = LRC_TAG.match(s, pos)
            if not m:
                break
            times.append(int(m.group(1)) * 60 + float(m.group(2)))
            pos = m.end()
        text = s[pos:].strip()
        # a section label ("<Instrumental>", "<Bridge>") is not sung: it is a gap
        if re.fullmatch(r'<[^<>]*>', text):
            text = ''
        # a transcriber's doubt ("a ring [on the] telephone"): the words are sung
        text = re.sub(r'\s+', ' ', re.sub(r'\[([^\[\]]*)\]', r'\1', text)).strip()
        for t in times:
            out.append({'t': round(t, 2), 'line': text})
    out.sort(key=lambda x: x['t'])          # stable: same-time lines keep file order
    return out

if __name__ == '__main__':
    import sys
    r = lookup('test', 'Depeche Mode', 'Enjoy the Silence', 'Violator', 376)
    print({k: (v[:60] + '…' if isinstance(v, str) and len(v) > 60 else v)
           for k, v in r.items()})
