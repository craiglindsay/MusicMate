"""Stable ids for the read model, kept across rebuilds in ids.db.

Until 30 Oct 2026 etl.py numbered artists, albums and tracks from 1 on every
build, so only about one track id in seven survived a rebuild. Every page and
speaker holding an id then pointed at a different recording, and the app had to
force a reload after each rescan. That made unattended intake impossible: a
rebuild in the middle of a song broke Now Playing and the queue.

Here each id is remembered against what it names:

  track       library-relative path; a path not seen before takes over the id
              of a path that vanished in the same build with the same skey and
              size (the file was moved or its folder renamed)
  album       folder; a new folder takes over the id of a vanished folder that
              most of its tracks came from (the album folder was renamed)
  artist      normalised name
  collection  folder

An id is never handed to anything else once retired, so a stale id can only
miss, never play the wrong song. The epoch changes only when ids.db is created
from nothing; a client holding ids from another epoch must reload.

Not user data: deleting ids.db costs one reload on every open page, nothing
more. It is seeded from the current jukebox.db the first time so even that is
avoided on the switch-over.
"""
import os, sqlite3, time, uuid, collections

HERE = os.path.dirname(os.path.abspath(__file__))
IDS_DB = os.environ.get('JUKEBOX_IDS') or os.path.join(HERE, 'ids.db')   # override for tests

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS track_id (
  path TEXT PRIMARY KEY, id INTEGER UNIQUE NOT NULL, skey TEXT, size INTEGER,
  album_id INTEGER, seen REAL);
CREATE INDEX IF NOT EXISTS ix_tid_skey ON track_id(skey, size);
CREATE TABLE IF NOT EXISTS album_id (
  folder TEXT PRIMARY KEY, id INTEGER UNIQUE NOT NULL, seen REAL);
CREATE TABLE IF NOT EXISTS artist_id (
  norm TEXT PRIMARY KEY, id INTEGER UNIQUE NOT NULL, seen REAL);
CREATE TABLE IF NOT EXISTS collection_id (
  folder TEXT PRIMARY KEY, id INTEGER UNIQUE NOT NULL, seen REAL);
