"""Tidy the CD+G karaoke store at D:\\Music\\Karaoke into one layout.

The folder arrived as a dozen collections in a dozen naming styles: zips
holding a .cdg and an .mp3, loose .cdg/.mp3 pairs, loose .cdg/.wma pairs,
"Artist - Title - SF 122 -16", "SFG052-15 - Presley, Elvis - Title",
"SFD9013-15 - Title - Artist, The", "artist-title", "01 - Title - Artist",
"Title [MM Karaoke]" and plain titles with no artist at all. After this run
every song is two files, an MP3 and a CDG, named the same way:

    D:\\Music\\Karaoke\\<Artist>\\<Artist> - <Title> [<disc code>].mp3
    D:\\Music\\Karaoke\\<Artist>\\<Artist> - <Title> [<disc code>].cdg

The disc code (SF246-08, SFG052-15, FLY009-05, QH1004MC-08 ...) keeps the
provenance and tells apart the same song from two discs; producer tags such
as [SF Karaoke] become the code SF, ME, MM. Songs whose artist could not be
settled go to `_unsorted`, graphics or audio without their partner to
`_orphans`, track lists, covers and disc images to `_source` (original
relative path kept), and torrent notes, desktop.ini and Thumbs.db are removed.

Which side of "A - B" is the artist is decided with the music library's
own knowledge: the artist names and track titles in jukebox.db. A side that
is a known artist (or reads "Surname, First" / "X, The") is the artist; a
side that is a known title is the title; a disc folder's undecided files
follow the majority order of its decided ones; the last resort is the most
common order, artist first. A title with no artist is looked up among the
library's tracks and takes the artist when the title is unambiguous.

Zips are extracted straight to their destination names and deleted once
both members have been written at the recorded size. WMA (and the two WAV)
backing tracks are transcoded to MP3 with ffmpeg and the originals moved to
D:\\Music\\_superseded\\Karaoke. Every action is written to
library/karaoke_tidy.json (plan) and karaoke_tidy_report.txt.

    python karaoke_tidy.py             # dry run: plan and report only
    python karaoke_tidy.py --apply
    python karaoke_tidy.py --folder "Karaoke Q-Z" --apply
"""
import os, re, sys, json, time, shutil, sqlite3, zipfile, argparse, collections, subprocess, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')

_MUSIC = os.environ.get('MUSICMATE_ROOT') or r'D:\Music'
# The store moved to C:\Music\Karaoke on 8 Oct 2026; one inside the music folder
# still wins. Superseded files go beside the store, on its own drive.
ROOT = os.environ.get('JUKEBOX_CDG') or os.path.join(_MUSIC, 'Karaoke')
if not os.environ.get('JUKEBOX_CDG') and not os.path.isdir(ROOT) and os.path.isdir(r'C:\Music\Karaoke'):
    ROOT = r'C:\Music\Karaoke'
SUPERSEDED = os.path.join(os.path.dirname(ROOT), '_superseded', 'Karaoke')
JUKEBOX_DB = os.path.join(HERE, '..', 'jukebox', 'jukebox.db')
PLAN = os.path.join(HERE, 'karaoke_tidy.json')
REPORT = os.path.join(HERE, 'karaoke_tidy_report.txt')
AUDIO = {'.mp3', '.wma', '.wav', '.mp2'}
JUNK = re.compile(r'(?i)^(torrent.*\.txt|desktop\.ini|thumbs\.db)$')
SPECIAL = ('_unsorted', '_orphans', '_source')

# ------------------------------------------------------------------ text helpers
def norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'\(.*?\)|\[.*?\]', '', s)
    s = re.sub(r'\b(feat|ft|featuring)\b.*$', '', s)
    s = re.sub(r'^\s*the\s+|,\s*the\s*$', '', s)
    s = s.replace('&', ' and ')
    return re.sub(r'[^a-z0-9]', '', s)


