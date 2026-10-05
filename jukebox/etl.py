"""Build jukebox.db -- the read-model the player actually runs on.

Deliberately NOT pointed at library.db. That database is 377 MB of mostly
Chromaprint blobs, it is snapshotted and bulk-rewritten by the maintenance scripts
(library.db.pre-merge, .pre-canonize, .pre-convert ...), and WAL will not save a
reader from having the file replaced underneath it. So: read it once, read-only,
and derive a small purpose-built database from it.

This is also where the messy parts of the raw data get resolved once instead of on
every request: genre shelves, decade buckets, artists merged across the two source
trees, and -- importantly for Sonos -- the MIME type resolved from the CODEC rather
than the file extension.
"""
import os, re, sys, json, sqlite3, time, collections, argparse, hashlib
from pathlib import Path
import genremap

HERE = os.path.dirname(os.path.abspath(__file__))
# The library-organising toolchain and its databases live in ../library
SCRATCH = os.path.join(os.path.dirname(HERE), 'library')
SRC_DB = os.path.join(SCRATCH, 'library.db')
ART_DB = os.path.join(SCRATCH, 'artcache.db')
OUT_DB = os.path.join(HERE, 'jukebox.db')
ROOT = os.environ.get('MUSICMATE_ROOT') or r'D:\Music'
# The main library is one top-level folder; every other folder that has been
# scanned is a sub-library of its own (Compilations, Soundtracks, Christmas...),
# shown on Home as a card and browsed on its own page, never mixed into the
# main Artists/Albums/Genres/Eras (user's decision, 2 Sep 2026). Artist and
# genre rollups therefore count the main pool only; all_* columns count everything.
MAIN_POOL = 'VariousArtists'

