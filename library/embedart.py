"""Put the cover art inside the files, where it belongs.

folder.jpg is a Windows Media Player convention and these are its 200x200 cache
thumbnails, not cover art. Embedded art is what every modern player, phone and car
stereo reads, and it travels with the file when it is copied anywhere.

Size is a deliberate compromise: a full 1000px cover in every track would add ~9 GB
across 35,919 files, fifteen identical copies per album. 600px at q88 is ~80 KB,
sharp on a phone or a car screen, and the jukebox keeps the full-resolution original
in its own cache for the big screen.
"""
import os, io, sys, time, sqlite3, argparse, collections
from PIL import Image
import base64
import mutagen, mutagen.id3, mutagen.mp4, mutagen.flac, mutagen.ogg
import musiclib, artfetch

ROOT = musiclib.ROOT
EMBED_PX = 600
QUALITY = 88

def render(src_path, px=EMBED_PX):
    """Downsample a cached cover to the size we embed. Returns JPEG bytes."""
    try:
        with Image.open(src_path) as im:
            im = im.convert('RGB')
            if max(im.size) > px:
                im.thumbnail((px, px), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, 'JPEG', quality=QUALITY, optimize=True, progressive=True)
            return buf.getvalue()
    except Exception:
        return None

def set_art(full, data):
    """Attach a JPEG to one file, replacing whatever art it had."""
    try:
        a = mutagen.File(full)
        if a is None:
            return False
        if isinstance(a, mutagen.flac.FLAC):
            a.clear_pictures()
            p = mutagen.flac.Picture()
            p.type, p.mime, p.data = 3, 'image/jpeg', data
            a.add_picture(p)
        elif isinstance(a, mutagen.mp4.MP4):
            a['covr'] = [mutagen.mp4.MP4Cover(data, imageformat=mutagen.mp4.MP4Cover.FORMAT_JPEG)]
        elif isinstance(a, mutagen.ogg.OggFileType):
            # Vorbis comments have no picture field: the art is a FLAC picture
            # block, base64-encoded into METADATA_BLOCK_PICTURE. Without this
            # branch Ogg files fall through to the ID3 path and silently fail.
            pic = mutagen.flac.Picture()
            pic.type, pic.mime, pic.data = 3, 'image/jpeg', data
            try:
                from PIL import Image as _I
                with _I.open(io.BytesIO(data)) as _im:
                    pic.width, pic.height, pic.depth = _im.size[0], _im.size[1], 24
            except Exception:
                pass
            a['metadata_block_picture'] = [
                base64.b64encode(pic.write()).decode('ascii')]
        else:
            if a.tags is None:
                a.add_tags()
            if not hasattr(a.tags, 'delall'):
                return False
            a.tags.delall('APIC')
            a.tags.add(mutagen.id3.APIC(encoding=3, mime='image/jpeg', type=3,
                                        desc='Cover', data=data))
        a.save()
        return True
    except Exception:
        return False

def best_source(conn, acdb, folder):
    """The best cover available for this album: the fetched one, else what is on disk."""
    row = acdb.execute('SELECT local,new_w FROM art WHERE folder=? AND hit=1',
                       (folder,)).fetchone()
    if row and row[0]:
        p = os.path.join(artfetch.ARTDIR, row[0])
        if os.path.exists(p):
            return p, row[1], 'fetched'
    best, bp = 0, None
    for (rel,) in conn.execute(
            'SELECT path FROM files WHERE folder=? AND kind="image"', (folder,)):
        p = os.path.join(ROOT, rel.replace('/', os.sep))
        w = artfetch.img_size(p)
        if w > best:
            best, bp = w, p
    return (bp, best, 'sidecar') if bp else (None, 0, 'none')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--min-gain', type=int, default=1,
                    help='only re-embed when the new art beats the old by this factor')
    a = ap.parse_args()
    conn = musiclib.db(); conn.row_factory = sqlite3.Row
    acdb = artfetch.cache_db()

    folders = [r[0] for r in conn.execute(
        'SELECT DISTINCT folder FROM files WHERE kind="audio" ORDER BY folder')]
    if a.limit: folders = folders[:a.limit]

    stats = collections.Counter()
    done = errs = 0
    t0 = time.time()
    for i, folder in enumerate(folders):
        src, w, kind = best_source(conn, acdb, folder)
        if not src:
            stats['no art available'] += 1; continue
        cur = acdb.execute('SELECT cur_w FROM art WHERE folder=?', (folder,)).fetchone()
        cur_w = (cur[0] if cur else 0) or 0
        # nothing to gain from re-embedding art no better than what is already there
        if kind == 'sidecar' and w <= cur_w and cur_w >= EMBED_PX:
            stats['already good enough'] += 1; continue
        stats[f'source: {kind}'] += 1
        if not a.apply:
            continue
        data = render(src)
        if not data:
            stats['render failed'] += 1; continue
        for (rel,) in conn.execute(
                'SELECT path FROM files WHERE folder=? AND kind="audio"', (folder,)):
            full = os.path.join(ROOT, rel.replace('/', os.sep))
            if set_art(full, data):
                done += 1
            else:
                errs += 1
        if (i + 1) % 100 == 0:
            el = time.time() - t0
            print(f'   {i+1}/{len(folders)} folders, {done} tracks, '
                  f'eta {(len(folders)-i-1)*el/(i+1)/60:.0f}m', flush=True)

    print(f'\n{"APPLIED" if a.apply else "DRY RUN"}')
    for k, v in stats.most_common(): print(f'   {k:26} {v}')
    if a.apply:
        print(f'   tracks re-embedded at {EMBED_PX}px: {done}, failures {errs}')
    conn.close(); acdb.close()

if __name__ == '__main__':
    main()
