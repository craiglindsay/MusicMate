"""Fold a "credit" artist folder into the main artist's folder.

"Joe Strummer & The Mescaleros" and "Johnny Clegg & Savuka" are the same
people as Joe Strummer and Johnny Clegg for browsing purposes, and the user
wants one folder and one artist entry each. An album the main folder lacks
simply moves across. An album both folders hold is settled track by track:
chromaprint pairs the copies, measured cutoff picks the winner (merge.pick_winner,
the library's rule), the loser retires to _superseded, and a winning incoming
copy takes the incumbent's slot, file name, title and track number, since the
main folder's running order is the trusted one. Every file under the moved
albums gets albumartist = the main name so the app shows one artist too.
All moves are logged (op "fold-credit") and undoable. Dry run by default.

    python foldcredit.py "Joe Strummer" "Joe Strummer & The Mescaleros"
    python foldcredit.py "Johnny Clegg" "Johnny Clegg & Savuka" --apply
"""
import os, re, sys, time, sqlite3, argparse, unicodedata
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import musiclib, merge, quality, mutagen
from resolve_pairs import place_name, easy_tags

ROOT = musiclib.ROOT
OP = 'fold-credit'
POOL = 'VariousArtists'


def norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'\(.*?\)|\[.*?\]', '', s)
    return re.sub(r'\b(the|and|a|an)\b', '', s).replace('&', '').replace(' ', '')


