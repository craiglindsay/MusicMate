"""Word timings for karaoke by forced alignment of the lyrics to the vocal stem.

Runs in the karaoke venv (it needs torch), never in the server process:

    <venv>/python.exe align.py --dir "D:/MusicStems/ABBA - Dancing Queen [c015...]"
    <venv>/python.exe align.py --dir ... --print          # show the result

Why: the karaoke screen knew where each LINE started (an LRC file) and
guessed the words inside it, so a held note or an instrumental tail made the
words race ahead and then wait. Forced alignment (torchaudio's MMS_FA, a CTC
model) is given the vocal stem and the exact words and returns where each one
starts and ends, with a confidence. Each LRC line is aligned inside its own
window, because whole-song alignment slips on repeated choruses and long gaps.

Version 2 (23 Sep 2026). Version 1 cut each line's window from the LRC's own
timestamp, 0.8 s before it to 0.6 s after the next, with nothing to absorb
singing outside the line. Measured over 325 songs that clamped the aligner:
per-song drift piled up against the -0.8 s wall, a fifth of confident lines
ended within 80 ms of their window's end, and the check meant to catch a bad
placement could not fail. Now:

  1. the model runs ONCE over the whole stem (30 s chunks with context either
     side), so every later pass is only a cheap Viterbi over stored emissions;
  2. pass one aligns every line in a wide window (3 s either side) and notes
     how far its first word sits from the LRC time; the median of that drift
     over nine neighbouring lines is where the line really is, which follows
     a file made for another master, and a mid-song jump;
  3. pass two aligns each line together with its neighbours, in a window
     around their corrected times, with the MMS star token before, between
     and after the lines: a zero-cost "anything else" the model parks gaps,
     ad-libs and backing vocals on, while the neighbours' own words stop two
     lines claiming the same audio;
  4. each line is so aligned three times (in its own block and the blocks
     either side) and is trusted when those agree -- the audio decided, not
     the window; the model's own score is no guide on singing and no longer
     judges;
  5. a word ends where the voice under it stops (the envelope, env.json), not
     where CTC's last character spike fell: a held note used to leave the
     screen a median 0.9 s before the singer finished it;
  6. numbers are spelled out ("1999" -> nineteen ninety nine) so they are
     sound, not a zero-length token.

Output words.json beside the stems (the version-1 file is kept once as
words.v1.json for comparison):

    {"model": "MMS_FA", "version": 2, "sr": 16000, "offset": 0.42,
     "lines": [{"i": 0, "t": 33.64, "d": 0.41, "ok": true, "text": "Let me take you on a trip",
                "c": 0.81,                      # mean word confidence
                "words": [{"w": "Let", "s": 33.71, "e": 33.90, "ec": 33.85, "c": 0.93,
                           "u": [{"k": "char", "g": "l", "s": 33.71, "e": 33.77}, ...]},
                          ...]}]}

`d` is the line's drift from the LRC time (the local median), so the screen
can place a line the aligner doubted by its neighbours rather than the song.
`e` is where the voice under the word stops; `ec` is CTC's own end.

`words` are the line's text split on whitespace, one entry per token in the
same order, so the screen can index them directly. A token with no letters
("--", "...") gets a zero-length span at its neighbour's end so the indexes
still line up. `u` holds the units under each word: characters today (what a
CTC aligner over romanised text gives), phonemes later -- the singing-robot
project wants mouth shapes, and a phoneme aligner slots in here with
k="phone" without the screen having to change.

`c` is the model's mean per-token probability, which runs low on singing (a
median near 0.2 on a clean stem), so it is not judged on its own: `ok` says
whether the line is trusted. The screen uses `ok` and falls back to its
envelope estimate for the rest.
"""
import re, sys, json, time, argparse, sqlite3, subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
LYRICS_DB = HERE / 'lyrics.db'
SR = 16000
HOP = 320                       # samples per emission frame (20 ms)
CHUNK, CTX = 30.0, 1.5          # model chunk, and the context kept either side, seconds
WIDE = 3.0                      # pass one: padding either side of a line
PAD = 1.2                       # pass two: padding around the corrected line
MAX_LINE = 24.0                 # a line longer than this is an LRC with lines missing
EDGE = 0.08                     # words this close to a window edge were clamped by it
STABLE = 0.15                   # every block within this of the others: the audio decided
NEAR = (0.8, 0.6)               # the fallback window for a line the blocks disagree on
GLOBAL = 15.0                   # pass zero looks this far either side for the file's clock
STRAY = 0.3                     # a first letter this far ahead of the second is a stray
SNAP = (0.4, 0.05)              # a voice rise this far before (after) a word's start is its start
VOICE_FLOOR = 28               # env.json rms above this is voice (the screen's own floor)


