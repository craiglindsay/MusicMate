"""CD+G karaoke discs -> the karaoke screen's timed text.

A .cdg file is the CD+Graphics subcode of a karaoke disc: 24-byte packets at
300 a second that paint a 300x216, 16-colour screen with tile draws (normal or
XOR), palette loads and screen presets. The lyrics are pixels. The sung-so-far
highlight is a colour change over those pixels, swept left to right as the
words are sung. So:

  * decode the stream and sample the screen every 100 ms;
  * a horizontal band of non-background pixels is a lyric line; a band lives
    from the sample its text appears to the sample it is erased or replaced;
  * the band's pixel mask (which pixels are text, whatever their colour) is
    split into words at the wide gaps, and each word's start is the first
    sample where its pixels have begun changing colour, its end the sample
    where nearly all have;
  * the text comes from the mask, not the disc: glyphs are cut out as
    connected components and looked up in a table keyed by their exact bitmap;
    a band with an unknown glyph goes to RapidOCR (4x upscale) once, and when
    the recognised text has one character per glyph the table learns them.
    Karaoke producers use one bitmap font across hundreds of discs, so the
    table fills in the first few dozen songs and OCR fades out.

A band that is never highlighted (title card, producer credit, "Instrumental")
is not a lyric and is dropped. The result is written per song to
jukebox/cdgtext/<id>.json in the shape the karaoke screen already consumes:
`synced` lines ({t, line}) and `words` lines ({i, t, text, words:[{w, s, e}]}).
The id is the SHA-1 of the .cdg file, so it survives renames.

The store index is jukebox/cdg.db: one row per song found under
D:\\Music\\Karaoke with its artist/title/disc code (from the tidy layout), the
text state, and the library track it corresponds to when artist and title
match one. Karaoke discs are covers, so that link is a pointer, never an
identity: the CD+G song is played from its own MP3.

    python cdg.py scan                    # (re)index the store
    python cdg.py text --limit 30         # extract text for songs without it
    python cdg.py text --id <id> --debug  # one song, with PNGs of every band
    python cdg.py eval                    # compare with lyrics.db where a match exists

Run with the karaoke venv's python (RapidOCR, numpy, Pillow live there).
"""
import os, re, io, sys, json, time, sqlite3, hashlib, argparse, collections, unicodedata
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
# The store moved to C:\Music\Karaoke on 8 Oct 2026 to free space on D:. A store
# inside the music folder still wins, so other machines keep the old default.
_IN_ROOT = Path(os.environ.get('MUSICMATE_ROOT') or r'D:\Music') / 'Karaoke'
STORE = Path(os.environ.get('JUKEBOX_CDG') or (_IN_ROOT if _IN_ROOT.is_dir() or not Path(r'C:\Music\Karaoke').is_dir() else r'C:\Music\Karaoke'))
TEXT_DIR = HERE / 'cdgtext'
DB = HERE / 'cdg.db'
GLYPHS = HERE / 'cdgglyphs.json'
W, H = 300, 216
SAMPLE = 30                 # packets between screen samples: 0.1 s
VERSION = 1


# ---------------------------------------------------------------- the screen
class Screen:
    def __init__(self):
        self.px = np.zeros((H, W), np.uint8)
        self.pal = [(0, 0, 0)] * 16
        self.lut = np.zeros(16, np.uint32)
        self.bg = 0
        self.scrolls = 0

    def rgb(self):
        """The screen as packed 0xRRGGBB values: what the viewer actually sees."""
        return self.lut[self.px]

    def bg_rgb(self):
        return int(self.lut[self.bg])

    def packet(self, p):
        if p[0] & 0x3F != 9:
            return
        ins = p[1] & 0x3F
        d = p[4:20]
        if ins == 1:                                   # memory preset: clear to a colour
            if d[1] & 0x3F == 0:
                self.bg = d[0] & 15
                self.px[:] = self.bg
        elif ins == 2:                                 # border preset: nothing visible for us
            pass
        elif ins in (6, 38):                           # tile block, normal / XOR
            c0, c1 = d[0] & 15, d[1] & 15
            row, col = (d[2] & 31) * 12, (d[3] & 63) * 6
            if row + 12 > H or col + 6 > W:
                return
            bits = np.array([[(d[4 + y] >> (5 - x)) & 1 for x in range(6)] for y in range(12)], np.uint8)
            tile = np.where(bits == 1, c1, c0).astype(np.uint8)
            if ins == 6:
                self.px[row:row + 12, col:col + 6] = tile
            else:
                self.px[row:row + 12, col:col + 6] ^= tile
        elif ins in (20, 24):                          # scroll: rare on these discs; counted, not honoured
            self.scrolls += 1
        elif ins in (30, 31):
            base = 0 if ins == 30 else 8
            for i in range(8):
                hi, lo = d[2 * i] & 0x3F, d[2 * i + 1] & 0x3F
                r = (hi & 0x3C) >> 2
                g = ((hi & 3) << 2) | ((lo & 0x30) >> 4)
                b = lo & 15
                self.pal[base + i] = (r * 17, g * 17, b * 17)
                self.lut[base + i] = (r * 17 << 16) | (g * 17 << 8) | (b * 17)


def read_packets(path):
    b = Path(path).read_bytes()
    n = len(b) // 24
    return b, n


