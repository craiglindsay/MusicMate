"""Music library inventory + merge toolkit.

Subcommands:
  scan        walk the tree, read tags into SQLite (resumable, fast)
  fingerprint compute chromaprint fingerprints in parallel (resumable, slow)
  stats       summary of what's in the DB

Everything is keyed on the path relative to ROOT, so VariousArtists/... and
the other top-level libraries (Compilations/..., Soundtracks/...) live in one
namespace. ROOT is the MUSICMATE_ROOT environment variable, default D:\\Music.
"""
import os, sys, sqlite3, json, subprocess, argparse, time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.environ.get('MUSICMATE_ROOT') or r'D:\Music'
_HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(_HERE, 'library.db')
FPCALC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'fpcalc.exe')

AUDIO_EXT = {'.mp3','.m4a','.m4p','.aac','.flac','.wma','.ogg','.wav','.aiff','.aif',
             '.mp4','.alac','.ape','.wv','.m4b','.opus',
             '.mpc','.mpp','.mp+',      # Musepack -- real tracks, easy to mistake for junk
             '.oga','.spx','.tta','.dsf','.mp2','.ra','.shn','.aa','.aax'}
IMG_EXT   = {'.jpg','.jpeg','.png','.bmp','.gif','.tif','.tiff','.webp'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path        TEXT PRIMARY KEY,   -- relative to ROOT, forward slashes
  src         TEXT,               -- top-level library: VariousArtists / Compilations / ...
  kind        TEXT,               -- audio | image | other
  folder      TEXT,               -- containing folder, relative
  filename    TEXT,
  ext         TEXT,
  size        INTEGER,
  mtime       REAL,
  duration    REAL,
  bitrate     INTEGER,
  samplerate  INTEGER,
  channels    INTEGER,
  codec       TEXT,
  brmode      TEXT,
  artist      TEXT,
  albumartist TEXT,
  album       TEXT,
  title       TEXT,
  track       INTEGER,
  tracktotal  INTEGER,
  disc        INTEGER,
  disctotal   INTEGER,
  year        TEXT,
  genre       TEXT,
  has_art     INTEGER,
  art_bytes   INTEGER,
  tag_err     TEXT,
  fp          BLOB,               -- raw chromaprint vector, zlib-packed uint32
  fp_dur      INTEGER,
  fp_err      TEXT,
  scanned_at  REAL,
  cutoff      REAL,               -- spectral cutoff in Hz (quality.py), the lossy-source tell
  cutoff_at   REAL
);
CREATE INDEX IF NOT EXISTS ix_files_src    ON files(src);
CREATE INDEX IF NOT EXISTS ix_files_kind   ON files(kind);
CREATE INDEX IF NOT EXISTS ix_files_folder ON files(folder);
CREATE INDEX IF NOT EXISTS ix_files_fp     ON files(fp);

