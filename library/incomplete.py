"""Flag albums that look incomplete, with confidence, avoiding the obvious traps.

Two traps in this library:
  - multi-disc box sets were flattened into one folder with continuous numbering
    (track 1..43), so "gaps" there are often just discs, not missing songs
  - single/EP folders legitimately hold 1-3 tracks
"""
import sqlite3, re, collections, json, sys
import merge

def analyse(conn):
    albums = merge.load_albums(conn)
    out = []
    for folder, a in albums.items():
        tr = a['tracks']
        nums = sorted(t['track'] for t in tr if t['track'])
        tots = [t['tracktotal'] for t in tr if t['tracktotal']]
        n = len(tr)
        discs = {merge.disc_of(folder, t) for t in tr}
        discs.discard(None)
        multidisc = len(discs) > 1 or bool(re.search(r'\b(disc|cd)\s*\d', folder, re.I))

        verdict, conf, detail = None, 0, ''
        if nums:
            mx = max(nums)
            missing = sorted(set(range(1, mx + 1)) - set(nums))
            dupes = len(nums) - len(set(nums))
            if tots:
                tot = collections.Counter(tots).most_common(1)[0][0]
                if n < tot:
                    verdict, conf = 'incomplete', 0.95
                    detail = f'{n} of {tot} tracks per tag (missing {tot-n})'
            if not verdict and missing:
                if n >= mx:
                    # as many files as the highest number, yet gaps: the folder
                    # holds repeats (alternate takes, a merged second copy), it is
                    # not missing anything
                    verdict, conf = 'duplicate-track-numbers', 0.6
                    detail = (f'{n} files but numbered only to {mx} - '
                              f'{n - len(set(nums))} repeated number(s), gaps at {missing[:8]}')
                # continuous numbering well past a normal album = flattened box set
                elif mx > 30 and len(missing) / mx < 0.25:
                    verdict, conf = 'likely-boxset', 0.3
                    detail = f'{n} tracks numbered to {mx}, {len(missing)} gaps - probably flattened discs'
                elif mx <= 30:
                    verdict, conf = 'incomplete', 0.8 if len(missing) <= mx * 0.5 else 0.6
                    detail = f'{n} of ~{mx} tracks, missing {missing[:15]}'
                else:
                    verdict, conf = 'incomplete', 0.5
                    detail = f'{n} tracks, numbered to {mx}, {len(missing)} missing'
            if not verdict and dupes and not multidisc:
                verdict, conf = 'duplicate-track-numbers', 0.5
                detail = f'{dupes} repeated track numbers in one folder'
        if not verdict and n <= 3:
            is_single = re.search(r'(single|cd-?s|maxi|ep|remixes)', folder, re.I)
            verdict = 'known-single' if is_single else 'stray-or-single'
            conf = 0.3 if is_single else 0.7
            detail = f'only {n} track(s)' + (' (folder says single/EP)' if is_single else '')
        if not verdict and not nums:
            verdict, conf = 'untracked', 0.6
            detail = f'{n} files, no track numbers at all'

        if verdict:
            out.append(dict(folder=folder, src=a['src'], artist=a['artist'],
                            album=a['album'], n=n, verdict=verdict,
                            confidence=conf, detail=detail))
    return out

if __name__ == '__main__':
    conn = sqlite3.connect('library.db'); conn.row_factory = sqlite3.Row
    res = analyse(conn)
    by = collections.Counter(r['verdict'] for r in res)
    print(f'{len(res)} albums flagged out of {len(merge.load_albums(conn))}\n')
    for k, v in by.most_common(): print(f'   {k:26} {v}')
    print()
    for verdict in ('incomplete', 'stray-or-single', 'untracked',
                    'duplicate-track-numbers', 'likely-boxset', 'known-single'):
        rows = [r for r in res if r['verdict'] == verdict]
        rows.sort(key=lambda r: -r['confidence'])
        print(f'\n===== {verdict} ({len(rows)}) =====')
        for r in rows[:20]:
            print(f'  [{r["confidence"]:.2f}] {r["folder"][:66]}')
            print(f'         {r["detail"]}')
    json.dump(res, open('incomplete.json', 'w', encoding='utf-8'), indent=1)
    print('\nfull list -> incomplete.json')