def lrc_lines(skey):
    """The song's LRC lines exactly as the server hands them to the screen
    (lyrics.parse_synced), so a line's index here is its index there."""
    sys.path.insert(0, str(HERE))
    from lyrics import parse_synced
    c = sqlite3.connect(f'file:{LYRICS_DB}?mode=ro', uri=True)
    r = c.execute('SELECT synced FROM lyric WHERE skey=?', (skey,)).fetchone()
    c.close()
    return parse_synced(r[0] if r else '')


def decode(path):
    """vocals.opus -> float32 mono at 16 kHz, via ffmpeg."""
    import numpy as np
    p = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'f32le', '-ac', '1', '-ar', str(SR), '-'],
                       capture_output=True, check=True)
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


_ONES = ('zero one two three four five six seven eight nine ten eleven twelve thirteen '
         'fourteen fifteen sixteen seventeen eighteen nineteen').split()
_TENS = 'twenty thirty forty fifty sixty seventy eighty ninety'.split()


def say_number(n):
    """How a number is sung. Years are read in pairs, as people sing them."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10 - 2] + ('' if n % 10 == 0 else ' ' + _ONES[n % 10])
    if 1100 <= n <= 2099 and n % 100 != 0 and not (2000 <= n <= 2009):
        lo = n % 100
        return say_number(n // 100) + ' ' + (say_number(lo) if lo >= 10 else 'oh ' + _ONES[lo])
    if n < 1000:
        return _ONES[n // 100] + ' hundred' + ('' if n % 100 == 0 else ' ' + say_number(n % 100))
    if n < 1000000:
        return say_number(n // 1000) + ' thousand' + ('' if n % 1000 == 0 else ' ' + say_number(n % 1000))
    return ' '.join(_ONES[int(d)] for d in str(n))


def norm_word(w):
    """What the aligner can see of a word: lowercase letters and apostrophes,
    accents folded, digits spelled out. Everything else is not sound. A number
    can come back as several words' worth of letters, space-separated
    ("24" -> "twenty four"); the screen still sees one word."""
    import unicodedata
    s = unicodedata.normalize('NFKD', w).encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'\d+', lambda m: ' ' + say_number(int(m.group(0))) + ' ', s)
    parts = [re.sub(r"[^a-z']", '', p).strip("'") for p in s.split()]
    return ' '.join(p for p in parts if p)


def emissions(model, wave):
    """Log-probabilities for the whole stem, frame by frame, from 30 s chunks
    run with context either side so no frame is judged at a chunk's edge."""
    import torch
    n = len(wave)
    frames = n // HOP
    out = None
    step, ctx = int(CHUNK * SR), int(CTX * SR)
    for c0 in range(0, n, step):
        a, b = max(0, c0 - ctx), min(n, c0 + step + ctx)
        if b - a < SR // 4:
            continue
        with torch.inference_mode():
            em, _ = model(torch.from_numpy(wave[a:b]).unsqueeze(0))
        em = em[0]
        if out is None:
            out = torch.full((frames, em.size(1)), -30.0)
        per = (b - a) / em.size(0)                    # samples per frame in this chunk
        f0, f1 = c0 // HOP, min(frames, (c0 + step) // HOP)
        idx = ((torch.arange(f0, f1) * HOP - a) / per).round().long().clamp(0, em.size(0) - 1)
        out[f0:f1] = em[idx]
    return out


