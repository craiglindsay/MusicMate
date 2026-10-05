"""Find duplicate albums/tracks across the libraries and decide which copy wins.

Matching is deliberately layered:
  1. group files into albums by folder
  2. pair albums across sources on normalised artist+album
  3. inside a paired album, match tracks by acoustic fingerprint, falling back to
     track number + title + duration when a fingerprint is missing
  4. rank the matched copies on measured quality, not just declared bitrate

Nothing here moves files; it produces a plan for review.
"""
import os, re, sys, json, sqlite3, unicodedata, collections, difflib
import numpy as np
import musiclib, quality

ROOT = musiclib.ROOT

# ------------------------------------------------------------------ normalising
_PAREN = re.compile(r'\(([^)]*)\)|\[([^\]]*)\]')
NOISE  = {'bonus','bonus track','bonus tracks','deluxe','deluxe edition','remaster',
          'remastered','explicit','clean','digital','digital remaster','expanded',
          'special edition','anniversary edition','disc','cd','edition','version',
          'the album','soundtrack','ost','limited edition','reissue','mono','stereo'}

def strip_accents(s):
    return unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode()

def norm_artist(s):
    s = strip_accents(s).lower().strip()
    s = re.sub(r'^(the|a|an)\s+', '', s)
    s = re.sub(r',\s*(the|a|an)$', '', s)
    s = re.sub(r'\s*(feat|ft|featuring|with)\b.*$', '', s)
    s = re.sub(r'&', ' and ', s)
    s = re.sub(r'[^a-z0-9]+', '', s)
    return s

def norm_album(s):
    s = strip_accents(s).lower().strip()
    # drop parenthetical qualifiers that are pure edition noise
    def _drop(m):
        inner = (m.group(1) or m.group(2) or '').strip().lower()
        inner_c = re.sub(r'[^a-z0-9 ]', '', inner)
        if any(w in inner_c for w in NOISE) or re.fullmatch(r'\d{2,4}', inner_c):
            return ' '
        return ' ' + inner + ' '
    s = _PAREN.sub(_drop, s)
    s = re.sub(r'\b(disc|cd|disk|volume|vol)\s*\.?\s*\d+\b', ' ', s)
    s = re.sub(r'^(the|a|an)\s+', '', s)
    s = re.sub(r'[^a-z0-9]+', '', s)
    return s

def norm_title(s):
    s = strip_accents(s).lower().strip()
    s = _PAREN.sub(' ', s)
    s = re.sub(r'\s*-\s*(live|remix|edit|single|album|radio|remaster).*$', '', s)
    s = re.sub(r'^\d{1,3}[\s._-]+', '', s)
    s = re.sub(r'[^a-z0-9]+', '', s)
    return s

_DISC_ONLY = re.compile(r'^\s*(?:disc|cd|disk)\s*\.?\s*(\d+)\s*$', re.I)
_DISC_ANY  = re.compile(r'\b(?:disc|cd|disk)\s*\.?\s*(\d+)\b', re.I)

def album_and_disc(parts):
    """Album title and disc number from a folder path.

    A folder named just "Disc 2" carries no title of its own -- and norm_album
    strips the disc marker, so it would normalise to the empty string and collide
    with every other disc under that artist. Take the title from the parent and
    keep the disc number, so discs of one release stay distinct.
    """
    name = parts[-1]
    m = _DISC_ONLY.match(name)
    if m and len(parts) >= 2:
        return parts[-2], int(m.group(1))
    m = _DISC_ANY.search(name)
    return name, int(m.group(1)) if m else 0

def norm_album_safe(s):
    """norm_album, but never empty -- fall back to the raw normalised name."""
    v = norm_album(s)
    if v:
        return v
    return re.sub(r'[^a-z0-9]+', '', strip_accents(s).lower()) or 'unknown'

def disc_of(folder, row):
    if row['disc']: return row['disc']
    m = re.search(r'\b(?:disc|cd|disk)\s*\.?\s*(\d+)\b', folder, re.I)
    return int(m.group(1)) if m else None

# ------------------------------------------------------------------ fingerprints
POPCNT = np.array([bin(i).count('1') for i in range(256)], dtype=np.uint8)

def _bits(a):
    return POPCNT[np.frombuffer(a.astype('<u4').tobytes(), dtype=np.uint8)].sum()

