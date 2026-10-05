"""Quality ranking for choosing between duplicate copies of a track.

Bitrate alone is a trap: this library has been through several format migrations,
so a 256k AAC may well be an upscale of a 128k MP3 -- worse than the 192k MP3
sitting next to it. We therefore combine the declared bitrate with a measured
spectral cutoff, which survives transcoding and exposes the upscales.
"""
import os, subprocess, numpy as np

ROOT = os.environ.get('MUSICMATE_ROOT') or r'D:\Music'
FFMPEG = 'ffmpeg'

LOSSLESS_EXT = {'.flac', '.wav', '.ape', '.wv', '.aiff', '.aif', '.tta', '.shn', '.dsf', '.alac'}

# Rough bitrate-efficiency relative to MP3 at equal perceived quality.
CODEC_EFF = {
    'MPEGInfo':      1.00,   # MP3
    'MP4Info':       1.30,   # AAC
    'OggVorbisInfo': 1.25,   # Vorbis
    'Musepack':      1.35,
    'MusepackInfo':  1.35,
    'ASFInfo':       1.10,   # WMA
    'OpusInfo':      1.50,
}

# Lowpass a competent encoder leaves at a given *MP3-equivalent* bitrate (kHz).
# Measuring well below the expected value means the source was already lossy.
EXPECTED_CUTOFF = [(96, 15.0), (128, 16.0), (160, 17.0), (192, 18.0),
                   (224, 19.0), (256, 19.5), (320, 20.0)]

def expected_cutoff(eff_kbps):
    best = EXPECTED_CUTOFF[-1][1]
    for br, cut in EXPECTED_CUTOFF:
        if eff_kbps <= br:
            return cut
    return best

