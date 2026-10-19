"""Human-timed word references for split songs, borrowed from karaoke discs.

A CD+G disc wipes each word as it is sung, and somebody timed those wipes by
hand. cdg.py already reads them into words (cdgtext/<id>.json). The disc's
audio is a cover recording, so its clock is not the song's -- but its backing
track and the song's backing stem are instrumentals of the same arrangement,
so their harmony can be lined up (chroma features, dynamic time warping) and
the disc's word times carried across onto the song's clock.

Several labels (CB, DK, SC, ...) often timed the same song independently, so
each song can have several references; how far they disagree with each other
is how good the reference is.

    python jukebox/tools/discref.py                  # every split song with a disc
    python jukebox/tools/discref.py --only "Lyin"    # folder names containing this
    python jukebox/tools/discref.py --force          # redo existing references

Writes jukebox/cache/discref/<skey>.json. System Python with numpy; ffmpeg on PATH.
"""
import os, re, sys, json, time, sqlite3, argparse, subprocess
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent.parent
STEMS = Path(os.environ.get('JUKEBOX_STEMS') or r'D:\MusicStems')
_IN_ROOT = Path(os.environ.get('MUSICMATE_ROOT') or r'D:\Music') / 'Karaoke'
CDG_STORE = Path(os.environ.get('JUKEBOX_CDG') or (_IN_ROOT if _IN_ROOT.is_dir() or not Path(r'C:\Music\Karaoke').is_dir() else r'C:\Music\Karaoke'))
CDG_DB = HERE / 'cdg.db'
TEXT_DIR = HERE / 'cdgtext'
OUT = HERE / 'cache' / 'discref'
SR, NFFT, HOP = 22050, 4096, 1102          # ~20 feature frames a second


def norm_name(s):
    s = re.sub(r'\(.*?\)|\[.*?\]', '', (s or '').lower()).replace('&', 'and')
    s = re.sub(r'[^a-z0-9]', '', s)
    return s[3:] if s.startswith('the') else s


def decode(path):
    p = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'f32le', '-ac', '1', '-ar', str(SR), '-'],
                       capture_output=True, check=True)
    return np.frombuffer(p.stdout, dtype=np.float32)


ONSET_W = 0.8          # weight of the onset part against the harmony part


