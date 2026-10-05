"""Per-song cover art for compilation folders, embedded into the files.

A Billboard year folder keeps its generated sleeve as the ALBUM cover; this
finds the single's or original album's cover for each SONG on it, so Now
Playing can show Debbie Gibson's sleeve while "Shake Your Love" plays. Deezer
is asked by performer and title (keyless, 1000 px covers, 0.3 s pace), iTunes
is the fallback with its 20-a-minute limit respected; a hit must pass the
same performer gate the album fetcher uses (artfetch.artist_ok) and a title
match, and an album result is preferred to a single when both match. The
cover is scaled to 600 px and embedded (embedart.set_art); the library row
gets has_art=1. Every attempt is recorded in artcache.db (table trackart,
keyed by path) so misses are not asked again. Only Era and VariousArtists may
be written to (the project rule); other pools need --force-pool.

    python trackart.py --pool Era                 # plan, first 40 shown
    python trackart.py --pool Era --limit 20 --apply
    python trackart.py --pool Era --apply         # everything without art
    python trackart.py --pool Era --redo --apply  # ask again for recorded misses
"""
import os, re, io, sys, time, sqlite3, argparse, difflib, urllib.request, urllib.parse, json
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import musiclib, merge, artfetch, embedart
from PIL import Image

ROOT = musiclib.ROOT
CACHE = os.path.join(HERE, 'artcache.db')
ALLOWED = ('Era', 'VariousArtists')
RATE = artfetch.RATE
ITUNES_RATE = artfetch.ITUNES_RATE
UA = artfetch.UA
SCHEMA = """
CREATE TABLE IF NOT EXISTS trackart (
  path TEXT PRIMARY KEY, hit INTEGER, score REAL, artist TEXT, title TEXT,
  got_artist TEXT, got_title TEXT, got_album TEXT, url TEXT, reason TEXT, ts REAL
);
"""


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def clean_title(t):
    """What the shops index: the title without featured credits and remix tags."""
    t = re.sub(r'\s*[\(\[].*?[\)\]]', '', t or '')
    t = re.sub(r'\s+(feat\.?|featuring|ft\.?)\s.*$', '', t, flags=re.I)
    return t.strip(' -')


def title_ok(want, got):
    a, b = merge.norm_title(clean_title(want)), merge.norm_title(clean_title(got))
    if not a or not b:
        return 0
    return difflib.SequenceMatcher(None, a, b).ratio()


def deezer_track(artist, title):
    q = 'artist:"%s" track:"%s"' % (artist.replace('"', ''), clean_title(title).replace('"', ''))
    url = 'https://api.deezer.com/search/track?limit=8&q=' + urllib.parse.quote(q)
    try:
        data = (_get(url) or {}).get('data') or []
    except Exception:
        data = []
    if not data:
        # looser: plain words
        url = 'https://api.deezer.com/search/track?limit=8&q=' + urllib.parse.quote(artist + ' ' + clean_title(title))
        try:
            data = (_get(url) or {}).get('data') or []
        except Exception:
            return None
    best = None
    for d in data:
        ga = (d.get('artist') or {}).get('name') or ''
        gt = d.get('title_short') or d.get('title') or ''
        alb = d.get('album') or {}
        if not artfetch.artist_ok(artist, ga):
            continue
        ts = title_ok(title, gt)
        if ts < 0.80:
            continue
        cover = alb.get('cover_xl') or alb.get('cover_big')
        if not cover:
            continue
        # an album beats a single, a closer title beats a looser one, and a
        # hits collection ("The Essential...", "Very Best of") loses to the
        # record the song came from when both are offered
        rt = (alb.get('record_type') or '').lower()
        score = ts + (0.15 if rt == 'album' else 0) + (0.05 if rt == 'ep' else 0)
        if artfetch.WRONG_RECORD.search(alb.get('title') or '') or (rt == 'compile'):
            score -= 0.25
        if best is None or score > best[0]:
            best = (score, {'artist': ga, 'title': gt, 'album': alb.get('title') or '', 'url': cover, 'score': round(ts, 3), 'src': 'deezer'})
    return best[1] if best else None


def itunes_track(artist, title):
    url = 'https://itunes.apple.com/search?' + urllib.parse.urlencode(
        {'term': artist + ' ' + clean_title(title), 'entity': 'song', 'limit': 8})
    try:
        data = (_get(url) or {}).get('results') or []
    except Exception as e:
        if getattr(e, 'code', None) == 403:
            raise
        return None
    for d in data:
        ga, gt = d.get('artistName') or '', d.get('trackName') or ''
        if not artfetch.artist_ok(artist, ga):
            continue
        ts = title_ok(title, gt)
        if ts < 0.80 or not d.get('artworkUrl100'):
            continue
        return {'artist': ga, 'title': gt, 'album': d.get('collectionName') or '',
                'url': d['artworkUrl100'].replace('100x100bb', '600x600bb'), 'score': round(ts, 3), 'src': 'itunes'}
    return None


