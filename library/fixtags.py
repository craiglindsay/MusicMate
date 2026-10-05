"""Rewrite the tags of files whose metadata was clobbered.

For these seven folders AcoustID confirmed what the filenames already said: the
folder and file names are right and the tags belong to some other record entirely.
So the audio supplies artist and title, and the album comes from the folder.

Why not AcoustID's album: a recording belongs to many release groups, and the
service may hand back a compilation ("Pump It: Workout Mix") rather than the record
this folder actually is. The folder name has been independently corroborated here,
so it is the better source for that one field.
"""
import os, sys, re, time, sqlite3, argparse, collections
import mutagen, mutagen.id3
import musiclib, merge, tags as tagmod, canonize

ROOT = musiclib.ROOT
MIN_SCORE = 0.85

def plan(conn, acdb, folders):
    """Per folder: settle the artist once, and only accept a title the filename agrees with."""
    canon, _amb, _rej = canonize.canon_map(conn)
    out, weak, conflict = [], [], []
    for folder in folders:
        rows = conn.execute('SELECT * FROM files WHERE folder=? AND kind="audio" '
                            'ORDER BY filename', (folder,)).fetchall()
        ids = {}
        for r in rows:
            hit = acdb.execute(
                'SELECT artist,title,album,score FROM ident WHERE path=? AND title IS NOT NULL',
                (r['path'],)).fetchone()
            if hit and (hit[3] or 0) >= MIN_SCORE:
                ids[r['path']] = hit
            elif hit:
                weak.append((r['path'], hit[3]))

        # One artist and one album for the whole folder: AcoustID answers per
        # recording, so it happily returns "Black Eyed Peas" for one track and
        # "The Black Eyed Peas" for the next.
        arts = collections.Counter(i[0] for i in ids.values() if i[0])
        albs = collections.Counter(i[2] for i in ids.values() if i[2])
        if not arts:
            continue
        artist = arts.most_common(1)[0][0]
        artist = canon.get(merge.norm_artist(artist), (artist,))[0]
        album = albs.most_common(1)[0][0] if albs else None
        if album:
            # MusicBrainz stylises some titles with typographic marks ("The E<bullet>N<bullet>D").
            # Fold those to their plain equivalents so the tag stays searchable.
            album = canonize.clean_name(album).replace('•', '.')

        # A title AcoustID gives for two different files means one of them holds the
        # wrong audio; renaming both would leave the album with a duplicate and lose
        # a track. Those get reported instead.
        title_count = collections.Counter(i[1] for i in ids.values() if i[1])

        for r in rows:
            hit = ids.get(r['path'])
            if not hit:
                continue
            _a, title, _al, score = hit
            stem = re.sub(r'^\d{1,3}\s*[-.]?\s*', '',
                          os.path.splitext(r['filename'])[0])
            # Some filenames repeat the artist ("01 - Arcadia - Election Day"),
            # which would otherwise look like a disagreement with the title.
            cand = {stem}
            for a2 in {artist, _a}:
                if a2 and stem.lower().startswith(a2.lower()):
                    cand.add(re.sub(r'^\s*[-–]\s*', '', stem[len(a2):]))
            agrees = any(merge.norm_title(x) == merge.norm_title(title or '')
                         for x in cand)
            changes = {}
            if artist and artist != r['artist']:
                changes['artist'] = artist
            if artist and artist != r['albumartist']:
                changes['albumartist'] = artist
            if album and album != r['album']:
                changes['album'] = album
            if title and title != r['title']:
                if agrees:
                    changes['title'] = title
                else:
                    conflict.append((r['path'], r['filename'], title,
                                     title_count[title] > 1))
            if changes:
                out.append((r['path'], changes, score))
    return out, weak, conflict

