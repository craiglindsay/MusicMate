"""Work out which apparently-missing tracks are genuinely absent.

A gap in an album's numbering does not mean the music is gone. The track may sit in
a greatest-hits folder, on another disc of the same set, or in _superseded where the
merge retired it as a lesser copy but where it is still the only copy of *that song*.

So: index everything, including _superseded, then for each gap ask whether some
other copy of the same release supplies it.
"""
import os, sys, sqlite3, collections, argparse, json, re
import mutagen
import musiclib, merge, incomplete

ROOT = musiclib.ROOT
AUDIO = musiclib.AUDIO_EXT

SCHEMA = """
CREATE TABLE IF NOT EXISTS shelf (
  path     TEXT PRIMARY KEY,   -- relative to ROOT, inside _superseded
  folder   TEXT,
  filename TEXT,
  artist   TEXT,
  album    TEXT,
  title    TEXT,
  track    INTEGER,
  duration REAL,
  bitrate  INTEGER,
  ext      TEXT,
  size     INTEGER
);
"""

def index_shelf(conn, rescan=False):
    conn.executescript(SCHEMA)
    if not rescan and conn.execute('SELECT COUNT(*) FROM shelf').fetchone()[0]:
        return conn.execute('SELECT COUNT(*) FROM shelf').fetchone()[0]
    base = os.path.join(ROOT, musiclib.SUPERSEDED)
    if not os.path.isdir(base):
        return 0
    batch, n = [], 0
    for dp, _dn, fns in os.walk(base):
        for fn in fns:
            if os.path.splitext(fn)[1].lower() not in AUDIO:
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, ROOT).replace(os.sep, '/')
            t = musiclib.read_tags(full, os.path.splitext(fn)[1].lower())
            batch.append((rel, os.path.dirname(rel), fn, t.get('artist'),
                          t.get('album'), t.get('title'), t.get('track'),
                          t.get('duration'), t.get('bitrate'),
                          os.path.splitext(fn)[1].lower(),
                          os.path.getsize(full) if os.path.exists(full) else 0))
            n += 1
            if len(batch) >= 400:
                conn.executemany('INSERT OR REPLACE INTO shelf VALUES (?,?,?,?,?,?,?,?,?,?,?)', batch)
                conn.commit(); batch.clear()
                print(f'   indexed {n}', file=sys.stderr, flush=True)
    if batch:
        conn.executemany('INSERT OR REPLACE INTO shelf VALUES (?,?,?,?,?,?,?,?,?,?,?)', batch)
    conn.commit()
    return n

def album_key(artist, album):
    return (merge.norm_artist(artist or ''), merge.norm_album_safe(album or ''))

def build_index(conn):
    """(artist, album) -> {track number: [(where, path, bitrate, ext, title)]}"""
    idx = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in conn.execute('SELECT path,folder,artist,albumartist,album,title,track,'
                          'bitrate,ext FROM files WHERE kind="audio"'):
        a = r[3] or r[2]
        if not r[6]: continue
        idx[album_key(a, r[4])][r[6]].append(('library', r[0], r[7], r[8], r[5]))
    for r in conn.execute('SELECT path,folder,artist,album,title,track,bitrate,ext FROM shelf'):
        if not r[5]: continue
        idx[album_key(r[2], r[3])][r[5]].append(('superseded', r[0], r[6], r[7], r[4]))
    return idx

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rescan', action='store_true')
    a = ap.parse_args()
    conn = musiclib.db(); conn.row_factory = sqlite3.Row
    n = index_shelf(conn, a.rescan)
    print(f'_superseded indexed: {n} audio files')
    idx = build_index(conn)

    res = incomplete.analyse(conn)
    inc = [r for r in res if r['verdict'] == 'incomplete']
    recovered, absent, report = 0, 0, []
    for r in inc:
        folder = r['folder']
        rows = conn.execute('SELECT artist,albumartist,album,track FROM files '
                            'WHERE folder=? AND kind="audio"', (folder,)).fetchall()
        if not rows: continue
        have = {x['track'] for x in rows if x['track']}
        if not have: continue
        aa = collections.Counter((x['albumartist'] or x['artist']) for x in rows
                                 if (x['albumartist'] or x['artist']))
        al = collections.Counter(x['album'] for x in rows if x['album'])
        if not aa or not al: continue
        key = album_key(aa.most_common(1)[0][0], al.most_common(1)[0][0])
        mx = max(have)
        gaps = sorted(set(range(1, mx + 1)) - have)
        if not gaps: continue
        found = []
        for g in gaps:
            cands = [c for c in idx.get(key, {}).get(g, [])
                     if os.path.dirname(c[1]) != folder]
            if cands:
                found.append((g, cands[0]))
        recovered += len(found); absent += len(gaps) - len(found)
        report.append(dict(folder=folder, present=len(rows), gaps=gaps,
                           found=[(g, c[0], c[1], c[4]) for g, c in found],
                           still_missing=[g for g in gaps
                                          if g not in {x[0] for x in found}]))
    json.dump(report, open('gaps.json', 'w', encoding='utf-8'), indent=1)
    print(f'\nincomplete albums examined : {len(report)}')
    print(f'gap tracks found elsewhere : {recovered}')
    print(f'gap tracks genuinely absent: {absent}')
    print()
    withfound = [r for r in report if r['found']]
    print(f'albums where something was recoverable: {len(withfound)}')
    for r in sorted(withfound, key=lambda x: -len(x['found']))[:12]:
        print(f'   {r["folder"][-56:]}')
        for g, where, path, title in r['found'][:4]:
            print(f'      track {g:3} -> [{where}] {str(title)[:34]:36} {path[-40:]}')
    print('\nfull detail -> gaps.json')
    conn.close()

if __name__ == '__main__':
    main()
