"""Offline analysis of a separated vocal stem, for the karaoke screen.

The karaoke visuals have to move on real data or not at all. A room can HEAR
the song, so anything that claims to follow the music but does not is spotted
within seconds, and it discredits the parts that are honest.

Live analysis is not available: playback is frequently sent to a Sonos speaker,
so the browser often has no audio signal at all, and the television has no
WebGL to spare either way. But the vocal stem is a FILE sitting on disk, so it
can be analysed once, here, and shipped as a few kilobytes of numbers that the
screen reads by playback position. That works identically on every device and
every output.

What comes out:

    rms         loudness of the voice, 20 per second, 0-255
    onsets      where energy rises after a dip -- word attacks. Word timings
                estimated by dividing a line proportionally are always slightly
                wrong, and a singer feels every error; snapping them to real
                attacks is a poor substitute for forced alignment that costs
                nothing extra.
    sustains    held notes: energy high and steady for over half a second.
                These are the moments people actually perform, and line-level
                lyrics render them as dead air.
    climax      the loudest sustained window, almost always the last chorus.
                Knowing it in advance is what allows the screen to hold
                something back for it.

Everything is derived from the vocal stem only. The mix is never touched.
"""
import io, sys, json, wave, math, array
from pathlib import Path

HOP_HZ = 20          # 50 ms -- fine enough for word attacks, small enough to ship
SUSTAIN_MIN = 0.5    # seconds held before it counts as a held note
SUSTAIN_LEVEL = 0.55 # fraction of the song's own peak
CLIMAX_WIN = 20.0    # seconds


def read_mono(path):
    """Mono float samples and the sample rate, without pulling in numpy."""
    with wave.open(str(path), 'rb') as w:
        if w.getsampwidth() != 2:
            raise SystemExit('expected 16-bit PCM, got %d bytes' % w.getsampwidth())
        ch, sr, n = w.getnchannels(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    a = array.array('h')
    a.frombytes(raw)
    if ch == 1:
        return a, sr
    mono = array.array('h', bytes(len(a) // ch * 2))
    for i in range(len(mono)):
        mono[i] = (a[i * ch] + a[i * ch + 1]) // 2
    return mono, sr


def envelope(samples, sr):
    hop = max(1, sr // HOP_HZ)
    out = []
    for i in range(0, len(samples) - hop, hop):
        acc = 0
        for j in range(i, i + hop, 8):        # every 8th sample is plenty at 50 ms
            v = samples[j]
            acc += v * v
        out.append(math.sqrt(acc / (hop / 8.0)))
    peak = max(out) or 1.0
    return [min(255, int(v / peak * 255)) for v in out], peak


def find_onsets(env, floor=28, rise=26):
    """Energy climbing through a threshold after a dip -- a word attack."""
    on, armed = [], True
    for i in range(1, len(env)):
        if env[i] < floor:
            armed = True
        elif armed and env[i] - env[i - 1] >= rise:
            on.append(round(i / float(HOP_HZ), 2))
            armed = False
    return on


def find_sustains(env):
    """Runs where the voice stays loud and steady -- the held notes."""
    thr = SUSTAIN_LEVEL * 255
    need = int(SUSTAIN_MIN * HOP_HZ)
    out, i, n = [], 0, len(env)
    while i < n:
        if env[i] < thr:
            i += 1
            continue
        j = i
        while j < n and env[j] >= thr:
            j += 1
        if j - i >= need:
            seg = env[i:j]
            mean = sum(seg) / float(len(seg))
            var = sum((v - mean) ** 2 for v in seg) / len(seg)
            if var < 2400:                      # steady, not a burst of syllables
                out.append([round(i / float(HOP_HZ), 2),
                            round(j / float(HOP_HZ), 2), int(mean)])
        i = j
    return out


def find_climax(env):
    """The loudest sustained window: where the screen should spend its budget."""
    w = int(CLIMAX_WIN * HOP_HZ)
    if len(env) <= w:
        return [0.0, round(len(env) / float(HOP_HZ), 2)]
    run = sum(env[:w])
    best, at = run, 0
    for i in range(1, len(env) - w):
        run += env[i + w - 1] - env[i - 1]
        if run > best:
            best, at = run, i
    return [round(at / float(HOP_HZ), 2), round((at + w) / float(HOP_HZ), 2)]


def analyse(path):
    samples, sr = read_mono(path)
    env, _peak = envelope(samples, sr)
    dur = len(samples) / float(sr)
    loud = [v for v in env if v > 20]
    return {
        'hz': HOP_HZ,
        'duration': round(dur, 2),
        'rms': env,
        'onsets': find_onsets(env),
        'sustains': find_sustains(env),
        'climax': find_climax(env),
        # a ballad has a wide quiet-to-loud spread, an anthem is compressed;
        # this is what picks the screen's preset before a note is played
        'dynamics': round((max(env) - (sum(loud) / len(loud) if loud else 0)) / 255.0, 3),
        'voiced': round(len(loud) / float(len(env) or 1), 3),
    }


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('usage: envelope.py <vocals.wav> [out.json]')
    src = Path(sys.argv[1])
    data = analyse(src)
    blob = json.dumps(data, separators=(',', ':'))
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix('.env.json')
    io.open(dst, 'w', encoding='utf-8').write(blob)
    print('%s' % src.name)
    print('  duration      %.1f s' % data['duration'])
    print('  rms points    %d at %d Hz' % (len(data['rms']), data['hz']))
    print('  onsets        %d word attacks' % len(data['onsets']))
    print('  sustains      %d held notes' % len(data['sustains']))
    print('  climax        %.1f s to %.1f s' % (data['climax'][0], data['climax'][1]))
    print('  dynamics      %.3f   voiced %.0f%% of the song'
          % (data['dynamics'], data['voiced'] * 100))
    print('  shipped size  %.1f KB' % (len(blob) / 1024.0))