FIELDS = dict(canonize.FIELDS)
FIELDS['title'] = {
    'ID3':  ('TIT2', lambda v: mutagen.id3.TIT2(encoding=3, text=[v])),
    'MP4':  ('\xa9nam', lambda v: [v]),
    'ASF':  ('Title', lambda v: [v]),
    'VC':   ('title', lambda v: [v]),
    'APEv2': ('Title', lambda v: v),
}
FIELDS['album'] = {
    'ID3':  ('TALB', lambda v: mutagen.id3.TALB(encoding=3, text=[v])),
    'MP4':  ('\xa9alb', lambda v: [v]),
    'ASF':  ('WM/AlbumTitle', lambda v: [v]),
    'VC':   ('album', lambda v: [v]),
    'APEv2': ('Album', lambda v: v),
}

def rw(audio, k, field, value=None):
    spec = FIELDS[field].get(k)
    if not spec: return None if value is None else False
    key, make = spec
    if value is None:
        try:
            if k == 'ID3':
                fr = audio.tags.getall(key)
                return str(fr[0].text[0]) if fr and fr[0].text else None
            v = audio.tags.get(key)
            if isinstance(v, list): v = v[0] if v else None
            return None if v is None else str(v)
        except Exception:
            return None
    if audio.tags is None:
        try: audio.add_tags()
        except Exception: return False
    if k == 'ID3':
        audio.tags.delall(key); audio.tags.add(make(value))
    else:
        audio.tags[key] = make(value)
    return True

def apply(conn, work, dry=True, op='fixtags'):
    conn.executescript(tagmod.BACKUP_SCHEMA)
    files = fields = 0
    errs = []
    for rel, changes, score in work:
        full = os.path.join(ROOT, rel.replace('/', os.sep))
        try:
            audio = mutagen.File(full)
            if audio is None: continue
            k = tagmod.kind_of(audio)
            if k is None: continue
            touched = False
            for field, value in changes.items():
                if k not in FIELDS.get(field, {}): continue
                old = rw(audio, k, field)
                if old == value: continue
                if dry:
                    fields += 1; touched = True; continue
                if not rw(audio, k, field, value): continue
                conn.execute('INSERT INTO tag_backup (ts,op,path,field,old,new) '
                             'VALUES (?,?,?,?,?,?)',
                             (time.time(), op, rel, field, old, value))
                fields += 1; touched = True
            if touched:
                files += 1
                if not dry:
                    audio.save()
                    for f in ('artist', 'albumartist', 'album', 'title'):
                        if f in changes:
                            conn.execute(f'UPDATE files SET {f}=? WHERE path=?',
                                         (changes[f], rel))
        except Exception as e:
            errs.append((rel, f'{type(e).__name__}: {str(e)[:70]}'))
    if not dry: conn.commit()
    return files, fields, errs

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    import json
    tg = json.load(open('acoustid_targets.json', encoding='utf-8'))
    conn = musiclib.db(); conn.row_factory = sqlite3.Row
    acdb = sqlite3.connect('acoustid_cache.db', timeout=120)
    work, weak, conflict = plan(conn, acdb, tg['clob'])
    print(f'{"APPLYING" if a.apply else "DRY RUN"}: {len(work)} tracks to retag '
          f'({len(weak)} below score {MIN_SCORE}, skipped)')
    print(f'  titles left alone because the audio disagrees with the filename: {len(conflict)}')
    for pth, fn, t, dup in conflict:
        note = ' - audio DUPLICATES another track in this album' if dup else ''
        print(f'     {fn[:40]:42} audio says {t[:26]!r}{note}')
    byf = collections.Counter(os.path.dirname(w[0]) for w in work)
    for f, n in byf.most_common():
        print(f'   {n:3}  {f.split("/",1)[1][:62]}')
    print('\n  sample:')
    for rel, ch, sc in work[:5]:
        print(f'     {os.path.basename(rel)[:42]:44} score={sc:.2f}')
        for k2, v in ch.items(): print(f'        {k2:12} -> {v[:46]}')
    files, fields, errs = apply(conn, work, dry=not a.apply)
    print(f'\n{"would write" if not a.apply else "wrote"} {fields} fields across '
          f'{files} files, errors {len(errs)}')
    for p, e in errs[:5]: print(f'   {p}: {e}')
    conn.close(); acdb.close()
