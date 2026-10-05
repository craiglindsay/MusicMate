"""Build one Spotify playlist per Billboard year-end chart stored in this folder.

    python jukebox/charts/spotify.py                 # every billboard-*.json
    python jukebox/charts/spotify.py 2016 2017       # a few years
    python jukebox/charts/spotify.py --dry-run 2016  # match only, create nothing
    python jukebox/charts/spotify.py --rematch 2016  # ignore cached search results
    python jukebox/charts/spotify.py --weak          # re-search only missing / low-confidence matches

Setup, once:
  * create an app at https://developer.spotify.com/dashboard with the redirect URI
    http://127.0.0.1:8888/callback and put its Client ID in <repo>/.spotify_client
  * the first run opens a browser for consent; the token is cached in <repo>/.spotify_token

Standard library only. Uses the PKCE flow, so no client secret is needed.

Files kept beside the charts:
  spotify-matches.json    every search decision, keyed "YYYY-rank", so reruns are cheap
  spotify-overrides.json  hand fixes: {"2016-2": "spotify:track:..."} or a track URL; wins over search
  spotify-report.txt      songs matched with low confidence or not at all, for review
"""
import base64
import glob
import hashlib
import http.server
import json
import re
import secrets
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CLIENT_FILE = REPO / '.spotify_client'
TOKEN_FILE = REPO / '.spotify_token'
MATCHES = HERE / 'spotify-matches.json'
OVERRIDES = HERE / 'spotify-overrides.json'
REPORT = HERE / 'spotify-report.txt'
REDIRECT = 'http://127.0.0.1:8888/callback'
SCOPES = 'playlist-modify-public playlist-modify-private playlist-read-private user-read-private'
API = 'https://api.spotify.com/v1'
PLAYLIST_NAME = 'Billboard Year-End Hot 100: {year}'
CONFIDENT = 70      # scores below this go in the report but are still added
PACE = 1.0          # seconds between searches; the app's daily quota ran out at full speed


# ------------------------------------------------------------------ auth (PKCE)
def client_id():
    for f in (CLIENT_FILE, REPO / '.spotify_key'):
        if f.exists():
            # the file may hold labels and the secret too; the ID is the first 32-hex token
            ids = re.findall(r'[0-9a-f]{32}', f.read_text(encoding='utf-8'))
            if ids:
                return ids[0]
    sys.exit(f'put your Spotify app Client ID in {CLIENT_FILE}')


class _Catch(http.server.BaseHTTPRequestHandler):
    code = None

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catch.code = q.get('code', [None])[0]
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        msg = ('MusicMate is connected to Spotify. You can close this tab.' if _Catch.code
               else 'Spotify said: ' + q.get('error', ['?'])[0])
        self.wfile.write(f'<h2 style="font-family:sans-serif">{msg}</h2>'.encode())

    def log_message(self, *a):
        pass


