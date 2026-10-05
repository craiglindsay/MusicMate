"""Fold the remnant of "Duran Duran - The Album Collection 1981-2007" into the
Duran Duran folder, and retire the "For Luna" folder.

Disc 19 (Beautiful Colours, the Astronaut demos) becomes its own album. Disc
18's new mix joins Mastermixes. Disc 21 (Arcadia 12" mixes) folds into the
Arcadia folder. Every track that fingerprints as a copy of one already in the
library is settled by measured cutoff, the library's rule: the loser retires
to _superseded, and when the incoming copy wins it takes the incumbent's
slot and tags. Authorised by the user on 1 Sep 2026. Dry run by default.
"""
import os, re, sys, time, json, sqlite3, argparse, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import musiclib, merge, quality, mutagen
from resolve_pairs import place_name, easy_tags, norm_title

ROOT = musiclib.ROOT
OP = 'dd-collection'
COLL = 'VariousArtists/Duran Duran - The Album Collection 1981-2007 (MP3-EAC-320kBs)'
DD = 'VariousArtists/Duran Duran'
LUNA = 'VariousArtists/For Luna'
DEST = {'18.': DD + '/Mastermixes', '19.': DD + '/Beautiful Colours', '21.': DD + '/Arcadia'}


def clean_name(fn):
    """'06 - Duran Duran - Virus (Album version).mp3' -> '06 - Virus (Album version).mp3'"""
    return re.sub(r'^(\d\d) - (?:Duran Duran|Arcadia) - ', r'\1 - ', fn)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--apply', action='store_true'); a = ap.parse_args()
    conn = musiclib.db(); conn.row_factory = sqlite3.Row
    coll = [dict(r) for r in conn.execute("SELECT * FROM files WHERE folder LIKE ? AND kind='audio' ORDER BY folder, filename", (COLL + '/%',))]
    dd = [dict(r) for r in conn.execute("SELECT * FROM files WHERE folder LIKE ? AND kind='audio'", (DD + '/%',))]
    cache = {}
    def fp(r):
        if r['path'] not in cache:
            cache[r['path']] = musiclib.unpack_fp(r['fp']) if r['fp'] else None
        return cache[r['path']]
    pairs, fresh = [], []
    for c in coll:
        best = None
        for d in dd:
            if abs((d['duration'] or 0) - (c['duration'] or 0)) > 12:
                continue
            fc, fd = fp(c), fp(d)
            if fc is None or fd is None:
                continue
            x = merge.fp_distance(fc, fd)
            if best is None or x < best[0]:
                best = (x, d)
        if best and best[0] <= merge.FP_SAME:
            pairs.append((c, best[1], best[0]))
        else:
            fresh.append(c)
    print(len(coll), 'tracks:', len(pairs), 'are copies of tracks you have,', len(fresh), 'are new')

    # measure what the decision needs
    need = [r for c, d, _ in pairs for r in (c, d) if r['cutoff'] is None]
    print('measuring cutoff for', len(need), 'files', flush=True)
    for r in need:
        try:
            r['cutoff'] = quality.measure_cutoff(os.path.join(ROOT, r['path'].replace('/', os.sep)))
        except Exception:
            r['cutoff'] = None
        if a.apply:
            conn.execute('UPDATE files SET cutoff=?, cutoff_at=? WHERE path=?', (r['cutoff'], time.time(), r['path']))
    cutoffs = {r['path']: r['cutoff'] for c, d, _ in pairs for r in (c, d)}
    dec = []
    for c, d, x in pairs:
        w, l, why = merge.pick_winner(c, d, cutoffs)
        dec.append((c, d, w, l, why))
        print('  %s | %s (%dk) vs %s (%dk %s) -> keep %s [%s]' % (
            c['folder'].split('/')[-1][:3], clean_name(c['filename'])[:40], (c['bitrate'] or 0) / 1000,
            d['filename'][:40], (d['bitrate'] or 0) / 1000, d['ext'], 'incoming' if w is c else 'existing', why))
    for c in fresh:
        print('  %s | %s -> %s' % (c['folder'].split('/')[-1][:3], clean_name(c['filename']), DEST[c['folder'].split('/')[-1][:3]]))
    luna = [r[0] for r in conn.execute("SELECT path FROM files WHERE path LIKE ?", (LUNA + '/%',))]
    print(len(luna), 'files under For Luna to retire')
    if not a.apply:
        print('dry run'); return

    snap = os.path.join(HERE, 'library.db.pre-dd-collection')
    if not os.path.exists(snap):
        conn.commit(); import shutil; shutil.copy(os.path.join(HERE, 'library.db'), snap)
    for c, d, w, l, why in dec:
        if w is c:
            place = l['path']; tags = easy_tags(os.path.join(ROOT, place.replace('/', os.sep)))
            musiclib._move(conn, l['path'], musiclib.SUPERSEDED, OP, 'lost to the collection copy', False)
            title = l['title'] or c['title']
            dst = os.path.dirname(place) + '/' + place_name(l, title, c['ext'])
            got = musiclib._move_to(conn, c['path'], dst, OP, 'collection copy takes the slot')
            f = mutagen.File(os.path.join(ROOT, got.replace('/', os.sep)), easy=True)
            if f is not None and f.tags is not None:
                ch = False
                for k in ('album', 'tracknumber', 'discnumber', 'albumartist'):
                    v = tags.get(k)
                    if v and f.get(k) != v:
                        f[k] = v; ch = True
                if ch:
                    f.save()
                    conn.execute('UPDATE files SET album=?, albumartist=? WHERE path=?',
                                 ((tags.get('album') or [None])[0], (tags.get('albumartist') or [None])[0], got))
        else:
            musiclib._move(conn, c['path'], musiclib.SUPERSEDED, OP, 'duplicate of a better copy', False)
    for c in fresh:
        dest = DEST[c['folder'].split('/')[-1][:3]]
        musiclib._move_to(conn, c['path'], dest + '/' + clean_name(c['filename']), OP, 'new to the library')
    for rel in luna:
        musiclib._move(conn, rel, musiclib.SUPERSEDED, 'retire', 'For Luna: no longer needed', False)
    conn.commit()
    for top in (COLL, LUNA):
        p = os.path.join(ROOT, top.replace('/', os.sep))
        for dp, dn, fn in os.walk(p, topdown=False):
            if not os.listdir(dp):
                os.rmdir(dp)
        print('removed' if not os.path.exists(p) else 'NOT EMPTY:', top)
    print('done')


if __name__ == '__main__':
    main()
