"""Find real cover art for albums whose only art is a Windows Media thumbnail.

The folder.jpg files in this library are WMP's 200x200 cache, not cover art: 76% of
albums have nothing larger, and not one exceeds 1000px. Upscaling that is not an
option, so the art has to come from somewhere else.

The match is the whole risk here. Asking iTunes for "Elliott Smith XO" returns
"Either/Or", and "Buddha Bar IV" returns a different Buddha Bar compilation --
confidently, as the first result. Wrong art is worse than small art, so every hit is
checked against the album we actually asked about and anything doubtful is refused.

Nothing is written into D:\\Music by this script; downloads land in a cache.
"""
import os, io, re, sys, json, time, sqlite3, difflib, argparse, collections
import urllib.request, urllib.parse
from PIL import Image
import musiclib, merge

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, 'artcache.db')
ARTDIR = os.path.join(HERE, 'cache', 'art')
UA = 'Jukebox/1.0 (personal music library)'
# Deezer is the primary source: no key, 1000x1000 covers, and it tolerates this
# pace comfortably. iTunes is the fallback -- it allows only ~20 requests a MINUTE
# and answers 403 to everything for a while once you exceed that, which is exactly
# what happened on the first attempt at this.
RATE = 0.30
ITUNES_RATE = 3.1
GOOD_ENOUGH = 600                # px; below this an album is worth upgrading

SCHEMA = """
CREATE TABLE IF NOT EXISTS art (
  folder     TEXT PRIMARY KEY,
  cur_w      INTEGER,      -- best resolution already on disk
  cur_src    TEXT,         -- 'sidecar' | 'embedded' | 'none'
  want       INTEGER,      -- 1 if we tried to upgrade it
  hit        INTEGER,      -- 1 if we accepted a remote cover
  score      REAL,         -- match confidence 0..1
  mb_album   TEXT,         -- what the remote actually returned
  mb_artist  TEXT,
  url        TEXT,
  local      TEXT,         -- cached file, relative to ARTDIR
  new_w      INTEGER,
  reason     TEXT,         -- why refused, when refused
  ts         REAL
);
"""

def cache_db():
    c = sqlite3.connect(CACHE, timeout=120)
    c.executescript(SCHEMA)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=120000')
    return c

# ---------------------------------------------------------------- current art
def img_size(path):
    try:
        with Image.open(path) as im:
            return max(im.size)
    except Exception:
        return 0

def embedded_size(full):
    """Largest embedded picture in a track, in pixels."""
    try:
        import mutagen
        a = mutagen.File(full)
        if a is None or a.tags is None:
            return 0
        blobs = []
        t = a.tags
        if hasattr(t, 'getall'):
            blobs = [p.data for p in t.getall('APIC')]
        elif 'covr' in t:
            blobs = [bytes(x) for x in t['covr']]
        elif getattr(a, 'pictures', None):
            blobs = [p.data for p in a.pictures]
        best = 0
        for b in blobs:
            try:
                with Image.open(io.BytesIO(b)) as im:
                    best = max(best, max(im.size))
            except Exception:
                pass
        return best
    except Exception:
        return 0

def survey(conn, acdb, limit=None):
    """Record the best art each album already has."""
    folders = [r[0] for r in conn.execute(
        'SELECT DISTINCT folder FROM files WHERE kind="audio"')]
    if limit: folders = folders[:limit]
    have = {r[0] for r in acdb.execute('SELECT folder FROM art WHERE cur_w IS NOT NULL')}
    todo = [f for f in folders if f not in have]
    print(f'{len(folders)} albums, {len(folders)-len(todo)} already surveyed, '
          f'measuring {len(todo)}')
    for i, folder in enumerate(todo):
        best, src = 0, 'none'
        for (p,) in conn.execute(
                'SELECT path FROM files WHERE folder=? AND kind="image"', (folder,)):
            w = img_size(os.path.join(musiclib.ROOT, p.replace('/', os.sep)))
            if w > best: best, src = w, 'sidecar'
        # the two tracks most likely to carry the biggest picture
        for (p,) in conn.execute(
                'SELECT path FROM files WHERE folder=? AND kind="audio" AND has_art>0 '
                'ORDER BY art_bytes DESC LIMIT 2', (folder,)):
            w = embedded_size(os.path.join(musiclib.ROOT, p.replace('/', os.sep)))
            if w > best: best, src = w, 'embedded'
        acdb.execute('INSERT OR REPLACE INTO art (folder,cur_w,cur_src,ts) '
                     'VALUES (?,?,?,?)', (folder, best, src, time.time()))
        if (i + 1) % 200 == 0:
            acdb.commit(); print(f'   {i+1}/{len(todo)}', flush=True)
    acdb.commit()

