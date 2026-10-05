"""Bring every artist/albumartist tag under one artist folder to one spelling.

The jukebox names an artist from the album-artist tag (falling back to the
artist tag), so one folder whose albums are tagged four ways shows as four
artists. OMD's folder had "OMD", "Orchestral Manoeuvres in the Dark",
"Orchestral Manoeuvres in the Dark (OMD)" and the misspelt "Manouevres".

A value is rewritten when it is the target name spelled differently
(accents, case, "The", a bracketed alias) or one of the --also aliases; a
joined credit ("A / B") is rewritten when every part qualifies. Anything
else -- a real featured artist -- is left alone and listed. Old values go to
tag_backup (op "unify-artist"). Dry run by default.

    python unifyartist.py "Orchestral Manoeuvres in the Dark" --also OMD --also "Orchestral Manouevres in the Dark (OMD)"
    python unifyartist.py "Orchestral Manoeuvres in the Dark" --also OMD ... --apply
"""
import os, re, sys, time, sqlite3, argparse, collections, unicodedata
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import musiclib, mutagen

ROOT = musiclib.ROOT
OP = 'unify-artist'
POOL = 'VariousArtists'


def norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'\(.*?\)|\[.*?\]', '', s)
    s = re.sub(r'\b(the|and|a|an)\b', '', s)
    return re.sub(r'[^a-z0-9]', '', s)


def set_tag(conn, rel, field, value):
    full = os.path.join(ROOT, rel.replace('/', os.sep))
    f = mutagen.File(full, easy=True)
    if f is None or f.tags is None:
        return False
    old = (f.get(field) or [None])[0]
    if old == value:
        return False
    f[field] = [value]; f.save()
    conn.execute('INSERT INTO tag_backup (ts,op,path,field,old,new) VALUES (?,?,?,?,?,?)',
                 (time.time(), OP, rel, field, old, value))
    conn.execute('UPDATE files SET %s=? WHERE path=?' % field, (value, rel))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('name', help='the one spelling; also the folder name under VariousArtists unless --folder')
    ap.add_argument('--folder', help='artist folder if it differs from the name')
    ap.add_argument('--also', action='append', default=[], help='an alias to rewrite as well (repeatable)')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    conn = musiclib.db(); conn.row_factory = sqlite3.Row
    folder = POOL + '/' + (a.folder or a.name)
    target = norm(a.name)
    aliases = {norm(x) for x in a.also}

    def qualifies(v):
        parts = [p for p in re.split(r'\s*/\s*', v or '') if p.strip()]
        return bool(parts) and all(norm(p) == target or norm(p) in aliases for p in parts)

    rows = conn.execute("SELECT path, artist, albumartist FROM files WHERE folder LIKE ? AND kind='audio'",
                        (folder + '/%',)).fetchall()
    if not rows:
        print('nothing under', folder); return
    todo, kept = [], collections.Counter()
    for r in rows:
        for field in ('albumartist', 'artist'):
            v = r[field]
            if v == a.name:
                continue
            if v is None or v == '' or qualifies(v):
                todo.append((r['path'], field, v))
            else:
                kept[(field, v)] += 1
    by = collections.Counter((f, v) for _, f, v in todo)
    print('%d files under %s; %d tag values to rewrite as "%s":' % (len(rows), folder, len(todo), a.name))
    for (f, v), n in sorted(by.items(), key=lambda x: -x[1]):
        print('  %-12s %4d  %s' % (f, n, v))
    if kept:
        print('left alone (real credits, or spellings not covered):')
        for (f, v), n in sorted(kept.items(), key=lambda x: -x[1]):
            print('  %-12s %4d  %s' % (f, n, v))
    if not a.apply:
        print('dry run'); return
    n = 0
    for path, field, _ in todo:
        try:
            if set_tag(conn, path, field, a.name):
                n += 1
        except Exception as e:
            print('  failed', path, e)
    conn.commit()
    print('rewrote', n, 'tags')


if __name__ == '__main__':
    main()
