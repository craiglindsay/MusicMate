"""Score karaoke word timing against the disc references (tools/discref.py).

For every split song with a reference, each LRC word is matched to the discs'
words by sequence (the same words in the same order), its reference time is
the median over the discs that have it, and every timing source is scored
the way the karaoke screen would show it:

    lrc   the LRC line times, words spread across the line by letters
    v1    words.v1.json (or words.json when it is still version 1)
    v2    words.json when it is version 2 (or --v2 <file name>)

A line the aligner did not trust is shown as the screen shows it: the LRC
time moved by the drift (v1: the song's median; v2: the line's own `d`),
words spread by letters.

    python jukebox/tools/align_eval.py
    python jukebox/tools/align_eval.py --v2 words.v2test.json --min-discs 2
"""
import os, re, sys, json, sqlite3, argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
STEMS = Path(os.environ.get('JUKEBOX_STEMS') or r'D:\MusicStems')
REF = HERE / 'cache' / 'discref'
LYRICS_DB = HERE / 'lyrics.db'
LRC_LINE = re.compile(r'\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)')
LEAD = 0.25
TRUST_FAIR = False       # --fair: also take the lines version 2 marks fair
MATCH_TOL = 4.0          # --tol: how far from the LRC a disc word may be matched
ALT = 'none'             # --alt doubt|all: use version 2's star-less fallback placement for those lines


def nw(w):
    return re.sub(r"[^a-z0-9]", '', w.lower())


def lrc(c, skey):
    """(time, text) per line, parsed exactly as the server does it."""
    from lyrics import parse_synced
    r = c.execute('select synced from lyric where skey=?', (skey,)).fetchone()
    return [(x['t'], x['line']) for x in parse_synced(r[0] if r else '')]


def spread(t, e, words):
    total = sum(len(w) + 1 for w in words) or 1
    acc, out = 0, []
    for w in words:
        out.append(t + acc / total * (e - t)); acc += len(w) + 1
    return out


