"""Bring albums from the staging folder into the library.

The staging folder (MUSICMATE_STAGING, default ~/ToBeProcessed) is laid out
Artist/Album (Year)/[CD 01/]Artist - Album - NN - Title.ext. That structure is
the most reliable evidence we have: many files arrived with no album-artist, no
disc number, a reissue's date, or an edition's album title. So the folder tree
supplies artist, album, year, disc and track, the tags fill in the rest, and
AcoustID is asked to confirm the audio wherever a tag had to be inferred or
disagrees with the folder.

Per album, in order:
  1. read tags; infer what is missing from the path
  2. chromaprint every file; compare with the library's fingerprints for
     duplicates; AcoustID-identify the files whose tags were inferred
  3. write the settled tags (every old value logged to tag_backup, op ingest)
  4. embed cover art at 600px (existing embedded art, else Cover Art Archive by
     the MusicBrainz release group we already know from the chart work, else a
     MusicBrainz search); the full-size cover goes into the art cache for etl
  5. move the files to D:\\Music\\<Pool>\\<Artist>\\<Album>\\[Disc N\\]NN - Title.ext
     (logged in `moves`, op ingest, so `musiclib.py undo --op ingest` reverses it),
     register them in library.db with their fingerprints, and remove the emptied
     source folders

Pools: an artist's albums go to VariousArtists (soundtracks credited to one
artist included, as the library already does with 8 Mile); a "Various Artists"
staging folder goes to Soundtracks or Compilations by its name; anything that
reads as a Christmas record goes to Christmas.

Duplicates: a file whose audio matches a library track (bit error rate below
merge.FP_SAME) in the SAME album is a second copy; the higher-bitrate one is kept
and the other goes to _superseded/incoming (logged, undoable). A match elsewhere
(a compilation, Era) is only reported; albums are kept whole.

An album is left in staging and reported, not ingested, when AcoustID disagrees
with the folder's artist on more than 30% of the tracks it could identify.

    python ingest.py                       # dry run over everything
    python ingest.py --artist "Adele"      # one artist
    python ingest.py --limit 5 --apply     # ingest the first five albums
    python ingest.py --apply
"""
import os, re, io, sys, json, time, shutil, sqlite3, argparse, collections, subprocess
from concurrent.futures import ThreadPoolExecutor
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
for _s in (sys.stdout, sys.stderr):  # album titles carry characters cp1252 lacks
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')
import mutagen, mutagen.id3, mutagen.mp4
import musiclib, merge, canonize, artfetch, embedart, fixart, acoustid
import tags as tagmod
import numpy as np

ROOT = musiclib.ROOT
STAGING = os.environ.get('MUSICMATE_STAGING') or os.path.join(os.path.expanduser('~'), 'ToBeProcessed')
OP = 'ingest'
MAIN = 'VariousArtists'
SUPERSEDED = os.path.join(ROOT, musiclib.SUPERSEDED, 'incoming')
CHARTS = os.path.join(HERE, '..', 'jukebox', 'charts')
PLAN = os.path.join(HERE, 'ingestplan.json')
REPORT = os.path.join(HERE, 'ingestreport.txt')
AUDIO = {'.mp3', '.m4a', '.flac', '.ogg', '.opus', '.wma', '.aac', '.wav', '.aiff'}

FILE_RE = re.compile(r'^(?P<artist>.+?) - (?P<album>.+?) - (?P<track>\d{1,3}) - (?P<title>.+)$')
NUM_RE = re.compile(r'^(?P<track>\d{1,3})[\s._-]+(?P<title>.+)$')
FOLDER_RE = re.compile(r'^(?P<album>.+?)\s*\((?P<year>\d{4})\)\s*$')
CHRISTMAS = re.compile(r'christmas|xmas|holiday|noel|navidad', re.I)
SOUNDTRACK = re.compile(r'soundtrack|motion picture|music from|inspired by|original score|(?<![a-z])the album(?![a-z])', re.I)
VARIOUS = 'Various Artists'
# Spellings the user has settled (8 Sep 2026): keyed by merge.norm_artist of the arrival.
ALIASES = {'pnk': 'Pink', 'ye': 'Kanye West'}


