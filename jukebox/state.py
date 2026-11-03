"""Favourites and play history -- the only part of the system the user writes to.

Kept in its own database, deliberately. jukebox.db is disposable: it is rebuilt from
scratch whenever the library changes, and its integer ids are regenerated every time.
Storing a favourite against a track id would quietly detach it on the next rebuild,
and storing it against a file path would break the next time files move -- 28,000 of
them moved during the library cleanup.

So state is keyed on a stable identity derived from artist + title + duration, which
survives both. The pleasant side effect: favouriting a song on a greatest-hits
compilation also marks it on the original album, because it is the same recording.
"""
import os, re, time, sqlite3, hashlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DB = HERE / 'state.db'

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS favourite (
  kind      TEXT NOT NULL,          -- 'track' | 'album' | 'artist'
  key       TEXT NOT NULL,          -- stable key for that kind
  label     TEXT,                   -- what it was called when favourited
  detail    TEXT,
  created   REAL,
  PRIMARY KEY (kind, key)
);

CREATE TABLE IF NOT EXISTS play (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  skey      TEXT NOT NULL,          -- stable track key
  album_key TEXT,
  artist_key TEXT,
  started   REAL,
  seconds   REAL,                   -- how much actually played
  completed INTEGER,                -- 1 once past the "it counts" threshold
  room      TEXT                    -- which Sonos room / group it played on
);
CREATE INDEX IF NOT EXISTS ix_play_skey    ON play(skey);
CREATE INDEX IF NOT EXISTS ix_play_started ON play(started DESC);
CREATE INDEX IF NOT EXISTS ix_play_album   ON play(album_key);

