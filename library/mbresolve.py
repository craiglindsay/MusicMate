"""Resolve artist names against MusicBrainz for canonical name + sort name.

Why an external lookup: whether an artist's name carries "The" is a fact about the
artist, not something derivable from the files. "Eagles" is correct and "The Eagles"
is a different British band; "Doors" should be "The Doors". A blanket rule gets one
of those wrong whichever way it points.

Only artist names leave this machine. Results are cached, so a re-run costs nothing.
MusicBrainz asks for <=1 request/second and a descriptive User-Agent; both respected.
"""
import os, sys, json, time, sqlite3, urllib.request, urllib.parse, argparse
import merge, musiclib

UA = 'MusicLibraryTidy/1.0 (personal music library maintenance script)'
RATE = 1.3                     # MusicBrainz throttles below its documented 1/sec

SCHEMA = """
CREATE TABLE IF NOT EXISTS mb_artist (
  query      TEXT PRIMARY KEY,   -- the name we asked about
  norm       TEXT,               -- normalised key it belongs to
  mb_name    TEXT,               -- canonical name
  mb_sort    TEXT,               -- canonical sort name
  mbid       TEXT,
  score      INTEGER,
  type       TEXT,
  country    TEXT,
  disamb     TEXT,
  n_cand     INTEGER,            -- how many plausible same-name artists existed
  ts         REAL
);
"""

def fetch(name, tries=4):
    url = 'https://musicbrainz.org/ws/2/artist?' + urllib.parse.urlencode(
        {'query': f'artist:"{name}"', 'fmt': 'json', 'limit': 8})
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={'User-Agent': UA, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            code = getattr(e, 'code', None)
            if code == 404:
                return {'artists': []}
            # 503 is MusicBrainz shedding load, not a permanent failure: back off
            # hard and keep going rather than abandoning the artist
            time.sleep(min(30, 3 * (2 ** i)))
    return None

def best(cands, want_norm):
    """Pick the artist whose name normalises to what we asked for, best score first."""
    ok = [a for a in cands
          if merge.norm_artist(a.get('name', '')) == want_norm]
    if not ok:
        return None, len(cands)
    ok.sort(key=lambda a: -(a.get('score') or 0))
    return ok[0], len(ok)

def _retry(conn, sql, args, tries=6):
    for i in range(tries):
        try:
            conn.execute(sql, args); return True
        except sqlite3.OperationalError:
            time.sleep(0.5 * (2 ** i))
    return False

def run(conn, names, verbose=True):
    conn.executescript(SCHEMA)
    have = {r[0] for r in conn.execute('SELECT query FROM mb_artist')}
    todo = [n for n in names if n not in have]
    if verbose:
        print(f'{len(names)} artists, {len(names)-len(todo)} cached, fetching {len(todo)}')
        print(f'  ~{len(todo)*RATE/60:.0f} min at {RATE}s/request')
    t0 = time.time()
    for i, name in enumerate(todo):
        try:
            d = fetch(name)
        except Exception:
            d = None
        time.sleep(RATE)
        wn = merge.norm_artist(name)
        if d is None:
            _retry(conn, 'INSERT OR REPLACE INTO mb_artist '
                   '(query,norm,ts,score) VALUES (?,?,?,?)', (name, wn, time.time(), -1))
        else:
            a, ncand = best(d.get('artists', []), wn)
            if a:
                _retry(conn,
                    'INSERT OR REPLACE INTO mb_artist '
                    '(query,norm,mb_name,mb_sort,mbid,score,type,country,disamb,n_cand,ts) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (name, wn, a.get('name'), a.get('sort-name'), a.get('id'),
                     a.get('score'), a.get('type'), a.get('country'),
                     a.get('disambiguation'), ncand, time.time()))
            else:
                _retry(conn, 'INSERT OR REPLACE INTO mb_artist '
                       '(query,norm,ts,score,n_cand) VALUES (?,?,?,?,?)',
                       (name, wn, time.time(), 0, len(d.get('artists', []))))
        if (i + 1) % 25 == 0:
            try: conn.commit()
            except sqlite3.OperationalError: pass
            el = time.time() - t0
            if verbose:
                print(f'   {i+1}/{len(todo)}  eta {(len(todo)-i-1)*RATE/60:.0f}m', flush=True)
    conn.commit()

