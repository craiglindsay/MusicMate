"""Settle the "UnknownAlbum" folders under VariousArtists.

These are tracks that arrived with no album tag and were parked in
<Artist>/UnknownAlbum by the pool fold. Most are copies of songs the artist's
album folders already hold, so they get the Beatles treatment (resolve_pairs):
chromaprint pairs each stray with an album copy, merge.pick_winner decides on
measured cutoff, the loser retires to _superseded (op "resolve-unknown", so
`python musiclib.py undo --op resolve-unknown` restores it), and a winning
stray takes the album copy's slot, file name and album/track/disc/title tags so
the album stays complete and in order.

A stray that matches nothing the artist owns is a LEFTOVER. It is left where it
is and reported; with --ident it is also identified through AcoustID so the
report can say which album it belongs to. What to do with leftovers is the
user's call, not this script's.

    python resolve_unknown.py                    # plan, every artist
    python resolve_unknown.py --ident            # plan + identify leftovers
    python resolve_unknown.py --artist "Bon Jovi" --apply
    python resolve_unknown.py --apply
"""
import os, re, sys, time, sqlite3, argparse, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import musiclib, merge, quality, mutagen, unicodedata
from resolve_pairs import place_name, easy_tags

ROOT = musiclib.ROOT
OP = 'resolve-unknown'
POOL = 'VariousArtists'
COLS = {'albumartist': 'albumartist', 'title': 'title', 'tracknumber': 'track',
        'album': 'album', 'discnumber': 'disc'}


def norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'\(.*?\)|\[.*?\]', '', s)
    s = re.sub(r'\b(the|and|a|an)\b', '', s)
    return re.sub(r'[^a-z0-9]', '', s)


# A remix, live take, edit or instrumental is never a missing album track.
# AcoustID fingerprints the first two minutes, so a club mix that opens like
# the single identifies as the single; the names carry the warning instead.
VARIANT = re.compile(r'\b(re)?mix|\blive\b|\bedit\b|\bversion\b|instrumental|megamix|\bdub\b|acoustic|\bdemo\b|karaoke', re.I)


def is_variant(*names):
    return any(VARIANT.search(n or '') for n in names)


def set_field(conn, rel, field, value):
    if value is None or value == '':
        return
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
    col = COLS.get(field)
    if col in ('track', 'disc'):
        m = re.match(r'\d+', str(value))
        conn.execute('UPDATE files SET %s=? WHERE path=?' % col, (int(m.group()) if m else None, rel))
    elif col:
        conn.execute('UPDATE files SET %s=? WHERE path=?' % col, (value, rel))


def plan(conn, artists=None):
    """-> {artist: dict(unknown=[rows], pairs=[(u, m, dist)], left=[(u, nearest_dist)])}"""
    folders = [f for (f,) in conn.execute(
        "SELECT DISTINCT folder FROM files WHERE src=? AND kind='audio' AND folder LIKE ?",
        (POOL, POOL + '/%/UnknownAlbum%'))]
    by_artist = collections.defaultdict(list)
    for f in folders:
        by_artist[f.split('/')[1]].append(f)
    cache = {}

    def fp(r):
        if r['path'] not in cache:
            cache[r['path']] = musiclib.unpack_fp(r['fp']) if r['fp'] else None
        return cache[r['path']]

    out = {}
    for artist in sorted(by_artist):
        if artists and artist not in artists:
            continue
        urows = [dict(r) for f in by_artist[artist] for r in conn.execute(
            "SELECT * FROM files WHERE folder=? AND kind='audio' ORDER BY filename", (f,))]
        mrows = [dict(r) for r in conn.execute(
            "SELECT * FROM files WHERE folder LIKE ? AND folder NOT LIKE ? AND kind='audio'",
            (POOL + '/' + artist + '/%', '%/UnknownAlbum%'))]
        used, pairs, left = set(), [], []
        for u in urows:
            best = None
            for m in mrows:
                if m['path'] in used or abs((m['duration'] or 0) - (u['duration'] or 0)) > 12:
                    continue
                fu, fm = fp(u), fp(m)
                if fu is None or fm is None:
                    continue
                d = merge.fp_distance(fu, fm)
                if best is None or d < best[0]:
                    best = (d, m)
            if best and best[0] <= merge.FP_SAME:
                used.add(best[1]['path']); pairs.append((u, best[1], best[0]))
            else:
                left.append((u, best[0] if best else None))
        out[artist] = dict(unknown=urows, pairs=pairs, left=left, n_albums=len(mrows))
    return out