def set_field(conn, rel, field, value):
    full = os.path.join(ROOT, rel.replace('/', os.sep))
    f = mutagen.File(full, easy=True)
    if f is None or f.tags is None:
        return
    old = (f.get(field) or [None])[0]
    if old == value:
        return
    f[field] = [value]; f.save()
    conn.execute('INSERT INTO tag_backup (ts,op,path,field,old,new) VALUES (?,?,?,?,?,?)',
                 (time.time(), OP, rel, field, old, value))
    col = {'albumartist': 'albumartist', 'title': 'title', 'tracknumber': 'track'}.get(field)
    if col:
        conn.execute('UPDATE files SET %s=? WHERE path=?' % col, (value if col != 'track' else int(re.match(r'\d+', value).group()), rel))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('main'); ap.add_argument('credit'); ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    conn = musiclib.db(); conn.row_factory = sqlite3.Row
    MAIN, CREDIT = POOL + '/' + a.main, POOL + '/' + a.credit
    main_albums = {}
    for (f,) in conn.execute("SELECT DISTINCT folder FROM files WHERE folder LIKE ? AND kind='audio'", (MAIN + '/%',)):
        main_albums[norm(f[len(MAIN) + 1:])] = f
    credit_albums = sorted({f for (f,) in conn.execute("SELECT DISTINCT folder FROM files WHERE folder LIKE ? AND kind='audio'", (CREDIT + '/%',))})
    if not credit_albums:
        print('nothing under', CREDIT); return
    cache = {}
    def fp(r):
        if r['path'] not in cache:
            cache[r['path']] = musiclib.unpack_fp(r['fp']) if r['fp'] else None
        return cache[r['path']]

    plan_moves, plan_pairs, plan_extra = [], [], []      # whole albums, resolved pairs, unmatched tracks into an existing album
    for calb in credit_albums:
        name = calb[len(CREDIT) + 1:]
        target = main_albums.get(norm(name))
        crows = [dict(r) for r in conn.execute("SELECT * FROM files WHERE folder=? AND kind='audio'", (calb,))]
        if not target:
            plan_moves.append((calb, MAIN + '/' + name, len(crows)))
            continue
        mrows = [dict(r) for r in conn.execute("SELECT * FROM files WHERE folder=? AND kind='audio'", (target,))]
        used = set()
        for c in crows:
            best = None
            for m in mrows:
                if m['path'] in used or abs((m['duration'] or 0) - (c['duration'] or 0)) > 12:
                    continue
                fc, fm = fp(c), fp(m)
                if fc is None or fm is None:
                    continue
                d = merge.fp_distance(fc, fm)
                if best is None or d < best[0]:
                    best = (d, m)
            if best and best[0] <= merge.FP_SAME:
                used.add(best[1]['path']); plan_pairs.append((c, best[1], best[0], target))
            else:
                plan_extra.append((c, target))
    # cutoffs for the pairs
    need = [r for c, m, _, _ in plan_pairs for r in (c, m) if r['cutoff'] is None]
    print(len(plan_moves), 'albums move whole,', len(plan_pairs), 'track pairs to settle,', len(plan_extra), 'tracks join an existing album; measuring', len(need))
    for r in need:
        try:
            r['cutoff'] = quality.measure_cutoff(os.path.join(ROOT, r['path'].replace('/', os.sep)))
        except Exception:
            r['cutoff'] = None
        if a.apply:
            conn.execute('UPDATE files SET cutoff=?, cutoff_at=? WHERE path=?', (r['cutoff'], time.time(), r['path']))
    cutoffs = {r['path']: r['cutoff'] for c, m, _, _ in plan_pairs for r in (c, m)}
    dec = []
    for c, m, d, target in plan_pairs:
        w, l, why = merge.pick_winner(c, m, cutoffs)
        dec.append((c, m, w, l, why, target))
        print('  pair %-38s %dk vs %-38s %dk -> keep %s [%s]' % (c['filename'][:38], (c['bitrate'] or 0) / 1000, m['filename'][:38], (m['bitrate'] or 0) / 1000, 'incoming' if w is c else 'main', why))
    for src, dst, n in plan_moves:
        print('  move %s (%d) -> %s' % (src[len(POOL) + 1:], n, dst[len(POOL) + 1:]))
    for c, target in plan_extra:
        print('  add  %s -> %s' % (c['filename'], target[len(POOL) + 1:]))
    if not a.apply:
        print('dry run'); return

    moves_for_art = []
    for c, m, w, l, why, target in dec:
        if w is c:
            tags = easy_tags(os.path.join(ROOT, m['path'].replace('/', os.sep)))
            musiclib._move(conn, m['path'], musiclib.SUPERSEDED, OP, 'lost to the %s copy' % a.credit, False)
            dst = target + '/' + place_name(m, m['title'] or c['title'], c['ext'])
            got = musiclib._move_to(conn, c['path'], dst, OP, 'incoming copy takes the slot')
            if m['title'] and m['title'] != c['title']:
                set_field(conn, got, 'title', m['title'])
            tn = (tags.get('tracknumber') or [None])[0]
            if tn:
                set_field(conn, got, 'tracknumber', tn)
            set_field(conn, got, 'albumartist', a.main)
        else:
            musiclib._move(conn, c['path'], musiclib.SUPERSEDED, OP, 'duplicate of a better copy', False)
    for c, target in plan_extra:
        got = musiclib._move_to(conn, c['path'], target + '/' + c['filename'], OP, 'joins the album')
        set_field(conn, got, 'albumartist', a.main)
    for src, dst, n in plan_moves:
        for (p,) in conn.execute("SELECT path FROM files WHERE path LIKE ?", (src + '/%',)).fetchall():
            got = musiclib._move_to(conn, p, dst + p[len(src):], OP, 'album moves whole')
            moves_for_art.append((p, got, 'album'))
            if conn.execute("SELECT kind FROM files WHERE path=?", (got,)).fetchone()[0] == 'audio':
                set_field(conn, got, 'albumartist', a.main)
    conn.commit()
    musiclib.art_follow(moves_for_art, apply=True)
    p = os.path.join(ROOT, CREDIT.replace('/', os.sep))
    for dp, dn, fn in os.walk(p, topdown=False):
        if not os.listdir(dp):
            os.rmdir(dp)
    print('removed' if not os.path.exists(p) else 'NOT EMPTY:', CREDIT)


if __name__ == '__main__':
    main()
