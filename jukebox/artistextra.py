"""Top tracks and related artists, for artist radio and a "similar artists" shelf.

Ranking an artist's best songs needs a signal the library does not contain. Play
history only knows what you have already played, so it can never surface the
song you have not got round to, and file order says nothing at all. Deezer
publishes a popularity-ranked top-tracks list and a related-artists list per
artist, both keyless, and it is already the source of the artist photographs.

What is stored is only the ORDER and the names. Radio then matches those titles
against tracks you actually own, so it plays your files, ranked by how well
known the songs are -- and quietly skips anything you do not have.

Related artists are stored whole and filtered to your library at query time, so
the shelf changes as the library grows without refetching.
"""
import re, json, time, sqlite3, urllib.request, urllib.parse
from pathlib import Path

import artistinfo as ai

DB = Path(__file__).resolve().parent / 'jukebox.db'
GAP = 0.34

SCHEMA = """
CREATE TABLE IF NOT EXISTS artist_top (
  artist_id INTEGER, pos INTEGER, title TEXT, norm TEXT
);
CREATE INDEX IF NOT EXISTS ix_top ON artist_top(artist_id, pos);
CREATE TABLE IF NOT EXISTS artist_similar (
  artist_id INTEGER, pos INTEGER, name TEXT, norm TEXT, dz_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_sim ON artist_similar(artist_id, pos);
CREATE TABLE IF NOT EXISTS artist_dz (
  artist_id INTEGER PRIMARY KEY, dz_id INTEGER, dz_name TEXT, fetched REAL, status TEXT
);
"""


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def dz_artist(name):
    """The Deezer artist that is actually this artist -- ranked, never first-hit.

    Deezer holds several artists under one name and does not return the famous
    one first; taking the first match is what once put another performer's
    photograph on the band America."""
    url = 'https://api.deezer.com/search/artist?limit=10&q=' + urllib.parse.quote(name)
    try:
        data = (ai._get(url) or {}).get('data') or []
    except Exception:
        return None
    n = norm(name)
    exact = [d for d in data if norm(d.get('name')) == n and (d.get('nb_album') or 0) > 0]
    pool = exact or [d for d in data if (d.get('nb_album') or 0) > 0
                     and n and (n in norm(d.get('name')) or norm(d.get('name')) in n)]
    if not pool:
        return None
    pool.sort(key=lambda d: (-(d.get('nb_fan') or 0), -(d.get('nb_album') or 0)))
    return pool[0]


def dz_list(dz_id, what, limit=50):
    url = 'https://api.deezer.com/artist/%d/%s?limit=%d' % (dz_id, what, limit)
    try:
        return (ai._get(url) or {}).get('data') or []
    except Exception:
        return []


def build(db=DB, limit=None, only_missing=True, verbose=True, min_tracks=1):
    c = sqlite3.connect(db, timeout=60)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    done = set()
    if only_missing:
        done = {r[0] for r in c.execute("SELECT artist_id FROM artist_dz WHERE status='ok'")}
    todo = [r for r in c.execute('SELECT id, name, n_tracks FROM artist WHERE n_tracks >= ? '
                                 'ORDER BY n_tracks DESC', (min_tracks,))
            if r['id'] not in done]
    if limit:
        todo = todo[:limit]
    if verbose:
        print('artists to fetch top tracks and related for: %d' % len(todo))

    ntop = nsim = miss = 0
    for i, a in enumerate(todo, 1):
        d = dz_artist(a['name'])
        time.sleep(GAP)
        if not d:
            miss += 1
            c.execute('INSERT OR REPLACE INTO artist_dz (artist_id,dz_id,dz_name,fetched,status) '
                      'VALUES (?,?,?,?,?)', (a['id'], None, None, time.time(), 'none'))
            c.commit()
            continue
        top = dz_list(d['id'], 'top', 50)
        time.sleep(GAP)
        rel = dz_list(d['id'], 'related', 20)
        time.sleep(GAP)
        c.execute('DELETE FROM artist_top WHERE artist_id=?', (a['id'],))
        c.executemany('INSERT INTO artist_top (artist_id,pos,title,norm) VALUES (?,?,?,?)',
                      [(a['id'], n, t.get('title'), norm(t.get('title')))
                       for n, t in enumerate(top)])
        c.execute('DELETE FROM artist_similar WHERE artist_id=?', (a['id'],))
        c.executemany('INSERT INTO artist_similar (artist_id,pos,name,norm,dz_id) '
                      'VALUES (?,?,?,?,?)',
                      [(a['id'], n, r.get('name'), norm(r.get('name')), r.get('id'))
                       for n, r in enumerate(rel)])
        c.execute('INSERT OR REPLACE INTO artist_dz (artist_id,dz_id,dz_name,fetched,status) '
                  'VALUES (?,?,?,?,?)',
                  (a['id'], d['id'], d.get('name'), time.time(), 'ok'))
        c.commit()
        ntop += len(top)
        nsim += len(rel)
        if verbose and i % 25 == 0:
            print('  %4d/%d  %d top tracks, %d related so far  (last: %s)'
                  % (i, len(todo), ntop, nsim, a['name'][:26]))
    n_ok = c.execute("SELECT COUNT(*) FROM artist_dz WHERE status='ok'").fetchone()[0]
    c.close()
    if verbose:
        print('')
        print('%d artists resolved, %d top tracks and %d related artists stored (%d not found)'
              % (n_ok, ntop, nsim, miss))
    return n_ok, ntop, nsim


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int)
    ap.add_argument('--redo', action='store_true')
    a = ap.parse_args()
    build(limit=a.limit, only_missing=not a.redo)