def nart(s):
    """merge.norm_artist with the settled spellings folded in, so P!nk and Pink compare equal."""
    n = merge.norm_artist(s or '')
    return merge.norm_artist(ALIASES[n]) if n in ALIASES else n
ACOUSTID_MIN = 0.85
DISAGREE_MAX = 0.30


def sanitize(name):
    ILLEGAL = {'<': '(', '>': ')', ':': ' -', '"': "'", '/': '-', '\\': '-', '|': '-', '?': '', '*': ''}
    s = ''.join(ILLEGAL.get(ch, ch) for ch in canonize.clean_name(name or '') if ord(ch) >= 32)
    return re.sub(r'\s+', ' ', s).strip().rstrip('. ') or 'Unknown'


def disc_number(sub, index):
    m = re.search(r'(\d+)\s*$', sub or '')
    return int(m.group(1)) if m else index + 1


# ------------------------------------------------------------------ the library as it stands
class Library:
    def __init__(self, conn):
        self.conn = conn
        self.artist_folders = {}
        for (folder,) in conn.execute(
                "SELECT DISTINCT folder FROM files WHERE kind='audio' AND src=?", (MAIN,)):
            parts = folder.split('/')
            if len(parts) >= 2:
                self.artist_folders.setdefault(merge.norm_artist(parts[1]), parts[1])
        self.by_title = collections.defaultdict(list)
        for r in conn.execute("SELECT path,folder,filename,title,artist,albumartist,album,bitrate,ext,fp,duration "
                              "FROM files WHERE kind='audio' AND fp IS NOT NULL"):
            t = merge.norm_title(r[3] or os.path.splitext(r[2])[0])
            if t:
                self.by_title[t].append(r)
        self._fp = {}
        # chart albums: (artist, album) -> release group id, from the Billboard work
        self.charts = {}
        tsv = os.path.join(CHARTS, 'billboard-all-distinct-albums.tsv')
        if os.path.exists(tsv):
            for line in open(tsv, encoding='utf-8').read().splitlines()[1:]:
                f = line.split('\t')
                if len(f) >= 3 and f[0]:
                    self.charts[(merge.norm_artist(f[2]), merge.norm_album_safe(f[1]))] = f[0]

    def fp(self, row):
        p = row[0]
        if p not in self._fp:
            self._fp[p] = musiclib.unpack_fp(row[9])
        return self._fp[p]

    def duplicates(self, title, fp_vec):
        """Library tracks whose audio matches: [(distance, row)]."""
        out = []
        for r in self.by_title.get(merge.norm_title(title), []):
            d = merge.fp_distance(fp_vec, self.fp(r))
            if d < merge.FP_SAME:
                out.append((d, r))
        return sorted(out, key=lambda x: x[0])


# ------------------------------------------------------------------ reading an album
def read_album(artist_dir, album_dir):
    """Every audio file under one staging album with the tags it has and the
    values the path implies. Returns (info, tracks)."""
    apath = os.path.join(STAGING, artist_dir, album_dir)
    m = FOLDER_RE.match(album_dir)
    info = {'artist_dir': artist_dir, 'album_dir': album_dir,
            'album': (m.group('album') if m else album_dir).strip(), 'year': m.group('year') if m else None,
            'various': merge.norm_artist(artist_dir) in ('variousartists', 'various', 'va')}
    tracks = []
    subs = sorted(d for d in os.listdir(apath) if os.path.isdir(os.path.join(apath, d)))
    walk = [(apath, None, 0)] + [(os.path.join(apath, s), s, i) for i, s in enumerate(subs)]
    for folder, sub, idx in walk:
        for fn in sorted(os.listdir(folder)):
            full = os.path.join(folder, fn)
            ext = os.path.splitext(fn)[1].lower()
            if not os.path.isfile(full) or ext not in AUDIO:
                continue
            t = musiclib.read_tags(full, ext)
            t['full'] = full; t['ext'] = ext; t['sub'] = sub
            stem = os.path.splitext(fn)[0]
            fm = FILE_RE.match(stem) or NUM_RE.match(stem)
            t['fn_track'] = int(fm.group('track')) if fm else None
            t['fn_title'] = fm.group('title').strip() if fm else stem
            t['fn_artist'] = fm.group('artist').strip() if fm and 'artist' in fm.groupdict() else None
            t['disc_inferred'] = disc_number(sub, idx) if sub else None
            tracks.append(t)
    info['n'] = len(tracks)
    info['multidisc'] = len(subs) > 1 or any(t.get('disc') and t['disc'] > 1 for t in tracks)
    return info, tracks