# ---------------------------------------------------------------- matching
def norm(s):
    return merge.norm_album_safe(s or '')

_ROMAN = {'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7,
          'viii': 8, 'ix': 9, 'x': 10, 'xi': 11, 'xii': 12}
_VOLKEY = re.compile(r'(?:vol(?:ume)?|part|pt|disc|cd)\s*\.?\s*(\d{1,2}|[ivx]{1,5})', re.I)
_VOLTAIL = re.compile(r'[\s,]+(\d{1,2}|[ivx]{1,5})\s*$', re.I)

def _as_int(tok):
    tok = tok.lower()
    return int(tok) if tok.isdigit() else _ROMAN.get(tok, 0)

def series_number(title):
    """Which instalment of a series this title is, 0 if it is not one.

    Handles "Vol. 2", "Volume 2", "Part 2", a bare trailing "2", and a bare trailing
    roman numeral ("Buddha Bar IV") -- all of which appear in this library.
    """
    t = re.sub(r'\(.*?\)|\[.*?\]', ' ', title or '').strip()
    m = _VOLKEY.search(t)
    if m:
        return _as_int(m.group(1))
    m = _VOLTAIL.search(t)
    if m:
        return _as_int(m.group(1))
    return 0

def artist_ok(want, got):
    """Same performer? A credit may extend the name ("Bob Marley & The Wailers",
    "The Peter Malick Group", "Philip Glass Ensemble, Philip Glass") but a name
    that merely contains ours is someone else: "Eagles in Drag", "Mr. Garbage",
    "Madonna Shqiptare", "Queen Marion", "Electronic dystopia" all shipped as
    covers before this test existed."""
    wa, ga = merge.norm_artist(want), merge.norm_artist(got)
    if not wa or not ga:
        return False
    if difflib.SequenceMatcher(None, wa, ga).ratio() >= 0.75:
        return True
    # a credit on either side may list several names: ours "B.B. King/Robert
    # Cray", theirs "Philip Glass Ensemble, Philip Glass, Michael Reisman"
    SEP = r'\s*(?:,|/|;|&|and|with|feat\.?|featuring|vs\.?)\s*'
    gtoks = [merge.norm_artist(t) for t in re.split(SEP, got, flags=re.I) if t]
    wtoks = [merge.norm_artist(t) for t in re.split(SEP, want, flags=re.I) if t]
    if wa in gtoks or ga in wtoks or set(gtoks) & set(wtoks):
        return True
    gl, wl = got.lower(), want.lower()
    k = gl.find(wl)
    if k < 0:
        return False
    before, after = gl[:k].strip(), gl[k + len(wl):].strip()
    if before and not re.fullmatch(r'the|dj|mc|sir|dr\.?|los|las|le|la|les', before):
        return False
    return (not after) or re.match(r'^(&|and|with|feat|featuring|/|,|;|vs\.?|y|et)', after) is not None         or re.search(r'(group|band|ensemble|orchestra|quartet|quintet|trio|singers|players|'
                     r'experience|project|allstars|all-stars|choir)$', after) is not None


# an extension of our title that names a DIFFERENT record of the same artist
WRONG_RECORD = re.compile(r'(?i)(greatest hits|best of|hits|live|remix|b-?sides|tribute|karaoke|'
                          r'cast recording|instrumental|unplugged|sessions?|demos?|'
                          r'in concert|soundtrack|dub|acoustic|dvd|bonus audio|highlights)')


