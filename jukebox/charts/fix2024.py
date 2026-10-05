"""One-off: wait for MusicBrainz to answer, finish the 2024 overrides, re-run 2024."""
import json, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = 'MusicMate/1.0 (home jukebox; resolving chart songs to recordings)'
OV = HERE / 'mb-overrides.json'


def rg_search(q):
    url = 'https://musicbrainz.org/ws/2/release-group?' + urllib.parse.urlencode({'query': q, 'fmt': 'json', 'limit': 12})
    for i in range(30):
        time.sleep(3)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'}), timeout=40) as r:
                return json.load(r)
        except Exception as e:
            print('  retry', getattr(e, 'code', e), flush=True)
            time.sleep(min(120, 10 * (i + 1)))
    return {}


def credit(g):
    return ''.join(x.get('name', '') for x in g.get('artist-credit', [])).lower()


def plain(g):
    return not g.get('secondary-types')


want = {'2024-37': ('twisters the album 2024 soundtrack', lambda g: 'twisters' in g['title'].lower() and 'Soundtrack' in (g.get('secondary-types') or []) and 'score' not in g['title'].lower(), "Twisters: The Album (Luke Combs - Ain't No Love in Oklahoma)"),
        '2024-38': ('releasegroup:"Vultures 1"', lambda g: g['title'].lower().startswith('vultures 1') and g.get('primary-type') == 'Album' and plain(g), '¥$ - Vultures 1'),
        '2024-8': ('releasegroup:Coyote AND artist:"Tommy Richman"', lambda g: g['title'].lower() == 'coyote' and 'richman' in credit(g) and g.get('primary-type') == 'Album' and plain(g), 'Tommy Richman - Coyote'),
        '2024-80': ('artist:4batz', lambda g: 'st4r' in g['title'].lower() and '4batz' in credit(g), '4Batz - u made me a st4r')}

ov = json.loads(OV.read_text(encoding='utf-8'))
for key, (q, pred, note) in want.items():
    if key in ov:
        continue
    d = rg_search(q)
    ok = sorted([g for g in d.get('release-groups', []) if pred(g)], key=lambda g: g.get('first-release-date') or '9999')
    for g in ok[:2]:
        print(key, g['title'], '|', credit(g), '|', g.get('primary-type'), g.get('secondary-types'), g.get('first-release-date'), g['id'], flush=True)
    if ok:
        ov[key] = {'album_group_mbid': ok[0]['id'], 'note': note}
    else:
        print('MISSING', key, [(g['title'], g.get('primary-type'), g.get('first-release-date')) for g in d.get('release-groups', [])[:6]], flush=True)
    OV.write_text(json.dumps(ov, indent=1, ensure_ascii=False), encoding='utf-8')

print('rerun', flush=True)
subprocess.run([sys.executable, '-u', str(HERE / 'musicbrainz.py'), '2024'], cwd=str(HERE))
print('DONE 2024', flush=True)
