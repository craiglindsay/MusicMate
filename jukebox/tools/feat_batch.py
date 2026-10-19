"""Pre-analyse the library for the backdrops (musicfeat.py), so a song's first
play does not wait a second or two for its features.

Optional: the server computes a song's features on first request anyway, and
prefetches the next in the queue. This just does the whole library ahead of
time. Resumable -- cached songs are skipped -- and safe to stop at any point.

    python tools/feat_batch.py              # everything not yet cached
    python tools/feat_batch.py --limit 200  # a taste, with the measured rate
    python tools/feat_batch.py --workers 4  # default: cores minus two

Measured 17 Oct 2026 on the server machine: 0.6-2.0 s a track single-threaded
(scales with length; decode and the STFT are most of it), ~25-45 KB gzipped a
track on disk. The whole library (52,942 tracks) is therefore around 1.5 GB
and, at four workers, roughly six hours. Reads the music; writes only under
jukebox/cache/feat/.
"""
import os, sys, time, sqlite3, argparse
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

HERE = Path(__file__).resolve().parent
JUKEBOX = HERE.parent
sys.path.insert(0, str(JUKEBOX))
import musicfeat as mf                                     # noqa: E402

ROOT = Path(os.environ.get('MUSICMATE_ROOT') or r'D:\Music')
FEAT_DIR = JUKEBOX / 'cache' / 'feat' / ('v%d' % mf.VERSION)


def _one(skey, path):
    t0 = time.perf_counter()
    d, blob = mf.compute_and_cache(FEAT_DIR, skey, path)
    return skey, d.get('error'), len(blob), time.perf_counter() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument('--pool', default=None, help='only this library folder (album.pool)')
    a = ap.parse_args()

    c = sqlite3.connect(f'file:{JUKEBOX / "jukebox.db"}?mode=ro', uri=True)
    q = ('SELECT DISTINCT track.skey, track.rel_path FROM track JOIN album ON album.id=track.album_id'
         + (' WHERE album.pool=?' if a.pool else '') + ' ORDER BY track.id')
    rows = c.execute(q, (a.pool,) if a.pool else ()).fetchall()
    c.close()
    FEAT_DIR.mkdir(parents=True, exist_ok=True)
    have = {p.name[:-8] for p in FEAT_DIR.glob('*.json.gz')}
    todo, seen = [], set()
    for skey, rel in rows:
        if skey in have or skey in seen:
            continue
        seen.add(skey)
        todo.append((skey, ROOT / rel.replace('/', os.sep)))
    if a.limit:
        todo = todo[:a.limit]
    print('%d songs cached, %d to do, %d workers' % (len(have), len(todo), a.workers))
    if not todo:
        return
    t0 = time.perf_counter()
    done = errs = size = 0
    cpu = 0.0
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(_one, k, p) for k, p in todo]
        for f in as_completed(futs):
            try:
                skey, err, n, dt = f.result()
            except Exception as e:                       # noqa: BLE001
                errs += 1
                print('  failed:', e)
                continue
            done += 1
            size += n
            cpu += dt
            if err:
                errs += 1
            if done % 25 == 0 or done == len(todo):
                el = time.perf_counter() - t0
                rate = done / el
                print('  %d/%d  %.2f s/track (wall %.2f)  %.1f KB avg  %d errors  ETA %.0f min'
                      % (done, len(todo), cpu / done, 1 / rate, size / done / 1024.0, errs,
                         (len(todo) - done) / rate / 60), flush=True)
    print('done: %d analysed, %d errors, %.1f MB, %.1f min' % (done, errs, size / 1048576.0, (time.perf_counter() - t0) / 60))


if __name__ == '__main__':
    main()