def fp_distance(a, b, max_off=40):
    """Min bit-error-rate between two raw chromaprint vectors. 0 = identical."""
    a = np.asarray(a, dtype=np.uint32); b = np.asarray(b, dtype=np.uint32)
    if a.size < 30 or b.size < 30:
        return 1.0
    best = 1.0
    for off in range(-max_off, max_off + 1, 1):
        if off < 0: x, y = a[-off:], b[:a.size + off]
        else:       x, y = a[:b.size - off] if off else a, b[off:]
        n = min(x.size, y.size)
        if n < 30:
            continue
        x, y = x[:n], y[:n]
        d = _bits(np.bitwise_xor(x, y)) / (32.0 * n)
        if d < best:
            best = d
    return best

# Calibrated on a known AAC-vs-MP3 duplicate album (Eminem/Encore, 20 tracks):
# true matches scored 0.023-0.088, non-matches 0.373+. These sit in that gap.
FP_SAME  = 0.15
FP_MAYBE = 0.25

# ------------------------------------------------------------------ loading
def load_albums(conn, srcs=None):
    q = ("SELECT * FROM files WHERE kind='audio'"
         + (" AND src IN (%s)" % ','.join('?' * len(srcs)) if srcs else ''))
    rows = conn.execute(q, srcs or []).fetchall()
    albums = collections.defaultdict(list)
    for r in rows:
        albums[r['folder']].append(r)
    out = {}
    for folder, tracks in albums.items():
        parts = folder.split('/')
        src = parts[0]
        # the folder is the most reliable artist/album signal in this library,
        # but prefer a consistent albumartist tag when one exists
        aa = collections.Counter(t['albumartist'] for t in tracks if t['albumartist'])
        ar = collections.Counter(t['artist'] for t in tracks if t['artist'])
        al = collections.Counter(t['album'] for t in tracks if t['album'])
        folder_artist = parts[1] if len(parts) > 2 else (parts[1] if len(parts) > 1 else '')
        # "Itunes Matched" is a nested library inside VariousArtists
        if len(parts) > 2 and parts[1].lower() == 'itunes matched':
            src = 'VariousArtists/Itunes Matched'
            folder_artist = parts[2] if len(parts) > 3 else ''
        tag_artist = (aa.most_common(1)[0][0] if aa else
                      (ar.most_common(1)[0][0] if ar else ''))
        # A grab-bag folder ("Assorted", a singles box) holds tracks from many
        # releases. Taking its most common album tag makes it masquerade as
        # whichever album happens to lead the count -- Pet Shop Boys "Assorted" is
        # 13 untagged + 10 "Nightlife", and would swallow the real Nightlife album.
        # Only trust the tag when it genuinely describes the folder.
        tag_album, coherence = '', 1.0
        if al:
            top, cnt = al.most_common(1)[0]
            # against every track in the folder, not just the tagged ones -- a
            # folder that is half untagged is exactly the incoherent case we mean
            # to catch
            coherence = cnt / max(1, len(tracks))
            if coherence >= 0.6:
                tag_album = top
        folder_album, fdisc = album_and_disc(parts)
        # a disc number from the tags is as good as one from the folder name
        tdiscs = {t['disc'] for t in tracks if t['disc']}
        tdisc = tdiscs.pop() if len(tdiscs) == 1 else 0
        _, tag_disc_in_name = album_and_disc([tag_album or folder_album])
        disc = fdisc or tag_disc_in_name or tdisc or 0
        out[folder] = dict(
            folder=folder, src=src, tracks=tracks, disc=disc,
            coherence=coherence,
            artist=tag_artist or folder_artist,
            album=tag_album or folder_album,
            folder_artist=folder_artist, folder_album=folder_album,
            key=(norm_artist(tag_artist or folder_artist),
                 norm_album_safe(tag_album or folder_album), disc),
            fkey=(norm_artist(folder_artist), norm_album_safe(folder_album), disc),
        )
    return out

def library_of(a):
    """Which of the three pools this album belongs to."""
    return a['src']

