"""Resolve albums that are the same record filed twice under different titles.

The grid still shows pairs like "Fijacion Oral Volumen 1" beside "Fijacion Oral,
Volume 1", "Speak & Spell" beside "Speak & Spell [digital version] (Bonus
Tracks)", and "Nomads Indians Saints" beside the same album plus bonus tracks.
Earlier passes matched on the album title, so anything spelled differently was
invisible to them.

Loosening the title match alone is dangerous: forty-two different artists have an
album called "Greatest Hits", and normalising titles hard enough to catch
"Volumen 1" vs "Volume 1" also collapses those. So the title is used ONLY to
propose candidates, and the decision is made on the audio:

    * same artist (a pairing across artists is never proposed here),
    * tracks matched one-to-one by chromaprint, not by title,
    * at least 60% of the smaller album's tracks must find a partner.

An album that merely shares a name shares no fingerprints, so it never passes.

Resolution keeps the better copy of every matched song, judged on measured
spectral cutoff scored against its rival, and moves the unmatched tracks of the
smaller album into the larger one -- a bonus-tracks edition therefore ends up as
one album holding everything, at the best quality available. Losers go to
_superseded, so it is all reversible.
"""
import os, re, sys, sqlite3, collections
from pathlib import Path

import musiclib, merge, quality

HERE = Path(__file__).resolve().parent
JUKE = HERE.parent / 'jukebox' / 'jukebox.db'
OP = 'album-dupe'
MIN_MATCH = 0.6
MARGIN = 1.0

VOL = re.compile(r'(?i)\b(vol(?:umen|ume)?)\s*\.?\s*')
NOISE = re.compile(r'(?i)\b(bonus ?tracks?|digital version|deluxe|remaster(?:ed)?|'
                   r'expanded|special edition|edition|reissue|explicit|ep)\b')


def loose(title, artist):
    t = (title or '').lower()
    t = re.sub(r'^\s*(19|20)\d{2}\s*[-–—]\s*', '', t)
    if artist:
        t = re.sub(r'^\s*' + re.escape(artist.lower()) + r'\s*[-–—]\s*', '', t)
    t = re.sub(r'[\(\[][^\)\]]*[\)\]]', ' ', t)      # (Bonus Tracks), [digital version]
    t = NOISE.sub(' ', t)
    t = VOL.sub('vol', t)                            # volumen 1 / volume 1 / vol. 1
    t = t.split('/')[0]      # "Cantos de Amor / Love Songs" is one title
    t = re.sub(r'\b(the|a|an)\b', ' ', t)
    return re.sub(r'[^a-z0-9]', '', t)


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def candidates():
    c = sqlite3.connect(f'file:{JUKE}?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    alb = {r['id']: dict(r) for r in c.execute(
        """SELECT album.id, album.title, album.n_tracks, album.folder, album.collection_id,
                  COALESCE(album.is_compilation,0) comp, album.artist_id, artist.name art
           FROM album JOIN artist ON artist.id = album.artist_id""")}
    paths = collections.defaultdict(list)
    for r in c.execute('SELECT album_id, rel_path FROM track'):
        paths[r['album_id']].append(r['rel_path'])
    c.close()

    by = collections.defaultdict(list)
    for a in alb.values():
        if a['collection_id'] or a['comp'] or not a['folder']:
            continue
        k = loose(a['title'], a['art'])
        if k:
            by[(norm(a['art']), k)].append(a)
    out = []
    for _k, v in by.items():
        if len(v) < 2:
            continue
        v.sort(key=lambda a: -(a['n_tracks'] or 0))
        for b in v[1:]:
            out.append((v[0], b, paths[v[0]['id']], paths[b['id']]))
    return out


def main(apply=False):
    conn = musiclib.db()
    conn.row_factory = sqlite3.Row
    info = {r['path']: dict(r) for r in conn.execute(
        "SELECT path,fp,bitrate,ext,codec,brmode,cutoff FROM files WHERE kind='audio'")}

    pairs = candidates()
    print('candidate album pairs (same artist, same title once normalised): %d' % len(pairs))
    plans, rejected = [], 0
    for big, small, bpaths, spaths in pairs:
        bf = [(p, musiclib.unpack_fp(info[p]['fp']))
              for p in bpaths if p in info and info[p].get('fp')]
        matched, unmatched = [], []
        for sp in spaths:
            r = info.get(sp)
            if not r or not r.get('fp'):
                continue
            g = musiclib.unpack_fp(r['fp'])
            best, bd = None, 1.0
            for p, f in bf:
                d = merge.fp_distance(g, f)
                if d < bd:
                    bd, best = d, p
            if best is not None and bd <= merge.FP_MAYBE:
                matched.append((sp, best, bd))
            else:
                unmatched.append(sp)
        total = len(matched) + len(unmatched)
        if not total or len(matched) / float(total) < MIN_MATCH:
            rejected += 1
            continue
        plans.append((big, small, matched, unmatched))
        print('  %-30s %2dtr  <-  %-28s %2dtr   %d matched, %d unique'
              % ((big['title'] or '')[:30], big['n_tracks'], (small['title'] or '')[:28],
                 small['n_tracks'], len(matched), len(unmatched)))
    print('  (%d candidate pairs rejected: too few tracks matched by audio)' % rejected)

    if not apply:
        print('')
        print('DRY RUN  %d album pairs would merge' % len(plans))
        conn.close()
        return

    dropped = folded = swapped = 0
    for big, small, matched, unmatched in plans:
        for sp, bp, bd in matched:
            a, b = info[sp], info[bp]
            sa, _ = quality.score_pair(a, a.get('cutoff'), b.get('cutoff'))
            sb, _ = quality.score_pair(b, b.get('cutoff'), a.get('cutoff'))
            if sa > sb + MARGIN:                       # the copy in the smaller album wins
                musiclib._move(conn, bp, musiclib.SUPERSEDED, OP,
                               'replaced by a better copy (%.0f vs %.0f)' % (sb, sa), False)
                musiclib._move_to(conn, sp, big['folder'] + '/' + sp.split('/')[-1], OP,
                                  'better copy moved into %s' % big['folder'])
                swapped += 1
            else:
                musiclib._move(conn, sp, musiclib.SUPERSEDED, OP,
                               'duplicate of %s (%.0f vs %.0f)' % (big['folder'], sa, sb), False)
                dropped += 1
        for sp in unmatched:
            musiclib._move_to(conn, sp, big['folder'] + '/' + sp.split('/')[-1], OP,
                              'only copy, folded into %s' % big['folder'])
            folded += 1
    conn.commit()
    conn.close()
    print('')
    print('APPLIED  %d duplicates superseded, %d better copies swapped in, %d unique tracks folded in'
          % (dropped, swapped, folded))


if __name__ == '__main__':
    main(apply='--apply' in sys.argv)