-- record of every file we move, so any step can be undone
CREATE TABLE IF NOT EXISTS moves (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL,
  op      TEXT,      -- which stage did this
  reason  TEXT,
  src     TEXT,      -- original path relative to ROOT
  dst     TEXT,      -- new path relative to ROOT
  size    INTEGER
);
CREATE INDEX IF NOT EXISTS ix_moves_op ON moves(op);
"""

def db(path=DB):
    c = sqlite3.connect(path, timeout=60)
    c.executescript(SCHEMA)
    c.execute('PRAGMA journal_mode=WAL')
    return c

# ---------------------------------------------------------------- tag reading
def _first(a, keys):
    for k in keys:
        try: v = a.get(k)
        except Exception: v = None
        if v:
            if isinstance(v, list): v = v[0]
            if isinstance(v, tuple): v = v[0]
            try: v = str(v).strip()
            except Exception: v = None
            if v: return v
    return None

def _num(s):
    """'3/12' -> (3,12); '05' -> (5,None); '(3, 12)' -> (3,12)"""
    if s is None: return (None, None)
    s = str(s).strip().strip('()')
    n = t = None
    import re
    m = re.match(r'^\s*(\d+)', s)
    if m: n = int(m.group(1))
    m = re.search(r'[/,]\s*(\d+)', s)
    if m: t = int(m.group(1))
    if t == 0: t = None
    return (n, t)

def read_tags(full, ext):
    """Return dict of audio metadata, or {'tag_err': ...}."""
    import mutagen
    out = {}
    try:
        a = mutagen.File(full)
    except Exception as e:
        return {'tag_err': f'{type(e).__name__}: {str(e)[:150]}'}
    if a is None:
        return {'tag_err': 'unreadable/unknown format'}
    i = a.info
    out['duration']   = round(getattr(i, 'length', 0) or 0, 2)
    out['bitrate']    = int(getattr(i, 'bitrate', 0) or 0)
    out['samplerate'] = int(getattr(i, 'sample_rate', 0) or 0)
    out['channels']   = int(getattr(i, 'channels', 0) or 0)
    out['codec']      = type(i).__name__
    bm = getattr(i, 'bitrate_mode', None)
    if bm is not None: out['brmode'] = str(bm)

    out['artist']      = _first(a, ['artist','TPE1','\xa9ART','Author'])
    out['albumartist'] = _first(a, ['albumartist','TPE2','aART','WM/AlbumArtist',
                                    'ALBUMARTIST','album artist'])
    out['album']       = _first(a, ['album','TALB','\xa9alb','WM/AlbumTitle'])
    out['title']       = _first(a, ['title','TIT2','\xa9nam','Title'])
    out['year']        = _first(a, ['date','TDRC','TYER','\xa9day','WM/Year','year',
                                    'originaldate','TDOR'])
    out['genre']       = _first(a, ['genre','TCON','\xa9gen','WM/Genre'])

    trk = _first(a, ['tracknumber','TRCK','WM/TrackNumber','WM/Track'])
    dsc = _first(a, ['discnumber','TPOS','WM/PartOfSet'])
    # mp4 stores these as [(n, total)]
    try:
        if a.tags is not None:
            if 'trkn' in a.tags:
                p = a.tags['trkn'][0]; trk = f'{p[0]}/{p[1]}'
            if 'disk' in a.tags:
                p = a.tags['disk'][0]; dsc = f'{p[0]}/{p[1]}'
    except Exception:
        pass
    out['track'], out['tracktotal'] = _num(trk)
    out['disc'],  out['disctotal']  = _num(dsc)
    tt = _first(a, ['TRCK:total','totaltracks','TOTALTRACKS','WM/TrackCount'])
    if out['tracktotal'] is None and tt: out['tracktotal'] = _num(tt)[0]

    # embedded artwork
    n = b = 0
    try:
        t = a.tags
        if t is not None:
            if hasattr(t, 'getall'):
                pics = t.getall('APIC'); n = len(pics)
                b = sum(len(p.data) for p in pics)
            elif 'covr' in t:
                n = len(t['covr']); b = sum(len(x) for x in t['covr'])
            elif 'WM/Picture' in t:
                n = len(t['WM/Picture'])
        if not n and getattr(a, 'pictures', None):
            n = len(a.pictures); b = sum(len(p.data) for p in a.pictures)
        if not n and t is not None and 'metadata_block_picture' in t:
            # Vorbis keeps cover art as a base64 FLAC picture block in the comment
            # dict -- invisible to every other branch here, so Ogg files read as
            # having no art when they are perfectly well illustrated.
            import base64
            blobs = t['metadata_block_picture']
            n = len(blobs)
            for enc in blobs:
                try:
                    b += len(base64.b64decode(enc))
                except Exception:
                    pass
    except Exception:
        pass
    out['has_art'], out['art_bytes'] = n, b
    return out

# ---------------------------------------------------------------- scan
def art_follow(moves, apply):
    """Fetched-art cache rows and files are keyed by folder; move them along."""
    ac = sqlite3.connect(os.path.join(_HERE, 'artcache.db'))
    artdir = os.path.join(_HERE, 'cache', 'art')
    folder_map = {}
    for s, d, why in moves:
        folder_map[os.path.dirname(s)] = os.path.dirname(d)
    n = 0
    for old, new in folder_map.items():
        r = ac.execute('SELECT local FROM art WHERE folder=?', (old,)).fetchone()
        if not r:
            continue
        n += 1
        if not apply:
            continue
        local = r[0]
        newlocal = new.replace('/', '__') + '.jpg' if local else None
        if local and os.path.exists(os.path.join(artdir, local)):
            os.replace(os.path.join(artdir, local), os.path.join(artdir, newlocal))
        if ac.execute('SELECT 1 FROM art WHERE folder=?', (new,)).fetchone():
            ac.execute('DELETE FROM art WHERE folder=?', (new,))
        ac.execute('UPDATE art SET folder=?, local=? WHERE folder=?', (new, newlocal, old))
    ac.commit(); ac.close()
    return n


def cmd_scan(args):
    conn = db()
    cur = conn.cursor()
    known = {p: (s, m) for p, s, m in
             cur.execute('SELECT path,size,mtime FROM files')}
    roots = args.roots or ['VariousArtists']
    seen, added, updated, skipped = set(), 0, 0, 0
    batch = []
    t0 = time.time()

    for sub in roots:
        base = os.path.join(ROOT, sub)
        if not os.path.isdir(base):
            print(f'  !! missing: {base}', file=sys.stderr); continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in ('$recycle.bin', 'system volume information')
                           and not d.startswith('_superseded')
                           and not d.startswith('_quarantine')]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel  = os.path.relpath(full, ROOT).replace('\\', '/')
                seen.add(rel)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                prev = known.get(rel)
                if prev and prev[0] == st.st_size and abs(prev[1] - st.st_mtime) < 1:
                    skipped += 1
                    continue
                ext  = os.path.splitext(fn)[1].lower()
                kind = 'audio' if ext in AUDIO_EXT else ('image' if ext in IMG_EXT else 'other')
                rec = dict(path=rel, src=rel.split('/')[0], kind=kind,
                           folder=os.path.dirname(rel), filename=fn, ext=ext,
                           size=st.st_size, mtime=st.st_mtime, scanned_at=time.time())
                if kind == 'audio':
                    rec.update(read_tags(full, ext))
                batch.append(rec)
                if prev: updated += 1
                else:    added += 1
                if len(batch) >= 500:
                    _flush(conn, batch); batch.clear()
                    n = added + updated
                    print(f'   {n} scanned ({n/max(1,time.time()-t0):.0f}/s)',
                          file=sys.stderr, flush=True)
    if batch: _flush(conn, batch)

    # files that vanished (moved/deleted since last scan) -- judged only within
    # the roots just walked, so scanning one pool cannot delete another's rows
    walked = tuple(r.rstrip('/') + '/' for r in roots)
    gone = {p for p in known if p.startswith(walked)} - seen
    if gone and not args.keep_missing:
        cur.executemany('DELETE FROM files WHERE path=?', [(g,) for g in gone])
    conn.commit()
    print(f'scan done: +{added} new, ~{updated} changed, {skipped} unchanged, '
          f'-{len(gone)} gone, {time.time()-t0:.0f}s')
    conn.close()

def _flush(conn, batch):
    cols = ['path','src','kind','folder','filename','ext','size','mtime','duration',
            'bitrate','samplerate','channels','codec','brmode','artist','albumartist',
            'album','title','track','tracktotal','disc','disctotal','year','genre',
            'has_art','art_bytes','tag_err','scanned_at']
    # We only get here for files that are new or whose size/mtime changed. But a tag
    # write changes the bytes without touching a single audio sample, and both the
    # fingerprint and the spectral cutoff describe the audio -- so keep them unless
    # the decoded stream itself moved. Duration/samplerate/channels are the tell;
    # bitrate is excluded because VBR figures are derived from file size and shift
    # when tags grow.
    same_audio = ("files.duration IS excluded.duration AND "
                  "files.samplerate IS excluded.samplerate AND "
                  "files.channels IS excluded.channels")
    keep = lambda c: f"{c}=CASE WHEN {same_audio} THEN files.{c} ELSE NULL END"
    q = f"INSERT INTO files ({','.join(cols)}) VALUES ({','.join('?'*len(cols))}) " \
        f"ON CONFLICT(path) DO UPDATE SET " + \
        ','.join(f'{c}=excluded.{c}' for c in cols if c != 'path') + \
        ", " + ", ".join(keep(c) for c in ('fp', 'fp_dur', 'fp_err', 'cutoff'))
    conn.executemany(q, [[r.get(c) for c in cols] for r in batch])
    conn.commit()

# ---------------------------------------------------------------- fingerprint
import array, zlib

def pack_fp(ints):
    """uint32 list -> compact blob."""
    return zlib.compress(array.array('I', ints).tobytes(), 6)

def unpack_fp(blob):
    """blob -> array of uint32."""
    a = array.array('I'); a.frombytes(zlib.decompress(blob)); return a

def _fp_one(rel):
    """Raw chromaprint vector: comparable across encodings by bit-error-rate,
    which the compressed AcoustID string is not."""
    full = os.path.join(ROOT, rel.replace('/', os.sep))
    try:
        p = subprocess.run([FPCALC, '-raw', '-length', '120', full],
                           capture_output=True, text=True, timeout=180)
        if p.returncode != 0:
            return rel, None, None, (p.stderr or 'fpcalc failed').strip()[:150]
        dur = raw = None
        for line in p.stdout.splitlines():
            if line.startswith('DURATION='):      dur = int(float(line[9:]))
            elif line.startswith('FINGERPRINT='): raw = line[12:]
        if not raw:
            return rel, None, None, 'no fingerprint returned'
        ints = [int(x) for x in raw.split(',') if x]
        if len(ints) < 10:
            return rel, None, dur, f'too short ({len(ints)} frames)'
        return rel, pack_fp(ints), dur, None
    except subprocess.TimeoutExpired:
        return rel, None, None, 'timeout'
    except Exception as e:
        return rel, None, None, f'{type(e).__name__}: {str(e)[:120]}'

def cmd_fingerprint(args):
    conn = db()
    sql = ("SELECT path FROM files WHERE kind='audio' AND fp IS NULL "
           "AND (fp_err IS NULL OR ?)")
    params = [1 if args.retry else 0]
    if args.src:
        sql += " AND src=?"; params.append(args.src)
    todo = [r[0] for r in conn.execute(sql + " ORDER BY path", params)]
    if args.limit: todo = todo[:args.limit]
    print(f'fingerprinting {len(todo)} files with {args.workers} workers')
    t0, done, batch = time.time(), 0, []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for rel, fp, dur, err in ex.map(_fp_one, todo):
            batch.append((fp, dur, err, rel)); done += 1
            if len(batch) >= 200:
                conn.executemany('UPDATE files SET fp=?,fp_dur=?,fp_err=? WHERE path=?', batch)
                conn.commit(); batch.clear()
                el = time.time() - t0
                print(f'   {done}/{len(todo)}  {done/max(el,1):.1f}/s  '
                      f'eta {(len(todo)-done)/max(done/max(el,1),.01)/60:.0f}m',
                      file=sys.stderr, flush=True)
    if batch:
        conn.executemany('UPDATE files SET fp=?,fp_dur=?,fp_err=? WHERE path=?', batch)
    conn.commit()
    nerr = conn.execute("SELECT COUNT(*) FROM files WHERE fp_err IS NOT NULL").fetchone()[0]
    print(f'fingerprint done: {done} in {time.time()-t0:.0f}s, {nerr} errors')
    conn.close()

# ---------------------------------------------------------------- moving
QUARANTINE = '_quarantine'   # under ROOT, sibling of the libraries
SUPERSEDED = '_superseded'

def _move(conn, rel, destroot, op, reason, dry):
    """Move ROOT/rel -> ROOT/destroot/rel, logging it so it can be undone."""
    src = os.path.join(ROOT, rel.replace('/', os.sep))
    dst_rel = f'{destroot}/{rel}'
    dst = os.path.join(ROOT, dst_rel.replace('/', os.sep))
    if dry:
        return dst_rel
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):                       # never clobber
        b, e = os.path.splitext(dst); i = 2
        while os.path.exists(f'{b}__{i}{e}'): i += 1
        dst = f'{b}__{i}{e}'
        dst_rel = os.path.relpath(dst, ROOT).replace('\\', '/')
    try:
        size = os.path.getsize(src)
    except OSError:
        size = None
    os.replace(src, dst)
    conn.execute('INSERT INTO moves (ts,op,reason,src,dst,size) VALUES (?,?,?,?,?,?)',
                 (time.time(), op, reason, rel, dst_rel, size))
    conn.execute('DELETE FROM files WHERE path=?', (rel,))
    return dst_rel

def _move_to(conn, rel, dst_rel, op, reason, dry=False):
    """Move ROOT/rel to an explicit ROOT/dst_rel, logging it so it can be undone.

    _move() quarantines into a holding root, preserving the path. This is for
    relocating a file INTO another album folder, where the destination is
    chosen rather than derived."""
    src = os.path.join(ROOT, rel.replace('/', os.sep))
    dst = os.path.join(ROOT, dst_rel.replace('/', os.sep))
    if dry:
        return dst_rel
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):                       # never clobber
        b, e = os.path.splitext(dst); i = 2
        while os.path.exists(f'{b}__{i}{e}'): i += 1
        dst = f'{b}__{i}{e}'
        dst_rel = os.path.relpath(dst, ROOT).replace('\\', '/')
    try:
        size = os.path.getsize(src)
    except OSError:
        size = None
    os.replace(src, dst)
    conn.execute('INSERT INTO moves (ts,op,reason,src,dst,size) VALUES (?,?,?,?,?,?)',
                 (time.time(), op, reason, rel, dst_rel, size))
    conn.execute('UPDATE files SET path=?, folder=?, filename=? WHERE path=?',
                 (dst_rel, os.path.dirname(dst_rel), os.path.basename(dst_rel), rel))
    return dst_rel

OS_JUNK = {'thumbs.db', 'desktop.ini', '.ds_store', '.directory', 'albumart.ini'}

def cmd_sweep(args):
    """Quarantine OS turds and Windows-Media art caches. Non-destructive."""
    conn = db()
    plan = []          # (rel, reason)

    for rel, fn in conn.execute(
            "SELECT path,filename FROM files WHERE kind='other'").fetchall():
        if fn.lower() in OS_JUNK:
            plan.append((rel, 'os-artifact'))

    # Windows Media art caches -- only where a real cover survives in the folder
    imgs = conn.execute(
        "SELECT path,folder,filename,size FROM files WHERE kind='image'").fetchall()
    byfolder = {}
    for p, f, n, s in imgs:
        byfolder.setdefault(f, []).append((p, n, s))
    for folder, items in byfolder.items():
        real = [(p, n, s) for p, n, s in items
                if not n.lower().startswith('albumart')]
        if not real:
            continue                       # nothing else to fall back on: keep
        best_real = max(s for _, _, s in real)
        for p, n, s in items:
            ln = n.lower()
            if not ln.startswith('albumart'):
                continue
            if ln == 'albumartsmall.jpg' or ln.endswith('_small.jpg'):
                plan.append((p, 'wmp-art-thumbnail'))
            elif s <= best_real:
                plan.append((p, 'wmp-art-cache'))
            # a WMP cache LARGER than every real cover stays put

    seen, uniq = set(), []
    for rel, why in plan:
        if rel not in seen:
            seen.add(rel); uniq.append((rel, why))

    tot = sum(1 for _ in uniq)
    bysize = conn.execute(
        f"SELECT COALESCE(SUM(size),0) FROM files WHERE path IN "
        f"({','.join('?'*len(uniq))})", [r for r, _ in uniq]).fetchone()[0] if uniq else 0
    byreason = {}
    for _, w in uniq: byreason[w] = byreason.get(w, 0) + 1
    print(f'{"DRY RUN: would quarantine" if args.dry_run else "quarantining"} '
          f'{tot} files ({bysize/1e9:.2f} GB) -> {QUARANTINE}/')
    for w, n in sorted(byreason.items()): print(f'    {w:22} {n}')
    if args.dry_run:
        for rel, why in uniq[:15]: print(f'      e.g. {why:22} {rel}')
        conn.close(); return
    n = 0
    for rel, why in uniq:
        try:
            _move(conn, rel, QUARANTINE, 'sweep', why, False); n += 1
        except OSError as e:
            print(f'   !! {rel}: {e}', file=sys.stderr)
        if n % 1000 == 0: conn.commit(); print(f'   {n}/{tot}', file=sys.stderr, flush=True)
    conn.commit()
    print(f'quarantined {n} files. Undo with:  python musiclib.py undo --op sweep')
    conn.close()

def cmd_undo(args):
    conn = db()
    rows = conn.execute('SELECT id,src,dst FROM moves WHERE op=? ORDER BY id DESC',
                        (args.op,)).fetchall()
    print(f'{"DRY RUN: would restore" if args.dry_run else "restoring"} {len(rows)} files '
          f'from op={args.op}')
    if args.dry_run:
        for _, s, d in rows[:10]: print(f'   {d}  ->  {s}')
        conn.close(); return
    n = 0
    for mid, s, d in rows:
        src = os.path.join(ROOT, d.replace('/', os.sep))
        dst = os.path.join(ROOT, s.replace('/', os.sep))
        if not os.path.exists(src): continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
        conn.execute('DELETE FROM moves WHERE id=?', (mid,)); n += 1
    conn.commit()
    print(f'restored {n} files -- re-run "scan" to put them back in the index')
    conn.close()

# ---------------------------------------------------------------- stats
def cmd_stats(args):
    conn = db()
    q = lambda s, *a: conn.execute(s, a).fetchall()
    print('=== files by library / kind ===')
    for src, kind, n, b in q("SELECT src,kind,COUNT(*),SUM(size) FROM files "
                             "GROUP BY src,kind ORDER BY src,kind"):
        print(f'  {src:18} {kind:6} {n:7}  {(b or 0)/1e9:8.2f} GB')
    print('\n=== audio by extension ===')
    for src, ext, n, br in q("SELECT src,ext,COUNT(*),AVG(bitrate) FROM files "
                             "WHERE kind='audio' GROUP BY src,ext ORDER BY src,COUNT(*) DESC"):
        print(f'  {src:18} {ext:7} {n:7}  avg {(br or 0)/1000:6.0f} kbps')
    print('\n=== tag completeness (audio) ===')
    tot = q("SELECT COUNT(*) FROM files WHERE kind='audio'")[0][0] or 1
    for c in ('artist','albumartist','album','title','track','tracktotal','disc','year','genre'):
        n = q(f"SELECT COUNT(*) FROM files WHERE kind='audio' AND {c} IS NOT NULL "
              f"AND {c}!=''")[0][0]
        print(f'  {c:12} {n:7} ({100*n/tot:5.1f}%)')
    n = q("SELECT COUNT(*) FROM files WHERE kind='audio' AND has_art>0")[0][0]
    print(f'  {"embedded art":12} {n:7} ({100*n/tot:5.1f}%)')
    print('\n=== fingerprint coverage ===')
    fp  = q("SELECT COUNT(*) FROM files WHERE kind='audio' AND fp IS NOT NULL")[0][0]
    er  = q("SELECT COUNT(*) FROM files WHERE kind='audio' AND fp_err IS NOT NULL")[0][0]
    print(f'  {fp}/{tot} fingerprinted, {er} errors')
    te = q("SELECT COUNT(*) FROM files WHERE tag_err IS NOT NULL")[0][0]
    print(f'  {te} tag read errors')
    print('\n=== moves recorded ===')
    for op, n, b in q("SELECT op,COUNT(*),SUM(size) FROM moves GROUP BY op"):
        print(f'  {op:20} {n:7}  {(b or 0)/1e9:.2f} GB')
    conn.close()

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest='cmd', required=True)
    s = sp.add_parser('scan');        s.add_argument('roots', nargs='*')
    s.add_argument('--keep-missing', action='store_true'); s.set_defaults(f=cmd_scan)
    s = sp.add_parser('fingerprint'); s.add_argument('--workers', type=int, default=8)
    s.add_argument('--limit', type=int); s.add_argument('--retry', action='store_true')
    s.add_argument('--src'); s.set_defaults(f=cmd_fingerprint)
    s = sp.add_parser('sweep');       s.add_argument('--dry-run', action='store_true')
    s.set_defaults(f=cmd_sweep)
    s = sp.add_parser('undo');        s.add_argument('--op', required=True)
    s.add_argument('--dry-run', action='store_true'); s.set_defaults(f=cmd_undo)
    s = sp.add_parser('stats');       s.set_defaults(f=cmd_stats)
    a = ap.parse_args(); a.f(a)

def prune_empty_dirs(base):
    """Remove directories left empty by a move.

    Folders that Explorer customised carry the read-only attribute, and Windows
    refuses to rmdir those even when empty -- clear it first.
    """
    import ctypes
    FILE_ATTRIBUTE_NORMAL = 0x80
    removed = 0
    for dp, dns, fns in os.walk(base, topdown=False):
        try:
            if os.listdir(dp):
                continue
            ctypes.windll.kernel32.SetFileAttributesW(dp, FILE_ATTRIBUTE_NORMAL)
            os.rmdir(dp)
            removed += 1
        except OSError:
            pass
    return removed
