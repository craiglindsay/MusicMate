"""Quarantine duplicate copies of a track sitting inside one album folder.

These are folders that hold the same album two, three or five times over --
Moby's "Go" is 54 files that are 27 tracks ripped twice, one set at ~266kbps
named "01 - Natural Blues.mp3" and one at ~213kbps named "01 Natural Blues.mp3".
The disc splitter found them by accident: by track numbering alone they look
exactly like a multi-disc set, and it took a duplicate check (same titles, same
durations across "discs") to tell them apart.

Every pair is confirmed by acoustic fingerprint before anything moves -- tags
and durations agree on plenty of things that are not the same recording, and a
bit-error-rate against the audio itself is the only honest test. Anything that
cannot be confirmed is left alone and reported.

The keeper is the highest-bitrate copy. Losers move to _superseded/ via
musiclib._move, so "python musiclib.py undo --op dedupe-copies" puts every one
of them back.
"""
import os, re, sys, sqlite3, collections
import numpy as np
import musiclib, merge

HERE = os.path.dirname(os.path.abspath(__file__))
JUKE = os.path.join(HERE, '..', 'jukebox', 'jukebox.db')
OP = 'dedupe-copies'


def norm_title(s):
    s = (s or '').lower()
    s = re.sub(r'^\s*\d{1,3}\s*[-._)]?\s*', '', s)      # drop a track prefix
    return re.sub(r'[^a-z0-9]+', '', s)


def key_of(r):
    """Group on what the audio claims to be, not on the filename."""
    t = norm_title(r['title']) or norm_title(os.path.splitext(r['filename'])[0])
    return (t, int(round(r['duration'] or 0)))


def target_folders():
    """The album folders discsplit flagged as duplicate copies rather than discs."""
    sys.path.insert(0, os.path.join(HERE, 'jukebox'))
    sys.path.insert(0, os.path.join(HERE, "..", "jukebox")); import discsplit
    _n, _t, dups = discsplit.build(db=JUKE)
    j = sqlite3.connect(JUKE)
    j.row_factory = sqlite3.Row
    out = []
    for aid, _ntr, _k in dups:
        r = j.execute('SELECT album.folder, album.title, artist.name art FROM album '
                      'JOIN artist ON artist.id=album.artist_id WHERE album.id=?',
                      (aid,)).fetchone()
        if r and r['folder']:
            out.append((r['folder'], r['art'], r['title']))
    j.close()
    return out


def plan(conn, folder):
    """[(keeper, [losers])] for one folder, fingerprint-confirmed."""
    rows = conn.execute("SELECT path,filename,ext,bitrate,size,duration,title,fp "
                        "FROM files WHERE folder=? AND kind='audio'", (folder,)).fetchall()
    groups = collections.defaultdict(list)
    for r in rows:
        groups[key_of(r)].append(r)

    keeps, unsure = [], []
    for k, g in groups.items():
        if len(g) < 2:
            continue
        g = sorted(g, key=lambda r: (-(r['bitrate'] or 0), -(r['size'] or 0), r['path']))
        keeper, rest = g[0], g[1:]
        kfp = musiclib.unpack_fp(keeper['fp']) if keeper['fp'] else None
        losers = []
        for r in rest:
            rfp = musiclib.unpack_fp(r['fp']) if r['fp'] else None
            if kfp is None or rfp is None:
                unsure.append((keeper['path'], r['path'], 'no fingerprint'))
                continue
            d = merge.fp_distance(kfp, rfp)
            if d <= merge.FP_SAME:
                losers.append((r, d))
            else:
                unsure.append((keeper['path'], r['path'], 'BER %.3f' % d))
        if losers:
            keeps.append((keeper, losers))
    return keeps, unsure


def main(apply=False):
    conn = musiclib.db()
    conn.row_factory = sqlite3.Row
    folders = target_folders()
    print('%d album folders flagged as duplicate copies' % len(folders))
    tot_move = tot_bytes = 0
    all_unsure = []
    for folder, art, title in folders:
        keeps, unsure = plan(conn, folder)
        all_unsure += unsure
        n = sum(len(l) for _, l in keeps)
        if not n and not unsure:
            continue
        b = sum(r['size'] or 0 for _, l in keeps for r, _ in l)
        tot_move += n
        tot_bytes += b
        print('  %-20s %-34s keep %2d, drop %3d  (%5.1f MB)%s' % (
            (art or '')[:20], (title or '')[:34], len(keeps), n, b / 1e6,
            '  [%d unsure]' % len(unsure) if unsure else ''))
        if apply:
            for _keeper, losers in keeps:
                for r, d in losers:
                    musiclib._move(conn, r['path'], musiclib.SUPERSEDED, OP,
                                   'duplicate copy in same folder (BER %.3f)' % d, False)
    if apply:
        conn.commit()
    print('')
    print('%s %d files, %.2f GB' % ('MOVED' if apply else 'WOULD MOVE', tot_move, tot_bytes / 1e9))
    if all_unsure:
        print('%d pairs NOT confirmed, left in place:' % len(all_unsure))
        for a, b, why in all_unsure[:15]:
            print('   %-58s %s' % (b.split('/')[-1][:58], why))
    conn.close()


if __name__ == '__main__':
    main(apply='--apply' in sys.argv)
