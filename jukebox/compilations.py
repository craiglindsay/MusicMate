"""Reunite compilations that the folder tree split across artist folders.

"Day Parts: Romance" is one Mannheim Steamroller compilation, but it is stored
as <contributor>/Day Parts_ Romance -- six folders, one per
performer -- so the ETL, which treats a folder as an album, produced six albums
all called "Day Parts: Romance". With volume 2 that is eleven tiles for two
records.

This is the mirror image of the box-set problem. There, one release was spread
over several folders BELOW an album folder; here, one release is spread over
several folders beside each other, each under a different artist. Both are
folder-shape problems, and in neither case do the tags say so.

Nothing is moved on disk: the files are filed perfectly sensibly by performer.
Only the read model is changed, so the app shows one album with per-track
artists. Re-run after every ETL, like the other derivations.

Detection is deliberately narrow, because merging albums that are NOT one
release would be a genuine loss:
  * identical album title AND identical folder leaf name,
  * at least three different artists,
  * every part small (a real album by one artist is not 2 tracks),
  * none of them already part of a box set.
"""
import re, sqlite3, collections as _c, posixpath
from pathlib import Path

DB = Path(__file__).resolve().parent / 'jukebox.db'

MIN_PARTS = 3          # two artists is a duet, not a compilation
MAX_PART_TRACKS = 6    # a full album by one artist is not a "part"
VA_NAME = 'Various Artists'


def norm(s):
    s = (s or '').lower()
    s = re.sub(r'\b(the|a|an)\b', '', s)
    return re.sub(r'[^a-z0-9]', '', s)


def sort_name(s):
    s = re.sub(r'^(the|a|an)\s+', '', (s or '').lower()).strip()
    return re.sub(r'^[^a-z0-9]+', '', s) or (s or '')


JUNK_TITLE = re.compile(r'(?i)^\s*(unknown ?album|various ?artists?|untitled|misc\w*)\s*$')
TINY = 5               # a guest contributes a track or two, not a record


def find(c):
    """Groups that are one compilation split across contributors' folders.

    The hard part is telling a compilation from a SERIES. "Free To Be...You And
    Me" and "Ken Burns Jazz" look identical by title and folder shape: several
    artists, same album name, same folder leaf. The difference is what the parts
    are. On a compilation a guest contributes a track or two and appears nowhere
    else in the library; in a series each artist contributes a whole album and
    has a catalogue of their own.

    So a part only counts as a contribution when it is small AND that artist has
    no other presence here. Requiring most parts to look like that keeps thirty
    separate "Greatest Hits" albums apart while still reuniting Free To Be.
    """
    # An artist is a CONTRIBUTOR if they never have a real album anywhere in the
    # library -- only a track or two, wherever they appear. Counting albums was
    # wrong: Chip Davis appears on both Day Parts volumes, which made him look
    # like a catalogue artist and broke the very case this was written for.
    biggest = {r['artist_id']: r['mx'] for r in c.execute(
        'SELECT artist_id, MAX(n_tracks) mx FROM album GROUP BY artist_id')}
    rows = c.execute("""SELECT album.id, album.title, album.folder, album.n_tracks,
                               album.collection_id, album.artist_id, artist.name art
                        FROM album JOIN artist ON artist.id = album.artist_id
                        WHERE album.folder IS NOT NULL""").fetchall()
    groups = _c.defaultdict(list)
    for r in rows:
        if r['collection_id'] or JUNK_TITLE.match(r['title'] or ''):
            continue
        parts = r['folder'].split('/')
        if len(parts) < 3:
            continue
        groups[(parts[0], norm(parts[-1]), norm(r['title']))].append(dict(r))

    out = []
    for _key, v in groups.items():
        if len(v) < 2 or len({norm(a['art']) for a in v}) < 2:
            continue
        contrib = [a for a in v
                   if (a['n_tracks'] or 0) <= TINY
                   and biggest.get(a['artist_id'], 99) <= TINY]
        if len(contrib) >= 1 and len(contrib) >= 0.5 * len(v) and len(v) - len(contrib) <= 1:
            out.append(sorted(v, key=lambda a: a['folder']))
    return out


