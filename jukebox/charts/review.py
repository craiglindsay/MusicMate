"""Review a resolved year: list the picks that are not studio albums, then the distinct albums.

    python jukebox/charts/review.py 2009          # report + distinct list
    python jukebox/charts/review.py 2009 --all    # every song row as well

Writes billboard-YYYY-distinct-albums.tsv (release-group id, album, artist, year, release id).
"""
import collections
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OK = ('Album', 'Album / Soundtrack')


def primary(artist):
    for sep in (' featuring ', ' and ', ' or ', ' with ', ' & ', ' x '):
        artist = artist.split(sep)[0]
    return artist


def main(argv):
    year = int([a for a in argv if a.isdigit()][0])
    chart = json.loads((HERE / f'billboard-{year}.json').read_text(encoding='utf-8'))
    songs = chart['songs']
    kinds = collections.Counter(s.get('album_type', 'none') for s in songs)
    print(f'{year}: {dict(kinds)}')
    print('-- needs a look')
    for s in songs:
        if s.get('album_type', '') not in OK:
            print(f"{s['rank']:3} | {s['title']} - {s['artist']} | {s.get('album', '-')} "
                  f"({s.get('album_date', '')[:4]}) [{s.get('album_type', 'none')}]")
    if '--all' in argv:
        print('-- every row')
        for s in songs:
            print(f"{s['rank']:3} | {s['title']} - {s['artist']} | {s.get('album', '-')} "
                  f"({s.get('album_date', '')[:4]}) | {s.get('album_group_mbid', '-')}")
    seen = {}
    for s in songs:
        g = s.get('album_group_mbid')
        if g and g not in seen:
            seen[g] = (s['album'], primary(s['artist']), s.get('album_date', '')[:4],
                       s.get('album_mbid', ''), s.get('album_type', ''))
    lines = ['album_group_mbid\talbum\tartist\tyear\trelease_mbid']
    lines += [f'{g}\t{a}\t{ar}\t{y}\t{rel}' for g, (a, ar, y, rel, t) in seen.items()]
    (HERE / f'billboard-{year}-distinct-albums.tsv').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'-- {len(seen)} distinct albums')
    for g, (a, ar, y, rel, t) in sorted(seen.items(), key=lambda kv: kv[1][1].lower()):
        tag = '' if t in OK else f'  [{t}]'
        print(f'{g}  {ar} - {a} ({y}){tag}')


if __name__ == '__main__':
    main(sys.argv[1:])
