"""Two last shapes of duplicate tile, fixed in the read model only.

A. COMPLEMENTARY FRAGMENTS. "Fijacion Oral Volumen 1" (4 tracks) sits beside
   "Fijacion Oral, Volume 1" (3 tracks): the same record split over two folders
   holding DIFFERENT songs. The duplicate resolver refused them, correctly --
   it merges copies of the same audio, and these share none. They need
   combining, not de-duplicating. Same for "Speak & Spell" and its four bonus
   tracks sitting in their own folder.

   Only a small fragment is folded, and only when it shares an artist and a
   normalised title with a larger album AND none of its audio already exists
   there. Two genuinely different records by one artist would share fingerprints
   nowhere, but they would also not be three tracks long.

B. A TITLE THAT BELONGS TO ANOTHER ALBUM. Trisha Yearwood's "Songbook: A
   Collection of Hits" carries the tag "Where Your Road Leads", which is the
   name of a different album she also has -- so the grid showed that title
   twice. Where a tag title collides with another album by the same artist and
   the folder name does not, the folder wins. This is the same principle the
   rest of the library runs on: the folder is the authority, tags are evidence.

Nothing moves on disk, so a rebuild re-derives it. Run after the others.
"""
import re, sqlite3, collections as _c
from pathlib import Path

DB = Path(__file__).resolve().parent / 'jukebox.db'
TINY = 6                 # a fragment, not a record
DISCY = re.compile(r'(?i)^\s*(disc|disk|cd)\s*\.?\s*\w{1,5}\s*$')


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


VOL = re.compile(r'(?i)\b(vol(?:umen|ume)?)\s*\.?\s*')
NOISE = re.compile(r'(?i)\b(bonus ?tracks?|digital version|deluxe|remaster(?:ed)?|'
                   r'expanded|special edition|edition|reissue|explicit|ep|box ?set)\b')


def loose(title, artist):
    t = (title or '').lower()
    if artist:
        t = re.sub(r'^\s*' + re.escape(artist.lower()) + r'\s*[-–—]\s*', '', t)
    t = re.sub(r'[\(\[][^\)\]]*[\)\]]', ' ', t)
    t = NOISE.sub(' ', t)
    t = VOL.sub('vol', t)
    t = t.split('/')[0]      # "Cantos de Amor / Love Songs"
    t = re.sub(r'\b(the|a|an)\b', ' ', t)
    return re.sub(r'[^a-z0-9]', '', t)


def fold_fragments(c, verbose):
    rows = [dict(r) for r in c.execute(
        """SELECT album.id, album.title, album.n_tracks, album.folder, album.artist_id,
                  album.collection_id, COALESCE(album.is_compilation,0) comp, artist.name art
           FROM album JOIN artist ON artist.id = album.artist_id""")]
    keys = _c.defaultdict(set)
    for r in c.execute('SELECT album_id, skey FROM track'):
        keys[r['album_id']].add(r['skey'])
    by = _c.defaultdict(list)
    for a in rows:
        if a['collection_id'] or a['comp'] or not a['folder']:
            continue
        k = loose(a['title'], a['art'])
        if k:
            by[(a['artist_id'], k)].append(a)

    folded = 0
    for _k, v in by.items():
        if len(v) < 2:
            continue
        v.sort(key=lambda a: -(a['n_tracks'] or 0))
        keep = v[0]
        for small in v[1:]:
            if (small['n_tracks'] or 0) > TINY:
                continue
            if keys[keep['id']] & keys[small['id']]:
                continue                       # shares audio: not our business
            c.execute('UPDATE track SET album_id=? WHERE album_id=?', (keep['id'], small['id']))
            c.execute('DELETE FROM album WHERE id=?', (small['id'],))
            c.execute("DELETE FROM search WHERE kind='album' AND CAST(ref AS INTEGER)=?",
                      (small['id'],))
            keys[keep['id']] |= keys[small['id']]
            folded += 1
            if verbose:
                print('fold  %-34s <- %-30s %dtr'
                      % ((keep['title'] or '')[:34], (small['title'] or '')[:30],
                         small['n_tracks'] or 0))
    return folded


def retitle_collisions(c, verbose):
    # A box-set disc legitimately shares its title with its siblings and its
    # folder is called "Disc 1", so retitling from the folder there produces an
    # album literally named "Disc 1" -- worse than the collision it fixes.
    rows = [dict(r) for r in c.execute(
        """SELECT album.id, album.title, album.folder, album.artist_id, artist.name art
           FROM album JOIN artist ON artist.id = album.artist_id
           WHERE album.folder IS NOT NULL AND album.collection_id IS NULL
             AND COALESCE(album.is_compilation,0)=0""")]
    by_artist = _c.defaultdict(list)
    for a in rows:
        by_artist[a['artist_id']].append(a)

    fixed = 0
    for _aid, v in by_artist.items():
        if len(v) < 2:
            continue
        titles = _c.Counter(norm(a['title']) for a in v)
        for a in v:
            leaf = a['folder'].split('/')[-1]
            if titles[norm(a['title'])] < 2:
                continue                       # this title is not contested
            if norm(leaf) == norm(a['title']):
                continue                       # folder agrees; it is the real owner
            if titles[norm(leaf)] == 0 and norm(leaf) != norm(a['title']):
                # no album claims the folder name, so the tag is the odd one out
                clean = re.sub(r'\s*[-_]\s*$', '', leaf).strip()
                if not clean or norm(clean) == norm(a['title']):
                    continue
                if DISCY.match(clean):        # "Disc 2" is not an album name
                    continue
                c.execute('UPDATE album SET title=?, sort_title=? WHERE id=?',
                          (clean, re.sub(r'^(the|a|an)\s+', '', clean.lower()).strip(), a['id']))
                c.execute("UPDATE search SET label=? WHERE kind='album' AND CAST(ref AS INTEGER)=?",
                          (clean, a['id']))
                fixed += 1
                if verbose:
                    print('title %-34s -> %s' % ((a['title'] or '')[:34], clean[:40]))
    return fixed