def sanitize(name):
    ILLEGAL = {'<': '(', '>': ')', ':': ' -', '"': "'", '/': '-', '\\': '-', '|': '-', '?': '', '*': ''}
    s = ''.join(ILLEGAL.get(ch, ch) for ch in (name or '') if ord(ch) >= 32)
    return re.sub(r'\s+', ' ', s).strip().rstrip('. ') or 'Unknown'


SMALL = {'a', 'an', 'the', 'and', 'or', 'of', 'to', 'in', 'on', 'at', 'for', 'by', 'with', 'from', 'but', 'nor', 'as', 'vs', 'feat', 'ft'}


def title_case(s):
    """For names that arrived ALL CAPS or all lower. Mixed case is left alone."""
    if s != s.upper() and s != s.lower():
        return s
    words = s.lower().split()
    out = []
    for i, w in enumerate(words):
        if i and w in SMALL:
            out.append(w)
        else:
            out.append(re.sub(r"(^|[-'(/])([a-z])", lambda m: m.group(1) + m.group(2).upper(), w))
    return ' '.join(out)


# names whose comma is part of the name, not a catalogue "Surname, First"
KEEP_COMMA = {norm(x) for x in ('10,000 Maniacs', 'Crosby, Stills & Nash', 'Crosby, Stills, Nash & Young',
              'Earth, Wind & Fire', 'Emerson, Lake & Palmer', 'Harry Connick, Jr.', 'Tyler, The Creator',
              'Lipps, Inc', 'Blood, Sweat & Tears', 'Peter, Paul & Mary', 'Hamilton, Joe Frank & Reynolds')}


def reorder_comma(a):
    """Surname, First -> First Surname; X, The -> The X. Real comma names kept."""
    if ',' not in a or norm(a) in KEEP_COMMA:
        return a
    m = re.match(r'^(.+?),\s*(The)$', a, re.I)
    if m:
        return 'The ' + m.group(1)
    m = re.match(r"^([A-Z][\w'.-]+),\s*([A-Z][\w'.]+(?: [A-Z][\w'.]+)?)(\s*(?:&|and|feat\.?|ft\.?)\s+.+)?$", a, re.I)
    if m and ' ' not in m.group(1):
        return m.group(2).strip() + ' ' + m.group(1) + (m.group(3) or '')
    return a


def fix_artist(a, know=None):
    """Tidy spacing and case, and put a catalogue-order name the right way round."""
    a = re.sub(r'\s+', ' ', a).strip(' -_')
    return title_case(reorder_comma(a))


def hyphen_split(text, know):
    """'artist-title' with no spaced dash: try every hyphen, prefer the split
    the library recognises; else the first hyphen, artist first."""
    best, bscore = None, -1
    for m in re.finditer(r'-', text):
        i = m.start()
        left, right = text[:i].strip(), text[i + 1:].strip()
        if not left or not right or left.count('(') != left.count(')'):
            continue
        sc = (3 if know.is_artist(left) else 0) + (2 if know.is_title(right) else 0)
        rsc = (3 if know.is_artist(right) else 0) + (2 if know.is_title(left) else 0)
        if rsc > sc and rsc > bscore:
            best, bscore = (right, left), rsc
        elif sc > bscore:
            best, bscore = (left, right), sc
    return best


def fix_title(t):
    t = re.sub(r'\s+', ' ', t).strip(' -_')
    m = re.match(r'^(.+?),\s*(The|A)$', t, re.I)
    if m:
        t = m.group(2).title() + ' ' + m.group(1)
    return title_case(t)