def decide(conn, P, apply):
    need = [r for a in P.values() for u, m, _ in a['pairs'] for r in (u, m) if r['cutoff'] is None]
    if need:
        print('measuring cutoff for', len(need), 'files')
    for r in need:
        try:
            r['cutoff'] = quality.measure_cutoff(os.path.join(ROOT, r['path'].replace('/', os.sep)))
        except Exception:
            r['cutoff'] = None
        if apply:
            conn.execute('UPDATE files SET cutoff=?, cutoff_at=? WHERE path=?', (r['cutoff'], time.time(), r['path']))
    for a in P.values():
        cutoffs = {r['path']: r['cutoff'] for u, m, _ in a['pairs'] for r in (u, m)}
        a['dec'] = []
        for u, m, d in a['pairs']:
            w, l, why = merge.pick_winner(u, m, cutoffs)
            a['dec'].append((u, m, w, l, why))


def find_joins(conn, P, ident):
    """A leftover whose identified album is one of the artist's own folders joins it."""
    for artist, a in P.items():
        a['joins'] = []
        if not ident:
            continue
        folders = {}
        for (f, alb) in conn.execute(
                "SELECT folder, min(album) FROM files WHERE folder LIKE ? AND folder NOT LIKE ? AND kind='audio' GROUP BY folder",
                (POOL + '/' + artist + '/%', '%/UnknownAlbum%')):
            folders[norm(f.split('/')[-1])] = (f, alb)
            if alb:
                folders.setdefault(norm(alb), (f, alb))
        keep = []
        for u, d in a['left']:
            i = ident.get(u['path'])
            ok = i and not i.get('err') and i.get('album') and not is_variant(u['filename'], u['title'], i.get('title'))
            hit = folders.get(norm(i['album'])) if ok else None
            if hit:
                a['joins'].append((u, hit[0], hit[1], i))
            else:
                keep.append((u, d))
        a['left'] = keep


def report(P, ident=None):
    tp = tl = tj = 0
    for artist, a in P.items():
        print('\n== %s: %d unknown, %d album tracks to match against; %d pairs, %d leftovers'
              % (artist, len(a['unknown']), a['n_albums'], len(a['pairs']), len(a['left'])))
        for u, m, w, l, why in a['dec']:
            print('  pair %-34s %3dk vs %-44s %3dk -> keep %s [%s]' % (
                u['filename'][:34], (u['bitrate'] or 0) / 1000,
                m['path'][len(POOL) + len(artist) + 2:][:44], (m['bitrate'] or 0) / 1000,
                'stray' if w is u else 'album', why))
        for u, f, alb, i in a.get('joins', []):
            print('  join %-44s -> %s   (AcoustID: %s / %s %.2f)' % (
                u['filename'][:44], f[len(POOL) + len(artist) + 2:], i['album'], i['title'], i['score'] or 0))
        for u, d in a['left']:
            extra = ''
            if ident is not None:
                i = ident.get(u['path'])
                if i:
                    extra = '  => %s / %s / %s (%.2f)' % (i['artist'], i['album'], i['title'], i['score'] or 0) \
                        if not i.get('err') else '  => ' + i['err']
            print('  left %-44s %3dk %5.0fs nearest=%s%s' % (
                u['filename'][:44], (u['bitrate'] or 0) / 1000, u['duration'] or 0,
                ('%.2f' % d) if d is not None else '-', extra))
        tp += len(a['pairs']); tl += len(a['left'])
    print('\n%d pairs to settle, %d leftovers' % (tp, tl))


def identify(P):
    import acoustid
    paths = [u['path'] for a in P.values() for u, _ in a['left']]
    if not paths:
        return {}
    acoustid.run(paths, verbose=True)
    c = acoustid.cache_db()
    c.row_factory = sqlite3.Row
    q = ','.join('?' * len(paths))
    return {r['path']: dict(r) for r in c.execute(f'SELECT * FROM ident WHERE path IN ({q})', paths)}