def strip_edition_noise(c, verbose):
    """Drop a bracketed EDITION gloss when the folder disagrees with it.

    After merging, "Speak & Spell" wore the title of the bonus-tracks edition it
    absorbed, because that was the tag on the tracks that came in.

    Only brackets whose CONTENT is an edition marker are removed. A first
    version of this stripped every parenthetical and changed 156 titles,
    including "Was (not Was)" -> "Was", which is the band's name, and
    "Take Five (The Essential Dave Brubeck)" -> "Take Five". A parenthesis is
    part of the title far more often than it is packaging.
    """
    fixed = 0
    rows = c.execute("""SELECT album.id, album.title, album.folder, artist.name art
                        FROM album JOIN artist ON artist.id = album.artist_id
                        WHERE album.folder IS NOT NULL
                          AND (album.title LIKE '%[%' OR album.title LIKE '%(%')""").fetchall()
    for a in rows:
        bare, changed = a['title'] or '', False
        for grp in re.findall(r'[\(\[]([^\)\]]*)[\]\)]', bare):
            if NOISE.search(grp):
                bare = bare.replace('(' + grp + ')', ' ').replace('[' + grp + ']', ' ')
                changed = True
        if not changed:
            continue
        bare = re.sub(r'\s{2,}', ' ', bare).strip(' -_')
        if not bare or norm(bare) == norm(a['title']):
            continue
        # the folder often repeats the artist ("Depeche Mode - Singles Box 6"),
        # so drop that before asking whether it corroborates the trimmed title
        leaf = a['folder'].split('/')[-1]
        art = (a['art'] or '').strip()
        if art:
            leaf = re.sub('^' + re.escape(art) + r'\s*[-–—:]\s*', '', leaf, flags=re.I)
        if norm(leaf).startswith(norm(bare)) or norm(bare).startswith(norm(leaf)):
            c.execute('UPDATE album SET title=?, sort_title=? WHERE id=?',
                      (bare, re.sub(r'^(the|a|an)\s+', '', bare.lower()).strip(), a['id']))
            c.execute("UPDATE search SET label=? WHERE kind='album' AND CAST(ref AS INTEGER)=?",
                      (bare, a['id']))
            fixed += 1
            if verbose:
                print('trim  %-42s -> %s' % ((a['title'] or '')[:42], bare[:34]))
    return fixed


def strip_artist_prefix(c, verbose):
    """Drop a leading "<artist> - " from an album title.

    Depeche Mode's singles boxes arrived in three shapes -- "Depeche Mode -
    Singles Box 1", "Singles Box 2", "Singles Box 5 (Box Set)" -- so one series
    read as three. The artist is already shown beside the title, so the prefix
    carries nothing.
    """
    fixed = 0
    for a in c.execute("SELECT album.id, album.title, artist.name art FROM album "
                       "JOIN artist ON artist.id = album.artist_id").fetchall():
        t, art = (a['title'] or '').strip(), (a['art'] or '').strip()
        if not art or len(art) < 3:
            continue
        m = re.match('^' + re.escape(art) + r'\s*[-–—:]\s*(.+)$', t, re.I)
        if not m:
            continue
        clean = m.group(1).strip()
        if not clean or norm(clean) == norm(t):
            continue
        c.execute('UPDATE album SET title=?, sort_title=? WHERE id=?',
                  (clean, re.sub(r'^(the|a|an)\s+', '', clean.lower()).strip(), a['id']))
        c.execute("UPDATE search SET label=? WHERE kind='album' AND CAST(ref AS INTEGER)=?",
                  (clean, a['id']))
        fixed += 1
        if verbose:
            print('unprefix %-38s -> %s' % (t[:38], clean[:34]))
    return fixed


def build(db=DB, verbose=False):
    c = sqlite3.connect(db, timeout=60)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA busy_timeout=60000')
    folded = fold_fragments(c, verbose)
    fixed = retitle_collisions(c, verbose)
    fixed += strip_edition_noise(c, verbose)
    fixed += strip_artist_prefix(c, verbose)
    c.execute("""UPDATE album SET
                   n_tracks = (SELECT COUNT(*) FROM track WHERE track.album_id=album.id),
                   duration = (SELECT COALESCE(SUM(duration),0) FROM track WHERE track.album_id=album.id)""")
    c.execute('DELETE FROM album WHERE n_tracks=0')
    # the rollup must stay pool-aware: n_albums/n_tracks are the MAIN library's
    # (what the Artists page lists), all_* cover every library (etl.py agrees)
    c.execute("""UPDATE artist SET
                   n_albums = (SELECT COUNT(*) FROM album WHERE album.artist_id=artist.id AND album.pool='VariousArtists'),
                   n_tracks = (SELECT COALESCE(SUM(n_tracks),0) FROM album WHERE album.artist_id=artist.id AND album.pool='VariousArtists'),
                   all_albums = (SELECT COUNT(*) FROM album WHERE album.artist_id=artist.id),
                   all_tracks = (SELECT COALESCE(SUM(n_tracks),0) FROM album WHERE album.artist_id=artist.id)""")
    c.commit()
    c.close()
    return folded, fixed


if __name__ == '__main__':
    f, t = build(verbose=True)
    print('')
    print('%d fragments folded into their album, %d contested titles taken from the folder' % (f, t))