# ------------------------------------------------------------------ what the library knows
class Knowledge:
    def __init__(self):
        self.artists = {}          # norm -> display name
        self.titles = collections.defaultdict(collections.Counter)   # norm title -> Counter(artist)
        c = sqlite3.connect(JUKEBOX_DB)
        for (n,) in c.execute('SELECT name FROM artist'):
            self.artists.setdefault(norm(n), n)
        for t, a in c.execute("SELECT track.title, COALESCE(NULLIF(track.track_artist,''), artist.name) "
                              'FROM track JOIN album ON album.id=track.album_id JOIN artist ON artist.id=album.artist_id'):
            if t and a:
                self.titles[norm(t)][a] += 1
                self.artists.setdefault(norm(a), a)
        c.close()

    def is_artist(self, s):
        return norm(s) in self.artists

    def is_title(self, s):
        # a bare number ("911", "411") is a band name far more often than a title
        return norm(s) in self.titles and not re.fullmatch(r'\d+', (s or '').strip())

    def canonical(self, a):
        return self.artists.get(norm(a), a)

    def artist_for_title(self, t):
        c = self.titles.get(norm(t))
        if not c:
            return None
        (a, n), = c.most_common(1)
        if len(c) == 1 or n >= 2 * sum(c.values()) / 3:
            return a
        return None


# ------------------------------------------------------------------ names
LABEL_TAG = re.compile(r'\s*\[([A-Za-z]{1,4})\s*Karaoke\]\s*', re.I)
# disc codes as they appear in file and folder names
CODE_RES = [
    re.compile(r'(?i)\b(SFGD?|SFMW|SFD|SFTPB|SFTP|SFABBA|SFCELT|SFSCOT|SFWC|SF)\s*(\d{1,4})\s*-\s*(\d{1,2})\b'),
    re.compile(r'(?i)\b(FLY|MRH)\s*(\d{2,4})\s*-\s*(\d{1,2})\b'),
    re.compile(r'(?i)\b(QH-?[A-Z0-9]*\d[A-Z0-9]*(?:-\d{2})?)\s*-\s*(\d{1,2})\b'),
    re.compile(r'(?i)\b(SFMEAT|SFTJ|SFSB)\s*-\s*(\d{1,2})\b'),
]
FOLDER_CODE = re.compile(r'(?i)^(SFGD?|SFMW|SFTPB|SFTP|SF|FLY|MRH|QH[A-Z0-9-]+?)\s*[- ]?\s*(\d{1,4})\b')


def code_from(text):
    """Return (code, remaining text) or (None, text)."""
    for rx in CODE_RES:
        m = rx.search(text)
        if m:
            g = m.groups()
            if len(g) == 3:
                code = f'{g[0].upper()}{int(g[1]):0{3 if g[0].upper() in ("SF", "SFG", "SFGD", "SFMW", "FLY", "MRH", "SFCELT", "SFSCOT", "SFABBA") else len(g[1])}d}-{int(g[2]):02d}'
            else:
                code = f'{g[0].upper().replace("QH-", "QH")}-{int(g[1]):02d}'
            rest = (text[:m.start()] + ' ' + text[m.end():])
            return code, rest
    return None, text


def split_sides(rest, track_no=None):
    """track_no: the disc's track number when the code came from the folder, so a
    leading number is dropped only when it IS that number (or no code was seen);
    "911 - All I Want Is You" keeps its artist."""
    rest = LABEL_TAG.sub(' ', rest)
    rest = rest.replace('_', ' ').strip()
    m = re.match(r'^\s*(\d{1,3})\s*[-.]\s*', rest)
    if m and (track_no is None or int(m.group(1)) == track_no):
        rest = rest[m.end():]                                # leading track number
    rest = re.sub(r'^\s*SF\s*\d{1,2}\s+', '', rest)          # "SF 08 YOU HAVE BEEN LOVED"
    if re.search(r'\S {2,}\S', rest) and ' - ' not in rest:  # "Frank Sinatra   All Of Me"
        return [p.strip() for p in re.split(r' {2,}', rest.strip(' -')) if p.strip()]
    rest = re.sub(r'\s+', ' ', rest).strip(' -')
    if ' - ' in rest:
        parts = [p.strip() for p in rest.split(' - ') if p.strip()]
    elif re.search(r'\S -\S|\S- \S', rest):                  # "Duran Duran -Come Undone"
        parts = [p.strip() for p in re.split(r' -|- ', rest, 1) if p.strip()]
    elif re.fullmatch(r"[a-z0-9'&,.! ]+-[a-z0-9'&,.! ]+", rest):   # lowercase "artist-title"
        parts = [p.strip() for p in rest.split('-', 1)]
    else:
        parts = [rest]
    return parts


