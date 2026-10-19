"""Resolve the album fragments that are alternate masterings, keeping the better copy.

These 31 pairs are the same songs at the same track numbers, but the audio
genuinely differs (chromaprint 0.15-0.27, well above the 0.15 that marks a
re-encode of one master). So neither "they are duplicates, drop one" nor "they
are different songs, keep both" is quite true: they are competing copies, and
the library's existing policy applies -- keep the better one.

File date cannot decide it. Every mtime in this library is the date the files
were copied to this machine, so provenance was flattened long ago. Codec cannot
decide it either: the higher-bitrate AAC is usually but NOT always better --
"Fresh Aire 2" has a 256k m4a that reaches only 15.8kHz against a 155k mp3
reaching 19.2kHz, which is what an upscaled transcode looks like.

So the decision is made the way the original merge made it: by measured
spectral cutoff scored against the competing copy, since the two hold the same
audio and the higher cutoff is the best estimate of what the source really had.

Losers move to _superseded; a winning fragment is moved INTO the album folder
so the album ends up with one copy of each song, at the best quality available.
Everything is logged and reversible.
"""
import os, re, sys, sqlite3, collections, pickle
from pathlib import Path

import musiclib, merge, quality

HERE = Path(__file__).resolve().parent
JUKE = HERE.parent / 'jukebox' / 'jukebox.db'
OP = 'fragment-quality'
MARGIN = 1.0          # scores this close are not a real difference


def norm(s):
    s = (s or '').lower()
    s = re.sub(r'\b(the|a|an)\b', '', s)
    return re.sub(r'[^a-z0-9]', '', s)


def build_pairs():
    """[(full_album, frag_album, frag_path, album_path, ber)] still unresolved."""
    c = sqlite3.connect(f'file:{JUKE}?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    alb = {r['id']: dict(r) for r in c.execute(
        """SELECT album.id, album.title, album.n_tracks, album.folder, album.collection_id,
                  COALESCE(album.is_compilation,0) comp FROM album""")}
    titles, paths = collections.defaultdict(set), collections.defaultdict(list)
    for r in c.execute('SELECT album_id, title, rel_path FROM track'):
        titles[r['album_id']].add(norm(r['title']))
        paths[r['album_id']].append(r['rel_path'])
    c.close()

    by = collections.defaultdict(list)
    for a in alb.values():
        if a['collection_id'] or a['comp']:
            continue
        by[norm(a['title'])].append(a)

    conn = musiclib.db()
    conn.row_factory = sqlite3.Row
    info = {r['path']: dict(r) for r in conn.execute(
        "SELECT path,fp,bitrate,ext,codec,brmode,cutoff FROM files WHERE kind='audio'")}
    out = []
    for _t, v in by.items():
        if len(v) < 2:
            continue
        v.sort(key=lambda a: -(a['n_tracks'] or 0))
        full = v[0]
        fulls = [(p, musiclib.unpack_fp(info[p]['fp']))
                 for p in paths[full['id']] if p in info and info[p].get('fp')]
        for frag in v[1:]:
            if not titles[frag['id']] or frag['n_tracks'] >= full['n_tracks']:
                continue
            if len(titles[full['id']] & titles[frag['id']]) / float(len(titles[frag['id']])) < 0.5:
                continue
            for gp in paths[frag['id']]:
                r = info.get(gp)
                if not r or not r.get('fp'):
                    continue
                g = musiclib.unpack_fp(r['fp'])
                best, bd = None, 1.0
                for p, f in fulls:
                    d = merge.fp_distance(g, f)
                    if d < bd:
                        bd, best = d, p
                if best is None or bd <= merge.FP_SAME:
                    continue                    # exact dupes were handled already
                out.append((full, frag, gp, best, bd, info))
    conn.close()
    return out


def main(apply=False):
    todo = build_pairs()
    conn = musiclib.db()
    conn.row_factory = sqlite3.Row
    print('alternate-mastering pairs: %d' % len(todo))
    keep_f = keep_a = skip = 0
    actions = []
    for full, frag, gp, bp, bd, info in todo:
        a, b = info[gp], info[bp]
        sa, _ = quality.score_pair(a, a.get('cutoff'), b.get('cutoff'))
        sb, _ = quality.score_pair(b, b.get('cutoff'), a.get('cutoff'))
        if abs(sa - sb) <= MARGIN:
            skip += 1
            continue
        if sa > sb:
            actions.append(('swap', full, gp, bp, sa, sb))   # fragment is better
            keep_f += 1
        else:
            actions.append(('drop', full, gp, bp, sa, sb))   # album copy is better
            keep_a += 1

    for what, full, gp, bp, sa, sb in actions:
        print('  %-26s %-4s %s' % ((full['title'] or '')[:26], what, gp.split('/')[-1][:44]))
        if not apply:
            continue
        if what == 'drop':
            musiclib._move(conn, gp, musiclib.SUPERSEDED, OP,
                           'lower quality than the album copy (%.0f vs %.0f)' % (sa, sb), False)
        else:
            # retire the album's copy first so the better file can take its place
            musiclib._move(conn, bp, musiclib.SUPERSEDED, OP,
                           'replaced by a better copy (%.0f vs %.0f)' % (sb, sa), False)
            musiclib._move_to(conn, gp, full['folder'] + '/' + gp.split('/')[-1], OP,
                              'better copy moved into %s' % full['folder'])
    if apply:
        conn.commit()
    print('')
    print('%s  fragment better: %d, album better: %d, too close: %d'
          % ('APPLIED' if apply else 'DRY RUN', keep_f, keep_a, skip))
    conn.close()


if __name__ == '__main__':
    main(apply='--apply' in sys.argv)
