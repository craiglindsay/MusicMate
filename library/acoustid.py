"""Identify tracks by their audio via AcoustID.

Needed because in the folders we care about the tags are the unreliable evidence:
"Black Eyed Peas/The E_N_D_" is tagged end to end as Morrissey, and no amount of
local reasoning can settle that. The waveform can.

Results land in their own SQLite file. A long network job holding the main library
database is what killed two earlier runs, so this one never opens it.
"""
import os, sys, json, time, sqlite3, subprocess, urllib.request, urllib.parse, argparse
import musiclib

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, 'acoustid_cache.db')
KEYFILE = os.path.join(HERE, '.acoustid_key')
UA = 'MusicLibraryTidy/1.0 (personal music library maintenance)'
RATE = 0.35                     # AcoustID allows 3 requests/second

SCHEMA = """
CREATE TABLE IF NOT EXISTS ident (
  path      TEXT PRIMARY KEY,
  score     REAL,
  acoustid  TEXT,
  rec_id    TEXT,
  artist    TEXT,
  title     TEXT,
  album     TEXT,      -- best release-group title
  ts        REAL,
  err       TEXT
);
"""

def cache_db():
    c = sqlite3.connect(CACHE, timeout=120)
    c.executescript(SCHEMA)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=120000')
    return c

def key():
    return open(KEYFILE, encoding='utf-8').read().strip()

def fingerprint(rel):
    """Compressed fingerprint + duration, which is what the web service wants."""
    full = os.path.join(musiclib.ROOT, rel.replace('/', os.sep))
    try:
        o = subprocess.run([musiclib.FPCALC, '-length', '120', full],
                           capture_output=True, text=True, timeout=180)
        fp = dur = None
        for line in o.stdout.splitlines():
            if line.startswith('FINGERPRINT='): fp = line[12:]
            elif line.startswith('DURATION='):  dur = int(float(line[9:]))
        return fp, dur
    except Exception:
        return None, None

def lookup(fp, dur, k, tries=4):
    # meta must be SPACE separated. Written as "recordings+releasegroups" the
    # urlencoder escapes the plus to %2B, the server sees one unknown meta name and
    # quietly returns scores with no metadata -- a silent no-op, not an error.
    q = {'client': k, 'format': 'json', 'meta': 'recordings releasegroups',
         'duration': dur, 'fingerprint': fp}
    data = urllib.parse.urlencode(q).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(
                'https://api.acoustid.org/v2/lookup', data=data,
                headers={'User-Agent': UA,
                         'Content-Type': 'application/x-www-form-urlencoded'})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except Exception as e:
            if getattr(e, 'code', None) == 400:
                return None
            time.sleep(min(30, 2 * (2 ** i)))
    return None

def best(resp):
    """Highest-scoring recording, with the release group most likely to be its album."""
    if not resp or resp.get('status') != 'ok':
        return None
    for res in sorted(resp.get('results', []), key=lambda r: -(r.get('score') or 0)):
        for rec in (res.get('recordings') or []):
            if not rec.get('title'):
                continue
            artists = ', '.join(a.get('name', '') for a in (rec.get('artists') or []))
            groups = [g.get('title') for g in (rec.get('releasegroups') or [])
                      if g.get('title')]
            # prefer an album over a compilation/single when the choice exists
            album = None
            for g in (rec.get('releasegroups') or []):
                if g.get('type') == 'Album':
                    album = g.get('title'); break
            if not album and groups:
                album = groups[0]
            return dict(score=res.get('score'), acoustid=res.get('id'),
                        rec_id=rec.get('id'), artist=artists or None,
                        title=rec.get('title'), album=album)
    return None

def run(paths, verbose=True):
    k = key()
    conn = cache_db()
    have = {r[0] for r in conn.execute('SELECT path FROM ident')}
    todo = [p for p in paths if p not in have]
    if verbose:
        print(f'{len(paths)} tracks, {len(paths)-len(todo)} cached, looking up {len(todo)}')
        print(f'  ~{len(todo)*RATE/60:.0f} min at {RATE}s/request')
    t0 = time.time()
    for i, rel in enumerate(todo):
        fp, dur = fingerprint(rel)
        if not fp or not dur:
            conn.execute('INSERT OR REPLACE INTO ident (path,ts,err) VALUES (?,?,?)',
                         (rel, time.time(), 'no fingerprint'))
        else:
            resp = lookup(fp, dur, k)
            b = best(resp)
            if b:
                conn.execute(
                    'INSERT OR REPLACE INTO ident '
                    '(path,score,acoustid,rec_id,artist,title,album,ts) '
                    'VALUES (?,?,?,?,?,?,?,?)',
                    (rel, b['score'], b['acoustid'], b['rec_id'], b['artist'],
                     b['title'], b['album'], time.time()))
            else:
                conn.execute('INSERT OR REPLACE INTO ident (path,ts,err) VALUES (?,?,?)',
                             (rel, time.time(),
                              'no match' if resp else 'lookup failed'))
        time.sleep(RATE)
        if (i + 1) % 50 == 0:
            try: conn.commit()
            except sqlite3.OperationalError: pass
            if verbose:
                el = time.time() - t0
                print(f'   {i+1}/{len(todo)}  eta {(len(todo)-i-1)*RATE/60:.0f}m', flush=True)
    conn.commit()
    n = conn.execute('SELECT COUNT(*) FROM ident WHERE title IS NOT NULL').fetchone()[0]
    tot = conn.execute('SELECT COUNT(*) FROM ident').fetchone()[0]
    conn.close()
    return n, tot

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--phase', choices=['1', '2', 'all'], default='all')
    ap.add_argument('--limit', type=int)
    a = ap.parse_args()
    tg = json.load(open('acoustid_targets.json', encoding='utf-8'))
    lib = musiclib.db()
    def tracks(folders):
        out = []
        for f in folders:
            out += [r[0] for r in lib.execute(
                'SELECT path FROM files WHERE folder=? AND kind="audio"', (f,))]
        return out
    paths = []
    if a.phase in ('1', 'all'): paths += tracks(tg['clob'])
    if a.phase in ('2', 'all'): paths += tracks(tg['dup'])
    lib.close()
    seen, uniq = set(), []
    for p in paths:
        if p not in seen: seen.add(p); uniq.append(p)
    if a.limit: uniq = uniq[:a.limit]
    n, tot = run(uniq)
    print(f'\nidentified {n}/{tot}')
