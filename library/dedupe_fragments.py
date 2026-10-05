"""Fold stray album fragments back into the album they belong to.

Thirty-odd albums appear twice in the grid: the full record, and beside it a
tile with the same name and cover holding two or three tracks. These escaped
the original merge because their folder or artist name differed just enough not
to pair -- "Will Smith" against "Smith,Will/Will Smith", "Black Celebration"
against "Black Celebration (86)", "12 Greatest Hits Vol II" against "12
Greatest Hits, Vol. 2".

The important distinction, and the reason this is not a delete pass: most
fragments are wholly contained in the full album and are simply redundant
copies, but some are not. Enya's "The Celts" fragment overlaps the main album
by only half -- the rest is music that exists nowhere else. So every fragment
track is compared to the full album BY AUDIO, and then:

    already there  -> quarantined to _superseded, reversible like any sweep
    not there      -> MOVED INTO the full album's folder, so nothing is lost

Tags and titles are not trusted for that decision; a chromaprint bit-error-rate
against the actual audio is. Anything that cannot be fingerprinted is left
alone and reported.
"""
import os, re, sys, sqlite3, collections
from pathlib import Path

import numpy as np
import musiclib, merge

HERE = Path(__file__).resolve().parent
JUKE = HERE / 'jukebox' / 'jukebox.db'
OP_DUP = 'fragment-dupe'
OP_FOLD = 'fragment-fold'


def norm(s):
    s = (s or '').lower()
    s = re.sub(r'\b(the|a|an)\b', '', s)
    return re.sub(r'[^a-z0-9]', '', s)


def pairs():
    """[(full_album, fragment_album)] from the read model."""
    c = sqlite3.connect(f'file:{JUKE}?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    alb = {r['id']: dict(r) for r in c.execute(
        """SELECT album.id, album.title, album.n_tracks, album.folder, album.collection_id,
                  COALESCE(album.is_compilation,0) AS comp, artist.name art
           FROM album JOIN artist ON artist.id = album.artist_id""")}
    titles = collections.defaultdict(set)
    paths = collections.defaultdict(list)
    for r in c.execute('SELECT album_id, title, rel_path FROM track'):
        titles[r['album_id']].add(norm(r['title']))
        paths[r['album_id']].append(r['rel_path'])
    c.close()

    by = collections.defaultdict(list)
    for a in alb.values():
        if a['collection_id'] or a['comp']:
            continue
        by[norm(a['title'])].append(a)

    out = []
    for _t, v in by.items():
        if len(v) < 2:
            continue
        v.sort(key=lambda a: -(a['n_tracks'] or 0))
        full = v[0]
        for frag in v[1:]:
            kf, kg = titles[full['id']], titles[frag['id']]
            if not kg or frag['n_tracks'] >= full['n_tracks']:
                continue
            if len(kf & kg) / float(len(kg)) >= 0.5:
                out.append((full, frag, paths[full['id']], paths[frag['id']]))
    return out


def main(apply=False, dupes_only=False):
    conn = musiclib.db()
    conn.row_factory = sqlite3.Row
    fp = {}
    for r in conn.execute("SELECT path, fp FROM files WHERE kind='audio' AND fp IS NOT NULL"):
        fp[r['path']] = r['fp']

    todo = pairs()
    print('fragment pairs: %d' % len(todo))
    n_dup = n_fold = n_skip = 0
    for full, frag, fpaths, gpaths in todo:
        fulls = [(p, musiclib.unpack_fp(fp[p])) for p in fpaths if p in fp]
        acts = []
        for gp in gpaths:
            if gp not in fp:
                n_skip += 1
                acts.append((gp, 'skip', 'no fingerprint'))
                continue
            g = musiclib.unpack_fp(fp[gp])
            best, bd = None, 1.0
            for p, f in fulls:
                d = merge.fp_distance(g, f)
                if d < bd:
                    bd, best = d, p
            if best is not None and bd <= merge.FP_SAME:
                acts.append((gp, 'dupe', '%.3f' % bd))
                n_dup += 1
            else:
                acts.append((gp, 'fold', '%.3f' % bd))
                n_fold += 1
        print('  %-30s full=%2dtr  fragment=%2dtr  %s'
              % ((full['title'] or '')[:30], full['n_tracks'], frag['n_tracks'],
                 ' '.join('%s' % a[1] for a in acts)))
        if not apply:
            continue
        dest = full['folder']
        for gp, what, why in acts:
            if what == 'dupe':
                musiclib._move(conn, gp, musiclib.SUPERSEDED, OP_DUP,
                               'already in %s (BER %s)' % (dest, why), False)
            elif what == 'fold' and not dupes_only:
                target = dest + '/' + gp.split('/')[-1]
                musiclib._move_to(conn, gp, target, OP_FOLD,
                                  'unique track folded into %s' % dest)
    if apply:
        conn.commit()
    print('')
    print('%s  %d duplicates to quarantine, %d unique tracks to fold in, %d unverifiable'
          % ('APPLIED' if apply else 'DRY RUN', n_dup, n_fold, n_skip))
    conn.close()


if __name__ == '__main__':
    main(apply='--apply' in sys.argv, dupes_only='--dupes-only' in sys.argv)