def library_artists(conn):
    """Distinct artist strings worth asking about."""
    seen = {}
    for col in ('albumartist', 'artist'):
        for v, n in conn.execute(
                f'SELECT {col},COUNT(*) FROM files WHERE kind="audio" '
                f'AND {col} IS NOT NULL AND {col}!="" GROUP BY {col}'):
            v = v.strip()
            if not v or v.lower() == 'various artists':
                continue
            if len(v) > 70:                 # torrent-style folder junk, never a real name
                continue
            seen[v] = seen.get(v, 0) + n
    return [k for k, _ in sorted(seen.items(), key=lambda kv: -kv[1])]

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mb_cache.db')

def cache_db():
    """Dedicated file for the long network run.

    The first attempt at this died on "database is locked": a 25-minute job holding
    library.db loses a race with anything else that opens it. Keeping the fetch in
    its own file removes the contention entirely, and the result is folded back in
    one short transaction at the end.
    """
    c = sqlite3.connect(CACHE, timeout=120)
    c.executescript(SCHEMA)
    # Without WAL a plain reader blocks the writer, and checking progress from
    # another shell is enough to kill a half-hour run. This is what took out the
    # first two attempts.
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=120000')
    return c

def merge_into_library():
    lib = musiclib.db()
    lib.executescript(SCHEMA)
    cc = sqlite3.connect(CACHE, timeout=120)
    cols = ('query,norm,mb_name,mb_sort,mbid,score,type,country,disamb,n_cand,ts')
    rows = cc.execute(f'SELECT {cols} FROM mb_artist').fetchall()
    lib.executemany(f'INSERT OR REPLACE INTO mb_artist ({cols}) '
                    f'VALUES ({",".join("?"*11)})', rows)
    lib.commit(); lib.close(); cc.close()
    return len(rows)

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int)
    ap.add_argument('--merge-only', action='store_true')
    a = ap.parse_args()
    if a.merge_only:
        print('merged', merge_into_library(), 'rows into library.db'); sys.exit()
    lib = musiclib.db()
    names = library_artists(lib)
    cols = 'query,norm,mb_name,mb_sort,mbid,score,type,country,disamb,n_cand,ts'
    try:
        seed = lib.execute(f'SELECT {cols} FROM mb_artist').fetchall()
    except sqlite3.OperationalError:
        seed = []
    lib.close()
    conn = cache_db()
    if seed:
        conn.executemany(f'INSERT OR REPLACE INTO mb_artist ({cols}) '
                         f'VALUES ({",".join("?"*11)})', seed)
        conn.commit()
    if a.limit: names = names[:a.limit]
    run(conn, names)
    hit = conn.execute('SELECT COUNT(*) FROM mb_artist WHERE mb_name IS NOT NULL').fetchone()[0]
    tot = conn.execute('SELECT COUNT(*) FROM mb_artist').fetchone()[0]
    conn.close()
    print(f'resolved {hit}/{tot}')
    print('merged', merge_into_library(), 'rows into library.db')

# ---------------------------------------------------------------- sort-order names
SUFFIX = {'jr', 'jr.', 'sr', 'sr.', 'ii', 'iii', 'iv', 'the', 'a', 'an'}

def flip_inverted(name):
    """'Taylor,James' -> 'James Taylor'. Returns None when it is not an inversion.

    Only a genuine Surname,Firstname pair flips. Commas also appear inside real
    names -- "10,000 Maniacs", "Earth, Wind & Fire", "Crosby, Stills, Nash & Young",
    "Harry Connick, Jr." -- and flipping any of those would wreck them, so the test
    is deliberately narrow: exactly one comma, no ampersand or slash, no digits, no
    name suffix, and at most two words on either side.
    """
    s = (name or '').strip()
    if s.count(',') != 1 or '&' in s or '/' in s or ' and ' in s.lower():
        return None
    if any(ch.isdigit() for ch in s):
        return None
    left, right = [p.strip() for p in s.split(',')]
    if not left or not right:
        return None
    if right.lower().rstrip('.') in SUFFIX or left.lower() in SUFFIX:
        return None
    if len(left.split()) > 2 or len(right.split()) > 2:
        return None
    return f'{right} {left}'

def is_stub(row_type, row_country):
    """MusicBrainz rows with neither a type nor a country are throwaway entries.

    They are how "OMD" matched a Ragga Dancehall act and "Buddha Bar" matched an
    Amazon scrape. Declining them keeps the name we already had.
    """
    return not row_type and not row_country