def label_tag(text):
    m = LABEL_TAG.search(text)
    return m.group(1).upper() if m else None


# ------------------------------------------------------------------ units
class Unit:
    def __init__(self, kind, folder, stem, files):
        self.kind = kind          # zip | pair | cdg-only | audio-only
        self.folder = folder      # relative folder
        self.stem = stem          # name without extension (zip stem or file stem)
        self.files = files        # dict ext -> absolute path (for zip: {'zip': path, 'members': {...}})
        self.artist = self.title = self.code = None
        self.method = ''
        self.sides = []


def collect(folder_filter=None):
    units, leftovers = [], []
    for dp, dns, fs in os.walk(ROOT):
        rel = os.path.relpath(dp, ROOT).replace('\\', '/')
        rel = '' if rel == '.' else rel
        top = rel.split('/')[0] if rel else ''
        if top in SPECIAL:
            dns[:] = []
            continue
        if folder_filter and not (rel == folder_filter or rel.startswith(folder_filter + '/')):
            continue
        stems = collections.defaultdict(dict)
        for f in fs:
            full = os.path.join(dp, f)
            b, e = os.path.splitext(f)
            e = e.lower()
            if e == '.zip':
                try:
                    with zipfile.ZipFile(full) as z:
                        members = {os.path.splitext(i.filename)[1].lower(): (i.filename, i.file_size) for i in z.infolist()}
                except Exception as ex:
                    leftovers.append((full, 'bad zip ' + str(ex)))
                    continue
                aud = next((x for x in members if x in AUDIO), None)
                if '.cdg' in members and aud:
                    units.append(Unit('zip', rel, b, {'zip': full, 'cdg': members['.cdg'], 'audio': members[aud], 'aext': aud}))
                elif '.cdg' in members or aud:
                    units.append(Unit('cdg-only' if '.cdg' in members else 'audio-only', rel, b, {'zip': full}))
                else:
                    leftovers.append((full, 'zip without song'))
            elif e == '.cdg' or e in AUDIO:
                stems[b][e] = full
            else:
                leftovers.append((full, 'junk' if JUNK.match(f) else 'source'))
        for b, d in stems.items():
            aud = next((x for x in d if x in AUDIO), None)
            if '.cdg' in d and aud:
                units.append(Unit('pair', rel, b, {'cdg': d['.cdg'], 'audio': d[aud], 'aext': aud}))
            elif '.cdg' in d:
                units.append(Unit('cdg-only', rel, b, {'cdg': d['.cdg']}))
            else:
                units.append(Unit('audio-only', rel, b, {'audio': d[aud], 'aext': aud}))
    return units, leftovers


# ------------------------------------------------------------------ deciding names
def tracklists():
    """disc code -> {track no: (artist or None, title)} from the discs' own text files."""
    out = {}
    rx = re.compile(r'^\s*(\d{1,2})\s*[-.]\s*(.+?)\s*$')
    for dp, _, fs in os.walk(ROOT):
        fm = FOLDER_CODE.match(os.path.basename(dp))
        if not fm:
            continue
        for f in fs:
            if not f.lower().endswith('.txt') or JUNK.match(f):
                continue
            code_base = None
            m = FOLDER_CODE.match(os.path.basename(dp))
            if m:
                code_base = f'{m.group(1).upper()}{int(m.group(2)):03d}'
            try:
                lines = open(os.path.join(dp, f), encoding='utf-8', errors='replace').read().splitlines()
            except OSError:
                continue
            for ln in lines:
                mm = rx.match(ln)
                if not mm or not code_base:
                    continue
                parts = [p.strip() for p in mm.group(2).split(' - ')]
                if len(parts) >= 2:
                    out[f'{code_base}-{int(mm.group(1)):02d}'] = (parts[0], ' - '.join(parts[1:]))
                else:
                    out[f'{code_base}-{int(mm.group(1)):02d}'] = (None, parts[0])
    return out


