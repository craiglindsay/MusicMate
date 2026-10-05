"""Resolve every song in the Billboard year-end lists to a MusicBrainz recording.

    python jukebox/charts/musicbrainz.py            # every billboard-*.json
    python jukebox/charts/musicbrainz.py 2016 2017  # a few years
    python jukebox/charts/musicbrainz.py --weak     # re-search only missing / low-confidence matches

Writes `mbid` (recording) and `artist_mbids` into each song of billboard-YYYY.json, and keeps
the full decision (candidate title, credit, first release, score) in mb-matches.json keyed
"YYYY-rank". Hand fixes go in mb-overrides.json as {"YYYY-rank": "<recording mbid>"}.
Low-confidence and unmatched songs are listed in mb-report.txt.

Standard library only. MusicBrainz asks for <=1 request/second and a descriptive User-Agent;
both respected, as in library/mbresolve.py. Only titles and artist names leave this machine.
"""
import glob
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spotify import NOISE, artists_of, base_title, norm, norm_artist  # noqa: E402

HERE = Path(__file__).resolve().parent
MATCHES = HERE / 'mb-matches.json'
OVERRIDES = HERE / 'mb-overrides.json'
REPORT = HERE / 'mb-report.txt'
UA = 'MusicMate/1.0 (home jukebox; resolving chart songs to recordings)'
RATE = 1.3
CONFIDENT = 70
WS = 'https://musicbrainz.org/ws/2/recording'


def fetch(query, tries=5):
    url = WS + '?' + urllib.parse.urlencode({'query': query, 'fmt': 'json', 'limit': 25})
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {'recordings': []}
            time.sleep(min(30, 3 * (2 ** i)))      # 503 is load shedding, not failure
        except Exception:
            time.sleep(min(30, 3 * (2 ** i)))
    return None


def score(song, year, rec):
    want_title, want_base = norm(song['title']), base_title(song['title'])
    got_title, got_base = norm(rec.get('title', '')), base_title(rec.get('title', ''))
    s = 0
    if got_title == want_title:
        s += 50
    elif got_base == want_base:
        s += 40
    elif want_base and (want_base in got_title or got_base in want_title):
        s += 25
    else:
        return 0
    credit = ' '.join(norm(c.get('name') or c.get('artist', {}).get('name', ''))
                      for c in rec.get('artist-credit', []))
    want = [norm_artist(a) for a in artists_of(song['artist'])]
    tight = credit.replace(' ', '')
    hits = sum(1 for a in want if a and (a in credit or norm(a) in credit or a.replace(' ', '') in tight))
    if not hits:
        return 0
    s += 30 * hits / len(want)
    # a live / remix / instrumental recording of the right song is the wrong recording
    text = rec.get('title', '') + ' ' + rec.get('disambiguation', '')
    extra = set(w.lower() for w in NOISE.findall(text)) - set(w.lower() for w in NOISE.findall(song['title']))
    s -= 25 * len(extra)
    ry = int((rec.get('first-release-date') or '0')[:4] or 0)
    if ry and year - 3 <= ry <= year + 1:
        s += 10
    elif ry and ry > year + 1:
        s -= 15
    rels = rec.get('releases', [])
    kinds = {(r.get('release-group') or {}).get('primary-type') for r in rels}
    if kinds & {'Album', 'Single', 'EP'}:
        s += 5
    s += min(len(rels), 20) / 2           # the canonical recording appears on the most releases
    s += (rec.get('score') or 0) / 10
    if rec.get('video'):
        s -= 10           # some album recordings carry a stray video flag; a nudge, not a veto
    return round(s)


def noisy(song, rec):
    """True when the recording is a live / remix / edit take the chart title does not ask for."""
    text = rec.get('title', '') + ' ' + (rec.get('disambiguation') or '')
    return bool(set(w.lower() for w in NOISE.findall(text)) -
                set(w.lower() for w in NOISE.findall(song['title'])))


def lucene(s):
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r'\\\1', s)


# chart spelling -> the name MusicBrainz indexes, for the query itself (scoring has its own aliases)
SEARCH_AS = {'Pink': 'P!nk', "Soulja Boy Tell 'Em": 'Soulja Boy', 'Kesha': 'Ke$ha', 'Jay-Z': 'Jay-Z'}