# ------------------------------------------------------------------ track match
def match_tracks(A, B, fpcache):
    """Pair tracks between two albums. Returns (pairs, only_a, only_b).

    pairs: list of (rowA, rowB, how, distance)
    """
    ta, tb = list(A['tracks']), list(B['tracks'])
    pairs, used_b = [], set()

    def fp(r):
        if r['path'] not in fpcache:
            fpcache[r['path']] = (musiclib.unpack_fp(r['fp']) if r['fp'] else None)
        return fpcache[r['path']]

    # pass 1 -- acoustic fingerprint, best match wins
    cand = []
    for i, x in enumerate(ta):
        fx = fp(x)
        if fx is None: continue
        for j, y in enumerate(tb):
            fy = fp(y)
            if fy is None: continue
            if x['duration'] and y['duration'] and abs(x['duration'] - y['duration']) > 25:
                continue                      # cheap reject before the expensive compare
            d = fp_distance(fx, fy)
            if d <= FP_MAYBE:
                cand.append((d, i, j))
    cand.sort()
    used_a = set()
    for d, i, j in cand:
        if i in used_a or j in used_b: continue
        used_a.add(i); used_b.add(j)
        pairs.append((ta[i], tb[j], 'fingerprint', d))

    # pass 2 -- metadata for anything left (missing/failed fingerprints)
    for i, x in enumerate(ta):
        if i in used_a: continue
        bt, bj = None, None
        for j, y in enumerate(tb):
            if j in used_b: continue
            tx, ty = norm_title(x['title'] or ''), norm_title(y['title'] or '')
            if tx and tx == ty:
                sc = 2
            elif (x['track'] and x['track'] == y['track']
                  and disc_of(A['folder'], x) == disc_of(B['folder'], y)):
                sc = 1
            else:
                continue
            if x['duration'] and y['duration'] and abs(x['duration'] - y['duration']) > 20:
                continue
            if bt is None or sc > bt:
                bt, bj = sc, j
        if bj is not None:
            used_a.add(i); used_b.add(bj)
            pairs.append((ta[i], tb[bj], 'title' if bt == 2 else 'tracknum', None))

    only_a = [ta[i] for i in range(len(ta)) if i not in used_a]
    only_b = [tb[j] for j in range(len(tb)) if j not in used_b]
    return pairs, only_a, only_b

# ------------------------------------------------------------------ ranking
def pick_winner(x, y, cutoffs):
    """Return (winner, loser, reason). cutoffs: path -> measured kHz (or None).

    These two files are the same recording, so they share a content ceiling. That
    makes the comparison relative: judging each against an absolute bitrate->cutoff
    table misfires on genuinely narrow-band material (old or mono recordings, skits),
    where whichever copy happens to fall on the far side of a threshold wins for no
    real reason. A copy is only penalised for bandwidth *the other copy kept*.
    """
    cx, cy = cutoffs.get(x['path']), cutoffs.get(y['path'])
    sx, nx = quality.score_pair(x, cx, cy)
    sy, ny = quality.score_pair(y, cy, cx)

    # Cutoffs matching to within measurement noise across *different* codecs means
    # one copy was almost certainly made from the other: a transcode inherits the
    # source's lowpass exactly. Awarding AAC its efficiency bonus there would hand
    # the win to the derived copy, so compare raw bitrate instead.
    if (cx is not None and cy is not None and abs(cx - cy) < 0.2
            and x['ext'] != y['ext']
            and x['ext'] not in quality.LOSSLESS_EXT
            and y['ext'] not in quality.LOSSLESS_EXT):
        sx = (x['bitrate'] or 0) / 1000.0
        sy = (y['bitrate'] or 0) / 1000.0
        nx = ny = ['identical cutoff across codecs - one derived from the other']

    # a materially shorter file is a fragment, never the better copy
    dx, dy = x['duration'] or 0, y['duration'] or 0
    if dx and dy and abs(dx - dy) > 20:
        if dx > dy: return x, y, f'longer ({dx:.0f}s vs {dy:.0f}s) - other looks truncated'
        else:       return y, x, f'longer ({dy:.0f}s vs {dx:.0f}s) - other looks truncated'

    # Neither copy lost bandwidth and their rates are close: the spectral evidence
    # says they are the same quality, so do not let a nominal bitrate gap decide it.
    # This is what keeps a VBR rip from losing to a CBR one that merely reports a
    # bigger number.
    clean = ('lossy source' not in ' '.join(nx)) and ('lossy source' not in ' '.join(ny))
    hi = max(sx, sy)
    if clean and hi > 0 and abs(sx - sy) / hi < 0.15:
        sx = sy = hi

    if abs(sx - sy) < 8:
        # too close to call on audio; prefer richer tags, then embedded art
        tx = sum(1 for k in ('albumartist','album','title','track','year','genre') if x[k])
        ty = sum(1 for k in ('albumartist','album','title','track','year','genre') if y[k])
        if tx != ty:
            return (x, y, 'equivalent audio, better tags') if tx > ty else \
                   (y, x, 'equivalent audio, better tags')
        if (x['has_art'] or 0) != (y['has_art'] or 0):
            return (x, y, 'equivalent audio, has art') if (x['has_art'] or 0) > (y['has_art'] or 0) else \
                   (y, x, 'equivalent audio, has art')
        return (x, y, 'equivalent; kept incumbent')
    if sx > sy:
        return x, y, f'higher quality ({sx:.0f} vs {sy:.0f}; {"; ".join(nx) or "bitrate"})'
    return y, x, f'higher quality ({sy:.0f} vs {sx:.0f}; {"; ".join(ny) or "bitrate"})'