"""

KINDS = {'track': ('track_id', 'path'), 'album': ('album_id', 'folder'),
         'artist': ('artist_id', 'norm'), 'collection': ('collection_id', 'folder')}


def _open(path=IDS_DB):
    c = sqlite3.connect(path, timeout=60)
    c.executescript(SCHEMA)
    return c


def epoch(path=IDS_DB):
    """The current epoch, or None before the first build has made one."""
    if not os.path.exists(path):
        return None
    try:
        c = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=10)
        r = c.execute("SELECT v FROM meta WHERE k='epoch'").fetchone()
        c.close()
        return r[0] if r else None
    except sqlite3.Error:
        return None


def seed(c, jukebox_db):
    """Adopt the ids of an existing read model, so the switch to stable ids
    does not itself invalidate the pages that are open."""
    if not os.path.exists(jukebox_db):
        return 0
    j = sqlite3.connect(f'file:{jukebox_db}?mode=ro', uri=True, timeout=60)
    now = time.time()
    try:
        c.executemany('INSERT OR IGNORE INTO artist_id (norm,id,seen) VALUES (?,?,?)',
                      [(n, i, now) for i, n in j.execute('SELECT id, norm FROM artist WHERE norm IS NOT NULL')])
        c.executemany('INSERT OR IGNORE INTO album_id (folder,id,seen) VALUES (?,?,?)',
                      [(f, i, now) for i, f in j.execute('SELECT id, folder FROM album WHERE folder IS NOT NULL')])
        c.executemany('INSERT OR IGNORE INTO track_id (path,id,skey,size,album_id,seen) VALUES (?,?,?,?,?,?)',
                      [(p, i, k, s, a, now) for i, p, k, s, a in
                       j.execute('SELECT id, rel_path, skey, size, album_id FROM track')])
        try:
            c.executemany('INSERT OR IGNORE INTO collection_id (folder,id,seen) VALUES (?,?,?)',
                          [(f, i, now) for i, f in j.execute('SELECT id, folder FROM collection WHERE folder IS NOT NULL')])
        except sqlite3.OperationalError:
            pass
        return c.execute('SELECT COUNT(*) FROM track_id').fetchone()[0]
    finally:
        j.close()


class IdMap:
    """One build's view of ids.db. Call track() for every track BEFORE album()
    for its folder, so a renamed folder can be recognised by its tracks."""

    def __init__(self, path=IDS_DB, seed_from=None):
        fresh = not os.path.exists(path)
        self.c = _open(path)
        self.now = time.time()
        if self.c.execute("SELECT v FROM meta WHERE k='epoch'").fetchone() is None:
            n = seed(self.c, seed_from) if (fresh and seed_from) else 0
            self.c.execute("INSERT INTO meta VALUES ('epoch', ?)", (uuid.uuid4().hex[:8],))
            self.c.commit()
            if n:
                print(f'   ids: seeded {n} track ids from the current read model')
        self.epoch = self.c.execute("SELECT v FROM meta WHERE k='epoch'").fetchone()[0]
        self.known = {}                 # kind -> {key: id}
        self.next = {}
        for kind, (table, col) in KINDS.items():
            self.known[kind] = dict(self.c.execute(f'SELECT {col}, id FROM {table}'))
            self.next[kind] = max(self.known[kind].values(), default=0) + 1
        self.used = {k: {} for k in KINDS}          # key -> id handed out this build
        self.taken = {k: set() for k in KINDS}      # ids handed out this build
        self._tracks = {}                           # path -> (skey, size)
        self._new_tracks = []                       # (path, skey, size) not known by path
        self._folder_old_album = collections.defaultdict(collections.Counter)
        self._resolved = False
        # old track rows by (skey, size), for spotting moved files
        self._old_by_sig = collections.defaultdict(list)
        for p, i, k, s, a in self.c.execute('SELECT path, id, skey, size, album_id FROM track_id'):
            self._old_by_sig[(k, s)].append((p, i, a))
        self._old_album_of = {i: a for i, a in self.c.execute('SELECT id, album_id FROM track_id')}

    def _fresh(self, kind):
        i = self.next[kind]
        self.next[kind] += 1
        return i

    def _hand(self, kind, key, i):
        self.used[kind][key] = i
        self.taken[kind].add(i)
        return i

    # ---- simple kinds
    def artist(self, norm):
        return self._simple('artist', norm)

    def collection(self, folder):
        return self._simple('collection', folder)

    def _simple(self, kind, key):
        if key in self.used[kind]:
            return self.used[kind][key]
        i = self.known[kind].get(key)
        if i is None or i in self.taken[kind]:
            i = self._fresh(kind)
        return self._hand(kind, key, i)

    # ---- tracks and albums
    def track(self, path, skey, size, folder):
        """The id for the file at path. Moved files are recognised once every
        track of the build has been seen, so resolve() must run before the ids
        of new paths are final; known paths are final at once."""
        if path in self.used['track']:
            return self.used['track'][path]
        i = self.known['track'].get(path)
        if i is not None and i not in self.taken['track']:
            self._folder_old_album[folder][self._old_album_of.get(i)] += 1
            return self._hand('track', path, i)
        self._new_tracks.append((path, skey, size, folder))
        return None

    def resolve(self):
        """Give each new path the id of a vanished file with the same skey and
        size, or a fresh id. Returns {path: id} for the new paths."""
        out = {}
        for path, skey, size, folder in self._new_tracks:
            i = None
            for p, old, a in self._old_by_sig.get((skey, size), ()):
                if p not in self.used['track'] and old not in self.taken['track'] \
                        and p not in self._seen_paths:
                    i = old
                    self._folder_old_album[folder][a] += 1
                    break
            if i is None:
                i = self._fresh('track')
            out[path] = self._hand('track', path, i)
        self._new_tracks = []
        self._resolved = True
        return out

    def album(self, folder):
        if folder in self.used['album']:
            return self.used['album'][folder]
        i = self.known['album'].get(folder)
        if i is None or i in self.taken['album']:
            i = None
            # a renamed folder: most of its tracks used to belong to one album
            # whose folder is gone from this build
            votes = self._folder_old_album.get(folder)
            if votes:
                old, n = votes.most_common(1)[0]
                if old is not None and n * 2 > sum(votes.values()) \
                        and old not in self.taken['album'] \
                        and self._album_folder.get(old) not in self._seen_folders:
                    i = old
            if i is None:
                i = self._fresh('album')
        return self._hand('album', folder, i)

    def begin(self, paths, folders):
        """Tell the map every path and folder in this build up front, so a
        vanished file or folder can be told from one merely not reached yet."""
        self._seen_paths = set(paths)
        self._seen_folders = set(folders)
        self._album_folder = {i: f for f, i in self.known['album'].items()}

    # ---- persist
    def save(self, jukebox_db):
        """Record what the finished read model contains. Run after every step
        that merges or moves rows, so track -> album reflects the final model."""
        j = sqlite3.connect(f'file:{jukebox_db}?mode=ro', uri=True, timeout=60)
        now = self.now
        c = self.c
        for kind, (table, col) in KINDS.items():
            rows = self.used[kind]
            # a key that now has a different id (the old id was taken over by a
            # moved file) must lose its old row first; ids are UNIQUE
            c.executemany(f'DELETE FROM {table} WHERE id=? AND {col}<>?',
                          [(i, k) for k, i in rows.items()])
            c.executemany(f'INSERT OR REPLACE INTO {table} ({col}, id, seen) VALUES (?,?,?)'
                          if kind != 'track' else
                          f'INSERT OR REPLACE INTO {table} ({col}, id, seen, skey, size, album_id) '
                          f'VALUES (?,?,?,NULL,NULL,NULL)',
                          [(k, i, now) for k, i in rows.items()])
        c.executemany('UPDATE track_id SET skey=?, size=?, album_id=? WHERE id=?',
                      list(j.execute('SELECT skey, size, album_id, id FROM track')))
        c.execute("INSERT OR REPLACE INTO meta VALUES ('built', ?)", (str(now),))
        c.commit()
        j.close()

    def close(self):
        self.c.close()


ACTIVE = None      # the build's map while etl.py runs; the post-build steps use it


def active():
    """The map a post-build step should take ids from: the running build's, or
    for a step run on its own, the shared ids.db without move detection."""
    global ACTIVE
    if ACTIVE is None:
        ACTIVE = IdMap()
        ACTIVE.begin((), ())
    return ACTIVE
