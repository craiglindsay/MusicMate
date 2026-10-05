"""Apply MusicBrainz canonical artist names, and populate the sort fields.

The convention every major tagger follows keeps two values: the display name carries
the article ("The Beatles") and a separate sort name moves it to the back
("Beatles, The"), so the library reads correctly and still files under B. Putting
"Beatles, The" in the display field -- which this library does in places -- is the
one arrangement the convention rules out.

Only applied where MusicBrainz gave an unambiguous answer; near-ties are reported.
"""
import os, sys, time, sqlite3, argparse, collections
import mutagen, mutagen.id3
import musiclib, merge, tags as tagmod, mbresolve

ROOT = musiclib.ROOT

FIELDS = {
    'artist': {
        'ID3':  ('TPE1', lambda v: mutagen.id3.TPE1(encoding=3, text=[v])),
        'MP4':  ('\xa9ART', lambda v: [v]),
        'ASF':  ('Author', lambda v: [v]),
        'VC':   ('artist', lambda v: [v]),
        'APEv2':('Artist', lambda v: v),
    },
    'albumartist': tagmod.WRITERS['albumartist'],
    'artistsort': {
        'ID3':  ('TSOP', lambda v: mutagen.id3.TSOP(encoding=3, text=[v])),
        'MP4':  ('soar', lambda v: [v]),
        'ASF':  ('WM/ArtistSortOrder', lambda v: [v]),
        'VC':   ('artistsort', lambda v: [v]),
        'APEv2':('ArtistSort', lambda v: v),
    },
    'albumartistsort': {
        'ID3':  ('TSO2', lambda v: mutagen.id3.TSO2(encoding=3, text=[v])),
        'MP4':  ('soaa', lambda v: [v]),
        'ASF':  ('WM/AlbumArtistSortOrder', lambda v: [v]),
        'VC':   ('albumartistsort', lambda v: [v]),
        'APEv2':('AlbumArtistSort', lambda v: v),
    },
}

def read(audio, k, field):
    spec = FIELDS[field].get(k)
    if not spec or audio.tags is None: return None
    key = spec[0]
    try:
        if k == 'ID3':
            fr = audio.tags.getall(key)
            return str(fr[0].text[0]) if fr and fr[0].text else None
        v = audio.tags.get(key)
        if v is None: return None
        if isinstance(v, list): v = v[0] if v else None
        return None if v is None else str(v)
    except Exception:
        return None

def write(audio, k, field, value):
    spec = FIELDS[field].get(k)
    if not spec: return False
    key, make = spec
    if audio.tags is None:
        try: audio.add_tags()
        except Exception: return False
    if k == 'ID3':
        audio.tags.delall(key)
        audio.tags.add(make(value))
    else:
        audio.tags[key] = make(value)
    return True

MIN_SCORE = 90

# MusicBrainz writes names with typographic punctuation: a real U+2010 HYPHEN in
# "Buddha-Bar", a curly apostrophe in "O'Connor". They look identical to the ASCII
# forms but break typing, search and sorting, so fold them. Letter diacritics are a
# different matter entirely -- "Bjork" -> "Bjork" and "Anibal" -> "Anibal" are
# corrections worth having, so those are kept exactly as returned.
PUNCT = {0x2010: '-', 0x2011: '-', 0x2012: '-', 0x2013: '-', 0x2014: '-',
         0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201C: '"', 0x201D: '"',
         0x2026: '...', 0x00A0: ' ', 0x2032: "'"}

def clean_name(s):
    return (s or '').translate(PUNCT)

def is_stylised(s):
    """Names carrying decorative symbols ('A*Teens') are branding, not spellings."""
    import unicodedata
    return any(unicodedata.category(ch) in ('So', 'Sk') for ch in s or '')

def canon_map(conn):
    """normalised key -> (canonical name, sort name). Confident answers only."""
    out, ambiguous, rejected = {}, [], []
    for q, norm, name, sort, score, ncand, country, typ in conn.execute(
            'SELECT query,norm,mb_name,mb_sort,score,n_cand,country,type '
            'FROM mb_artist WHERE mb_name IS NOT NULL'):
        if (score or 0) < MIN_SCORE:
            continue
        if is_stylised(name):
            rejected.append((q, name, 'decorative symbol')); continue
        if mbresolve.is_stub(typ, country):
            # no type and no country: a throwaway MusicBrainz row. This is how
            # "OMD" matched a Ragga Dancehall act and "Buddha Bar" an Amazon
            # scrape. Keep the name we already have.
            rejected.append((q, name, 'MusicBrainz stub entry')); continue
        name, sort = clean_name(name), clean_name(sort or name)
        if norm in out and out[norm][0] != name:
            ambiguous.append((norm, out[norm][0], name))
            continue
        out[norm] = (name, sort)
    return out, ambiguous, rejected

