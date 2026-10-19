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
_VOLKEY = re.compile(r'\b(?:vol(?:ume)?|part|pt|disc|cd)\s*\.?\s*(\d{1,2}|[ivx]{1,5})\b', re.I)
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
    SEP = r'\s*(?:,|/|;|&|\band\b|\bwith\b|\bfeat\.?|\bfeaturing\b|\bvs\.?)\s*'
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
    return (not after) or re.match(r'^(&|and\b|with\b|feat|featuring\b|/|,|;|vs\.?|y\b|et\b)', after) is not None         or re.search(r'\b(group|band|ensemble|orchestra|quartet|quintet|trio|singers|players|'
                     r'experience|project|allstars|all-stars|choir)$', after) is not None


# an extension of our title that names a DIFFERENT record of the same artist
WRONG_RECORD = re.compile(r'(?i)\b(greatest hits|best of|hits\b|live\b|remix|b-?sides|tribute|karaoke|'
                          r'cast recording|instrumental|unplugged|sessions?\b|demos?\b|'
                          r'in concert|soundtrack|dub\b|acoustic|dvd|bonus audio|highlights)')


_EDITION_WORDS = re.compile(r'(deluxe|expanded|special|anniversary|remaster(ed)?|edition|version|'
                            r'bonustracks?|\d+(st|nd|rd|th)?|super|digital|legacy|collectors|reissue|'
                            r'the|and|mix|master)*')


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
        # Words added outside brackets have to read as an edition, or be the
        # artist's own name ("Recurring Dream: The Very Best of Crowded House").
        # Anything else is another record that starts the same way: "Deep Forest"
        # took the cover of "Deep Forest Delevingne Crystal Clear".
        rest = merge.norm_album_safe(re.sub(r'[\(\[][^\)\]]*[\)\]]', ' ', got_album))
        rest = rest[len(wl):] if rest.startswith(wl) else rest
        if rest and rest != merge.norm_artist(want_artist) and not _EDITION_WORDS.fullmatch(rest):
            return (alb, None) if alb >= 0.80 else (alb, f'album mismatch ({got_album!r})')
        return max(0.90, alb), None
    # ours extending theirs is weaker evidence: "Janet Jackson's Rhythm Nation"
    # starts with "Janet." but is not it
    if wl.startswith(gl) and len(gl) >= 4 and alb >= 0.80:
        return max(0.85, alb), None
    if alb >= 0.80:
        return alb, None
    return alb, f'album mismatch ({got_album!r})'

# ---------------------------------------------------------------- what to ask for
# The search is sent words, not the normalised keys the matcher compares, so the
# raw tag has to be cleaned first: "Vitalogy (2011 Remaster)" found nothing as an
# exact phrase while "Vitalogy" is the first hit (16 Oct 2026: 1,025 misses in
# 1,393 lookups, most of them records both services carry).
MAIN = 'VariousArtists'
_EDITION = re.compile(r'(?i)\s*[\(\[][^\)\]]*\b(remaster(?:ed)?|deluxe|expanded|edition|bonus|reissue|'
                      r'anniversary|explicit|clean|version|mono|stereo|ep|\d{4})\b[^\)\]]*[\)\]]')
_YEARTAIL = re.compile(r'\s+[-(\[]?\s*(19|20)\d{2}\s*[)\]]?\s*$')    # " - 1998", " (1998)", not "1986-1995"
_YEARHEAD = re.compile(r'^\s*(19|20)\d{2}\s*-\s*')
_SOUNDTRACK = re.compile(r'(?i)\s*[-(\[]?\s*\b(original (motion picture |broadway |cast )?soundtrack|'
                         r'music from the motion picture|soundtrack|o\.?s\.?t\.?|st)\b\s*[)\]]?\s*$')
_VA = re.compile(r'(?i)^\s*(va|v\.a\.|various artists?|various)\s*[-_:]\s*')
_DISC_ONLY = re.compile(r'(?i)^\s*(disc|cd|disk)\s*\.?\s*\d+\s*$')
VARIOUS = ('variousartists', 'various', 'va')


def search_title(s):
    """The words to send for an album title: edition noise, years and stray
    underscores (the old rips' stand-in for ':' and '?') taken off."""
    s = (s or '').replace('_', ' ')
    s = _VA.sub('', s)
    for _ in range(3):
        s = _EDITION.sub('', s)
    s = _YEARTAIL.sub('', _YEARHEAD.sub('', s))
    return re.sub(r'\s+', ' ', s).strip(' -,.')