def search(song, year):
    primary = artists_of(song['artist'])[0]
    primary = SEARCH_AS.get(primary, primary)
    plain = re.sub(r'[(\[].*?[)\]]', '', song['title']).strip()
    queries = [f'recording:"{lucene(song["title"])}" AND artist:"{lucene(primary)}"']
    if plain != song['title']:
        queries.append(f'recording:"{lucene(plain)}" AND artist:"{lucene(primary)}"')
    queries.append(f'{lucene(plain)} {lucene(primary)}')
    best, best_s, seen, pool = None, 0, set(), {}
    for q in queries:
        time.sleep(RATE)
        res = fetch(q)
        for rec in (res or {}).get('recordings', []):
            if rec['id'] in seen:
                continue
            seen.add(rec['id'])
            sc = score(song, year, rec)
            if sc > best_s:
                best, best_s = rec, sc
            if sc >= 60 and not noisy(song, rec):
                # MusicBrainz keeps one recording per master, so the album version and the
                # single / compilation appearances are often different recordings of one song.
                # Pool every plausible recording's releases so the studio album can be found.
                for r in compact_releases(rec.get('releases', [])):
                    pool.setdefault(r['id'], r)
        if best_s >= CONFIDENT:
            break
    if not best:
        return None
    album = pick_album(list(pool.values()), year)
    if not album or not album['album_type'] in ('Album', 'Album / Soundtrack'):
        # nothing studio in the pool: ask for official album recordings of the song outright
        time.sleep(RATE)
        res = fetch(f'recording:"{lucene(plain)}" AND artist:"{lucene(primary)}" AND primarytype:album '
                    f'AND status:official AND NOT secondarytype:compilation AND NOT secondarytype:live')
        for rec in (res or {}).get('recordings', []):
            if score(song, year, rec) >= 60 and not noisy(song, rec):
                for r in compact_releases(rec.get('releases', [])):
                    pool.setdefault(r['id'], r)
    credits = best.get('artist-credit', [])
    return {'mbid': best['id'], 'score': best_s, 'title': best.get('title'),
            'credit': ''.join((c.get('name') or c['artist']['name']) + (c.get('joinphrase') or '')
                              for c in credits),
            'artist_mbids': [c['artist']['id'] for c in credits if c.get('artist')],
            'first_release': best.get('first-release-date'),
            'disambiguation': best.get('disambiguation') or '',
            'n_releases': len(best.get('releases', [])),
            'releases': list(pool.values())}


# ------------------------------------------------------------------ albums
def compact_releases(rels):
    out = []
    for r in rels:
        rg = r.get('release-group') or {}
        out.append({'id': r['id'], 'title': r.get('title'), 'status': r.get('status'),
                    'date': r.get('date') or '', 'country': r.get('country') or '',
                    'rg': rg.get('id'), 'type': rg.get('primary-type') or '',
                    'secondary': rg.get('secondary-types') or [],
                    'tracks': r.get('track-count') or 0})
    return out


def lookup_releases(mbid):
    """The releases a recording appears on, for matches cached before albums were wanted."""
    url = f'{WS}/{mbid}?' + urllib.parse.urlencode({'inc': 'releases+release-groups', 'fmt': 'json'})
    time.sleep(RATE)
    for i in range(5):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return compact_releases(json.load(r).get('releases', []))
        except Exception:
            time.sleep(min(30, 3 * (2 ** i)))
    return []


_groups = {}


def group_releases(rgid):
    """Every release in a release group, shaped like compact_releases(); for album overrides."""
    if rgid in _groups:
        return _groups[rgid]
    url = f'https://musicbrainz.org/ws/2/release-group/{rgid}?' + urllib.parse.urlencode(
        {'inc': 'releases', 'fmt': 'json'})
    time.sleep(RATE)
    rels = []
    for i in range(5):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                g = json.load(r)
            for r in g.get('releases', []):
                rels.append({'id': r['id'], 'title': r.get('title'), 'status': r.get('status'),
                             'date': r.get('date') or '', 'country': r.get('country') or '',
                             'rg': rgid, 'type': g.get('primary-type') or '',
                             'secondary': g.get('secondary-types') or [], 'tracks': 0})
            break
        except Exception:
            time.sleep(min(30, 3 * (2 ** i)))
    _groups[rgid] = rels
    return rels


def pick_album(rels, year):
    """The album the hit came from: an official studio album released around the chart year,
    else an EP, else the single, else whatever compilation or soundtrack carried it."""
    def rank(r):
        sec = set(r['secondary'])
        if r['type'] == 'Album' and not sec:
            kind = 0
        elif r['type'] == 'Album' and sec <= {'Soundtrack'}:
            kind = 1
        elif r['type'] == 'EP':
            kind = 2
        elif r['type'] == 'Single':
            kind = 3
        elif r['type'] == 'Album' and 'Live' not in sec and 'Remix' not in sec:
            kind = 4          # compilation
        else:
            kind = 5
        y = int(r['date'][:4]) if r['date'][:4].isdigit() else 9999
        late = 0 if y <= year + 1 else 1            # a later reissue or best-of is not "the album"
        early = 0 if y >= year - 3 else 1
        official = 0 if r['status'] == 'Official' else 1
        us = 0 if r['country'] in ('US', 'XW') else 1
        return (kind, late, official, early, abs(y - year), us, -r['tracks'])   # the album nearest the chart year
    rels = [r for r in rels if r.get('rg')]
    if not rels:
        return None
    group = min(rels, key=rank)['rg']
    # the album is the release group; the representative release is its earliest official
    # edition, so a 2007 deluxe reissue does not stand in for a 2006 original
    r = min((x for x in rels if x['rg'] == group),
            key=lambda x: (0 if x['status'] == 'Official' else 1, x['date'][:4] or '9999',
                           0 if x['country'] in ('US', 'XW') else 1, -x['tracks']))
    return {'album': r['title'], 'album_mbid': r['id'], 'album_group_mbid': r['rg'],
            'album_type': r['type'] + (' / ' + ', '.join(r['secondary']) if r['secondary'] else ''),
            'album_date': r['date']}


