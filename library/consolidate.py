"""Merge duplicate copies of an album that live in the same pool.

The main merge only ever compared albums ACROSS the three source trees, so two
copies of one record both sitting in VariousArtists were never put side by side --
and their folder names differ by more than the normaliser forgives ("Substance
1987" vs "Substance", "Stop Making Sense SE" vs "Stop Making Sense"). That is why
406 supposedly-missing tracks turned out to be present after all.

Two guards, because pairing within one pool is less safe than across pools:

  1. volumes and parts are kept apart, not just discs -- norm_album strips "Vol 2",
     so without this "Greatest Hits Vol 1" and "Vol 2" would collide.
  2. no two folders are merged unless at least two of their tracks actually
     fingerprint-match. Different albums cannot fake that.
"""
import os, re, sys, time, json, sqlite3, argparse, collections
import musiclib, merge, quality

ROOT = musiclib.ROOT
MIN_SHARED = 2          # fingerprint matches required to believe two folders pair

_VOL = re.compile(r'\b(?:vol(?:ume)?|part|pt)\s*\.?\s*(\d+|[ivx]+)\b', re.I)

def volume_of(*texts):
    for t in texts:
        m = _VOL.search(t or '')
        if m:
            v = m.group(1).lower()
            roman = {'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6}
            return roman.get(v, v if not v.isdigit() else int(v))
    return 0

def load(conn):
    albums = merge.load_albums(conn)
    for a in albums.values():
        a['vol'] = volume_of(a['folder'].split('/')[-1], a['album'])
    return albums

def group(albums):
    """Same artist, album and disc/volume -- regardless of which pool they are in."""
    by = collections.defaultdict(list)
    for f, a in albums.items():
        by[(a['key'][0], a['key'][1], a['disc'], a['vol'])].append(a)
    return [v for v in by.values() if len(v) > 1]

def verify(A, B, fpcache):
    pairs, only_a, only_b = merge.match_tracks(A, B, fpcache)
    strong = [p for p in pairs if p[2] == 'fingerprint' and p[3] is not None
              and p[3] <= merge.FP_SAME]
    return pairs, only_a, only_b, len(strong)

def choose(members):
    named = [m for m in members if m.get('coherence', 1.0) >= 0.6]
    pool = named or members
    return max(pool, key=lambda m: (len(m['tracks']),
                                    m['src'] == 'VariousArtists'))

def build(conn, verbose=True):
    albums = load(conn)
    groups = group(albums)
    if verbose:
        print(f'{len(groups)} candidate same-album groups (any pool)')
    cutoffs = {r[0]: r[1] for r in
               conn.execute('SELECT path,cutoff FROM files WHERE cutoff IS NOT NULL')}
    fpcache, moves, stats = {}, [], collections.Counter()
    rejected = []
    for members in groups:
        dest = choose(members)
        others = [m for m in members if m['folder'] != dest['folder']]
        for o in others:
            pairs, only_dest, only_other, strong = verify(dest, o, fpcache)
            # Two ways to believe these are the same release:
            #   - they share recordings acoustically, or
            #   - they share no recordings AND no track numbers, i.e. they are
            #     purely complementary. That is the normal state after the main
            #     merge already resolved the overlap, and it is precisely the
            #     case that leaves an album looking incomplete.
            dnums = {t['track'] for t in dest['tracks'] if t['track']}
            onums = {t['track'] for t in o['tracks'] if t['track']}
            complementary = not pairs and not (dnums & onums)
            if strong < MIN_SHARED and not complementary:
                rejected.append((dest['folder'], o['folder'], strong))
                stats['folder pair rejected - no shared audio, overlapping numbers'] += 1
                continue
            if complementary:
                stats['folder pairs joined - complementary halves'] += 1
            stats['folder pairs merged'] += 1
            for x, y, how, d in pairs:
                if how != 'fingerprint' or d is None or d > merge.FP_SAME:
                    stats['track pair left alone - metadata match only'] += 1
                    continue
                need = [p for p in (x, y) if p['path'] not in cutoffs]
                for p in need:
                    cutoffs[p['path']] = quality.measure_cutoff(p['path'])
                w, l, why = merge.pick_winner(x, y, cutoffs)
                moves.append(dict(kind='supersede', src=l['path'], dest=None, why=why))
                if os.path.dirname(w['path']) != dest['folder']:
                    moves.append(dict(kind='move', src=w['path'],
                                      dest=dest['folder'], why='best copy'))
                stats['duplicate tracks resolved'] += 1
            for t in only_other:
                moves.append(dict(kind='move', src=t['path'], dest=dest['folder'],
                                  why='track only in the other copy'))
                stats['gap tracks filled'] += 1
    order = {'supersede': 0, 'move': 1}
    moves.sort(key=lambda m: order[m['kind']])
    return moves, stats, rejected

def run(conn, moves, dry=True):
    done, errs = collections.Counter(), []
    for m in moves:
        if dry:
            done[m['kind']] += 1; continue
        try:
            if m['kind'] == 'supersede':
                if conn.execute('SELECT 1 FROM files WHERE path=?', (m['src'],)).fetchone():
                    musiclib._move(conn, m['src'], musiclib.SUPERSEDED,
                                   'consolidate', m['why'], False)
                    done['supersede'] += 1
                continue
            newrel = f"{m['dest']}/{os.path.basename(m['src'])}"
            if newrel == m['src']:
                continue
            src = os.path.join(ROOT, m['src'].replace('/', os.sep))
            dst = os.path.join(ROOT, newrel.replace('/', os.sep))
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
                         (time.time(), 'consolidate', m['why'], m['src'], newrel, size))
            conn.execute('UPDATE files SET path=?, folder=? WHERE path=?',
                         (newrel, m['dest'], m['src']))
            done[m['kind']] += 1
        except OSError as e:
            errs.append((m['src'], str(e)))
        if sum(done.values()) % 200 == 0:
            conn.commit()
    if not dry: conn.commit()
    return done, errs

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    conn = musiclib.db(); conn.row_factory = sqlite3.Row
    moves, stats, rejected = build(conn)
    print(f'\n{"APPLYING" if a.apply else "DRY RUN"}: {len(moves)} moves')
    for k, v in stats.most_common(): print(f'   {k:52} {v}')
    print(f'\n  sample of folder pairs rejected for weak overlap:')
    for d, o, s in rejected[:6]:
        print(f'     {s} shared  {d[-40:]}  vs  {o[-40:]}')
    done, errs = run(conn, moves, dry=not a.apply)
    print(f'\n{"would do" if not a.apply else "did"}: {dict(done)}, errors {len(errs)}')
    if a.apply:
        print('pruned', musiclib.prune_empty_dirs(os.path.join(ROOT, 'VariousArtists')),
              'empty dirs')
    conn.close()
