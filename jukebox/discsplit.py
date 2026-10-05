"""Reconstruct disc boundaries for albums whose discs were merged into one folder.

The problem, concretely: "Rio (Collector's Edition)" is 28 files in one flat
folder. Track numbers run 1..15 and 1..15 again, the `disk` atom is absent on
every file, and nothing in the filenames says which disc a track came from. The
read-model sorted by (disc or 1, track, title), so the two discs interleaved and
the album played 1, 2, 2, 3, 3, 4, 4 ... instead of one disc and then the other.

227 albums in this library are affected, 5,117 tracks, and NOT ONE of them
carries a disc tag -- so this cannot be read, only inferred.

How the inference works. Tracks are grouped by track number; the largest group
size is the disc count. Walking the numbers in order, each group is dealt out to
distinct discs, choosing the arrangement whose titles best match what each disc
already holds. Bracketed qualifiers carry that signal: once "Last Chance On The
Stairway (Manchester Square Demo)" lands on a disc, "My Own Way (Manchester
Square Demo)" prefers the same one and "My Own Way (2009 Digital Remaster)"
prefers the other. Where titles carry no such signal the split degrades to a
stable alphabetical deal, which still yields contiguous discs.

What this does NOT recover is which physical disc is which: the numbering is
this program's, not the pressing's. Discs are ordered by their lowest track
number, then by size. So playback is contiguous and sensible, but "Disc 2" here
is not a claim about the booklet. Albums touched are flagged disc_inferred=1 so
the UI can say so rather than implying certainty.

Read-model only -- no audio file or tag is written. Idempotent.
"""
import re, sqlite3, collections as _c
from itertools import permutations, combinations
from pathlib import Path

DB = Path(__file__).resolve().parent / 'jukebox.db'

PARENS = re.compile(r'[\(\[]([^\)\]]{2,60})[\)\]]')
WORD = re.compile(r'[a-z0-9]+')
# words too common to say anything about which disc a track belongs to
STOP = {'version', 'mix', 'remix', 'edit', 'the', 'a', 'an', 'of', 'and',
        'digital', 'remaster', 'remastered', 'single', 'album', 'track'}


def tokens(title):
    """Signal words from a title's bracketed qualifiers.

    The qualifier is where disc identity hides on this kind of release --
    "(Manchester Square Demo)" vs "(2009 Digital Remaster)". The song name
    itself is useless here, because both discs carry the same songs."""
    out = set()
    for g in PARENS.findall(title or ''):
        for w in WORD.findall(g.lower()):
            if w not in STOP and len(w) > 1:
                out.add(w)
    return out


def score(tok, profile):
    if not tok or not profile:
        return 0.0
    return sum(profile.get(w, 0) for w in tok) / float(len(tok))


def split_album(tracks, k):
    """tracks: [(id, track_no, title)] -> {track_id: disc_index 0..k-1}"""
    by_no = _c.defaultdict(list)
    for tid, no, title in tracks:
        by_no[no].append((tid, title or ''))
    profiles = [_c.Counter() for _ in range(k)]
    counts = [0] * k
    last = [0] * k          # highest track number placed on each disc so far
    out = {}
    for no in sorted(by_no):
        group = sorted(by_no[no], key=lambda p: p[1].lower())   # stable
        m = len(group)
        best, best_score = None, None
        # choose which m of the k discs receive these, and in what arrangement
        for discs in combinations(range(k), m):
            for perm in permutations(discs):
                # Continuity dominates: a disc whose run ends at no-1 is where
                # this track belongs. Without this a uniquely numbered track
                # chased the best word match and the discs alternated
                # 1, 2, 1, 2 -- worse than the interleave being fixed.
                s = 2.0 * sum(1.0 / (1 + abs(no - 1 - last[perm[i]])) for i in range(m))
                s += sum(score(tokens(group[i][1]), profiles[perm[i]]) for i in range(m))
                s -= 0.01 * sum(counts[d] for d in perm)
                if best_score is None or s > best_score:
                    best_score, best = s, perm
        for i, d in enumerate(best):
            tid, title = group[i]
            out[tid] = d
            counts[d] += 1
            last[d] = max(last[d], no)
            for w in tokens(title):
                profiles[d][w] += 1
    return out