def settle(info, tracks, lib):
    """Decide the tags each file should carry. Records what changed and why."""
    folder_artist = canonize.clean_name(info['artist_dir'])
    folder_artist = ALIASES.get(merge.norm_artist(folder_artist), folder_artist)
    if info['various']:
        albumartist = VARIOUS
        dest_artist = None
    else:
        albumartist = lib.artist_folders.get(nart(folder_artist), folder_artist)
        dest_artist = albumartist
    # album title: the folder's, edition suffixes and all reissue titles dropped
    album = canonize.clean_name(info['album'])
    year = info['year']
    # pool
    name_blob = f"{info['album_dir']} {' '.join(t.get('album') or '' for t in tracks[:3])}"
    if CHRISTMAS.search(name_blob):
        pool = 'Christmas'
    elif info['various']:
        pool = 'Soundtracks' if SOUNDTRACK.search(name_blob) else 'Compilations'
    else:
        pool = MAIN
    if pool == MAIN:
        dest_folder = f'{MAIN}/{sanitize(dest_artist)}/{sanitize(album)}'
    else:
        dest_folder = f'{pool}/{sanitize(album)}' if info['various'] else f'{pool}/{sanitize(dest_artist)} - {sanitize(album)}'
    rgid = lib.charts.get((merge.norm_artist(albumartist), merge.norm_album_safe(album)))
    info.update(albumartist=albumartist, album_title=album, year_final=year, pool=pool,
                dest_folder=dest_folder, rgid=rgid)

    seen_numbers = collections.Counter()
    for t in tracks:
        want = {}
        want['albumartist'] = albumartist
        want['album'] = album
        if year:
            want['date'] = year
        # artist: keep a real credit, fill a blank from the folder
        if not t.get('artist'):
            want['artist'] = t['fn_artist'] or (albumartist if not info['various'] else None)
            t['inferred_artist'] = True
        elif merge.norm_artist(t['artist']) in ALIASES:
            want['artist'] = ALIASES[merge.norm_artist(t['artist'])]
        # title
        if not t.get('title'):
            want['title'] = t['fn_title']
            t['inferred_title'] = True
        # track number
        if not t.get('track') and t['fn_track']:
            want['track'] = t['fn_track']
        # disc: the subfolder decides when the tag is silent
        if info['multidisc']:
            d = t.get('disc') or t['disc_inferred'] or 1
            if not t.get('disc') or (t['disc_inferred'] and t['disc'] != t['disc_inferred']):
                want['disc'] = t['disc_inferred'] or d
            t['disc_final'] = want.get('disc', t.get('disc') or 1)
        else:
            t['disc_final'] = 1
            if t.get('disc') and t['disc'] != 1:
                want['disc'] = 1
        t['track_final'] = want.get('track', t.get('track'))
        t['title_final'] = want.get('title', t.get('title'))
        t['artist_final'] = want.get('artist', t.get('artist'))
        seen_numbers[(t['disc_final'], t['track_final'])] += 1
        # drop no-op writes
        cur = {'albumartist': t.get('albumartist'), 'album': t.get('album'), 'date': (t.get('year') or '')[:4],
               'artist': t.get('artist'), 'title': t.get('title'), 'track': t.get('track'), 'disc': t.get('disc')}
        t['changes'] = {k: v for k, v in want.items() if v is not None and str(cur.get(k) or '') != str(v)}
        if rgid:
            t['changes']['musicbrainz_releasegroupid'] = rgid
        # destination file name
        tn = t['track_final']
        base = f"{tn:02d} - {sanitize(t['title_final'])}" if tn else sanitize(t['title_final'])
        sub = f"Disc {t['disc_final']}/" if info['multidisc'] else ''
        t['dest'] = f"{dest_folder}/{sub}{base}{t['ext']}"
        # a folder artist tag that names someone else entirely is worth a look
        t['artist_mismatch'] = bool(t.get('artist')) and not info['various'] and \
            nart(albumartist) not in nart(t['artist']) and \
            nart(t['artist']) not in nart(albumartist)
    info['numbering_clash'] = sum(1 for v in seen_numbers.values() if v > 1)
    return info, tracks