# ---------------------------------------------------------------- bands
class Band:
    """One lyric line's life on screen. Keeps a snapshot of the band's colours
    every time they change, so fill colour, word spans and highlight times are
    all worked out once the line is gone and everything about it is known."""
    _n = 0

    def __init__(self, t, y0, y1, mask, colours, bg=0):
        Band._n += 1
        self.id = Band._n
        self.bg = bg
        self.t0 = t
        self.t1 = t
        self.y0, self.y1 = y0, y1
        self.mask = mask                      # every non-background pixel (identity)
        self.snaps = [(t, colours.copy())]    # (time, colour indices over the band region)
        self.fill = None                      # fill-only mask, once known
        self.highlighted = False
        self.words = None
        self.text = None
        self.ocr = None

    def grow(self, t, mask, colours):
        """The line is still being drawn: take the bigger mask, start afresh."""
        self.t0 = t
        self.mask = mask
        self.snaps = [(t, colours.copy())]

    def see(self, t, colours):
        self.t1 = t
        if not np.array_equal(colours, self.snaps[-1][1]):
            self.snaps.append((t, colours.copy()))

    def finish(self):
        """Fill mask, word spans, per-word highlight (start, end) times."""
        init = self.snaps[0][1]
        # the last snapshot before the wipe began: colours still on screen
        last = init
        for t, colours in self.snaps[1:]:
            if ((colours == self.bg) & self.mask).sum() > 0.1 * self.mask.sum():
                break
            last = colours
        changed = (init != last) & self.mask & (last != self.bg)
        vals = init[self.mask]
        if changed.sum() >= 0.2 * self.mask.sum():
            values, counts = np.unique(init[changed], return_counts=True)
            self.highlighted = True
        else:
            values, counts = (np.unique(vals, return_counts=True) if vals.size else (np.array([0]), np.array([1])))
            self.highlighted = False
        # Outlined fonts: the fill and its outline both change colour as the word is
        # sung. The fill is the candidate whose strokes are the fatter ones (an
        # outline is one pixel wide), not simply the one with the most pixels.
        order = np.argsort(counts)[::-1][:3]
        cands = [values[k] for k in order if counts[k] >= 0.15 * counts.sum()]
        fill_c = max(cands, key=lambda c: run_length((init == c) & self.mask)) if cands else values[0]
        fill = (init == fill_c) & self.mask
        # a font with no outline: the fill is the whole mask
        if fill.sum() < 0.25 * self.mask.sum():
            fill = self.mask
        self.fill = fill
        self.words = split_words(fill)
        starts, ends = [], []
        for (x0, x1) in self.words:
            region = fill[:, x0:x1 + 1]
            tot = region.sum()
            s = e = None
            if tot and self.highlighted:
                for t, colours in self.snaps[1:]:
                    cur = colours[:, x0:x1 + 1]
                    if ((cur == self.bg) & region).sum() > 0.5 * tot:
                        break                            # the line is being wiped
                    frac = ((cur != init[:, x0:x1 + 1]) & region & (cur != self.bg)).sum() / tot
                    if s is None and frac >= 0.15:
                        s = t
                    if frac >= 0.85:
                        e = t
                        break
            starts.append(s); ends.append(e)
        return starts, ends


def run_length(mask):
    """Mean horizontal run of set pixels: fat for a fill, about one for an outline."""
    if not mask.any():
        return 0.0
    padded = np.pad(mask.astype(np.int8), ((0, 0), (1, 1)))
    starts = ((padded[:, 1:] - padded[:, :-1]) == 1).sum()
    return mask.sum() / max(1, starts)


def find_bands(rgb, bg):
    """Rows holding text, grouped into bands separated by empty rows."""
    mask = rgb != bg
    rows = mask.any(axis=1)
    bands = []
    y = 0
    while y < H:
        if not rows[y]:
            y += 1
            continue
        y0 = y
        y1 = y
        gap = 0
        while y < H and gap <= 2:
            if rows[y]:
                gap = 0
                y1 = y
            else:
                gap += 1
            y += 1
        m = mask[y0:y1 + 1]
        if m.sum() >= 12 and (y1 - y0) >= 5:
            bands.append((y0, y1, m))
    return bands


def split_words(mask):
    """x-spans of words: glyph runs separated by the wide gaps."""
    cols = mask.any(axis=0)
    runs = []
    x = 0
    while x < W:
        if not cols[x]:
            x += 1
            continue
        x0 = x
        while x < W and cols[x]:
            x += 1
        runs.append((x0, x - 1))
    if not runs:
        return []
    gaps = [runs[i + 1][0] - runs[i][1] - 1 for i in range(len(runs) - 1)]
    if not gaps:
        return runs
    # letter gaps are 1-3 px and word spaces 6-10 on every font measured
    # (Sunfly old and new, Quick Hits, Zoom); a taller face spreads a little
    thresh = 6 if mask.shape[0] >= 25 else 5
    words = [list(runs[0])]
    for i, g in enumerate(gaps):
        if g >= thresh:
            words.append(list(runs[i + 1]))
        else:
            words[-1][1] = runs[i + 1][1]
    return [tuple(w) for w in words]


def similar_masks(a, b):
    if a.shape != b.shape:
        return False
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return union > 0 and inter / union > 0.8


# ---------------------------------------------------------------- glyphs and text
class Glyphs:
    """Bitmap -> character, learnt from OCR, persisted as JSON."""
    def __init__(self):
        self.table = {}
        self.votes = collections.defaultdict(collections.Counter)
        if GLYPHS.exists():
            j = json.loads(GLYPHS.read_text(encoding='utf-8'))
            for k, v in j.get('votes', {}).items():
                self.votes[k] = collections.Counter(v)
            for k in self.votes:
                self._settle(k)
        self.dirty = False

    def _settle(self, key):
        """Two agreeing readings, and two thirds of all readings, make a glyph
        trusted. One reading is a guess: a wrong OCR line once taught a whole
        font's letters wrong and, every glyph then 'known', nothing re-checked."""
        v = self.votes[key]
        best, n = v.most_common(1)[0]
        if n >= 2 and n >= 2 * sum(v.values()) / 3:
            self.table[key] = best
        else:
            self.table.pop(key, None)

    @staticmethod
    def key(bm):
        h, w = bm.shape
        return f'{w}x{h}:' + hashlib.sha1(np.packbits(bm).tobytes()).hexdigest()[:16]

    def learn(self, key, ch):
        self.votes[key][ch] += 1
        self._settle(key)
        self.dirty = True

    def save(self):
        if not self.dirty:
            return
        GLYPHS.write_text(json.dumps({'table': self.table, 'votes': {k: dict(v) for k, v in self.votes.items()}},
                                     ensure_ascii=False), encoding='utf-8')
        self.dirty = False


