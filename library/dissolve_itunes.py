"""Fold the nested "Itunes Matched" tree into the main library.

What is left there after the merge has no counterpart in the main tree, so it is
real music that simply needs a home rather than anything to discard.

Where the artist already has a folder in the main tree, the tracks go into *that*
folder, spelling and all -- creating "The B-52's" next to an existing "B-52s" would
split the artist in two, which is the thing this whole exercise is trying to undo.
"""
import os, sys, time, sqlite3, argparse, collections, re
import musiclib, merge, mbresolve

ROOT = musiclib.ROOT
SRC_PREFIX = 'VariousArtists/Itunes Matched/'

def main_tree_artists(conn):
    """normalised artist -> existing folder name in the main tree."""
    out = {}
    for (folder,) in conn.execute(
            'SELECT DISTINCT folder FROM files WHERE kind="audio" '
            'AND path LIKE "VariousArtists/%" AND path NOT LIKE ?',
            (SRC_PREFIX + '%',)):
        parts = folder.split('/')
        if len(parts) < 2:
            continue
        artist = parts[1]
        key = merge.norm_artist(artist)
        if not key:
            continue
        # prefer the spelling that already holds the most tracks
        out.setdefault(key, collections.Counter())[artist] += 1
    return {k: v.most_common(1)[0][0] for k, v in out.items()}

_ALIAS_CACHE = {}

def artist_aliases(conn, name):
    """Every spelling this artist might already be filed under, best first.

    Covers the sort-inverted form ("Smith,Will" -> "Will Smith"), the trailing
    article ("Doors, The" -> "The Doors") and the MusicBrainz canonical name.
    """
    if name in _ALIAS_CACHE:
        return _ALIAS_CACHE[name]
    out = [name]
    flipped = mbresolve.flip_inverted(name)
    if flipped:
        out.append(flipped)
    m = re.match(r'^(.*),\s*(the|a|an)$', name.strip(), re.I)
    if m:
        out.append(f'{m.group(2)} {m.group(1)}')
    for cand in list(out):
        row = conn.execute('SELECT mb_name FROM mb_artist WHERE query=? '
                           'AND mb_name IS NOT NULL', (cand,)).fetchone()
        if row and row[0] not in out:
            out.append(row[0])
    _ALIAS_CACHE[name] = out
    return out

def plan(conn):
    existing = main_tree_artists(conn)
    rows = conn.execute(
        'SELECT path,folder,album FROM files WHERE path LIKE ?',
        (SRC_PREFIX + '%',)).fetchall()
    moves, stats = [], collections.Counter()
    for path, folder, album in rows:
        rest = folder[len(SRC_PREFIX):] if folder.startswith(SRC_PREFIX) else None
        if rest is None:
            continue
        parts = rest.split('/')
        artist = parts[0]
        albdir = '/'.join(parts[1:]) if len(parts) > 1 else ''
        # Match the existing folder through every spelling of the same artist:
        # "Smith,Will" and "Will Smith" normalise to different keys, so a plain
        # lookup would create a second folder for an artist already in the tree.
        target_artist, matched = artist, False
        for cand in artist_aliases(conn, artist):
            hit = existing.get(merge.norm_artist(cand))
            if hit:
                target_artist, matched = hit, True
                break
        stats['reused existing artist folder' if matched
              else 'new artist folder'] += 1
        if not albdir:
            albdir = 'Unknown Album'
        elif re.search(r'unknown', albdir, re.I):
            # the chosen rule: fall back to the album tag when there is one,
            # otherwise keep it visible under Unknown Album
            albdir = album.strip() if album and not re.search(r'unknown', album, re.I) \
                     else 'Unknown Album'
            stats['unknown-album track'] += 1
        dest = f'VariousArtists/{target_artist}/{albdir}'
        if dest == folder:
            continue
        moves.append((path, dest))
    return moves, stats, existing

def run(conn, moves, dry=True):
    done, errs = 0, []
    for src_rel, dest in moves:
        newrel = f'{dest}/{os.path.basename(src_rel)}'
        if dry:
            done += 1; continue
        src = os.path.join(ROOT, src_rel.replace('/', os.sep))
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
            conn.execute('INSERT INTO moves (ts,op,reason,src,dst,size) '
                         'VALUES (?,?,?,?,?,?)',
                         (time.time(), 'itunes', 'folded iTunes Matched into main tree',
                          src_rel, newrel, size))
            conn.execute('UPDATE files SET path=?, folder=?, src=? WHERE path=?',
                         (newrel, dest, 'VariousArtists', src_rel))
            done += 1
        except OSError as e:
            errs.append((src_rel, str(e)))
        if done % 200 == 0:
            conn.commit()
    if not dry: conn.commit()
    return done, errs

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    conn = musiclib.db()
    moves, stats, existing = plan(conn)
    print(f'{"APPLYING" if a.apply else "DRY RUN"}: {len(moves)} files to fold in')
    for k, v in stats.most_common(): print(f'   {k:34} {v}')
    print('\n  destinations:')
    dests = collections.Counter(d for _, d in moves)
    for d, n in dests.most_common(16):
        print(f'     {n:3}  {d}')
    print(f'     ... {len(dests)} destination folders in total')
    done, errs = run(conn, moves, dry=not a.apply)
    print(f'\n{"would move" if not a.apply else "moved"} {done}, errors {len(errs)}')
    for p, e in errs[:6]: print(f'   {p}: {e}')
    if a.apply:
        pruned = musiclib.prune_empty_dirs(os.path.join(ROOT, 'VariousArtists'))
        print(f'pruned {pruned} empty directories')
    conn.close()