def align_window(aligner, tokenizer, em, a, b, groups, star=True):
    """Forced alignment of consecutive lines inside [a, b) seconds of the
    stored emissions: `groups` holds each line's tokens, and a star token sits
    before, between and after them, so a gap, an ad-lib or a backing vocal
    between two lines is nobody's word. Per line, per token, a list of
    (start, end, score) per character in seconds; None when it cannot align.
    With star=False there is nothing to park other sound on, so the words must
    fill the window: version 1's behaviour, a strong pull towards the window."""
    f0, f1 = int(a * SR / HOP), min(em.size(0), int(b * SR / HOP))
    if f1 - f0 < 12 or not any(groups):
        return None
    words = ['*'] if star else []
    for g in groups:
        if g:
            words += g + (['*'] if star else [])
    try:
        spans = aligner(em[f0:f1], tokenizer(words))
    except Exception:
        return None
    sec = HOP / SR
    conv = [[((f0 + t.start) * sec, (f0 + t.end) * sec, float(t.score)) for t in span] for span in spans]
    out, pos = [], (1 if star else 0)
    for g in groups:
        out.append(conv[pos:pos + len(g)] if g else [])
        pos += len(g) + (1 if (g and star) else 0)
    return out


def voice_end(rms, hz, e, limit):
    """Where the voice sounding at `e` stops: the envelope stays above the
    floor, allowing a 0.1 s breath, up to `limit`."""
    if not rms or limit <= e:
        return e
    i = int(e * hz)
    if i >= len(rms) or (rms[i] <= VOICE_FLOOR and (i + 1 >= len(rms) or rms[i + 1] <= VOICE_FLOOR)):
        return e                                       # nothing is sounding: CTC's end stands
    stop, gap, last = min(len(rms), int(limit * hz)), 0, e
    while i < stop:
        if rms[i] > VOICE_FLOOR:
            last, gap = (i + 1) / hz, 0
        else:
            gap += 1
            if gap > hz * 0.1:
                break
        i += 1
    return max(e, min(last, limit))


