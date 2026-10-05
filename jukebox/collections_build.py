"""Group box sets and multi-disc albums, derived from the folder tree.

Why folders and not tags: a box set is exactly the thing whose tags are least
trustworthy. All 22 discs of Duran Duran's "The Singles 1986-2004" carry the tag
"The Singles 1986-1995" -- wrong years, repeated 22 times -- so the Albums grid
showed twenty identical tiles.

Two different things live here, and conflating them is the easy mistake:

  kind='album'  one release pressed across several discs (The White Album,
                Disc 1 / Disc 2). This is really just an album, so it shows as
                ONE ordinary tile and opens as one track list with disc heads.

  kind='set'    a box of separate releases (28 Depeche Mode CD singles). One
                tile that opens into its discs, each its own album.

The rules are deliberately conservative, because the costly error is merging
records that are genuinely different albums. Two traps found in this library:

  * "Vol." is NOT a disc marker. Tim McGraw's "Greatest Hits, Vol. 2" and
    "Vol. 3" are different albums, as are Madonna's GHV1 and GHV2.
  * A parent folder is not automatically a set. "Annie Lennox/Medusa" contains
    "Bare" and "Diva" -- three separate albums filed oddly, not a 2-disc set.

So a group forms only on positive evidence: an explicit Disc/Disk/CD number
(-> one album), an ordered NN-prefixed run of named discs (-> box set), or four
or more siblings under one parent (-> box set). Anything else is left alone.

Idempotent: safe to re-run, and re-run at the end of every ETL.
"""
import re, sqlite3, collections as _c, posixpath
from pathlib import Path

DB = Path(__file__).resolve().parent / 'jukebox.db'

SCHEMA = """
CREATE TABLE IF NOT EXISTS collection (
  id INTEGER PRIMARY KEY, artist_id INTEGER, name TEXT, sort_name TEXT,
  folder TEXT UNIQUE, n_discs INTEGER, n_tracks INTEGER, duration REAL,
  year_min INTEGER, year_max INTEGER, art_album_id INTEGER, kind TEXT DEFAULT 'set'
);
CREATE INDEX IF NOT EXISTS ix_coll_artist ON collection(artist_id);
"""

# an explicit disc number -- note that "vol" is absent on purpose
WORD_NUM = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6,
            'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'i': 1, 'ii': 2,
            'iii': 3, 'iv': 4, 'v': 5, 'vi': 6}
# "One More From The Road Disc Two" spells the number out, so a digits-only
# pattern left it as two unrelated albums sitting side by side in the grid
MARKER = re.compile(r'(?i)^(.*?)[\s._-]*[\(\[]?\s*(?:disc|disk|cd)\s*\.?\s*'
                    r'(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|i{1,3}|iv|vi?)'
                    r'\s*[\)\]]?\s*$')


def disc_number(tok):
    tok = (tok or '').strip().lower()
    if tok.isdigit():
        return int(tok)
    return WORD_NUM.get(tok)
# an ordered run like "01 - Notorious"
NUMBERED = re.compile(r'^\s*(\d{1,3})\s*[-._)]\s*(.+)$')
MIN_NAMED = 4          # unnumbered siblings needed before calling it a box


def sort_name(s):
    s = re.sub(r'^(the|a|an)\s+', '', (s or '').lower()).strip()
    return re.sub(r'^[^a-z0-9]+', '', s) or (s or '')


def read_leaf(leaf):
    """(disc_no, label, how) where how is 'marker' | 'number' | None."""
    m = MARKER.match(leaf)
    if m:
        n = disc_number(m.group(2))
        if n:
            return n, 'Disc %d' % n, 'marker'
    m = NUMBERED.match(leaf)
    if m:
        return int(m.group(1)), m.group(2).strip(), 'number'
    return None, leaf, None


