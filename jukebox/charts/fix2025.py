"""One-off: write the 2025 overrides (waiting out MusicBrainz outages), then re-run 2025."""
import json, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = 'MusicMate/1.0 (home jukebox; resolving chart songs to recordings)'
OV = HERE / 'mb-overrides.json'

ov = json.loads(OV.read_text(encoding='utf-8'))
KPOP = '59f47b29-50dd-4335-a81b-02297c790208'
fixed = {'2025-13': ('ccd3238f-1db1-4349-90f3-6941a259f2c1', "Morgan Wallen - I'm the Problem"),
         '2025-72': ('ccd3238f-1db1-4349-90f3-6941a259f2c1', "Morgan Wallen - I'm the Problem (I Ain't Comin' Back)"),
         '2025-25': (KPOP, 'KPop Demon Hunters soundtrack (Golden)'),
         '2025-68': (KPOP, "KPop Demon Hunters soundtrack (How It's Done)"),
         '2025-37': ('8e25d6da-5fd3-4f30-9735-7ac39ddb9816', "Benson Boone - American Heart (Sorry I'm Here for Someone Else)"),
         '2025-39': ('bb13ce80-b24b-4a9f-9bca-79f8743dc167', 'Doechii - Anxiety (single)'),
         '2025-59': ('ea885664-bf25-3e52-8ce0-15a84aed4a83', 'Mariah Carey - Merry Christmas'),
         '2025-65': ('5b24e859-1063-3e56-b91e-0c5f6a17354d', 'Wham! - Last Christmas (single)'),
         '2025-67': ('d8699075-d5d1-4b3a-8db3-f23751c4873d', 'Brenda Lee - Merry Christmas from Brenda Lee'),
         '2025-81': ('39509f63-766a-3d6b-9d24-68199925221b', 'Bobby Helms - Jingle Bell Rock (single)'),
         '2025-78': ('ebc84434-b577-41b8-8772-3b859b00a2f0', "Cody Johnson - Leather (deluxe; I'm Gonna Love You)")}
for k, (g, n) in fixed.items():
    ov[k] = {'album_group_mbid': g, 'note': n}
OV.write_text(json.dumps(ov, indent=1, ensure_ascii=False), encoding='utf-8')


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


want = {'2025-14': ('gracie abrams the secret of us', lambda g: 'secret of us' in g['title'].lower() and 'abrams' in credit(g) and g.get('primary-type') == 'Album' and plain(g), 'Gracie Abrams - The Secret of Us'),
        '2025-75': ('doechii alligator bites never heal', lambda g: 'alligator' in g['title'].lower() and 'doechii' in credit(g) and g.get('primary-type') == 'Album', 'Doechii - Alligator Bites Never Heal'),
        '2025-71': ('ella langley hungover', lambda g: g['title'].lower().startswith('hungover') and 'langley' in credit(g) and g.get('primary-type') == 'Album' and plain(g), 'Ella Langley - Hungover'),
        '2025-97': ('don toliver no pole', lambda g: g['title'].lower() == 'no pole' and 'toliver' in credit(g) and g.get('primary-type') == 'Single' and plain(g), 'Don Toliver - No Pole (single)')}
for key, (q, pred, note) in want.items():
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
subprocess.run([sys.executable, '-u', str(HERE / 'musicbrainz.py'), '2025'], cwd=str(HERE))
print('DONE 2025', flush=True)