def match_score(want_artist, want_album, got_artist, got_album):
    """How much do we believe this is the same record? 0..1, and a reason if not."""
    if not artist_ok(want_artist, got_artist):
        ga = merge.norm_artist(got_artist)
        # A compilation is credited to "Various Artists" upstream while our tag names
        # whichever performer happened to be on track 1. Allow it, but only when the
        # album title itself is an exact match -- that is what carries the identity.
        if ga in ('variousartists', 'various') and norm(want_album) == norm(got_album):
            return 0.85, None
        return 0.0, f'artist mismatch ({got_artist!r})'
    # A series number must match exactly. norm_album strips "Volume N", so without
    # this "Anthology Volume 1" happily takes the cover of "Anthology 4" -- and the
    # number is often bare or roman ("Buddha Bar IV" vs "Buddha Bar VIII").
    if series_number(want_album) != series_number(got_album):
        return 0.0, f'volume mismatch ({got_album!r})'
    wl, gl = norm(want_album), norm(got_album)
    if wl == gl:
        return 1.0, None
    alb = difflib.SequenceMatcher(None, wl, gl).ratio()
    # A returned title that extends ours is usually an edition ("Abbey Road" ->
    # "Abbey Road (2019 Mix)"), unless the extra words name a different record
    # of the same artist: "Bon Jovi" -> "Bon Jovi Greatest Hits", "American
    # Idiot" -> "American Idiot - The Original Broadway Cast Recording".
    if gl.startswith(wl) and len(wl) >= 4:
        k = got_album.lower().find(want_album.lower())
        suffix = got_album[k + len(want_album):] if k >= 0 else got_album
        if WRONG_RECORD.search(suffix) and not WRONG_RECORD.search(want_album):
            return alb, f'album mismatch ({got_album!r})'
        return max(0.90, alb), None
    # ours extending theirs is weaker evidence: "Janet Jackson's Rhythm Nation"
    # starts with "Janet." but is not it
    if wl.startswith(gl) and len(gl) >= 4 and alb >= 0.80:
        return max(0.85, alb), None
    if alb >= 0.80:
        return alb, None
    return alb, f'album mismatch ({got_album!r})'

def deezer(artist, album):
    q = urllib.parse.urlencode({'q': f'artist:"{artist}" album:"{album}"', 'limit': 6})
    for i in range(3):
        try:
            r = urllib.request.Request('https://api.deezer.com/search/album?' + q,
                                       headers={'User-Agent': UA})
            with urllib.request.urlopen(r, timeout=25) as resp:
                d = json.load(resp)
            return [{'artist': x['artist']['name'], 'album': x['title'],
                     'art': x.get('cover_xl') or x.get('cover_big')}
                    for x in (d.get('data') or []) if x.get('cover_xl') or x.get('cover_big')]
        except Exception:
            time.sleep(1.5 * (i + 1))
    return []

def itunes(artist, album):
    q = urllib.parse.urlencode({'term': f'{artist} {album}', 'entity': 'album',
                                'limit': 6, 'country': 'US'})
    for i in range(2):
        try:
            r = urllib.request.Request('https://itunes.apple.com/search?' + q,
                                       headers={'User-Agent': UA})
            with urllib.request.urlopen(r, timeout=25) as resp:
                d = json.load(resp)
            return [{'artist': x.get('artistName', ''), 'album': x.get('collectionName', ''),
                     'art': (x.get('artworkUrl100') or '').replace('100x100bb', '1000x1000bb')}
                    for x in (d.get('results') or []) if x.get('artworkUrl100')]
        except Exception as e:
            if getattr(e, 'code', None) == 403:
                return None            # rate limited: caller stops using this source
            time.sleep(2)
    return []

def download(url, dest):
    try:
        r = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(r, timeout=40) as resp:
            data = resp.read()
        if len(data) < 8000:
            return 0
        with Image.open(io.BytesIO(data)) as im:
            w = max(im.size)
            if w < 500:
                return 0
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, 'wb').write(data)
        return w
    except Exception:
        return 0