def components(mask):
    """Connected components (8-way) of a small boolean image -> list of (x0, y0, x1, y1, bitmap)."""
    h, w = mask.shape
    lab = np.zeros((h, w), np.int32)
    n = 0
    comps = []
    for y in range(h):
        for x in range(w):
            if mask[y, x] and not lab[y, x]:
                n += 1
                stack = [(y, x)]
                lab[y, x] = n
                pts = []
                while stack:
                    cy, cx = stack.pop()
                    pts.append((cy, cx))
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            ny, nx = cy + dy, cx + dx
                            if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not lab[ny, nx]:
                                lab[ny, nx] = n
                                stack.append((ny, nx))
                ys = [p[0] for p in pts]; xs = [p[1] for p in pts]
                comps.append([min(xs), min(ys), max(xs), max(ys), n])
    # merge pieces that overlap in x (the dot of an i, the two parts of a ':' or '=')
    comps.sort(key=lambda c: c[0])
    merged = []
    for c in comps:
        if merged and c[0] <= merged[-1][2] - 1 and min(c[2], merged[-1][2]) - max(c[0], merged[-1][0]) >= 0.4 * min(c[2] - c[0] + 1, merged[-1][2] - merged[-1][0] + 1):
            m = merged[-1]
            m[0], m[1], m[2], m[3] = min(m[0], c[0]), min(m[1], c[1]), max(m[2], c[2]), max(m[3], c[3])
            m[4] = m[4] if isinstance(m[4], list) else [m[4]]
            m[4].append(c[4])
        else:
            merged.append(c)
    out = []
    for x0, y0, x1, y1, ids in merged:
        ids = ids if isinstance(ids, list) else [ids]
        bm = np.isin(lab[y0:y1 + 1, x0:x1 + 1], ids)
        out.append((x0, y0, x1, y1, bm))
    return out


_VOCAB = None


def vocab():
    """Lowercase words the library's lyrics and titles use: the yardstick for
    whether a line of recognised text is words at all."""
    global _VOCAB
    if _VOCAB is None:
        words = set()
        try:
            c = sqlite3.connect(HERE / 'lyrics.db')
            for (a, b) in c.execute('SELECT plain, synced FROM lyric'):
                words.update(re.findall(r"[a-z][a-z']+", (a or b or '').lower()))
            c.close()
        except sqlite3.Error:
            pass
        try:
            c = sqlite3.connect(HERE / 'jukebox.db')
            for (t,) in c.execute('SELECT title FROM track'):
                words.update(re.findall(r"[a-z][a-z']+", (t or '').lower()))
            c.close()
        except sqlite3.Error:
            pass
        _VOCAB = words
    return _VOCAB


_FREQ = None


def freq():
    """Word frequencies over the lyric cache, for spelling corrections."""
    global _FREQ
    if _FREQ is None:
        c = collections.Counter()
        try:
            db_ = sqlite3.connect(HERE / 'lyrics.db')
            for (a, b) in db_.execute('SELECT plain, synced FROM lyric'):
                c.update(re.findall(r"[a-z][a-z']+", (a or b or '').lower()))
            db_.close()
        except sqlite3.Error:
            pass
        _FREQ = c
    return _FREQ


I_FORMS = {'i', "i'm", "i'll", "i've", "i'd"}


def fix_word_case(tok):
    """SOMETHINg -> SOMETHING, BIggER -> BIGGER, somethINg -> something: a word
    that mixes cases beyond its first letter is a glyph read two ways, never
    the writer's intent (McDonald loses; a lyric sheet rarely has one)."""
    m = re.match(r"^([^A-Za-z]*)([A-Za-z][A-Za-z']*)(.*)$", tok)
    if not m:
        return tok
    pre, core, post = m.groups()
    rest = [ch for ch in core[1:] if ch.isalpha()]
    if not rest or all(ch.isupper() for ch in rest) or all(ch.islower() for ch in rest):
        return tok
    ups = sum(1 for ch in rest if ch.isupper())
    if ups >= len(rest) - ups:
        core = core.upper()
    else:
        core = core[0] + core[1:].lower()
    return pre + core + post


def fix_line_case(words):
    """A line shouted in capitals is set in sentence case: the screen's type is
    large already, and the discs' own capitals are a 1990s habit, not meaning."""
    letters = [ch for w in words for ch in w if ch.isalpha()]
    if len(letters) < 6 or sum(1 for ch in letters if ch.isupper()) < 0.7 * len(letters):
        return words
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        core = lw.strip(".,;:!?\"()")
        if core in I_FORMS:
            lw = lw.replace(core, 'I' + core[1:], 1)
        if i == 0:
            m = re.search(r'[a-z]', lw)
            if m:
                lw = lw[:m.start()] + lw[m.start()].upper() + lw[m.start() + 1:]
        out.append(lw)
    return out


def _edits(w):
    letters = "abcdefghijklmnopqrstuvwxyz'"
    splits = [(w[:i], w[i:]) for i in range(len(w) + 1)]
    yield from (a + b[1:] for a, b in splits if b)
    yield from (a + b[1] + b[0] + b[2:] for a, b in splits if len(b) > 1)
    yield from (a + c + b[1:] for a, b in splits if b for c in letters)
    yield from (a + c + b for a, b in splits for c in letters)


