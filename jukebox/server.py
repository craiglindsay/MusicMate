"""Jukebox server: browse and play the library over HTTP.

Bound to the LAN on purpose. Sonos fetches audio from this server directly, so
127.0.0.1 would make the whole thing unusable the moment a speaker is involved.

Three things here are load-bearing rather than incidental:

  * Range requests. Without 206 support the browser cannot seek, cannot reliably
    learn a track's duration, and Sonos will not stream at all.
  * Content-Type from the CODEC, not the extension. Seven files in this library are
    MP3 streams named .m4a; announced as audio/mp4 they fail silently.
  * Opaque integer ids in URLs. 5,103 paths contain an apostrophe, 1,318 an
    ampersand and 284 non-ASCII characters -- and a path in a URL is also a
    directory-traversal hole. Ids avoid both problems at once.
"""
import os, io, re, sys, time, json, sqlite3, mimetypes, socket, argparse, collections
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import (JSONResponse, FileResponse, Response, RedirectResponse,
                               StreamingResponse, HTMLResponse)

HERE = Path(__file__).resolve().parent
SCRATCH = HERE.parent
DB = HERE / 'jukebox.db'
ROOT = Path(os.environ.get('MUSICMATE_ROOT') or r'D:\Music')
ARTCACHE = HERE / 'cache' / 'art'
# One main library and any number of sub-libraries, each a top-level folder
# under the music root (album.pool). The browse endpoints default to the main
# pool; pool='*' means everything (favourites, queues, karaoke), any other value
# is one sub-library. See etl.py for the rollups.
MAIN_POOL = 'VariousArtists'


def pool_where(pool, alias='album', lead='WHERE'):
    """-> (clause, args) scoping a query to a pool. None = main, '*' = all."""
    if pool == '*':
        return '', []
    return f'{lead} {alias}.pool = ?', [pool or MAIN_POOL]
ARTCACHE.mkdir(parents=True, exist_ok=True)

# Album art is cached by album id and served immutable for a year. A rebuild
# REASSIGNS ids, so every cached image silently becomes another album's cover --
# a Madonna sleeve on a Maroon 5 record. This token changes whenever the read
# model is rebuilt and rides on every art URL, so both this cache and the
# browser's are invalidated together.
def _art_gen():
    try:
        return str(int(DB.stat().st_mtime))
    except Exception:
        return '0'

ART_GEN = _art_gen()


def art_gen():
    """The CURRENT generation, read each time it is needed. The read model is
    rebuilt without a restart now (the System page's rescan), so a value taken
    once at startup went stale: pages kept being handed the old token, the
    browser kept its year-long cache of covers keyed by the old ids, and every
    sleeve in a rebuilt library came up as another record's (2 Sep 2026). A
    stat per call is microseconds."""
    return _art_gen()

app = FastAPI(title='MusicMate')


@app.middleware('http')
async def no_stale_ui(request, call_next):
    """The page and the JSON must never be served from a browser's cache.

    Nothing set Cache-Control on them, so a television happily kept a copy of
    the UI for days: fixes appeared not to land, and stale JSON meant stale
    track ids -- the same ids that played the wrong album. Media and artwork
    keep their long caches, because those URLs carry a build token and really
    are immutable."""
    resp = await call_next(request)
    path = request.url.path
    if resp.status_code == 404 and 'text/html' in (request.headers.get('accept') or ''):
        # a browser NAVIGATED to something that is not there: the TV once showed
        # a bare {"detail":"Not Found"} page and nothing said which address it
        # had asked for (12 Sep 2026). Media and API fetches are not logged.
        try:
            with open(HERE / 'logs' / 'notfound.log', 'a', encoding='utf-8') as f:
                f.write('%s %s %s ua=%s ref=%s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S'), request.client.host if request.client else '-',
                                                    str(request.url), request.headers.get('user-agent', '-')[:120], request.headers.get('referer', '-')))
        except OSError:
            pass
    if path.startswith(('/api/art/', '/api/artist-photo/', '/api/stream/', '/media/')):
        return resp
    if path == '/' or path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
    return resp

def db():
    c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def rows(sql, *args):
    c = db()
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()

def one(sql, *args):
    r = rows(sql, *args)
    return r[0] if r else None

def safe_path(rel):
    """Resolve a library-relative path, refusing anything that escapes the root."""
    p = (ROOT / rel.replace('/', os.sep)).resolve()
    if not str(p).lower().startswith(str(ROOT.resolve()).lower()):
        raise HTTPException(403, 'outside the library')
    return p

# ---------------------------------------------------------------- browse
@app.get('/api/axes')
def axes():
    a = one('SELECT COUNT(*) n FROM artist WHERE n_albums > 0')['n']
    al = one('SELECT COUNT(*) n FROM album WHERE pool=?', MAIN_POOL)['n']
    g = one('SELECT COUNT(*) n FROM genre WHERE n_albums > 0')['n']
    yr = one('SELECT MIN(year) lo, MAX(year) hi FROM album WHERE year IS NOT NULL AND pool=?', MAIN_POOL)
    t = one('SELECT COUNT(*) n, COALESCE(SUM(track.duration),0) d FROM track '
            'JOIN album ON album.id=track.album_id WHERE album.pool=?', MAIN_POOL)
    return {
        'tracks': t['n'], 'hours': round(t['d'] / 3600, 1),
        'axes': [
            {'key': 'artist', 'label': 'Artist', 'count': a, 'sub': f'{a} performers'},
            {'key': 'album', 'label': 'Album', 'count': al, 'sub': f'{al} records'},
            {'key': 'genre', 'label': 'Genre', 'count': g, 'sub': f'{g} shelves'},
            {'key': 'year', 'label': 'Year', 'count': 0,
             'sub': f"{yr['lo']}\u2013{yr['hi']}" if yr and yr['lo'] else 'unknown'},
        ]}

@app.get('/api/artists')
def artists(sort: str = 'name', letter: str = None, pool: str = None,
            limit: int = Query(2000, le=5000), offset: int = 0):
    order = {'name': 'artist.sort_name', 'albums': 'n_albums DESC, artist.sort_name',
             'tracks': 'n_tracks DESC, artist.sort_name'}.get(sort, 'artist.sort_name')
    conds, args = [], []
    if letter:
        if letter == '#':
            conds.append("substr(artist.sort_name,1,1) NOT BETWEEN 'a' AND 'z'")
        else:
            conds.append('substr(artist.sort_name,1,1) = ?'); args.append(letter.lower())
    photo = ('(artist_info.image_file IS NOT NULL) AS has_photo, '
             'CAST(COALESCE(artist_info.fetched,0) AS INTEGER) AS photo_v ')
    # has_photo lets a tile use a picture of the artist instead of falling back
    # to whichever album cover happened to be biggest
    if pool and pool != '*':
        # a sub-library: counts and the cover come from that pool's albums only
        where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
        return rows(f'SELECT artist.id,artist.name,COUNT(DISTINCT album.id) AS n_albums,'
                    f'SUM(album.n_tracks) AS n_tracks,SUM(album.duration) AS duration,'
                    f'(SELECT id FROM album a2 WHERE a2.artist_id=artist.id AND a2.pool=? '
                    f' ORDER BY (a2.art_path IS NULL), a2.art_w DESC LIMIT 1) AS art_album_id,'
                    f'{photo}'
                    f'FROM artist JOIN album ON album.artist_id=artist.id AND album.pool=? '
                    f'LEFT JOIN artist_info ON artist_info.artist_id=artist.id '
                    f'{where} GROUP BY artist.id ORDER BY {order} LIMIT ? OFFSET ?',
                    pool, pool, *args, limit, offset)
    if pool == '*':
        cols = 'artist.all_albums AS n_albums,artist.all_tracks AS n_tracks'
    else:
        cols = 'artist.n_albums,artist.n_tracks'
        conds.append('artist.n_albums > 0')          # the main library only
    where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
    return rows(f'SELECT artist.id,artist.name,{cols},'
                f'artist.duration,artist.art_album_id,{photo}'
                f'FROM artist LEFT JOIN artist_info ON artist_info.artist_id=artist.id '
                f'{where} ORDER BY {order} LIMIT ? OFFSET ?',
                *args, limit, offset)

@app.get('/api/letters')
def letters(of: str = 'artist'):
    """How many artists/albums sit under each initial.

    The alphabet rail needs this to grey out empty letters and size its hit targets;
    without it the rail is a guess and half its keys lead nowhere.
    """
    tbl = 'artist' if of == 'artist' else 'album'
    if tbl == 'artist':
        rows_ = rows("SELECT CASE WHEN substr(sort_name,1,1) BETWEEN 'a' AND 'z' "
                     "THEN substr(sort_name,1,1) ELSE '#' END AS letter, COUNT(*) n "
                     "FROM artist WHERE n_albums > 0 GROUP BY letter ORDER BY letter")
    else:
        rows_ = rows("SELECT CASE WHEN substr(sort_title,1,1) BETWEEN 'a' AND 'z' "
                     "THEN substr(sort_title,1,1) ELSE '#' END AS letter, COUNT(*) n "
                     "FROM album WHERE pool=? GROUP BY letter ORDER BY letter", MAIN_POOL)
    have = {r['letter']: r['n'] for r in rows_}
    return [{'letter': L, 'n': have.get(L, 0)}
            for L in ['#'] + [chr(c) for c in range(ord('a'), ord('z') + 1)]]

@app.get('/api/albums/by-letter')
def albums_by_letter(letter: str, limit: int = Query(400, le=2000), offset: int = 0):
    if letter == '#':
        where = "WHERE substr(album.sort_title,1,1) NOT BETWEEN 'a' AND 'z'"
        args = []
    else:
        where = 'WHERE substr(album.sort_title,1,1)=?'
        args = [letter.lower()]
    return rows(
        'SELECT album.id,album.title,album.year,album.n_tracks,album.art_w,'
        'album.art_kind,artist.name AS artist,artist.id AS artist_id FROM album '
        f'JOIN artist ON artist.id=album.artist_id {where} AND album.pool=? '
        'ORDER BY album.sort_title LIMIT ? OFFSET ?', *args, MAIN_POOL, limit, offset)

def collapse_collections(album_rows):
    """Replace a box set's member discs with a single tile for the set.

    Twenty tiles all reading "The Singles 1986-1995" is what the grid looked
    like before this: the discs of one box set, each tagged with the same wrong
    title. Members are folded down to the first position the set appears at, so
    ordering by artist/year/title still lands the set where you would look."""
    cache, out, seen = {}, [], set()
    for r in album_rows:
        cid = r.pop('collection_id', None)
        if not cid:
            r['kind'] = 'album'
            out.append(r)
            continue
        if cid in seen:
            continue
        seen.add(cid)
        if cid not in cache:
            cache[cid] = one('SELECT collection.*, artist.name AS artist FROM collection '
                             'JOIN artist ON artist.id=collection.artist_id '
                             'WHERE collection.id=?', cid)
        c = cache[cid]
        if not c:                     # should not happen, but never drop a row
            r['kind'] = 'album'
            out.append(r)
            continue
        # a multi-disc album is just an album on the shelf; only a real box
        # set advertises its discs
        out.append({'kind': 'collection' if c['kind'] == 'set' else 'discalbum',
                    'coll_kind': c['kind'], 'id': c['id'], 'title': c['name'],
                    'artist': c['artist'], 'artist_id': c['artist_id'],
                    'year': c['year_min'], 'year_max': c['year_max'],
                    'n_tracks': c['n_tracks'], 'n_discs': c['n_discs'],
                    'duration': c['duration'], 'art_id': c['art_album_id']})
    return out

@app.get('/api/collection/{cid}')
def collection(cid: int):
    c = one('SELECT collection.*, artist.name AS artist FROM collection '
            'JOIN artist ON artist.id=collection.artist_id WHERE collection.id=?', cid)
    if not c:
        raise HTTPException(404)
    c['discs'] = rows(
        'SELECT id,title,disc_label,disc_no,year,n_tracks,duration,art_w,art_kind '
        'FROM album WHERE collection_id=? ORDER BY (disc_no IS NULL), disc_no, sort_title', cid)
    return c

@app.get('/api/collection/{cid}/tracks')
def collection_tracks(cid: int):
    """Every track in the set, disc order then track order -- for Play all."""
    out = rows(
        'SELECT track.id, track.skey, track.title, track.duration, track.track_artist, '
        'album.id AS album_id, album.title AS album, album.disc_label, album.disc_no, '
        'album.artist_id, artist.name AS album_artist '
        'FROM track JOIN album ON album.id=track.album_id '
        'JOIN artist ON artist.id=album.artist_id '
        'WHERE album.collection_id=? '
        'ORDER BY (album.disc_no IS NULL), album.disc_no, track.sort_key', cid)
    _mark_synced(out)
    return out

@app.get('/api/artist/{aid}')
def artist(aid: int):
    a = one('SELECT * FROM artist WHERE id=?', aid)
    if not a:
        raise HTTPException(404)
    info = one('SELECT wiki_title, bio, bio_url, image_file, fetched '
               'FROM artist_info WHERE artist_id=?', aid)
    if info:
        a['bio'] = info['bio']
        a['bio_url'] = info['bio_url']
        a['has_photo'] = bool(info['image_file'])
        a['photo_v'] = int(info['fetched'] or 0)
    else:
        a['bio'] = a['bio_url'] = None
        a['has_photo'] = False
        a['photo_v'] = 0
    a['albums'] = collapse_collections(rows(
        'SELECT id,title,year,decade,n_tracks,duration,art_w,art_kind,collection_id,pool '
        'FROM album WHERE artist_id=? ORDER BY (pool != ?), (year IS NULL), year, sort_title', aid, MAIN_POOL))
    return a