def wma_tags(path):
    try:
        import mutagen
        m = mutagen.File(path)
        t = m.tags or {}
        g = lambda k: str(t[k][0]).strip() if k in t and str(t[k][0]).strip() else None
        return g('Author') or g('WM/AlbumArtist'), g('Title')
    except Exception:
        return None, None


def decide(units, know):
    lists = tracklists()
    folder_votes = collections.defaultdict(collections.Counter)
    for u in units:
        text = u.stem
        code, rest = code_from(text)
        track_no = None
        if code:
            m = re.search(r'-(\d{2})$', code)
            track_no = int(m.group(1)) if m else None
        else:
            # the folder may carry the disc and the file only the track number
            fm = FOLDER_CODE.match(os.path.basename(u.folder))
            tn = re.match(r'^\s*(\d{1,3})\s*[-._ ]', text) or re.match(r'^\s*SF\s*(\d{1,2})\s+', text)
            if fm and tn:
                code = f'{fm.group(1).upper()}{int(fm.group(2)):03d}-{int(tn.group(1)):02d}'
                track_no = int(tn.group(1))
        tag = label_tag(text)
        if not code and tag:
            code = tag
        u.code = code
        sides = split_sides(rest, track_no)
        u.sides = sides
        # a known disc list settles it outright
        if code in lists and lists[code][0]:
            a, t = lists[code]
            u.artist, u.title, u.method = a, t, 'tracklist'
            continue
        if len(sides) >= 2:
            a_side, b_side = sides[0], ' - '.join(sides[1:])
            # "SFGD001 - Hear'Say", "QH1004MC - Miley Cyrus": the folder names the artist
            fh = re.match(r'^[A-Za-z0-9]+\s*-\s*(.+)$', os.path.basename(u.folder))
            hint = norm(fh.group(1)) if fh else None
            sa = (3 if know.is_artist(a_side) else 0) + (2 if re.search(r',\s*(The|[A-Z][a-z]+)$', a_side) else 0) + (2 if know.is_title(b_side) else 0) \
                + (5 if hint and norm(a_side) == hint else 0)
            sb = (3 if know.is_artist(b_side) else 0) + (2 if re.search(r',\s*(The|[A-Z][a-z]+)$', b_side) else 0) + (2 if know.is_title(a_side) else 0) \
                + (5 if hint and norm(b_side) == hint else 0)
            if sa > sb:
                u.artist, u.title, u.method = a_side, b_side, 'known'
                folder_votes[u.folder]['at'] += 1
            elif sb > sa:
                u.artist, u.title, u.method = b_side, a_side, 'known'
                folder_votes[u.folder]['ta'] += 1
            else:
                u.method = 'undecided'
        elif len(sides) == 1 and '-' in sides[0] and re.search(r'\w-\s?\w', sides[0]) and hyphen_split(sides[0], know):
            u.artist, u.title = hyphen_split(sides[0], know)
            u.method = 'hyphen'
        elif len(sides) == 1 and sides[0]:
            t = sides[0]
            a = None
            if u.kind == 'pair' and u.files.get('aext') == '.wma':
                a, t2 = wma_tags(u.files['audio'])
                if t2:
                    t = t2
            if not a and code in lists:
                t = lists[code][1] or t
            if not a:
                a = know.artist_for_title(t)
                u.method = 'title-lookup' if a else 'no-artist'
            else:
                u.method = 'wma-tag'
            u.artist, u.title = a, t
        else:
            u.method = 'no-name'
    # undecided two-sided names follow their folder's majority, else artist first
    for u in units:
        if u.method != 'undecided':
            continue
        v = folder_votes.get(u.folder, collections.Counter())
        order = 'ta' if v['ta'] > v['at'] else 'at'
        a_side, b_side = u.sides[0], ' - '.join(u.sides[1:])
        u.artist, u.title = (a_side, b_side) if order == 'at' else (b_side, a_side)
        u.method = 'folder-vote' if v else 'default-order'
    for u in units:
        if u.artist:
            # the library's own spelling wins (ABBA, Meat Loaf), but a library
            # folder still in catalogue order (Police, The) is turned round too
            u.artist = reorder_comma(know.canonical(fix_artist(u.artist)))
        if u.title:
            u.title = fix_title(u.title)