# ------------------------------------------------------------------ audio checks
def _fp_abs(full):
    rel, fp, dur, err = musiclib._fp_one(full)
    return full, fp, dur, err


def fingerprint_all(tracks, workers=8):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for full, fp, dur, err in ex.map(_fp_abs, [t['full'] for t in tracks]):
            for t in tracks:
                if t['full'] == full:
                    t['fp'], t['fp_dur'], t['fp_err'] = fp, dur, err


def check_duplicates(tracks, lib, info):
    folders = collections.Counter()
    for t in tracks:
        t['dupes'] = []
        if not t.get('fp'):
            continue
        vec = musiclib.unpack_fp(t['fp'])
        for d, r in lib.duplicates(t['title_final'] or '', vec):
            same_album = merge.norm_album_safe(r[6] or r[1].split('/')[-1]) == merge.norm_album_safe(info['album_title']) \
                and merge.norm_artist(r[5] or r[4] or '') == merge.norm_artist(info['albumartist'])
            t['dupes'].append({'path': r[0], 'distance': round(d, 3), 'bitrate': r[7], 'same_album': same_album})
            if same_album:
                folders[re.sub(r'/(?:disc|cd|disk)\s*\d+$', '', r[1], flags=re.I)] += 1
    if folders:
        # the library already has this album: anything we keep joins it there
        existing = folders.most_common(1)[0][0]
        info['existing_folder'] = existing
        for t in tracks:
            t['dest'] = existing + t['dest'][len(info['dest_folder']):]
        info['dest_folder'] = existing


def acoustid_verify(tracks, info, acdb):
    """Ask AcoustID about files whose tags were inferred or disagree with the folder."""
    need = [t for t in tracks if t.get('inferred_artist') or t.get('inferred_title') or t.get('artist_mismatch')]
    if not need:
        info['acoustid'] = {'asked': 0}
        return
    k = acoustid.key()
    agree = disagree = unknown = 0
    for t in need:
        row = acdb.execute('SELECT score,artist,title FROM ident WHERE path=?', (t['full'],)).fetchone()
        if not row:
            fp, dur = acoustid.fingerprint(t['full'])
            b = acoustid.best(acoustid.lookup(fp, dur, k)) if fp and dur else None
            if b:
                acdb.execute('INSERT OR REPLACE INTO ident (path,score,acoustid,rec_id,artist,title,album,ts) '
                             'VALUES (?,?,?,?,?,?,?,?)', (t['full'], b['score'], b['acoustid'], b['rec_id'],
                                                         b['artist'], b['title'], b['album'], time.time()))
            else:
                acdb.execute('INSERT OR REPLACE INTO ident (path,ts,err) VALUES (?,?,?)', (t['full'], time.time(), 'no match'))
            acdb.commit()
            time.sleep(acoustid.RATE)
            row = (b['score'], b['artist'], b['title']) if b else (None, None, None)
        score, a_artist, a_title = row
        t['acoustid'] = {'score': score, 'artist': a_artist, 'title': a_title}
        if not score or score < ACOUSTID_MIN:
            unknown += 1; continue
        want = nart(info['albumartist'])
        got = nart(a_artist or '')
        credited = nart(t.get('artist_final') or '')
        if want in got or got in want or (credited and (credited in got or got in credited)) or info['various']:
            agree += 1
        else:
            disagree += 1
            t['acoustid_disagree'] = True
    info['acoustid'] = {'asked': len(need), 'agree': agree, 'disagree': disagree, 'unknown': unknown}
    info['blocked'] = bool(agree + disagree) and disagree / (agree + disagree) > DISAGREE_MAX