def voice_rises(wave):
    """Times the voice comes out of at least 0.25 s of silence, from 10 ms
    loudness: below the song's loud level less 34 dB is silence, above it
    less 24 dB is singing, and the rise is the first frame past silence."""
    import numpy as np
    hop = SR // 100
    n = len(wave) // hop
    if not n:
        return []
    db = 10 * np.log10((wave[:n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    loud = np.percentile(db, 95)
    on, off = db > loud - 24, db < loud - 34
    out, quiet, j = [], 0, 0
    for i in range(n):
        if off[i]:
            quiet += 1; j = i + 1
        elif on[i]:
            if quiet >= 25:
                out.append(round(j / 100, 3))
            quiet = 0
    return out


def global_offset(wave, starts, fit):
    """How far the LRC's clock is from the recording's, to the nearest bar or
    so. Every line start pulses a comb; the comb is slid over +-GLOBAL seconds
    against the voice's onsets (10 ms loudness, rises smoothed over 0.1 s),
    and the best few alignments, plus no shift at all, go to `fit` -- the
    aligner's own score for the words placed there -- which picks."""
    import numpy as np
    if len(starts) < 4:
        return 0.0
    hop = SR // 100
    n = len(wave) // hop
    db = 10 * np.log10((wave[:n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    loud = np.percentile(db, 95)
    v = np.clip((db - (loud - 34)) / 10.0, 0, 1)              # 0 silent .. 1 singing
    v = np.convolve(v, np.ones(10) / 10, mode='same')
    rise = np.maximum(0, np.diff(v, prepend=v[0]))
    rise = np.convolve(rise, np.ones(15) / 15, mode='same')    # a line may start a little either side
    idx = np.array([int(t * 100) for t in starts])
    shifts = np.arange(-GLOBAL * 100, GLOBAL * 100 + 1, 5)      # 50 ms steps
    score = np.zeros(len(shifts))
    for q, sh in enumerate(shifts):
        j = idx + int(sh)
        ok = (j >= 0) & (j < n)
        score[q] = rise[j[ok]].sum() / max(1, ok.sum())
    # local maxima, best first, at least a second apart
    order = np.argsort(-score)
    cands = []
    for q in order:
        D = shifts[q] / 100.0
        if all(abs(D - c) >= 1.0 for c in cands):
            cands.append(D)
        if len(cands) >= 5:
            break
    if all(abs(c) > 0.5 for c in cands):
        cands.append(0.0)
    best = max(cands, key=fit)
    return float(best)


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0.0


def align(stem_dir, lines, verbose=False):
    import torchaudio
    bundle = torchaudio.pipelines.MMS_FA
    model = bundle.get_model(with_star=True)
    tokenizer = bundle.get_tokenizer()
    aligner = bundle.get_aligner()
    wave = decode(Path(stem_dir) / 'vocals.opus')
    total = len(wave) / SR
    t0 = time.time()
    em = emissions(model, wave)
    rises = voice_rises(wave)
    try:
        env = json.loads((Path(stem_dir) / 'env.json').read_text(encoding='utf-8'))
        rms, hz = env.get('rms') or [], env.get('hz') or 20
    except Exception:
        rms, hz = [], 20

    # what each LRC line asks the aligner to find
    items = []
    for i, L in enumerate(lines):
        words = L['line'].split()
        if not words:
            continue
        nxt = lines[i + 1]['t'] if i + 1 < len(lines) else total
        toks = [norm_word(w) for w in words]
        items.append({'i': i, 't': L['t'], 'nxt': max(L['t'] + 1.0, min(nxt, L['t'] + MAX_LINE)),
                      'words': words, 'toks': toks, 'keep': [k for k, t in enumerate(toks) if t]})

    for it in items:
        # the model's words for a line, and which screen word each belongs to
        # ("twenty four" is two words to the model, one on the screen)
        it['flat'], it['owner'] = [], []
        for k in it['keep']:
            for piece in it['toks'][k].split():
                it['flat'].append(piece); it['owner'].append(k)

    # pass zero: the file's clock, coarsely, before anything is windowed.
    # Pass one only looks WIDE seconds either side, and some LRC files are out
    # by more: the user had nudged Come Undone +5 s and To The Shore +10.6 s by
    # hand, and version 2 found neither. Candidates come from lining the LRC's
    # line starts up with where the voice starts, over +-GLOBAL seconds; music
    # repeats every bar, so that alone can land a bar out, and the aligner
    # judges between the candidates by how well the words fit.
    def fit(D):
        """mean letter score of a spread of lines aligned D seconds from the LRC"""
        pick = items[::max(1, len(items) // 16)][:16]
        sc = []
        for it in pick:
            if not it['flat']:
                continue
            res = align_window(aligner, tokenizer, em, max(0.0, it['t'] + D - 1.0),
                               min(total, it['nxt'] + D + 1.0), [it['flat']])
            if res and res[0]:
                us = [u for w in res[0] for u in w]
                sc.append(sum(u[2] for u in us) / len(us))
        return sum(sc) / len(sc) if sc else -1

    G = global_offset(wave, [it['t'] for it in items if it['flat']], fit)

    def passes(G):
        """Passes one and two, the tidying and the verdicts, with the file's
        clock taken as G seconds out."""
        # pass one: each line alone in a wide window around the coarse clock, only
        # to learn where the file's clock sits line by line
        raw = []
        for it in items:
            a, b = max(0.0, it['t'] + G - WIDE), min(total, it['nxt'] + G + WIDE)
            res = align_window(aligner, tokenizer, em, a, b, [it['flat']]) if it['flat'] else None
            spans = res[0] if res else None
            it['p1'] = None
            if spans:
                first, last = spans[0][0][0], spans[-1][-1][1]
                clamped = first - a < EDGE or b - last < EDGE
                raw.append(None if clamped else first - it['t'])
                if not clamped:
                    it['p1'] = first
            else:
                raw.append(None)
        # A line whose words also occur in a line near it (the same chorus line
        # twice, "They can't hurt you now" then "Can't hurt you now") can land on
        # its twin in the wide window, so it says nothing about the file's clock.
        # Because the Night's run of repeats dragged the drift 1.5 s early for
        # every doubted line in it, against a song offset of +0.3 s.
        for j, it in enumerate(items):
            me = ' '.join(it['flat'])
            for m in range(max(0, j - 3), min(len(items), j + 4)):
                if m == j or abs(items[m]['t'] - it['t']) > 2 * WIDE + (it['nxt'] - it['t']):
                    continue
                other = ' '.join(items[m]['flat'])
                if me and other and (me in other or other in me):
                    raw[j] = None
                    break
        known = [d for d in raw if d is not None]
        offset = _median(known) if known else G
        for j, it in enumerate(items):
            near = [d for d in raw[max(0, j - 4):j + 5] if d is not None]
            it['d'] = _median(near) if len(near) >= 3 else offset

        # pass two: each line with its neighbours either side, in a window around
        # where they really are. The neighbours' own words are in the transcript,
        # so two lines can no longer claim the same audio (Dancing Queen: "jive"
        # and "Having" both at 25.72 s when each line was aligned alone). Every
        # line is seen three times -- as the middle of its own block and as a
        # neighbour in the blocks either side -- and keeps its own block's result.
        seen = [[] for _ in items]            # first-word times of each line, from every block it was in
        final = [None] * len(items)
        for j, it in enumerate(items):
            blk = [m for m in (j - 1, j, j + 1) if 0 <= m < len(items)]
            a = max(0.0, items[blk[0]]['t'] + it['d'] - PAD)
            b = min(total, items[blk[-1]]['nxt'] + it['d'] + PAD)
            res = align_window(aligner, tokenizer, em, a, b, [items[m]['flat'] for m in blk])
            if not res:
                continue
            for m, spans in zip(blk, res):
                if spans:
                    seen[m].append(spans[0][0][0])
                    if m == j:
                        final[j] = spans

        out = []
        for j, it in enumerate(items):
            entry = {'i': it['i'], 't': round(it['t'], 2), 'd': round(it['d'], 3), 'text': ' '.join(it['words']),
                     'c': 0.0, 'words': [], 'p1': None if it['p1'] is None else round(it['p1'], 3),
                     'seen': [round(x, 3) for x in seen[j]]}
            spans = final[j]
            sn = seen[j]
            entry['stable'] = len(sn) >= 2 and max(sn) - min(sn) <= STABLE
            entry['how'] = 'block'
            if not entry['stable'] and it['flat']:
                # The blocks disagree. Three placements are tried and the model's
                # likelihood picks: the line's own block; the line alone in a tight
                # window around where its neighbours' drift says it is; and alone
                # in a tight window around the LRC's own time, which is right more
                # often than the drift for a line the file got right and its
                # neighbours did not (version 1's window, with the star). Same text
                # in each, so the scores compare. The voice starts in the stem are
                # deliberately NOT used to choose: tools/stem_eval.py judges by them.
                def score(sp):
                    us = [u for w in sp for u in w]
                    return sum(u[2] for u in us) / len(us) if us else -1
                cands = [(score(spans), 'block', spans)] if spans else []
                for how, tc in (('near', it['t'] + it['d']), ('lrc', it['t'])):
                    a = max(0.0, tc - NEAR[0])
                    b = min(total, it['nxt'] + (tc - it['t']) + NEAR[1])
                    res = align_window(aligner, tokenizer, em, a, b, [it['flat']])
                    if res and res[0]:
                        cands.append((score(res[0]), how, res[0]))
                # version 1's tight star-less window, but around the corrected time:
                # over all 325 songs it had the smallest tail on the lines the blocks
                # disagree on. Kept beside the choice, for the screen to pick by rule.
                a = max(0.0, it['t'] + it['d'] - NEAR[0])
                b = min(total, it['nxt'] + it['d'] + NEAR[1])
                res = align_window(aligner, tokenizer, em, a, b, [it['flat']], star=False)
                if res and res[0]:
                    alt = {}
                    for k, span in zip(it['owner'], res[0]):
                        alt.setdefault(k, span[0][0])
                    last = a
                    entry['alt'] = []
                    for k in range(len(it['words'])):
                        last = alt.get(k, last)
                        entry['alt'].append(round(last, 3))
                if cands:
                    # a placement far from where the neighbours' drift puts the line
                    # was wrong more often than not (17 of 32 over half a second out,
                    # 45 songs); one near it wins when there is one
                    est = it['t'] + it['d']
                    close = [c for c in cands if abs(c[2][0][0][0] - est) <= 1.0]
                    best = max(close or cands, key=lambda c: c[0])
                    spans, entry['how'] = best[2], best[1]
            if not spans:
                entry['ok'] = False
                out.append(entry); continue
            per = {}
            for k, span in zip(it['owner'], spans):
                per.setdefault(k, []).extend(span)
            placed = {}
            for k, span in per.items():
                chars = it['toks'][k].replace(' ', '')
                placed[k] = {'w': it['words'][k], 's': round(span[0][0], 3), 'e': round(span[-1][1], 3),
                             'c': round(sum(u[2] for u in span) / len(span), 3),
                             'u': [{'k': 'char', 'g': ch, 's': round(u[0], 3), 'e': round(u[1], 3)}
                                   for u, ch in zip(span, chars)]}
            ks = sorted(placed)
            last_e = placed[ks[0]]['s']
            for k in range(len(it['words'])):
                if k in placed:
                    entry['words'].append(placed[k]); last_e = placed[k]['e']
                else:
                    entry['words'].append({'w': it['words'][k], 's': round(last_e, 3), 'e': round(last_e, 3),
                                           'c': 0.0, 'u': []})
            entry['c'] = round(sum(placed[k]['c'] for k in ks) / len(ks), 3)
            out.append(entry)
            if verbose:
                print('  %3d %6.2f d=%+.2f seen=%s  %s' % (
                    it['i'], it['t'], it['d'], ','.join('%.2f' % x for x in seen[j]),
                    ' '.join('%s@%.2f' % (w['w'], w['s']) for w in entry['words'][:6])))

        # Word starts, tidied against the stem itself.
        #  - A first letter far ahead of the rest of its word is a stray (the model
        #    put a "t" on a breath a second early: Waterloo, "The" at 20.16 with
        #    "h" at 20.54 and "e" at 21.26, sung at 21.2); the word starts where
        #    its letters gather.
        #  - Where the voice comes out of silence just before a word, the word
        #    starts there. The star before a line soaks up a vowel's attack, so a
        #    line opening "I" or "Ah" landed at the vowel's peak, 0.2-0.3 s after
        #    the singer began (lines v1 and v2 both trusted, 45 songs).
        # Crossings are judged on the aligner's own placements, before the tidying
        # below moves anything (a snap used to pull a line's first word back over
        # the previous line's last one, and both were then doubted). When one of
        # the pair is stable, it is the other that is wrong.
        crossed = set()
        for n in range(len(out) - 1):
            a_ws = [w for w in out[n]['words'] if w['c'] > 0]
            b_ws = [w for w in out[n + 1]['words'] if w['c'] > 0]
            if a_ws and b_ws and a_ws[-1]['s'] >= b_ws[0]['s']:
                sa, sb = out[n].get('stable'), out[n + 1].get('stable')
                if sa and not sb:
                    crossed.add(n + 1)
                elif sb and not sa:
                    crossed.add(n)
                else:
                    crossed.update((n, n + 1))

        import bisect
        prev_line_end = -1e9
        for L in out:
            ws = [w for w in L['words'] if w['c'] > 0]
            for m, w in enumerate(ws):
                u = w['u']
                q = 0                                  # skip leading strays, one after another
                while q + 1 < len(u) and u[q + 1]['s'] - u[q]['e'] > STRAY:
                    q += 1
                if q:
                    # a stray only if the voice actually stops and starts again after
                    # it; a held first syllable spreads its letters too (Fix You's
                    # "When", right at 72.20 and pushed to 72.66 by a letter rule alone)
                    r = bisect.bisect_right(rises, u[q]['s'] + SNAP[1]) - 1
                    if r >= 0 and rises[r] > u[0]['e']:
                        w['s'] = round(rises[r], 3)
                prev_end = ws[m - 1]['e'] if m else prev_line_end
                k = bisect.bisect_right(rises, w['s'] + SNAP[1]) - 1
                if k >= 0 and w['s'] - SNAP[0] <= rises[k] and rises[k] > prev_end:
                    w['s'] = round(min(w['s'], rises[k]), 3)
            if ws:
                prev_line_end = ws[-1]['e']

        # a word lasts while the voice under it lasts, up to the next word; the
        # next line's first word bounds the last word of this one
        starts = [next((w['s'] for w in L['words'] if w['c'] > 0), None) for L in out]
        for n, L in enumerate(out):
            ws = [w for w in L['words'] if w['c'] > 0]
            nxt_line = next((s for s in starts[n + 1:] if s is not None), total)
            for m, w in enumerate(ws):
                limit = ws[m + 1]['s'] - 0.02 if m + 1 < len(ws) else min(nxt_line - 0.1, w['e'] + 4.0)
                w['ec'] = w['e']
                w['e'] = round(voice_end(rms, hz, w['e'], limit), 3)

        # Which lines to trust. The model's probabilities are no guide on singing
        # (with the star token a line placed within 0.1 s can score 0.02), so the
        # test is STABILITY: the line was aligned in up to three blocks, with
        # different neighbours and window edges; when they put its first word in
        # the same place, the audio decided it, not the window. Also doubted: a
        # line that crosses its neighbour (lines are sung in order -- a chorus
        # word dragged onto the next line's audio, Lyin' Eyes line 43), one far
        # from where its neighbours say it is, and one packed or stretched beyond
        # what singing does.
        cs = sorted(L['c'] for L in out if L['words'] and L['c'] > 0)
        med = cs[len(cs) // 2] if cs else 0
        for n, L in enumerate(out):
            if 'ok' in L:
                continue
            ws = [w for w in L['words'] if w['c'] > 0]
            sane = bool(ws) and n not in crossed
            if sane:
                first, last = ws[0]['s'], ws[-1]['ec']
                sane = abs(first - (L['t'] + L['d'])) <= 2.0 and 0.08 <= (last - first) / len(ws) <= 3.0
            L['ok'] = sane and L['stable']
            # a sane line from the tight fallback window: better than spreading
            # the words by letters, not as sure as a stable one
            L['fair'] = sane and not L['stable']
        return out, offset, med

    out, offset, med = passes(G)
    if abs(G) > WIDE:
        # A shift beyond pass one's reach is only taken if it makes more of
        # the song trustworthy than no shift does: repetitive music can fit
        # the words a few bars out (Lyle Lovett, My Baby Don't Tolerate,
        # -5.2 s with 6 of 26 lines trusted). Come Undone, 11 s out by the
        # voice and the discs alike, passes easily.
        trusted = lambda o: sum(1 for L in o if L.get('ok'))
        alt = passes(0.0)
        if trusted(alt[0]) >= trusted(out):
            out, offset, med = alt
            G = 0.0

    return {'model': 'MMS_FA', 'version': 3, 'sr': SR, 'duration': round(total, 2), 'offset': round(offset, 3),
            'coarse': round(G, 2),
            'made': time.time(), 'seconds': round(time.time() - t0, 1), 'median_c': round(med, 3), 'lines': out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', required=True, help='stem folder holding vocals.opus')
    ap.add_argument('--skey', help='song key; default: the [key] in the folder name')
    ap.add_argument('--print', action='store_true')
    ap.add_argument('--out', default='words.json', help='file name to write beside the stems')
    a = ap.parse_args()
    d = Path(a.dir)
    skey = a.skey or re.search(r'\[([0-9a-f]+)\]', d.name).group(1)
    lines = lrc_lines(skey)
    if not lines:
        print('no time-synced lyrics for', skey); return 2
    res = align(d, lines, verbose=a.print)
    dest = d / a.out
    # the first version's result is kept once, for comparison
    if a.out == 'words.json' and dest.exists() and not (d / 'words.v1.json').exists():
        try:
            if json.loads(dest.read_text(encoding='utf-8')).get('version', 1) == 1:
                dest.replace(d / 'words.v1.json')
        except Exception:
            pass
    dest.write_text(json.dumps(res, separators=(',', ':')), encoding='utf-8')
    good = sum(1 for L in res['lines'] if L.get('ok'))
    print('aligned %d lines (%d confident, offset %+.2fs) in %.0fs -> %s' % (
        len(res['lines']), good, res['offset'], res['seconds'], dest))
    return 0


if __name__ == '__main__':
    sys.exit(main())
