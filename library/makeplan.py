"""Generate the full merge plan.

Three phases so the expensive part is only paid where it changes an outcome:
  1. pair albums and match tracks acoustically   (cheap)
  2. measure spectral cutoff on the files that are actually in contention
     (expensive: a real decode each, so parallel and cached)
  3. decide winners with that evidence in hand
"""
import sqlite3, json, time, sys, collections, os
from concurrent.futures import ThreadPoolExecutor
import merge, quality, musiclib

def main():
    conn = sqlite3.connect('library.db'); conn.row_factory = sqlite3.Row
    albums = merge.load_albums(conn)
    groups, singles = merge.pair_albums(albums)
    print(f'{len(groups)} cross-pool groups, {len(singles)} unpaired albums')

    # ---- phase 1: match tracks
    t0 = time.time(); fpc = {}; matched = []
    for i, g in enumerate(groups):
        ms = sorted(g['members'],
                    key=lambda m: merge.POOLS.index(m['src']) if m['src'] in merge.POOLS else 99)
        base = merge.choose_destination(ms)
        others = [m for m in ms if m['folder'] != base['folder']]
        recs = []
        for o in others:
            pairs, only_base, only_other = merge.match_tracks(base, o, fpc)
            recs.append(dict(other=o, pairs=pairs, only_base=only_base, only_other=only_other))
        matched.append(dict(g=g, base=base, recs=recs))
        if (i+1) % 100 == 0: print(f'   matched {i+1}/{len(groups)}', flush=True)
    print(f'phase 1 done in {time.time()-t0:.0f}s')

    # ---- phase 2: cutoffs, only for files in contention
    need = set()
    for m in matched:
        for r in m['recs']:
            for x, y, how, d in r['pairs']:
                need.add(x['path']); need.add(y['path'])
    need = sorted(need)
    # reuse anything measured on a previous run -- a decode each, so worth caching
    cutoffs = {r[0]: r[1] for r in
               conn.execute('SELECT path,cutoff FROM files WHERE cutoff IS NOT NULL')}
    todo = [p for p in need if p not in cutoffs]
    print(f'phase 2: {len(need)} contested files, {len(need)-len(todo)} cached, '
          f'measuring {len(todo)}')
    t0 = time.time()
    if todo:
        wconn = sqlite3.connect('library.db', timeout=60)
        batch = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for i, (p, c) in enumerate(zip(todo, ex.map(quality.measure_cutoff, todo))):
                cutoffs[p] = c
                batch.append((c, time.time(), p))
                if len(batch) >= 300:
                    wconn.executemany('UPDATE files SET cutoff=?,cutoff_at=? WHERE path=?', batch)
                    wconn.commit(); batch.clear()
                if (i+1) % 500 == 0:
                    el = time.time()-t0
                    print(f'   {i+1}/{len(todo)}  {(i+1)/el:.1f}/s  '
                          f'eta {(len(todo)-i-1)/max((i+1)/el,.1)/60:.0f}m', flush=True)
        if batch:
            wconn.executemany('UPDATE files SET cutoff=?,cutoff_at=? WHERE path=?', batch)
        wconn.commit(); wconn.close()
    print(f'phase 2 done in {time.time()-t0:.0f}s')

    # ---- phase 3: decide
    plan = {'groups': [], 'stats': collections.Counter()}
    for m in matched:
        base = m['base']
        entry = dict(base=base['folder'], base_src=base['src'],
                     how=m['g']['how'],
                     albums=[dict(folder=x['folder'], src=x['src'], n=len(x['tracks']))
                             for x in m['g']['members']],
                     decisions=[], fills=[])
        for r in m['recs']:
            for x, y, how, d in r['pairs']:
                w, l, why = merge.pick_winner(x, y, cutoffs)
                dec = dict(match=how, dist=None if d is None else round(d, 3),
                           keep=w['path'], drop=l['path'], why=why,
                           keep_br=w['bitrate'], drop_br=l['bitrate'],
                           keep_ext=w['ext'], drop_ext=l['ext'],
                           keep_cut=cutoffs.get(w['path']), drop_cut=cutoffs.get(l['path']))
                dec['confidence'] = merge.classify(dec)
                entry['decisions'].append(dec)
                plan['stats']['pairs'] += 1
                plan['stats']['conf:' + dec['confidence']] += 1
                plan['stats']['winner:' + (w['src'] if w['src'] else '?')] += 1
            for t in r['only_other']:
                entry['fills'].append(dict(path=t['path'], src=r['other']['src'],
                                           track=t['track'], title=t['title'],
                                           br=t['bitrate'], ext=t['ext']))
                plan['stats']['fill'] += 1
        plan['groups'].append(entry)

    plan['singles'] = [dict(folder=a['folder'], src=a['src'], n=len(a['tracks']))
                       for a in singles]
    plan['stats'] = dict(plan['stats'])
    json.dump(plan, open('mergeplan.json', 'w', encoding='utf-8'), indent=1)
    print('\n=== PLAN SUMMARY ===')
    for k, v in sorted(plan['stats'].items()): print(f'   {k:34} {v}')
    print('\nwritten to mergeplan.json')

if __name__ == '__main__':
    main()