# ------------------------------------------------------------------ writing
def write_tags(t, dest_rel, conn):
    """Apply t['changes'] to the file (still in staging), logging old values against the destination path."""
    full = t['full']
    a = mutagen.File(full, easy=True)
    if a is None:
        return 0
    if a.tags is None:
        a.add_tags()
    n = 0
    for field, value in t['changes'].items():
        if field == 'musicbrainz_releasegroupid':
            continue
        old = (a.tags.get(field) or [None])[0]
        if field == 'track':
            key = 'tracknumber'
            old = (a.tags.get(key) or [None])[0]
            tot = t.get('tracktotal')
            a.tags[key] = [f'{value}/{tot}' if tot else str(value)]
        elif field == 'disc':
            key = 'discnumber'
            old = (a.tags.get(key) or [None])[0]
            a.tags[key] = [str(value)]
        else:
            a.tags[field] = [str(value)]
        conn.execute('INSERT INTO tag_backup (ts,op,path,field,old,new) VALUES (?,?,?,?,?,?)',
                     (time.time(), OP, dest_rel, field, None if old is None else str(old), str(value)))
        n += 1
    a.save()
    rg = t['changes'].get('musicbrainz_releasegroupid')
    if rg:
        raw = mutagen.File(full)
        if isinstance(raw, mutagen.mp4.MP4):
            raw['----:com.apple.iTunes:MusicBrainz Release Group Id'] = [rg.encode()]
        elif raw.tags is not None and hasattr(raw.tags, 'delall'):
            raw.tags.delall('TXXX:MusicBrainz Release Group Id')
            raw.tags.add(mutagen.id3.TXXX(encoding=3, desc='MusicBrainz Release Group Id', text=[rg]))
        raw.save()
        conn.execute('INSERT INTO tag_backup (ts,op,path,field,old,new) VALUES (?,?,?,?,?,?)',
                     (time.time(), OP, dest_rel, 'musicbrainz_releasegroupid', None, rg))
        n += 1
    return n


def find_art(info, tracks, acdb):
    """Full-size cover as PIL Image plus where it came from, or (None, reason)."""
    from PIL import Image
    best, bw = None, 0
    for t in tracks:
        try:
            a = mutagen.File(t['full'])
            data = None
            if isinstance(a, mutagen.mp4.MP4) and a.tags and 'covr' in a.tags:
                data = bytes(a.tags['covr'][0])
            elif a is not None and a.tags is not None and hasattr(a.tags, 'getall'):
                pics = a.tags.getall('APIC')
                data = pics[0].data if pics else None
            if data:
                im = Image.open(io.BytesIO(data)); im.load()
                if max(im.size) > bw:
                    best, bw = im.convert('RGB'), max(im.size)
        except Exception:
            pass
    if best is not None and bw >= 500:
        return best, f'embedded {bw}px'
    im = None
    if info.get('rgid'):
        try:
            im = fixart.download_front(info['rgid'])
        except Exception:
            im = None
        if im is not None:
            return im, f'coverartarchive rg {info["rgid"]}'
    if not info['various']:
        try:
            time.sleep(1.1)
            cands = fixart.mb_release_groups(info['albumartist'], info['album_title'])
            pick = fixart.pick(cands, info['albumartist'], info['album_title'])
            if pick:
                im = fixart.download_front(pick['id'])
                if im is not None:
                    return im, f'coverartarchive search {pick["id"]}'
        except Exception:
            pass
    if best is not None:
        return best, f'embedded {bw}px (small)'
    return None, 'no art found'


