"""Apply the merge plan.

Pairwise decisions cannot just be replayed: in a three-way group the base album is
compared against each other album separately, so one file can win one comparison and
lose another. Instead we union the pairs into per-recording clusters and choose a
single winner across every copy, which is both consistent and order-independent.

Order of operations matters too -- losers leave the destination folder before winners
arrive, so a winner can take the filename its loser was occupying.
"""
import sqlite3, json, os, sys, time, collections, argparse
import merge, musiclib

ROOT = musiclib.ROOT

class UF:
    def __init__(self): self.p = {}
    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb: self.p[ra] = rb

def build(conn, plan, include_review=False):
    rows = {r['path']: r for r in conn.execute("SELECT * FROM files WHERE kind='audio'")}
    cutoffs = {r[0]: r[1] for r in
               conn.execute('SELECT path,cutoff FROM files WHERE cutoff IS NOT NULL')}
    moves, stats = [], collections.Counter()
    skipped_missing = 0

    for g in plan['groups']:
        dest = g['base']
        decs = [d for d in g['decisions']
                if d['confidence'] == 'auto' or (include_review and d['confidence'] == 'review')]
        if not decs and not g['fills']:
            continue

        uf = UF()
        for d in decs:
            if d['keep'] in rows and d['drop'] in rows:
                uf.union(d['keep'], d['drop'])

        clusters = collections.defaultdict(set)
        for d in decs:
            for p in (d['keep'], d['drop']):
                if p in rows: clusters[uf.find(p)].add(p)

        for members in clusters.values():
            cand = [rows[p] for p in members]
            if len(cand) < 2:
                continue
            # reduce to a single winner across every copy of this recording
            best = cand[0]
            for other in cand[1:]:
                best, _, _ = merge.pick_winner(best, other, cutoffs)
            for r in cand:
                if r['path'] == best['path']:
                    continue
                moves.append(dict(kind='supersede', src=r['path'], dest=None,
                                  why=f'superseded within {dest}'))
                stats['supersede'] += 1
            if os.path.dirname(best['path']) != dest:
                moves.append(dict(kind='promote', src=best['path'], dest=dest,
                                  why='best copy moves into merged album'))
                stats['promote'] += 1

        for f in g['fills']:
            if f['path'] not in rows:
                skipped_missing += 1; continue
            if os.path.dirname(f['path']) == dest:
                continue
            moves.append(dict(kind='fill', src=f['path'], dest=dest,
                              why=f'only copy of this track (from {f["src"]})'))
            stats['fill'] += 1

    # losers out first, then arrivals -- frees filenames in the destination
    order = {'supersede': 0, 'promote': 1, 'fill': 2}
    moves.sort(key=lambda m: order[m['kind']])
    if skipped_missing:
        print(f'  note: {skipped_missing} planned files no longer on disk, skipped')
    return moves, stats

def run(moves, dry=True):
    conn = musiclib.db()
    done, errs = collections.Counter(), []
    for i, m in enumerate(moves):
        try:
            if m['kind'] == 'supersede':
                if not dry:
                    musiclib._move(conn, m['src'], musiclib.SUPERSEDED, 'merge', m['why'], False)
                done['supersede'] += 1
            else:
                newrel = f"{m['dest']}/{os.path.basename(m['src'])}"
                if newrel == m['src']:
                    continue
                if not dry:
                    src = os.path.join(ROOT, m['src'].replace('/', os.sep))
                    dst = os.path.join(ROOT, newrel.replace('/', os.sep))
                    if not os.path.exists(src):
                        continue
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    if os.path.exists(dst):
                        b, e = os.path.splitext(dst); k = 2
                        while os.path.exists(f'{b}__{k}{e}'): k += 1
                        dst = f'{b}__{k}{e}'
                        newrel = os.path.relpath(dst, ROOT).replace(os.sep, '/')
                    try: size = os.path.getsize(src)
                    except OSError: size = None
                    os.replace(src, dst)
                    conn.execute(
                        'INSERT INTO moves (ts,op,reason,src,dst,size) VALUES (?,?,?,?,?,?)',
                        (time.time(), 'merge', m['why'], m['src'], newrel, size))
                    conn.execute('UPDATE files SET path=?, folder=? WHERE path=?',
                                 (newrel, m['dest'], m['src']))
                done[m['kind']] += 1
        except OSError as e:
            errs.append((m['src'], str(e)))
        if not dry and (i + 1) % 1000 == 0:
            conn.commit(); print(f'   {i+1}/{len(moves)}', file=sys.stderr, flush=True)
    if not dry: conn.commit()
    conn.close()
    return done, errs

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='actually move files')
    ap.add_argument('--include-review', action='store_true')
    a = ap.parse_args()
    conn = sqlite3.connect('library.db'); conn.row_factory = sqlite3.Row
    plan = json.load(open('mergeplan.json', encoding='utf-8'))
    moves, stats = build(conn, plan, a.include_review)
    print(f'{"APPLYING" if a.apply else "DRY RUN"}: {len(moves)} moves')
    for k, v in stats.most_common(): print(f'   {k:12} {v}')
    if not a.apply:
        print('\nsample:')
        for m in moves[:6] + moves[-6:]:
            print(f'   [{m["kind"]:9}] {m["src"][:74]}')
            if m['dest']: print(f'                -> {m["dest"][:70]}/')
    done, errs = run(moves, dry=not a.apply)
    print(f'\n{"would move" if not a.apply else "moved"}: {dict(done)}')
    if errs:
        print(f'errors: {len(errs)}')
        for p, e in errs[:10]: print(f'   {p}: {e}')
