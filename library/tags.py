"""Safe tag repairs.

Deliberately narrow. Only changes that are mechanically decidable from evidence
already in the file tree go here; anything needing judgement (which release a
stray track belongs to, whether two numberings are one album or two) is reported
instead, because a wrong "fix" destroys correct information.

Every original value is snapshotted before a write, so `undo` restores exactly.
"""
import os, sys, sqlite3, json, time, collections, argparse, re
import mutagen
import musiclib, merge

ROOT = musiclib.ROOT

BACKUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS tag_backup (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     REAL,
  op     TEXT,
  path   TEXT,
  field  TEXT,
  old    TEXT,     -- NULL means the field was absent
  new    TEXT
);
CREATE INDEX IF NOT EXISTS ix_tagbk_path ON tag_backup(path);
CREATE INDEX IF NOT EXISTS ix_tagbk_op   ON tag_backup(op);
"""

# how to write one logical field in each tag dialect
WRITERS = {
    'albumartist': {
        'ID3':  ('TPE2', lambda v: mutagen.id3.TPE2(encoding=3, text=[v])),
        'MP4':  ('aART', lambda v: [v]),
        'ASF':  ('WM/AlbumArtist', lambda v: [v]),
        'VC':   ('albumartist', lambda v: [v]),
        'APEv2':('Album Artist', lambda v: v),
    },
}

def kind_of(audio):
    t = type(audio).__name__
    if t in ('MP3', 'AIFF', 'TrueAudio'): return 'ID3'
    if t in ('MP4', 'M4A'):               return 'MP4'
    if t == 'ASF':                        return 'ASF'
    if t in ('FLAC', 'OggVorbis', 'OggOpus', 'OggFLAC'): return 'VC'
    if t in ('Musepack', 'MonkeysAudio', 'WavPack', 'APEv2File'): return 'APEv2'
    if getattr(audio, 'tags', None).__class__.__name__ == 'ID3': return 'ID3'
    return None

def read_field(audio, kindk, field):
    spec = WRITERS[field].get(kindk)
    if not spec: return None
    key = spec[0]
    try:
        t = audio.tags
        if t is None: return None
        if kindk == 'ID3':
            fr = t.getall(key)
            return str(fr[0].text[0]) if fr and fr[0].text else None
        v = t.get(key)
        if v is None: return None
        if isinstance(v, list): v = v[0] if v else None
        return None if v is None else str(v)
    except Exception:
        return None

def write_field(audio, kindk, field, value):
    spec = WRITERS[field].get(kindk)
    if not spec:
        return False
    key, make = spec
    if audio.tags is None:
        try: audio.add_tags()
        except Exception: return False
    if kindk == 'ID3':
        audio.tags.delall(key)
        audio.tags.add(make(value))
    else:
        audio.tags[key] = make(value)
    return True

# ------------------------------------------------------------------ canonical names
VARIOUS = 'Various Artists'

def _penalty(s):
    """Lower is a better display form for the same artist."""
    p = 0
    if re.search(r',\s*(the|a|an)\s*$', s, re.I):
        p += 50                      # "Rolling Stones,The" -- sort-order artefact
    letters = [ch for ch in s if ch.isalpha()]
    if letters and (all(ch.islower() for ch in letters) or
                    all(ch.isupper() for ch in letters)):
        p += 20                      # "greenday" / "REM" lose to proper casing
    if s != s.strip():
        p += 5
    # An accented form is the more complete spelling, not damage: this library
    # stores a genuine U+00E9 in "Sinead O'Connor". Prefer it, gently.
    if any(ord(ch) > 127 for ch in s):
        p -= 2
    return p

def canonical_artists(conn):
    """normalised artist key -> best display spelling seen in the library."""
    forms = collections.defaultdict(collections.Counter)
    for col in ('artist', 'albumartist'):
        for v, n in conn.execute(
                f'SELECT {col},COUNT(*) FROM files WHERE kind="audio" '
                f'AND {col} IS NOT NULL GROUP BY {col}'):
            v = (v or '').strip()
            if v:
                forms[merge.norm_artist(v)][v] += n
    out = {}
    for key, counter in forms.items():
        if not key:
            continue
        out[key] = min(counter, key=lambda s: (_penalty(s), -counter[s], len(s)))
    return out

def plan_albumartist(conn):
    """Fill in a missing albumartist from what the folder already tells us.

    The value written is the library's canonical spelling for that artist, so we
    do not bake "Beatles" into some albums and "The Beatles" into others -- this
    field is what players group albums by.
    """
    canon = canonical_artists(conn)
    albums = merge.load_albums(conn)
    todo = []
    for folder, a in albums.items():
        tracks = a['tracks']
        artists = collections.Counter(t['artist'] for t in tracks if t['artist'])
        if not artists:
            continue
        top, cnt = artists.most_common(1)[0]
        share = cnt / len(tracks)
        existing = collections.Counter(t['albumartist'] for t in tracks if t['albumartist'])
        if existing:
            # an albumartist already agreed on in this folder is the best answer
            value = existing.most_common(1)[0][0]
        elif share >= 0.8:
            value = top
        elif len(artists) >= 4 and share < 0.5:
            # many different performers: a compilation, and that is what
            # albumartist exists to express
            value = VARIOUS
        else:
            value = a['folder_artist'] or top
        if not value:
            continue
        if value != VARIOUS:
            value = canon.get(merge.norm_artist(value), value)
        for t in tracks:
            if not t['albumartist']:
                todo.append((t['path'], value))
    return todo

def apply_albumartist(conn, todo, dry=True, op='albumartist'):
    conn.executescript(BACKUP_SCHEMA)
    done, skipped, errs = 0, 0, []
    for i, (rel, value) in enumerate(todo):
        full = os.path.join(ROOT, rel.replace('/', os.sep))
        try:
            audio = mutagen.File(full)
            if audio is None:
                skipped += 1; continue
            k = kind_of(audio)
            if k is None or k not in WRITERS['albumartist']:
                skipped += 1; continue
            old = read_field(audio, k, 'albumartist')
            if old:
                skipped += 1; continue          # never overwrite an existing value
            if dry:
                done += 1; continue
            if not write_field(audio, k, 'albumartist', value):
                skipped += 1; continue
            audio.save()
            conn.execute('INSERT INTO tag_backup (ts,op,path,field,old,new) '
                         'VALUES (?,?,?,?,?,?)',
                         (time.time(), op, rel, 'albumartist', old, value))
            conn.execute('UPDATE files SET albumartist=? WHERE path=?', (value, rel))
            done += 1
        except Exception as e:
            errs.append((rel, f'{type(e).__name__}: {str(e)[:90]}'))
        if not dry and (i + 1) % 1000 == 0:
            conn.commit(); print(f'   {i+1}/{len(todo)}', file=sys.stderr, flush=True)
    if not dry: conn.commit()
    return done, skipped, errs

def undo(conn, op):
    conn.executescript(BACKUP_SCHEMA)
    rows = conn.execute('SELECT id,path,field,old FROM tag_backup WHERE op=? '
                        'ORDER BY id DESC', (op,)).fetchall()
    n = 0
    for rid, rel, field, old in rows:
        full = os.path.join(ROOT, rel.replace('/', os.sep))
        try:
            audio = mutagen.File(full)
            if audio is None: continue
            k = kind_of(audio)
            if old is None:
                key = WRITERS[field][k][0]
                if k == 'ID3': audio.tags.delall(key)
                else:
                    try: del audio.tags[key]
                    except Exception: pass
            else:
                write_field(audio, k, field, old)
            audio.save()
            conn.execute('UPDATE files SET albumartist=? WHERE path=?', (old, rel))
            conn.execute('DELETE FROM tag_backup WHERE id=?', (rid,))
            n += 1
        except Exception:
            pass
    conn.commit()
    return n

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--undo', metavar='OP')
    a = ap.parse_args()
    conn = sqlite3.connect('library.db', timeout=60); conn.row_factory = sqlite3.Row
    if a.undo:
        print('restored', undo(conn, a.undo), 'tags'); sys.exit()
    todo = plan_albumartist(conn)
    vals = collections.Counter(v for _, v in todo)
    print(f'{"APPLYING" if a.apply else "DRY RUN"}: albumartist for {len(todo)} files')
    print(f'   distinct values: {len(vals)}')
    for v, n in vals.most_common(12): print(f'      {v[:44]:46} {n}')
    nvar = sum(n for v, n in vals.items() if v == VARIOUS)
    print(f'   marked "{VARIOUS}" (compilations): {nvar}')
    done, skipped, errs = apply_albumartist(conn, todo, dry=not a.apply)
    print(f'\n{"would write" if not a.apply else "wrote"} {done}, skipped {skipped}, errors {len(errs)}')
    for p, e in errs[:8]: print(f'   {p[:70]}: {e}')