def build(db=DB, verbose=False, limit=None):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    if 'disc_inferred' not in {r['name'] for r in c.execute('PRAGMA table_info(album)')}:
        c.execute('ALTER TABLE album ADD COLUMN disc_inferred INTEGER DEFAULT 0')
    # Undo a previous run before redoing it, or an album that no longer
    # qualifies keeps the discs the old rules gave it.
    prev = [r[0] for r in c.execute('SELECT id FROM album WHERE disc_inferred=1')]
    for aid in prev:
        c.execute('UPDATE album SET disc_count=1 WHERE id=?', (aid,))
        for t in c.execute('SELECT id, track, title FROM track WHERE album_id=?', (aid,)).fetchall():
            c.execute('UPDATE track SET disc=NULL, sort_key=? WHERE id=?',
                      ('01.%03d.%s' % (t['track'] or 0, (t['title'] or '').lower()), t['id']))
    c.execute('UPDATE album SET disc_inferred=0')

    alb = _c.defaultdict(list)
    for r in c.execute('SELECT id, album_id, disc, track, title, duration FROM track'):
        alb[r['album_id']].append(r)

    done = 0
    dups = []
    for aid, ts in alb.items():
        if any(t['disc'] for t in ts):
            continue                       # real disc tags: leave them alone
        numbered = [t for t in ts if t['track']]
        if not numbered:
            continue
        cnt = _c.Counter(t['track'] for t in numbered)
        k = max(cnt.values())
        if k < 2 or k > 6:                 # >6 is broken tags, not a disc split
            continue
        # A merged multi-disc album RESTARTS its numbering, so most of the
        # number range repeats. An album with one or two stray duplicates is
        # just mis-tagged, and splitting it shreds a perfectly good order --
        # "Sleep Sounds" (26 tracks, 2 duplicates) came out alternating discs.
        top = max(cnt)
        dup = sum(1 for n, v in cnt.items() if v >= 2)
        if top < 4 or dup < 0.5 * top:
            continue
        assign = split_album([(t['id'], t['track'], t['title']) for t in numbered], k)

        members = _c.defaultdict(list)
        for t in numbered:
            members[assign[t['id']]].append(t)

        # Three copies of one album in a folder look exactly like a 3-disc set
        # by the numbers -- "Big Willie Style" was 46 files that were the same
        # 16 tracks three times over, Intro at 111s/112s/112s. Real discs carry
        # different music, so if the discs hold the same titles at the same
        # lengths this is duplication, not a pressing.
        sig = {d: {((t['title'] or '').strip().lower(), round((t['duration'] or 0) / 3))
                   for t in v} for d, v in members.items()}
        ds = sorted(sig, key=lambda d: len(sig[d]))
        dupish = False
        for i in range(len(ds)):
            for j in range(i + 1, len(ds)):
                small = sig[ds[i]]
                if small and len(small & sig[ds[j]]) >= 0.6 * len(small):
                    dupish = True
        if dupish:
            dups.append((aid, len(numbered), k))
            continue
        order = sorted(members, key=lambda d: (min(x['track'] for x in members[d]),
                                               -len(members[d])))
        rank = {d: i + 1 for i, d in enumerate(order)}

        for t in numbered:
            d = rank[assign[t['id']]]
            sk = '%02d.%03d.%s' % (d, t['track'] or 0, (t['title'] or '').lower())
            c.execute('UPDATE track SET disc=?, sort_key=? WHERE id=?', (d, sk, t['id']))
        # untracked leftovers sort after the numbered ones
        for t in ts:
            if not t['track']:
                c.execute('UPDATE track SET sort_key=? WHERE id=?',
                          ('99.999.%s' % (t['title'] or '').lower(), t['id']))
        c.execute('UPDATE album SET disc_count=?, disc_inferred=1 WHERE id=?', (len(order), aid))
        done += 1
        if verbose:
            sizes = '/'.join(str(len(members[d])) for d in order)
            name = c.execute('SELECT title FROM album WHERE id=?', (aid,)).fetchone()[0]
            print('%-44s %d discs (%s)' % ((name or '')[:44], len(order), sizes))
        if limit and done >= limit:
            break
    c.commit()
    n_tr = c.execute('SELECT COUNT(*) FROM track JOIN album ON album.id=track.album_id '
                     'WHERE album.disc_inferred=1').fetchone()[0]
    c.close()
    return done, n_tr, dups


if __name__ == '__main__':
    n, t, dups = build(verbose=True)
    print('')
    print('%d albums split into discs, %d tracks re-sequenced' % (n, t))
    if dups:
        print('%d albums look like duplicate copies, not discs (left alone):' % len(dups))
        for aid, n_tr, k in dups[:20]:
            print('   album id %-6d %3d tracks, %dx repeated' % (aid, n_tr, k))