# ------------------------------------------------------------------ destinations
def plan(units, leftovers):
    taken = set()
    actions = []
    for u in units:
        if u.kind in ('cdg-only', 'audio-only'):
            src = u.files.get('zip') or u.files.get('cdg') or u.files.get('audio')
            dst = os.path.join(ROOT, '_orphans', os.path.basename(src))
            actions.append({'op': 'move', 'src': src, 'dst': dst, 'why': u.kind})
            continue
        if u.artist:
            base = f'{sanitize(u.artist)} - {sanitize(u.title)}' + (f' [{u.code}]' if u.code else '')
            folder = os.path.join(ROOT, sanitize(u.artist))
        else:
            base = sanitize(u.title or u.stem) + (f' [{u.code}]' if u.code else '')
            folder = os.path.join(ROOT, '_unsorted')
        key = (folder.lower(), base.lower())
        if key in taken:
            i = 2
            while (folder.lower(), f'{base}__{i}'.lower()) in taken:
                i += 1
            base = f'{base}__{i}'
            key = (folder.lower(), base.lower())
        taken.add(key)
        dst_cdg = os.path.join(folder, base + '.cdg')
        dst_mp3 = os.path.join(folder, base + '.mp3')
        if u.kind == 'zip':
            actions.append({'op': 'unzip', 'src': u.files['zip'], 'cdg_member': u.files['cdg'][0], 'cdg_size': u.files['cdg'][1],
                            'audio_member': u.files['audio'][0], 'audio_size': u.files['audio'][1], 'aext': u.files['aext'],
                            'dst_cdg': dst_cdg, 'dst_audio': dst_mp3 if u.files['aext'] == '.mp3' else os.path.join(folder, base + u.files['aext']),
                            'transcode_to': dst_mp3 if u.files['aext'] != '.mp3' else None,
                            'artist': u.artist, 'title': u.title, 'code': u.code, 'method': u.method})
        else:
            actions.append({'op': 'move', 'src': u.files['cdg'], 'dst': dst_cdg, 'artist': u.artist, 'title': u.title, 'code': u.code, 'method': u.method})
            if u.files['aext'] == '.mp3':
                actions.append({'op': 'move', 'src': u.files['audio'], 'dst': dst_mp3})
            else:
                actions.append({'op': 'transcode', 'src': u.files['audio'], 'dst': dst_mp3})
    for full, why in leftovers:
        if why == 'junk':
            actions.append({'op': 'delete', 'src': full, 'why': why})
        else:
            rel = os.path.relpath(full, ROOT)
            actions.append({'op': 'move', 'src': full, 'dst': os.path.join(ROOT, '_source', rel), 'why': why})
    return actions


# ------------------------------------------------------------------ doing
def _mkdir(p):
    os.makedirs(os.path.dirname(p), exist_ok=True)


def _unique(dst):
    if not os.path.exists(dst):
        return dst
    b, e = os.path.splitext(dst)
    i = 2
    while os.path.exists(f'{b}__{i}{e}'):
        i += 1
    return f'{b}__{i}{e}'


def transcode(src, dst):
    _mkdir(dst)
    r = subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-vn', '-codec:a', 'libmp3lame', '-q:a', '2', dst],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 10000:
        raise RuntimeError('ffmpeg: ' + r.stderr.strip()[:200])
    keep = os.path.join(SUPERSEDED, os.path.relpath(src, ROOT))
    _mkdir(keep)
    os.replace(src, _unique(keep))


