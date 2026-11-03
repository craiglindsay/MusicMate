"""The year an album first came out, from MusicBrainz, for the Eras. (1 Nov 2026)

The tags carry the year of the copy we have, so a 2020 remaster of Highway to
Hell sat in the 2020s. MusicBrainz groups every edition of an album into a
release group whose first-release-date is the original's. This looks up each
main-library artist's release groups (cached in requests.db through wanted.mb),
matches our albums by title the way the Get button does (edition wording cut),
and records the original year per album folder in origyear.db.

etl.py reads origyear.db and uses the original year whenever it is earlier
than the tagged one; a later MusicBrainz year is a title clash, not a reissue,
and the tag stands. Each run also patches the live jukebox.db, so the Eras move
without a rebuild.

  python origyear.py          artists not looked up yet (what the intake runs)
  python origyear.py --all    every artist again (cached answers are reused)
"""
import sys, time, sqlite3, argparse, collections
from pathlib import Path

import wanted

HERE = Path(__file__).resolve().parent
DB = HERE / 'origyear.db'
JUKEBOX_DB = HERE / 'jukebox.db'
MAIN_POOL = 'VariousArtists'
YEAR = 365 * wanted.DAY             # release dates do not change; ask again yearly

SCHEMA = """
CREATE TABLE IF NOT EXISTS album (folder TEXT PRIMARY KEY, year INTEGER, rgid TEXT, rg_title TEXT, at REAL);
CREATE TABLE IF NOT EXISTS artist (norm TEXT PRIMARY KEY, mbid TEXT, at REAL);
"""


def db():
    c = sqlite3.connect(DB, timeout=30)
    c.executescript(SCHEMA)
    return c


def years():
    """album folder -> original year, for etl.py"""
    if not DB.exists():
        return {}
    c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=30)
    try:
        return dict(c.execute('SELECT folder, year FROM album WHERE year IS NOT NULL'))
    finally:
        c.close()


def artist_mbid(name):
    """wanted.artist_mbid, then the spellings it misses: "Police, The" and
    "RUN-DMC" (MusicBrainz has Run-D.M.C.; a search for the hyphenated name
    scores it too low)"""
    tries = [name]
    if name.lower().endswith(', the'):
        tries.append('The ' + name[:-5])
    if '-' in name:
        tries.append(name.replace('-', ' '))
    for n in tries:
        m = wanted.artist_mbid(n)
        if m:
            return m
    # a plain search, kept only when the name normalises the same
    # (MusicBrainz spells it Run-D.M.C. with a Unicode hyphen)
    norm = wanted.merge.norm_artist(name)
    for a in wanted.mb('artist', {'query': name, 'limit': 5}).get('artists') or []:
        if wanted.merge.norm_artist(a.get('name', '')) == norm and int(a.get('score') or 0) >= 80:
            return a['id']
    return None


def release_groups(mbid):
    """every album and EP release group, studio ones first, then oldest first"""
    out, offset = [], 0
    while True:
        d = wanted.mb('release-group', {'artist': mbid, 'type': 'album|ep', 'limit': 100, 'offset': offset}, ttl=YEAR)
        rgs = d.get('release-groups') or []
        out += [g for g in rgs if (g.get('first-release-date') or '')[:4].isdigit()]
        offset += len(rgs)
        if not rgs or offset >= (d.get('release-group-count') or 0) or offset >= 1000:
            break
    return sorted(out, key=lambda g: (not wanted._studio(g), g['first-release-date']))


def match(title, rgs):
    keys = {wanted.merge.norm_album_safe(title), wanted.core_title(title)}
    for g in rgs:
        if {wanted.merge.norm_album_safe(g['title']), wanted.core_title(g['title'])} & keys:
            return g
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--all', action='store_true')
    a = ap.parse_args()
    j = sqlite3.connect(f'file:{JUKEBOX_DB}?mode=ro', uri=True, timeout=30)
    j.row_factory = sqlite3.Row
    albums = collections.defaultdict(list)
    names = {}
    for r in j.execute('SELECT album.folder, album.title, album.year, artist.name, artist.norm FROM album '
                       'JOIN artist ON artist.id = album.artist_id WHERE album.pool=? AND album.is_compilation=0',
                       (MAIN_POOL,)):
        albums[r['norm']].append(dict(r))
        names[r['norm']] = r['name']
    j.close()
    c = db()
    done = {n for n, in c.execute('SELECT norm FROM artist WHERE mbid IS NOT NULL')}
    todo = [n for n in sorted(albums) if a.all or n not in done]
    print(f'{len(todo)} artist(s) to look up', flush=True)
    t0, found = time.time(), 0
    for i, norm in enumerate(todo, 1):
        try:
            mbid = artist_mbid(names[norm])
            rgs = release_groups(mbid) if mbid else []
        except Exception as e:                      # network: leave it for the next run
            print(f'  {names[norm]}: {e}', flush=True)
            continue
        now = time.time()
        for al in albums[norm]:
            g = match(al['title'], rgs)
            y = int(g['first-release-date'][:4]) if g else None
            c.execute('INSERT OR REPLACE INTO album VALUES (?,?,?,?,?)',
                      (al['folder'], y, g and g['id'], g and g['title'], now))
            if y and al['year'] and y < al['year']:
                found += 1
                print(f'  {names[norm]} - {al["title"]}: {al["year"]} -> {y}', flush=True)
        c.execute('INSERT OR REPLACE INTO artist VALUES (?,?,?)', (norm, mbid, now))
        c.commit()
        if i % 50 == 0:
            print(f'{i}/{len(todo)} artists, {time.time() - t0:.0f}s', flush=True)
    c.close()
    print(f'{found} album(s) dated earlier than their tags; patched {apply()} in jukebox.db')


def apply():
    """Move albums in the live read model to their original year (etl does
    the same on every rebuild)."""
    ys = years()
    j = sqlite3.connect(JUKEBOX_DB, timeout=60)
    n = 0
    for aid, folder, year in j.execute('SELECT id, folder, year FROM album').fetchall():
        y = ys.get(folder)
        if y and (not year or y < year):
            j.execute('UPDATE album SET year=?, decade=? WHERE id=?', (y, y // 10 * 10, aid))
            n += 1
    j.commit()
    j.close()
    return n


if __name__ == '__main__':
    main()
