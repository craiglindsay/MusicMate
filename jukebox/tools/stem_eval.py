"""Score line starts against the split voice itself, with no outside reference.

Where the voice comes back after a silence, the vocal stem says exactly when
the singing starts: the level jumps out of the noise floor. That moment is
the one piece of timing a stem gives unambiguously, so it is the test here.
For every LRC line that begins such a phrase (the stem quiet for 0.4 s or
more just before it), each timing source's first-word time is compared with
the nearest voice start within 1.5 s:

    lrc   the LRC line time as published
    v1    words.v1.json (or words.json when it is still version 1)
    v2    words.json when it is version 2 (or --v2 <file name>)

Every source is placed as the karaoke screen would place it (align_eval's
screen_starts), minus the reading lead, so it is the SUNG time being tested.

The aligners read the same stem, so this is not independent of them; but it
reads it differently (loudness, not a speech model), and it cannot be fooled
by a cover singer's phrasing the way the disc references can.

    python jukebox/tools/stem_eval.py
    python jukebox/tools/stem_eval.py --v2 words.v2test.json --only-v2
"""
import re, sys, json, sqlite3, argparse, subprocess
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import align_eval as E

HERE = Path(__file__).resolve().parent.parent
CACHE = HERE / 'cache' / 'voicestarts'
SR, HOP = 16000, 160                        # 10 ms frames
QUIET, NEAR = 0.4, 1.5
UNANIMOUS = False        # --unanimous: only lines where every source points at the same voice start


def voice_starts(d):
    """Times the voice starts after at least QUIET seconds below its floor."""
    CACHE.mkdir(parents=True, exist_ok=True)
    key = re.search(r'\[([0-9a-f]+)\]$', d.name).group(1)
    cf = CACHE / f'{key}.json'
    if cf.exists():
        return json.loads(cf.read_text())
    p = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(d / 'vocals.opus'), '-f', 'f32le', '-ac', '1',
                        '-ar', str(SR), '-'], capture_output=True, check=True)
    x = np.frombuffer(p.stdout, dtype=np.float32)
    n = len(x) // HOP
    rms = np.sqrt((x[:n * HOP].reshape(n, HOP) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    loud = np.percentile(db, 95)
    on = db > loud - 24                     # singing
    off = db < loud - 34                    # silence (with a dead band between, no chatter)
    out, quiet = [], 0
    for i in range(n):
        if off[i]:
            quiet += 1
        elif on[i]:
            if quiet * HOP / SR >= QUIET:
                # step back to where the rise began: the first frame above the silence line
                j = i
                while j > 0 and not off[j - 1]:
                    j -= 1
                out.append(round(j * HOP / SR, 3))
            quiet = 0
    cf.write_text(json.dumps(out))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--v2', default=None)
    ap.add_argument('--only-v2', action='store_true', help='only songs that have a version-2 result')
    ap.add_argument('--fair', action='store_true')
    ap.add_argument('--alt', default='none', choices=('none', 'doubt', 'all'))
    ap.add_argument('--songs-with', help='only songs that have this file beside the stems (same-set comparisons)')
    ap.add_argument('--unanimous', action='store_true',
                    help='score only lines every source ties to the same voice start (no vote, so no bias between sources)')
    a = ap.parse_args()
    E.TRUST_FAIR = a.fair
    E.ALT = a.alt
    global UNANIMOUS
    UNANIMOUS = a.unanimous
    c = sqlite3.connect(f'file:{E.LYRICS_DB}?mode=ro', uri=True)
    errs = {'lrc': [], 'v1': [], 'v2': []}
    songs = 0
    for d in sorted(E.STEMS.iterdir()):
        m = re.search(r'\[([0-9a-f]+)\]$', d.name)
        if not m or not (d / 'vocals.opus').exists():
            continue
        W = {}
        for name in ('words.v1.json', 'words.json') + ((a.v2,) if a.v2 else ()):
            p = d / name
            if p.exists():
                j = json.loads(p.read_text(encoding='utf-8'))
                if name == a.v2:
                    W['v2'] = j
                else:
                    W.setdefault('v%d' % j.get('version', 1), j)
        if a.songs_with and not (d / a.songs_with).exists():
            continue
        if a.only_v2 and ('v2' not in W or (a.v2 and not (d / a.v2).exists())):
            continue
        lines = E.lrc(c, m.group(1))
        if not lines or not W:
            continue
        vs = np.array(voice_starts(d))
        if not len(vs):
            continue
        songs += 1
        dur = (W.get('v1') or W.get('v2'))['duration']
        systems = {'lrc': {(i, 0): t for i, (t, txt) in enumerate(lines) if txt.split()}}
        for v in ('v1', 'v2'):
            if v in W:
                S = E.screen_starts(lines, W[v], dur)
                systems[v] = {k: s for k, s in S.items() if k[1] == 0}
        # a line is tested only if it begins a phrase by EVERY source's account:
        # some voice start lies within NEAR of each source's placement
        keys = set.intersection(*[set(S) for S in systems.values()])
        for k in keys:
            near = {name: vs[np.argmin(abs(vs - S[k]))] for name, S in systems.items()}
            if any(abs(near[n] - systems[n][k]) > NEAR for n in near):
                continue
            # the phrase start they are all being compared with: the one most
            # sources point at (they usually agree; when not, the median source's)
            if UNANIMOUS and len(set(near.values())) > 1:
                continue                     # the sources point at different phrase starts
            target = sorted(near.values())[len(near) // 2]
            for name, S in systems.items():
                errs[name].append(S[k] - target)
    print('songs:', songs)
    for name in ('lrc', 'v1', 'v2'):
        if errs[name]:
            print('%-4s phrase starts  %s' % (name, E.summary(errs[name])))


if __name__ == '__main__':
    sys.exit(main())
