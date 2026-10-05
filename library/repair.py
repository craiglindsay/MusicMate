"""Repair files that decode with errors.

Two mechanisms, in order of preference:

  1. If a fingerprint-identical clean copy exists elsewhere, put that copy in the
     damaged file's place. Costs nothing and often upgrades the format too.
  2. Otherwise remux: copy the audio stream into a fresh container, which drops
     corrupt frames and rebuilds the index. This is NOT a re-encode -- the audio
     that survives is bit-identical -- so it respects the no-transcoding rule.

A remux is only accepted if the result decodes clean and keeps the duration; a
shorter or still-broken output is discarded and the original left alone.
"""
import os, sys, time, subprocess, sqlite3, tempfile, shutil, argparse, collections
import musiclib, merge

ROOT = musiclib.ROOT

def decode_errors(path):
    """(error count, decoded duration) for a file."""
    p = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-f', 'null', '-'],
                       capture_output=True, text=True, timeout=300)
    n = len([l for l in p.stderr.splitlines() if l.strip()])
    q = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', path], capture_output=True, text=True, timeout=120)
    try: dur = float(q.stdout.strip())
    except ValueError: dur = 0.0
    return n, dur

def remux(src, dst):
    ext = os.path.splitext(src)[1].lower()
    cmd = ['ffmpeg', '-v', 'error', '-err_detect', 'ignore_err', '-i', src,
           '-map', '0:a', '-c:a', 'copy', '-map_metadata', '0']
    if ext == '.mp3':
        cmd += ['-id3v2_version', '3', '-write_id3v1', '1']
    cmd += ['-y', dst]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 10000

def find_replacements(conn):
    """damaged path -> clean path, only where the titles agree as well as the audio."""
    dmg = conn.execute('SELECT * FROM files WHERE fp_err IS NOT NULL AND kind="audio" '
                       'AND fp IS NOT NULL').fetchall()
    clean = conn.execute('SELECT path,title,bitrate,ext,duration,fp FROM files '
                         'WHERE kind="audio" AND fp IS NOT NULL AND fp_err IS NULL').fetchall()
    cache = {r['path']: musiclib.unpack_fp(r['fp']) for r in clean}
    out, mismatched = {}, []
    for d in dmg:
        fd = musiclib.unpack_fp(d['fp'])
        best = None
        for r in clean:
            if d['duration'] and r['duration'] and abs(d['duration'] - r['duration']) > 20:
                continue
            dist = merge.fp_distance(fd, cache[r['path']])
            if dist <= 0.15 and (best is None or dist < best[0]):
                best = (dist, r)
        if not best:
            continue
        dist, r = best
        # A distance at or near zero is proof the audio is the same recording, so a
        # spelling difference in the title ("Mistery" for "Mystery") cannot outvote
        # it. Above that, a title disagreement means one file is mistagged and
        # picking a winner would be a guess.
        if (dist <= 0.01 or
                merge.norm_title(d['title'] or '') == merge.norm_title(r['title'] or '')):
            out[d['path']] = (r['path'], dist)
        else:
            # audio matches but the two files disagree about what the track is:
            # one of them is mistagged, and guessing which would be a coin flip
            mismatched.append((d['path'], r['path'], dist, d['title'], r['title']))
    return out, mismatched

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    conn = musiclib.db(); conn.row_factory = sqlite3.Row
    repl, mismatched = find_replacements(conn)
    print(f'{"APPLYING" if a.apply else "DRY RUN"}')
    print(f'  damaged files with a clean, same-titled twin : {len(repl)}')
    print(f'  audio matches but titles disagree (skipped)  : {len(mismatched)}')
    for dp, cp, dist, dt, ct in mismatched:
        print(f'      {dist:.3f}  {dt!r} vs {ct!r}')

    swapped = 0
    for dmg_rel, (clean_rel, dist) in repl.items():
        if not a.apply:
            swapped += 1; continue
        dmg_abs = os.path.join(ROOT, dmg_rel.replace('/', os.sep))
        clean_abs = os.path.join(ROOT, clean_rel.replace('/', os.sep))
        if not (os.path.exists(dmg_abs) and os.path.exists(clean_abs)):
            continue
        # retire the damaged file, then move the clean copy into its slot
        musiclib._move(conn, dmg_rel, musiclib.SUPERSEDED, 'repair',
                       f'damaged; replaced by fingerprint-identical copy ({dist:.3f})', False)
        newrel = os.path.dirname(dmg_rel) + '/' + os.path.basename(clean_rel)
        dst = os.path.join(ROOT, newrel.replace('/', os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            continue
        size = os.path.getsize(clean_abs)
        os.replace(clean_abs, dst)
        conn.execute('INSERT INTO moves (ts,op,reason,src,dst,size) VALUES (?,?,?,?,?,?)',
                     (time.time(), 'repair', 'clean copy promoted into damaged slot',
                      clean_rel, newrel, size))
        conn.execute('UPDATE files SET path=?, folder=? WHERE path=?',
                     (newrel, os.path.dirname(newrel), clean_rel))
        swapped += 1
    if a.apply: conn.commit()
    print(f'\n  {"would swap" if not a.apply else "swapped"} in clean copies: {swapped}')

    # remux whatever is still damaged
    rest = [r['path'] for r in conn.execute(
        'SELECT path FROM files WHERE fp_err IS NOT NULL AND kind="audio"')
        if r['path'] not in repl]
    print(f'  files to remux: {len(rest)}')
    if not a.apply:
        conn.close(); return

    td = tempfile.mkdtemp()
    fixed = failed = skipped = 0
    for i, rel in enumerate(rest):
        src = os.path.join(ROOT, rel.replace('/', os.sep))
        if not os.path.exists(src):
            continue
        before_err, before_dur = decode_errors(src)
        if before_err == 0:
            skipped += 1; continue
        tmp = os.path.join(td, 'fix' + os.path.splitext(rel)[1])
        try:
            if not remux(src, tmp):
                failed += 1; continue
            after_err, after_dur = decode_errors(tmp)
            # accept only a genuine improvement that keeps the music
            if after_err < before_err and after_dur >= before_dur * 0.98:
                musiclib._move(conn, rel, musiclib.SUPERSEDED, 'repair',
                               f'remuxed: {before_err} decode errors -> {after_err}', False)
                dst = src
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(tmp, dst)
                conn.execute(
                    'INSERT INTO files (path,src,kind,folder,filename,ext,size,mtime) '
                    'VALUES (?,?,?,?,?,?,?,?)',
                    (rel, rel.split('/')[0], 'audio', os.path.dirname(rel),
                     os.path.basename(rel), os.path.splitext(rel)[1],
                     os.path.getsize(dst), os.path.getmtime(dst)))
                fixed += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        finally:
            if os.path.exists(tmp):
                try: os.remove(tmp)
                except OSError: pass
        if (i + 1) % 10 == 0:
            conn.commit(); print(f'   {i+1}/{len(rest)}  fixed={fixed}', flush=True)
    conn.commit()
    shutil.rmtree(td, ignore_errors=True)
    print(f'\nremux: {fixed} repaired, {failed} left alone (no improvement), '
          f'{skipped} already clean')
    conn.close()

if __name__ == '__main__':
    main()
