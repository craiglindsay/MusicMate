"""One-off: wait for MusicBrainz to answer, finish the three 2023 overrides, re-run 2023."""
import json, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = 'MusicMate/1.0 (home jukebox; resolving chart songs to recordings)'
OV = HERE / 'mb-overrides.json'


def rg_search(q):
    url = 'https://musicbrainz.org/ws/2/release-group?' + urllib.parse.urlencode({'query': q, 'fmt': 'json', 'limit': 10})
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


want = {'2023-44': ('fifty fifty the beginning cupid', lambda g: 'cupid' in g['title'].lower() and 'fifty' in credit(g) and g.get('primary-type') == 'EP' and not g.get('secondary-types'), 'Fifty Fifty - The Beginning: Cupid EP'),
        '2023-84': ('wham! last christmas', lambda g: g['title'].lower().startswith('last christmas') and 'wham' in credit(g) and g.get('primary-type') == 'Single' and (g.get('first-release-date') or '9999') < '1990', 'Wham! - Last Christmas (single)'),
        '2023-31': ('gunna a gift & a curse', lambda g: 'gift' in g['title'].lower() and 'curse' in g['title'].lower() and 'setlist' not in g['title'].lower() and 'gunna' in credit(g) and g.get('primary-type') == 'Album' and not g.get('secondary-types'), 'Gunna - A Gift & a Curse')}

ov = json.loads(OV.read_text(encoding='utf-8'))
for key, (q, pred, note) in want.items():
    if key in ov:
        continue
    d = rg_search(q)
    ok = sorted([g for g in d.get('release-groups', []) if pred(g)], key=lambda g: g.get('first-release-date') or '9999')
    for g in ok[:2]:
        print(key, g['title'], '|', credit(g), '|', g.get('primary-type'), g.get('first-release-date'), g['id'], flush=True)
    if ok:
        ov[key] = {'album_group_mbid': ok[0]['id'], 'note': note}
    else:
        print('MISSING', key, [(g['title'], g.get('primary-type'), g.get('first-release-date')) for g in d.get('release-groups', [])[:6]], flush=True)
    OV.write_text(json.dumps(ov, indent=1, ensure_ascii=False), encoding='utf-8')

print('rerun', flush=True)
subprocess.run([sys.executable, '-u', str(HERE / 'musicbrainz.py'), '2023'], cwd=str(HERE))
print('DONE 2023', flush=True)
