"""Fetch the Billboard Year-End Hot 100 for a range of years from Wikipedia and
store each year as its own JSON list in this folder (billboard-YYYY.json).

Standard library only. Run from anywhere:  python jukebox/charts/billboard.py 2007 2025
"""
import html
import json
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
URL = 'https://en.wikipedia.org/w/api.php?action=parse&format=json&prop=text&page=Billboard_Year-End_Hot_100_singles_of_{year}'
UA = 'MusicMate/1.0 (home jukebox; chart lists)'


class Tables(HTMLParser):
    """Collect every <table class=wikitable> as a list of rows of cell texts."""
    def __init__(self):
        super().__init__()
        self.tables, self.row, self.cell, self.depth = [], None, None, 0
        self.in_table = False
        self.span = 1          # rowspan of the cell being read
        self.carry = {}        # column index -> [text, rows remaining] from rowspans above

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'table' and 'wikitable' in (a.get('class') or ''):
            self.in_table = True
            self.tables.append([])
            self.carry = {}
        elif self.in_table and tag == 'tr':
            self.row = []
        elif self.in_table and tag in ('td', 'th'):
            self.cell = []
            try:
                self.span = int(a.get('rowspan') or 1)
            except ValueError:
                self.span = 1
        elif self.in_table and tag == 'br' and self.cell is not None:
            self.cell.append(' ')

    def handle_endtag(self, tag):
        if tag == 'table' and self.in_table:
            self.in_table = False
        elif tag in ('td', 'th') and self.cell is not None:
            text = re.sub(r'\s+', ' ', ''.join(self.cell)).strip()
            text = re.sub(r'\[\w+\]', '', text).strip()   # footnote markers
            # a cell carried down from a rowspan above occupies this column first
            while len(self.row) in self.carry:
                self.row.append(self.carry[len(self.row)][0])
            if self.span > 1:
                self.carry[len(self.row)] = [text, self.span]
            self.row.append(text)
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            while len(self.row) in self.carry:
                self.row.append(self.carry[len(self.row)][0])
            if self.row:
                self.tables[-1].append(self.row)
            for k in list(self.carry):
                self.carry[k][1] -= 1
                if self.carry[k][1] <= 0:
                    del self.carry[k]
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def fetch(year):
    req = urllib.request.Request(URL.format(year=year), headers={'User-Agent': UA})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)['parse']['text']['*']
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 5:
                raise
            time.sleep(5 * (attempt + 1))   # Wikipedia asked us to slow down


def parse(page_html):
    p = Tables()
    p.feed(page_html)
    best = []
    for t in p.tables:
        rows = [r for r in t if len(r) >= 3 and r[0].rstrip('.').isdigit()]
        if len(rows) > len(best):
            best = rows
    out, last = [], 0
    for r in best:
        rank = int(r[0].rstrip('.'))
        title = html.unescape(r[1]).strip('"“” ')
        artist = html.unescape(r[2])
        out.append({'rank': rank, 'title': title, 'artist': artist})
        last = rank
    return out


def main(first, last):
    for year in range(first, last + 1):
        songs = parse(fetch(year))
        path = HERE / f'billboard-{year}.json'
        # keep enrichment other scripts wrote (MusicBrainz ids) when refreshing the chart itself
        if path.exists():
            old = {s['rank']: s for s in json.loads(path.read_text(encoding='utf-8'))['songs']}
            for s in songs:
                for k, v in old.get(s['rank'], {}).items():
                    s.setdefault(k, v)
        path.write_text(json.dumps({'chart': 'Billboard Year-End Hot 100', 'year': year,
                                    'source': f'https://en.wikipedia.org/wiki/Billboard_Year-End_Hot_100_singles_of_{year}',
                                    'songs': songs}, indent=1, ensure_ascii=False), encoding='utf-8')
        ranks = [s['rank'] for s in songs]
        gaps = sorted(set(range(1, 101)) - set(ranks))
        print(f'{year}: {len(songs)} songs' + (f'  MISSING ranks {gaps}' if gaps else ''))
        time.sleep(2)


if __name__ == '__main__':
    a = [int(x) for x in sys.argv[1:]] or [2007, 2025]
    main(a[0], a[-1])