def build(db=DB, verbose=False):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    cols = {r['name'] for r in c.execute('PRAGMA table_info(album)')}
    for col, ddl in (('collection_id', 'INTEGER'), ('disc_label', 'TEXT'),
                     ('disc_no', 'INTEGER')):
        if col not in cols:
            c.execute('ALTER TABLE album ADD COLUMN %s %s' % (col, ddl))
    if 'kind' not in {r['name'] for r in c.execute('PRAGMA table_info(collection)')}:
        c.execute("ALTER TABLE collection ADD COLUMN kind TEXT DEFAULT 'set'")
    c.execute('CREATE INDEX IF NOT EXISTS ix_album_coll ON album(collection_id)')
    c.execute('DELETE FROM collection')
    c.execute('UPDATE album SET collection_id=NULL, disc_label=NULL, disc_no=NULL')

    rows = c.execute("SELECT id, folder, title, artist_id, n_tracks, duration, "
                     "year, art_w, art_kind FROM album WHERE folder IS NOT NULL").fetchall()

    cands = {}          # key -> (display name, [rows])

    # Shape 1 -- nesting: root/artist/<Set>/<Disc>
    nested = _c.defaultdict(list)
    for r in rows:
        if len(r['folder'].split('/')) >= 4:
            nested[posixpath.dirname(r['folder'])].append(r)
    claimed = set()
    for folder, members in nested.items():
        if len(members) < 2:
            continue
        segs = folder.split('/')
        cands[folder] = (' · '.join(segs[2:]) or segs[-1], members)
        claimed.update(m['id'] for m in members)

    # Shape 2 -- siblings: ".../White Album (Disc 1)" beside "(Disc 2)".
    # Nesting alone missed five of these, a second White Album among them,
    # because nothing about their depth marks them as belonging together.
    sib = _c.defaultdict(list)
    for r in rows:
        if r['id'] in claimed:
            continue
        leaf = r['folder'].split('/')[-1]
        m = MARKER.match(leaf)
        if not m:
            # Sometimes only the TITLE carries the marker: Lynyrd Skynyrd's
            # "One More From The Road Disc One" sits in a folder with no disc
            # in its name, beside a sibling folder that does have one.
            m = MARKER.match((r['title'] or '').strip())
        if not m or not disc_number(m.group(2)):
            continue
        base = (m.group(1) or '').strip(' -_.')
        if not base:
            continue
        sib[(posixpath.dirname(r['folder']), base.lower())].append((r, base))
    for (parent, _b), pairs in sib.items():
        if len(pairs) < 2:
            continue
        key = parent + '/' + pairs[0][1]
        while key in cands:
            key += ' '
        cands[key] = (pairs[0][1], [r for r, _ in pairs])

    made = skipped = 0
    for folder in sorted(cands):
        name, members = cands[folder]
        # read the leaf, falling back to the title where the disc lives there
        def _read(m):
            got = read_leaf(m['folder'].split('/')[-1])
            if got[2] is None:
                alt = read_leaf((m['title'] or '').strip())
                if alt[2] == 'marker':
                    return alt
            return got
        read = [(m,) + _read(m) for m in members]
        hows = {r[3] for r in read}

        if hows == {'marker'}:
            kind = 'album'                      # Disc 1 / Disc 2 of one release
        elif hows == {'number'}:
            kind = 'set'                        # an ordered run of named discs
        elif len(members) >= MIN_NAMED:
            kind = 'set'                        # a big folder of named discs
        else:
            skipped += 1                        # not enough evidence: leave alone
            if verbose:
                print('skip  %-46s %d albums' % (name[:46], len(members)))
            continue

        tcount = _c.Counter((m['title'] or '').strip().lower() for m in members)
        prepared = []
        for m, n, lab, how in read:
            t = (m['title'] or '').strip()
            # A real disc number always wins: a tag reading "White Album (Disc 1)"
            # must not override the clean "Disc 1" the folder already gave us.
            if how != 'marker' and t and tcount[t.lower()] == 1 \
                    and sort_name(t) != sort_name(name):
                lab = t
            prepared.append([m, n, lab])
        prepared.sort(key=lambda p: (p[1] is None, p[1] or 0, (p[2] or '').lower()))
        for i, p in enumerate(prepared, 1):
            if p[1] is None:
                p[1] = i

        years = [p[0]['year'] for p in prepared if p[0]['year']]
        art = sorted(prepared, key=lambda p: -((p[0]['art_w'] or 0) if p[0]['art_kind'] else 0))
        cid = c.execute(
            'INSERT INTO collection (artist_id,name,sort_name,folder,n_discs,n_tracks,'
            'duration,year_min,year_max,art_album_id,kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (_c.Counter(m['artist_id'] for m in members).most_common(1)[0][0],
             name, sort_name(name), folder, len(prepared),
             sum(p[0]['n_tracks'] or 0 for p in prepared),
             sum(p[0]['duration'] or 0 for p in prepared),
             min(years) if years else None, max(years) if years else None,
             art[0][0]['id'] if art else None, kind)).lastrowid
        for p in prepared:
            c.execute('UPDATE album SET collection_id=?, disc_label=?, disc_no=? WHERE id=?',
                      (cid, p[2], p[1], p[0]['id']))
        made += 1
        if verbose:
            print('%-5s %-46s %2d discs' % (kind, name[:46], len(prepared)))
    c.commit()
    n_alb = c.execute('SELECT COUNT(*) FROM album WHERE collection_id IS NOT NULL').fetchone()[0]
    n_set = c.execute("SELECT COUNT(*) FROM collection WHERE kind='set'").fetchone()[0]
    n_one = c.execute("SELECT COUNT(*) FROM collection WHERE kind='album'").fetchone()[0]
    c.close()
    return made, n_alb, n_set, n_one, skipped


if __name__ == '__main__':
    m, a, ns, no, sk = build(verbose=True)
    print('')
    print('%d groups covering %d albums: %d box sets, %d multi-disc albums (%d left alone)'
          % (m, a, ns, no, sk))