# ------------------------------------------------------------------ pairing
import difflib

POOLS = ['VariousArtists', 'VariousArtists/Itunes Matched']

def pair_albums(albums, fuzzy=0.90):
    """Group albums that are the same release across different pools."""
    # The folder tree is the trustworthy signal here: tags are wrong often enough
    # that keying on them alone cross-links unrelated albums (an Enigma album in
    # this library is tagged end-to-end as Basement Jaxx / Rooty). So group on the
    # folder key, and only admit the tag key when its artist corroborates the
    # folder's -- otherwise a mistagged album drags a stranger into its group.
    by_key = collections.defaultdict(list)
    for f, a in albums.items():
        by_key[a['fkey']].append(a)
        if a['key'] != a['fkey'] and a['key'][0] and a['fkey'][0]:
            if difflib.SequenceMatcher(None, a['key'][0], a['fkey'][0]).ratio() >= 0.6:
                by_key[a['key']].append(a)

    groups, claimed = [], set()
    for key, members in by_key.items():
        members = [m for m in members if m['folder'] not in claimed]
        pools = {m['src'] for m in members}
        if len(members) > 1 and len(pools) > 1:
            for m in members: claimed.add(m['folder'])
            groups.append(dict(key=key, members=members, how='exact'))

    # fuzzy pass: same artist, near-identical album title, still unclaimed
    rest = [a for f, a in albums.items() if f not in claimed]
    by_artist = collections.defaultdict(list)
    for a in rest: by_artist[a['key'][0]].append(a)
    for artist, items in by_artist.items():
        if not artist or len(items) < 2: continue
        for i in range(len(items)):
            if items[i]['folder'] in claimed: continue
            grp = [items[i]]
            for j in range(i + 1, len(items)):
                if items[j]['folder'] in claimed: continue
                if items[i]['src'] == items[j]['src']: continue
                if items[i]['disc'] != items[j]['disc']: continue   # never merge discs
                r = difflib.SequenceMatcher(None, items[i]['key'][1],
                                            items[j]['key'][1]).ratio()
                if r >= fuzzy:
                    grp.append(items[j])
            if len(grp) > 1 and len({g['src'] for g in grp}) > 1:
                for g in grp: claimed.add(g['folder'])
                groups.append(dict(key=items[i]['key'], members=grp, how='fuzzy'))
    singles = [a for f, a in albums.items() if f not in claimed]
    return groups, singles

def track_key_str(r):
    return f"{r['track'] or '?'} {r['title'] or r['filename']}"

def build_plan(conn, only_pools=None, limit=None, verbose=False):
    albums = load_albums(conn)
    if only_pools:
        albums = {f: a for f, a in albums.items() if a['src'] in only_pools}
    groups, singles = pair_albums(albums)
    if limit: groups = groups[:limit]

    fpcache, cutoffs = {}, {}
    plan = {'groups': [], 'stats': collections.Counter()}

    for g in groups:
        members = sorted(g['members'], key=lambda m: POOLS.index(m['src'])
                         if m['src'] in POOLS else 99)
        base = members[0]
        entry = dict(key=list(g['key']), how=g['how'],
                     albums=[dict(folder=m['folder'], src=m['src'],
                                  n=len(m['tracks'])) for m in members],
                     decisions=[], unique=[], conflicts=[])
        for other in members[1:]:
            pairs, only_a, only_b = match_tracks(base, other, fpcache)
            for x, y, how, dist in pairs:
                w, l, why = pick_winner(x, y, cutoffs)
                entry['decisions'].append(dict(
                    match=how, dist=None if dist is None else round(dist, 3),
                    keep=w['path'], drop=l['path'], why=why,
                    keep_br=w['bitrate'], drop_br=l['bitrate']))
                plan['stats']['pairs'] += 1
                plan['stats'][f'win:{w["src"] if "src" in w.keys() else "?"}'] += 1
            for t in only_b:
                entry['unique'].append(dict(path=t['path'], src=other['src'],
                                            track=track_key_str(t)))
                plan['stats']['unique_in_other'] += 1
            for t in only_a:
                entry['unique'].append(dict(path=t['path'], src=base['src'],
                                            track=track_key_str(t)))
        plan['groups'].append(entry)
    plan['singles'] = [dict(folder=a['folder'], src=a['src'], n=len(a['tracks']))
                       for a in singles]
    return plan