def fix_spelling(tok):
    """A word the lyric vocabulary has never seen, one edit from a common word
    it has: take the common word. Conservative: alphabetic words of four or
    more letters only, one edit, a candidate seen at least five times."""
    m = re.match(r"^([^A-Za-z]*)([A-Za-z][A-Za-z']*)(.*)$", tok)
    if not m:
        return tok
    pre, core, post = m.groups()
    low = core.lower()
    if len(low) < 4 or low in vocab() or not low.isalpha():
        return tok
    f = freq()
    best, bn = None, 0
    for cand in set(_edits(low)):
        n = f.get(cand, 0)
        if n > bn:
            best, bn = cand, n
    if not best or bn < 5:
        return tok
    if core.isupper():
        best = best.upper()
    elif core[0].isupper():
        best = best[0].upper() + best[1:]
    return pre + best + post


def polish(words):
    """Case and spelling for one line's word list (strings). Returns new list."""
    # an apostrophe drawn low reads as a full stop or comma: WHAT.S, don,t
    ws = [re.sub(r"(?<=[A-Za-z])[.,](?=(?:s|t|ll|re|ve|m|d)\b)", "'", w, flags=re.I) for w in words]
    ws = [fix_word_case(w) for w in ws]
    ws = [fix_spelling(w) for w in ws]
    return fix_line_case(ws)


def wordness(text):
    """Share of the alphabetic tokens (3+ letters) that are known words."""
    toks = [t for t in re.findall(r"[A-Za-z][A-Za-z']+", text or '') if len(t) >= 3]
    if not toks:
        return 1.0
    v = vocab()
    return sum(1 for t in toks if t.lower().strip("'") in v) / len(toks)


_OCR = None


def ocr_engine():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


def band_image(mask, scale=4, pad=10):
    from PIL import Image
    h, w = mask.shape
    im = Image.fromarray(np.where(mask, 0, 255).astype(np.uint8), 'L')
    im = im.resize((w * scale, h * scale), Image.NEAREST)
    canvas = Image.new('L', (im.width + 2 * pad * scale, im.height + 2 * pad * scale), 255)
    canvas.paste(im, (pad * scale, pad * scale))
    return canvas.convert('RGB')


def ocr_band(mask):
    """Text of a band by OCR. The band is one line already cut out, so only the
    recogniser runs (no detection): far faster, and nothing to get wrong."""
    im = band_image(mask, 3, 4)
    try:
        res, _ = ocr_engine()(np.array(im), use_det=False, use_cls=False, use_rec=True)
    except TypeError:
        res, _ = ocr_engine()(np.array(im))
    if not res:
        return ''
    first = res[0]
    if isinstance(first, (list, tuple)) and len(first) >= 2 and not isinstance(first[0], str):
        res.sort(key=lambda r: min(p[0] for p in r[0]))
        return ' '.join(r[1] for r in res).strip()
    return ' '.join(str(r[0]) if isinstance(r, (list, tuple)) else str(r) for r in res).strip()


def read_band(band, glyphs, stats):
    """Set band.text from glyph table, else OCR (learning glyphs when they line up)."""
    comps = components(band.fill)
    if not comps:
        return
    words = band.words or split_words(band.fill)
    keys = [Glyphs.key(c[4]) for c in comps]
    known = [glyphs.table.get(k) for k in keys]
    if all(known):
        # assemble: a space wherever a word boundary falls between glyphs
        out = []
        wi = 0
        for (x0, y0, x1, y1, bm), ch, k in zip(comps, known, keys):
            while wi < len(words) - 1 and x0 > words[wi][1]:
                wi += 1
                out.append(' ')
            v = glyphs.votes.get(k)
            if v and 'I' in v and 'l' in v:
                ch = '\x00'                          # I or l: the word will tell
            out.append(ch)
        text = re.sub(r'\s+', ' ', ''.join(out)).strip()
        text = ' '.join(settle_il(w) for w in text.split(' '))
        if wordness(text) >= 0.6 or len(text) < 4:
            band.text = text
            stats['glyph'] += 1
            return
        stats['doubted'] += 1                       # known glyphs, but not words: read it properly
    text = ocr_band(band.fill)
    stats['ocr'] += 1
    band.ocr = text
    band.text = text
    text = ' '.join(fix_word_case(w) for w in text.split())
    band.text = text
    chars = text.replace(' ', '')
    if chars and len(chars) == len(comps) and wordness(text) >= 0.6:
        for k, ch in zip(keys, chars):
            glyphs.learn(k, ch)
        stats['learnt'] += 1


# ---------------------------------------------------------------- extraction
def overlap(a, b):
    inter = np.logical_and(a, b).sum()
    return inter / max(1, min(a.sum(), b.sum()))