def album_facts(conn, folder):
    """What the files say about one album folder: the most common album-artist
    and album, the per-track artists, and whether it is a various-artists record."""
    aa, alb, arts = collections.Counter(), collections.Counter(), collections.Counter()
    for a1, a2, a3 in conn.execute('SELECT albumartist,artist,album FROM files '
                                   'WHERE folder=? AND kind="audio"', (folder,)):
        if a1: aa[a1.strip()] += 1
        if a2: arts[a2.strip()] += 1
        if a3: alb[a3.strip()] += 1
    albumartist = aa.most_common(1)[0][0] if aa else None
    album = alb.most_common(1)[0][0] if alb else None
    parts = folder.split('/')
    pool, leaf = parts[0], parts[-1]
    if _DISC_ONLY.match(leaf) and len(parts) >= 3:
        leaf = parts[-2]
    folder_artist = parts[1] if pool == MAIN and len(parts) >= 3 else None
    if pool != MAIN and '-' in leaf:
        # collection folders are "Artist-Album" (Christmas) or "VA-Album" (Compilations)
        head, tail = leaf.split('-', 1)
        if _VA.match(head + '-'):
            leaf = tail
        elif any(artist_ok(head, x) for x in list(aa) + list(arts)):
            folder_artist, leaf = head.strip(), tail
    various = bool(albumartist and merge.norm_artist(albumartist) in VARIOUS)
    if not albumartist and pool != MAIN and len(arts) >= 4:
        various = arts.most_common(1)[0][1] * 2 < sum(arts.values())
    soundtrack = pool == 'Soundtracks' or bool(_SOUNDTRACK.search(leaf)) \
        or bool(_SOUNDTRACK.search(album or ''))
    return {'albumartist': albumartist, 'album': album, 'artists': arts, 'leaf': leaf,
            'folder_artist': folder_artist, 'various': various, 'soundtrack': soundtrack,
            'pool': pool}


def queries(f):
    """(artist, title) pairs to try, best first; artist None means by title alone."""
    titles = []
    for t in (f['album'], f['leaf']):
        # placeholder names only: "Unknown Pleasures" and "Superunknown" are records
        if not t or re.fullmatch(r'(?i)\s*(unknown( album)?|assorted|untitled)\s*', t) or _DISC_ONLY.match(t):
            continue
        for v in (search_title(t), search_title(_SOUNDTRACK.sub('', t)) if f['soundtrack'] else None, t):
            if v and v not in titles:
                titles.append(v)
    artists = ['Various Artists'] if f['various'] else []
    for a in (f['albumartist'], f['folder_artist']):
        if a and merge.norm_artist(a) not in VARIOUS and a not in artists:
            artists.append(a)
    if not artists and f['artists']:
        artists.append(f['artists'].most_common(1)[0][0])
    if f['various'] or f['soundtrack']:
        artists.append(None)
    out, seen = [], set()
    for t in titles:
        for a in artists:
            k = (merge.norm_artist(a or ''), merge.norm_album_safe(t))
            if k not in seen:
                seen.add(k); out.append((a, t))
    return out[:6]


_ST_PAREN = re.compile(r'(?i)\s*[\(\[][^\)\]]*(soundtrack|motion picture|original cast|o\.s\.t)[^\)\]]*[\)\]]')


def core_title(s):
    """A title with any soundtrack wording taken off, normalised for comparing:
    "Until the End of the World (Music from the Motion Picture Soundtrack)" and
    "Even More Dazed & Confused" compare equal to the bare names."""
    s = _SOUNDTRACK.sub('', _ST_PAREN.sub('', (s or '').replace('&', ' and ')))
    return merge.norm_album_safe(s)