def apply(conn, P):
    snap = os.path.join(HERE, 'library.db.pre-resolve-unknown')
    if not os.path.exists(snap):
        conn.commit(); import shutil; shutil.copy(os.path.join(HERE, 'library.db'), snap)
    n = 0
    for artist, a in P.items():
        for u, m, w, l, why in a['dec']:
            if w is u:
                tags = easy_tags(os.path.join(ROOT, m['path'].replace('/', os.sep)))
                musiclib._move(conn, m['path'], musiclib.SUPERSEDED, OP, 'lost to the UnknownAlbum copy', False)
                dst = m['folder'] + '/' + place_name(m, m['title'] or u['title'], u['ext'])
                got = musiclib._move_to(conn, u['path'], dst, OP, 'UnknownAlbum copy takes the slot')
                set_field(conn, got, 'title', m['title'])
                set_field(conn, got, 'album', (tags.get('album') or [m['album']])[0])
                set_field(conn, got, 'tracknumber', (tags.get('tracknumber') or [None])[0])
                set_field(conn, got, 'discnumber', (tags.get('discnumber') or [None])[0])
                set_field(conn, got, 'albumartist', (tags.get('albumartist') or [m['albumartist'] or artist])[0])
            else:
                musiclib._move(conn, u['path'], musiclib.SUPERSEDED, OP, 'duplicate of a better copy', False)
            n += 1
        for u, f, alb, i in a.get('joins', []):
            got = musiclib._move_to(conn, u['path'], f + '/' + u['filename'], OP, 'joins the album (AcoustID)')
            set_field(conn, got, 'album', alb or i['album'])
            if norm(u['title']) == norm(alb or i['album']) and i.get('title'):
                set_field(conn, got, 'title', i['title'])
            n += 1
        conn.commit()
        for f in {u['folder'] for u in a['unknown']}:
            p = os.path.join(ROOT, f.replace('/', os.sep))
            if os.path.isdir(p) and not os.listdir(p):
                os.rmdir(p); print('removed empty', f)
    print('settled', n, 'pairs')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--artist', action='append')
    ap.add_argument('--ident', action='store_true', help='identify leftovers via AcoustID')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--assorted', action='store_true',
                    help='afterwards, move whatever is still in UnknownAlbum to <Artist>/Assorted')
    a = ap.parse_args()
    conn = musiclib.db(); conn.row_factory = sqlite3.Row
    P = plan(conn, a.artist)
    decide(conn, P, a.apply)
    ident = identify(P) if a.ident else None
    find_joins(conn, P, ident)
    report(P, ident)
    if not a.apply:
        if a.assorted:
            to_assorted(conn, a.artist, dry=True)
        print('dry run'); return
    apply(conn, P)
    if a.assorted:
        to_assorted(conn, a.artist, dry=False)


def to_assorted(conn, artists=None, dry=True):
    """The leftovers become <Artist>/Assorted, the pool fold's name for an
    artist's stray tracks (user's decision, 1 Sep 2026). Tags are left alone:
    the existing Assorted folders carry no album tag either."""
    folders = [f for (f,) in conn.execute(
        "SELECT DISTINCT folder FROM files WHERE src=? AND kind='audio' AND folder LIKE ?",
        (POOL, POOL + '/%/UnknownAlbum%'))]
    n = 0
    for f in sorted(folders):
        artist = f.split('/')[1]
        if artists and artist not in artists:
            continue
        dst = POOL + '/' + artist + '/Assorted'
        for (g,) in conn.execute("SELECT DISTINCT folder FROM files WHERE folder LIKE ? AND kind='audio'", (POOL + '/' + artist + '/%',)):
            if g.split('/')[-1].lower().endswith('assorted'):     # e.g. an existing "Devo-Assorted"
                dst = g
        rows = conn.execute("SELECT path FROM files WHERE path LIKE ?", (f + '/%',)).fetchall()
        print('  %s%s (%d) -> %s' % ('' if dry else 'move ', f[len(POOL) + 1:], len(rows), dst[len(POOL) + 1:]))
        for (p,) in rows:
            musiclib._move_to(conn, p, dst + p[len(f):], OP, 'leftover to Assorted', dry=dry)
            n += 1
        if not dry:
            conn.commit()
            full = os.path.join(ROOT, f.replace('/', os.sep))
            if os.path.isdir(full) and not os.listdir(full):
                os.rmdir(full)
    print('%d files %sto Assorted' % (n, '' if not dry else 'would go '))


if __name__ == '__main__':
    main()