def extract(cdg_path, debug_dir=None):
    """Run the disc; return (lyric bands with word times, duration, notes)."""
    b, n = read_packets(cdg_path)
    scr = Screen()
    live = []
    done = []
    dying = []          # (time, y0, y1, mask) of lines just erased: their shrinking remains are not new lines
    Band._n = 0
    for i in range(n):
        scr.packet(b[i * 24:i * 24 + 24])
        if i % SAMPLE:
            continue
        t = i / 300.0
        rgb = scr.rgb()
        found = find_bands(rgb, scr.bg_rgb())
        seen = set()
        for y0, y1, m in found:
            hit = None
            for L in live:
                if L.id in seen:
                    continue
                if abs(L.y0 - y0) <= 3 and abs(L.y1 - y1) <= 3 and L.mask.shape == m.shape:
                    if overlap(L.mask, m) > 0.8:
                        hit = L
                        if m.sum() > L.mask.sum() * 1.02:        # still being drawn
                            L.grow(t, m.copy(), rgb[y0:y1 + 1])
                        break
            if hit is None:
                # rows shifted a little as the line grew: same line if it contains the old one
                for L in live:
                    if L.id in seen or L.mask.shape[0] == m.shape[0]:
                        continue
                    lo, hi = max(L.y0, y0), min(L.y1, y1)
                    if hi - lo > 0.6 * (L.y1 - L.y0) and m.sum() >= L.mask.sum():
                        hit = L
                        L.y0, L.y1 = y0, y1
                        L.grow(t, m.copy(), rgb[y0:y1 + 1])
                        break
            if hit is None:
                for L in live:
                    if L.id in seen or not (L.y0 - 2 <= y0 and L.y1 + 2 >= y1):
                        continue
                    sub = L.mask[max(0, y0 - L.y0):max(0, y0 - L.y0) + m.shape[0]]
                    if sub.shape == m.shape and np.logical_and(sub, m).sum() >= 0.9 * m.sum():
                        hit = L                                   # shrinking: same line, on its way out
                        break
            if hit is None:
                remnant = False
                for (td, dy0, dy1, dm) in dying:
                    if t - td < 2.5 and dy0 <= y0 + 2 and dy1 >= y1 - 2:
                        sub = dm[y0 - dy0:y1 - dy0 + 1] if dm.shape[0] >= (y1 - y0 + 1) else None
                        if sub is not None and sub.shape == m.shape and np.logical_and(sub, m).sum() >= 0.9 * m.sum():
                            remnant = True
                            break
                if remnant:
                    continue
                hit = Band(t, y0, y1, m.copy(), rgb[y0:y1 + 1], scr.bg_rgb())
                live.append(hit)
            seen.add(hit.id)
            hit.see(t, rgb[hit.y0:hit.y1 + 1])
        for L in live[:]:
            if L.id not in seen:
                live.remove(L)
                done.append(L)
                dying.append((t, L.y0, L.y1, L.mask))
        dying = [d for d in dying if t - d[0] < 2.5]
    done.extend(live)
    duration = n / 300.0
    lines = []
    for Bd in done:
        starts, ends = Bd.finish()
        if not Bd.highlighted or not any(s is not None for s in starts):
            continue                                    # never sung: a title card or credit
        lines.append((Bd, starts, ends))
    lines.sort(key=lambda x: next(s for s in x[1] if s is not None))
    if debug_dir:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        for k, (Bd, starts, ends) in enumerate(lines):
            band_image(Bd.fill, 2, 2).save(Path(debug_dir) / f'{k:03d}_{Bd.t0:07.2f}.png')
    return lines, duration, {'scrolls': scr.scrolls, 'bands': len(done)}


I_WORDS = {'i', "i'm", "i'll", "i've", "i'd", 'it', "it's", 'in', 'is', 'if', 'ill', "isn't", 'into', 'its'}


def settle_il(word):
    """Fill the I-or-l glyphs of one word: a capital where the word is one of the
    I-words or the letter starts a capitalised word; otherwise a lowercase l."""
    if '\x00' not in word:
        return word
    core = word.strip(".,;:!?\"()")
    as_i = core.replace('\x00', 'I')
    if as_i.lower().replace('l', 'i') in I_WORDS or as_i.lower() in I_WORDS:
        return word.replace('\x00', 'I')
    others = [ch for ch in core if ch.isalpha()]
    if others and all(ch.isupper() for ch in others):
        return word.replace('\x00', 'I')                 # an all-caps disc: CHIQUITITA
    out = []
    for i, ch in enumerate(word):
        if ch != '\x00':
            out.append(ch); continue
        prev = word[i - 1] if i else ''
        nxt = word[i + 1] if i + 1 < len(word) else ''
        letter = lambda ch: ch.isalpha() or ch == '\x00'
        if (not prev or not letter(prev)) and (not nxt or nxt.isupper() or not letter(nxt)):
            out.append('I')                          # alone, or before a capital: I
        else:
            out.append('l')
    return ''.join(out)


I_L = {"l": "I", "l'm": "I'm", "l'll": "I'll", "l've": "I've", "l'd": "I'd", "lt": "It", "lt's": "It's",
       "ln": "In", "ls": "Is", "lf": "If"}


def fix_token(tok):
    # an apostrophe drawn low reads as a comma: don,t -> don't
    tok = re.sub(r"(?<=[A-Za-z]),(?=[a-z])", "'", tok)
    core = tok.strip(".,;:!?\"()")
    if core in I_L:
        return tok.replace(core, I_L[core], 1)
    if len(core) > 1 and core[0] == 'l' and core[1].isupper():
        return tok.replace(core, 'I' + core[1:], 1)
    return tok


