"""Artist photographs and biographies.

Two gaps this fills. The artist thumbnail was whichever album happened to have
the biggest cover art, so Duran Duran was represented by the Rio Collector's
Edition sleeve and Erasure by a box set -- a picture of a record, not of the
band. And an artist page was a bare grid of albums with nothing about the
artist on it.

Sources, both keyless:

  image  Deezer's artist search returns picture_xl (1000px). Reliable and
         generous; it was already the primary source for album art here.

  bio    Wikipedia, in ONE request per artist: generator=search combined with
         prop=extracts returns the candidate pages AND their intro text
         together, so disambiguation happens locally instead of costing extra
         round trips.

Disambiguation is the whole problem with artist names, and it is why this does
not just fetch the page matching the name. "Air" resolves to the atmosphere,
"Bush" to a shrub, "Garbage" to refuse. Trying "<name> (band)" is not enough
either -- that sent Air to a Japanese singer. So candidates must BOTH read as
musical AND have a title matching the artist name; a page that merely mentions
the band is rejected. That gets Air to the French duo and Bush to the British
band.

Wikimedia rate-limits anonymous callers hard (HTTP 429 after a short burst), so
requests are paced and backed off. Resumable: already-fetched artists are
skipped, so it can be stopped and restarted.
"""
import os, re, html, json, time, hashlib, sqlite3, urllib.request, urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / 'jukebox.db'
PIX = HERE / 'artistpix'
UA = 'SignalJukebox/1.0 (personal offline music library indexer)'

# Deezer answers "no picture" with a URL whose hash segment is the MD5 of the
# empty string, then serves a grey silhouette. It is a 147KB JPEG, so a size
# check does not catch it -- 32 artists came back with the same placeholder,
# which is why Duran Duran showed a generic avatar.
DZ_EMPTY = 'd41d8cd98f00b204e9800998ecf8427e'
PLACEHOLDER_SHA1 = {
    'aa40d58203c229f79cdcba76b86cf4d2e34937dc',
    '384a2c3376b4bb3960986d33bc3173542da0fee9',
}

WIKI_GAP = 1.1          # seconds between Wikimedia calls
DEEZER_GAP = 0.34

MUSICAL = re.compile(r'(?i)\b(band|singer|musician|rapper|duo|trio|group|songwriter|composer|'
                     r'dj|producer|ensemble|orchestra|guitarist|drummer|vocalist|rock|pop|'
                     r'hip hop|electronic|jazz|metal|folk|soul|r&b|indie)\b')

# A parenthetical naming a WORK, not a performer. "Jack Johnson (album)" shares
# the artist's base name and reads as musical, so it slipped through until the
# qualifier itself was checked.
NOT_ARTIST = re.compile(r'(?i)\b(album|song|single|ep|mixtape|soundtrack|film|movie|'
                        r'tv series|video game|novel|book|magazine|tour|record label|'
                        r'disambiguation)\b')


def qualifier(t):
    m = re.search(r'\(([^)]*)\)\s*$', t or '')
    return (m.group(1) if m else '').strip()

SCHEMA = """
CREATE TABLE IF NOT EXISTS artist_info (
  artist_id  INTEGER PRIMARY KEY,
  name       TEXT,
  wiki_title TEXT,
  bio        TEXT,
  bio_url    TEXT,
  image_url  TEXT,
  image_file TEXT,
  status     TEXT,       -- ok | partial | none
  fetched    REAL
);
"""


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def base_key(s):
    """Artist name reduced for comparison: no leading article, no trailing
    Wikipedia qualifier. "Bush (British band)" and "Bush" both become "bush"."""
    s = re.sub(r'\s*\([^)]*\)\s*$', '', s or '')
    s = re.sub(r'(?i)^(the|a|an)\s+', '', s.strip())
    return norm(s)


# Artist names in the library are not always the name the world uses. The
# misses were all shape, not absence: "Police, The" (a sort name that leaked
# into the name field), "Macklemore &amp; Ryan Lewis" (an HTML entity that
# survived tagging), "Orchestral Manouevres in the Dark (OMD)" (a trailing
# gloss), and collaborations like "Johnny Clegg and Savuka".
NOT_AN_ARTIST = re.compile(r'(?i)^(various artists?|soundtrack|unknown artist|va)$')


def variants(name):
    """The name, then progressively more forgiving forms of it."""
    out, seen = [], set()

    def add(x):
        x = (x or '').strip()
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)

    n = html.unescape(name or '').strip()
    add(n)
    m = re.match(r'^(.*),\s*(the|a|an)$', n, re.I)      # "Police, The"
    if m:
        add('%s %s' % (m.group(2), m.group(1)))
    add(re.sub(r'\s*\([^)]*\)\s*$', '', n))            # drop "(OMD)"
    for sep in (' & ', ' and ', ' with ', ' feat. ', ' featuring '):
        if sep in n.lower():
            i = n.lower().index(sep)
            add(n[:i])                                    # the lead performer
            break
    return out


