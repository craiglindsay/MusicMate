"""Retry biographies for artists that came back empty, keeping their photos.

Worth a second pass because the resolution strategy changed underneath them:
the first run searched Wikipedia for "<name> musician band", which ranks band
MEMBERS and ALBUMS above the band itself -- "Fleetwood Mac musician band" puts
Mick Fleetwood, Bob Welch, Peter Green and the 1975 album ahead of the band,
whose own article never reaches the top ten. Lookups now try the exact title
first, then Wikipedia's own disambiguation forms, and only then fall back to
search.

Only rows with no bio are touched; images and everything else are left alone.
Resumable and safe to re-run.
"""
import sqlite3, time, urllib.parse, sys
from pathlib import Path

import artistinfo as ai

DB = Path(__file__).resolve().parent / 'jukebox.db'


def main(limit=None):
    c = sqlite3.connect(DB, timeout=60)
    c.row_factory = sqlite3.Row
    todo = c.execute("""SELECT ai2.artist_id, ai2.name, a.n_tracks
                        FROM artist_info ai2 JOIN artist a ON a.id = ai2.artist_id
                        WHERE ai2.bio IS NULL
                        ORDER BY a.n_tracks DESC""").fetchall()
    if limit:
        todo = todo[:limit]
    print('artists still without a bio: %d' % len(todo))
    got = miss = err = 0
    for i, r in enumerate(todo, 1):
        try:
            title, bio, pic = ai.fetch_bio(r['name'])
        except ai.LookupFailed:
            err += 1
            time.sleep(6)
            continue
        if bio:
            c.execute('UPDATE artist_info SET wiki_title=?, bio=?, bio_url=?, status=? '
                      'WHERE artist_id=?',
                      (title, bio,
                       'https://en.wikipedia.org/wiki/'
                       + urllib.parse.quote(title.replace(' ', '_')),
                       'ok', r['artist_id']))
            got += 1
        else:
            miss += 1
        if i % 20 == 0:
            c.commit()
            print('  %4d/%d  found %d  none %d  errors %d   (last: %s)'
                  % (i, len(todo), got, miss, err, r['name'][:26]))
        time.sleep(0.5)
    c.commit()
    total = c.execute('SELECT COUNT(*) FROM artist_info WHERE bio IS NOT NULL').fetchone()[0]
    c.close()
    print('')
    print('recovered %d bios (%d still none, %d errors); %d artists now have one'
          % (got, miss, err, total))


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