def correct_from_reference(wlines, ref_text):
    """The library already has this song's words: take spelling and case from
    there wherever ours is a near miss, and split a word the pixel gaps missed
    when it is two reference words run together. Timing is kept; a split shares
    it by letter count."""
    import difflib
    ref = re.findall(r"[A-Za-z0-9'][A-Za-z0-9'\-]*", ref_text)
    if len(ref) < 20:
        return 0
    ref_l = [w.lower() for w in ref]
    ours = [(li, wi) for li, L in enumerate(wlines) for wi in range(len(L['words']))]
    ours_l = [wlines[li]['words'][wi]['w'].strip(".,;:!?\"()").lower() for li, wi in ours]
    sm = difflib.SequenceMatcher(None, ours_l, ref_l, autojunk=False)
    fixes = 0
    edits = []                                   # (li, wi, new tokens)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                li, wi = ours[i1 + k]
                w = wlines[li]['words'][wi]['w']
                core = w.strip(".,;:!?\"()")
                if core and core != ref[j1 + k] and core.lower() == ref_l[j1 + k]:
                    edits.append((li, wi, [w.replace(core, ref[j1 + k], 1)]))
            continue
        if tag != 'replace':
            continue
        if i2 - i1 == j2 - j1:
            for k in range(i2 - i1):
                a, b = ours_l[i1 + k], ref_l[j1 + k]
                if a and difflib.SequenceMatcher(None, a, b).ratio() >= 0.6:
                    li, wi = ours[i1 + k]
                    w = wlines[li]['words'][wi]['w']
                    core = w.strip(".,;:!?\"()")
                    edits.append((li, wi, [w.replace(core, ref[j1 + k], 1)]))
        elif i2 - i1 == 1 and 2 <= j2 - j1 <= 3:
            joined = ''.join(ref_l[j1:j2])
            if ours_l[i1] == joined or difflib.SequenceMatcher(None, ours_l[i1], joined).ratio() >= 0.85:
                li, wi = ours[i1]
                edits.append((li, wi, ref[j1:j2]))
    for li, wi, toks in sorted(edits, key=lambda e: (e[0], -e[1])):
        L = wlines[li]
        old = L['words'][wi]
        if len(toks) == 1:
            old['w'] = toks[0]
            fixes += 1
            continue
        total = sum(len(t) for t in toks)
        span = old['e'] - old['s']
        new, pos = [], 0.0
        for t in toks:
            frac = len(t) / total
            new.append({'w': t, 's': round(old['s'] + span * pos, 2), 'e': round(old['s'] + span * (pos + frac), 2), 'c': old.get('c', 0.9)})
            pos += frac
        L['words'][wi:wi + 1] = new
        fixes += 1
    for L in wlines:
        L['text'] = ' '.join(w['w'] for w in L['words'])
    return fixes


def to_timed_text(lines, glyphs, stats, duration):
    """Bands -> the screen's two shapes. Word times: highlight sweep; gaps filled sensibly."""
    synced, wlines = [], []
    for i, (Bd, starts, ends) in enumerate(lines):
        read_band(Bd, glyphs, stats)
        text = (Bd.text or '').strip()
        if not text:
            continue
        toks = [fix_token(t) for t in text.split()]
        spans = list(zip(Bd.words, starts, ends))
        # reconcile OCR tokens with pixel word spans
        if len(toks) != len(spans):
            spans = fit_spans(toks, Bd, starts, ends)
        pairs = list(zip(toks, spans))
        while pairs and not re.search(r'[A-Za-z0-9]', pairs[0][0]):
            pairs.pop(0)                                 # count-in marks: "+ + + +", "♪♪"
        toks = [p[0] for p in pairs]
        spans = [p[1] for p in pairs]
        toks = polish(toks)
        words = []
        for k, (tok, (span, s, e)) in enumerate(zip(toks, spans)):
            if s is None:
                s = words[-1]['e'] if words else None
            if s is None:
                continue
            if e is None or e <= s:
                nxt = next((x[1] for x in spans[k + 1:] if x[1] is not None), None)
                e = min(s + 1.2, nxt) if nxt else s + 0.6
            words.append({'w': tok, 's': round(s, 2), 'e': round(e, 2), 'c': 0.9})
        if not words:
            continue
        line_t = words[0]['s']
        synced.append({'t': round(line_t, 2), 'line': ' '.join(w['w'] for w in words)})
        wlines.append({'i': len(synced) - 1, 't': round(line_t, 2), 'text': synced[-1]['line'],
                       'c': 0.9, 'ok': True, 'shown': round(Bd.t0, 2), 'hidden': round(Bd.t1, 2), 'words': words})
    return synced, wlines


def fit_spans(toks, Bd, starts, ends):
    """OCR gave a different word count from the pixel gaps: share the band's
    width among the tokens by character count and take each token's timing from
    the pixel words it overlaps."""
    x0 = min(w[0] for w in Bd.words); x1 = max(w[1] for w in Bd.words)
    total = sum(len(t) for t in toks) + len(toks) - 1
    out = []
    pos = 0
    for tok in toks:
        a = x0 + (x1 - x0) * pos / total
        pos += len(tok) + 1
        bx = x0 + (x1 - x0) * (pos - 1) / total
        s = e = None
        for (wx0, wx1), ws, we in zip(Bd.words, starts, ends):
            if wx1 >= a and wx0 <= bx:
                if ws is not None and (s is None or ws < s):
                    s = ws
                if we is not None and (e is None or we > e):
                    e = we
        out.append(((int(a), int(bx)), s, e))
    return out


# ---------------------------------------------------------------- store index
def norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'\(.*?\)|\[.*?\]', '', s)
    s = re.sub(r'\b(feat|ft|featuring)\b.*$', '', s)
    s = re.sub(r'^\s*the\s+', '', s)
    s = s.replace('&', ' and ')
    return re.sub(r'[^a-z0-9]', '', s)


SCHEMA = """
CREATE TABLE IF NOT EXISTS song (
  id TEXT PRIMARY KEY,           -- sha1 of the .cdg
  artist TEXT, title TEXT, code TEXT,
  rel_cdg TEXT, rel_mp3 TEXT, cdg_size INTEGER, cdg_mtime REAL,
  duration REAL,
  text_state TEXT DEFAULT 'none', -- none | ok | empty | fail
  n_lines INTEGER, n_words INTEGER, ocr_bands INTEGER, glyph_bands INTEGER, seconds REAL, made REAL, note TEXT,
  lib_skey TEXT, lib_artist TEXT, lib_title TEXT
);
CREATE INDEX IF NOT EXISTS song_artist ON song(artist);
CREATE INDEX IF NOT EXISTS song_lib ON song(lib_skey);
"""


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


NAME_RE = re.compile(r'^(?P<artist>.+?) - (?P<title>.+?)(?: \[(?P<code>[^\]]+)\])?(?:__\d+)?$')