sys.path.insert(0, SCRATCH)
import merge as mergemod

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS artist (
  id INTEGER PRIMARY KEY, name TEXT, sort_name TEXT, norm TEXT,
  mbid TEXT, n_albums INTEGER, n_tracks INTEGER, duration REAL, art_album_id INTEGER,
  all_albums INTEGER, all_tracks INTEGER
);
CREATE TABLE IF NOT EXISTS genre (
  id INTEGER PRIMARY KEY, shelf TEXT UNIQUE, n_albums INTEGER, n_tracks INTEGER
);
CREATE TABLE IF NOT EXISTS subgenre (
  id INTEGER PRIMARY KEY, genre_id INTEGER, name TEXT, n_albums INTEGER
);
CREATE TABLE IF NOT EXISTS album (
  id INTEGER PRIMARY KEY, artist_id INTEGER, folder TEXT UNIQUE, title TEXT,
  sort_title TEXT, year INTEGER, decade INTEGER, genre_id INTEGER, subgenre TEXT,
  n_tracks INTEGER, duration REAL, disc_count INTEGER,
  art_w INTEGER, art_path TEXT, art_kind TEXT, added REAL, pool TEXT,
  is_compilation INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS track (
  id INTEGER PRIMARY KEY, album_id INTEGER, artist_id INTEGER,
  disc INTEGER, track INTEGER, title TEXT, track_artist TEXT,
  duration REAL, bitrate INTEGER, ext TEXT, mime TEXT, size INTEGER,
  rel_path TEXT, sort_key TEXT, skey TEXT, has_art INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_track_skey ON track(skey);
CREATE INDEX IF NOT EXISTS ix_album_artist ON album(artist_id);
CREATE INDEX IF NOT EXISTS ix_album_genre  ON album(genre_id);
CREATE INDEX IF NOT EXISTS ix_album_decade ON album(decade);
CREATE INDEX IF NOT EXISTS ix_track_album  ON track(album_id);
CREATE INDEX IF NOT EXISTS ix_artist_norm  ON artist(norm);
"""

# Sonos and every browser decide what they can play from the Content-Type header.
# Deriving it from the extension is wrong here: seven files in this library are MP3
# streams that happen to be named .m4a, and they fail silently when announced as MP4.
MIME_BY_CODEC = {
    'MPEGInfo': 'audio/mpeg',
    'MP4Info': 'audio/mp4',
    'StreamInfo': 'audio/flac',
    'OggVorbisInfo': 'audio/ogg',
    'ASFInfo': 'audio/x-ms-wma',
    'MusepackInfo': 'audio/x-musepack',
    'OpusInfo': 'audio/ogg',
}
MIME_BY_EXT = {'.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.flac': 'audio/flac',
               '.ogg': 'audio/ogg', '.wav': 'audio/wav', '.wma': 'audio/x-ms-wma'}

def mime_for(codec, ext):
    return MIME_BY_CODEC.get(codec) or MIME_BY_EXT.get((ext or '').lower(), 'audio/mpeg')

def parse_year(raw):
    if not raw:
        return None
    m = re.match(r'\s*(\d{4})', str(raw))
    if not m:
        return None
    y = int(m.group(1))
    # 21 tracks are tagged 2027; a year in the future is a typo, not a release date
    return y if 1900 <= y <= time.localtime().tm_year else None

def stable_key(artist, title, duration):
    """An identity for a recording that survives things the integer id does not.

    Track ids are regenerated on every ETL rebuild, and file paths move -- 28,000
    files moved during the library cleanup alone. Favourites and play history keyed
    on either would silently detach from their music. Artist + title + duration
    rounded to five seconds survives both, and survives most retagging too.
    """
    a = re.sub(r'[^a-z0-9]', '', (artist or '').lower())
    t = re.sub(r'[^a-z0-9]', '', (title or '').lower())
    d = int(round((duration or 0) / 5.0))
    return hashlib.sha1(f'{a}|{t}|{d}'.encode()).hexdigest()[:16]

def sort_title(s):
    """Lowercase, article-stripped key used for ordering and the A-Z index.

    Case matters here: 'B' is not BETWEEN 'a' AND 'z' in SQLite, so a properly
    capitalised sort name silently lands in the '#' bucket and vanishes from the
    alphabet rail.
    """
    s = re.sub(r'^(the|a|an)\s+', '', (s or '').lower()).strip()
    return re.sub(r'^[^a-z0-9]+', '', s) or (s or '')

KEEP_BACKUPS = 5
CARRIED = None


def rotate_backups(db):
    """Keep the last few read models so a bad rebuild is recoverable.

    This exists because a stale WAL replayed onto a restored copy corrupted the
    database once. Backups are taken with sqlite's own backup API rather than a
    file copy, precisely so an open WAL cannot poison them."""
    import sqlite3 as _s
    db = Path(db)
    if not db.exists():
        return None
    for i in range(KEEP_BACKUPS - 1, 0, -1):
        older, newer = db.with_suffix('.db.bak%d' % (i + 1)), db.with_suffix('.db.bak%d' % i)
        if newer.exists():
            if older.exists():
                older.unlink()
            newer.rename(older)
    dest = db.with_suffix('.db.bak1')
    try:
        src = _s.connect(f'file:{db}?mode=ro', uri=True)
        dst = _s.connect(dest)
        src.backup(dst)                     # consistent even with a live WAL
        dst.close(); src.close()
        return dest
    except Exception as e:
        print(f'   backup skipped: {e}')
        return None


def album_added(tracks):
    """When this record arrived: the earliest CREATION time among its files.

    Modification time is useless for this -- every tag rewrite bumps it, so the
    day's retagging made forty albums look brand new. Windows keeps creation
    time through copies (robocopy preserved it in the migration) and through
    tag writes and renames, so the Cure's Greatest Hits still says 2012. Falls
    back to the library's mtime when a file cannot be stat'ed."""
    best = None
    for t in tracks:
        try:
            st_ = os.stat(os.path.join(ROOT, t['path'].replace('/', os.sep)))
            c = min(st_.st_ctime, st_.st_mtime)      # a copy can reset creation; take the older
        except OSError:
            c = t['mtime'] or None
        if c and (best is None or c < best):
            best = c
    return best


VA_NAME = 'Various Artists'


def album_credit(folder, tracks, names, pool):
    """Who an album is credited to, and whether it is a compilation.

    The tags decide when they can: any albumartist tag is trusted. Without
    one, a folder whose tracks name four or more performers with nobody holding
    half of them is a compilation -- the Billboard year folders under Era are
    the case in point (2 Sep 2026). Crediting such a folder to whichever singer
    had two hits that year is wrong, and so is turning each of those singers
    into an artist with an album. In the main pool the folder tree is
    Artist/Album, so a multi-performer folder is a DJ mix or a tribute filed
    under its maker and the folder artist is credited. Elsewhere the album
    becomes Various Artists and every track keeps its own performer.
    -> (name, is_compilation)"""
    top, n = names.most_common(1)[0]
    # "Various", "Various Artists", "VA": one artist, however it was spelled
    if re.sub(r'[^a-z]', '', (top or '').lower()) in ('various', 'variousartists', 'va', 'variousartist'):
        return VA_NAME, True
    # an albumartist tag is trusted when most tracks carry one; one tagged
    # track in thirty (a year folder where one download was tagged) is not
    tagged = sum(1 for t in tracks if t['albumartist'])
    if tagged * 2 > len(tracks) or len(names) < 4 or n / float(len(tracks)) > 0.5:
        return top, False
    parts = folder.split('/')
    if pool == MAIN_POOL and len(parts) >= 3:
        return parts[1], False
    return VA_NAME, True


def carry_over(db):
    """Rows this ETL does not produce but must not destroy (artist photos/bios)."""
    import sqlite3 as _s
    db = Path(db)
    if not db.exists():
        return None
    try:
        c = _s.connect(f'file:{db}?mode=ro', uri=True)
        c.row_factory = _s.Row
        rows = [dict(r) for r in c.execute('SELECT * FROM artist_info')]
        c.close()
        return rows
    except Exception:
        return None


def restore_carried(db, rows):
    import sqlite3 as _s
    if not rows:
        return 0
    c = _s.connect(db, timeout=60)
    cols = [d[1] for d in c.execute('PRAGMA table_info(artist_info)')]
    if not cols:
        c.execute("""CREATE TABLE artist_info (artist_id INTEGER PRIMARY KEY, name TEXT,
          wiki_title TEXT, bio TEXT, bio_url TEXT, image_url TEXT, image_file TEXT,
          status TEXT, fetched REAL, bio_status TEXT, img_status TEXT)""")
        cols = [d[1] for d in c.execute('PRAGMA table_info(artist_info)')]
    # ids are reassigned by a rebuild, so re-key on the artist NAME
    live = {n: i for i, n in c.execute('SELECT id, name FROM artist')}
    keep = [r for r in rows if r.get('name') in live]
    for r in keep:
        r['artist_id'] = live[r['name']]
    use = [c_ for c_ in cols if c_ in (keep[0].keys() if keep else [])]
    c.executemany('INSERT OR REPLACE INTO artist_info (%s) VALUES (%s)'
                  % (','.join(use), ','.join('?' * len(use))),
                  [[r.get(k) for k in use] for r in keep])
    c.commit(); c.close()
    return len(keep)


# artistextra.py fills these from Deezer (top tracks and related artists for
# artist radio). Like artist_info they are keyed by artist id, which a rebuild
# reassigns, so they are carried across on the artist NAME. Losing them cost a
# day of "no such table: artist_top" errors on 1 Sep 2026.
EXTRA_TABLES = ('artist_top', 'artist_similar', 'artist_dz')
EXTRA = None


def carry_extra(db):
    import sqlite3 as _s
    db = Path(db)
    if not db.exists():
        return None
    out = {}
    try:
        c = _s.connect(f'file:{db}?mode=ro', uri=True)
        c.row_factory = _s.Row
        for t in EXTRA_TABLES:
            try:
                out[t] = [dict(r) for r in c.execute(
                    f'SELECT t.*, artist.name AS _name FROM {t} t JOIN artist ON artist.id=t.artist_id')]
            except Exception:
                pass
        c.close()
    except Exception:
        return None
    return {k: v for k, v in out.items() if v} or None


def restore_extra(db, data):
    import sqlite3 as _s
    if not data:
        return 0
    from artistextra import SCHEMA as _extra_schema
    c = _s.connect(db, timeout=60)
    c.executescript(_extra_schema)
    live = {n: i for i, n in c.execute('SELECT id, name FROM artist')}
    n = 0
    for t, rows in data.items():
        cols = [d[1] for d in c.execute(f'PRAGMA table_info({t})')]
        keep = []
        for r in rows:
            aid = live.get(r.pop('_name'))
            if aid is None:
                continue
            r['artist_id'] = aid
            keep.append([r.get(k) for k in cols])
        c.executemany(f'INSERT OR REPLACE INTO {t} ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})', keep)
        n += len(keep)
    c.commit(); c.close()
    return n


def main():
    global CARRIED, EXTRA
    b = rotate_backups(OUT_DB)
    if b:
        print(f'   backup: previous read model saved as {os.path.basename(str(b))}')
    CARRIED = carry_over(OUT_DB)
    if CARRIED:
        print(f'   carrying {len(CARRIED)} artist photo/bio records across the rebuild')
    EXTRA = carry_extra(OUT_DB)
    if EXTRA:
        print('   carrying artist radio data across the rebuild: '
              + ', '.join(f'{k} {len(v)}' for k, v in EXTRA.items()))
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=OUT_DB)
    a = ap.parse_args()

    if os.path.exists(a.out):
        os.remove(a.out)
    for suffix in ('-wal', '-shm'):
        p = a.out + suffix
        if os.path.exists(p):
            os.remove(p)

    src = sqlite3.connect(f'file:{SRC_DB}?mode=ro', uri=True, timeout=60)
    src.row_factory = sqlite3.Row
    src.execute('PRAGMA query_only=1')
    # artcache.db only exists once library/artfetch.py has run; a fresh install
    # has none, so stand in an empty table and use the art embedded in the files
    if os.path.exists(ART_DB):
        art = sqlite3.connect(f'file:{ART_DB}?mode=ro', uri=True, timeout=60)
    else:
        print('no artcache.db yet (library/artfetch.py fetches covers); using embedded art')
        art = sqlite3.connect(':memory:')
        art.execute('CREATE TABLE art (folder TEXT PRIMARY KEY, hit INTEGER, local TEXT, new_w INTEGER, cur_w INTEGER)')
    art.row_factory = sqlite3.Row

    out = sqlite3.connect(a.out)
    out.executescript(SCHEMA)

    t0 = time.time()
    rows = src.execute(
        'SELECT path,src,folder,filename,ext,size,duration,bitrate,codec,'
        'artist,albumartist,album,title,track,disc,year,genre,has_art,mtime '
        'FROM files WHERE kind="audio"').fetchall()
    print(f'read {len(rows)} tracks from library.db')

    # ---- group into albums by folder
    byfolder = collections.defaultdict(list)
    for r in rows:
        byfolder[r['folder']].append(r)

    # ---- artists, merged across the two source trees and across spellings
    artists = {}          # norm -> dict
    for folder, tracks in byfolder.items():
        names = collections.Counter(
            (t['albumartist'] or t['artist']) for t in tracks
            if (t['albumartist'] or t['artist']))
        parts = folder.split('/')
        # depth 2 means the track sits directly under the artist with no album
        # folder; it still belongs to that artist and needs somewhere to live
        folder_artist = parts[1] if len(parts) >= 2 else None
        if not names:
            # untagged entirely (live bootlegs, mostly) -- the folder still knows
            if not folder_artist:
                continue
            names = collections.Counter({folder_artist: 1})
        name, _comp = album_credit(folder, tracks, names, parts[0])
        norm = mergemod.norm_artist(name)
        if not norm:
            # norm_artist strips non-ASCII, so a name written in another script
            # ("Celine Dion" in katakana) normalises to nothing and the artist
            # vanishes. The folder is the reliable fallback here.
            fallback = folder_artist
            norm = mergemod.norm_artist(fallback or '')
            if norm:
                name = fallback
        if not norm:
            continue
        a_ = artists.setdefault(norm, {'name': name, 'norm': norm, 'count': 0})
        # keep the spelling that the most tracks agree on
        if names.most_common(1)[0][1] > a_['count']:
            a_['name'], a_['count'] = name, names.most_common(1)[0][1]

    mb = {}
    try:
        for q, n, s in src.execute(
                'SELECT norm,mb_name,mb_sort FROM mb_artist WHERE mb_name IS NOT NULL'):
            mb[q] = (n, s)
    except sqlite3.OperationalError:
        pass

    aid = {}
    for i, (norm, a_) in enumerate(sorted(artists.items(), key=lambda kv: kv[1]['name'].lower()), 1):
        aid[norm] = i
        canon, sortn = mb.get(norm, (a_['name'], None))
        out.execute('INSERT INTO artist (id,name,sort_name,norm,n_albums,n_tracks,duration) '
                    'VALUES (?,?,?,?,0,0,0)',
                    (i, a_['name'], sort_title(sortn or a_['name']), norm))

    # ---- genres
    gid = {}
    for i, shelf in enumerate(genremap.shelves(), 1):
        gid[shelf] = i
        out.execute('INSERT INTO genre (id,shelf,n_albums,n_tracks) VALUES (?,?,0,0)',
                    (i, shelf))

    # ---- albums and tracks
    album_id = track_id = 0
    subcount = collections.Counter()
    for folder in sorted(byfolder):
        tracks = byfolder[folder]
        names = collections.Counter((t['albumartist'] or t['artist']) for t in tracks
                                    if (t['albumartist'] or t['artist']))
        parts = folder.split('/')
        if not names:
            fa = parts[1] if len(parts) >= 2 else None
            if not fa:
                continue
            names = collections.Counter({fa: 1})
        credit, is_comp = album_credit(folder, tracks, names, parts[0])
        artist_norm = mergemod.norm_artist(credit)
        if artist_norm not in aid:
            artist_norm = mergemod.norm_artist(parts[1] if len(parts) >= 2 else '')
        if artist_norm not in aid:
            continue
        titles = collections.Counter(t['album'] for t in tracks if t['album'])
        title = (titles.most_common(1)[0][0] if titles else
                 (folder.split('/')[-1] if len(parts) > 2 else 'Loose tracks'))

        years = [parse_year(t['year']) for t in tracks]
        years = [y for y in years if y]
        year = collections.Counter(years).most_common(1)[0][0] if years else None

        graw = collections.Counter(t['genre'] for t in tracks if t['genre'])
        shelf, sub = genremap.shelf_of(graw.most_common(1)[0][0] if graw else None)
        pool = folder.split('/')[0]
        if sub and pool == MAIN_POOL:
            subcount[(shelf, sub)] += 1

        arow = art.execute('SELECT hit,local,new_w,cur_w FROM art WHERE folder=?',
                           (folder,)).fetchone()
        art_path = art_kind = None
        art_w = 0
        if arow and arow['hit'] and arow['local']:
            p = os.path.join(SCRATCH, 'cache', 'art', arow['local'])
            if os.path.exists(p):
                art_path, art_w, art_kind = p, arow['new_w'] or 0, 'fetched'
        if not art_path and any(t['has_art'] for t in tracks):
            art_kind, art_w = 'embedded', (arow['cur_w'] if arow else 0) or 0

        album_id += 1
        discs = {t['disc'] for t in tracks if t['disc']}
        out.execute(
            'INSERT INTO album (id,artist_id,folder,title,sort_title,year,decade,'
            'genre_id,subgenre,n_tracks,duration,disc_count,art_w,art_path,art_kind,added,pool,is_compilation) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (album_id, aid[artist_norm], folder, title, sort_title(title), year,
             (year // 10 * 10) if year else None, gid[shelf], sub, len(tracks),
             sum(t['duration'] or 0 for t in tracks), max(1, len(discs)),
             art_w, art_path, art_kind, album_added(tracks), pool, 1 if is_comp else 0))

        for t in tracks:
            track_id += 1
            # 227 folders repeat a (disc,track) pair and 709 tracks have no number,
            # so the filename is the tiebreaker that makes the order deterministic
            sk = (f"{t['disc'] or 1:02d}."
                  f"{t['track'] if t['track'] is not None else 999:03d}."
                  f"{t['filename'].lower()}")
            out.execute(
                'INSERT INTO track (id,album_id,artist_id,disc,track,title,'
                'track_artist,duration,bitrate,ext,mime,size,rel_path,sort_key,skey,has_art) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (track_id, album_id, aid[artist_norm], t['disc'], t['track'],
                 t['title'] or os.path.splitext(t['filename'])[0],
                 t['artist'], t['duration'], t['bitrate'], t['ext'],
                 mime_for(t['codec'], t['ext']), t['size'], t['path'], sk,
                 stable_key(names.most_common(1)[0][0],
                            t['title'] or os.path.splitext(t['filename'])[0],
                            t['duration']),
                 1 if t['has_art'] else 0))

    for (shelf, sub), n in subcount.items():
        out.execute('INSERT INTO subgenre (genre_id,name,n_albums) VALUES (?,?,?)',
                    (gid[shelf], sub, n))

    # ---- rollups
    out.executescript(f"""
    UPDATE artist SET
      n_albums = (SELECT COUNT(*) FROM album WHERE album.artist_id = artist.id AND album.pool = '{MAIN_POOL}'),
      n_tracks = (SELECT COUNT(*) FROM track JOIN album ON album.id = track.album_id
                  WHERE track.artist_id = artist.id AND album.pool = '{MAIN_POOL}'),
      duration = (SELECT COALESCE(SUM(track.duration),0) FROM track JOIN album ON album.id = track.album_id
                  WHERE track.artist_id = artist.id AND album.pool = '{MAIN_POOL}'),
      all_albums = (SELECT COUNT(*) FROM album WHERE album.artist_id = artist.id),
      all_tracks = (SELECT COUNT(*) FROM track WHERE track.artist_id = artist.id);
    UPDATE artist SET art_album_id = (
      SELECT id FROM album WHERE album.artist_id = artist.id
      ORDER BY (pool != '{MAIN_POOL}'), (art_path IS NULL), art_w DESC, n_tracks DESC LIMIT 1);
    UPDATE genre SET
      n_albums = (SELECT COUNT(*) FROM album WHERE album.genre_id = genre.id AND album.pool = '{MAIN_POOL}'),
      n_tracks = (SELECT COALESCE(SUM(n_tracks),0) FROM album WHERE album.genre_id = genre.id AND album.pool = '{MAIN_POOL}');
    DELETE FROM artist WHERE all_tracks = 0;
    """)

    out.executescript("""
    CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
      kind, ref, label, detail, pool UNINDEXED, tokenize='unicode61');
    """)
    # an artist belongs to the main library if any of their albums do, else to
    # the first sub-library that holds them
    out.execute(f"INSERT INTO search (kind,ref,label,detail,pool) "
                f"SELECT 'artist', id, name, '', "
                f"(SELECT pool FROM album WHERE album.artist_id = artist.id "
                f" ORDER BY (pool != '{MAIN_POOL}'), pool LIMIT 1) FROM artist")
    out.execute("INSERT INTO search (kind,ref,label,detail,pool) "
                "SELECT 'album', album.id, album.title, artist.name, album.pool "
                "FROM album JOIN artist ON artist.id = album.artist_id")
    out.execute("INSERT INTO search (kind,ref,label,detail,pool) "
                "SELECT 'track', track.id, track.title, album.title, album.pool "
                "FROM track JOIN album ON album.id = track.album_id")
    out.commit()

    n = lambda q: out.execute(q).fetchone()[0]
    print(f'\nbuilt {a.out} in {time.time()-t0:.0f}s')
    print(f'   artists {n("SELECT COUNT(*) FROM artist")}   '
          f'albums {n("SELECT COUNT(*) FROM album")}   '
          f'tracks {n("SELECT COUNT(*) FROM track")}')
    print(f'   size {os.path.getsize(a.out)/1e6:.1f} MB '
          f'(library.db is {os.path.getsize(SRC_DB)/1e6:.0f} MB)')
    print('\n   shelves:')
    for shelf, na, nt in out.execute(
            'SELECT shelf,n_albums,n_tracks FROM genre WHERE n_albums>0 '
            'ORDER BY n_tracks DESC'):
        print(f'      {shelf:22} {na:5} albums {nt:6} tracks')
    print('\n   decades:')
    for d, na, nt in out.execute(
            'SELECT decade,COUNT(*),SUM(n_tracks) FROM album GROUP BY decade '
            'ORDER BY decade'):
        print(f'      {d if d else "unknown":>8} {na:5} albums {nt:6} tracks')
    out.close(); src.close(); art.close()

    # Album ids are reassigned by a rebuild, and the art cache is keyed by id,
    # so anything left behind would be served as the wrong album's cover.
    try:
        cache = Path(HERE) / 'cache' / 'art'
        n_art = 0
        if cache.exists():
            for f in cache.iterdir():
                if f.is_file():
                    f.unlink(); n_art += 1
        print('')
        print(f'   art cache: cleared {n_art} files (album ids have been reassigned)')
    except Exception as e:
        print(f'   art cache: could not clear ({e})')

    if CARRIED:
        n = restore_carried(str(OUT_DB), CARRIED)
        print('')
        print(f'   carried over: {n} artist photo/bio records re-keyed onto the new ids')
    if EXTRA:
        n = restore_extra(str(OUT_DB), EXTRA)
        print(f'   carried over: {n} artist radio rows (top tracks, related artists) re-keyed')

    # Box sets are derived from the folder tree, not the tags -- see
    # collections_build for why. Re-derived here so a rebuild never loses them.
    try:
        import discsplit
        n_sp, n_tr, n_dup = discsplit.build()
        print('')
        print(f'   disc split: {n_sp} merged multi-disc albums recovered '
              f'({n_tr} tracks); {len(n_dup)} duplicate-copy albums left alone')
    except Exception as e:
        print('')
        print(f'   disc split: SKIPPED ({e})')

    try:
        import collections_build
        r = collections_build.build()
        n_sets, n_albums = r[0], r[1]
        print('')
        print(f'   collections: {n_sets} sets covering {n_albums} albums')
    except Exception as e:
        print('')
        print(f'   collections: SKIPPED ({e})')

    # must run AFTER collections: a box-set disc is not a compilation part
    try:
        import compilations
        n_c, n_p = compilations.build()
        print('')
        print(f'   compilations: {n_c} reunited from {n_c + n_p} split album entries')
    except Exception as e:
        print('')
        print(f'   compilations: SKIPPED ({e})')

    # last: fold leftover fragments and settle contested titles
    try:
        import album_tidy
        n_f, n_t = album_tidy.build()
        print('')
        print(f'   tidy: {n_f} fragments folded in, {n_t} contested titles taken from the folder')
    except Exception as e:
        print('')
        print(f'   tidy: SKIPPED ({e})')

if __name__ == '__main__':
    main()