@app.get('/api/pools')
def pools():
    """The sub-libraries: every scanned top-level folder other than the main one."""
    out = rows('SELECT pool, COUNT(*) n_albums, SUM(n_tracks) n_tracks, '
               'COUNT(DISTINCT artist_id) n_artists, MAX(added) added '
               'FROM album WHERE pool IS NOT NULL AND pool != ? GROUP BY pool ORDER BY pool', MAIN_POOL)
    for p_ in out:
        p_['art'] = [r['id'] for r in rows('SELECT id FROM album WHERE pool=? AND art_kind IS NOT NULL '
                                           'ORDER BY art_w DESC LIMIT 4', p_['pool'])]
    return out


@app.get('/api/lib/{pool}/tracks')
def lib_tracks(pool: str, limit: int = Query(6000, le=20000)):
    """Every song in a sub-library, in artist / album / running order."""
    got = rows('SELECT track.id, track.title, track.duration, track.track_artist, track.skey, track.has_art, '
               'album.id AS album_id, album.title AS album, album.artist_id, album.is_compilation, album.pool, '
               'artist.name AS album_artist '
               'FROM track JOIN album ON album.id=track.album_id JOIN artist ON artist.id=album.artist_id '
               'WHERE album.pool=? ORDER BY artist.sort_name, album.year, album.sort_title, track.sort_key '
               'LIMIT ?', pool, limit)
    return [{'id': r['id'], 'title': r['title'], 'duration': r['duration'], 'skey': r['skey'],
             'album_id': r['album_id'], 'album': r['album'], 'has_art': r['has_art'],
             'is_compilation': r['is_compilation'], 'pool': r['pool'],
             'artist': r['track_artist'] or r['album_artist'], 'artist_id': r['artist_id']} for r in got]

ARTPIX = HERE / 'artistpix'

# The enrichment pass may not have run yet, and the app DB is opened read-only,
# so ensure the table exists once here with a writable handle -- the artist
# queries LEFT JOIN it and would otherwise fail outright.
def _ensure_artist_info():
    try:
        w = sqlite3.connect(DB, timeout=30)
        w.execute('CREATE TABLE IF NOT EXISTS artist_info ('
                  'artist_id INTEGER PRIMARY KEY, name TEXT, wiki_title TEXT, bio TEXT, '
                  'bio_url TEXT, image_url TEXT, image_file TEXT, status TEXT, fetched REAL)')
        w.commit(); w.close()
    except Exception as e:
        print('artist_info guard:', e)
_ensure_artist_info()

@app.get('/api/artist-photo/{aid}')
def artist_photo(aid: int):
    """A picture of the artist, as opposed to a picture of one of their records."""
    r = one('SELECT image_file FROM artist_info WHERE artist_id=?', aid)
    if not r or not r['image_file']:
        raise HTTPException(404)
    p = ARTPIX / r['image_file']
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p), media_type='image/jpeg',
                        headers={'Cache-Control': 'public, max-age=604800'})

@app.get('/api/artist/{aid}/radio')
def artist_radio(aid: int, limit: int = Query(60, le=1000)):
    """The artist's best-known songs, in that order, restricted to what you own.

    The ranking comes from Deezer's popularity list; the audio is always yours.
    Titles are matched loosely -- a remaster or a live cut carries a suffix the
    chart entry does not -- and a track is only taken once however many albums
    it appears on, so a radio does not repeat the same song from a compilation.
    Anything left over is appended by play count, so an artist with no chart
    data still gets a sensible run.
    """
    tracks = rows(
        "SELECT track.id, track.title, track.duration, track.skey, "
        "album.id AS album_id, album.title AS album, album.year, "
        "COALESCE(NULLIF(track.track_artist,''), artist.name) AS artist, album.artist_id "
        "FROM track JOIN album ON album.id=track.album_id "
        "JOIN artist ON artist.id=album.artist_id "
        "WHERE album.artist_id=?", aid)
    if not tracks:
        raise HTTPException(404, 'no tracks for that artist')

    def key(s):
        s = re.sub(r'[\(\[].*?[\)\]]', ' ', (s or '').lower())
        return re.sub(r'[^a-z0-9]', '', s)

    # Which COPY of a song to play matters as much as which song. A plain
    # "Enjoy The Silence" sitting on "Live in Berlin" has exactly the same
    # title as the studio cut, so title length alone picked live takes and
    # remix collections. Judge the album it sits on as well.
    LIVEY = re.compile(r'(?i)\b(live|unplugged|concert|in concert|tour|remix(?:es|ed)?|'
                       r'mixes|mix|instrumental|karaoke|demo|session|acoustic|'
                       r'soundtrack|tribute|bootleg)\b')

    def rank_copy(t):
        alb = t['album'] or ''
        return (1 if LIVEY.search(alb) else 0,          # studio album first
                1 if LIVEY.search(t['title'] or '') else 0,
                len(t['title'] or ''),                   # then the plainest title
                t['year'] or 9999)

    best = {}
    for t in tracks:
        k = key(t['title'])
        if k not in best or rank_copy(t) < rank_copy(best[k]):
            best[k] = t

    ranked, used = [], set()
    for r in rows('SELECT norm FROM artist_top WHERE artist_id=? ORDER BY pos', aid):
        t = best.get(re.sub(r'[^a-z0-9]', '', r['norm'] or ''))
        if t and t['id'] not in used:
            used.add(t['id'])
            ranked.append(t)
    if len(ranked) < limit:
        # fall back to how much YOU have played them, then to album order
        plays = {}
        try:
            for p in st.most_played(500, 'track'):
                plays[p['key']] = p.get('n', 0)
        except Exception:
            plays = {}
        rest = [t for t in best.values() if t['id'] not in used]
        rest.sort(key=lambda t: (-plays.get(t['skey'], 0), t['year'] or 9999, t['title'] or ''))
        ranked.extend(rest[:limit - len(ranked)])
    ranked = ranked[:limit]
    _mark_synced(ranked)         # lyrics + stems for the row microphones; drops skey
    return ranked


@app.get('/api/artist/{aid}/similar')
def artist_similar(aid: int, limit: int = Query(12, le=40)):
    """Related artists, filtered to the ones you actually have."""
    out = []
    for r in rows('SELECT name, norm, pos FROM artist_similar WHERE artist_id=? ORDER BY pos', aid):
        hit = one('SELECT artist.id, artist.name, artist.n_albums, artist.n_tracks, '
                  '(artist_info.image_file IS NOT NULL) AS has_photo, '
                  'CAST(COALESCE(artist_info.fetched,0) AS INTEGER) AS photo_v, '
                  'artist.art_album_id '
                  'FROM artist LEFT JOIN artist_info ON artist_info.artist_id=artist.id '
                  'WHERE artist.norm=? AND artist.id<>?', r['norm'], aid)
        if hit:
            out.append(hit)
        if len(out) >= limit:
            break
    return out


@app.get('/api/albums')
def albums(sort: str = 'artist', pool: str = None, limit: int = Query(500, le=5000), offset: int = 0):
    order = {'artist': 'artist.sort_name, album.year, album.sort_title',
             'title': 'album.sort_title',
             'year': '(album.year IS NULL), album.year DESC, album.sort_title',
             'tracks': 'album.n_tracks DESC'}.get(sort, 'artist.sort_name')
    where, args = pool_where(pool)
    return collapse_collections(rows(
        'SELECT album.id,album.title,album.year,album.n_tracks,album.duration,'
        'album.art_w,album.art_kind,album.collection_id,album.pool,'
        'artist.name AS artist,artist.id AS artist_id '
        f'FROM album JOIN artist ON artist.id=album.artist_id {where} '
        f'ORDER BY {order} LIMIT ? OFFSET ?', *args, limit, offset))

@app.get('/api/album/{alid}')
def album(alid: int):
    a = one('SELECT album.*, artist.name AS artist FROM album '
            'JOIN artist ON artist.id=album.artist_id WHERE album.id=?', alid)
    if not a:
        raise HTTPException(404)
    a.pop('art_path', None)          # never leak a filesystem path to the client
    if a.get('collection_id'):
        a['collection'] = one('SELECT id,name,n_discs FROM collection WHERE id=?',
                              a['collection_id'])
    a['tracks'] = rows(
        'SELECT id,skey,disc,track,title,track_artist,duration,bitrate,ext,mime,has_art '
        'FROM track WHERE album_id=? ORDER BY sort_key', alid)
    _mark_synced(a['tracks'])
    return a

@app.get('/api/tracks')
def tracks_by_id(ids: str):
    """Rehydrate player queue entries from bare ids.

    The queue is persisted (and read back off a speaker) as ids only; this turns
    them into the objects the player paints, preserving the requested order --
    SQL IN does not, and the order IS the queue."""
    try:
        want = [int(x) for x in ids.split(',') if x.strip()][:600]
    except ValueError:
        raise HTTPException(400, 'ids must be integers')
    if not want:
        return []
    ph = ','.join('?' * len(want))
    got = rows('SELECT track.id, track.title, track.duration, track.track_artist, track.has_art, '
               'album.id AS album_id, album.title AS album, album.artist_id, album.is_compilation, album.pool, '
               'artist.name AS album_artist '
               'FROM track JOIN album ON album.id=track.album_id '
               'JOIN artist ON artist.id=album.artist_id '
               f'WHERE track.id IN ({ph})', *want)
    by = {r['id']: r for r in got}
    out = []
    for i in want:
        r = by.get(i)
        if not r:
            continue
        out.append({'id': r['id'], 'title': r['title'], 'duration': r['duration'],
                    'album_id': r['album_id'], 'album': r['album'],
                    'artist': r['track_artist'] or r['album_artist'],
                    'artist_id': r['artist_id'], 'has_art': r['has_art'],
                    'is_compilation': r['is_compilation'], 'pool': r['pool']})
    return out

@app.get('/api/genres')
def genres():
    out = rows('SELECT id,shelf,n_albums,n_tracks FROM genre WHERE n_albums>0 '
               'ORDER BY n_tracks DESC')
    for g in out:
        g['sub'] = rows('SELECT name,n_albums FROM subgenre WHERE genre_id=? '
                        'ORDER BY n_albums DESC LIMIT 40', g['id'])
        g['art'] = [r['id'] for r in rows(
            'SELECT id FROM album WHERE genre_id=? AND art_kind IS NOT NULL AND pool=? '
            'ORDER BY art_w DESC LIMIT 4', g['id'], MAIN_POOL)]
    return out

@app.get('/api/genre/{gid}/albums')
def genre_albums(gid: int, sub: str = None, limit: int = Query(500, le=3000), offset: int = 0):
    where, args = 'WHERE album.genre_id=? AND album.pool=?', [gid, MAIN_POOL]
    if sub:
        where += ' AND album.subgenre=?'
        args.append(sub)
    return collapse_collections(rows(
        'SELECT album.id,album.title,album.year,album.n_tracks,album.art_w,'
        'album.art_kind,album.collection_id,artist.name AS artist FROM album '
        f'JOIN artist ON artist.id=album.artist_id {where} '
        'ORDER BY artist.sort_name, album.year LIMIT ? OFFSET ?', *args, limit, offset))

@app.get('/api/decades')
def decades():
    out = rows('SELECT decade, COUNT(*) n_albums, SUM(n_tracks) n_tracks FROM album '
               'WHERE decade IS NOT NULL AND pool=? GROUP BY decade ORDER BY decade', MAIN_POOL)
    unknown = one('SELECT COUNT(*) n_albums, SUM(n_tracks) n_tracks FROM album '
                  'WHERE decade IS NULL AND pool=?', MAIN_POOL)
    if unknown and unknown['n_albums']:
        unknown['decade'] = None
        out.append(unknown)
    return out

@app.get('/api/decade/{dec}')
def decade(dec: int, year: int = None, limit: int = Query(600, le=3000), offset: int = 0):
    """The 2000s alone hold 16,386 tracks, so that decade opens by year."""
    if year:
        where, args = 'WHERE album.year=? AND album.pool=?', [year, MAIN_POOL]
    else:
        where, args = 'WHERE album.decade=? AND album.pool=?', [dec, MAIN_POOL]
    albums_ = collapse_collections(rows(
        'SELECT album.id,album.title,album.year,album.n_tracks,album.art_w,'
        'album.art_kind,album.collection_id,artist.name AS artist FROM album '
        f'JOIN artist ON artist.id=album.artist_id {where} '
        'ORDER BY album.year, artist.sort_name LIMIT ? OFFSET ?', *args, limit, offset))
    years = rows('SELECT year, COUNT(*) n_albums, SUM(n_tracks) n_tracks FROM album '
                 'WHERE decade=? AND pool=? GROUP BY year ORDER BY year', dec, MAIN_POOL)
    return {'decade': dec, 'years': years, 'albums': albums_}

@app.get('/api/search')
def search(q: str, limit: int = Query(60, le=200)):
    if not q or len(q) < 2:
        return []
    term = ' '.join(f'{w}*' for w in re.findall(r'\w+', q))
    if not term:
        return []
    try:
        out = rows('SELECT kind,ref,label,detail,pool FROM search WHERE search MATCH ? '
                   'ORDER BY rank LIMIT ?', term, limit)
    except sqlite3.OperationalError:            # read model predates the pool column
        out = rows('SELECT kind,ref,label,detail FROM search WHERE search MATCH ? '
                   'ORDER BY rank LIMIT ?', term, limit)
    return out + _cdg_search(q, 12)