CREATE TABLE IF NOT EXISTS playlist (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  name    TEXT UNIQUE, created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS playlist_item (
  playlist_id INTEGER, pos INTEGER, skey TEXT, label TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_pli ON playlist_item(playlist_id, pos);

CREATE TABLE IF NOT EXISTS kar_sync (
  skey      TEXT PRIMARY KEY,       -- stable track key
  offset_ms INTEGER NOT NULL,       -- +ve: lyrics shown later than the LRC says
  updated   REAL
);
"""

# A play counts once it has run past this much of the track. Without a threshold,
# skipping through an album to find one song poisons the recents list with the
# eleven tracks you rejected on the way.
# A play counts (Last.fm's rule, 2 Nov 2026): half the song, or four minutes,
# whichever comes first; four minutes when the length is unknown.
MAX_SECONDS = 240
MIN_FRACTION = 0.5

def counts_after(duration):
    return min(duration * MIN_FRACTION, MAX_SECONDS) if duration else MAX_SECONDS

def db():
    c = sqlite3.connect(STATE_DB, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    if not _MIGRATED:
        _migrate(c)
    return c

# Per person (2 Nov 2026). A person is picked on each device (podcasts.py keeps
# the list); favourites and plays carry their person_id. 0 is the household:
# a device where nobody has been picked, and everything from before people
# existed. A person sees their own favourites and plays; the household sees
# its own favourites and everybody's plays.
_MIGRATED = []

def _migrate(c):
    cols = [r[1] for r in c.execute('PRAGMA table_info(play)')]
    if 'person_id' not in cols:
        c.execute('ALTER TABLE play ADD COLUMN person_id INTEGER DEFAULT 0')
        c.execute('CREATE INDEX IF NOT EXISTS ix_play_person ON play(person_id, started)')
    cols = [r[1] for r in c.execute('PRAGMA table_info(favourite)')]
    if 'person_id' not in cols:
        c.executescript("""
            CREATE TABLE favourite_p (
              person_id INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL, key TEXT NOT NULL,
              label TEXT, detail TEXT, created REAL, PRIMARY KEY (person_id, kind, key));
            INSERT INTO favourite_p (person_id, kind, key, label, detail, created)
              SELECT 0, kind, key, label, detail, created FROM favourite;
            DROP TABLE favourite;
            ALTER TABLE favourite_p RENAME TO favourite;""")
    c.commit()
    _MIGRATED.append(1)

def _pw(who, col='person_id'):
    """the WHERE fragment for a person's plays: theirs, or everyone's for the household"""
    return (f' AND {col}=%d' % int(who)) if who else ''

def _norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())

def track_key(artist, title, duration):
    d = int(round((duration or 0) / 5.0))
    return hashlib.sha1(f'{_norm(artist)}|{_norm(title)}|{d}'.encode()).hexdigest()[:16]

def album_key(artist, album):
    return hashlib.sha1(f'{_norm(artist)}|{_norm(album)}'.encode()).hexdigest()[:16]

def artist_key(artist):
    return hashlib.sha1(_norm(artist).encode()).hexdigest()[:16]

# ---------------------------------------------------------------- favourites
def favourite(kind, key, label=None, detail=None, on=True, who=0):
    c = db()
    try:
        if on:
            c.execute('INSERT OR REPLACE INTO favourite (person_id,kind,key,label,detail,created) '
                      'VALUES (?,?,?,?,?,?)', (who or 0, kind, key, label, detail, time.time()))
        else:
            c.execute('DELETE FROM favourite WHERE person_id=? AND kind=? AND key=?', (who or 0, kind, key))
        c.commit()
        return is_favourite(kind, key, who)
    finally:
        c.close()

def is_favourite(kind, key, who=0):
    c = db()
    try:
        return c.execute('SELECT 1 FROM favourite WHERE person_id=? AND kind=? AND key=?',
                         (who or 0, kind, key)).fetchone() is not None
    finally:
        c.close()

def kar_offset(skey):
    """The singer's lyric timing correction for a song, in ms. 0 if none."""
    c = db()
    try:
        r = c.execute('SELECT offset_ms FROM kar_sync WHERE skey=?', (skey,)).fetchone()
        return int(r[0]) if r else 0
    finally:
        c.close()

def set_kar_offset(skey, ms):
    c = db()
    try:
        ms = int(max(-60000, min(60000, ms)))
        if ms == 0:
            c.execute('DELETE FROM kar_sync WHERE skey=?', (skey,))
        else:
            c.execute('INSERT OR REPLACE INTO kar_sync (skey, offset_ms, updated) VALUES (?,?,?)',
                      (skey, ms, time.time()))
        c.commit()
        return ms
    finally:
        c.close()

def favourites(kind=None, limit=500, who=0):
    c = db()
    try:
        if kind:
            rs = c.execute('SELECT * FROM favourite WHERE person_id=? AND kind=? ORDER BY created DESC '
                           'LIMIT ?', (who or 0, kind, limit))
        else:
            rs = c.execute('SELECT * FROM favourite WHERE person_id=? ORDER BY created DESC LIMIT ?',
                           (who or 0, limit))
        return [dict(r) for r in rs]
    finally:
        c.close()

# ---------------------------------------------------------------- plays
def start_play(skey, album_key_=None, artist_key_=None, room=None, who=0):
    c = db()
    try:
        cur = c.execute('INSERT INTO play (skey,album_key,artist_key,started,seconds,'
                        'completed,room,person_id) VALUES (?,?,?,?,0,0,?,?)',
                        (skey, album_key_, artist_key_, time.time(), room, who or 0))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()

def update_play(play_id, seconds, duration=None):
    """Called as Sonos reports position. Marks completed once it genuinely counts."""
    done = 0
    if seconds >= counts_after(duration):
        done = 1
    c = db()
    try:
        c.execute('UPDATE play SET seconds=?, completed=? WHERE id=?',
                  (seconds, done, play_id))
        c.commit()
        return done
    finally:
        c.close()

def recent(limit=60, kind='track', who=0):
    """Most recently played, de-duplicated so one album does not fill the list."""
    c = db()
    try:
        col = {'track': 'skey', 'album': 'album_key', 'artist': 'artist_key'}[kind]
        rs = c.execute(
            f'SELECT {col} AS key, MAX(started) AS last_played, COUNT(*) AS plays, '
            f'MAX(room) AS room FROM play WHERE completed=1 AND {col} IS NOT NULL{_pw(who)} '
            f'GROUP BY {col} ORDER BY last_played DESC LIMIT ?', (limit,))
        return [dict(r) for r in rs]
    finally:
        c.close()

def most_played(limit=60, kind='track', since_days=None, who=0):
    c = db()
    try:
        col = {'track': 'skey', 'album': 'album_key', 'artist': 'artist_key'}[kind]
        where = 'WHERE completed=1' + _pw(who)
        args = []
        if since_days:
            where += ' AND started > ?'
            args.append(time.time() - since_days * 86400)
        rs = c.execute(
            f'SELECT {col} AS key, COUNT(*) AS plays, MAX(started) AS last_played '
            f'FROM play {where} AND {col} IS NOT NULL GROUP BY {col} '
            f'ORDER BY plays DESC, last_played DESC LIMIT ?', *[args + [limit]])
        return [dict(r) for r in rs]
    finally:
        c.close()

def never_played_albums(jukebox_db, limit=200, who=0):
    """Albums with no completed play -- the 'you own this and have not heard it' shelf."""
    c = db()
    try:
        played = {r[0] for r in c.execute(
            'SELECT DISTINCT album_key FROM play WHERE completed=1 AND album_key IS NOT NULL' + _pw(who))}
    finally:
        c.close()
    j = sqlite3.connect(f'file:{jukebox_db}?mode=ro', uri=True)
    j.row_factory = sqlite3.Row
    try:
        out = []
        for r in j.execute(
                'SELECT album.id,album.title,album.year,album.n_tracks,artist.name artist '
                'FROM album JOIN artist ON artist.id=album.artist_id '
                'ORDER BY RANDOM() LIMIT 2000'):
            if album_key(r['artist'], r['title']) not in played:
                out.append(dict(r))
                if len(out) >= limit:
                    break
        return out
    finally:
        j.close()

def stats():
    c = db()
    try:
        return {
            'favourites': {k: n for k, n in c.execute(
                'SELECT kind, COUNT(*) FROM favourite GROUP BY kind')},
            'plays': c.execute('SELECT COUNT(*) FROM play WHERE completed=1').fetchone()[0],
            'distinct_tracks': c.execute(
                'SELECT COUNT(DISTINCT skey) FROM play WHERE completed=1').fetchone()[0],
        }
    finally:
        c.close()

if __name__ == '__main__':
    print('state db:', STATE_DB)
    print(stats())


# ---------------------------------------------------------------- smart playlists
# Playlists that build themselves from what you actually listen to. A hand-curated
# list starts empty and stays empty; these are useful the moment there is any
# history at all, and they stay useful without maintenance.

SMART = [
    ('recent',       'Recently Played',  'what has been on lately'),
    ('most',         'Most Played',      'the ones you keep coming back to'),
    ('favourites',   'Favourites',       'everything you have starred'),
    ('never',        'Never Played',     'you own it and have not heard it'),
    ('added',        'Recently Added',   'newest arrivals in the library'),
    ('forgotten',    'Not Played Lately','loved once, untouched for a year'),
]

def smart_playlist(kind, jukebox_db, limit=100, who=0, **kw):
    """Resolve a smart playlist to concrete tracks from the read-model."""
    j = sqlite3.connect(f'file:{jukebox_db}?mode=ro', uri=True)
    j.row_factory = sqlite3.Row
    sel = ('SELECT track.id, track.title, track.duration, track.skey, '
           'album.id AS album_id, album.title AS album, artist.name AS artist '
           'FROM track JOIN album ON album.id=track.album_id '
           'JOIN artist ON artist.id=album.artist_id ')
    try:
        if kind == 'recent':
            keys = [r['key'] for r in recent(limit, 'track', who)]
            return _by_keys(j, sel, keys)

        if kind == 'most':
            keys = [r['key'] for r in most_played(limit, 'track', who=who)]
            return _by_keys(j, sel, keys)

        if kind == 'favourites':
            out = _by_keys(j, sel, [f['key'] for f in favourites('track', limit, who)])
            seen = {t['skey'] for t in out}
            # a favourited ALBUM or ARTIST contributes its tracks too, so the list is
            # not empty just because someone stars albums rather than individual songs
            fav_albums = {f['key'] for f in favourites('album', 200, who)}
            fav_artists = {f['key'] for f in favourites('artist', 200, who)}
            if fav_albums or fav_artists:
                for r in j.execute(sel):
                    if len(out) >= limit:
                        break
                    if r['skey'] in seen:
                        continue
                    if (album_key(r['artist'], r['album']) in fav_albums
                            or artist_key(r['artist']) in fav_artists):
                        out.append(dict(r)); seen.add(r['skey'])
            return out[:limit]

        if kind == 'never':
            c = db()
            try:
                played = {r[0] for r in c.execute(
                    'SELECT DISTINCT skey FROM play WHERE completed=1' + _pw(who))}
            finally:
                c.close()
            out = []
            for r in j.execute(sel + 'ORDER BY RANDOM() LIMIT 4000'):
                if r['skey'] not in played:
                    out.append(dict(r))
                    if len(out) >= limit:
                        break
            return out

        if kind == 'added':
            return [dict(r) for r in j.execute(sel + 'ORDER BY track.id DESC LIMIT ?',
                                               (limit,))]

        if kind == 'forgotten':
            c = db()
            try:
                cutoff = time.time() - 365 * 86400
                keys = [r[0] for r in c.execute(
                    'SELECT skey FROM play WHERE completed=1' + _pw(who) + ' GROUP BY skey '
                    'HAVING MAX(started) < ? ORDER BY COUNT(*) DESC LIMIT ?',
                    (cutoff, limit))]
            finally:
                c.close()
            return _by_keys(j, sel, keys)

        raise ValueError(f'unknown smart playlist: {kind}')
    finally:
        j.close()

def _by_keys(j, sel, keys):
    """Resolve stable keys back to tracks, preserving the order they came in."""
    if not keys:
        return []
    q = ','.join('?' * len(keys))
    found = {}
    for r in j.execute(sel + f'WHERE track.skey IN ({q})', keys):
        found.setdefault(r['skey'], dict(r))     # first copy wins
    return [found[k] for k in keys if k in found]

# ------------------------------------------------------------------ user playlists
# Items are stored by STABLE KEY, not track id, for the same reason favourites
# and play history are: the read-model is rebuilt from scratch whenever the
# library is re-scanned and ids are not guaranteed to survive that. A key is
# derived from artist, title and rounded duration, so a playlist keeps pointing
# at the right song even if the file is replaced by a better copy.

def pl_list():
    c = db()
    try:
        return [dict(r) for r in c.execute(
            'SELECT playlist.id, playlist.name, playlist.created, playlist.updated, '
            'COUNT(playlist_item.playlist_id) AS n '
            'FROM playlist LEFT JOIN playlist_item ON playlist_item.playlist_id=playlist.id '
            'GROUP BY playlist.id ORDER BY playlist.updated DESC')]
    finally:
        c.close()


def pl_create(name):
    name = (name or '').strip()
    if not name:
        raise ValueError('a playlist needs a name')
    c = db()
    try:
        now = time.time()
        try:
            cur = c.execute('INSERT INTO playlist (name,created,updated) VALUES (?,?,?)',
                            (name, now, now))
            c.commit()
            return {'id': cur.lastrowid, 'name': name, 'n': 0}
        except sqlite3.IntegrityError:
            r = c.execute('SELECT id,name FROM playlist WHERE name=?', (name,)).fetchone()
            return {'id': r['id'], 'name': r['name'], 'existed': True}
    finally:
        c.close()


def pl_rename(pid, name):
    name = (name or '').strip()
    if not name:
        raise ValueError('a playlist needs a name')
    c = db()
    try:
        c.execute('UPDATE playlist SET name=?, updated=? WHERE id=?', (name, time.time(), pid))
        c.commit()
        return c.total_changes > 0
    finally:
        c.close()


def pl_delete(pid):
    c = db()
    try:
        c.execute('DELETE FROM playlist_item WHERE playlist_id=?', (pid,))
        c.execute('DELETE FROM playlist WHERE id=?', (pid,))
        c.commit()
        return True
    finally:
        c.close()


def pl_add(pid, items):
    """items: [{skey,label,detail}] appended in order, skipping ones already there."""
    c = db()
    try:
        have = {r['skey'] for r in c.execute(
            'SELECT skey FROM playlist_item WHERE playlist_id=?', (pid,))}
        pos = c.execute('SELECT COALESCE(MAX(pos),-1)+1 n FROM playlist_item '
                        'WHERE playlist_id=?', (pid,)).fetchone()['n']
        added = 0
        for it in items:
            k = it.get('skey')
            if not k or k in have:
                continue
            have.add(k)
            c.execute('INSERT INTO playlist_item (playlist_id,pos,skey,label,detail) '
                      'VALUES (?,?,?,?,?)',
                      (pid, pos, k, it.get('label'), it.get('detail')))
            pos += 1
            added += 1
        c.execute('UPDATE playlist SET updated=? WHERE id=?', (time.time(), pid))
        c.commit()
        return {'added': added, 'skipped': len(items) - added}
    finally:
        c.close()


def pl_remove(pid, skey):
    c = db()
    try:
        c.execute('DELETE FROM playlist_item WHERE playlist_id=? AND skey=?', (pid, skey))
        c.execute('UPDATE playlist SET updated=? WHERE id=?', (time.time(), pid))
        c.commit()
        return True
    finally:
        c.close()


def pl_reorder(pid, skeys):
    c = db()
    try:
        for i, k in enumerate(skeys):
            c.execute('UPDATE playlist_item SET pos=? WHERE playlist_id=? AND skey=?',
                      (i, pid, k))
        c.execute('UPDATE playlist SET updated=? WHERE id=?', (time.time(), pid))
        c.commit()
        return True
    finally:
        c.close()


def pl_tracks(pid, jukebox_db):
    """The playlist resolved to real tracks, in order, dropping any now missing."""
    c = db()
    try:
        row = c.execute('SELECT id,name FROM playlist WHERE id=?', (pid,)).fetchone()
        if not row:
            return None
        keys = [r['skey'] for r in c.execute(
            'SELECT skey FROM playlist_item WHERE playlist_id=? ORDER BY pos', (pid,))]
        meta = dict(row)
    finally:
        c.close()
    j = sqlite3.connect(f'file:{jukebox_db}?mode=ro', uri=True)
    j.row_factory = sqlite3.Row
    sel = ('SELECT track.id, track.title, track.duration, track.skey, '
           'album.id AS album_id, album.title AS album, artist.name AS artist '
           'FROM track JOIN album ON album.id=track.album_id '
           'JOIN artist ON artist.id=album.artist_id ')
    try:
        meta['tracks'] = _by_keys(j, sel, keys)
    finally:
        j.close()
    meta['n'] = len(meta['tracks'])
    meta['missing'] = len(keys) - meta['n']
    return meta


def smart_catalogue(jukebox_db, who=0):
    """The smart playlists with their current sizes, for the browse screen."""
    out = []
    for key, name, blurb in SMART:
        try:
            n = len(smart_playlist(key, jukebox_db, limit=400, who=who))
            err = None
        except Exception as e:
            # never report an empty playlist when the truth is that the query broke:
            # a silent 0 here shipped a permanently empty Favourites list
            n, err = None, f'{type(e).__name__}: {e}'
        entry = {'key': key, 'name': name, 'blurb': blurb, 'n': n}
        if err:
            entry['error'] = err
        out.append(entry)
    return out
