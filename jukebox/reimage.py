"""Re-pick artist photographs now that candidates are ranked, not taken in order.

The first pass accepted the first Deezer result whose name matched exactly.
Deezer frequently holds several artists under one name and does not return the
well-known one first: searching "America" gives four, and the first exact match
has 2 fans and 0 albums -- an unrelated person whose photograph the 1970s band
was wearing. Matches are now ranked by following and catalogue size, and
entries with no albums are refused outright.

Only replaces a photo when the new pick is genuinely different, so unchanged
artists cost one cheap request and no disk churn. Bios are not touched.
"""
import hashlib, sqlite3, time, sys
from pathlib import Path

import artistinfo as ai

DB = Path(__file__).resolve().parent / 'jukebox.db'
PIX = Path(__file__).resolve().parent / 'artistpix'


def main(limit=None):
    c = sqlite3.connect(DB, timeout=60)
    c.row_factory = sqlite3.Row
    rows = c.execute("""SELECT ai2.artist_id, ai2.name, ai2.image_url, ai2.image_file,
                               a.n_tracks
                        FROM artist_info ai2 JOIN artist a ON a.id = ai2.artist_id
                        ORDER BY a.n_tracks DESC""").fetchall()
    if limit:
        rows = rows[:limit]
    print('artists to re-check: %d' % len(rows))
    changed = added = same = none = 0
    for i, r in enumerate(rows, 1):
        try:
            url, _dz = ai.fetch_image(r['name'])
        except Exception:
            url = None
        time.sleep(ai.DEEZER_GAP)
        if not url:
            # keep whatever we already have rather than blanking an artist
            if not r['image_file']:
                none += 1
            continue
        if url == r['image_url'] and r['image_file']:
            same += 1
            continue
        old = r['image_file']
        fn = ai.save_image(r['artist_id'], url)
        if not fn:
            continue
        # WAL still allows only one writer, and the bio pass holds its
        # transaction across a batch, so back off rather than die on BUSY
        for attempt in range(6):
            try:
                c.execute('UPDATE artist_info SET image_url=?, image_file=? WHERE artist_id=?',
                          (url, fn, r['artist_id']))
                c.commit()
                break
            except sqlite3.OperationalError:
                time.sleep(2 * (attempt + 1))
        if old:
            changed += 1
        else:
            added += 1
        if i % 25 == 0:
            c.commit()
            print('  %4d/%d  replaced %d  added %d  unchanged %d   (last: %s)'
                  % (i, len(rows), changed, added, same, r['name'][:26]))
    c.commit()
    tot = c.execute('SELECT COUNT(*) FROM artist_info WHERE image_file IS NOT NULL').fetchone()[0]
    c.close()
    print('')
    print('replaced %d, added %d, unchanged %d, still none %d -> %d artists have a photo'
          % (changed, added, same, none, tot))


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
