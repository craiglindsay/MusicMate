"""Settle confirmed duplicate pairs by measured quality: the Beatles files that
had no album tag against the studio-album copies they duplicate, and the
album folders that ended up holding two copies after the pool fold.

For each pair (same recording, confirmed by chromaprint), merge.pick_winner
decides on measured spectral cutoff scored against the rival, exactly as the
original merge did. The loser goes to _superseded via musiclib._move (op
"resolve-pairs", so `python musiclib.py undo --op resolve-pairs` restores
it). When the winner is the untagged copy, it takes the loser's place in the
album folder and inherits the loser's album, track and disc tags, so the
album stays complete and correctly ordered.

    python resolve_pairs.py             # plan
    python resolve_pairs.py --apply
"""
import os, re, sys, json, time, sqlite3, argparse, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import musiclib, merge, mutagen

ROOT = musiclib.ROOT
OP = 'resolve-pairs'
UNKNOWN = 'VariousArtists/Beatles/UnknownAlbum'
DOUBLED = ['VariousArtists/Orchestral Manoeuvres in the Dark/Liberator',
           'VariousArtists/Orchestral Manoeuvres in the Dark/The OMD Singles']
FP_JSON = os.path.join(os.environ.get('PAIRS_JSON_DIR', HERE), 'beatles_fp.json')
IDENT_JSON = os.path.join(os.environ.get('PAIRS_JSON_DIR', HERE), 'beatles_ident.json')

# The existing "White Album (Disc 2)" rip carries Disc 1's titles on Disc 2's
# songs (its "Piggies" runs 8:22, which is Revolution 9). Track numbers and
# durations are right, so the official running order puts the names back.
WHITE2 = 'VariousArtists/Beatles/The White Album/The White Album (Disc 2)'
WHITE2_TITLES = {1: ('Birthday', 162), 2: ('Yer Blues', 241), 3: ("Mother Nature's Son", 168),
                 4: ("Everybody's Got Something to Hide Except Me and My Monkey", 145),
                 5: ('Sexy Sadie', 195), 6: ('Helter Skelter', 270), 7: ('Long, Long, Long', 184),
                 8: ('Revolution 1', 255), 9: ('Honey Pie', 161), 10: ('Savoy Truffle', 175),
                 11: ('Cry Baby Cry', 182), 12: ('Revolution 9', 502), 13: ('Good Night', 191)}


def norm_title(s):
    import unicodedata
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'\(.*?\)', '', s)
    return re.sub(r'[^a-z0-9]', '', s)


def acoustid_titles():
    """filename -> the title AcoustID's Beatles recordings agree on, if any"""
    out = {}
    if not os.path.exists(IDENT_JSON):
        return out
    for path, v in json.load(open(IDENT_JSON, encoding='utf-8')).items():
        recs = [r for r in v.get('recordings', []) if any('beatles' in (a or '').lower() for a in r['artists'])]
        c = collections.Counter(r['title'] for r in recs)
        if c:
            out[os.path.basename(path)] = c.most_common(1)[0][0]
    return out


def true_title(unk, ex, acoustid):
    """Which title is right when the two copies disagree. The album copy's
    title stands unless AcoustID names the song and it is not that; failing
    AcoustID, a Disc 2 file is corrected from the official running order, and
    otherwise the copy whose title is a complete phrase (not truncated) wins."""
    tu, te = unk['title'] or os.path.splitext(unk['filename'])[0], ex['title'] or ''
    if norm_title(tu) == norm_title(te):
        return None
    at = acoustid.get(unk['filename'])
    if at and norm_title(at) != norm_title(te):
        return at
    if at:
        return None
    if ex['folder'] == WHITE2 and ex['track'] in WHITE2_TITLES:
        return WHITE2_TITLES[ex['track']][0]
    if len(tu) > len(te) and not re.search(r'\b\w{1,2}$', tu):
        return tu
    return None


def rows_in(conn, folder):
    return [dict(r) for r in conn.execute("SELECT * FROM files WHERE folder=? AND kind='audio'", (folder,))]


def fpof(cache, r):
    if r['path'] not in cache:
        cache[r['path']] = musiclib.unpack_fp(r['fp']) if r['fp'] else None
    return cache[r['path']]