def scan(conn):
    """Index every Artist/Artist - Title [code].cdg pair. Hash only new or changed files."""
    known = {r['rel_cdg']: r for r in conn.execute('SELECT id, rel_cdg, cdg_size, cdg_mtime FROM song')}
    lib = {}
    try:
        j = sqlite3.connect(HERE / 'jukebox.db')
        for t, a, sk in j.execute("SELECT track.title, COALESCE(NULLIF(track.track_artist,''), artist.name), track.skey "
                                  'FROM track JOIN album ON album.id=track.album_id JOIN artist ON artist.id=album.artist_id'):
            lib.setdefault((norm(a), norm(t)), (sk, a, t))
        j.close()
    except sqlite3.Error:
        pass
    n_new = n_seen = 0
    present = set()
    for adir in sorted(STORE.iterdir()):
        if not adir.is_dir() or adir.name.startswith('_'):
            continue
        for cdg in adir.glob('*.cdg'):
            mp3 = cdg.with_suffix('.mp3')
            if not mp3.exists():
                continue
            rel = str(cdg.relative_to(STORE)).replace('\\', '/')
            present.add(rel)
            st = cdg.stat()
            k = known.get(rel)
            if k and k['cdg_size'] == st.st_size and abs(k['cdg_mtime'] - st.st_mtime) < 1:
                n_seen += 1
                continue
            sha = hashlib.sha1(cdg.read_bytes()).hexdigest()[:16]
            m = NAME_RE.match(cdg.stem)
            artist = m.group('artist') if m else adir.name
            title = m.group('title') if m else cdg.stem
            code = m.group('code') if m else None
            hit = lib.get((norm(artist), norm(title)))
            conn.execute('INSERT OR REPLACE INTO song (id, artist, title, code, rel_cdg, rel_mp3, cdg_size, cdg_mtime, duration, '
                         'text_state, lib_skey, lib_artist, lib_title) VALUES (?,?,?,?,?,?,?,?,?,'
                         "COALESCE((SELECT text_state FROM song WHERE id=?), 'none'),?,?,?)",
                         (sha, artist, title, code, rel, str(mp3.relative_to(STORE)).replace('\\', '/'), st.st_size, st.st_mtime,
                          round(st.st_size / 7200.0, 2), sha, hit[0] if hit else None, hit[1] if hit else None, hit[2] if hit else None))
            n_new += 1
    gone = [r for r in known if r not in present]
    for rel in gone:
        conn.execute('DELETE FROM song WHERE rel_cdg=?', (rel,))
    conn.commit()
    tot = conn.execute('SELECT COUNT(*) FROM song').fetchone()[0]
    linked = conn.execute('SELECT COUNT(*) FROM song WHERE lib_skey IS NOT NULL').fetchone()[0]
    print(f'store: {tot} songs ({n_new} new or changed, {n_seen} unchanged, {len(gone)} gone); {linked} match a library track')


# ---------------------------------------------------------------- text batch
def text_one(conn, row, glyphs, debug=False):
    cdg = STORE / row['rel_cdg']
    t0 = time.time()
    stats = collections.Counter()
    try:
        lines, duration, notes = extract(cdg, (HERE / 'cdgdebug' / row['id']) if debug else None)
        synced, wlines = to_timed_text(lines, glyphs, stats, duration)
    except Exception as e:
        conn.execute("UPDATE song SET text_state='fail', note=?, made=? WHERE id=?", (str(e)[:200], time.time(), row['id']))
        conn.commit()
        return 'fail', str(e)
    state = 'ok' if synced else 'empty'
    raw = [l['text'] for l in wlines]
    fixes = 0
    if row['lib_skey']:
        ref = reference_text(row['lib_skey'])
        if ref:
            fixes = correct_from_reference(wlines, ref)
            synced = [{'t': l['t'], 'line': l['text']} for l in wlines]
    stats['fixes'] = fixes
    out = {'version': VERSION, 'source': 'cdg', 'id': row['id'], 'artist': row['artist'], 'title': row['title'],
           'code': row['code'], 'duration': round(duration, 2), 'made': time.time(), 'seconds': round(time.time() - t0, 1),
           'ocr_bands': stats['ocr'], 'glyph_bands': stats['glyph'], 'fixes': fixes, 'notes': notes, 'raw': raw,
           'synced': synced, 'words': {'model': 'cdg', 'version': VERSION, 'duration': round(duration, 2), 'lines': wlines}}
    TEXT_DIR.mkdir(exist_ok=True)
    (TEXT_DIR / f'{row["id"]}.json').write_text(json.dumps(out, ensure_ascii=False), encoding='utf-8')
    notes['wordness'] = round(wordness(' '.join(raw)), 3)
    out['notes'] = notes
    (TEXT_DIR / f'{row["id"]}.json').write_text(json.dumps(out, ensure_ascii=False), encoding='utf-8')
    conn.execute('UPDATE song SET text_state=?, n_lines=?, n_words=?, ocr_bands=?, glyph_bands=?, seconds=?, made=?, note=?, duration=? WHERE id=?',
                 (state, len(synced), sum(len(l['words']) for l in wlines), stats['ocr'], stats['glyph'],
                  round(time.time() - t0, 1), time.time(), json.dumps(notes), round(duration, 2), row['id']))
    conn.commit()
    return state, f"{len(synced)} lines, ocr {stats['ocr']} glyph {stats['glyph']} learnt {stats['learnt']} fixed {stats['fixes']}, {time.time() - t0:.1f}s"


_LYR = None