def fetch_image(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(12_000_000)
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert('RGB')
        if max(im.size) > embedart.EMBED_PX:
            im.thumbnail((embedart.EMBED_PX, embedart.EMBED_PX), Image.LANCZOS)
        buf = io.BytesIO(); im.save(buf, 'JPEG', quality=embedart.QUALITY, optimize=True, progressive=True)
        return buf.getvalue(), im.size[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pool', required=True, help='top-level folder, e.g. Era')
    ap.add_argument('--folder', help='only this library-relative folder')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--redo', action='store_true', help='ask again for recorded misses')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--force-pool', action='store_true')
    a = ap.parse_args()
    if a.pool not in ALLOWED and not a.force_pool:
        print('refusing to write into', a.pool, '-- only', ALLOWED, 'may be modified (use --force-pool if the user has said so)'); return 2
    conn = musiclib.db(); conn.row_factory = sqlite3.Row
    ac = sqlite3.connect(CACHE, timeout=120); ac.executescript(SCHEMA)
    where = "src=? AND kind='audio' AND (has_art IS NULL OR has_art=0)"
    args = [a.pool]
    if a.folder:
        where += ' AND folder LIKE ?'; args.append(a.folder + '%')      # a prefix: Era/196 covers the decade
    rows = conn.execute(f"SELECT path, artist, albumartist, title, filename FROM files WHERE {where} ORDER BY folder, track, filename", args).fetchall()
    done = {r[0]: r[1] for r in ac.execute('SELECT path, hit FROM trackart')}
    todo = [r for r in rows if a.redo or r['path'] not in done]
    if a.limit:
        todo = todo[:a.limit]
    print('%d files without art in %s; %d to look up%s' % (len(rows), a.pool, len(todo), '' if a.apply else ' (dry run)'))
    hits = misses = itunes_dead = 0
    t0 = time.time()
    for i, r in enumerate(todo, 1):
        artist = r['artist'] or ''
        title = r['title'] or os.path.splitext(r['filename'])[0]
        # "Perry Como - Prisoner Of Love" style file names carry both when tags are thin
        if not artist and ' - ' in title:
            artist, title = title.split(' - ', 1)
        if not artist or artist.lower().startswith('various'):
            reason = 'no performer'
            got = None
        else:
            got = deezer_track(artist, title)
            time.sleep(RATE)
            if not got and not itunes_dead:
                try:
                    got = itunes_track(artist, title); time.sleep(ITUNES_RATE)
                except Exception:
                    itunes_dead = 1; print('  iTunes answered 403; not asking it again this run')
            reason = None if got else 'no match'
        if got and a.apply:
            try:
                data, w = fetch_image(got['url'])
                full = os.path.join(ROOT, r['path'].replace('/', os.sep))
                if not embedart.set_art(full, data):
                    got = None; reason = 'embed failed'
                else:
                    conn.execute('UPDATE files SET has_art=1, art_bytes=? WHERE path=?', (len(data), r['path']))
            except Exception as e:
                got = None; reason = 'download failed: %s' % str(e)[:60]
        if got:
            hits += 1
        else:
            misses += 1
        if a.apply or i <= 40:
            print('  %-28s %-34s -> %s' % (artist[:28], title[:34],
                  ('%s / %s (%s %.2f)' % (got['got_album'] if 'got_album' in got else got['album'], got['artist'], got['src'], got['score'])) if got else reason))
        if a.apply:
            ac.execute('INSERT OR REPLACE INTO trackart (path,hit,score,artist,title,got_artist,got_title,got_album,url,reason,ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (r['path'], 1 if got else 0, got['score'] if got else None, artist, title,
                        got['artist'] if got else None, got['title'] if got else None, got['album'] if got else None,
                        got['url'] if got else None, reason, time.time()))
            if i % 25 == 0:
                conn.commit(); ac.commit()
                print('   %d/%d  hits=%d misses=%d  eta %.0fm' % (i, len(todo), hits, misses, (time.time() - t0) / i * (len(todo) - i) / 60))
    conn.commit(); ac.commit()
    print('done: %d hits, %d misses, %.0fs' % (hits, misses, time.time() - t0))
    if not a.apply:
        print('dry run: nothing written')


if __name__ == '__main__':
    sys.exit(main() or 0)