def chroma(x):
    """Per frame: harmony (12-bin chroma, the flat part removed so that frames
    differ) and onsets (rises in 16 log-spaced bands), which is what pins a
    beat to a frame; both unit length, joined. Plus frame energy.
    The onset idea is Ewert, Mueller and Grosche's (ICASSP 2009): chroma
    alone finds the right bar, onsets find the right moment within it."""
    n = 1 + max(0, (len(x) - NFFT) // HOP)
    win = np.hanning(NFFT).astype(np.float32)
    freqs = np.fft.rfftfreq(NFFT, 1 / SR)
    band = (freqs >= 55) & (freqs <= 4200)
    pc = (np.round(69 + 12 * np.log2(freqs[band] / 440.0)).astype(int)) % 12
    M = np.zeros((band.sum(), 12), dtype=np.float32)
    M[np.arange(band.sum()), pc] = 1
    edges = np.geomspace(60, 10000, 17)
    B = np.zeros((len(freqs), 16), dtype=np.float32)
    for b in range(16):
        B[(freqs >= edges[b]) & (freqs < edges[b + 1]), b] = 1
    ch = np.zeros((n, 12), dtype=np.float32)
    lb = np.zeros((n, 16), dtype=np.float32)
    energy = np.zeros(n, dtype=np.float32)
    for b0 in range(0, n, 512):
        idx = np.arange(b0, min(n, b0 + 512))
        frames = np.stack([x[i * HOP:i * HOP + NFFT] for i in idx]) * win
        mag = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
        energy[idx] = mag[:, band].sum(axis=1)
        ch[idx] = (mag[:, band] ** 2) @ M
        lb[idx] = np.log1p(1000 * (mag @ B) / (B.sum(axis=0) + 1))
    ch = np.log1p(10 * ch / (ch.max(axis=1, keepdims=True) + 1e-9))
    ch -= ch.mean(axis=1, keepdims=True)
    ch /= np.linalg.norm(ch, axis=1, keepdims=True) + 1e-6
    on = np.maximum(0, np.diff(lb, axis=0, prepend=lb[:1]))
    on = (on + np.roll(on, 1, axis=0)) / 2                    # a little smoothing
    on /= on.std(axis=0, keepdims=True) + 1e-6
    on -= on.mean(axis=1, keepdims=True)
    on /= np.linalg.norm(on, axis=1, keepdims=True) + 1e-6
    feat = np.hstack([ch, ONSET_W * on]) / np.sqrt(1 + ONSET_W ** 2)
    return feat.astype(np.float32), energy


def trim(energy):
    """First and last frame with music in them."""
    thr = np.percentile(energy, 95) * 0.02
    on = np.nonzero(energy > thr)[0]
    return (int(on[0]), int(on[-1]) + 1) if len(on) else (0, len(energy))


def dtw(A, B):
    """Path through cost 1 - cos(A_i, B_j), steps (1,1), (1,2), (2,1): the
    tempo ratio stays between one half and two. Returns (path, mean cost)."""
    C = (1 - A @ B.T).astype(np.float32)
    N, M = C.shape
    INF = np.float32(1e9)
    D = np.full((N, M), INF, dtype=np.float32)
    P = np.zeros((N, M), dtype=np.int8)
    D[0, 0] = C[0, 0]
    for i in range(1, N):
        c = np.full(M, INF, dtype=np.float32); s = np.zeros(M, dtype=np.int8)
        d11 = np.full(M, INF, dtype=np.float32); d11[1:] = D[i - 1, :-1]
        d12 = np.full(M, INF, dtype=np.float32); d12[2:] = D[i - 1, :-2] + C[i, 1:-1]
        d21 = np.full(M, INF, dtype=np.float32)
        if i >= 2:
            d21[1:] = D[i - 2, :-1] + C[i - 1, 1:]
        c = d11; s[:] = 0
        m = d12 < c; c = np.where(m, d12, c); s[m] = 1
        m = d21 < c; c = np.where(m, d21, c); s[m] = 2
        D[i] = c + C[i]; P[i] = s
    i, j = N - 1, M - 1
    if D[i, j] >= INF:
        return None, 1.0
    path = [(i, j)]
    while i > 0 or j > 0:
        s = P[i, j]
        if s == 0:
            i, j = i - 1, j - 1
        elif s == 1:
            path.append((i, j - 1)); i, j = i - 1, j - 2
        else:
            path.append((i - 1, j)); i, j = i - 2, j - 1
        if i < 0 or j < 0:
            return None, 1.0
        path.append((i, j))
    path.reverse()
    pa = np.array(path)
    return pa, float(C[pa[:, 0], pa[:, 1]].mean())


def disc_ids(c, skey, folder):
    a, _, t = folder[:folder.rindex(' [')].partition(' - ')
    want = (norm_name(a), norm_name(t))
    ids = []
    for cid, da, dt, sk, la, lt, n in c.execute(
            "select id,artist,title,lib_skey,lib_artist,lib_title,n_words from song where text_state='ok'"):
        if sk == skey or (norm_name(da), norm_name(dt)) == want or (norm_name(la), norm_name(lt)) == want:
            ids.append(cid)
    return ids


def build(d, c, force=False):
    skey = re.search(r'\[([0-9a-f]+)\]', d.name).group(1)
    dest = OUT / f'{skey}.json'
    if dest.exists() and not force:
        return None
    ids = disc_ids(c, skey, d.name)
    if not ids:
        return None
    t0 = time.time()
    song, se = chroma(decode(d / 'backing.opus'))
    s0, s1 = trim(se)
    discs = []
    for cid in ids:
        row = c.execute('select rel_mp3, code from song where id=?', (cid,)).fetchone()
        try:
            words = json.loads((TEXT_DIR / f'{cid}.json').read_text(encoding='utf-8')).get('words') or {}
            disc, de = chroma(decode(CDG_STORE / row[0]))
        except Exception as e:
            print('  skip disc', cid, e); continue
        d0, d1 = trim(de)
        path, cost = dtw(disc[d0:d1], song[s0:s1])
        if path is None:
            print('  no path for disc', cid); continue
        # disc seconds -> song seconds along the path (mean song frame per disc frame)
        di = path[:, 0] + d0; sj = path[:, 1] + s0
        uniq, inv = np.unique(di, return_inverse=True)
        sm = np.bincount(inv, weights=sj) / np.bincount(inv)
        td, ts = (uniq + 0.5) * HOP / SR, (sm + 0.5) * HOP / SR
        out_words = []
        for L in words.get('lines') or []:
            for w in L.get('words') or []:
                if w.get('s') is None:
                    continue
                out_words.append({'w': w['w'], 'd': w['s'], 's': round(float(np.interp(w['s'], td, ts)), 3)})
        discs.append({'id': cid, 'code': row[1], 'cost': round(cost, 4), 'words': out_words})
    if not discs:
        return None
    OUT.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({'skey': skey, 'song': d.name, 'made': time.time(), 'discs': discs}), encoding='utf-8')
    return len(discs), time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args()
    c = sqlite3.connect(f'file:{CDG_DB}?mode=ro', uri=True)
    n = 0
    for d in sorted(STEMS.iterdir()):
        if not re.search(r'\[[0-9a-f]+\]$', d.name) or not (d / 'backing.opus').exists():
            continue
        if a.only and a.only.lower() not in d.name.lower():
            continue
        r = build(d, c, a.force)
        if r:
            n += 1
            print('%-60s %d disc(s) %.0fs' % (d.name[:60], r[0], r[1]), flush=True)
    print('references written:', n)


if __name__ == '__main__':
    sys.exit(main())