def accept(f, artist, title, x):
    """Score one remote answer against the album: (score, None), or (0, reason)."""
    if merge.norm_artist(x['artist']) in VARIOUS and f['pool'] == MAIN \
            and not (f['various'] or f['soundtrack']):
        # an album filed under one artist is never a various-artists record:
        # "The Fame" took the cover of the "Fame" film soundtrack
        return 0.0, f'artist mismatch ({x["artist"]!r})'
    if artist is None or merge.norm_artist(artist) in VARIOUS:
        # By title alone (a various-artists record, or a soundtrack credited upstream
        # to its composer or star): the title has to be the same record exactly, and
        # the credit has to be one of ours or "Various Artists".
        want, got = core_title(title), core_title(x['album'])
        if want != got or series_number(title) != series_number(x['album']):
            return 0.0, f'album mismatch ({x["album"]!r})'
        if merge.norm_artist(x['artist']) in VARIOUS \
                or any(artist_ok(a, x['artist']) for a in f['artists']) \
                or (f['albumartist'] and artist_ok(f['albumartist'], x['artist'])):
            return 0.88, None
        # a soundtrack credited to its composer or star counts only when the answer
        # calls itself one: an exact title alone brought covers by tribute acts
        # ("Clockwork Orange" by Original Kick)
        # ...and not a re-recording ("Gladiator" by The Lyndhurst Orchestra) or the
        # stage show of a film ("Bend It Like Beckham (Original Cast Album)")
        if f['soundtrack'] and len(want) >= 6 and re.search(
                r'(?i)soundtrack|motion picture|o\.s\.t', x['album']) \
                and not re.search(r'(?i)orchestra|players|singers|ensemble|tribute|karaoke|'
                                  r'sound-?alike|cover', x['artist']) \
                and not (re.search(r'(?i)\bcast\b', x['album']) and not re.search(r'(?i)\bcast\b', title)):
            return 0.80, None
        return 0.0, f'artist mismatch ({x["artist"]!r})'
    return match_score(artist, title, x['artist'], x['album'])


# ---------------------------------------------------------------- sources
class Source:
    """One remote service with its own pace, and a cool-down when it says "slow
    down". A single 403 from iTunes used to switch it off for every album after it."""
    def __init__(self, gap, cooldown):
        self.gap, self.cooldown = gap, cooldown
        self.last = self.until = 0.0

    def ready(self):
        return time.time() >= self.until

    def pace(self):
        wait = self.gap - (time.time() - self.last)
        if wait > 0: time.sleep(wait)
        self.last = time.time()

    def throttled(self):
        self.until = time.time() + self.cooldown


DEEZER = Source(RATE, 10)
ITUNES = Source(ITUNES_RATE, 90)
MB = Source(1.1, 30)


def _get_json(url):
    r = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(r, timeout=25) as resp:
        return json.load(resp)


def deezer(artist, album, strict=True):
    if strict:
        q = f'artist:"{artist}" album:"{album}"' if artist else f'album:"{album}"'
    else:
        q = f'{artist or ""} {album}'.strip()
    url = 'https://api.deezer.com/search/album?' + urllib.parse.urlencode({'q': q, 'limit': 10})
    for i in range(3):
        DEEZER.pace()
        try:
            d = _get_json(url)
        except Exception:
            time.sleep(1.5 * (i + 1)); continue
        if d.get('error'):
            # a quota answer is HTTP 200 with an error body; it used to read as "no results"
            time.sleep(5 * (i + 1)); continue
        return [{'artist': x['artist']['name'], 'album': x['title'],
                 'art': x.get('cover_xl') or x.get('cover_big')}
                for x in (d.get('data') or []) if x.get('cover_xl') or x.get('cover_big')]
    return []


def itunes(artist, album):
    if not ITUNES.ready():
        return []
    q = urllib.parse.urlencode({'term': f'{artist or ""} {album}'.strip(), 'entity': 'album',
                                'limit': 10, 'country': 'US'})
    for i in range(2):
        ITUNES.pace()
        try:
            d = _get_json('https://itunes.apple.com/search?' + q)
            return [{'artist': x.get('artistName', ''), 'album': x.get('collectionName', ''),
                     'art': (x.get('artworkUrl100') or '').replace('100x100bb', '1000x1000bb')}
                    for x in (d.get('results') or []) if x.get('artworkUrl100')]
        except Exception as e:
            if getattr(e, 'code', None) in (403, 429):
                ITUNES.throttled(); return []
            time.sleep(2)
    return []


def musicbrainz(artist, album):
    """Release groups by title and artist, each pointing at its Cover Art Archive front."""
    if not MB.ready():
        return []
    q = 'releasegroup:"%s"' % album.replace('"', '')
    if artist:
        q += ' AND artist:"%s"' % artist.replace('"', '')
    url = 'https://musicbrainz.org/ws/2/release-group/?fmt=json&limit=8&query=' + urllib.parse.quote(q)
    d = None
    for i in range(3):
        # MusicBrainz answers 503 to bursts well inside its one-a-second rule; it is
        # the last resort, so wait and ask again rather than skip the album
        MB.pace()
        try:
            d = _get_json(url); break
        except Exception as e:
            if getattr(e, 'code', None) not in (429, 503):
                return []
            time.sleep(3 * (i + 1))
    if d is None:
        MB.throttled()
        return []
    out = []
    for rg in d.get('release-groups', []):
        credit = ''.join(a.get('name', '') + (a.get('joinphrase') or '')
                         for a in rg.get('artist-credit', []) if isinstance(a, dict))
        out.append({'artist': credit, 'album': rg.get('title', ''),
                    'art': 'https://coverartarchive.org/release-group/%s/front-1200' % rg['id']})
    return out


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


