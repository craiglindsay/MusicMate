"""Move tracks that are sitting under the wrong artist.

Some folders in this library are mislabelled containers: "U2/No Line On The Horizon"
holds nothing but Coldplay tracks, "Black Eyed Peas/The E_N_D_" holds none of that
album at all. Where a track's own artist tag disagrees with the folder it sits in,
the tag is the better evidence -- it came from the rip, the folder name did not.

Compilations are exempt: an album by many artists is supposed to have many artist
tags, and refiling those would dismantle it.
"""
import os, sys, time, sqlite3, argparse, collections, re
import musiclib, merge, mbresolve

ROOT = musiclib.ROOT

def sanitize(name):
    ILLEGAL = {'<': '(', '>': ')', ':': ' -', '"': "'", '/': '-', '\\': '-',
               '|': '-', '?': '', '*': ''}
    s = ''.join(ILLEGAL.get(ch, ch) for ch in (name or '') if ord(ch) >= 32)
    return re.sub(r'\s+', ' ', s).strip().rstrip('. ') or 'Unknown'

_alias = {}
def aliases(conn, name):
    if name in _alias: return _alias[name]
    out = [name]
    f = mbresolve.flip_inverted(name)
    if f: out.append(f)
    m = re.match(r'^(.*),\s*(the|a|an)$', (name or '').strip(), re.I)
    if m: out.append(f'{m.group(2)} {m.group(1)}')
    row = conn.execute('SELECT mb_name FROM mb_artist WHERE query=? AND mb_name IS NOT NULL',
                       (name,)).fetchone()
    if row and row[0] not in out: out.append(row[0])
    _alias[name] = out
    return out

def existing_artist_folders(conn):
    out = {}
    for (folder,) in conn.execute(
            'SELECT DISTINCT folder FROM files WHERE kind="audio" AND src="VariousArtists"'):
        parts = folder.split('/')
        if len(parts) < 2: continue
        out.setdefault(merge.norm_artist(parts[1]), collections.Counter())[parts[1]] += 1
    return {k: v.most_common(1)[0][0] for k, v in out.items()}

def plan(conn):
    existing = existing_artist_folders(conn)
    moves, skipped = [], collections.Counter()
    for (folder,) in conn.execute(
            'SELECT DISTINCT folder FROM files WHERE kind="audio" AND src="VariousArtists"'):
        parts = folder.split('/')
        if len(parts) < 3:
            continue
        rows = conn.execute('SELECT * FROM files WHERE folder=? AND kind="audio"',
                            (folder,)).fetchall()
        aa = collections.Counter(r['albumartist'] for r in rows if r['albumartist'])
        if aa and aa.most_common(1)[0][0].lower() == 'various artists':
            skipped['compilation'] += len(rows); continue
        fkey = merge.norm_artist(parts[1])
        candidates = []
        for r in rows:
            if not r['artist'] or not r['album']:
                continue
            akey = merge.norm_artist(r['artist'])
            if not akey or akey == fkey:
                continue
            # the folder may simply spell the artist differently
            if any(merge.norm_artist(c) == fkey for c in aliases(conn, r['artist'])):
                continue
            # a featured-artist credit is not a different artist
            if fkey and (fkey in akey or akey in fkey):
                skipped['artist substring (feat./variant)'] += 1; continue
            target_artist = existing.get(akey)
            if not target_artist:
                for c in aliases(conn, r['artist']):
                    if merge.norm_artist(c) in existing:
                        target_artist = existing[merge.norm_artist(c)]; break
            if not target_artist:
                target_artist = sanitize(r['artist'])
            dest = f'VariousArtists/{sanitize(target_artist)}/{sanitize(r["album"])}'
            if dest == folder:
                continue
            candidates.append((r['path'], dest, r['artist'], r['album']))
        # Only act on wholly mislabelled containers -- a folder none of whose tracks
        # are by the artist it is filed under. A folder that merely holds a few
        # strays, or a side project deliberately filed under the parent band
        # (Duran Duran/Arcadia), is a judgement call and stays put.
        # A junk container holds several unrelated artists. A folder whose tracks all
        # name ONE other artist is something else entirely -- a spelling variant
        # ("3 Dog Night" vs "Three Dog Night"), a misspelling ("Barry Mannalow"), or
        # a side project deliberately filed under the parent band (Duran Duran/
        # Arcadia). Refiling those would split an artist rather than tidy one.
        distinct = {merge.norm_artist(a) for _, _, a, _ in candidates}
        if candidates and len(candidates) == len([r for r in rows if r['artist']])                 and len(distinct) >= 2:
            moves.extend(candidates)
        elif candidates and len(distinct) < 2:
            skipped['single other artist - variant or side project'] += len(candidates)
        elif candidates:
            skipped['folder is mostly correct - strays left in place'] += len(candidates)
    return moves, skipped

def run(conn, moves, dry=True):
    done, errs = 0, []
    for rel, dest, art, alb in moves:
        newrel = f'{dest}/{os.path.basename(rel)}'
        if dry:
            done += 1; continue
        src = os.path.join(ROOT, rel.replace('/', os.sep))
        dst = os.path.join(ROOT, newrel.replace('/', os.sep))
        try:
            if not os.path.exists(src):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                b, e = os.path.splitext(dst); i = 2
                while os.path.exists(f'{b}__{i}{e}'): i += 1
                dst = f'{b}__{i}{e}'
                newrel = os.path.relpath(dst, ROOT).replace(os.sep, '/')
            size = os.path.getsize(src)
            os.replace(src, dst)
            conn.execute('INSERT INTO moves (ts,op,reason,src,dst,size) VALUES (?,?,?,?,?,?)',
                         (time.time(), 'refile',
                          f'artist tag says {art} / {alb}', rel, newrel, size))
            conn.execute('UPDATE files SET path=?, folder=? WHERE path=?',
                         (newrel, dest, rel))
            done += 1
        except OSError as e:
            errs.append((rel, str(e)))
        if done % 100 == 0:
            conn.commit()
    if not dry: conn.commit()
    return done, errs

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    conn = musiclib.db(); conn.row_factory = sqlite3.Row
    moves, skipped = plan(conn)
    print(f'{"APPLYING" if a.apply else "DRY RUN"}: {len(moves)} tracks sitting under the wrong artist')
    for k, v in skipped.most_common(): print(f'   exempt: {k:34} {v}')
    bysrc = collections.Counter(os.path.dirname(m[0]) for m in moves)
    print(f'\n  from {len(bysrc)} folders; the worst:')
    for f, n in bysrc.most_common(12):
        print(f'     {n:3}  {f[-62:]}')
    print('\n  sample destinations:')
    for rel, dest, art, alb in moves[:8]:
        print(f'     {os.path.basename(rel)[:40]:42} -> {dest[-52:]}')
    done, errs = run(conn, moves, dry=not a.apply)
    print(f'\n{"would move" if not a.apply else "moved"} {done}, errors {len(errs)}')
    for p, e in errs[:5]: print(f'   {p}: {e}')
    if a.apply:
        print('pruned', musiclib.prune_empty_dirs(os.path.join(ROOT, 'VariousArtists')), 'empty dirs')
    conn.close()
