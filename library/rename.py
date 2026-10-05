"""Restore filenames that were cut short, using the full title from the tags.

Only touches files where the name is provably a truncation: the filename body is a
strict prefix of the tag title. Where the tag was cut too (about 700 files here),
nothing local can recover the real name, so those are left alone.

The existing track-number prefix is preserved verbatim -- folders in this library
use both "NN - Title" and "NN Title", and imposing one would churn thousands of
files that are already fine.
"""
import os, re, sys, time, sqlite3, argparse
import musiclib

ROOT = musiclib.ROOT
MAXPATH = 240          # leave headroom under the Windows 260-char limit

# Windows forbids  < > : " / \ | ? *  -- replace them with readable stand-ins
# rather than deleting, and never touch legal characters like $ ! # & '
ILLEGAL = {'<': '(', '>': ')', ':': ' -', '"': "'", '/': '-', '\\': '-',
           '|': '-', '?': '', '*': ''}

def sanitize(name):
    out = []
    for ch in name:
        if ch in ILLEGAL:
            out.append(ILLEGAL[ch])
        elif ord(ch) < 32:
            continue
        else:
            out.append(ch)
    s = ''.join(out)
    s = re.sub(r'\s+', ' ', s).strip()
    s = s.rstrip('. ')                      # Windows drops trailing dot/space
    return s

def plan(conn):
    rows = conn.execute(
        'SELECT path,folder,filename,title,ext FROM files '
        'WHERE kind="audio" AND title IS NOT NULL AND title!=""').fetchall()
    out, toolong, collide = [], [], []
    seen = set()
    for path, folder, filename, title, ext in rows:
        stem = os.path.splitext(filename)[0]
        m = re.match(r'^(\d{1,3}\s*[-.]?\s*)(.*)$', stem)
        prefix, body = (m.group(1), m.group(2)) if m else ('', stem)
        body, title = body.strip(), title.strip()
        if len(body) < 8:
            continue
        if not (len(title) > len(body) + 2 and title.lower().startswith(body.lower())):
            continue
        newstem = prefix + sanitize(title)
        newname = newstem + ext
        if newname == filename:
            continue
        newpath = f'{folder}/{newname}'
        if len(os.path.join(ROOT, newpath.replace('/', os.sep))) > MAXPATH:
            toolong.append((path, newname)); continue
        if newpath.lower() in seen:
            collide.append((path, newname)); continue
        seen.add(newpath.lower())
        out.append((path, newpath, newname))
    return out, toolong, collide

def apply(conn, moves, dry=True):
    done, errs = 0, []
    for old, new, newname in moves:
        src = os.path.join(ROOT, old.replace('/', os.sep))
        dst = os.path.join(ROOT, new.replace('/', os.sep))
        if dry:
            done += 1; continue
        try:
            if not os.path.exists(src) or os.path.exists(dst):
                continue
            size = os.path.getsize(src)
            os.replace(src, dst)
            conn.execute('INSERT INTO moves (ts,op,reason,src,dst,size) '
                         'VALUES (?,?,?,?,?,?)',
                         (time.time(), 'rename', 'restored full title from tag',
                          old, new, size))
            conn.execute('UPDATE files SET path=?, filename=? WHERE path=?',
                         (new, newname, old))
            done += 1
        except OSError as e:
            errs.append((old, str(e)))
        if not dry and done % 200 == 0:
            conn.commit()
    if not dry: conn.commit()
    return done, errs

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    conn = musiclib.db()
    moves, toolong, collide = plan(conn)
    print(f'{"APPLYING" if a.apply else "DRY RUN"}: {len(moves)} filenames to restore')
    print(f'   skipped, path would exceed {MAXPATH} chars : {len(toolong)}')
    print(f'   skipped, name collides with a sibling      : {len(collide)}')
    print('\nsample:')
    for old, new, nn in moves[:12]:
        print(f'   {os.path.basename(old)[:46]:48} -> {nn[:52]}')
    if toolong:
        print('\ntoo long (left as-is):')
        for p, n in toolong[:4]: print(f'   {n[:70]}')
    done, errs = apply(conn, moves, dry=not a.apply)
    print(f'\n{"would rename" if not a.apply else "renamed"} {done}, errors {len(errs)}')
    for p, e in errs[:6]: print(f'   {p}: {e}')
    conn.close()