def find_pairs(conn):
    pairs = []          # (untagged_or_extra, album_copy, distance, kind)
    cache = {}
    # 1. the untagged Beatles files against the studio-album copies
    unk = {r['filename']: r for r in rows_in(conn, UNKNOWN)}
    if unk and os.path.exists(FP_JSON):
        for o in json.load(open(FP_JSON, encoding='utf-8')):
            u = unk.get(o['file'])
            if not u or not o['match']:
                continue
            m = conn.execute('SELECT * FROM files WHERE path=?', (o['match'],)).fetchone()
            if m is None:
                continue
            m = dict(m)
            d = merge.fp_distance(fpof(cache, u), fpof(cache, m))
            if d <= merge.FP_MAYBE:
                pairs.append((u, m, d, 'beatles'))
    # 2. folders holding two copies: pair files inside them by fingerprint
    for folder in DOUBLED:
        rs = rows_in(conn, folder)
        used = set()
        for i, a in enumerate(rs):
            if a['path'] in used:
                continue
            best = None
            for b in rs[i + 1:]:
                if b['path'] in used or abs((a['duration'] or 0) - (b['duration'] or 0)) > 6:
                    continue
                fa, fb = fpof(cache, a), fpof(cache, b)
                if fa is None or fb is None:
                    continue
                d = merge.fp_distance(fa, fb)
                if d <= merge.FP_SAME and (best is None or d < best[0]):
                    best = (d, b)
            if best:
                used.add(a['path']); used.add(best[1]['path'])
                # the copy with the album's own numbering ("NN - Title") is the "album copy"
                a_num = re.match(r'^\d\d - ', a['filename']) is not None
                x, y = (best[1], a) if a_num else (a, best[1])
                pairs.append((x, y, best[0], 'doubled'))
    return pairs


def decide(conn, pairs):
    cutoffs = {}
    for x, y, d, kind in pairs:
        for r in (x, y):
            cutoffs[r['path']] = r['cutoff']
    acoustid = acoustid_titles()
    out = []
    for x, y, d, kind in pairs:
        w, l, why = merge.pick_winner(x, y, cutoffs)
        fix = true_title(x, y, acoustid) if kind == 'beatles' else None
        out.append({'kind': kind, 'winner': w, 'loser': l, 'dist': round(d, 3), 'why': why,
                    'winner_is_album_copy': w['path'] == y['path'], 'retitle': fix})
    return out


def place_name(album_copy, title, ext):
    """The file name the album copy's slot should carry: its number, the song's title."""
    m = re.match(r'^(\d{1,2})\s*[-. ]?\s*', album_copy['filename'])
    num = m.group(1).zfill(2) if m else None
    safe = re.sub(r'[<>:"/\\|?*]', '', title).strip().rstrip('.')
    return ('%s - %s%s' % (num, safe, ext)) if num else safe + ext


def set_title(conn, rel, title):
    full = os.path.join(ROOT, rel.replace('/', os.sep))
    f = mutagen.File(full, easy=True)
    if f is None or f.tags is None:
        return
    old = (f.get('title') or [None])[0]
    if old == title:
        return
    f['title'] = [title]; f.save()
    conn.execute('INSERT INTO tag_backup (ts,op,path,field,old,new) VALUES (?,?,?,?,?,?)',
                 (time.time(), OP, rel, 'title', old, title))
    conn.execute('UPDATE files SET title=? WHERE path=?', (title, rel))