def do(a, log):
    op = a['op']
    if op == 'delete':
        os.remove(a['src'])
    elif op == 'move':
        dst = _unique(a['dst'])
        _mkdir(dst)
        os.replace(a['src'], dst)
        a['dst'] = dst
    elif op == 'transcode':
        dst = _unique(a['dst'])
        transcode(a['src'], dst)
        a['dst'] = dst
    elif op == 'unzip':
        with zipfile.ZipFile(a['src']) as z:
            for member, size, key in ((a['cdg_member'], a['cdg_size'], 'dst_cdg'), (a['audio_member'], a['audio_size'], 'dst_audio')):
                dst = _unique(a[key])
                _mkdir(dst)
                with z.open(member) as f, open(dst, 'wb') as out:
                    shutil.copyfileobj(f, out, 1 << 20)
                if os.path.getsize(dst) != size:
                    raise RuntimeError(f'size mismatch extracting {member}')
                a[key] = dst
        os.remove(a['src'])
        if a.get('transcode_to'):
            dst = _unique(a['transcode_to'])
            transcode(a['dst_audio'], dst)
            a['transcode_to'] = dst
    a['done'] = time.time()
    log.write(json.dumps(a, ensure_ascii=False) + '\n')


def prune_empty(root):
    for dp, dns, fs in os.walk(root, topdown=False):
        if dp == root or os.path.basename(dp) in SPECIAL:
            continue
        if not os.listdir(dp):
            os.rmdir(dp)


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--folder', help='only this relative folder')
    ap.add_argument('--limit', type=int)
    a = ap.parse_args()

    know = Knowledge()
    print(f'library knows {len(know.artists)} artist names and {len(know.titles)} titles', flush=True)
    units, leftovers = collect(a.folder)
    print(f'{len(units)} song units, {len(leftovers)} other files', flush=True)
    decide(units, know)
    actions = plan(units, leftovers)
    if a.limit:
        actions = actions[:a.limit]

    methods = collections.Counter(u.method for u in units)
    kinds = collections.Counter(u.kind for u in units)
    artists = collections.Counter(u.artist for u in units if u.artist)
    with open(PLAN, 'w', encoding='utf-8') as f:
        json.dump(actions, f, ensure_ascii=False, indent=0)
    with open(REPORT, 'w', encoding='utf-8') as f:
        f.write(f'units by kind: {dict(kinds)}\nnames decided by: {dict(methods)}\n')
        f.write(f'{len(artists)} artists; unsorted (no artist): {sum(1 for u in units if not u.artist and u.kind in ("zip", "pair"))}\n\n')
        for m in ('no-artist', 'no-name', 'default-order', 'folder-vote', 'title-lookup', 'wma-tag', 'undecided'):
            rows = [u for u in units if u.method == m]
            if not rows:
                continue
            f.write(f'## {m} ({len(rows)})\n')
            for u in rows[:400]:
                f.write(f'   {u.folder}/{u.stem}  ->  {u.artist} | {u.title} | {u.code}\n')
            f.write('\n')
        f.write('## sample of known/tracklist decisions\n')
        for u in [x for x in units if x.method in ('known', 'tracklist')][::40][:150]:
            f.write(f'   {u.folder}/{u.stem}  ->  {u.artist} | {u.title} | {u.code}\n')
    print('kinds', dict(kinds)); print('methods', dict(methods)); print(f'{len(artists)} artists')
    print(f'plan: {PLAN}\nreport: {REPORT}')
    if not a.apply:
        print('DRY RUN: nothing moved'); return

    os.makedirs(SUPERSEDED, exist_ok=True)
    logp = os.path.join(HERE, 'karaoke_tidy.log.jsonl')
    fails = 0
    t0 = time.time()
    with open(logp, 'a', encoding='utf-8') as log:
        for i, act in enumerate(actions, 1):
            try:
                do(act, log)
            except Exception as e:
                fails += 1
                log.write(json.dumps({'op': 'FAILED', 'action': act, 'error': str(e)}, ensure_ascii=False) + '\n')
                print(f'  !! {act["op"]} {act.get("src")}: {e}', flush=True)
            if i % 500 == 0:
                print(f'  {i}/{len(actions)} done, {fails} failed, {int(time.time() - t0)}s', flush=True)
    prune_empty(ROOT)
    print(f'done: {len(actions) - fails} actions, {fails} failed, {int(time.time() - t0)}s; log {logp}')


if __name__ == '__main__':
    main()
