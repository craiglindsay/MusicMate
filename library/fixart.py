"""Replace one album's fetched cover with the right one, by hand.

artfetch.py asks Deezer and iTunes by artist + album title, and a title-only
hit can win when the artist tag is wrong ("A View To A Kill" credited to a rap
group) or when a generic title matches something else ("Electronic" ->
"Electronic dystopia"). This looks the record up on MusicBrainz instead, takes
the Cover Art Archive front image for that release group, writes it into the
art cache under the folder's key, and records the hit in artcache.db so etl.py
picks it up on the next rebuild.

    python fixart.py "VariousArtists/Electronic/Electronic"
    python fixart.py "<folder>" --artist "Duran Duran" --album "A View to a Kill"
    python fixart.py "<folder>" --embed        # also replace the picture inside the files

Folders are relative to D:\\Music with forward slashes, as everywhere else in
these scripts. --embed only touches files under VariousArtists.
"""
import os, io, re, sys, json, time, sqlite3, argparse, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get('MUSICMATE_ROOT') or r'D:\Music'
ARTDIR = os.path.join(HERE, 'cache', 'art')
CACHE = os.path.join(HERE, 'artcache.db')
UA = {'User-Agent': 'MusicMate/1.0 (personal library; fixart)'}
ALLOWED = ('VariousArtists/',)


def get(url, timeout=30):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def mb_release_groups(artist, album):
    q = 'release:"%s" AND artist:"%s"' % (album.replace('"', ''), artist.replace('"', ''))
    u = 'https://musicbrainz.org/ws/2/release-group/?query=' + urllib.parse.quote(q) + '&fmt=json&limit=10'
    d = json.loads(get(u))
    out = []
    for rg in d.get('release-groups', []):
        names = [a.get('name', '') for a in rg.get('artist-credit', []) if isinstance(a, dict)]
        out.append({'id': rg['id'], 'title': rg.get('title', ''), 'type': rg.get('primary-type') or '',
                    'date': rg.get('first-release-date', ''), 'artist': ', '.join(names), 'score': rg.get('score', 0)})
    return out


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def pick(cands, artist, album):
    """Exact title and artist first; albums before singles; then MusicBrainz's own score."""
    want_t, want_a = norm(album), norm(artist)
    def key(c):
        return (norm(c['title']) != want_t, want_a not in norm(c['artist']),
                {'Album': 0, 'EP': 1, 'Single': 2}.get(c['type'], 3), -c['score'])
    import artfetch
    good = [c for c in cands if artfetch.artist_ok(artist, c['artist'])]
    return sorted(good, key=key)[0] if good else None


def download_front(rgid):
    from PIL import Image
    for size in ('front-1200', 'front-500'):
        try:
            data = get('https://coverartarchive.org/release-group/%s/%s' % (rgid, size))
        except Exception:
            continue
        im = Image.open(io.BytesIO(data)).convert('RGB')
        return im
    return None


def embed(folder_abs, jpeg_bytes):
    import mutagen
    from mutagen.id3 import APIC
    from mutagen.mp4 import MP4Cover
    n = 0
    for fn in sorted(os.listdir(folder_abs)):
        p = os.path.join(folder_abs, fn)
        a = mutagen.File(p)
        if a is None or a.tags is None:
            continue
        try:
            if hasattr(a.tags, 'delall'):                      # ID3
                a.tags.delall('APIC')
                a.tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=jpeg_bytes))
            elif 'covr' in a.tags or fn.lower().endswith(('.m4a', '.mp4')):
                a.tags['covr'] = [MP4Cover(jpeg_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
            elif hasattr(a, 'pictures'):                       # FLAC
                from mutagen.flac import Picture
                a.clear_pictures()
                pic = Picture(); pic.type = 3; pic.mime = 'image/jpeg'; pic.data = jpeg_bytes
                a.add_picture(pic)
            else:
                continue
            a.save(); n += 1
        except Exception as e:
            print('   could not embed in', fn, e)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('folder', help='library-relative folder, forward slashes')
    ap.add_argument('--artist'); ap.add_argument('--album')
    ap.add_argument('--embed', action='store_true', help='also replace the picture inside the files')
    ap.add_argument('--rg', help='use this MusicBrainz release-group id instead of searching')
    a = ap.parse_args()
    folder = a.folder.strip('/').replace('\\', '/')

    lib = sqlite3.connect(os.path.join(HERE, 'library.db'))
    meta = lib.execute('SELECT albumartist, artist, album FROM files WHERE folder=? AND kind="audio" '
                       'AND album IS NOT NULL LIMIT 1', (folder,)).fetchone()
    artist = a.artist or (meta and (meta[0] or meta[1])) or folder.split('/')[1]
    album = a.album or (meta and meta[2]) or folder.split('/')[-1]
    print('looking up', artist, '-', album)

    if a.rg:
        chosen = {'id': a.rg, 'title': album, 'artist': artist, 'type': '?', 'date': ''}
    else:
        cands = mb_release_groups(artist, album)
        for c in cands:
            print('   ', c['type'] or '-', c['date'][:4], c['artist'], '|', c['title'])
        chosen = pick(cands, artist, album)
        if not chosen:
            print('no MusicBrainz release group matched that artist; try --artist/--album or --rg')
            return 1
    print('using', chosen['type'], chosen['date'][:4], chosen['artist'], '-', chosen['title'], chosen['id'])

    im = download_front(chosen['id'])
    if im is None:
        print('the Cover Art Archive has no front image for that release group')
        return 1
    rel = folder.replace('/', '__') + '.jpg'
    dest = os.path.join(ARTDIR, rel)
    im.save(dest, 'JPEG', quality=92)
    print('wrote', rel, im.width, 'px')

    ac = sqlite3.connect(CACHE)
    ac.execute('INSERT OR IGNORE INTO art (folder) VALUES (?)', (folder,))
    ac.execute('UPDATE art SET want=1,hit=1,score=1.0,mb_album=?,mb_artist=?,url=?,local=?,new_w=?,'
               'reason=NULL,ts=? WHERE folder=?',
               (chosen['title'], chosen['artist'], 'caa:' + chosen['id'], rel, im.width, time.time(), folder))
    ac.commit()

    if a.embed:
        if not folder.startswith(ALLOWED):
            print('not embedding: only VariousArtists may be modified')
        else:
            buf = io.BytesIO(); im.save(buf, 'JPEG', quality=90)
            n = embed(os.path.join(ROOT, folder.replace('/', os.sep)), buf.getvalue())
            print('embedded in', n, 'files')
    print('done; run jukebox/etl.py if the album had no fetched art before (the path is new)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