def lookup(f, dest):
    """Ask each source in turn, with every (artist, title) spelling, until an answer
    is accepted and downloads. Deezer offering only wrong records used to end the
    search there: iTunes was asked only when Deezer returned nothing at all.
    Returns (score, answer, width, source), or (None, reason)."""
    qs = queries(f)
    if not qs:
        return None, 'no album name anywhere'
    why_last = 'no results'
    tried = set()
    # Deezer is fast and asked every spelling; the slow two only the first few, so
    # an album nobody carries costs ~15 s rather than a minute. MusicBrainz goes
    # before iTunes: one request a second against one every three, and it found
    # more of what the other two missed. A bare title is too loose for MusicBrainz.
    for name, ask, most in (('deezer', deezer, 6),
                            ('deezer', lambda a, t: deezer(a, t, strict=False), 6),
                            ('musicbrainz', musicbrainz, 3),
                            ('itunes', itunes, 2)):
        for artist, title in [q for q in qs if not (name == 'musicbrainz' and q[0] is None)][:most]:
            best = None
            for x in ask(artist, title) or []:
                sc, why = accept(f, artist, title, x)
                if why is None and (best is None or sc > best[0]):
                    best = (sc, x)
                elif why:
                    why_last = why
            if best and best[1]['art'] not in tried:
                tried.add(best[1]['art'])
                w = download(best[1]['art'], dest)
                if w:
                    return best[0], best[1], w, name
                why_last = 'download failed'
    return None, why_last


RETRY_AFTER = 30 * 86400    # a refused album is asked again after a month, not on every rescan


def fetch(conn, acdb, limit=None, threshold=GOOD_ENOUGH, retry=False, only=None):
    before = time.time() + 1 if retry else time.time() - RETRY_AFTER
    rows = acdb.execute(
        'SELECT folder,cur_w FROM art WHERE cur_w < ? '
        'AND (want IS NULL OR (hit=0 AND COALESCE(ts,0) < ?)) ORDER BY folder',
        (threshold, before)).fetchall()
    if only:
        rows = [r for r in rows if r[0].startswith(only)]
    if limit: rows = rows[:limit]
    print(f'{len(rows)} albums below {threshold}px to look up', flush=True)
    hits = misses = 0
    by_src = collections.Counter()
    t0 = time.time()
    for i, (folder, cur_w) in enumerate(rows):
        rel = folder.replace('/', '__') + '.jpg'
        res = lookup(album_facts(conn, folder), os.path.join(ARTDIR, rel))
        if res[0] is None:
            acdb.execute('UPDATE art SET want=1,hit=0,score=0,reason=?,ts=? WHERE folder=?',
                         (res[1], time.time(), folder))
            misses += 1
        else:
            sc, x, w, src = res
            acdb.execute(
                'UPDATE art SET want=1,hit=1,score=?,mb_album=?,mb_artist=?,url=?,'
                'local=?,new_w=?,reason=NULL,ts=? WHERE folder=?',
                (sc, x['album'], x['artist'], x['art'], rel, w, time.time(), folder))
            hits += 1; by_src[src] += 1
        if (i + 1) % 50 == 0:
            acdb.commit()
            per = (time.time() - t0) / (i + 1)
            print(f'   {i+1}/{len(rows)}  hits={hits} misses={misses}  '
                  f'eta {(len(rows)-i-1)*per/60:.0f}m', flush=True)
    acdb.commit()
    print()
    print(f'fetched {hits} ({", ".join(f"{k} {v}" for k, v in by_src.most_common()) or "none"}), '
          f'refused/missed {misses}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['survey', 'fetch', 'stats'])
    ap.add_argument('--limit', type=int)
    ap.add_argument('--retry', action='store_true', help='ask again about albums refused in the last month')
    ap.add_argument('--only', help='folder prefix, e.g. Soundtracks/')
    a = ap.parse_args()
    conn = musiclib.db(); acdb = cache_db()
    if a.cmd == 'survey':
        survey(conn, acdb, a.limit)
    elif a.cmd == 'fetch':
        fetch(conn, acdb, a.limit, retry=a.retry, only=a.only)
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