def plan(conn):
    canon, ambiguous, rejected = canon_map(conn)
    rows = conn.execute(
        'SELECT path,artist,albumartist FROM files WHERE kind="audio"').fetchall()
    work = []
    def lookup(value):
        """Canonical entry for a tag value, un-inverting sort-order names first.

        "Taylor,James" is James Taylor written back-to-front; asking MusicBrainz
        for it verbatim matches an unrelated Dominican singer of that name.
        """
        flipped = mbresolve.flip_inverted(value)
        if flipped:
            hit = canon.get(merge.norm_artist(flipped))
            if hit:
                return hit
        return canon.get(merge.norm_artist(value))

    for path, artist, albumartist in rows:
        changes = {}
        if artist:
            hit = lookup(artist)
            if hit:
                if hit[0] != artist: changes['artist'] = hit[0]
                changes['artistsort'] = hit[1]
        if albumartist and albumartist.lower() != 'various artists':
            hit = lookup(albumartist)
            if hit:
                if hit[0] != albumartist: changes['albumartist'] = hit[0]
                changes['albumartistsort'] = hit[1]
        elif albumartist:
            changes['albumartistsort'] = 'Various Artists'
        if changes:
            work.append((path, changes))
    return work, canon, ambiguous, rejected

def apply(conn, work, dry=True, op='canonize'):
    conn.executescript(tagmod.BACKUP_SCHEMA)
    written = fieldcount = 0
    errs = []
    for i, (rel, changes) in enumerate(work):
        full = os.path.join(ROOT, rel.replace('/', os.sep))
        try:
            audio = mutagen.File(full)
            if audio is None: continue
            k = tagmod.kind_of(audio)
            if k is None: continue
            touched = False
            for field, value in changes.items():
                if k not in FIELDS[field]: continue
                old = read(audio, k, field)
                if old == value: continue
                if dry:
                    fieldcount += 1; touched = True; continue
                if not write(audio, k, field, value): continue
                conn.execute('INSERT INTO tag_backup (ts,op,path,field,old,new) '
                             'VALUES (?,?,?,?,?,?)',
                             (time.time(), op, rel, field, old, value))
                fieldcount += 1; touched = True
            if touched:
                written += 1
                if not dry:
                    audio.save()
                    if 'artist' in changes:
                        conn.execute('UPDATE files SET artist=? WHERE path=?',
                                     (changes['artist'], rel))
                    if 'albumartist' in changes:
                        conn.execute('UPDATE files SET albumartist=? WHERE path=?',
                                     (changes['albumartist'], rel))
        except Exception as e:
            errs.append((rel, f'{type(e).__name__}: {str(e)[:80]}'))
        if not dry and (i + 1) % 1000 == 0:
            conn.commit(); print(f'   {i+1}/{len(work)}', file=sys.stderr, flush=True)
    if not dry: conn.commit()
    return written, fieldcount, errs

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--max', type=int, help='stop after this many files (resumable)')
    a = ap.parse_args()
    conn = musiclib.db()
    conn.executescript(tagmod.BACKUP_SCHEMA)
    work, canon, ambiguous, rejected = plan(conn)
    # A previous run may have been interrupted. Skipping files already recorded in
    # the backup log makes this resumable without re-opening 30,000 files to
    # discover there is nothing to do.
    done = {r[0] for r in conn.execute(
        "SELECT DISTINCT path FROM tag_backup WHERE op='canonize'")}
    if done:
        before = len(work)
        work = [w for w in work if w[0] not in done]
        print(f'  resuming: {before - len(work)} files already written')
    if a.max:
        work = work[:a.max]
    print(f'{"APPLYING" if a.apply else "DRY RUN"}')
    print(f'  artists resolved confidently : {len(canon)}')
    print(f'  ambiguous, left alone        : {len(ambiguous)}')
    print(f'  stylised, rejected           : {len(rejected)}')
    for q, n, why in rejected[:6]:
        print(f'      kept {q!r} rather than {n!r} ({why})')
    print(f'  files to touch               : {len(work)}')
    renames = collections.Counter()
    for _, ch in work:
        for f in ('artist', 'albumartist'):
            if f in ch: renames[f] += 1
    print(f'  display-name corrections     : artist {renames["artist"]}, '
          f'albumartist {renames["albumartist"]}')
    seen = {}
    for _, ch in work:
        if 'artist' in ch: seen[ch['artist']] = seen.get(ch['artist'], 0) + 1
    print('\n  sample display-name changes:')
    for v, n in sorted(seen.items(), key=lambda kv: -kv[1])[:14]:
        print(f'     -> {v[:44]:46} {n} files')
    w, fc, errs = apply(conn, work, dry=not a.apply)
    print(f'\n{"would touch" if not a.apply else "touched"} {w} files, '
          f'{fc} tag fields, errors {len(errs)}')
    for p, e in errs[:6]: print(f'   {p[:64]}: {e}')
    conn.close()
