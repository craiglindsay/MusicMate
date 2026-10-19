"""Offline analysis of a library track's full mix, for the backdrops.

The backdrops behind Now Playing were built to react to GL.energy, which is a
WebAudio analyser on the page's own <audio> element. On the television that
signal does not exist: the music is nearly always cast to a Sonos room, so the
browser has nothing to listen to, and inside the Android TV WebView the
analyser yields nothing even when it does play locally (user, 17 Oct 2026:
"Pulse seems to be a one time thing ... grain just scrolls, with no change in
direction or speed based on the music playing or not playing"). Karaoke solved
the same problem a month earlier by analysing its vocal stem once, offline
(envelope.py), and reading the numbers by playback position. This is the
equivalent for the whole mix: a few kilobytes per song, keyed by skey so it
survives a read-model rebuild, computed on first request and cached under
jukebox/cache/feat/.

What comes out, all at FPS frames a second unless said otherwise, each stream
quantised to a byte and shipped base64:

    loud      perceptual loudness (A-weighted power in dB, normalised to the
              track's own loud passages): what "how loud is it right now" means
    low/mid/high
              20-160 Hz, 160-2000 Hz, 2000-5500 Hz, each in dB against its own
              range, so a quiet record's bass still moves its visual
    flux      onset strength (half-wave rectified change of a log-mel
              spectrum): the attacks, continuous
    onsets    the flux peaks, in centiseconds: discrete attacks for impulses
    beats     a beat grid in centiseconds from dynamic-programming beat
              tracking (Ellis 2007) at the estimated tempo, with `bpm` and a
              `beatconf` 0..1 that the client MUST read: a ballad, a live
              recording or spoken word gets a grid too, but a weak one, and
              the honest thing then is to move on onsets and loudness instead
    bar       4/4 is assumed; `down` is the beat index (0-3) that behaves most
              like beat one, `downconf` how clearly. Enough for a slightly
              stronger accent, never for anything that would look wrong
    sections  boundaries from a novelty curve over a self-similarity matrix of
              the log-mel spectrum, each with the mean loudness of the section
              that follows: where the song changes shape
    arc       loudness smoothed over about six seconds, one a second: the
              song's energy over its length, for calm/excited moods
    spec      the log-mel spectrum (30-5400 Hz) in SPEC_N bands (the 40 analysis
              bands paired) at SPEC_HZ, row-major (`specn` bands a row), each band in dB against its own loud
              passages blended with the song's overall level, so every band
              moves but a bass-heavy record still looks bass-heavy: what the
              Ridges backdrop draws its lines from
    climax    the loudest sustained window, usually the last chorus

Decoding is ffmpeg to 11 kHz mono (the top octave of a lossy library is dead
and nothing here needs it); everything else is numpy. About a second a track
on the server machine, measured in the HANDOFF.
"""
import io, os, sys, json, math, base64, time, subprocess
from pathlib import Path

import numpy as np

VERSION = 2               # 2 (18 Oct 2026): + spec, for the Ridges backdrop
SR = 11000               # Hz; HOP divides it exactly so the frame rate is a round number
HOP = 110                # 100 analysis frames a second: onsets and beats need the resolution
FRAME = 1024             # 93 ms window, 10.7 Hz bins: enough for a 20-160 Hz bass band
FPS = 20                 # shipped rate; five analysis frames averaged into one
N_MEL = 40
F_LO, F_HI = 30.0, 5400.0
BANDS = [(20.0, 160.0), (160.0, 2000.0), (2000.0, 5500.0)]
SPEC_HZ = 10              # the spectrum ships at half the rate: a line every tenth of a second
SPEC_N = 20               # and in half the bands: 45 KB a song uncompressed (40 bands was 113 KB gzipped)


# ---------------------------------------------------------------- decode
def decode(path, sr=SR):
    """Mono float32 samples in -1..1 at sr, through ffmpeg."""
    cmd = ['ffmpeg', '-v', 'error', '-nostdin', '-i', str(path), '-vn', '-ac', '1', '-ar', str(sr),
           '-f', 's16le', '-']
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    if r.returncode != 0 or len(r.stdout) < sr:
        raise RuntimeError('decode failed: ' + (r.stderr.decode('utf-8', 'replace')[-200:] or 'no audio'))
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------- spectra
def _hz_to_mel(f):
    return 2595.0 * np.log10(1.0 + f / 700.0)


def _mel_to_hz(m):
    return 700.0 * (10.0 ** (m / 2595.0) - 1.0)