# ------------------------------------------------------------------ execution
DEST_POOL = 'VariousArtists'      # everything consolidates here

def choose_destination(members):
    """Album folder the merged result should live in.

    Prefer a folder whose own name matches the release, not merely the one with the
    most files: a mixed "Assorted"/singles folder can out-count the real album and
    would otherwise become the destination that everything is consolidated into.
    """
    def named_right(m):
        return norm_album_safe(m['folder_album']) == m['key'][1] or                norm_album_safe(m['folder_album']) == m['fkey'][1]
    main = [m for m in members if m['src'] == DEST_POOL]
    pool = main or members
    titled = [m for m in pool if named_right(m) and m.get('coherence', 1.0) >= 0.6]
    if titled:
        return max(titled, key=lambda m: len(m['tracks']))
    coherent = [m for m in pool if m.get('coherence', 1.0) >= 0.6]
    if coherent:
        return max(coherent, key=lambda m: len(m['tracks']))
    return max(pool, key=lambda m: len(m['tracks']))

def classify(dec):
    """Only an acoustic match is safe to act on unattended."""
    if dec['match'] == 'fingerprint' and dec['dist'] is not None and dec['dist'] <= FP_SAME:
        return 'auto'
    if dec['match'] == 'fingerprint':
        return 'review'
    return 'review'

def plan_moves(conn, plan):
    """Turn a plan into concrete (src, dest_folder, kind, why) move records."""
    albums = load_albums(conn)
    moves = []
    for g in plan['groups']:
        members = [albums[a['folder']] for a in g['albums'] if a['folder'] in albums]
        if not members: continue
        dest = choose_destination(members)
        for dec in g['decisions']:
            conf = classify(dec)
            dec['confidence'] = conf
            if conf != 'auto':
                continue
            loser = dec['drop']
            moves.append(dict(kind='supersede', src=loser, dest=None,
                              why=dec['why'], group=dest['folder']))
            keep = dec['keep']
            if os.path.dirname(keep) != dest['folder']:
                moves.append(dict(kind='promote', src=keep, dest=dest['folder'],
                                  why='winner moves into merged album',
                                  group=dest['folder']))
        for u in g['unique']:
            if os.path.dirname(u['path']) != dest['folder']:
                moves.append(dict(kind='fill', src=u['path'], dest=dest['folder'],
                                  why=f'track only present in {u["src"]}',
                                  group=dest['folder']))
    return moves

def apply_moves(conn, moves, dry=True):
    done = collections.Counter()
    for m in moves:
        if m['kind'] == 'supersede':
            if dry: done['supersede'] += 1; continue
            musiclib._move(conn, m['src'], musiclib.SUPERSEDED, 'merge', m['why'], False)
            done['supersede'] += 1
        else:
            newrel = f"{m['dest']}/{os.path.basename(m['src'])}"
            if newrel == m['src']:
                continue
            if dry: done[m['kind']] += 1; continue
            src = os.path.join(ROOT, m['src'].replace('/', os.sep))
            dst = os.path.join(ROOT, newrel.replace('/', os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                b, e = os.path.splitext(dst); i = 2
                while os.path.exists(f'{b}__{i}{e}'): i += 1
                dst = f'{b}__{i}{e}'
                newrel = os.path.relpath(dst, ROOT).replace(os.sep, '/')
            try: size = os.path.getsize(src)
            except OSError: size = None
            os.replace(src, dst)
            conn.execute('INSERT INTO moves (ts,op,reason,src,dst,size) VALUES (?,?,?,?,?,?)',
                         (__import__('time').time(), 'merge', m['why'], m['src'], newrel, size))
            conn.execute('UPDATE files SET path=?, folder=? WHERE path=?',
                         (newrel, m['dest'], m['src']))
            done[m['kind']] += 1
    if not dry: conn.commit()
    return done