def _post_token(data):
    req = urllib.request.Request('https://accounts.spotify.com/api/token',
                                 data=urllib.parse.urlencode(data).encode(),
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.load(r)
    tok['expires_at'] = time.time() + tok.get('expires_in', 3600) - 60
    if 'refresh_token' not in tok and TOKEN_FILE.exists():
        tok['refresh_token'] = json.loads(TOKEN_FILE.read_text())['refresh_token']
    TOKEN_FILE.write_text(json.dumps(tok))
    return tok


def login():
    cid = client_id()
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
    url = 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode({
        'client_id': cid, 'response_type': 'code', 'redirect_uri': REDIRECT,
        'scope': SCOPES, 'code_challenge_method': 'S256', 'code_challenge': challenge})
    srv = http.server.HTTPServer(('127.0.0.1', 8888), _Catch)
    print('Opening your browser to approve MusicMate. If nothing opens, visit:\n ', url)
    webbrowser.open(url)
    while _Catch.code is None:
        srv.handle_request()
    srv.server_close()
    return _post_token({'client_id': cid, 'grant_type': 'authorization_code', 'code': _Catch.code,
                        'redirect_uri': REDIRECT, 'code_verifier': verifier})


def token():
    if TOKEN_FILE.exists():
        tok = json.loads(TOKEN_FILE.read_text())
        if tok.get('expires_at', 0) > time.time():
            return tok['access_token']
        if tok.get('refresh_token'):
            try:
                return _post_token({'client_id': client_id(), 'grant_type': 'refresh_token',
                                    'refresh_token': tok['refresh_token']})['access_token']
            except urllib.error.HTTPError:
                pass
    return login()['access_token']


# ------------------------------------------------------------------ api
def call(method, path, params=None, body=None):
    url = path if path.startswith('http') else API + path
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    for attempt in range(8):
        req = urllib.request.Request(url, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Authorization': 'Bearer ' + token(),
                                              'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get('Retry-After', '2'))
                if wait > 600:
                    # a new app's daily quota is spent; sleeping would just look hung
                    until = time.strftime('%a %H:%M', time.localtime(time.time() + wait))
                    sys.exit(f'\nSpotify quota exhausted; retry after {until} '
                             f'({wait // 3600}h {wait % 3600 // 60}m). Progress is saved.')
                time.sleep(wait + 1)
                continue
            if e.code == 401 and attempt == 0:
                if TOKEN_FILE.exists():
                    TOKEN_FILE.unlink()
                continue
            raise RuntimeError(f'{method} {url} -> {e.code}: {e.read()[:300]!r}') from None
    raise RuntimeError('gave up after repeated 429s: ' + url)


# ------------------------------------------------------------------ matching
SEP = re.compile(r'\s+(?:featuring|feat\.?|ft\.?|with|and|&|x|vs\.?|/)\s+|,\s+', re.I)
NOISE = re.compile(r'\b(karaoke|tribute|cover|originally performed|made famous|in the style of|'
                   r'instrumental|sped[- ]up|slowed|8d|nightcore|remix|live|acoustic|version|edit|'
                   r'mix|re-?recorded|rerecording)\b', re.I)
# credited names that contain a separator word and must not be split
KEEP = ['Earth, Wind & Fire', 'Tyler, the Creator', 'Florence and the Machine', 'Florence + the Machine',
        'Macklemore & Ryan Lewis', 'Dan + Shay', 'Brooks & Dunn', 'Chloe x Halle', 'Lil Nas X',
        'Simon & Garfunkel', 'Hall & Oates', 'Mumford & Sons', 'Marshmello and Bastille', 'Rae Sremmurd',
        'Sugarland', 'Big & Rich', 'Montgomery Gentry', 'Kool & the Gang', 'Zac Brown Band',
        'Hootie & the Blowfish', 'Bell Biv DeVoe', 'Salt-N-Pepa', 'Panic! at the Disco',
        'Portugal. The Man', 'Silk Sonic', 'Fall Out Boy', 'Gym Class Heroes', 'Plain White T\'s',
        'Boys Like Girls', 'Timbaland', 'Cobra Starship', 'Owl City', 'Cage the Elephant',
        'Florida Georgia Line', 'Maroon 5', 'X Ambassadors', 'Fifth Harmony', 'Ella Mai',
        'Of Monsters and Men', 'Tones and I', 'Rage Against the Machine', 'Ayo & Teo', 'Bill & Ted']


def artists_of(s):
    """'Drake featuring Wizkid and Kyla' -> ['Drake', 'Wizkid', 'Kyla']"""
    guard = {}
    for i, k in enumerate(KEEP):
        if k.lower() in s.lower():
            tag = f'\x00{i}\x00'
            s = re.sub(re.escape(k), tag, s, flags=re.I)
            guard[tag] = k
    parts = []
    for p in SEP.split(s):
        p = p.strip()
        for tag, k in guard.items():
            p = p.replace(tag, k)
        if p:
            parts.append(p)
    return parts or [s.strip()]


# chart spelling -> how Spotify credits the artist, compared after norm()
ALIAS = {'pink': 'p nk', 'soulja boy tell em': 'soulja boy', 'the dey': 'the d e y',
         'ty dolla sign': 'ty dolla sign', 'jay z': 'jay z', 'kesha': 'kesha', 'a ap rocky': 'a ap rocky'}


def norm(s):
    s = ''.join(' ' if unicodedata.category(ch) == 'Pd' else ch for ch in s)   # every kind of dash
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower()
    s = s.replace('&', 'and').replace('$', 's')
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


def norm_artist(s):
    n = norm(s)
    return ALIAS.get(n, n)


def base_title(t):
    """Drop a parenthetical or dash suffix for looser comparison."""
    return norm(re.sub(r'\s*[\(\[].*?[\)\]]|\s+-\s+.*$', '', t))


def score(song, year, tr):
    want_title, want_base = norm(song['title']), base_title(song['title'])
    got_title, got_base = norm(tr['name']), base_title(tr['name'])
    s = 0
    if got_title == want_title:
        s += 50
    elif got_base == want_base:
        s += 40
    elif want_base and (want_base in got_title or got_base in want_title):
        s += 25
    else:
        return 0
    want = [norm_artist(a) for a in artists_of(song['artist'])]
    got = ' '.join(norm(a['name']) for a in tr['artists'])
    hits = sum(1 for a in want if a and (a in got or norm(a) in got))
    if not hits:
        return 0
    s += 30 * hits / len(want)
    # noise words in the Spotify title that are not in the chart title are a bad sign
    extra = set(w.lower() for w in NOISE.findall(tr['name'])) - \
        set(w.lower() for w in NOISE.findall(song['title']))
    s -= 25 * len(extra)
    if tr['album'].get('album_type') == 'compilation':
        s -= 5
    ry = int((tr['album'].get('release_date') or '0')[:4] or 0)
    if ry and year - 3 <= ry <= year + 1:
        s += 10
    elif ry and ry > year + 1:
        s -= 10           # a later re-release or re-recording
    s += min(tr.get('popularity', 0), 80) / 8
    return round(s)


def search(song, year):
    primary = artists_of(song['artist'])[0]
    plain = re.sub(r'[(\[].*?[)\]]', '', song['title']).strip()
    queries = [f'track:"{song["title"]}" artist:"{primary}"',
               f'{song["title"]} {primary}']
    if plain != song['title']:
        queries.append(f'{plain} {primary}')
    best, best_s, seen = None, 0, set()
    for q in queries:
        time.sleep(PACE)
        res = call('GET', '/search', {'q': q, 'type': 'track', 'limit': 10, 'market': 'from_token'})
        for tr in res.get('tracks', {}).get('items', []):
            if tr['id'] in seen:
                continue
            seen.add(tr['id'])
            sc = score(song, year, tr)
            if sc > best_s:
                best, best_s = tr, sc
        if best_s >= CONFIDENT:
            break           # one query per song is the norm; extras only for shaky matches
    if not best:
        return None
    return {'uri': best['uri'], 'score': best_s, 'name': best['name'],
            'artists': ', '.join(a['name'] for a in best['artists']),
            'album': best['album']['name'], 'released': best['album'].get('release_date')}


def load_json(p, default):
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else default


def as_uri(v):
    m = re.search(r'track[/:]([A-Za-z0-9]{22})', v)
    return f'spotify:track:{m.group(1)}' if m else None


# ------------------------------------------------------------------ playlists
def my_playlists():
    out, url = {}, '/me/playlists?limit=50'
    while url:
        page = call('GET', url)
        for p in page['items']:
            out[p['name']] = p['id']
        url = page.get('next')
    return out


def ensure_playlist(existing, name, desc):
    if name in existing:
        call('PUT', f'/playlists/{existing[name]}', body={'description': desc})
        return existing[name]
    p = call('POST', '/me/playlists', body={'name': name, 'public': True, 'description': desc})
    existing[name] = p['id']
    return p['id']


def set_items(pid, uris):
    call('PUT', f'/playlists/{pid}/items', body={'uris': uris[:100]})
    for i in range(100, len(uris), 100):
        call('POST', f'/playlists/{pid}/items', body={'uris': uris[i:i + 100]})


# ------------------------------------------------------------------ main
def main(argv):
    dry = '--dry-run' in argv
    refresh = '--rematch' in argv
    weak = '--weak' in argv
    years = [int(a) for a in argv if a.isdigit()]
    files = sorted(glob.glob(str(HERE / 'billboard-*.json')))
    if years:
        files = [f for f in files if int(re.search(r'(\d{4})', Path(f).name).group(1)) in years]
    matches = load_json(MATCHES, {})
    overrides = load_json(OVERRIDES, {})
    report = []
    existing = None if dry else my_playlists()

    for f in files:
        chart = json.loads(Path(f).read_text(encoding='utf-8'))
        year, uris = chart['year'], []
        print(f'{year}:', end=' ', flush=True)
        for song in chart['songs']:
            key = f'{year}-{song["rank"]}'
            if key in overrides:
                m = {'uri': as_uri(overrides[key]), 'score': 100, 'name': '(override)', 'artists': ''}
            else:
                m = matches.get(key)
                if m is None or refresh or (weak and (not m or m['score'] < CONFIDENT)):
                    m = search(song, year)
                    matches[key] = m
                    MATCHES.write_text(json.dumps(matches, indent=1, ensure_ascii=False), encoding='utf-8')
            if not m or not m.get('uri'):
                report.append(f'{key:9} NO MATCH   {song["title"]} - {song["artist"]}')
                print('x', end='', flush=True)
                continue
            if m['score'] < CONFIDENT:
                report.append(f'{key:9} score {m["score"]:3} {song["title"]} - {song["artist"]}'
                              f'   =>  {m["name"]} - {m["artists"]} [{m.get("album", "")}, {m.get("released", "")}]')
            uris.append(m['uri'])
            print('.', end='', flush=True)
        print(f' {len(uris)}/100')
        if not dry:
            pid = ensure_playlist(existing, PLAYLIST_NAME.format(year=year),
                                  f'Billboard Year-End Hot 100 for {year}, in chart order. '
                                  f'Source: Wikipedia. Built by MusicMate.')
            set_items(pid, uris)
            print(f'   -> https://open.spotify.com/playlist/{pid}')

    REPORT.write_text('\n'.join(report) + '\n', encoding='utf-8')
    print(f'\n{len(report)} songs to review in {REPORT}')


if __name__ == '__main__':
    main(sys.argv[1:])