def _mel_filters(n_fft, sr, n_mel, f_lo, f_hi):
    n_bins = n_fft // 2 + 1
    freqs = np.linspace(0, sr / 2.0, n_bins)
    pts = _mel_to_hz(np.linspace(_hz_to_mel(f_lo), _hz_to_mel(f_hi), n_mel + 2))
    fb = np.zeros((n_mel, n_bins), dtype=np.float32)
    for i in range(n_mel):
        lo, c, hi = pts[i], pts[i + 1], pts[i + 2]
        up = (freqs - lo) / max(c - lo, 1e-6)
        down = (hi - freqs) / max(hi - c, 1e-6)
        fb[i] = np.clip(np.minimum(up, down), 0, 1)
    return fb


def _a_weight(freqs):
    """IEC A-weighting as a power gain, so loudness favours what a room hears."""
    f2 = np.maximum(freqs, 1.0) ** 2
    num = (12194.0 ** 2) * f2 * f2
    den = (f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194.0 ** 2)
    ra = num / den
    db = 20 * np.log10(np.maximum(ra, 1e-12)) + 2.0
    return (10.0 ** (db / 10.0)).astype(np.float32)


_MEL = _mel_filters(FRAME, SR, N_MEL, F_LO, F_HI)
_FREQS = np.linspace(0, SR / 2.0, FRAME // 2 + 1)
_AW = _a_weight(_FREQS)
_WIN = np.hanning(FRAME).astype(np.float32)
_BAND_IDX = [np.where((_FREQS >= lo) & (_FREQS < hi))[0] for lo, hi in BANDS]


def spectra(x):
    """Per 100 Hz frame: A-weighted power, three band powers, log-mel row.
    Chunked so a nine-minute track does not need a half-gigabyte of complex
    spectrum at once."""
    n = 1 + max(0, (len(x) - FRAME) // HOP)
    pad = np.concatenate([np.zeros(FRAME // 2, np.float32), x, np.zeros(FRAME, np.float32)])
    loud = np.empty(n, np.float32)
    bands = np.empty((n, 3), np.float32)
    mel = np.empty((n, N_MEL), np.float32)
    step = 4000
    for s in range(0, n, step):
        e = min(n, s + step)
        idx = np.arange(s, e)[:, None] * HOP + np.arange(FRAME)[None, :]
        fr = pad[idx] * _WIN
        p = np.abs(np.fft.rfft(fr, axis=1)) ** 2
        loud[s:e] = p @ _AW
        for b in range(3):
            bands[s:e, b] = p[:, _BAND_IDX[b]].sum(axis=1)
        mel[s:e] = np.log10(p @ _MEL.T + 1e-7)
    return loud, bands, mel


# ---------------------------------------------------------------- helpers
def _db_norm(p, floor_db, top_pct=99.0):
    """Power -> 0..1 on a dB scale whose top is this track's own loud passages."""
    db = 10 * np.log10(np.maximum(p, 1e-10))
    top = np.percentile(db, top_pct)
    return np.clip((db - (top - floor_db)) / floor_db, 0, 1)


def _pool(v, k, how='mean'):
    """k consecutive analysis frames -> one shipped frame."""
    n = len(v) // k
    a = v[:n * k].reshape(n, k)
    return a.max(axis=1) if how == 'max' else a.mean(axis=1)


def _bytes(v):
    return base64.b64encode(np.clip(np.round(v * 255), 0, 255).astype(np.uint8).tobytes()).decode('ascii')


def _smooth(v, n):
    if n <= 1:
        return v
    k = np.hanning(n + 2)[1:-1]
    k /= k.sum()
    return np.convolve(v, k, mode='same')


def _peaks(v, min_gap, thr):
    """Local maxima above thr, at least min_gap frames apart (greedy by height)."""
    cand = np.where((v[1:-1] > v[:-2]) & (v[1:-1] >= v[2:]) & (v[1:-1] > thr))[0] + 1
    order = cand[np.argsort(-v[cand])]
    taken = np.zeros(len(v), bool)
    out = []
    for i in order:
        if taken[max(0, i - min_gap):i + min_gap + 1].any():
            continue
        taken[i] = True
        out.append(int(i))
    return sorted(out)


# ---------------------------------------------------------------- onsets and beats
def onset_strength(mel):
    """Half-wave rectified spectral flux of the log-mel spectrum, 100 Hz,
    normalised so its loud attacks are about 1."""
    d = np.diff(mel, axis=0, prepend=mel[:1])
    ose = np.maximum(d, 0).sum(axis=1)
    ose = ose - _smooth(ose, 150)                      # take out the slow level
    ose = np.maximum(ose, 0)
    ref = np.percentile(ose, 99.0)
    return ose / ref if ref > 0 else ose


def tempo(ose, hz=SR / HOP):
    """Autocorrelation of the onset strength with a log-Gaussian prior around
    110 bpm (Ellis), peak refined by parabola. Returns (bpm, strength 0..1)."""
    v = ose - ose.mean()
    n = len(v)
    lo, hi = int(hz * 60 / 220), int(hz * 60 / 40)   # 220 .. 40 bpm
    if n < hi * 4:
        return 0.0, 0.0
    f = np.fft.rfft(v, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:hi + 2]
    ac /= max(ac[0], 1e-9)
    lags = np.arange(hi + 2, dtype=np.float64)
    bpm = 60.0 * hz / np.maximum(lags, 1)
    prior = np.exp(-0.5 * ((np.log2(np.maximum(bpm, 1) / 110.0)) / 0.9) ** 2)
    w = ac * prior
    w[:lo] = 0
    # a lag only counts with its double (the half tempo) in agreement a little,
    # which breaks ties between a tempo and its octave in favour of the one
    # whose beats also line up two at a time
    comb = w.copy()
    for lag in range(lo, hi):
        if 2 * lag < len(w):
            comb[lag] += 0.5 * w[2 * lag]
    i = int(np.argmax(comb[lo:hi])) + lo
    if 0 < i < len(ac) - 1:
        a, b, c = ac[i - 1], ac[i], ac[i + 1]
        den = a - 2 * b + c
        i = i + (0.5 * (a - c) / den if den != 0 else 0)
    strength = float(np.clip(ac[int(round(i))], 0, 1))
    return float(60.0 * hz / i), strength


def track_beats(ose, bpm, hz=SR / HOP, tightness=400.0):
    """Dynamic-programming beat tracking (Ellis 2007): the beat sequence that
    maximises onset strength at beats while keeping intervals near the period.
    Returns beat frame indices."""
    if bpm <= 0:
        return np.zeros(0, int)
    period = hz * 60.0 / bpm
    n = len(ose)
    lo, hi = int(round(period * 0.5)), int(round(period * 2.0))
    if n <= hi:
        return np.zeros(0, int)
    score = np.full(n, -1e9)
    back = np.full(n, -1, int)
    win = np.arange(-hi, -lo + 1)
    pen = -tightness * (np.log(-win / period)) ** 2
    score[:hi] = ose[:hi]
    for t in range(hi, n):
        cand = score[t + win] + pen
        k = int(np.argmax(cand))
        score[t] = ose[t] + cand[k]
        back[t] = t + win[k]
    # the best end point: the top of the last period, then walk back
    tail = score[n - hi:]
    end = n - hi + int(np.argmax(tail))
    beats = []
    t = end
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats.reverse()
    return np.array(beats, int)


def beat_confidence(ose, beats, strength):
    """How much the beats mean: onset strength on the grid against off it,
    the autocorrelation strength, and the steadiness of the intervals."""
    if len(beats) < 8:
        return 0.0
    on = ose[beats].mean()
    off_idx = np.clip(((beats[:-1] + beats[1:]) // 2), 0, len(ose) - 1)
    off = ose[off_idx].mean()
    contrast = (on - off) / max(on + off, 1e-6)        # -1..1
    iv = np.diff(beats).astype(np.float64)
    steadiness = 1.0 - min(1.0, iv.std() / max(iv.mean(), 1) * 4)
    c = 0.5 * np.clip(contrast * 1.6, 0, 1) + 0.3 * np.clip(strength * 2.5, 0, 1) + 0.2 * steadiness
    return float(np.clip(c, 0, 1))


def downbeat(ose, low_db, beats):
    """4/4 assumed: the beat index whose beats carry the most attack and bass
    is beat one. (down, confidence)."""
    if len(beats) < 8:
        return 0, 0.0
    lowd = np.maximum(np.diff(low_db, prepend=low_db[:1]), 0)
    v = ose[beats] + 2.0 * lowd[beats] * 10
    s = np.array([v[k::4].mean() for k in range(4)])
    order = np.argsort(-s)
    margin = (s[order[0]] - s[order[1]]) / max(s[order[0]], 1e-6)
    return int(order[0]), float(np.clip(margin * 2, 0, 1))


# ---------------------------------------------------------------- structure
def sections(mel, loud01, hz=SR / HOP, step_s=0.5, kernel_s=16.0, min_gap_s=8.0):
    """Boundaries where the texture changes, from a checkerboard-kernel
    novelty curve over the self-similarity of the log-mel spectrum, pooled to
    two frames a second. Each boundary carries the mean loudness (0..1) of the
    section it starts."""
    k = int(hz * step_s)
    n = len(mel) // k
    if n < 8:
        return []
    f = mel[:n * k].reshape(n, k, N_MEL).mean(axis=1)
    f = f - f.mean(axis=0)
    f /= (np.linalg.norm(f, axis=1, keepdims=True) + 1e-6)
    ssm = f @ f.T
    half = max(2, int(kernel_s / step_s / 2))
    g = np.hanning(2 * half)[:, None] * np.hanning(2 * half)[None, :]
    sign = np.ones((2 * half, 2 * half))
    sign[:half, half:] = -1
    sign[half:, :half] = -1
    ck = g * sign
    nov = np.zeros(n)
    for i in range(half, n - half):
        nov[i] = (ssm[i - half:i + half, i - half:i + half] * ck).sum()
    nov = np.maximum(nov, 0)
    if nov.max() > 0:
        nov /= nov.max()
    peaks = _peaks(nov, int(min_gap_s / step_s), 0.18)
    lp = loud01[:n * k].reshape(n, k).mean(axis=1) if len(loud01) >= n * k else np.zeros(n)
    out = []
    bounds = [0] + peaks + [n]
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b - a < 2:
            continue
        out.append([round(a * step_s, 2), round(float(lp[a:b].mean()), 3)])
    return out


def climax(loud01, hz=FPS, win_s=20.0):
    w = int(win_s * hz)
    if len(loud01) <= w:
        return [0.0, round(len(loud01) / float(hz), 2)]
    cs = np.concatenate([[0.0], np.cumsum(loud01)])
    sums = cs[w:] - cs[:-w]
    at = int(np.argmax(sums))
    return [round(at / float(hz), 2), round((at + w) / float(hz), 2)]


# ---------------------------------------------------------------- the whole thing
def analyse(path):
    t0 = time.perf_counter()
    x = decode(path)
    dur = len(x) / float(SR)
    loud_p, band_p, mel = spectra(x)
    hz = SR / float(HOP)
    k = int(round(hz / FPS))

    loud = _db_norm(loud_p, 45.0, 99.5) ** 1.2
    bands = [_db_norm(band_p[:, b], 40.0, 99.0) for b in range(3)]
    ose = onset_strength(mel)
    bpm, strength = tempo(ose, hz)
    beats = track_beats(ose, bpm, hz) if bpm else np.zeros(0, int)
    # The octave question: autocorrelation cannot tell a tempo from its double,
    # and the prior only leans. Measured on five records (17 Oct 2026): a grid
    # that is one octave too FAST has beats of alternating strength (every
    # second one lands on nothing: Hotel California live came out at 151, alt
    # 0.59, where 75.5 has alt 0.02), and a ballad picked at double speed has
    # a half-tempo grid whose beats are both stronger and more confident
    # (Someone Like You, 135 -> 67.6). Take on Me stays at 84.4: its 169 grid
    # alternates 0.52, which is the half-time feel most people tap to it.
    def _alt(b):
        on = ose[b]
        return abs(on[0::2].mean() - on[1::2].mean()) / max(on.mean(), 1e-6)
    for _ in range(2):
        if len(beats) < 8 or bpm / 2 < 50:
            break
        half = track_beats(ose, bpm / 2, hz)
        if len(half) < 8:
            break
        if _alt(beats) > 0.45 or (beat_confidence(ose, half, strength) > beat_confidence(ose, beats, strength)
                                 and ose[half].mean() > 1.2 * ose[beats].mean()):
            beats, bpm = half, bpm / 2
        else:
            break
    if len(beats) > 8 and bpm * 2 <= 200:
        dbl = track_beats(ose, bpm * 2, hz)
        if len(dbl) > 8 and _alt(dbl) < 0.3 and ose[dbl].mean() >= 0.75 * ose[beats].mean():
            beats, bpm = dbl, bpm * 2
    conf = beat_confidence(ose, beats, strength)
    # the grid's own tempo is the truth; the autocorrelation only chose the neighbourhood
    if len(beats) > 8:
        bpm = 60.0 * hz / float(np.median(np.diff(beats)))
    down, downconf = downbeat(ose, bands[0], beats)
    on_idx = _peaks(_smooth(ose, 3), int(hz * 0.12), 0.22)

    loud20 = _pool(loud, k)
    flux20 = _pool(np.clip(ose, 0, 1), k, 'max')
    secs = sections(mel, loud)
    arc = _smooth(_pool(loud, int(hz)), 6)             # one a second, ~6 s window
    ks = int(round(hz / SPEC_HZ))
    n_s = len(mel) // ks
    db = 10.0 * mel[:n_s * ks].reshape(n_s, ks, SPEC_N, N_MEL // SPEC_N).mean(axis=(1, 3))
    own = np.clip((db - (np.percentile(db, 98, axis=0) - 36.0)) / 36.0, 0, 1)
    whole = np.clip((db - (np.percentile(db, 99.5) - 50.0)) / 50.0, 0, 1)
    spec = 0.6 * own + 0.4 * whole
    out = {
        'v': VERSION, 'hz': FPS, 'dur': round(dur, 2),
        'loud': _bytes(loud20),
        'low': _bytes(_pool(bands[0], k)), 'mid': _bytes(_pool(bands[1], k)), 'high': _bytes(_pool(bands[2], k)),
        'flux': _bytes(flux20),
        'onsets': [int(round(i / hz * 100)) for i in on_idx],
        'bpm': round(bpm, 2), 'beatconf': round(conf, 3),
        'beats': [int(round(b / hz * 100)) for b in beats],
        'bar': {'n': 4, 'down': down, 'downconf': round(downconf, 3)},
        'sections': secs,
        'arc': _bytes(np.clip(arc, 0, 1)),
        'climax': climax(loud20),
        'spechz': SPEC_HZ, 'specn': SPEC_N, 'spec': _bytes(spec.reshape(-1)),
        # share of A-weighted power per band over the song, so a client can tell
        # a bass-led record from a bright one before it starts
        'balance': [round(float(v), 3) for v in (band_p.mean(axis=0) / max(band_p.mean(axis=0).sum(), 1e-9))],
        'ms': int((time.perf_counter() - t0) * 1000),
    }
    return out


def summary(d):
    n = len(d['beats'])
    return ('%.1f s  %.1f bpm  beatconf %.2f  down %d (%.2f)  %d beats  %d onsets  %d sections  climax %s  %d ms'
            % (d['dur'], d['bpm'], d['beatconf'], d['bar']['down'], d['bar']['downconf'], n,
               len(d['onsets']), len(d['sections']), d['climax'], d['ms']))


# ---------------------------------------------------------------- cache
def cache_path(cache_dir, skey):
    return Path(cache_dir) / (skey + '.json.gz')


def load_cached(cache_dir, skey):
    """The cached analysis as gzip bytes, or None. (Served as-is with
    Content-Encoding: gzip; nothing is re-encoded per request.)"""
    p = cache_path(cache_dir, skey)
    try:
        return p.read_bytes()
    except OSError:
        return None


def compute_and_cache(cache_dir, skey, path):
    """Analyse and store. A track ffmpeg cannot decode is cached as an error so
    it is not retried on every song start."""
    import gzip
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    try:
        d = analyse(path)
    except Exception as e:                      # noqa: BLE001 - any failure is a cached miss
        d = {'v': VERSION, 'error': str(e)[:200]}
    blob = gzip.compress(json.dumps(d, separators=(',', ':')).encode('utf-8'), 6)
    p = cache_path(cache_dir, skey)
    tmp = p.with_suffix('.part')
    tmp.write_bytes(blob)
    os.replace(tmp, p)
    return d, blob


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('usage: musicfeat.py <audio file> [out.json]')
    src = Path(sys.argv[1])
    d = analyse(src)
    blob = json.dumps(d, separators=(',', ':'))
    if len(sys.argv) > 2:
        io.open(sys.argv[2], 'w', encoding='utf-8').write(blob)
    print(src.name)
    print('  ' + summary(d))
    print('  sections      ' + ', '.join('%.0fs/%.2f' % (s[0], s[1]) for s in d['sections']))
    print('  balance       low %.2f  mid %.2f  high %.2f' % tuple(d['balance']))
    import gzip
    print('  shipped size  %.1f KB json, %.1f KB gzipped' % (len(blob) / 1024.0, len(gzip.compress(blob.encode())) / 1024.0))
