"""Collapse 120 raw genre strings into shelves you can actually browse.

The raw tags are a mess: `Other` is the third largest value, `Alt` / `Alternative` /
`Alt. Rock` / `AlternRock Alt. Rock` / `Alternative & Punk` are five spellings of one
idea, `rock_pop` is a filename-safe string that leaked into a tag, and twelve tracks
carry Shift-JIS mojibake for the Japanese word for rock.

Two levels: a handful of shelves you can see from across a room, each opening onto
the finer genres underneath it. Anything unrecognised lands in Unfiled, which is
browsable but is not given a shelf of its own.
"""

# shelf -> the raw values that belong on it (lowercased, compared loosely)
SHELVES = {
    'Rock': [
        'rock', 'rock/pop', 'rock_pop', 'classic rock', 'hard rock', 'rock & roll',
        'rock and roll', 'southern rock', 'general rock', 'slow rock', 'pop rock',
        'progressive rock', 'psychedelic rock', 'symphonic rock', 'glam rock',
        'album rock', 'arena rock', 'rock &amp; roll',
    ],
    'Alternative': [
        'alternative', 'alt', 'alt. rock', 'alternrock alt. rock', 'alternative rock',
        'alternative rock/pop', 'alternative/punk', 'alternative & punk', 'indie',
        'indie rock', 'indie pop', 'britpop', 'grunge', 'lo-fi', 'noise',
        'ambient alternative', 'college rock',
    ],
    'Punk & Metal': [
        'punk', 'punk rock', 'metal', 'heavy metal', 'death metal', 'thrash metal',
        'industrial', 'techno-industrial', 'hardcore', 'ska',
    ],
    'Pop': [
        'pop', 'top 40', 'instrumental pop', 'pop-folk', 'teen pop', 'dance pop',
        'synthpop', 'synth-pop', 'new wave', 'retro', 'europop',
    ],
    'Electronic': [
        'electronic', 'electronica', 'electronica/dance', 'electronica & dance',
        'dance', 'techno', 'trance', 'house', 'club', 'eurodance', 'disco',
        'trip-hop', 'ambient', 'techno/ambient', 'downtempo', 'drum & bass',
        'dance hall', 'booty bass', 'remix', 'big beat',
    ],
    'Hip-Hop': [
        'hip-hop', 'hip hop', 'rap', 'rap/hip-hop', 'rap & hip hop', 'hip hop/rap',
        'hip-hop/rap', 'gangsta rap', 'old school rap',
    ],
    'R&B & Soul': [
        'r&b', 'r & b', 'rnb', 'soul', 'soul / r&b', 'soul/r&b', 'funk', 'motown',
        'quiet storm', 'contemporary r&b',
    ],
    'Jazz': [
        'jazz', 'jazz vocal', 'vocal jazz', 'swing', 'fusion', 'bebop', 'big band',
        'smooth jazz', 'acid jazz',
    ],
    'Blues': ['blues', 'delta blues', 'electric blues', 'rhythm and blues'],
    'Country': [
        'country', 'country & western', 'bluegrass', 'americana', 'alt-country',
        'cajun', 'honky tonk',
    ],
    'Folk & Acoustic': [
        'folk', 'folk-rock', 'folk/rock', 'folk rock', 'folk-rock folk/rock',
        'acoustic', 'singer-songwriter', 'celtic', 'folk comedy',
    ],
    'World': [
        'world', 'world/reggae', 'reggae', 'latin', 'ethnic', 'national music',
        'tribal', 'salsa', 'afrobeat', 'bossa nova', 'tango', 'flamenco',
    ],
    'Classical & Opera': [
        'classical', 'opera', 'orchestral', 'chamber music', 'baroque', 'symphony',
    ],
    'Ambient & New Age': [
        'new age', 'meditation', 'relaxation', 'nature', 'space music',
    ],
    'Easy Listening': [
        'easy listening', 'vocal', 'vocal/easy listening', 'lounge', 'instrumental',
        'oldies', 'standards', 'crooner',
    ],
    'Soundtrack': [
        'soundtrack', 'soundtracks', 'soundtracks/musicals', 'musical', 'score',
        'anime', 'game',
    ],
    'Spoken & Comedy': [
        'comedy', 'comedy/spoken word', 'spoken word', 'holiday/comedy', 'audiobook',
        'sound clip', 'other/spoken',
    ],
    'Gospel & Christian': [
        'gospel', 'christian rock', 'christian/gospel', 'gospel & religious',
        'christian', 'religious', 'praise & worship',
    ],
    'Holiday': ['holiday music', 'christmas', 'holiday'],
    "Children's": ["children's", 'children', 'kids', 'lullaby'],
}

# values that carry no information at all -- they must not become a shelf
NOISE = {
    'other', 'miscellaneous', 'general', 'unclassifiable', 'unknown', 'genre',
    'none', 'various', 'misc', '', 'unfiled',
}

UNFILED = 'Unfiled'

def _key(s):
    return ''.join(ch for ch in (s or '').lower().strip()
                   if ch.isalnum() or ch in ' &/-').strip()

_LOOKUP = {}
for shelf, values in SHELVES.items():
    for v in values:
        _LOOKUP[_key(v)] = shelf

def shelf_of(raw):
    """Which shelf a raw genre string belongs on. Returns (shelf, subgenre)."""
    if raw is None:
        return UNFILED, None
    s = raw.strip()
    k = _key(s)
    if not k or k in NOISE:
        return UNFILED, None
    # mojibake and other unreadable values are noise, not a genre
    if any(ord(ch) > 0x2000 or ord(ch) in (0xfffd,) for ch in s):
        return UNFILED, None
    if k in _LOOKUP:
        return _LOOKUP[k], s
    # a compound like "Folk-Rock Folk/Rock" or "Alternative & Punk": try each part
    for sep in ('/', '&', ',', ' - ', ' '):
        for part in k.split(sep):
            p = part.strip()
            if p and p in _LOOKUP:
                return _LOOKUP[p], s
    return UNFILED, s

def shelves():
    return list(SHELVES.keys()) + [UNFILED]
