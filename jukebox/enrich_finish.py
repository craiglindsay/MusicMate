"""Finish the artist enrichment: photos first, then biographies, retrying until clear.

Two lessons are baked in.

ONE WRITER AT A TIME. These passes ran concurrently before and the image pass
starved: SQLite WAL still permits only a single writer, and the bio pass holds
its transaction across a batch whose every row waits on the network -- about
100 seconds -- which outlasts any sane backoff. Each image write then failed
AND burned its retry budget, so 41 minutes moved the count by three. The passes
are sequential here, and every write commits immediately.

RECORD WHY A LOOKUP FAILED. Previously a missing bio could mean "this artist
has no Wikipedia article" or "Wikimedia throttled us", and the two were
indistinguishable afterwards, so a retry could not tell whom to ask again.
bio_status now separates 'none' (asked, genuinely nothing there) from 'error'
(never got an answer), and only the latter is retried.

Pacing adapts: the gap grows after each throttled call and eases back down
after a clean run, so repeated passes converge instead of hammering.
"""
import sqlite3, time, sys, urllib.parse
from pathlib import Path

import artistinfo as ai

DB = Path(__file__).resolve().parent / 'jukebox.db'

GAP_MIN, GAP_MAX = 2.0, 20.0
MAX_ROUNDS = 12


def connect():
    c = sqlite3.connect(DB, timeout=120)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=120000')
    return c


def ensure_columns(c):
    cols = {r['name'] for r in c.execute('PRAGMA table_info(artist_info)')}
    if 'bio_status' not in cols:
        c.execute('ALTER TABLE artist_info ADD COLUMN bio_status TEXT')
    if 'img_status' not in cols:
        c.execute('ALTER TABLE artist_info ADD COLUMN img_status TEXT')
    c.commit()


def write(c, sql, args):
    """Commit at once: a long-held transaction is what starved the other pass."""
    for attempt in range(8):
        try:
            c.execute(sql, args)
            c.commit()
            return True
        except sqlite3.OperationalError:
            time.sleep(1.5 * (attempt + 1))
    return False


# ------------------------------------------------------------------ images
def images(c, log):
    rows = c.execute("""SELECT ai2.artist_id, ai2.name, ai2.image_url, ai2.image_file
                        FROM artist_info ai2 JOIN artist a ON a.id = ai2.artist_id
                        WHERE ai2.img_status IS NULL
                        ORDER BY a.n_tracks DESC""").fetchall()
    log('images: %d artists to check' % len(rows))
    changed = added = same = none = 0
    for i, r in enumerate(rows, 1):
        if i % 50 == 0:                     # before the continues, or it never prints
            log('  images %4d/%d  replaced %d  added %d  unchanged %d  none %d'
                % (i, len(rows), changed, added, same, none))
        try:
            url, _dz = ai.fetch_image(r['name'])
        except Exception:
            url = None
        if not url:
            none += 1
            write(c, 'UPDATE artist_info SET img_status=? WHERE artist_id=?',
                  ('none', r['artist_id']))
            time.sleep(ai.DEEZER_GAP)
            continue
        if url == r['image_url'] and r['image_file']:
            same += 1
            write(c, 'UPDATE artist_info SET img_status=? WHERE artist_id=?',
                  ('ok', r['artist_id']))
            time.sleep(ai.DEEZER_GAP)
            continue
        fn = ai.save_image(r['artist_id'], url)
        if fn:
            if r['image_file']:
                changed += 1
            else:
                added += 1
            write(c, 'UPDATE artist_info SET image_url=?, image_file=?, img_status=? '
                     'WHERE artist_id=?', (url, fn, 'ok', r['artist_id']))
        else:
            none += 1
            write(c, 'UPDATE artist_info SET img_status=? WHERE artist_id=?',
                  ('none', r['artist_id']))
        time.sleep(ai.DEEZER_GAP)
    log('images done: replaced %d, added %d, unchanged %d, no photo %d'
        % (changed, added, same, none))