def cache_art(acdb, folder, im, source):
    """Put the full cover in the art cache under the destination folder's key, for etl."""
    local = folder.replace('/', '__') + '.jpg'
    path = os.path.join(artfetch.ARTDIR, local)
    os.makedirs(artfetch.ARTDIR, exist_ok=True)
    im.save(path, 'JPEG', quality=92)
    acdb.execute('DELETE FROM art WHERE folder=?', (folder,))
    acdb.execute('INSERT INTO art (folder,cur_w,cur_src,want,hit,score,mb_album,mb_artist,url,local,new_w,reason,ts) '
                 'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (folder, 0, 'none', 1, 1, 1.0, None, None, source, local, max(im.size), None, time.time()))
    acdb.commit()
    return path


def move_in(t, conn, dest_rel):
    dst = os.path.join(ROOT, dest_rel.replace('/', os.sep))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        b, e = os.path.splitext(dst); i = 2
        while os.path.exists(f'{b}__{i}{e}'): i += 1
        dst = f'{b}__{i}{e}'
        dest_rel = os.path.relpath(dst, ROOT).replace('\\', '/')
    size = os.path.getsize(t['full'])
    shutil.move(t['full'], dst)
    conn.execute('INSERT INTO moves (ts,op,reason,src,dst,size) VALUES (?,?,?,?,?,?)',
                 (time.time(), OP, 'ingest', t['full'].replace('\\', '/'), dest_rel, size))
    return dest_rel


def supersede(t, conn, info):
    dst_rel = f"{musiclib.SUPERSEDED}/incoming/{sanitize(info['albumartist'])}/{sanitize(info['album_title'])}/{os.path.basename(t['full'])}"
    dst = os.path.join(ROOT, dst_rel.replace('/', os.sep))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        b, e = os.path.splitext(dst); i = 2
        while os.path.exists(f'{b}__{i}{e}'): i += 1
        dst = f'{b}__{i}{e}'; dst_rel = os.path.relpath(dst, ROOT).replace('\\', '/')
    size = os.path.getsize(t['full'])
    shutil.move(t['full'], dst)
    conn.execute('INSERT INTO moves (ts,op,reason,src,dst,size) VALUES (?,?,?,?,?,?)',
                 (time.time(), OP, 'dupe-of ' + t['dupes'][0]['path'], t['full'].replace('\\', '/'), dst_rel, size))
    return dst_rel


def register(conn, dest_rel, t):
    full = os.path.join(ROOT, dest_rel.replace('/', os.sep))
    st = os.stat(full)
    rec = dict(path=dest_rel, src=dest_rel.split('/')[0], kind='audio', folder=os.path.dirname(dest_rel),
               filename=os.path.basename(dest_rel), ext=t['ext'], size=st.st_size, mtime=st.st_mtime,
               scanned_at=time.time())
    rec.update(musiclib.read_tags(full, t['ext']))
    musiclib._flush(conn, [rec])
    conn.execute('UPDATE files SET fp=?, fp_dur=?, fp_err=? WHERE path=?', (t.get('fp'), t.get('fp_dur'), t.get('fp_err'), dest_rel))


def remove_source(info):
    apath = os.path.join(STAGING, info['artist_dir'], info['album_dir'])
    left = [os.path.join(dp, f) for dp, _, fs in os.walk(apath) for f in fs]
    if left:
        return f'{len(left)} files left behind'
    shutil.rmtree(apath, ignore_errors=True)
    art = os.path.join(STAGING, info['artist_dir'])
    if os.path.isdir(art) and not os.listdir(art):
        os.rmdir(art)
        return 'album and artist folder removed'
    return 'album folder removed'


# ------------------------------------------------------------------ main
def album_dirs(only_artist=None):
    out = []
    for a in sorted(os.listdir(STAGING)):
        ap = os.path.join(STAGING, a)
        if not os.path.isdir(ap) or (only_artist and merge.norm_artist(a) != merge.norm_artist(only_artist)):
            continue
        for b in sorted(os.listdir(ap)):
            if os.path.isdir(os.path.join(ap, b)):
                out.append((a, b))
    return out


def summarize(info, tracks):
    ch = collections.Counter(k for t in tracks for k in t['changes'])
    dup_same = sum(1 for t in tracks if any(d['same_album'] for d in t.get('dupes', [])))
    dup_other = sum(1 for t in tracks if t.get('dupes') and not any(d['same_album'] for d in t['dupes']))
    return {'artist': info['artist_dir'], 'album': info['album_dir'], 'tracks': info['n'], 'pool': info['pool'],
            'dest': info['dest_folder'], 'albumartist': info['albumartist'], 'year': info['year_final'],
            'rgid': info.get('rgid'), 'multidisc': info['multidisc'], 'numbering_clash': info['numbering_clash'],
            'tag_changes': dict(ch), 'inferred_titles': sum(1 for t in tracks if t.get('inferred_title')),
            'inferred_artists': sum(1 for t in tracks if t.get('inferred_artist')),
            'artist_mismatch': sum(1 for t in tracks if t.get('artist_mismatch')),
            'dupes_same_album': dup_same, 'dupes_elsewhere': dup_other,
            'acoustid': info.get('acoustid'), 'blocked': info.get('blocked', False),
            'art': info.get('art_source'), 'fp_errors': sum(1 for t in tracks if t.get('fp_err'))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--artist')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--skip-acoustid', action='store_true')
    ap.add_argument('--force', action='store_true', help='ingest even when AcoustID disagrees (use with --artist after review)')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--skip-pool', action='append', default=[], help='leave albums bound for this pool in staging (e.g. Christmas)')
    a = ap.parse_args()

    conn = musiclib.db()
    conn.executescript(tagmod.BACKUP_SCHEMA)
    acdb = artfetch.cache_db()
    iddb = acoustid.cache_db()
    lib = Library(conn)
    print(f'library: {len(lib.artist_folders)} artist folders, {sum(len(v) for v in lib.by_title.values())} fingerprinted tracks, '
          f'{len(lib.charts)} chart albums with release groups', flush=True)
    if a.apply:
        snap = os.path.join(HERE, 'library.db.pre-ingest')
        if not os.path.exists(snap):
            conn.commit(); shutil.copy(os.path.join(HERE, 'library.db'), snap); print('snapshot', snap)

    todo = album_dirs(a.artist)
    if a.limit:
        todo = todo[:a.limit]
    print(f'{len(todo)} albums to process ({"APPLY" if a.apply else "DRY RUN"})', flush=True)
    plan, report = [], []
    t0 = time.time()
    for i, (artist_dir, album_dir) in enumerate(todo, 1):
        info, tracks = read_album(artist_dir, album_dir)
        if not tracks:
            report.append(f'SKIP {artist_dir} / {album_dir}: no audio files'); continue
        info, tracks = settle(info, tracks, lib)
        if info['pool'] in a.skip_pool:
            line = f'[{i}/{len(todo)}] {artist_dir} / {album_dir} -> {info["dest_folder"]} HELD (pool {info["pool"]} skipped)'
            print(line, flush=True); report.append(line); continue
        fingerprint_all(tracks, a.workers)
        check_duplicates(tracks, lib, info)
        if not a.skip_acoustid:
            acoustid_verify(tracks, info, iddb)
        s = summarize(info, tracks)
        plan.append(s)
        flags = []
        if s['blocked']: flags.append('BLOCKED acoustid disagrees')
        if s['dupes_same_album']: flags.append(f"{s['dupes_same_album']}/{s['tracks']} already in library at {info.get('existing_folder')}")
        if s['dupes_elsewhere']: flags.append(f"{s['dupes_elsewhere']} dup elsewhere")
        if s['numbering_clash']: flags.append(f"{s['numbering_clash']} numbering clashes")
        if s['artist_mismatch']: flags.append(f"{s['artist_mismatch']} artist-tag mismatches")
        line = f"[{i}/{len(todo)}] {artist_dir} / {album_dir} -> {info['dest_folder']} ({info['n']} tracks" + \
               (f", {len(set(t['disc_final'] for t in tracks))} discs" if info['multidisc'] else '') + ')' + \
               (' ' + '; '.join(flags) if flags else '')
        print(line, flush=True)
        report.append(line)
        for t in tracks:
            if t.get('acoustid_disagree'):
                report.append(f"     acoustid says {t['acoustid']['artist']} - {t['acoustid']['title']} for {os.path.basename(t['full'])}")
        if not a.apply or (info.get('blocked') and not a.force):
            continue

        # ---- apply: decide each track first
        keep, drop, replace = [], [], []
        for t in tracks:
            same = [d for d in t.get('dupes', []) if d['same_album']]
            if same:
                # the library already holds this recording in this album: keep the better copy
                lib_br = same[0]['bitrate'] or 0
                if (t.get('bitrate') or 0) <= lib_br * 1.25:
                    drop.append(t)
                else:
                    replace.append((t, same[0]['path'])); keep.append(t)
            else:
                keep.append(t)
        source = 'not needed'
        jpeg = None
        if keep and not info.get('existing_folder'):
            im, source = find_art(info, tracks, acdb)
            info['art_source'] = source
            if im is not None:
                cache_art(acdb, info['dest_folder'], im, source)
                buf = io.BytesIO(); im2 = im.copy()
                if max(im2.size) > embedart.EMBED_PX:
                    from PIL import Image
                    im2.thumbnail((embedart.EMBED_PX, embedart.EMBED_PX), Image.LANCZOS)
                im2.save(buf, 'JPEG', quality=embedart.QUALITY, optimize=True, progressive=True)
                jpeg = buf.getvalue()
        elif keep and info.get('existing_folder'):
            # joining an album the library already has: wear its cover
            src_path, w, kind = embedart.best_source(conn, acdb, info['existing_folder'])
            if src_path:
                jpeg = embedart.render(src_path); source = f'existing album cover ({kind} {w}px)'
            else:
                for (rel,) in conn.execute('SELECT path FROM files WHERE folder=? AND kind="audio" AND has_art>0 LIMIT 1',
                                           (info['existing_folder'],)):
                    a_ = mutagen.File(os.path.join(ROOT, rel.replace('/', os.sep)))
                    data = None
                    if isinstance(a_, mutagen.mp4.MP4) and a_.tags and 'covr' in a_.tags:
                        data = bytes(a_.tags['covr'][0])
                    elif a_ is not None and a_.tags is not None and hasattr(a_.tags, 'getall'):
                        pics = a_.tags.getall('APIC'); data = pics[0].data if pics else None
                    if data:
                        from PIL import Image
                        im_ = Image.open(io.BytesIO(data)).convert('RGB')
                        if max(im_.size) > embedart.EMBED_PX:
                            im_.thumbnail((embedart.EMBED_PX, embedart.EMBED_PX), Image.LANCZOS)
                        buf = io.BytesIO(); im_.save(buf, 'JPEG', quality=embedart.QUALITY, optimize=True, progressive=True)
                        jpeg = buf.getvalue(); source = 'existing album cover (embedded)'
        s['art'] = source
        for t, lib_path in replace:
            # the library's copy loses to the incoming one: retire it, logged and undoable
            try:
                musiclib._move(conn, lib_path, musiclib.SUPERSEDED, OP, 'replaced by incoming ' + t['dest'], False)
            except OSError as e:
                report.append(f'     !! could not retire {lib_path}: {e}')
        moved = superseded = 0
        for t in drop:
            supersede(t, conn, info); superseded += 1
        for t in keep:
            write_tags(t, t['dest'], conn)
            if jpeg and not (t.get('has_art') and (t.get('art_bytes') or 0) > 30000):
                embedart.set_art(t['full'], jpeg)
            dest_rel = move_in(t, conn, t['dest'])
            register(conn, dest_rel, t)
            moved += 1
        conn.commit()
        outcome = remove_source(info)
        msg = f'     moved {moved}, superseded {superseded}, replaced {len(replace)} library copies, art: {source}; {outcome}'
        report.append(msg); print(msg, flush=True)

    json.dump(plan, open(PLAN, 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    open(REPORT, 'w', encoding='utf-8').write('\n'.join(report) + '\n')
    n_block = sum(1 for p in plan if p['blocked'])
    print(f'\n{len(plan)} albums planned, {n_block} blocked, {time.time()-t0:.0f}s. Plan: {PLAN}, report: {REPORT}')
    conn.close(); acdb.close(); iddb.close()


if __name__ == '__main__':
    main()
