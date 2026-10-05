"""Standardise the Billboard year folders under Era.

Era/<YYYY> holds the Billboard hits of one year, one folder per year, 1946 to
2006, as downloaded sets with four different title patterns ("Billboard Top
30 Hits of 1946", "Billboard Top Hits 1951", "Billboard Top 100 of 1970",
"BillBoard Top 100 of 2005"), one blank, stray album-artist tags on single
tracks, year tags that are wrong or missing, and four folders numbered 0-99.
None of that sorts. Every audio file in Era/<YYYY> gets:

    album        "Billboard Top Hits of <YYYY>"     one prefix, the year last
    albumartist  "Various Artists"
    date         <YYYY>                             the folder's year
    tracknumber  +1 where a folder is numbered from 0 with no gaps

Track titles, performers and genres are left alone. The folder is outside
VariousArtists; this is run on the user's explicit instruction naming the
Era folder (2 Sep 2026). Old values go to tag_backup (op "era-tags").

    python era_tags.py            # plan
    python era_tags.py --apply
"""
import os, re, sys, time, sqlite3, argparse, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import musiclib, mutagen

ROOT = musiclib.ROOT
OP = 'era-tags'
POOL = 'Era'
VA = 'Various Artists'
TITLE = 'Billboard Top Hits of %s'
COLS = {'album': 'album', 'albumartist': 'albumartist', 'date': 'year', 'tracknumber': 'track', 'artist': 'artist', 'title': 'title'}


def set_tag(conn, rel, field, value, dry):
    full = os.path.join(ROOT, rel.replace('/', os.sep))
    f = mutagen.File(full, easy=True)
    if f is None:
        return False
    old = (f.get(field) or [None])[0] if f.tags is not None else None
    if old == value:
        return False
    if dry:
        return True
    if f.tags is None:
        f.add_tags()
    f[field] = [value]; f.save()
    conn.execute('INSERT INTO tag_backup (ts,op,path,field,old,new) VALUES (?,?,?,?,?,?)',
                 (time.time(), OP, rel, field, old, value))
    col = COLS[field]
    if col in ('year', 'track'):
        m = re.match(r'\d+', str(value))
        conn.execute('UPDATE files SET %s=? WHERE path=?' % col, (int(m.group()) if m else None, rel))
    else:
        conn.execute('UPDATE files SET %s=? WHERE path=?' % col, (value, rel))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    conn = musiclib.db(); conn.row_factory = sqlite3.Row
    folders = [f for (f,) in conn.execute(
        "SELECT DISTINCT folder FROM files WHERE src=? AND kind='audio' ORDER BY folder", (POOL,))]
    changes = collections.Counter()
    skipped = []
    for folder in folders:
        m = re.fullmatch(r'Era/(\d{4})', folder)
        if not m:
            skipped.append(folder); continue
        year = m.group(1)
        rows = conn.execute("SELECT path, track FROM files WHERE folder=? AND kind='audio' ORDER BY track, filename", (folder,)).fetchall()
        nums = [r['track'] for r in rows if r['track'] is not None]
        shift = bool(nums) and min(nums) == 0 and len(set(nums)) == len(rows) and max(nums) == len(rows) - 1
        for r in rows:
            for field, value in (('album', TITLE % year), ('albumartist', VA), ('date', year)):
                if set_tag(conn, r['path'], field, value, not a.apply):
                    changes[field] += 1
            # the 1960s sets carry the performer inside the title, "The McCoys /
            # Hang On Sloopy", with the artist tag "Various": split them
            row = conn.execute('SELECT artist, title FROM files WHERE path=?', (r['path'],)).fetchone()
            art, ttl = (row['artist'] or ''), (row['title'] or '')
            if (not art or art.lower().startswith('various')) and ' / ' in ttl:
                performer, song = [x.strip() for x in ttl.split(' / ', 1)]
                if performer and song:
                    if set_tag(conn, r['path'], 'artist', performer, not a.apply): changes['artist'] += 1
                    if set_tag(conn, r['path'], 'title', song, not a.apply): changes['title'] += 1
            if shift and r['track'] is not None:
                if set_tag(conn, r['path'], 'tracknumber', str(r['track'] + 1), not a.apply):
                    changes['tracknumber'] += 1
        if shift:
            print('  %s: numbered 0-%d, shifting to 1-%d' % (folder, len(rows) - 1, len(rows)))
    if a.apply:
        conn.commit()
    print('%d year folders; tag writes%s: %s' % (len(folders) - len(skipped), '' if a.apply else ' planned', dict(changes)))
    if skipped:
        print('left alone (not a year folder):', skipped)
    if not a.apply:
        print('dry run')


if __name__ == '__main__':
    main()