def fetch(conn, acdb, limit=None, threshold=GOOD_ENOUGH):
    rows = acdb.execute(
        'SELECT folder,cur_w FROM art WHERE cur_w < ? AND (want IS NULL OR hit=0) '
        'ORDER BY folder', (threshold,)).fetchall()
    if limit: rows = rows[:limit]
    print(f'{len(rows)} albums below {threshold}px to look up (~{len(rows)*RATE/60:.0f} min)')
    hits = misses = 0
    itunes_ok = True
    last_itunes = 0.0
    for i, (folder, cur_w) in enumerate(rows):
        meta = conn.execute(
            'SELECT albumartist,artist,album FROM files WHERE folder=? AND kind="audio" '
            'AND album IS NOT NULL LIMIT 1', (folder,)).fetchone()
        album = meta[2] if meta else None
        artist = (meta[0] or meta[1]) if meta else None
        if not album or re.search(r'unknown', album, re.I):
            # the folder is usually named after the record even when the tag is not
            cand = folder.split('/')[-1]
            if not re.search(r'unknown|^\s*$|^cd\s*\d|^disc\s*\d', cand, re.I):
                album = cand
        if not artist:
            parts = folder.split('/')
            artist = parts[1] if len(parts) > 2 else None
        if not album or not artist:
            acdb.execute('UPDATE art SET want=1,hit=0,reason=?,ts=? WHERE folder=?',
                         ('no album name anywhere', time.time(), folder))
            misses += 1; continue

        best, why_last = None, 'no results'
        cands = deezer(artist, album)
        time.sleep(RATE)
        # only bother iTunes when Deezer offered nothing usable, and stay under its
        # much stricter limit
        if not cands and itunes_ok:
            wait = ITUNES_RATE - (time.time() - last_itunes)
            if wait > 0: time.sleep(wait)
            res = itunes(artist, album)
            last_itunes = time.time()
            if res is None:
                itunes_ok = False
            else:
                cands = res
        for x in cands:
            sc, why = match_score(artist, album, x['artist'], x['album'])
            if why is None and (best is None or sc > best[0]):
                best = (sc, x)
            elif why:
                why_last = why

        if not best:
            acdb.execute('UPDATE art SET want=1,hit=0,score=0,reason=?,ts=? WHERE folder=?',
                         (why_last, time.time(), folder))
            misses += 1
        else:
            sc, x = best
            rel = folder.replace('/', '__') + '.jpg'
            w = download(x['art'], os.path.join(ARTDIR, rel)) if x['art'] else 0
            if w:
                acdb.execute(
                    'UPDATE art SET want=1,hit=1,score=?,mb_album=?,mb_artist=?,url=?,'
                    'local=?,new_w=?,reason=NULL,ts=? WHERE folder=?',
                    (sc, x['album'], x['artist'], x['art'], rel, w, time.time(), folder))
                hits += 1
            else:
                acdb.execute('UPDATE art SET want=1,hit=0,score=?,reason=?,ts=? WHERE folder=?',
                             (sc, 'download failed', time.time(), folder))
                misses += 1
        if (i + 1) % 50 == 0:
            acdb.commit()
            print(f'   {i+1}/{len(rows)}  hits={hits} misses={misses}  '
                  f'eta {(len(rows)-i-1)*RATE/60:.0f}m', flush=True)
    acdb.commit()
    print()
    print(f'fetched {hits}, refused/missed {misses}')

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['survey', 'fetch', 'stats'])
    ap.add_argument('--limit', type=int)
    a = ap.parse_args()
    conn = musiclib.db(); acdb = cache_db()
    if a.cmd == 'survey':
        survey(conn, acdb, a.limit)
    elif a.cmd == 'fetch':
        fetch(conn, acdb, a.limit)
    else:
        tot = acdb.execute('SELECT COUNT(*) FROM art').fetchone()[0]
        print(f'albums surveyed: {tot}')
        buckets = collections.Counter()
        for (w,) in acdb.execute('SELECT cur_w FROM art WHERE cur_w IS NOT NULL'):
            buckets['none' if not w else '<=200' if w <= 200 else '201-400' if w <= 400
                    else '401-600' if w <= 600 else '601-1000' if w <= 1000 else '>1000'] += 1
        for k in ('none', '<=200', '201-400', '401-600', '601-1000', '>1000'):
            if buckets[k]: print(f'   current art {k:9} {buckets[k]}')
        h = acdb.execute('SELECT COUNT(*) FROM art WHERE hit=1').fetchone()[0]
        m = acdb.execute('SELECT COUNT(*) FROM art WHERE want=1 AND hit=0').fetchone()[0]
        print(f'   upgraded: {h}   refused/missed: {m}')
        for r, n in acdb.execute('SELECT reason,COUNT(*) FROM art WHERE want=1 AND hit=0 '
                                 'GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 8'):
            print(f'      {str(r)[:52]:54} {n}')
    conn.close(); acdb.close()
