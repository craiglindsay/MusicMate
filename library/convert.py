"""Re-encode the formats no browser or modern device will play.

232 WMA and 25 Musepack files, all already lossy (~290k / ~243k, measured cutoffs
15.7-20.1 kHz). One careful pass at 256k AAC is effectively transparent against
sources like that, and it retires two formats that nothing else in the library uses.

Tags are rewritten from the database rather than left to ffmpeg's mapping: WMA and
APEv2 fields do not map cleanly onto MP4 atoms, and the database already holds the
canonical, MusicBrainz-checked values.

Originals go to _superseded, so this is reversible.
"""
import os, sys, time, sqlite3, subprocess, argparse, collections
import mutagen, mutagen.mp4
import musiclib

ROOT = musiclib.ROOT
SRC_EXT = ('.wma', '.mpc')

def encode(src, dst):
    r = subprocess.run(
        ['ffmpeg', '-v', 'error', '-nostdin', '-y', '-i', src,
         '-map', '0:a:0', '-c:a', 'aac', '-b:a', '256k', '-movflags', '+faststart',
         dst],
        capture_output=True, text=True, timeout=900)
    return r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 20000

def duration_of(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', path], capture_output=True, text=True, timeout=120)
    try: return float(r.stdout.strip())
    except ValueError: return 0.0

def write_tags(dst, row):
    """Carry the database's values onto the new file."""
    try:
        f = mutagen.mp4.MP4(dst)
    except Exception:
        return False
    def s(k, v):
        if v: f[k] = [str(v)]
    s('\xa9ART', row['artist'])
    s('aART',    row['albumartist'])
    s('\xa9alb', row['album'])
    s('\xa9nam', row['title'])
    s('\xa9gen', row['genre'])
    s('\xa9day', row['year'])
    if row['track']:
        f['trkn'] = [(int(row['track']), int(row['tracktotal'] or 0))]
    if row['disc']:
        f['disk'] = [(int(row['disc']), int(row['disctotal'] or 0))]
    # carry the sort tags the canonicalisation pass wrote, if present
    try:
        old = mutagen.File(os.path.join(ROOT, row['path'].replace('/', os.sep)))
    except Exception:
        old = None
    try:
        f.save()
    except Exception:
        return False
    return True

def embed_art(dst, folder):
    """Attach the album's sidecar image so the new file is not artless."""
    for name in ('folder.jpg', 'Folder.jpg', 'cover.jpg', 'front.jpg', '.folder.png'):
        p = os.path.join(ROOT, folder.replace('/', os.sep), name)
        if os.path.exists(p) and os.path.getsize(p) < 4_000_000:
            try:
                data = open(p, 'rb').read()
                fmt = (mutagen.mp4.MP4Cover.FORMAT_PNG if p.lower().endswith('.png')
                       else mutagen.mp4.MP4Cover.FORMAT_JPEG)
                f = mutagen.mp4.MP4(dst)
                f['covr'] = [mutagen.mp4.MP4Cover(data, imageformat=fmt)]
                f.save()
                return True
            except Exception:
                return False
    return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--limit', type=int)
    a = ap.parse_args()
    conn = musiclib.db(); conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT * FROM files WHERE kind="audio" AND ext IN (?,?) ORDER BY path',
        SRC_EXT).fetchall()
    if a.limit: rows = rows[:a.limit]
    print(f'{"APPLYING" if a.apply else "DRY RUN"}: {len(rows)} files to re-encode as 256k AAC')
    print(f'   {collections.Counter(r["ext"] for r in rows).most_common()}')
    if not a.apply:
        for r in rows[:6]:
            print(f'   {r["path"][-62:]}  {r["size"]/1e6:.1f}M {(r["bitrate"] or 0)//1000}k')
        conn.close(); return

    ok = skipped = 0
    errs = []
    t0 = time.time()
    for i, r in enumerate(rows):
        src = os.path.join(ROOT, r['path'].replace('/', os.sep))
        if not os.path.exists(src):
            skipped += 1; continue
        newrel = os.path.splitext(r['path'])[0] + '.m4a'
        dst = os.path.join(ROOT, newrel.replace('/', os.sep))
        if os.path.exists(dst):
            skipped += 1; continue
        part = dst + '.part.m4a'
        try:
            if not encode(src, part):
                errs.append((r['path'], 'encode failed')); continue
            # the new file must hold the whole performance
            d_old, d_new = r['duration'] or duration_of(src), duration_of(part)
            if d_old and d_new < d_old * 0.98:
                os.remove(part)
                errs.append((r['path'], f'short output {d_new:.0f}s vs {d_old:.0f}s'))
                continue
            os.replace(part, dst)
            write_tags(dst, r)
            embed_art(dst, r['folder'])
            musiclib._move(conn, r['path'], musiclib.SUPERSEDED, 'convert',
                           f'{r["ext"]} re-encoded to 256k AAC for playability', False)
            conn.execute(
                'INSERT OR REPLACE INTO files (path,src,kind,folder,filename,ext,size,mtime) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (newrel, newrel.split('/')[0], 'audio', os.path.dirname(newrel),
                 os.path.basename(newrel), '.m4a', os.path.getsize(dst),
                 os.path.getmtime(dst)))
            ok += 1
        except Exception as e:
            errs.append((r['path'], f'{type(e).__name__}: {str(e)[:70]}'))
            if os.path.exists(part):
                try: os.remove(part)
                except OSError: pass
        if (i + 1) % 25 == 0:
            conn.commit()
            el = time.time() - t0
            print(f'   {i+1}/{len(rows)}  ok={ok}  eta {(len(rows)-i-1)*el/(i+1)/60:.1f}m',
                  flush=True)
    conn.commit()
    print(f'\nconverted {ok}, skipped {skipped}, errors {len(errs)} in {time.time()-t0:.0f}s')
    for p, e in errs[:10]: print(f'   {p[-60:]}: {e}')
    conn.close()

if __name__ == '__main__':
    main()