def measure_cutoff(path, ss=30, dur=30, sr=44100):
    """Highest frequency (kHz) still carrying real energy. None if undecodable."""
    full = path if os.path.isabs(path) else os.path.join(ROOT, path.replace('/', os.sep))
    cmd = [FFMPEG, '-v', 'quiet', '-ss', str(ss), '-t', str(dur), '-i', full,
           '-ac', '1', '-ar', str(sr), '-f', 'f32le', '-']
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=120)
        buf = p.stdout
        if len(buf) < sr * 4 * 2:                       # need >=2s; retry from 0
            p = subprocess.run([FFMPEG, '-v', 'quiet', '-t', str(dur), '-i', full,
                                '-ac', '1', '-ar', str(sr), '-f', 'f32le', '-'],
                               capture_output=True, timeout=120)
            buf = p.stdout
        if len(buf) < sr * 4:
            return None
        x = np.frombuffer(buf, dtype=np.float32)
        x = x[np.isfinite(x)]
        if x.size < sr:
            return None
        n = 8192
        nwin = min(120, x.size // n)
        if nwin < 2:
            return None
        w = np.hanning(n)
        acc = np.zeros(n // 2 + 1)
        for i in range(nwin):
            seg = x[i * n:(i + 1) * n] * w
            acc += np.abs(np.fft.rfft(seg)) ** 2
        acc /= nwin
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        db = 10 * np.log10(acc + 1e-20)
        # reference = strongest part of the spectrum, ignoring the very bottom
        ref = np.max(db[(freqs > 200) & (freqs < 4000)]) if np.any(freqs > 200) else np.max(db)
        thresh = ref - 55.0
        above = np.where(db > thresh)[0]
        if above.size == 0:
            return None
        return round(float(freqs[above[-1]]) / 1000.0, 2)
    except Exception:
        return None

# A VBR encode reports its *average* rate, which understates it: measured across
# this library, VBR at ~128k avg reaches 17.1kHz and at ~192k avg reaches 18.6kHz,
# matching CBR streams a full step or two higher. Without this, a V0 rip loses to a
# CBR 320 of the same recording purely on the nominal number.
VBR_BONUS = 1.15

def _field(f, key, default=None):
    """Works for both sqlite3.Row and plain dicts."""
    try:
        v = f[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v

def effective_kbps(bitrate, codec, ext, brmode=None):
    if ext.lower() in LOSSLESS_EXT:
        return 10_000
    eff = (bitrate or 0) / 1000.0 * CODEC_EFF.get(codec, 1.0)
    if brmode and ('VBR' in str(brmode) or 'ABR' in str(brmode)):
        eff *= VBR_BONUS
    return eff

def score(f, cutoff=None):
    """Composite quality score for one file. `f` is a dict-like DB row.

    Returns (score, notes). Higher is better. Only meaningful when comparing
    two copies of the *same* recording.
    """
    ext = (f['ext'] or '').lower()
    notes = []
    if ext in LOSSLESS_EXT:
        return 10_000 + (f['bitrate'] or 0) / 1000.0, ['lossless']

    eff = effective_kbps(f['bitrate'], f['codec'], ext, _field(f, 'brmode'))
    s = eff

    if cutoff is not None:
        exp = expected_cutoff(eff)
        if cutoff < 11.0:
            # No codec lowpasses this aggressively -- the *content* has no treble
            # (skits, spoken word, quiet passages). Says nothing about the encode.
            notes.append(f'cutoff {cutoff}kHz - content-limited, not comparable')
        elif cutoff < exp - 1.5:
            # measured band is far narrower than this bitrate should produce:
            # the audio was lossy before it was encoded again
            deficit = exp - cutoff
            s -= min(120, deficit * 30)
            notes.append(f'suspected transcode (cutoff {cutoff}kHz, expected ~{exp}kHz)')
        else:
            notes.append(f'cutoff {cutoff}kHz ok')

    # a file that is merely a fragment is never the better copy
    return s, notes

def score_pair(f, my_cutoff, other_cutoff):
    """Score one copy of a recording, judged against the competing copy.

    The two files hold the same audio, so the higher of the two measured cutoffs
    is the best available estimate of what the source material actually contains.
    Penalise this copy only for bandwidth the other one managed to keep -- that is
    the signature of a transcode, and it is invariant to how bright the music is.
    """
    ext = (f['ext'] or '').lower()
    if ext in LOSSLESS_EXT:
        return 10_000 + (f['bitrate'] or 0) / 1000.0, ['lossless']

    eff = effective_kbps(f['bitrate'], f['codec'], ext, _field(f, 'brmode'))
    s, notes = eff, []

    if my_cutoff is None:
        return s, ['no cutoff measured']

    # ceiling = what this recording demonstrably contains, capped by what a clean
    # encode at this bitrate could deliver anyway
    ceiling = my_cutoff if other_cutoff is None else max(my_cutoff, other_cutoff)
    ceiling = min(ceiling, expected_cutoff(eff))

    deficit = ceiling - my_cutoff
    if deficit > 1.5:
        # This copy is demonstrably missing bandwidth the other one kept, so its
        # declared bitrate is a lie about the audio underneath. Rate it by the
        # bitrate its measured band actually implies -- otherwise a 320k upscale of
        # a 128k source still outranks an honest 192k file.
        implied = implied_kbps(my_cutoff)
        s = min(s, implied)
        notes.append(f'lossy source: {my_cutoff}kHz vs {ceiling}kHz available '
                     f'(rates as ~{implied:.0f}k, not {eff:.0f}k)')
    else:
        notes.append(f'cutoff {my_cutoff}kHz full for this material')
    return s, notes

def implied_kbps(cutoff):
    """Invert the cutoff table: what bitrate does this measured band suggest?"""
    pts = EXPECTED_CUTOFF
    if cutoff <= pts[0][1]:
        return max(32.0, pts[0][0] * cutoff / max(pts[0][1], 0.1))
    for (b0, c0), (b1, c1) in zip(pts, pts[1:]):
        if cutoff <= c1:
            t = (cutoff - c0) / (c1 - c0) if c1 != c0 else 0
            return b0 + t * (b1 - b0)
    return float(pts[-1][0])

def duration_ok(a, b, tol=3.0):
    if not a or not b: return True
    return abs(a - b) <= tol
