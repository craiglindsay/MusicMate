"""Fold one spelling of an artist into the library's own: rewrite the artist and
album-artist tags that carry FROM so they carry TO.

Made for "Orchestral Manoeuvres in the dark (OMD)" (31 Oct 2026): The Punishment
of Luxury came in under Lidarr's spelling and showed as a second OMD with one
album, though it sat in the right folder; the jukebox groups artists by tag.
Every old value goes into tag_backup (op mergeartist), and library.db's rows
are brought up to date so the next etl.py build shows one artist.

    python mergeartist.py "Orchestral Manoeuvres in the dark (OMD)" "Orchestral Manoeuvres in the Dark"
    python mergeartist.py ... --apply
"""
import os, sys, time, sqlite3, argparse
import mutagen
import musiclib

OP = 'mergeartist'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src', help='the spelling to replace, exactly as tagged')
    ap.add_argument('dst', help="the library's spelling")
    ap.add_argument('--apply', action='store_true', help='write; without it, only list what would change')
    a = ap.parse_args()

    conn = musiclib.db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT path, artist, albumartist FROM files WHERE kind='audio' AND (artist=? OR albumartist=?) "
                        "ORDER BY path", (a.src, a.src)).fetchall()
    if not rows:
        print('no files tagged %r' % a.src)
        return
    n = 0
    for r in rows:
        fields = [f for f in ('artist', 'albumartist') if r[f] == a.src]
        print(('fix ' if a.apply else 'would fix ') + r['path'] + '  [' + ', '.join(fields) + ']')
        if not a.apply:
            continue
        full = os.path.join(musiclib.ROOT, r['path'])
        m = mutagen.File(full, easy=True)
        if m is None or m.tags is None:
            print('   unreadable, skipped')
            continue
        for f in fields:
            old = (m.tags.get(f) or [None])[0]
            m.tags[f] = [a.dst]
            conn.execute('INSERT INTO tag_backup (ts,op,path,field,old,new) VALUES (?,?,?,?,?,?)',
                         (time.time(), OP, r['path'], f, old, a.dst))
        m.save()
        st = os.stat(full)
        conn.execute('UPDATE files SET %s, size=?, mtime=? WHERE path=?' % ', '.join(f + '=?' for f in fields),
                     [a.dst] * len(fields) + [st.st_size, st.st_mtime, r['path']])
        n += 1
    conn.commit()
    print('%d of %d files %s' % (n if a.apply else len(rows), len(rows), 'retagged' if a.apply else 'to retag (dry run; add --apply)'))


if __name__ == '__main__':
    main()