def easy_tags(path):
    a = mutagen.File(path, easy=True)
    if a is None or a.tags is None:
        return {}
    return {k: a.get(k) for k in ('album', 'tracknumber', 'discnumber', 'albumartist', 'artist', 'date', 'genre', 'albumartistsort', 'artistsort')}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--apply', action='store_true'); a = ap.parse_args()
    conn = musiclib.db(); conn.row_factory = sqlite3.Row
    pairs = find_pairs(conn)
    dec = decide(conn, pairs)
    by = collections.Counter((d['kind'], 'album copy wins' if d['winner_is_album_copy'] else 'other copy wins') for d in dec)
    print(len(dec), 'pairs:', dict(by))
    unmeasured = [d for d in dec if d['winner']['cutoff'] is None or d['loser']['cutoff'] is None]
    print('decided without a cutoff on both sides:', len(unmeasured))
    ret = [d for d in dec if d['retitle']]
    print('titles corrected:', len(ret))
    for d in ret:
        keeper = d['winner'] if d['winner_is_album_copy'] else d['loser']
        print('   %s  ->  %s' % (keeper['path'].split('Beatles/')[1], d['retitle']))
    for d in dec[:12]:
        w, l = d['winner'], d['loser']
        print('  %s | keep %s (%s %dk %.1fkHz) over %s (%s %dk %.1fkHz) [%s]' % (
            d['kind'], w['filename'], w['ext'], (w['bitrate'] or 0) / 1000, w['cutoff'] or 0,
            l['filename'], l['ext'], (l['bitrate'] or 0) / 1000, l['cutoff'] or 0, d['why']))
    json.dump([{k: (v if not isinstance(v, dict) else v['path']) for k, v in d.items()} for d in dec],
              open(os.path.join(HERE, 'pairplan.json'), 'w', encoding='utf-8'), indent=0)
    if not a.apply:
        print('dry run; pairplan.json written'); return
    snap = os.path.join(HERE, 'library.db.pre-resolve-pairs')
    if not os.path.exists(snap):
        conn.commit(); import shutil; shutil.copy(os.path.join(HERE, 'library.db'), snap)
    moved = 0
    for d in dec:
        w, l = d['winner'], d['loser']
        if d['winner_is_album_copy']:
            musiclib._move(conn, l['path'], musiclib.SUPERSEDED, OP, d['kind'] + ' loser', False)
            if d['retitle']:
                set_title(conn, w['path'], d['retitle'])
                newname = place_name(w, d['retitle'], w['ext'])
                if newname != w['filename']:
                    musiclib._move_to(conn, w['path'], os.path.dirname(w['path']) + '/' + newname, OP, 'renamed to its real title')
        else:
            # the album copy loses: remember its place and tags, retire it, put the winner there
            place = l['path']
            tags = easy_tags(os.path.join(ROOT, place.replace('/', os.sep)))
            musiclib._move(conn, l['path'], musiclib.SUPERSEDED, OP, d['kind'] + ' loser', False)
            if d['kind'] == 'beatles':
                title = d['retitle'] or l['title'] or w['title'] or os.path.splitext(w['filename'])[0]
                dst = os.path.dirname(place) + '/' + place_name(l, title, w['ext'])
            else:
                dst = os.path.splitext(place)[0] + w['ext']
            got = musiclib._move_to(conn, w['path'], dst, OP, d['kind'] + ' winner takes the loser\'s place')
            if d['retitle']:
                set_title(conn, got, d['retitle'])
            f = mutagen.File(os.path.join(ROOT, got.replace('/', os.sep)), easy=True)
            if f is not None and f.tags is not None:
                changed = False
                for k, v in tags.items():
                    if v and not f.get(k):
                        f[k] = v; changed = True
                if changed:
                    f.save()
                    conn.execute('UPDATE files SET album=?, track=?, disc=?, albumartist=? WHERE path=?',
                                 ((tags.get('album') or [None])[0],
                                  int(re.match(r'\d+', (tags.get('tracknumber') or ['0'])[0]).group()) if re.match(r'\d+', (tags.get('tracknumber') or ['0'])[0]) else None,
                                  int(re.match(r'\d+', (tags.get('discnumber') or ['0'])[0]).group()) if re.match(r'\d+', (tags.get('discnumber') or ['0'])[0]) else None,
                                  (tags.get('albumartist') or [None])[0], got))
        moved += 1
        if moved % 25 == 0:
            conn.commit(); print('  ', moved, '/', len(dec), flush=True)
    # Disc 2 files that had no partner still carry Disc 1 titles
    for r in [dict(x) for x in conn.execute("SELECT * FROM files WHERE folder=? AND kind='audio'", (WHITE2,))]:
        want = WHITE2_TITLES.get(r['track'])
        if want and abs((r['duration'] or 0) - want[1]) <= 4 and norm_title(r['title']) != norm_title(want[0]):
            set_title(conn, r['path'], want[0])
            newname = place_name(r, want[0], r['ext'])
            if newname != r['filename']:
                musiclib._move_to(conn, r['path'], WHITE2 + '/' + newname, OP, 'renamed to its real title')
            print('  retitled', r['filename'], '->', want[0])
    conn.commit()
    d = os.path.join(ROOT, UNKNOWN.replace('/', os.sep))
    if os.path.isdir(d) and not os.listdir(d):
        os.rmdir(d); print('removed empty', UNKNOWN)
    print('resolved', moved, 'pairs')


if __name__ == '__main__':
    main()