def screen_starts(lines, W, dur):
    """Word start per (line index, word index), as the screen would place them."""
    body = [(i, t, txt.split()) for i, (t, txt) in enumerate(lines) if txt.split()]
    byI = {x['i']: x for x in (W or {}).get('lines', [])}
    v2 = (W or {}).get('version') == 2
    placed = {}
    drift = []
    for i, t, ws in body:
        x = byI.get(i)
        use_alt = x and x.get('alt') and len(x['alt']) == len(ws) and not x.get('ok') and (
            ALT == 'all' or (ALT == 'doubt' and not (TRUST_FAIR and x.get('fair'))))
        if use_alt:
            placed[i] = list(x['alt'])
            drift.append(x['alt'][0] - LEAD - t)
            continue
        if x and (x.get('ok') or (TRUST_FAIR and x.get('fair'))) and len(x.get('words') or []) == len(ws):
            st = [w['s'] for w in x['words']]
            placed[i] = st
            drift.append(st[0] - LEAD - t)
    drift.sort()
    d_song = max(-2.5, min(2.5, drift[len(drift) // 2])) if len(drift) >= 4 else 0.0
    starts = [None] * len(body)
    for n, (i, t, ws) in enumerate(body):
        if i in placed:
            continue
        x = byI.get(i)
        # version 2: the line's own drift, less the reading lead, as the screen does it
        d = ((x['d'] - LEAD if 'd' in x else d_song) if (v2 and x) else d_song) if W else 0.0
        starts[n] = t + d
    # line ends: the next line's start, capped by a letter estimate, for spread lines
    out = {}
    for n, (i, t, ws) in enumerate(body):
        if i in placed:
            for k, s in enumerate(placed[i]):
                out[(i, k)] = s
            continue
        t0 = starts[n]
        nxt = dur
        for m in range(n + 1, len(body)):
            j = body[m][0]
            nxt = placed[j][0] - LEAD if j in placed else starts[m]
            break
        est = max(1.2, min(nxt - t0, len(''.join(ws)) * 0.075))
        w0 = t0 + (min(LEAD, est * 0.3) if W else 0.0)     # the screen's karWordAt: words start LEAD in
        for k, s in enumerate(spread(w0, t0 + est, ws)):
            out[(i, k)] = s
    return out


def references(lines, ref, max_cost):
    """LRC word -> list of reference times, one per disc that has the word."""
    # rough LRC time of every word: repeated choruses make text alone ambiguous,
    # so a disc word may only match an LRC word within TOL seconds of it
    TOL = MATCH_TOL
    seq = []
    for n, (t, txt) in enumerate(lines):
        ws = txt.split()
        nxt = lines[n + 1][0] if n + 1 < len(lines) else t + 5
        est = spread(t, t + max(1.2, min(nxt - t, len(''.join(ws)) * 0.075)), ws)
        seq += [(n, k, nw(w), est[k]) for k, w in enumerate(ws) if nw(w)]
    got = {}
    for disc in ref['discs']:
        if disc['cost'] > max_cost:
            continue
        dw = [w for w in disc['words'] if nw(w['w'])]
        A, B = len(seq), len(dw)
        # longest common subsequence under the time tolerance
        L = [[0] * (B + 1) for _ in range(A + 1)]
        for x in range(A - 1, -1, -1):
            sx, tx = seq[x][2], seq[x][3]
            Lx, Lx1 = L[x], L[x + 1]
            for y in range(B - 1, -1, -1):
                if sx == nw(dw[y]['w']) and abs(dw[y]['s'] - tx) <= TOL:
                    Lx[y] = Lx1[y + 1] + 1
                else:
                    Lx[y] = Lx1[y] if Lx1[y] >= Lx[y + 1] else Lx[y + 1]
        x = y = 0; pairs = []
        while x < A and y < B:
            if seq[x][2] == nw(dw[y]['w']) and abs(dw[y]['s'] - seq[x][3]) <= TOL and L[x][y] == L[x + 1][y + 1] + 1:
                pairs.append((x, y)); x += 1; y += 1
            elif L[x + 1][y] >= L[x][y + 1]:
                x += 1
            else:
                y += 1
        # a lone match between gaps is as likely chance as not: keep runs of two or more
        keep = set()
        for q in range(len(pairs)):
            prev_ok = q > 0 and pairs[q - 1] == (pairs[q][0] - 1, pairs[q][1] - 1)
            next_ok = q + 1 < len(pairs) and pairs[q + 1] == (pairs[q][0] + 1, pairs[q][1] + 1)
            if prev_ok or next_ok:
                keep.add(q)
        for q in keep:
            x, y = pairs[q]
            got.setdefault((seq[x][0], seq[x][1]), []).append(dw[y]['s'])
    return got


def med(xs):
    xs = sorted(xs); n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def summary(errs):
    if not errs:
        return 'n=0'
    a = sorted(abs(e) for e in errs)
    n = len(a)
    return ('n=%5d  median %.3f  p90 %.3f  <=0.15s %4.1f%%  <=0.30s %4.1f%%  >0.5s %4.1f%%  bias %+.3f' % (
        n, a[n // 2], a[int(n * .9)], 100 * sum(x <= .15 for x in a) / n, 100 * sum(x <= .30 for x in a) / n,
        100 * sum(x > .5 for x in a) / n, med(errs)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--v2', default=None, help='file name of the version-2 result (default: words.json if v2)')
    ap.add_argument('--min-discs', type=int, default=2, help='score only words at least this many discs agree on')
    ap.add_argument('--agree', type=float, default=0.25, help='...and whose discs sit within this of their median')
    ap.add_argument('--max-cost', type=float, default=1.0)
    ap.add_argument('--only')
    ap.add_argument('--per-song', action='store_true')
    ap.add_argument('--only-v2', action='store_true', help='only songs that have a version-2 result')
    ap.add_argument('--fair', action='store_true', help="trust version 2's fair lines too")
    ap.add_argument('--alt', default='none', choices=('none', 'doubt', 'all'))
    ap.add_argument('--tol', type=float, default=4.0, help='seconds from the LRC a disc word may match (raise for badly-off files)')
    ap.add_argument('--songs-with', help='only songs that have this file beside the stems (same-set comparisons)')
    a = ap.parse_args()
    global TRUST_FAIR, ALT, MATCH_TOL
    TRUST_FAIR = a.fair
    ALT = a.alt
    MATCH_TOL = a.tol
    c = sqlite3.connect(f'file:{LYRICS_DB}?mode=ro', uri=True)
    errs = {'lrc': [], 'v1': [], 'v2': []}
    starts_err = {'lrc': [], 'v1': [], 'v2': []}
    noise = []
    songs = 0
    for f in sorted(REF.glob('*.json')):
        ref = json.loads(f.read_text(encoding='utf-8'))
        if a.only and a.only.lower() not in ref['song'].lower():
            continue
        d = STEMS / ref['song']
        lines = lrc(c, ref['skey'])
        if not lines or not d.exists():
            continue
        W = {}
        for name in ('words.v1.json', 'words.json') + ((a.v2,) if a.v2 else ()):
            p = d / name
            if p.exists():
                j = json.loads(p.read_text(encoding='utf-8'))
                W.setdefault('v%d' % j.get('version', 1), j) if name != a.v2 else W.__setitem__('v2', j)
        if a.songs_with and not (d / a.songs_with).exists():
            continue
        if a.only_v2 and ('v2' not in W or (a.v2 and not (d / a.v2).exists())):
            continue
        dur = (W.get('v1') or W.get('v2') or {}).get('duration', 1e4)
        got = references(lines, ref, a.max_cost)
        truth = {}
        for key, ts in got.items():
            if len(ts) < a.min_discs:
                continue
            m = med(ts)
            dev = [t - m for t in ts]
            if len(ts) >= 2:
                noise.extend(dev)
            if max(abs(x) for x in dev) <= a.agree or len(ts) >= 3 and sorted(abs(x) for x in dev)[len(ts) // 2] <= a.agree / 2:
                truth[key] = m
        if not truth:
            continue
        songs += 1
        systems = {'lrc': screen_starts(lines, None, dur)}
        for v in ('v1', 'v2'):
            if v in W:
                systems[v] = screen_starts(lines, W[v], dur)
        row = []
        for name, S in systems.items():
            e = [S[k] - t for k, t in truth.items() if k in S]
            errs[name].extend(e)
            starts_err[name].extend(S[k] - t for k, t in truth.items() if k in S and k[1] == 0)
            if e:
                row.append('%s %.2f' % (name, sorted(abs(x) for x in e)[len(e) // 2]))
        if a.per_song:
            print('%-55s words %4d  %s' % (ref['song'][:55], len(truth), '  '.join(row)))
    print('songs scored:', songs)
    print('reference noise (disc minus the discs\' median):', summary(noise))
    for name in ('lrc', 'v1', 'v2'):
        if errs[name]:
            print('%-4s words  %s' % (name, summary(errs[name])))
            print('%-4s lines  %s' % (name, summary(starts_err[name])))


if __name__ == '__main__':
    sys.exit(main())