def load_json(p, default):
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else default


def main(argv):
    weak = '--weak' in argv
    refresh = '--rematch' in argv
    years = [int(a) for a in argv if a.isdigit()]
    files = sorted(glob.glob(str(HERE / 'billboard-*.json')))
    if years:
        files = [f for f in files if int(re.search(r'(\d{4})', Path(f).name).group(1)) in years]
    matches = load_json(MATCHES, {})
    overrides = load_json(OVERRIDES, {})
    report = []
    for f in files:
        path = Path(f)
        chart = json.loads(path.read_text(encoding='utf-8'))
        year, done = chart['year'], 0
        print(f'{year}:', end=' ', flush=True)
        for song in chart['songs']:
            key = f'{year}-{song["rank"]}'
            ov = overrides.get(key, {})
            if isinstance(ov, str):
                ov = {'recording': ov}
            m = matches.get(key)
            if ov.get('recording'):
                m = {'mbid': ov['recording'], 'score': 100, 'title': '(override)', 'credit': '',
                     'artist_mbids': [], 'releases': []}
            else:
                shaky = not m or m['score'] < CONFIDENT or                     (pick_album(m.get('releases', []), year) or {}).get('album_type', '') not in ('Album', 'Album / Soundtrack')
                if m is None or refresh or (weak and shaky):
                    m = search(song, year)
                    matches[key] = m
                    MATCHES.write_text(json.dumps(matches, indent=1, ensure_ascii=False), encoding='utf-8')
            if m and 'releases' not in m:
                m['releases'] = lookup_releases(m['mbid'])
                MATCHES.write_text(json.dumps(matches, indent=1, ensure_ascii=False), encoding='utf-8')
            if m:
                song['mbid'] = m['mbid']
                song['artist_mbids'] = m.get('artist_mbids', [])
            else:
                song.pop('mbid', None)
                song.pop('artist_mbids', None)
                report.append(f'{key:9} NO MATCH   {song["title"]} - {song["artist"]}')
            rels = m['releases'] if m else []
            if ov.get('album_group_mbid'):
                rels = group_releases(ov['album_group_mbid'])
            album = pick_album(rels, year)
            for k in ('album', 'album_mbid', 'album_group_mbid', 'album_type', 'album_date'):
                if album:
                    song[k] = album[k]
                else:
                    song.pop(k, None)
            if not album:
                report.append(f'{key:9} NO ALBUM   {song["title"]} - {song["artist"]}')
            elif album['album_type'] not in ('Album', 'Album / Soundtrack'):
                report.append(f'{key:9} {album["album_type"]:10} {song["title"]} - {song["artist"]}'
                              f'   =>  {album["album"]} [{album["album_date"]}]')
            if m and album:
                done += 1
            if m and m['score'] < CONFIDENT:
                report.append(f'{key:9} score {m["score"]:3} {song["title"]} - {song["artist"]}'
                              f'   =>  {m["title"]} - {m["credit"]} [{m.get("first_release", "")}'
                              f'{", " + m["disambiguation"] if m.get("disambiguation") else ""}]')
            print('.' if m and album else 'x', end='', flush=True)
        # write the enriched list back beside the untouched chart fields
        path.write_text(json.dumps(chart, indent=1, ensure_ascii=False), encoding='utf-8')
        # a flat, tab-separated view of the same list for anything that just wants the ids
        lines = ['rank\ttitle\tartist\talbum\talbum_type\talbum_date\talbum_group_mbid\talbum_mbid\trecording_mbid']
        for s in chart['songs']:
            lines.append('\t'.join(str(s.get(k, '')) for k in
                                   ('rank', 'title', 'artist', 'album', 'album_type', 'album_date',
                                    'album_group_mbid', 'album_mbid', 'mbid')))
        (HERE / f'billboard-{year}-albums.tsv').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print(f' {done}/{len(chart["songs"])}')
    REPORT.write_text('\n'.join(report) + '\n', encoding='utf-8')
    print(f'\n{len(report)} songs to review in {REPORT}')


if __name__ == '__main__':
    main(sys.argv[1:])