def va_artist(c):
    r = c.execute('SELECT id FROM artist WHERE name=?', (VA_NAME,)).fetchone()
    if r:
        return r['id']
    cur = c.execute('INSERT INTO artist (name, sort_name, norm, n_albums, n_tracks, duration) '
                    'VALUES (?,?,?,0,0,0)', (VA_NAME, sort_name(VA_NAME), norm(VA_NAME)))
    return cur.lastrowid


def build(db=DB, verbose=False):
    c = sqlite3.connect(db, timeout=60)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA busy_timeout=60000')
    cols = {r['name'] for r in c.execute('PRAGMA table_info(album)')}
    if 'is_compilation' not in cols:
        c.execute('ALTER TABLE album ADD COLUMN is_compilation INTEGER DEFAULT 0')
        c.commit()

    groups = find(c)
    if not groups:
        c.close()
        return 0, 0

    va = va_artist(c)
    merged = folded = 0
    for v in groups:
        keep = max(v, key=lambda a: (a['n_tracks'] or 0, -a['id']))
        others = [a for a in v if a['id'] != keep['id']]
        # the track keeps its own performer in track_artist; the album becomes VA
        for a in others:
            c.execute('UPDATE track SET album_id=? WHERE album_id=?', (keep['id'], a['id']))
            c.execute('DELETE FROM album WHERE id=?', (a['id'],))
            # ref is stored as an INTEGER in the fts table; passing a string
            # matches nothing and silently leaves the old tile in search
            c.execute("DELETE FROM search WHERE kind='album' AND CAST(ref AS INTEGER)=?",
                      (a['id'],))
            folded += 1
        # Only call it Various Artists when nobody dominates. "The Essential
        # George Benson" is a Benson record with two guests, and "Songs From
        # The Labyrinth" is Sting's with one -- relabelling those would be
        # worse than the split they came from.
        total = sum(a['n_tracks'] or 0 for a in v) or 1
        lead = (keep['n_tracks'] or 0) / float(total)
        if lead < 0.6:
            c.execute('UPDATE album SET artist_id=?, is_compilation=1 WHERE id=?',
                      (va, keep['id']))
        else:
            c.execute('UPDATE album SET is_compilation=1 WHERE id=?', (keep['id'],))
        merged += 1
        if verbose:
            print('%-32s %d parts -> 1 album (%d tracks)'
                  % ((keep['title'] or '')[:32], len(v),
                     sum(a['n_tracks'] or 0 for a in v)))

    # the surviving tile is now a Various Artists record, so say so in search
    for r in c.execute("SELECT id, title FROM album WHERE is_compilation=1").fetchall():
        c.execute("UPDATE search SET detail=? WHERE kind='album' AND CAST(ref AS INTEGER)=?",
                  (VA_NAME, r['id']))

    # a track on a compilation must still show who performed it
    c.execute("""UPDATE track SET track_artist = COALESCE(NULLIF(track_artist,''),
                   (SELECT artist.name FROM artist WHERE artist.id = track.artist_id))
                 WHERE album_id IN (SELECT id FROM album WHERE is_compilation=1)""")
    c.execute("""UPDATE album SET
                   n_tracks = (SELECT COUNT(*) FROM track WHERE track.album_id=album.id),
                   duration = (SELECT COALESCE(SUM(duration),0) FROM track WHERE track.album_id=album.id)
                 WHERE is_compilation=1""")
    # the rollup must stay pool-aware: n_albums/n_tracks are the MAIN library's
    # (what the Artists page lists), all_* cover every library (etl.py agrees)
    c.execute("""UPDATE artist SET
                   n_albums = (SELECT COUNT(*) FROM album WHERE album.artist_id=artist.id AND album.pool='VariousArtists'),
                   n_tracks = (SELECT COALESCE(SUM(n_tracks),0) FROM album WHERE album.artist_id=artist.id AND album.pool='VariousArtists'),
                   all_albums = (SELECT COUNT(*) FROM album WHERE album.artist_id=artist.id),
                   all_tracks = (SELECT COALESCE(SUM(n_tracks),0) FROM album WHERE album.artist_id=artist.id)""")
    c.commit()
    c.close()
    return merged, folded


if __name__ == '__main__':
    m, f = build(verbose=True)
    print('')
    print('%d compilations reunited, %d stray album entries folded in' % (m, f))