def _get(url, tries=4, raw=False):
    for i in range(tries):
        try:
            r = urllib.request.Request(url, headers={'User-Agent': UA,
                                                     'Accept': '*/*' if raw else 'application/json'})
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.read() if raw else json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and i < tries - 1:
                time.sleep(3 * (i + 1))
                continue
            raise
        except Exception:
            if i < tries - 1:
                time.sleep(1.5)
                continue
            raise
    return None


class LookupFailed(Exception):
    """The service did not answer -- retryable, unlike 'no such artist'."""


# ---------------------------------------------------------------- bio
def fetch_bio(name):
    """(wiki_title, intro_text, lead_image_url). One request per name form."""
    if NOT_AN_ARTIST.match((name or '').strip()):
        return None, None, None
    for i, v in enumerate(variants(name)):
        r = _fetch_bio_one(v)
        if r[0]:
            return r
        if i:
            time.sleep(WIKI_GAP)
    return None, None, None


def _page_ok(p, name):
    """Is this page the artist? Returns the extract, or None."""
    ex = (p.get('extract') or '').strip()
    title = p.get('title', '')
    if not ex or 'missing' in p:
        return None
    if re.search(r'(?i)\bmay refer to\b', ex[:200]):     # a disambiguation page
        return None
    if not MUSICAL.search(ex[:400]):
        return None
    if base_key(title) != base_key(name) or NOT_ARTIST.search(qualifier(title)):
        return None
    return ex


def _fetch_bio_exact(name):
    """Ask for the page with exactly this title.

    Searching is worse than useless for a plain band name: "Fleetwood Mac
    musician band" ranks Mick Fleetwood, Bob Welch, Peter Green and the 1975
    album above the band, whose own page does not make the top ten. The article
    is almost always titled exactly what the band is called, so try that first.
    """
    url = ('https://en.wikipedia.org/w/api.php?action=query&format=json'
           '&prop=extracts|pageimages&exintro=1&explaintext=1&piprop=original'
           '&redirects=1&titles=' + urllib.parse.quote(name))
    try:
        pages = (_get(url) or {}).get('query', {}).get('pages', {})
    except Exception:
        raise LookupFailed(name)
    for p in pages.values():
        ex = _page_ok(p, name)
        if ex:
            return p.get('title'), ex, (p.get('original') or {}).get('source')
    return None, None, None


def _fetch_bio_one(name):
    hit = _fetch_bio_exact(name)
    if hit[0]:
        return hit
    time.sleep(WIKI_GAP)
    # Wikipedia's own disambiguation form. "Go West" alone is a disambiguation
    # page and search ranks the album above the band, but "Go West (band)" is
    # exactly where the article lives.
    for suffix in (' (band)', ' (musician)', ' (singer)'):
        hit = _fetch_bio_exact(name + suffix)
        if hit[0]:
            return hit
        time.sleep(WIKI_GAP)
    return _fetch_bio_search(name)


def _fetch_bio_search(name):
    url = ('https://en.wikipedia.org/w/api.php?action=query&format=json&generator=search'
           '&gsrsearch=' + urllib.parse.quote(name + ' musician band') +
           '&gsrlimit=10&prop=extracts|pageimages&exintro=1&explaintext=1&exlimit=max'
           '&piprop=original')
    try:
        pages = (_get(url) or {}).get('query', {}).get('pages', {})
    except Exception:
        # A rate-limited request is NOT the same as an artist having no page.
        # Caching it as 'none' would permanently blank artists that merely
        # happened to be asked for during a throttle.
        raise LookupFailed(name)
    n = base_key(name)
    best = None
    for p in pages.values():
        title, ex = p.get('title', ''), (p.get('extract') or '').strip()
        if not ex or not MUSICAL.search(ex[:400]):
            continue
        # The title must BE the artist, allowing only Wikipedia's own
        # parenthetical qualifier. Substring matching was far too loose: it
        # gave Erasure the page for "Always (Erasure song)" and Jack Johnson
        # the page for "Big Jack Johnson", a different musician entirely.
        if base_key(title) != n or NOT_ARTIST.search(qualifier(title)):
            continue
        score = 2 if norm(title) == norm(name) else 1
        if best is None or score > best[0]:
            best = (score, title, ex, (p.get('original') or {}).get('source'))
    if not best:
        return None, None, None
    return best[1], best[2], best[3]


# ---------------------------------------------------------------- image
def fetch_image(name):
    if NOT_AN_ARTIST.match((name or '').strip()):
        return None, None
    for i, v in enumerate(variants(name)):
        r = _fetch_image_one(v)
        if r[0]:
            return r
        if i:
            time.sleep(DEEZER_GAP)
    return None, None