# ------------------------------------------------------------------ bios
def bio_round(c, gap, log):
    """One sweep over artists that still owe us an answer. Returns (found, errors)."""
    rows = c.execute("""SELECT ai2.artist_id, ai2.name
                        FROM artist_info ai2 JOIN artist a ON a.id = ai2.artist_id
                        WHERE ai2.bio IS NULL
                          AND (ai2.bio_status IS NULL OR ai2.bio_status = 'error')
                        ORDER BY a.n_tracks DESC""").fetchall()
    if not rows:
        return 0, 0, 0
    log('  round over %d artists at %.1fs pacing' % (len(rows), gap))
    found = errors = none = 0
    streak = 0
    for i, r in enumerate(rows, 1):
        try:
            title, bio, _pic = ai.fetch_bio(r['name'])
        except ai.LookupFailed:
            errors += 1
            streak += 1
            write(c, 'UPDATE artist_info SET bio_status=? WHERE artist_id=?',
                  ('error', r['artist_id']))
            time.sleep(min(GAP_MAX, gap * (1 + streak)))
            continue
        streak = 0
        if bio:
            found += 1
            write(c, 'UPDATE artist_info SET wiki_title=?, bio=?, bio_url=?, bio_status=? '
                     'WHERE artist_id=?',
                  (title, bio, 'https://en.wikipedia.org/wiki/'
                   + urllib.parse.quote(title.replace(' ', '_')), 'ok', r['artist_id']))
        else:
            none += 1
            # asked and answered: there is no article, so stop asking
            write(c, 'UPDATE artist_info SET bio_status=? WHERE artist_id=?',
                  ('none', r['artist_id']))
        time.sleep(gap)
        if i % 25 == 0:
            log('    %4d/%d  found %d  none %d  errors %d' % (i, len(rows), found, none, errors))
    return found, errors, none


def bios(c, log):
    gap = GAP_MIN
    for rnd in range(1, MAX_ROUNDS + 1):
        left = c.execute("SELECT COUNT(*) FROM artist_info WHERE bio IS NULL AND "
                         "(bio_status IS NULL OR bio_status='error')").fetchone()[0]
        if not left:
            log('bios: nothing left to retry')
            break
        log('bios round %d: %d outstanding' % (rnd, left))
        found, errors, none = bio_round(c, gap, log)
        log('bios round %d: found %d, none %d, errors %d' % (rnd, found, none, errors))
        if errors == 0:
            break
        if found == 0 and none == 0:
            gap = min(GAP_MAX, gap * 2)       # nothing but throttling: back well off
            log('  no progress -- slowing to %.1fs' % gap)
        else:
            gap = min(GAP_MAX, gap * 1.4)


def main():
    c = connect()
    ensure_columns(c)

    def log(m):
        print('%s  %s' % (time.strftime('%H:%M:%S'), m), flush=True)

    log('=== photos ===')
    images(c, log)
    log('=== biographies ===')
    bios(c, log)

    n = c.execute('SELECT COUNT(*) FROM artist_info').fetchone()[0]
    b = c.execute('SELECT COUNT(*) FROM artist_info WHERE bio IS NOT NULL').fetchone()[0]
    im = c.execute('SELECT COUNT(*) FROM artist_info WHERE image_file IS NOT NULL').fetchone()[0]
    nb = c.execute("SELECT COUNT(*) FROM artist_info WHERE bio IS NULL AND bio_status='none'").fetchone()[0]
    er = c.execute("SELECT COUNT(*) FROM artist_info WHERE bio IS NULL AND "
                   "(bio_status IS NULL OR bio_status='error')").fetchone()[0]
    log('FINAL: %d artists | bios %d (%.0f%%) | photos %d (%.0f%%) | no article %d | unresolved %d'
        % (n, b, 100.0 * b / n, im, 100.0 * im / n, nb, er))
    c.close()


if __name__ == '__main__':
    main()