def _cdg_search(q, limit):
    """Karaoke discs, searched alongside the library. They live in their own
    index (cdg.db, rebuilt as discs are read) rather than in the read model's
    FTS table, so they are matched here and merged: the user expects one search
    box to find everything singable (20 Sep 2026). One row per song, the same
    best-read pressing the discs page shows."""
    if not _CDG_DB.exists():
        return []
    words = [w.lower() for w in re.findall(r'\w+', q)][:6]
    if not words:
        return []
    try:
        c = _cdg_conn()
    except HTTPException:
        return []
    try:
        where = ' AND '.join(["(LOWER(artist) LIKE ? OR LOWER(title) LIKE ?)"] * len(words))
        args = []
        for w in words:
            args += ['%' + w + '%', '%' + w + '%']
        rs = c.execute("SELECT id, artist, title, code, note, n_words, duration FROM song "
                       f"WHERE text_state='ok' AND {where} "
                       'ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE LIMIT 400', args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        c.close()
    best = _cdg_best_of_each(rs)
    # a hit in the title beats a hit only in the artist, as in the library's ranking
    joined = ' '.join(words)
    best.sort(key=lambda r: (0 if joined in (r['title'] or '').lower() else 1, (r['title'] or '').lower()))
    return [{'kind': 'disc', 'ref': r['id'], 'label': r['title'],
             'detail': (r['artist'] or '') + ' · Disc' + (' ' + r['code'] if r['code'] else '')
                       + (' · %d more' % r['alts'] if r['alts'] else ''),
             'pool': ''} for r in best[:limit]]

# ---------------------------------------------------------------- art
@app.get('/api/track-art/{tid}')
def track_art(tid: int, size: int = Query(600, ge=64, le=1400), g: str = None):
    """The picture embedded in ONE file. Albums keep their own cover; this is
    for a song on a compilation -- a Billboard year -- whose single's sleeve
    was embedded per track (library/trackart.py). Same cache and generation
    token as /api/art, keyed t{gen}_{tid}_{size}."""
    cached = ARTCACHE / f't{art_gen()}_{tid}_{size}.jpg'
    if cached.exists():
        return FileResponse(cached, media_type='image/jpeg',
                            headers={'Cache-Control': 'public, max-age=31536000, immutable'})
    t = one('SELECT rel_path FROM track WHERE id=?', tid)
    if not t:
        raise HTTPException(404)
    data = extract_embedded(t['rel_path'])
    if not data:
        raise HTTPException(404, 'no art')
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert('RGB')
            if max(im.size) > size:
                im.thumbnail((size, size), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, 'JPEG', quality=88, optimize=True)
            data = buf.getvalue()
    except Exception:
        raise HTTPException(404, 'bad art')
    try:
        cached.write_bytes(data)
    except OSError:
        pass
    return Response(content=data, media_type='image/jpeg',
                    headers={'Cache-Control': 'public, max-age=31536000, immutable'})


def extract_embedded(rel):
    import mutagen
    try:
        a = mutagen.File(str(safe_path(rel)))
        if a is None or a.tags is None:
            return None
        t = a.tags
        if hasattr(t, 'getall'):
            pics = t.getall('APIC')
            if pics:
                return pics[0].data
        if 'covr' in t:
            return bytes(t['covr'][0])
        if getattr(a, 'pictures', None):
            return a.pictures[0].data
        if 'metadata_block_picture' in t:
            import base64, mutagen.flac
            return mutagen.flac.Picture(
                base64.b64decode(t['metadata_block_picture'][0])).data
    except Exception:
        return None
    return None

@app.get('/api/art/{alid}')
def art(alid: int, size: int = Query(600, ge=64, le=1400), g: str = None):
    cached = ARTCACHE / f'{art_gen()}_{alid}_{size}.jpg'
    if cached.exists():
        return FileResponse(cached, media_type='image/jpeg',
                            headers={'Cache-Control': 'public, max-age=31536000, immutable'})
    a = one('SELECT art_path, art_kind FROM album WHERE id=?', alid)
    if not a:
        raise HTTPException(404)
    data = None
    if a['art_path'] and os.path.exists(a['art_path']):
        data = open(a['art_path'], 'rb').read()
    else:
        t = one('SELECT rel_path FROM track WHERE album_id=? ORDER BY sort_key LIMIT 8', alid)
        for r in rows('SELECT rel_path FROM track WHERE album_id=? ORDER BY sort_key LIMIT 8', alid):
            data = extract_embedded(r['rel_path'])
            if data:
                break
    if not data:
        raise HTTPException(404, 'no art')
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert('RGB')
            if max(im.size) > size:
                im.thumbnail((size, size), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, 'JPEG', quality=88, optimize=True)
            data = buf.getvalue()
        cached.write_bytes(data)
    except Exception:
        pass
    return Response(data, media_type='image/jpeg',
                    headers={'Cache-Control': 'public, max-age=31536000, immutable'})

# ---------------------------------------------------------------- audio
RANGE_RE = re.compile(r'bytes=(\d*)-(\d*)')

@app.get('/api/stream/{tid}')
@app.head('/api/stream/{tid}')
def stream(tid: int, request: Request, g: str = None):
    _check_build(g)
    t = one('SELECT rel_path, mime, size FROM track WHERE id=?', tid)
    if not t:
        raise HTTPException(404)
    path = safe_path(t['rel_path'])
    if not path.exists():
        raise HTTPException(404, 'file missing')
    total = path.stat().st_size
    hdr = {
        'Accept-Ranges': 'bytes',
        'Content-Type': t['mime'],
        'Cache-Control': 'no-cache',
    }
    rng = request.headers.get('range')
    if not rng:
        return FileResponse(path, media_type=t['mime'], headers=hdr)

    m = RANGE_RE.match(rng)
    if not m:
        return FileResponse(path, media_type=t['mime'], headers=hdr)
    start = int(m.group(1)) if m.group(1) else 0
    end = int(m.group(2)) if m.group(2) else total - 1
    end = min(end, total - 1)
    if start > end or start >= total:
        return Response(status_code=416,
                        headers={'Content-Range': f'bytes */{total}'})
    length = end - start + 1

    def chunks():
        with open(path, 'rb') as f:
            f.seek(start)
            left = length
            while left > 0:
                buf = f.read(min(65536, left))
                if not buf:
                    break
                left -= len(buf)
                yield buf

    hdr.update({'Content-Range': f'bytes {start}-{end}/{total}',
                'Content-Length': str(length)})
    return StreamingResponse(chunks(), status_code=206, headers=hdr,
                             media_type=t['mime'])

# ---------------------------------------------------------------- user state
import state as st
from pydantic import BaseModel

class FavIn(BaseModel):
    kind: str
    id: int
    on: bool = True

def _keys_for(kind, ident):
    """Stable key + labels for a track / album / artist id in the read-model."""
    if kind == 'track':
        r = one('SELECT track.skey, track.title, album.title alb, artist.name art '
                'FROM track JOIN album ON album.id=track.album_id '
                'JOIN artist ON artist.id=track.artist_id WHERE track.id=?', ident)
        return (r['skey'], r['title'], f"{r['art']} - {r['alb']}") if r else (None, None, None)
    if kind == 'album':
        r = one('SELECT album.title, artist.name art FROM album '
                'JOIN artist ON artist.id=album.artist_id WHERE album.id=?', ident)
        return (st.album_key(r['art'], r['title']), r['title'], r['art']) if r else (None, None, None)
    r = one('SELECT name FROM artist WHERE id=?', ident)
    return (st.artist_key(r['name']), r['name'], None) if r else (None, None, None)

@app.post('/api/favourite')
def set_favourite(f: FavIn):
    key, label, detail = _keys_for(f.kind, f.id)
    if not key:
        raise HTTPException(404)
    return {'kind': f.kind, 'id': f.id,
            'favourite': st.favourite(f.kind, key, label, detail, f.on)}

@app.get('/api/favourites')
def get_favourites(kind: str = None):
    return st.favourites(kind)

@app.get('/api/favourite/check')
def check_favourite(kind: str, id: int):
    key, _, _ = _keys_for(kind, id)
    return {'favourite': bool(key) and st.is_favourite(kind, key)}

class PlayIn(BaseModel):
    track_id: int
    room: str = None

@app.post('/api/play/start')
def play_start(p: PlayIn):
    r = one('SELECT track.skey, track.duration, album.title alb, artist.name art '
            'FROM track JOIN album ON album.id=track.album_id '
            'JOIN artist ON artist.id=track.artist_id WHERE track.id=?', p.track_id)
    if not r:
        raise HTTPException(404)
    pid = st.start_play(r['skey'], st.album_key(r['art'], r['alb']),
                        st.artist_key(r['art']), p.room)
    return {'play_id': pid, 'duration': r['duration']}

class TickIn(BaseModel):
    play_id: int
    seconds: float
    duration: float = None

@app.post('/api/play/tick')
def play_tick(t: TickIn):
    return {'counted': bool(st.update_play(t.play_id, t.seconds, t.duration))}

@app.get('/api/recent')
def recent(kind: str = 'track', limit: int = Query(60, le=300)):
    """Resolve stable keys back to things the UI can show."""
    out = []
    for r in st.recent(limit, kind):
        if kind == 'track':
            row = one('SELECT track.id,track.title,track.duration,album.id album_id,'
                      'album.title album,artist.name artist FROM track '
                      'JOIN album ON album.id=track.album_id '
                      'JOIN artist ON artist.id=track.artist_id WHERE track.skey=? LIMIT 1',
                      r['key'])
        else:
            row = None
        if row:
            row.update({'plays': r['plays'], 'last_played': r['last_played'],
                        'room': r.get('room')})
            out.append(row)
    return out

class PlNew(BaseModel):
    name: str


class PlAdd(BaseModel):
    track_ids: list = []      # tracks, or...
    album_id: int = None      # ...a whole album, in its own order
    collection_id: int = None # ...or a whole box set


class PlName(BaseModel):
    name: str


class PlOrder(BaseModel):
    skeys: list = []


def _items_for(track_ids=None, album_id=None, collection_id=None):
    """Turn a request into playlist items.

    The label and detail are stored alongside the key so a playlist can still
    show what a track WAS if the file later disappears, instead of silently
    shrinking."""
    sel = ("SELECT track.id, track.skey, track.title, "
           "COALESCE(NULLIF(track.track_artist,''), artist.name) AS artist, "
           "album.title AS album "
           "FROM track JOIN album ON album.id=track.album_id "
           "JOIN artist ON artist.id=album.artist_id ")
    if album_id:
        got = rows(sel + 'WHERE track.album_id=? ORDER BY track.sort_key', album_id)
    elif collection_id:
        got = rows(sel + 'WHERE album.collection_id=? '
                   'ORDER BY (album.disc_no IS NULL), album.disc_no, track.sort_key',
                   collection_id)
    else:
        ids = [int(i) for i in (track_ids or [])][:2000]
        if not ids:
            return []
        ph = ','.join('?' * len(ids))
        found = {r['id']: r for r in rows(sel + f'WHERE track.id IN ({ph})', *ids)}
        got = [found[i] for i in ids if i in found]   # keep the requested order
    return [{'skey': r['skey'], 'label': r['title'],
             'detail': (r['artist'] or '') + (' — ' + r['album'] if r['album'] else '')}
            for r in got]


@app.get('/api/pl')
def pl_all():
    """The user's own playlists (the smart ones live at /api/playlists)."""
    return st.pl_list()


@app.post('/api/pl')
def pl_new(b: PlNew):
    try:
        return st.pl_create(b.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get('/api/pl/{pid}')
def pl_get(pid: int):
    d = st.pl_tracks(pid, str(DB))
    if d is None:
        raise HTTPException(404, 'no such playlist')
    return d


@app.post('/api/pl/{pid}/add')
def pl_add(pid: int, b: PlAdd):
    items = _items_for(b.track_ids, b.album_id, b.collection_id)
    if not items:
        raise HTTPException(400, 'nothing to add')
    return st.pl_add(pid, items)


@app.post('/api/pl/{pid}/remove')
def pl_remove(pid: int, skey: str):
    st.pl_remove(pid, skey)
    return {'ok': True}


@app.post('/api/pl/{pid}/rename')
def pl_rename(pid: int, b: PlName):
    try:
        return {'ok': st.pl_rename(pid, b.name)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post('/api/pl/{pid}/order')
def pl_order(pid: int, b: PlOrder):
    st.pl_reorder(pid, b.skeys)
    return {'ok': True}


@app.delete('/api/pl/{pid}')
def pl_del(pid: int):
    st.pl_delete(pid)
    return {'ok': True}


@app.get('/api/playlists')
def playlists():
    return st.smart_catalogue(str(DB))

@app.get('/api/playlist/{key}')
def playlist(key: str, limit: int = Query(200, le=1000)):
    try:
        tracks = st.smart_playlist(key, str(DB), limit=limit)
    except ValueError:
        raise HTTPException(404, 'no such playlist')
    for t in tracks:
        t.pop('skey', None)
    return tracks

import lyrics as lyr

@app.get('/api/lyrics/by-name')
def get_lyrics_by_name(artist: str, title: str, album: str = None,
                       duration: int = None, force: bool = False):
    """Lyrics for something not in the library.

    A room playing Spotify or radio has no track id here, but LRCLIB is keyed on
    artist and title anyway. The cache key is derived the same way the ETL builds
    one so a track that IS in the library shares the cached entry."""
    import hashlib
    a = (artist or '').strip().lower()
    t = (title or '').strip().lower()
    if not (a and t):
        raise HTTPException(400, 'artist and title required')
    d = int(round((duration or 0) / 5.0))
    skey = hashlib.sha1(f'{a}|{t}|{d}'.encode()).hexdigest()[:16]
    r = lyr.lookup(skey, artist, title, album, duration, force)
    return {'status': r['status'], 'source': r['source'], 'url': r['url'],
            'art': r['art'], 'plain': r['plain'],
            'synced': lyr.parse_synced(r['synced']) if r['synced'] else None}

@app.get('/api/lyrics/warm')
def warm_lyrics(ids: str):
    """Resolve 'unknown' lyric status for the tracks on screen, at most 40.

    This is the one place that goes to the network for a list rather than for
    the song being played: the client calls it after an album has rendered, so
    the microphones can appear on rows without the page waiting for them. Each
    result is cached (misses too), so an album costs the network once."""
    try:
        want = [int(x) for x in ids.split(',') if x.strip()][:40]
    except ValueError:
        raise HTTPException(400, 'ids must be integers')
    if not want:
        return {}
    ph = ','.join('?' * len(want))
    got = rows('SELECT track.id, track.skey, track.title, track.duration, '
               'album.title alb, COALESCE(NULLIF(track.track_artist, \'\'), artist.name) art '
               'FROM track JOIN album ON album.id=track.album_id '
               'JOIN artist ON artist.id=album.artist_id '
               f'WHERE track.id IN ({ph})', *want)
    out = {}
    for t in got:
        try:
            r = lyr.lookup(t['skey'], t['art'], t['title'], t['alb'], t['duration'])
        except Exception:
            continue
        out[t['id']] = 'yes' if (r.get('synced') or '').strip() else 'no'
        time.sleep(0.2)          # a polite gap between lookups on a free service
    return out

@app.get('/api/lyrics/{tid}')
def get_lyrics(tid: int, force: bool = False):
    # the performer when the track has one (a compilation's "Various Artists"
    # album credit finds no lyrics for anybody -- the Billboard years, 3 Sep 2026)
    t = one('SELECT track.skey, track.title, track.duration, album.title alb, '
            'COALESCE(NULLIF(track.track_artist, \'\'), artist.name) art '
            'FROM track JOIN album ON album.id=track.album_id '
            'JOIN artist ON artist.id=track.artist_id WHERE track.id=?', tid)
    if not t:
        raise HTTPException(404)
    r = lyr.lookup(t['skey'], t['art'], t['title'], t['alb'], t['duration'], force)
    return {'status': r['status'], 'source': r['source'], 'url': r['url'],
            'art': r['art'], 'plain': r['plain'],
            'synced': lyr.parse_synced(r['synced']) if r['synced'] else None}


def _mark_synced(tracks):
    """Stamp each track dict with lyrics = 'yes' | 'no' | 'unknown'.

    Cache-only, one query for the whole list: this decides whether a track row
    gets a microphone, and a list is drawn far more often than anyone sings.
    'unknown' is honest; /api/lyrics/warm resolves it when a list is on screen.
    Strips skey from the payload afterwards -- the client keys nothing by it."""
    keys = [t.get('skey') for t in tracks if t.get('skey')]
    have = {}
    if keys:
        try:
            c = lyr.db()
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                ph = ','.join('?' * len(chunk))
                for r in c.execute(f'SELECT skey, synced, status, fetched FROM lyric '
                                   f'WHERE skey IN ({ph})', chunk):
                    if (r['synced'] or '').strip():
                        have[r['skey']] = 'yes'
                    elif r['status'] == 'ok' or                             (time.time() - (r['fetched'] or 0)) < lyr.MISS_TTL:
                        have[r['skey']] = 'no'      # a fresh miss; retried after a week
            c.close()
        except Exception:
            pass
    try:
        split = kar.ready_keys()
        busy = {j['skey']: j['state'] for j in kar.queued() if j.get('state') in ('queued', 'running')}
    except Exception:
        split, busy = set(), {}
    for t in tracks:
        k = t.pop('skey', None)
        t['lyrics'] = have.get(k, 'unknown')
        t['stems'] = 'ready' if k in split else busy.get(k, 'none')


def _check_build(g):
    """Refuse to serve audio to a page that predates the current read model.

    Track ids are reassigned by every rebuild -- measured at 14% survival -- so
    an id from an older build points at a DIFFERENT recording. The page would
    show the right title, art and lyrics from its own stale copy while the
    speaker played something else entirely. Better a clear error the client can
    recover from than confidently wrong audio."""
    if g and g != art_gen():
        raise HTTPException(409, 'stale build: reload')


@app.get('/media/{spec}')
@app.head('/media/{spec}')
def media(spec: str, request: Request):
    """Same stream, but the URL carries the file extension.

    Sonos sniffs the format from the path and answers 714 "Illegal MIME-type" for a
    URL ending in a bare id, however correct the Content-Type header is. So speakers
    get /media/14747.m4a while the browser can keep using /api/stream/14747.
    """
    m = re.match(r'^(\d+)(?:\.([A-Za-z0-9]+))?$', spec)
    if not m:
        raise HTTPException(404)
    return stream(int(m.group(1)), request)

# ---------------------------------------------------------------- sonos
import sonos as sn
import threading as _th

_SPEAKERS = {'at': 0, 'rooms': [], 'lock': _th.Lock()}
SPEAKER_TTL = 3600          # discovery costs ~9s; speakers are added rarely, so hold it an hour
ROOMS_TTL = 3600            # the assembled room list, likewise

# The room list is rebuilt from zone topology on every request, which is quick
# but not free, and every SPEAKER_TTL it also pays for a fresh SSDP sweep. The
# set of speakers in a house is close to static, so serve it from here and let
# a group change or an explicit refresh invalidate it.
_ROOMS = {'at': 0, 'data': None}

def lan_host():
    """The address a SPEAKER must use. Sonos fetches the stream itself, so a
    localhost URL is useless to it."""
    return f'http://{lan_ip()}' + ('' if PORT[0] == 80 else f':{PORT[0]}')

PORT = [8730]

def speakers(force=False):
    with _SPEAKERS['lock']:
        if force or time.time() - _SPEAKERS['at'] > SPEAKER_TTL or not _SPEAKERS['rooms']:
            try:
                _SPEAKERS['rooms'] = sn.discover()
                _SPEAKERS['at'] = time.time()
            except Exception:
                pass
        return _SPEAKERS['rooms']

@app.get('/api/sonos/rooms')
def sonos_rooms(refresh: bool = False):
    if not refresh and _ROOMS['data'] and time.time() - _ROOMS['at'] < ROOMS_TTL:
        return {**_ROOMS['data'], 'cached': True}
    return _sonos_rooms_build(refresh)


def _sonos_rooms_build(refresh: bool = False):
    """Rooms, not devices.

    Discovery finds every physical player, which is the wrong unit: a stereo pair is
    two devices but ONE room, and its left-hand satellite is marked Invisible and
    must never be sent audio. The zone topology already models this correctly, so
    the room list is built from topology and discovery is used only to find an entry
    point into each system (and to catch any player whose system reports nothing).
    """
    found = speakers(refresh)
    rooms, covered, groups = [], set(), []
    for d in found:
        if d['name'] in covered:
            continue
        try:
            gs = sn.zone_groups(d['ip'])
        except Exception:
            gs = []
        if not gs:
            continue
        visible = sorted({m['name'] for g in gs for m in g['members']})
        sysid = '|'.join(visible)
        groups.extend(gs)
        for g in gs:
            for m in g['members']:          # zone_groups already drops satellites
                if m['name'] in covered:
                    continue
                covered.add(m['name'])
                dev = next((x for x in found if x['ip'] == m['ip']), None)
                rooms.append({
                    'name': m['name'], 'ip': m['ip'], 'uuid': m['uuid'],
                    'model': (dev or {}).get('model'),
                    'group': g['name'], 'members': len(g['members']),
                    'coordinator_ip': g['coordinator_ip'], 'system': sysid,
                    'stereo_pair': sum(1 for x in found if x['name'] == m['name']) > 1,
                })
    # a player whose system answered nothing still deserves to be listed --
    # unless it is the bonded half of a stereo pair whose other half is
    # missing, in which case it must be shown but not offered: a queue sent to
    # an Invisible satellite is refused (the "Sonos Moves" case, 1 Sep 2026)
    for d in found:
        if d['name'] not in covered:
            covered.add(d['name'])
            room = {**d, 'group': d['name'], 'members': 1,
                    'coordinator_ip': d['ip'], 'system': d['name'],
                    'stereo_pair': False}
            try:
                inv = sn.invisible(d['ip']).get(d.get('uuid'))
            except Exception:
                inv = None
            if inv:
                room['stereo_pair'] = True
                room['unavailable'] = ('Half of a stereo pair; the other speaker is off the network'
                                       if not inv['partner_present'] else
                                       'Half of a stereo pair; pick its partner')
            rooms.append(room)
    rooms.sort(key=lambda r: r['name'].lower())
    return {'host': lan_host(), 'rooms': rooms}

class SonosPlay(BaseModel):
    ip: str
    track_ids: list[int]
    start: int = 0

@app.post('/api/sonos/play')
def sonos_play(p: SonosPlay):
    if not p.track_ids:
        raise HTTPException(400, 'no tracks')
    host = lan_host()
    ids = p.track_ids[:80]
    q = ','.join('?' * len(ids))
    meta = {r['id']: r for r in rows(
        f'SELECT track.id,track.title,track.ext,track.mime,track.duration,track.has_art,'
        f'album.id album_id,album.title album,album.is_compilation,album.pool,'
        f'COALESCE(NULLIF(track.track_artist, \'\'), artist.name) artist FROM track '
        f'JOIN album ON album.id=track.album_id '
        f'JOIN artist ON artist.id=track.artist_id WHERE track.id IN ({q})', *ids)}
    tracks = []
    for tid in ids:
        m = meta.get(tid)
        if not m:
            continue
        tracks.append({'url': f"{host}/media/{m['id']}{m['ext']}",
                       'title': m['title'], 'artist': m['artist'], 'album': m['album'],
                       'art': (f"{host}/api/track-art/{m['id']}?size=600"
                               if m['has_art'] and (m['is_compilation'] or m['pool'] != MAIN_POOL)
                               else f"{host}/api/art/{m['album_id']}?size=600"),
                       'duration': m['duration'], 'mime': m['mime']})
    if not tracks:
        raise HTTPException(404)
    # the speaker's own uuid is what the queue URI is addressed to
    uuid = None
    for r in speakers():
        if r['ip'] == p.ip:
            uuid = r.get('uuid')
    if not uuid:
        info = sn.describe(p.ip)
        uuid = info and info.get('uuid')
    if not uuid:
        raise HTTPException(502, 'could not identify that speaker')
    try:
        sn.play_queue(p.ip, uuid, tracks, min(p.start, len(tracks) - 1))
    except Exception as e:
        raise HTTPException(502, f'sonos refused: {e}')
    first = tracks[min(p.start, len(tracks) - 1)]
    return {'ok': True, 'playing': first['title'], 'queued': len(tracks)}

class SonosCtl(BaseModel):
    ip: str
    action: str

@app.post('/api/sonos/control')
def sonos_control(c: SonosCtl):
    allowed = {'Play', 'Pause', 'Stop', 'Next', 'Previous'}
    if c.action not in allowed:
        raise HTTPException(400, 'bad action')
    try:
        sn.simple(c.ip, c.action)
    except Exception as e:
        raise HTTPException(502, str(e))
    return {'ok': True}

@app.get('/api/sonos/state')
def sonos_state(ip: str, group: bool = False):
    try:
        vol = sn.group_volume(ip) if group else sn.volume(ip)
        return {**sn.transport_info(ip), **sn.position_info(ip),
                'volume': vol, 'muted': sn.mute(ip)}
    except Exception as e:
        raise HTTPException(502, str(e))

@app.get('/api/sonos/art')
def sonos_art(ip: str, u: str):
    """Proxy cover art for whatever a speaker is playing.

    The albumArtURI Sonos hands back is usually a path relative to the player
    itself (/getaa?...), so it means nothing to the browser without the host;
    for cloud services it can be an absolute URL. Fetching it here covers both
    and keeps the page from reaching out to third-party hosts directly."""
    import urllib.request, urllib.parse
    if u.startswith('/'):
        target = f'http://{ip}:{sn.SONOS_PORT}{u}'
    elif u.startswith('http://') or u.startswith('https://'):
        target = u
    else:
        raise HTTPException(400, 'bad art url')
    try:
        req = urllib.request.Request(target, headers={'User-Agent': 'Jukebox/1.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(4_000_000)
            ct = r.headers.get('Content-Type', 'image/jpeg')
    except Exception:
        raise HTTPException(404, 'no art')
    return Response(content=body, media_type=ct,
                    headers={'Cache-Control': 'public, max-age=900'})

@app.get('/api/sonos/queue')
def sonos_queue(ip: str):
    """The speaker's queue as our track ids, in order.

    Entries this server did not hand out (a radio stream, a Spotify track queued
    from the Sonos app) come back as None so the client can see the queue is not
    one of ours rather than silently mis-mapping it."""
    try:
        uris = sn.queue(ip)
    except Exception as e:
        raise HTTPException(502, str(e))
    ids = []
    for u in uris:
        m = re.search(r'/media/(\d+)(?:\.[A-Za-z0-9]+)?(?:\?|$)', u or '')
        ids.append(int(m.group(1)) if m else None)
    return {'ids': ids, 'n': len(ids), 'ours': sum(1 for i in ids if i is not None)}

class SonosVol(BaseModel):
    ip: str
    level: int = None      # absolute 0-100
    delta: int = None      # or nudge by a step, which is race-free
    group: bool = False    # grouped rooms move together, keeping their balance
    muted: bool = None

@app.post('/api/sonos/volume')
def sonos_volume(v: SonosVol):
    try:
        if v.muted is not None:
            return {'muted': sn.mute(v.ip, v.muted)}
        if v.delta is not None:
            lv = (sn.group_volume(v.ip, delta=v.delta) if v.group
                  else sn.relative_volume(v.ip, v.delta))
        elif v.level is not None:
            lv = sn.group_volume(v.ip, v.level) if v.group else sn.volume(v.ip, v.level)
        else:
            lv = sn.group_volume(v.ip) if v.group else sn.volume(v.ip)
        return {'volume': lv}
    except Exception as e:
        raise HTTPException(502, str(e))

class SonosMode(BaseModel):
    ip: str
    repeat: str = 'off'
    shuffle: bool = False


@app.post('/api/sonos/mode')
def sonos_mode(m: SonosMode):
    try:
        return {'mode': sn.set_play_mode(m.ip, m.repeat, m.shuffle)}
    except Exception as e:
        raise HTTPException(502, str(e))


class SonosSeek(BaseModel):
    ip: str
    seconds: float = 0


@app.post('/api/sonos/seek')
def sonos_seek(s: SonosSeek):
    try:
        return {'seconds': sn.seek(s.ip, s.seconds)}
    except Exception as e:
        raise HTTPException(502, str(e))


class SonosFlag(BaseModel):
    ip: str
    on: bool = True


@app.post('/api/sonos/crossfade')
def sonos_crossfade(f: SonosFlag):
    try:
        return {'on': sn.crossfade(f.ip, f.on)}
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get('/api/sonos/crossfade')
def sonos_crossfade_get(ip: str):
    try:
        return {'on': sn.crossfade(ip)}
    except Exception as e:
        raise HTTPException(502, str(e))


class SonosSleep(BaseModel):
    ip: str
    seconds: int = 0          # 0 cancels


@app.post('/api/sonos/sleep')
def sonos_sleep(b: SonosSleep):
    try:
        return {'seconds': sn.sleep_timer(b.ip, max(0, b.seconds))}
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get('/api/sonos/sleep')
def sonos_sleep_get(ip: str):
    try:
        return {'seconds': sn.sleep_timer(ip)}
    except Exception as e:
        raise HTTPException(502, str(e))


class SonosGroupSet(BaseModel):
    ips: list = []        # rooms that should play together; the FIRST is the coordinator


@app.post('/api/sonos/group-set')
def sonos_group_set(g: SonosGroupSet):
    """Make exactly this set of rooms one group.

    Sonos has no "play in these rooms" call -- a group is built by pointing each
    member at a coordinator, and torn down by telling a member to stand alone.
    So this reconciles: rooms in the request join the coordinator, rooms
    currently in its group but no longer wanted are released."""
    ips = [i for i in (g.ips or []) if i]
    if not ips:
        raise HTTPException(400, 'no rooms given')
    coord = ips[0]
    want = set(ips[1:])
    d = sn.describe(coord)
    if not d or not d.get('uuid'):
        raise HTTPException(502, 'could not identify the coordinator')
    cu = d['uuid']

    have = set()
    try:
        for grp in sn.zone_groups(coord):
            if any(m['ip'] == coord for m in grp['members']):
                have = {m['ip'] for m in grp['members'] if m['ip'] != coord}
    except Exception:
        pass

    joined, released, errors = [], [], []
    for ip in sorted(have - want):
        try:
            sn.unjoin(ip); released.append(ip)
        except Exception as e:
            errors.append({'ip': ip, 'error': str(e)[:120]})
    for ip in ips[1:]:
        if ip in have:
            continue
        try:
            sn.join(ip, cu); joined.append(ip)
        except Exception as e:
            errors.append({'ip': ip, 'error': str(e)[:120]})
    speakers(True)                         # topology changed; refresh the room cache
    _ROOMS['at'] = 0
    return {'coordinator': coord, 'joined': joined, 'released': released,
            'errors': errors}


class SonosGroup(BaseModel):
    ip: str
    coordinator_uuid: str = None      # omit to leave the group

@app.post('/api/sonos/group')
def sonos_group(g: SonosGroup):
    try:
        if g.coordinator_uuid:
            sn.join(g.ip, g.coordinator_uuid)
        else:
            sn.unjoin(g.ip)
    except Exception as e:
        raise HTTPException(502, str(e))
    _SPEAKERS['at'] = 0               # topology changed, re-read it
    _ROOMS['at'] = 0
    return {'ok': True}

@app.get('/api/build')
def build_id():
    return {'build': art_gen()}


@app.get('/api/recent-added')
def recent_added(limit: int = 12):
    """Albums by the newest file they hold. File times survived the migration
    (robocopy keeps them), so this is roughly when the record was ripped or
    bought -- the only "added" date the library has."""
    limit = max(1, min(60, limit))
    try:
        raw = rows('SELECT album.id, album.title, album.added, album.n_tracks, album.collection_id, '
                   'artist.name artist, artist.id artist_id '
                   'FROM album JOIN artist ON artist.id = album.artist_id '
                   'WHERE album.added IS NOT NULL AND album.pool=? ORDER BY album.added DESC LIMIT ?',
                   MAIN_POOL, limit * 4)
    except sqlite3.OperationalError:
        return []                      # read model predates the column
    # a two-disc album arrives as two entries a second apart and showed twice
    # on the shelf (10 Sep 2026); fold the discs to one tile, dated by the newest
    added = {}
    for r in raw:
        if r.get('collection_id') and r['collection_id'] not in added:
            added[r['collection_id']] = r['added']
    out = collapse_collections(raw)
    for r in out:
        if r.get('kind') in ('collection', 'discalbum'):
            r['added'] = added.get(r['id'])
    return out[:limit]


@app.get('/api/health')
def health():
    return {'ok': True,
            'tracks': one('SELECT COUNT(*) n FROM track')['n'],
            'albums': one('SELECT COUNT(*) n FROM album')['n'],
            'artists': one('SELECT COUNT(*) n FROM artist')['n']}

DIAG = []


@app.post('/api/diag')
async def api_diag(request: Request):
    """Collect what a device actually supports, so guessing stops."""
    body = await request.json()
    body['seen'] = time.strftime('%H:%M:%S')
    DIAG.append(body)
    return {'ok': True}


@app.get('/api/diag')
def api_diag_read():
    return DIAG[-5:]



# ======================================================================= karaoke
import karaoke as kar
import state as st_mod      # `st` is a local in the status endpoint below

# ======================================================================= backups
# state.db is the only file here that cannot be rebuilt: favourites, play
# history, playlists, karaoke offsets. A copy is taken through SQLite's online
# backup API (consistent even mid-write, WAL included) once a day, five kept,
# in backups/ beside the databases. The check runs hourly in a daemon thread so
# a server that is up for weeks still backs up, and a server started after a
# long gap backs up at once.
BACKUP_DIR = HERE / 'backups'
BACKUP_KEEP = 5
BACKUP_EVERY = 24 * 3600
STARTED = time.time()


def backup_state(force=False):
    """-> path of the copy made, or None when today's already exists."""
    BACKUP_DIR.mkdir(exist_ok=True)
    have = sorted(BACKUP_DIR.glob('state-*.db'))
    if not force and have and time.time() - have[-1].stat().st_mtime < BACKUP_EVERY:
        return None
    dst = BACKUP_DIR / time.strftime('state-%Y%m%d-%H%M.db')
    src = sqlite3.connect(st_mod.STATE_DB, timeout=30)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close(); src.close()
    for old in sorted(BACKUP_DIR.glob('state-*.db'))[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return dst


def _backup_loop():
    while True:
        try:
            got = backup_state()
            if got:
                print('backup: state.db ->', got.name)
        except Exception as e:
            print('backup: failed:', e)
        time.sleep(3600)


_th.Thread(target=_backup_loop, daemon=True, name='backup').start()


# ======================================================================= system page
# What the System page shows and does: server, library, karaoke store, backups,
# and the one maintenance job the user runs by hand -- a rescan of the music
# folders followed by a rebuild of the read model. Everything here is cheap to
# read except the rescan, which runs the library scripts as subprocesses in a
# thread and reports its progress for the page to poll.
import subprocess as _sp

LIB_DIR = HERE.parent / 'library'
LIB_DB = LIB_DIR / 'library.db'
ROOT_MUSIC = ROOT


def _version():
    try:
        h = _sp.run(['git', 'log', '-1', '--format=%h %cd', '--date=format:%Y-%m-%d %H:%M'],
                    cwd=str(HERE.parent), capture_output=True, text=True, timeout=5).stdout.strip()
        return h or 'unknown'
    except Exception:
        return 'unknown'


VERSION = _version()
RESCAN = {'state': 'idle', 'step': '', 'started': None, 'finished': None, 'log': [], 'roots': [],
          'lock': _th.Lock()}


def _fsize(p):
    try:
        return Path(p).stat().st_size
    except OSError:
        return 0


def _tail(p, n=8):
    try:
        with open(p, encoding='utf-8', errors='replace') as f:
            return [l.rstrip() for l in f.readlines()[-n:]]
    except OSError:
        return []


# ---- the television app: a sideloadable APK the server hands out itself.
# The TV's "Downloader" app fetches a URL and offers to install what it gets,
# so the address is all a person has to type; the System page shows it.
DIST = HERE / 'dist'


@app.get('/mm.apk')
@app.get('/musicmate.apk')
def tv_apk():
    """/mm.apk is the name to type with a remote; the long name still works."""
    p = DIST / 'mm.apk'
    if not p.exists():
        raise HTTPException(404, 'the TV app has not been built; run tvapp\\build.ps1')
    return FileResponse(str(p), media_type='application/vnd.android.package-archive',
                        filename='mm.apk', headers={'Cache-Control': 'no-store'})


@app.get('/api/admin/status')
def admin_status():
    lib = {}
    try:
        c = sqlite3.connect(f'file:{LIB_DB}?mode=ro', uri=True, timeout=10)
        lib['files'] = c.execute("SELECT COUNT(*) FROM files WHERE kind='audio'").fetchone()[0]
        lib['last_scan'] = c.execute('SELECT MAX(scanned_at) FROM files').fetchone()[0]
        lib['pools'] = [dict(name=r[0], files=r[1]) for r in
                        c.execute("SELECT src, COUNT(*) FROM files WHERE kind='audio' GROUP BY src ORDER BY 2 DESC")]
        lib['unfingerprinted'] = c.execute("SELECT COUNT(*) FROM files WHERE kind='audio' AND fp IS NULL").fetchone()[0]
        c.close()
    except Exception as e:
        lib['error'] = str(e)
    lib['size'] = _fsize(LIB_DB)
    try:
        folders = sorted(d.name for d in ROOT_MUSIC.iterdir()
                         if d.is_dir() and not d.name.startswith('_') and not d.name.startswith('$'))
    except OSError:
        folders = []
    lyrics = {}
    try:
        c = lyr.db()
        lyrics = dict(zip(('cached', 'synced', 'plain'), c.execute(
            "SELECT COUNT(*), SUM(synced IS NOT NULL AND synced!=''), SUM(plain IS NOT NULL AND plain!='') FROM lyric").fetchone()))
    except Exception:
        pass
    jobs = kar.queued()
    kq = {k: sum(1 for j in jobs if j.get('state') == k) for k in ('queued', 'running', 'error')}
    backups = sorted(BACKUP_DIR.glob('state-*.db')) if BACKUP_DIR.exists() else []
    rescan = {k: v for k, v in RESCAN.items() if k != 'lock'}
    rescan['log'] = rescan['log'][-40:]
    return {
        'server': {'version': VERSION, 'started': STARTED, 'uptime': time.time() - STARTED,
                   'pid': os.getpid(), 'python': sys.version.split()[0], 'host': lan_host(),
                   'launcher_log': _tail(HERE / 'logs' / 'launcher.log', 6),
                   'errors': _tail(HERE / 'logs' / 'server.err.log', 6)},
        'model': {'tracks': one('SELECT COUNT(*) n FROM track')['n'],
                  'albums': one('SELECT COUNT(*) n FROM album')['n'],
                  'artists': one('SELECT COUNT(*) n FROM artist')['n'],
                  'built': Path(DB).stat().st_mtime if Path(DB).exists() else None,
                  'size': _fsize(DB)},
        'library': lib, 'folders': folders,
        'karaoke': {'ready': len(kar.ready_keys()), 'queue': kq, 'store': kar.store_stats(),
                    'available': kar.available(),
                    'aligned': sum(1 for k in kar.ready_keys() if kar.aligned(k))},
        'lyrics': dict(lyrics, warm={k: v for k, v in WARM.items() if k != 'lock'}),
        'state': {'size': _fsize(st_mod.STATE_DB),
                  'backups': [{'name': b.name, 'size': b.stat().st_size, 'when': b.stat().st_mtime} for b in backups]},
        'rooms': {'cached': len((_ROOMS.get('data') or {}).get('rooms', [])), 'at': _ROOMS.get('at')},
        'tvapp': ({'size': (DIST / 'mm.apk').stat().st_size, 'built': (DIST / 'mm.apk').stat().st_mtime,
                   'url': lan_host() + '/mm.apk'} if (DIST / 'mm.apk').exists() else None),
        'rescan': rescan,
    }


class RescanReq(BaseModel):
    roots: list = ['VariousArtists']
    fingerprint: bool = True
    covers: bool = True       # afterwards, fetch covers for albums that have none


def _rescan_run(roots, fingerprint, covers=True):
    def log(line):
        with RESCAN['lock']:
            RESCAN['log'].append(time.strftime('%H:%M:%S ') + line)
            del RESCAN['log'][:-400]

    def run(step, cmd, cwd):
        with RESCAN['lock']:
            RESCAN['step'] = step
        log('> ' + ' '.join(cmd))
        p = _sp.Popen(cmd, cwd=str(cwd), stdout=_sp.PIPE, stderr=_sp.STDOUT,
                      text=True, errors='replace', bufsize=1,
                      env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'})
        for line in iter(p.stdout.readline, ''):
            if line.strip():
                log(line.rstrip())
        p.stdout.close()
        rc = p.wait()
        if rc != 0:
            raise RuntimeError('%s failed (exit %d)' % (step, rc))

    py = sys.executable
    try:
        if roots:
            run('scan', [py, 'musiclib.py', 'scan'] + roots, LIB_DIR)
            if fingerprint:
                run('fingerprint', [py, 'musiclib.py', 'fingerprint', '--workers', '4'], LIB_DIR)
        if covers:
            # the library's bulk cover fetcher (Deezer, then iTunes), only for
            # folders whose best art is small or missing and not yet tried; the
            # rebuild then copies what it found into the read model
            run('covers: survey', [py, 'artfetch.py', 'survey'], LIB_DIR)
            run('covers: fetch', [py, 'artfetch.py', 'fetch'], LIB_DIR)
        run('rebuild', [py, 'etl.py'], HERE)
        with RESCAN['lock']:
            RESCAN['state'] = 'done'; RESCAN['step'] = ''
        log('done: the read model has been rebuilt; open pages should be reloaded')
    except Exception as e:
        with RESCAN['lock']:
            RESCAN['state'] = 'error'; RESCAN['step'] = ''
        log('ERROR ' + str(e))
    finally:
        with RESCAN['lock']:
            RESCAN['finished'] = time.time()


@app.post('/api/admin/rescan')
def admin_rescan(r: RescanReq):
    roots = [x for x in r.roots if (ROOT_MUSIC / x).is_dir() and '/' not in x and '\\' not in x and not x.startswith('_')]
    if not roots:
        raise HTTPException(400, 'no such folder under the music root')
    with RESCAN['lock']:
        if RESCAN['state'] == 'running':
            raise HTTPException(409, 'a rescan is already running')
        RESCAN.update(state='running', step='starting', started=time.time(), finished=None, log=[], roots=roots)
    _th.Thread(target=_rescan_run, args=(roots, r.fingerprint, r.covers), daemon=True, name='rescan').start()
    return {'ok': True, 'roots': roots}


@app.post('/api/admin/covers')
def admin_covers():
    """Fetch covers for every album that has none, then rebuild. Same job
    runner and progress panel as the rescan, without the scan."""
    with RESCAN['lock']:
        if RESCAN['state'] == 'running':
            raise HTTPException(409, 'a rescan is already running')
        RESCAN.update(state='running', step='starting', started=time.time(), finished=None, log=[], roots=[])
    _th.Thread(target=_rescan_run, args=([], False, True), daemon=True, name='covers').start()
    return {'ok': True}


@app.get('/api/admin/rescan')
def admin_rescan_state():
    with RESCAN['lock']:
        d = {k: v for k, v in RESCAN.items() if k != 'lock'}
    d['log'] = d['log'][-60:]
    return d


@app.post('/api/admin/backup')
def admin_backup():
    got = backup_state(force=True)
    return {'ok': True, 'name': got.name if got else None}


class RefetchArt(BaseModel):
    artist: str = None
    album: str = None


@app.post('/api/admin/refetch-art/{alid}')
def admin_refetch_art(alid: int, r: RefetchArt):
    """Replace one album's cover by hand, from the page rather than a terminal.

    Runs library/fixart.py for the album's folder (MusicBrainz release group ->
    Cover Art Archive front image -> the library's art cache and artcache.db,
    so the next full rebuild agrees), then points this read model's album row
    at the new file and drops the served thumbnails for it. The client busts
    its own cache with a fresh query token."""
    a = one('SELECT folder, title FROM album WHERE id=?', alid)
    if not a:
        raise HTTPException(404)
    cmd = [sys.executable, 'fixart.py', a['folder']]
    if r.artist:
        cmd += ['--artist', r.artist]
    if r.album:
        cmd += ['--album', r.album]
    try:
        p = _sp.run(cmd, cwd=str(LIB_DIR), capture_output=True, text=True, errors='replace',
                    timeout=90, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    except _sp.TimeoutExpired:
        raise HTTPException(504, 'the cover lookup took too long')
    log = [l for l in (p.stdout + p.stderr).splitlines() if l.strip()]
    if p.returncode != 0:
        raise HTTPException(502, ' / '.join(log[-3:]) or 'no cover found')
    local = a['folder'].replace('/', '__') + '.jpg'
    path = LIB_DIR / 'cache' / 'art' / local
    if not path.exists():
        raise HTTPException(502, 'fixart reported success but wrote nothing')
    width = None
    try:
        from PIL import Image
        with Image.open(path) as im:
            width = im.width
    except Exception:
        pass
    w = sqlite3.connect(DB, timeout=30)
    w.execute("UPDATE album SET art_path=?, art_kind='fetched', art_w=? WHERE id=?", (str(path), width or 0, alid))
    w.commit(); w.close()
    for old in ARTCACHE.glob(f'{art_gen()}_{alid}_*.jpg'):
        try:
            old.unlink()
        except OSError:
            pass
    return {'ok': True, 'width': width, 'log': log[-6:]}


# ---- lyric pre-warm: resolve the cache for songs nobody has opened yet, at
# night, a bounded batch at a time, so the first karaoke request on an unseen
# album is not the moment the network gets asked.
WARM = {'last_run': None, 'done': 0, 'left': None, 'running': False, 'lock': _th.Lock()}
WARM_HOUR = 4
WARM_BATCH = 1500
WARM_GAP = 0.5


def _warm_todo():
    have = set()
    try:
        c = lyr.db()
        have = {r[0] for r in c.execute('SELECT skey FROM lyric')}
    except Exception:
        pass
    todo = [t for t in rows('SELECT track.id, track.skey, track.title, track.duration, '
                            'album.title alb, COALESCE(NULLIF(track.track_artist, \'\'), artist.name) art FROM track '
                            'JOIN album ON album.id=track.album_id JOIN artist ON artist.id=album.artist_id '
                            'ORDER BY album.added DESC, album.id, track.sort_key')
            if t['skey'] not in have]
    return todo


def warm_lyrics(limit=WARM_BATCH):
    with WARM['lock']:
        if WARM['running']:
            return 0
        WARM['running'] = True
    n = 0
    try:
        todo = _warm_todo()
        WARM['left'] = len(todo)
        for t in todo[:limit]:
            try:
                lyr.lookup(t['skey'], t['art'], t['title'], t['alb'], t['duration'])
                n += 1
            except Exception:
                pass
            time.sleep(WARM_GAP)
        WARM['left'] = max(0, len(todo) - n)
    finally:
        with WARM['lock']:
            WARM['running'] = False
            WARM['last_run'] = time.time()
            WARM['done'] = n
    return n


def _warm_loop():
    time.sleep(120)
    while True:
        try:
            lt = time.localtime()
            last = WARM['last_run']
            if lt.tm_hour == WARM_HOUR and (not last or time.time() - last > 20 * 3600):
                warm_lyrics()
        except Exception as e:
            print('lyrics warm: failed:', e)
        time.sleep(600)


_th.Thread(target=_warm_loop, daemon=True, name='lyricwarm').start()


@app.post('/api/admin/warm-lyrics')
def admin_warm(limit: int = 300):
    if WARM['running']:
        raise HTTPException(409, 'already warming')
    _th.Thread(target=warm_lyrics, args=(max(1, min(limit, 3000)),), daemon=True).start()
    return {'ok': True}


@app.post('/api/admin/restart')
def admin_restart():
    """Exit; the launcher (serve.ps1) starts a fresh process within seconds.
    Anything queued for separation comes back through kar.restore()."""
    if kar.queued() and any(j.get('state') == 'running' for j in kar.queued()):
        note = 'the running separation job will start over'
    else:
        note = ''
    _th.Timer(0.6, lambda: os._exit(0)).start()
    return {'ok': True, 'note': note}

# jobs the previous process left queued or half-done come straight back
try:
    _n_restored = kar.restore()
    if _n_restored:
        print(f'karaoke: re-queued {_n_restored} separation job(s) from the last run')
except Exception as _e:
    print('karaoke: could not restore the queue:', _e)


def _kar_track(tid: int):
    """(skey, absolute source path, label) for a library track."""
    r = one("""SELECT track.skey, track.rel_path, track.title, track.duration,
                      track.album_id, album.title alb, artist.name art
               FROM track JOIN album ON album.id = track.album_id
               JOIN artist ON artist.id = track.artist_id
               WHERE track.id = ?""", tid)
    if not r:
        raise HTTPException(404, 'no such track')
    return r


def _has_synced(skey):
    """Cache-only: does this song already have time-synced lyrics?

    Deliberately never fetches. This is called to decide whether to draw a
    microphone, which happens for every row on screen -- going to the network
    for that would rate-limit the library in seconds. 'unknown' is an honest
    answer and the client resolves it when someone actually asks.
    """
    try:
        c = lyr.db()
        r = c.execute('SELECT synced, status FROM lyric WHERE skey=?', (skey,)).fetchone()
        c.close()
    except Exception:
        return 'unknown'
    if not r:
        return 'unknown'
    return 'yes' if (r[0] or '').strip() else 'no'


@app.get('/api/karaoke/status/{tid}')
def karaoke_status(tid: int):
    t = _kar_track(tid)
    st = kar.status(t['skey'])
    st['skey'] = t['skey']
    st['lyrics'] = _has_synced(t['skey'])
    st['available'] = kar.available()
    st['offset_ms'] = st_mod.kar_offset(t['skey'])
    st['title'] = t['title']
    st['artist'] = t['art']
    st['album'] = t['alb']
    st['duration'] = t['duration']
    return st


class OffsetIn(BaseModel):
    offset_ms: int


@app.post('/api/karaoke/offset/{tid}')
def karaoke_offset(tid: int, body: OffsetIn):
    """Remember a lyric timing nudge for a song. LRC files are often a few
    hundred ms off, and the right correction is per song, not per player."""
    t = _kar_track(tid)
    return {'skey': t['skey'], 'offset_ms': st_mod.set_kar_offset(t['skey'], body.offset_ms)}


def _tracks_for_skeys(skeys):
    """One library track per song key, the plainest studio copy where a song
    sits on several albums. A stem is keyed by skey; the page needs a track."""
    skeys = [k for k in skeys if k]
    if not skeys:
        return {}
    out = {}
    for i in range(0, len(skeys), 400):
        chunk = skeys[i:i + 400]
        ph = ','.join('?' * len(chunk))
        for r in rows('SELECT track.id, track.skey, track.title, track.duration, '
                      'album.id AS album_id, album.title AS album, album.year, '
                      "COALESCE(NULLIF(track.track_artist,''), artist.name) AS artist, "
                      'album.artist_id, artist.name AS album_artist '
                      'FROM track JOIN album ON album.id=track.album_id '
                      'JOIN artist ON artist.id=album.artist_id '
                      f'WHERE track.skey IN ({ph})', *chunk):
            k = r['skey']
            livey = 1 if re.search(r'(?i)\b(live|unplugged|concert|remix|mixes|demo|session)\b',
                                   r['album'] or '') else 0
            score = (livey, len(r['title'] or ''), r['year'] or 9999)
            if k not in out or score < out[k][0]:
                out[k] = (score, r)
    return {k: v[1] for k, v in out.items()}


@app.get('/api/karaoke/library')
def karaoke_library(candidates: int = 0):
    """Everything to do with singing, for the Karaoke page: songs already
    split (ready), jobs in flight, and -- only with ?candidates=1 -- the songs
    with time-synced lyrics that could be split next. That list is nearly nine
    thousand rows and 1.9 MB; the page asks for it behind a button, because
    building it on arrival was what made the page slow on the television.
    Without it the reply carries the count as candidates_n. Cache-only;
    nothing here goes to the network."""
    made = kar.ready_made()
    jobs = [j for j in kar.queued() if j.get('state') in ('queued', 'running', 'error')]
    try:
        c = lyr.db()
        synced = [r[0] for r in c.execute("SELECT skey FROM lyric WHERE synced IS NOT NULL AND synced != ''")]
        c.close()
    except Exception:
        synced = []
    synced_set = set(synced)
    busy = set(made) | {j['skey'] for j in jobs}
    want = busy | synced_set if candidates else busy
    tr = _tracks_for_skeys(want)
    offs = {}
    try:
        c = st_mod.db()
        offs = {r[0]: r[1] for r in c.execute('SELECT skey, offset_ms FROM kar_sync')}
        c.close()
    except Exception:
        pass

    def pack(k, extra=None):
        r = tr.get(k)
        if not r:
            return None
        d = {'id': r['id'], 'title': r['title'], 'duration': r['duration'],
             'album_id': r['album_id'], 'album': r['album'], 'artist': r['artist'],
             'artist_id': r['artist_id'], 'album_artist': r['album_artist'],
             'lyrics': 'yes' if k in synced_set else 'unknown',
             'stems': 'ready' if k in made else 'none', 'offset_ms': offs.get(k, 0)}
        if extra:
            d.update(extra)
        return d

    ready = [pack(k, {'made': made[k]}) for k in made]
    ready = [x for x in ready if x]
    ready.sort(key=lambda x: ((x['album_artist'] or '').lower(), (x['title'] or '').lower()))
    inflight = []
    for j in jobs:
        d = pack(j['skey'], {'stems': j['state'], 'pct': j.get('pct', 0), 'msg': j.get('msg', '')})
        if d:
            inflight.append(d)
    cand = []
    if candidates:
        cand = [pack(k) for k in synced if k not in busy]
        cand = [x for x in cand if x]
        cand.sort(key=lambda x: ((x['album_artist'] or '').lower(), (x['title'] or '').lower()))
    return {'ready': ready, 'inflight': inflight, 'candidates': cand,
            'candidates_n': len(cand) if candidates else len(synced_set - busy),
            'store': kar.store_stats(), 'available': kar.available()}


@app.post('/api/karaoke/prepare/{tid}')
def karaoke_prepare(tid: int):
    if not kar.available():
        raise HTTPException(503, 'separation is not installed on the server')
    t = _kar_track(tid)
    src = safe_path(t['rel_path'])
    if not src.exists():
        raise HTTPException(404, 'file missing')
    st = kar.enqueue(t['skey'], src, '%s - %s' % (t['art'], t['title']))
    st['skey'] = t['skey']
    return st


@app.delete('/api/karaoke/prepare/{tid}')
def karaoke_cancel(tid: int):
    t = _kar_track(tid)
    return {'cancelled': kar.cancel(t['skey'])}


@app.get('/api/karaoke/queue')
def karaoke_queue():
    return {'jobs': kar.queued(), 'store': kar.store_stats(), 'available': kar.available()}


def _kar_file(tid: int, name: str, mime: str, request: Request):
    t = _kar_track(tid)
    p = kar.dir_for(t['skey']) / name
    if not p.exists():
        raise HTTPException(404, 'not prepared')
    total = p.stat().st_size
    hdr = {'Accept-Ranges': 'bytes', 'Content-Type': mime,
           'Cache-Control': 'public, max-age=604800'}
    rng = request.headers.get('range')
    if not rng:
        return FileResponse(p, media_type=mime, headers=hdr)
    m = re.match(r'bytes=(\d*)-(\d*)', rng)
    a = int(m.group(1)) if m and m.group(1) else 0
    b = int(m.group(2)) if m and m.group(2) else total - 1
    b = min(b, total - 1)
    if a > b:
        raise HTTPException(416)

    def chunks():
        with open(p, 'rb') as f:
            f.seek(a)
            left = b - a + 1
            while left > 0:
                buf = f.read(min(65536, left))
                if not buf:
                    break
                left -= len(buf)
                yield buf
    hdr['Content-Range'] = 'bytes %d-%d/%d' % (a, b, total)
    hdr['Content-Length'] = str(b - a + 1)
    return StreamingResponse(chunks(), status_code=206, headers=hdr)


@app.get('/api/karaoke/{tid}/backing.opus')
def karaoke_backing(tid: int, request: Request):
    return _kar_file(tid, 'backing.opus', 'audio/ogg', request)


@app.get('/api/karaoke/{tid}/vocals.opus')
def karaoke_vocals(tid: int, request: Request):
    return _kar_file(tid, 'vocals.opus', 'audio/ogg', request)


@app.get('/api/karaoke/{tid}/words')
def karaoke_words(tid: int):
    """Forced-alignment word timings, when the song has them (see align.py)."""
    t = _kar_track(tid)
    p = kar.dir_for(t['skey']) / 'words.json'
    if not p.exists():
        raise HTTPException(404, 'not aligned')
    return JSONResponse(json.loads(p.read_text(encoding='utf-8')),
                        headers={'Cache-Control': 'no-store'})


@app.post('/api/admin/align-missing')
def admin_align_missing():
    """Queue word alignment for every split song that has none, one at a time
    behind whatever is already queued."""
    n = 0
    for skey in kar.ready_keys():
        if not kar.aligned(skey):
            meta = {}
            try:
                meta = json.loads((kar.dir_for(skey) / 'meta.json').read_text(encoding='utf-8'))
            except Exception:
                pass
            kar.enqueue_align(skey, meta.get('title', ''))
            n += 1
    return {'ok': True, 'queued': n}


@app.get('/api/karaoke/{tid}/envelope')
def karaoke_envelope(tid: int):
    t = _kar_track(tid)
    p = kar.dir_for(t['skey']) / 'env.json'
    if not p.exists():
        raise HTTPException(404, 'not prepared')
    return JSONResponse(json.loads(p.read_text(encoding='utf-8')),
                        headers={'Cache-Control': 'public, max-age=604800'})


# ---- karaoke on a Sonos room -------------------------------------------------
# The browser mixes the two Opus stems live through WebAudio. A speaker cannot:
# Sonos plays no Opus and takes one stream, so each music/vocal setting the
# singer can pick is rendered once into an MP3 beside the stems (mix-m1-v0 is
# the plain instrumental) and handed to the room like any other track. The
# lyric clock then comes from the speaker's reported position, polled by the
# page, instead of an <audio> element.
_MIX_NAME = re.compile(r'^mix-m[01]-v[012]$')
_MIX_LOCK = _th.Lock()
VOCAL_GAIN = {0: 0.0, 1: 0.22, 2: 1.0}      # off, guide, full -- same as the page


def _kar_mix(skey: str, music: bool, vocals: int) -> Path:
    """Render (once) and return the MP3 for this music/vocal setting."""
    d = kar.dir_for(skey)
    name = 'mix-m%d-v%d' % (1 if music else 0, vocals)
    out = d / (name + '.mp3')
    if out.exists() and out.stat().st_size > 0:
        return out
    b, v = d / 'backing.opus', d / 'vocals.opus'
    if not (b.exists() and v.exists()):
        raise HTTPException(404, 'not prepared')
    import subprocess
    with _MIX_LOCK:
        if out.exists() and out.stat().st_size > 0:
            return out
        tmp = d / (name + '.part.mp3')
        gb, gv = (1.0 if music else 0.0), VOCAL_GAIN.get(vocals, 0.0)
        if gv == 0.0:
            cmd = ['ffmpeg', '-v', 'error', '-y', '-i', str(b),
                   '-c:a', 'libmp3lame', '-b:a', '192k', str(tmp)]
        else:
            flt = ('[0:a]volume=%.3f[a];[1:a]volume=%.3f[b];'
                   '[a][b]amix=inputs=2:duration=longest:normalize=0[o]' % (gb, gv))
            cmd = ['ffmpeg', '-v', 'error', '-y', '-i', str(b), '-i', str(v),
                   '-filter_complex', flt, '-map', '[o]',
                   '-c:a', 'libmp3lame', '-b:a', '192k', str(tmp)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not tmp.exists():
            raise HTTPException(500, 'mix failed: ' + (r.stderr or '')[-300:])
        os.replace(tmp, out)
    return out


@app.get('/api/karaoke/{tid}/mix/{name}.mp3')
def karaoke_mix(tid: int, name: str, request: Request):
    if not _MIX_NAME.match(name):
        raise HTTPException(404)
    t = _kar_track(tid)
    _kar_mix(t['skey'], name[5] == '1', int(name[-1]))     # rendered on first request
    return _kar_file(tid, name + '.mp3', 'audio/mpeg', request)


class KarCast(BaseModel):
    ips: list = []
    music: bool = True
    vocals: int = 0
    at: float = 0


@app.post('/api/karaoke/{tid}/cast')
def karaoke_cast(tid: int, c: KarCast):
    """Start (or restart at `at` seconds) this song's karaoke mix on the given
    rooms. Each ip is a coordinator; separate Sonos systems run in parallel."""
    if not c.ips:
        raise HTTPException(400, 'no room')
    t = _kar_track(tid)
    mix = _kar_mix(t['skey'], c.music, c.vocals)
    host = lan_host()
    url = f"{host}/api/karaoke/{tid}/mix/{mix.stem}.mp3"
    art = f"{host}/api/art/{t['album_id']}?size=600" if t.get('album_id') else None
    errs = []
    for ip in c.ips:
        try:
            sn.play_url(ip, url, title=(t.get('title') or '') + ' (karaoke)',
                        artist=t.get('art') or '', album=t.get('alb') or '',
                        art_url=art, duration=t.get('duration'), mime='audio/mpeg')
            if c.at and c.at > 0.5:
                # the transport is still TRANSITIONING for a beat after a new URI
                for i in range(6):
                    try:
                        sn.seek(ip, c.at); break
                    except Exception:
                        time.sleep(0.3)
        except Exception as e:
            errs.append(f'{ip}: {e}')
    if len(errs) == len(c.ips):
        raise HTTPException(502, 'sonos refused: ' + '; '.join(errs))
    return {'ok': True, 'url': url, 'duration': t.get('duration'), 'errors': errs}


# ---- CD+G karaoke discs -----------------------------------------------------
# A second karaoke source beside the split songs: the store of MP3+CDG discs at
# D:\Music\Karaoke, indexed by cdg.py into cdg.db with its text read from the
# disc graphics into jukebox/cdgtext/<id>.json. These are covers, never library
# tracks: they have their own ids (the SHA-1 of the .cdg), their own audio, and
# a pointer (lib_skey) to the library track of the same name when there is one,
# used only for artwork and for saying "there is a disc of this" on a row.
_CDG_DB = HERE / 'cdg.db'
_CDG_TEXT = HERE / 'cdgtext'
_CDG_STORE = Path(os.environ.get('JUKEBOX_CDG') or ROOT / 'Karaoke')


def _cdg_conn():
    if not _CDG_DB.exists():
        raise HTTPException(404, 'no karaoke disc index')
    c = sqlite3.connect(f'file:{_CDG_DB}?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    return c


def _cdg_song(cid: str):
    if not re.fullmatch(r'[0-9a-f]{16}', cid or ''):
        raise HTTPException(404)
    c = _cdg_conn()
    try:
        r = c.execute('SELECT * FROM song WHERE id=?', (cid,)).fetchone()
    finally:
        c.close()
    if not r:
        raise HTTPException(404, 'no such disc song')
    return dict(r)


def _cdg_text(cid: str):
    p = _CDG_TEXT / f'{cid}.json'
    if not p.exists():
        raise HTTPException(404, 'text not read yet')
    return json.loads(p.read_text(encoding='utf-8'))


def _cdg_pack(r, tids=None):
    d = {'id': r['id'], 'artist': r['artist'], 'title': r['title'], 'code': r['code'],
         'duration': r['duration'], 'text': r['text_state'], 'lib_skey': r['lib_skey']}
    if tids is not None and r['lib_skey'] in tids:
        d['lib_tid'], d['album_id'] = tids[r['lib_skey']]
    return d


def _cdg_lib_tids(skeys):
    """skey -> (track id, album id) for artwork and the library link."""
    out = {}
    skeys = [k for k in set(skeys) if k]
    for i in range(0, len(skeys), 400):
        chunk = skeys[i:i + 400]
        for r in rows(f"SELECT skey, MIN(id) id, MIN(album_id) album_id FROM track WHERE skey IN ({','.join('?' * len(chunk))}) GROUP BY skey", *chunk):
            out[r['skey']] = (r['id'], r['album_id'])
    return out


_CDG_NORM_TITLE = re.compile(r'\(.*?\)|\[.*?\]|[^a-z0-9]')
_SUNFLY = re.compile(r'^(SF|SFG|SFGD|SFMW|SFD|SFTP|SFTPB|SFABBA|SFCELT|SFSCOT|SFWC|FLY)', re.I)


def _cdg_best_of_each(rs):
    """One row per song. The same song sits on many discs (seventeen Dancing
    Queens); the user wants one. Chosen by how well its text read (wordness,
    from the read notes), then a Sunfly pressing over an unknown producer's,
    then the fuller lyric. The others are counted as `alts`."""
    groups = collections.OrderedDict()
    for r in rs:
        key = (re.sub(r'[^a-z0-9]', '', (r['artist'] or '').lower()),
               _CDG_NORM_TITLE.sub('', (r['title'] or '').lower()))
        groups.setdefault(key, []).append(r)

    def score(r):
        try:
            w = json.loads(r['note'] or '{}').get('wordness') or 0
        except Exception:
            w = 0
        return (round(w * 10) / 10, 1 if _SUNFLY.match(r['code'] or '') else 0, r['n_words'] or 0, r['duration'] or 0)
    out = []
    for key, members in groups.items():
        members.sort(key=score, reverse=True)
        best = dict(members[0])
        best['alts'] = len(members) - 1
        best['alt_ids'] = [m['id'] for m in members[1:]]
        out.append(best)
    return out


@app.get('/api/cdg/library')
def cdg_library(state: str = 'ok', all: int = 0):
    """Every disc song with readable text, compact, for the Karaoke discs page.
    One version per song unless ?all=1."""
    c = _cdg_conn()
    try:
        rs = c.execute('SELECT * FROM song WHERE text_state=? ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE', (state,)).fetchall()
        tot = c.execute('SELECT COUNT(*) FROM song').fetchone()[0]
        by_state = {r[0]: r[1] for r in c.execute('SELECT text_state, COUNT(*) FROM song GROUP BY text_state')}
    finally:
        c.close()
    versions = len(rs)
    if not all:
        rs = _cdg_best_of_each(rs)
    tids = _cdg_lib_tids([r['lib_skey'] for r in rs])
    songs = []
    for r in rs:
        d = _cdg_pack(r, tids)
        d['alts'] = r['alts'] if isinstance(r, dict) else 0
        songs.append(d)
    return {'songs': songs, 'total': tot, 'versions': versions, 'by_state': by_state,
            'artists': len({r['artist'] for r in rs})}


@app.get('/api/cdg/summary')
def cdg_summary():
    c = _cdg_conn()
    try:
        by_state = {r[0]: r[1] for r in c.execute('SELECT text_state, COUNT(*) FROM song GROUP BY text_state')}
        rs = c.execute("SELECT id, artist, title, code, note, n_words, duration FROM song WHERE text_state='ok'").fetchall()
    finally:
        c.close()
    songs = _cdg_best_of_each(rs)
    return {'ready': len(songs), 'versions': len(rs), 'total': sum(by_state.values()), 'by_state': by_state,
            'artists': len({r['artist'] for r in rs})}


@app.get('/api/cdg/artist')
def cdg_artist(name: str, all: int = 0):
    """The disc songs credited to an artist, for the block on their library page; one version per song."""
    c = _cdg_conn()
    try:
        rs = c.execute("SELECT * FROM song WHERE text_state='ok' AND artist=? COLLATE NOCASE ORDER BY title COLLATE NOCASE", (name,)).fetchall()
    finally:
        c.close()
    if not all:
        rs = _cdg_best_of_each(rs)
    tids = _cdg_lib_tids([r['lib_skey'] for r in rs])
    out = []
    for r in rs:
        d = _cdg_pack(r, tids)
        d['alts'] = r['alts'] if isinstance(r, dict) else 0
        out.append(d)
    return out


@app.get('/api/cdg/for-tracks')
def cdg_for_tracks(ids: str):
    """track id -> disc song ids, for the rows on screen: says 'a disc exists'."""
    tids = [int(x) for x in ids.split(',') if x.strip().isdigit()][:400]
    if not tids:
        return {}
    q = ','.join('?' * len(tids))
    sk = {r['skey']: r['id'] for r in rows(f'SELECT id, skey FROM track WHERE id IN ({q})', *tids)}
    if not sk:
        return {}
    c = _cdg_conn()
    try:
        keys = list(sk)
        out = {}
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            for r in c.execute(f"SELECT id, lib_skey FROM song WHERE text_state='ok' AND lib_skey IN ({','.join('?' * len(chunk))})", chunk):
                out.setdefault(sk[r['lib_skey']], []).append(r['id'])
    finally:
        c.close()
    return out


@app.get('/api/cdg/{cid}/status')
def cdg_status(cid: str):
    r = _cdg_song(cid)
    d = _cdg_pack(r, _cdg_lib_tids([r['lib_skey']]))
    d['lyrics'] = 'yes' if r['text_state'] == 'ok' else 'no'
    d['offset_ms'] = st_mod.kar_offset('cdg:' + cid)
    return d


@app.get('/api/cdg/{cid}/lyrics')
def cdg_lyrics(cid: str):
    j = _cdg_text(cid)
    return {'status': 'ok', 'source': 'cdg', 'synced': j.get('synced') or [], 'plain': '\n'.join(l['line'] for l in j.get('synced') or [])}


@app.get('/api/cdg/{cid}/words')
def cdg_words(cid: str):
    j = _cdg_text(cid)
    return JSONResponse(j.get('words') or {'lines': []}, headers={'Cache-Control': 'no-store'})


@app.get('/api/cdg/{cid}/envelope')
def cdg_envelope(cid: str):
    """The screen animates on the recording's envelope (design rule 1). A disc
    has no vocal stem, so the backing track's envelope stands in; computed on
    first open and kept beside the text."""
    r = _cdg_song(cid)
    p = _CDG_TEXT / f'{cid}.env.json'
    if not p.exists():
        import envelope as env, tempfile
        mp3 = _CDG_STORE / r['rel_mp3']
        if not mp3.exists():
            raise HTTPException(404, 'audio missing')
        # envelope.py reads 16-bit PCM WAV (it was written for the lossless stem)
        fd, wav = tempfile.mkstemp(suffix='.wav')
        os.close(fd)
        try:
            _sp.run(['ffmpeg', '-v', 'error', '-y', '-i', str(mp3), '-ac', '1', '-ar', '22050',
                     '-sample_fmt', 's16', wav], check=True, capture_output=True)
            e = env.analyse(wav)
        finally:
            try:
                os.remove(wav)
            except OSError:
                pass
        _CDG_TEXT.mkdir(exist_ok=True)
        p.write_text(json.dumps(e), encoding='utf-8')
    return JSONResponse(json.loads(p.read_text(encoding='utf-8')), headers={'Cache-Control': 'public, max-age=604800'})


@app.get('/api/cdg/{cid}/audio.mp3')
@app.head('/api/cdg/{cid}/audio.mp3')
def cdg_audio(cid: str, request: Request):
    r = _cdg_song(cid)
    path = _CDG_STORE / r['rel_mp3']
    if not path.exists():
        raise HTTPException(404, 'audio missing')
    total = path.stat().st_size
    hdr = {'Accept-Ranges': 'bytes', 'Content-Type': 'audio/mpeg', 'Cache-Control': 'no-cache'}
    rng = request.headers.get('range')
    m = RANGE_RE.match(rng) if rng else None
    if not m:
        return FileResponse(path, media_type='audio/mpeg', headers=hdr)
    start = int(m.group(1)) if m.group(1) else 0
    end = int(m.group(2)) if m.group(2) else total - 1
    end = min(end, total - 1)
    if start > end or start >= total:
        return Response(status_code=416, headers={'Content-Range': f'bytes */{total}'})
    length = end - start + 1

    def chunks():
        with open(path, 'rb') as f:
            f.seek(start)
            left = length
            while left > 0:
                buf = f.read(min(65536, left))
                if not buf:
                    break
                left -= len(buf)
                yield buf
    hdr.update({'Content-Range': f'bytes {start}-{end}/{total}', 'Content-Length': str(length)})
    return StreamingResponse(chunks(), status_code=206, headers=hdr, media_type='audio/mpeg')


@app.post('/api/cdg/{cid}/offset')
def cdg_offset(cid: str, body: OffsetIn):
    _cdg_song(cid)
    return {'id': cid, 'offset_ms': st_mod.set_kar_offset('cdg:' + cid, body.offset_ms)}


@app.post('/api/cdg/{cid}/cast')
def cdg_cast(cid: str, c: KarCast):
    """Play the disc's backing track on the given rooms; the page keeps the lyric clock from the speaker."""
    if not c.ips:
        raise HTTPException(400, 'no room')
    r = _cdg_song(cid)
    host = lan_host()
    url = f"{host}/api/cdg/{cid}/audio.mp3"
    tids = _cdg_lib_tids([r['lib_skey']])
    art = f"{host}/api/art/{tids[r['lib_skey']][1]}?size=600" if r['lib_skey'] in tids else None
    errs = []
    for ip in c.ips:
        try:
            sn.play_url(ip, url, title=(r['title'] or '') + ' (karaoke disc)', artist=r['artist'] or '',
                        album='Karaoke disc ' + (r['code'] or ''), art_url=art, duration=r['duration'], mime='audio/mpeg')
            if c.at and c.at > 0.5:
                for i in range(6):
                    try:
                        sn.seek(ip, c.at); break
                    except Exception:
                        time.sleep(0.3)
        except Exception as e:
            errs.append(f'{ip}: {e}')
    if len(errs) == len(c.ips):
        raise HTTPException(502, 'sonos refused: ' + '; '.join(errs))
    return {'ok': True, 'url': url, 'duration': r['duration'], 'errors': errs}


@app.get('/api/sonos/pos')
def sonos_pos(ip: str):
    """Transport state and position only: the karaoke page polls this several
    times a second to keep its lyric clock on the speaker, so it must be as
    light as UPnP allows (two calls, no volume)."""
    try:
        return {**sn.transport_info(ip), **{k: v for k, v in sn.position_info(ip).items()
                                             if k in ('position', 'duration', 'uri')}}
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get('/karaoke-mock', response_class=HTMLResponse)
def karaoke_mock():
    """Design mockup for the karaoke screen. Served from here so it can be
    judged on the television at real size, which is the only screen whose
    opinion matters for a 10-foot layout."""
    p = HERE / 'karaoke_mock.html'
    return p.read_text(encoding='utf-8') if p.exists() else '<h1>mockup missing</h1>'


@app.get('/diag', response_class=HTMLResponse)
def diag_page():
    """Deliberately written in old-fashioned JavaScript: it has to run on a
    browser too old for the app itself, which is the case being tested."""
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MusicMate diagnostics</title>
<style>body{background:#111;color:#eee;font:16px sans-serif;padding:18px;line-height:1.7}
b{color:#e8a04c} .bad{color:#ff6b6b} .ok{color:#7bd88f} pre{white-space:pre-wrap;font-size:14px}</style>
</head><body>
<h2>MusicMate diagnostics</h2><pre id="out">running...</pre>
<script>
var L = [];
function add(k, v){ L.push(k + ': ' + v); }
function cssOk(prop, val){
  try { return (window.CSS && CSS.supports) ? CSS.supports(prop, val) : 'unknown'; }
  catch(e){ return 'unknown'; }
}
var errs = [];
window.onerror = function(m, s, l){ errs.push(m + ' @' + l); };
add('userAgent', navigator.userAgent);
add('viewport', window.innerWidth + ' x ' + window.innerHeight);
add('screen', screen.width + ' x ' + screen.height);
add('devicePixelRatio', window.devicePixelRatio);
add('css clamp()', cssOk('width', 'clamp(10px,5vw,20px)'));
add('css min()', cssOk('width', 'min(10px,5vw)'));
add('css aspect-ratio', cssOk('aspect-ratio', '1'));
add('css zoom', cssOk('zoom', '1.5'));
add('css content-visibility', cssOk('content-visibility', 'auto'));
add('css backdrop-filter', cssOk('backdrop-filter', 'blur(4px)'));
add('css inset', cssOk('inset', '0'));
add('css gap in flex', cssOk('gap', '10px'));
add('css :has()', (function(){ try { document.querySelector(':has(*)'); return true; } catch(e){ return false; } })());
add('js optional chaining', (function(){ try { eval('({a:1})?.a'); return true; } catch(e){ return false; } })());
add('js nullish ??', (function(){ try { eval('null ?? 1'); return true; } catch(e){ return false; } })());
add('js spread in object', (function(){ try { eval('({...{a:1}})'); return true; } catch(e){ return false; } })());
add('js Array.flat', typeof [].flat === 'function');
add('js replaceAll', typeof ''.replaceAll === 'function');
add('IntersectionObserver', typeof IntersectionObserver !== 'undefined');
add('fetch', typeof fetch !== 'undefined');
add('pointer events', typeof PointerEvent !== 'undefined');
add('errors seen', errs.length ? errs.join(' | ') : 'none');
document.getElementById('out').textContent = L.join(String.fromCharCode(10));
try {
  var x = new XMLHttpRequest();
  x.open('POST', '/api/diag', true);
  x.setRequestHeader('Content-Type', 'application/json');
  x.send(JSON.stringify({ lines: L }));
} catch(e) {}
</script></body></html>"""


@app.get('/', response_class=HTMLResponse)
def index(request: Request):
    p = HERE / 'ui.html'
    if p.exists():
        html = p.read_text(encoding='utf-8')
        if 'MusicMateTV' in (request.headers.get('user-agent') or ''):
            # the television app: a 1080p Android TV reports 960 CSS px (density 2),
            # which crowds the nav off the right edge and caps the display size at
            # 100%. The layout was designed at 1280, so the WebView is told to lay
            # out at 1280 and scale to fit (setUseWideViewPort in MainActivity).
            html = html.replace('content="width=device-width, initial-scale=1, viewport-fit=cover"',
                                'content="width=1280"', 1)
        # hand the page the art generation so its URLs bust on a rebuild
        return html.replace('<head>', '<head><script>window.__ARTGEN=%r;</script>'
                            % art_gen(), 1)
    return '<h1>MusicMate API</h1><p>UI not built yet. Try /api/health</p>'

@app.get('/{rest:path}', include_in_schema=False)
def any_page(rest: str, request: Request):
    """A browser at any address that is not the API or media gets the app.

    The living-room TV was found stuck on a bare {"detail":"Not Found"} page
    at /q/ (12 Sep 2026): some shortcut on it points there, and nothing of
    ours ever did. A single-page app has exactly one page; anything a person
    navigates to should land on it rather than on JSON."""
    if rest.startswith(('api/', 'media/')) or 'text/html' not in (request.headers.get('accept') or ''):
        raise HTTPException(404)
    return RedirectResponse('/', status_code=302)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8730)
    a = ap.parse_args()
    import uvicorn
    PORT[0] = a.port
    print(f'jukebox on http://{lan_ip()}:{a.port}   (Sonos and the TV both need this address)')
    uvicorn.run(app, host=a.host, port=a.port, log_level='warning')