def _fetch_image_one(name):
    """(picture_url, deezer_name) or (None, None).

    Deezer often holds several artists with the SAME name, and the search does
    not return the famous one first: "America" yields four, and the first exact
    match has 2 fans and 0 albums -- some unrelated person, whose photo is what
    the 1970s band ended up wearing. Rank exact matches by following and
    catalogue size instead of list order, and never accept an entry with no
    albums at all.
    """
    url = 'https://api.deezer.com/search/artist?limit=10&q=' + urllib.parse.quote(name)
    try:
        data = (_get(url) or {}).get('data') or []
    except Exception:
        return None, None
    n = norm(name)

    def usable(d):
        pic = d.get('picture_xl') or d.get('picture_big') or ''
        return None if (not pic or DZ_EMPTY in pic) else pic

    def rank(d):
        return (-(d.get('nb_fan') or 0), -(d.get('nb_album') or 0))

    exact = [d for d in data if norm(d.get('name')) == n and usable(d)
             and (d.get('nb_album') or 0) > 0]
    if exact:
        exact.sort(key=rank)
        return usable(exact[0]), exact[0].get('name')

    # nothing named exactly this: accept a close name, still best-first
    near = [d for d in data if usable(d) and (d.get('nb_album') or 0) > 0
            and n and (n in norm(d.get('name')) or norm(d.get('name')) in n)]
    if near:
        near.sort(key=rank)
        return usable(near[0]), near[0].get('name')
    return None, None


def save_image(aid, url):
    PIX.mkdir(exist_ok=True)
    try:
        body = _get(url, raw=True)
    except Exception:
        return None
    if not body or len(body) < 2000:
        return None
    if hashlib.sha1(body).hexdigest() in PLACEHOLDER_SHA1:
        return None                            # the silhouette, at any URL
    fn = '%d.jpg' % aid
    (PIX / fn).write_bytes(body)
    return fn


# ---------------------------------------------------------------- driver
def build(db=DB, limit=None, only_missing=True, verbose=True, min_tracks=0):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    rows = c.execute('SELECT id, name, n_tracks FROM artist WHERE n_tracks >= ? '
                     'ORDER BY n_tracks DESC', (min_tracks,)).fetchall()
    # an 'error' row is a failed lookup, not an answer -- try it again
    done = ({r[0] for r in c.execute("SELECT artist_id FROM artist_info WHERE status!='error'")}
            if only_missing else set())
    todo = [r for r in rows if r['id'] not in done]
    if limit:
        todo = todo[:limit]
    if verbose:
        print('artists to enrich: %d (of %d)' % (len(todo), len(rows)))

    nb = ni = err = 0
    for i, a in enumerate(todo, 1):
        failed = False
        wiki_pic = None
        try:
            title, bio, wiki_pic = fetch_bio(a['name'])
        except LookupFailed:
            title, bio, failed = None, None, True
            err += 1
            time.sleep(5)          # back well off before the next one
        time.sleep(WIKI_GAP)
        pic, _dz = fetch_image(a['name'])
        time.sleep(DEEZER_GAP)
        fn = save_image(a['id'], pic) if pic else None
        # Deezer has no photo (or served the silhouette): use the lead image
        # from the artist's own Wikipedia page, which the bio request already
        # fetched, so this costs nothing extra.
        if not fn and wiki_pic:
            fn = save_image(a['id'], wiki_pic)
            if fn:
                pic = wiki_pic
                time.sleep(0.3)
        if bio:
            nb += 1
        if fn:
            ni += 1
        status = ('error' if failed else
                  'ok' if (bio and fn) else ('partial' if (bio or fn) else 'none'))
        c.execute('INSERT OR REPLACE INTO artist_info (artist_id,name,wiki_title,bio,bio_url,'
                  'image_url,image_file,status,fetched) VALUES (?,?,?,?,?,?,?,?,?)',
                  (a['id'], a['name'], title, bio,
                   ('https://en.wikipedia.org/wiki/' + urllib.parse.quote(title.replace(' ', '_'))
                    if title else None),
                   pic, fn, status, time.time()))
        if i % 25 == 0:
            c.commit()
            if verbose:
                print('  %4d/%d  bios %d  images %d  errors %d   (last: %s)'
                      % (i, len(todo), nb, ni, err, a['name'][:28]))
    c.commit()
    tot = c.execute('SELECT COUNT(*) FROM artist_info').fetchone()[0]
    wb = c.execute('SELECT COUNT(*) FROM artist_info WHERE bio IS NOT NULL').fetchone()[0]
    wi = c.execute('SELECT COUNT(*) FROM artist_info WHERE image_file IS NOT NULL').fetchone()[0]
    c.close()
    return {'processed': len(todo), 'total': tot, 'bios': wb, 'images': wi}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int)
    ap.add_argument('--min-tracks', type=int, default=0)
    ap.add_argument('--redo', action='store_true')
    ap.add_argument('--repair-images', action='store_true',
                    help='retry artists that ended up with no image or a placeholder')
    a = ap.parse_args()
    if a.repair_images:
        import sqlite3 as _s
        w = _s.connect(DB)
        # drop rows with no image so the normal resume logic picks them up again
        n = w.execute('DELETE FROM artist_info WHERE image_file IS NULL').rowcount
        w.commit(); w.close()
        print('queued %d artists for another try at an image' % n)
    r = build(limit=a.limit, only_missing=not a.redo, min_tracks=a.min_tracks)
    print('')
    print('processed %(processed)d -> %(total)d artists on file: %(bios)d bios, %(images)d images' % r)