def reference_text(skey):
    """The library's lyric text for this song, if the cache has it."""
    global _LYR
    try:
        if _LYR is None:
            _LYR = sqlite3.connect(HERE / 'lyrics.db')
        r = _LYR.execute('SELECT synced, plain FROM lyric WHERE skey=?', (skey,)).fetchone()
    except sqlite3.Error:
        return None
    if not r:
        return None
    txt = r[0] or r[1] or ''
    return re.sub(r'\[\d+:\d+(?:\.\d+)?\]', ' ', txt)


def cmd_text(a):
    conn = db()
    glyphs = Glyphs()
    if a.id:
        rows = conn.execute('SELECT * FROM song WHERE id=?', (a.id,)).fetchall()
    else:
        conds, args = [], []
        if not a.redo:
            conds.append("text_state='none'")
        if a.linked:
            conds.append('lib_skey IS NOT NULL')
        if a.before:
            conds.append('(made IS NULL OR made < ?)'); args.append(a.before)
        if a.ids:
            ids = [l.strip() for l in open(a.ids, encoding='utf-8') if l.strip()]
            conds.append(f"id IN ({','.join('?' * len(ids))})"); args.extend(ids)
        where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
        rows = conn.execute(f'SELECT * FROM song {where} ORDER BY artist, title LIMIT ?', (*args, a.limit or 1000000)).fetchall()
    print(f'{len(rows)} songs to read; glyph table {len(glyphs.table)}', flush=True)
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        state, msg = text_one(conn, r, glyphs, a.debug)
        print(f'[{i}/{len(rows)}] {r["artist"]} - {r["title"]}: {state} ({msg})', flush=True)
        if i % 10 == 0:
            glyphs.save()
    glyphs.save()
    print(f'done in {time.time() - t0:.0f}s; glyph table {len(glyphs.table)}')


def cmd_fix(a):
    """Re-polish every song's text in place: case, spelling, line case."""
    conn = db()
    rows = conn.execute("SELECT id, lib_skey FROM song WHERE text_state='ok'").fetchall()
    changed = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        fp = TEXT_DIR / f'{r["id"]}.json'
        if not fp.exists():
            continue
        j = json.loads(fp.read_text(encoding='utf-8'))
        lines = j.get('words', {}).get('lines', [])
        before = json.dumps([l['text'] for l in lines])
        for L in lines:
            toks = polish([w['w'] for w in L['words']])
            for w, t in zip(L['words'], toks):
                w['w'] = t
            L['text'] = ' '.join(w['w'] for w in L['words'])
        j['synced'] = [{'t': L['t'], 'line': L['text']} for L in lines]
        if json.dumps([l['text'] for l in lines]) != before:
            changed += 1
            j['polished'] = time.time()
            fp.write_text(json.dumps(j, ensure_ascii=False), encoding='utf-8')
        if i % 1000 == 0:
            print(f'  {i}/{len(rows)} ({changed} changed)', flush=True)
    print(f'polished {len(rows)} songs, {changed} changed, {time.time() - t0:.0f}s')


def cmd_eval(a):
    """Compare our text with lyrics.db for songs that match a library track with synced lyrics."""
    import difflib
    conn = db()
    lyr = sqlite3.connect(HERE / 'lyrics.db')
    rows = conn.execute("SELECT * FROM song WHERE text_state='ok' AND lib_skey IS NOT NULL").fetchall()
    scores = []
    for r in rows:
        got = lyr.execute("SELECT synced, plain FROM lyric WHERE skey=?", (r['lib_skey'],)).fetchone()
        if not got or not (got[0] or got[1]):
            continue
        ref = re.sub(r'\[\d+:\d+(?:\.\d+)?\]', ' ', got[0] or got[1] or '')
        ref_w = re.findall(r"[a-z0-9']+", ref.lower())
        j = json.loads((TEXT_DIR / f'{r["id"]}.json').read_text(encoding='utf-8'))
        raw_lines = j.get('raw') or [l['line'] for l in j['synced']]
        ours_w = re.findall(r"[a-z0-9']+", ' '.join(raw_lines).lower())
        if not ours_w or not ref_w:
            continue
        refset, ourset = set(ref_w), set(ours_w)
        prec = sum(1 for w in ours_w if w in refset) / len(ours_w)      # our words that are real words of the song
        rec = sum(1 for w in ref_w if w in ourset) / len(ref_w)         # the song's words we found
        scores.append((prec, rec, r['artist'], r['title'], len(ours_w), len(ref_w), r['code']))
    scores.sort(key=lambda s: (s[0], s[1]))
    if not scores:
        print('nothing to compare'); return
    med = lambda k: sorted(s[k] for s in scores)[len(scores) // 2]
    print(f'{len(scores)} songs compared; median word precision {med(0):.2f}, median recall {med(1):.2f}')
    for s in scores[:10]:
        print(f'  low  p={s[0]:.2f} r={s[1]:.2f}  {s[2]} - {s[3]} [{s[6]}]  (ours {s[4]} words, ref {s[5]})')
    for s in scores[-3:]:
        print(f'  high p={s[0]:.2f} r={s[1]:.2f}  {s[2]} - {s[3]} [{s[6]}]')


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('scan')
    t = sub.add_parser('text')
    t.add_argument('--limit', type=int); t.add_argument('--id'); t.add_argument('--debug', action='store_true')
    t.add_argument('--redo', action='store_true'); t.add_argument('--linked', action='store_true', help='only songs matching a library track')
    t.add_argument('--ids', help='a file of song ids, one per line: only these (the chosen versions first, say)')
    t.add_argument('--before', type=float, help='only songs last read before this epoch time (resume a pass)')
    sub.add_parser('eval')
    sub.add_parser('fix')
    a = ap.parse_args()
    if a.cmd == 'scan':
        scan(db())
    elif a.cmd == 'text':
        cmd_text(a)
    elif a.cmd == 'eval':
        cmd_eval(a)
    elif a.cmd == 'fix':
        cmd_fix(a)


if __name__ == '__main__':
    main()
