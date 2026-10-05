"""Generated sleeves for the Billboard year folders under Era.

No shop or database has a cover for "the Billboard hits of 1957 as somebody
downloaded them", so one is drawn: the year, large, "BILLBOARD" above it and
"TOP 100 HITS" / "TOP 30 HITS" / "TOP HITS" below, depending on how many songs
the folder holds, on a colour that belongs to the decade. 1000 px JPEG,
written into the art cache under the folder's key and recorded in artcache.db
exactly as a fetched cover would be, so etl.py copies it into the read model
and a later cover fetch leaves it alone (it is a hit). Re-run to redraw.

    python era_covers.py            # draw every year folder
    python era_covers.py --only 1985
"""
import os, re, sys, time, sqlite3, argparse
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import musiclib
from PIL import Image, ImageDraw, ImageFont

CACHE = os.path.join(HERE, 'artcache.db')
ARTDIR = os.path.join(HERE, 'cache', 'art')
FONTS = r'C:\Windows\Fonts'
SIZE = 1000

# background, accent (band + year), ink
DECADE = {
    1940: ('#2b2118', '#d9b26f', '#f3e9d2'),
    1950: ('#123c3f', '#7fd1c4', '#eaf7f4'),
    1960: ('#5a2a0c', '#ffb347', '#fff1dc'),
    1970: ('#4a3a0a', '#e9c46a', '#fff8e1'),
    1980: ('#3a0f3d', '#ff5fa2', '#ffe6f2'),
    1990: ('#0f2a4a', '#6fb1ff', '#e6f0ff'),
    2000: ('#12321f', '#7ee08a', '#e9fbe9'),
}


def label_for(n):
    if n >= 90:
        return 'TOP 100 HITS'
    if 28 <= n <= 40:
        return 'TOP 30 HITS'
    return 'TOP HITS'


def font(name, size):
    return ImageFont.truetype(os.path.join(FONTS, name), size)


def spaced(draw, xy, text, fnt, fill, spacing):
    """Letter-spaced text, centred on xy[0]."""
    widths = [draw.textlength(ch, font=fnt) for ch in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = xy[0] - total / 2
    for ch, w in zip(text, widths):
        draw.text((x, xy[1]), ch, font=fnt, fill=fill)
        x += w + spacing


def draw_cover(year, label):
    bg, acc, ink = DECADE.get(year // 10 * 10, DECADE[2000])
    im = Image.new('RGB', (SIZE, SIZE), bg)
    d = ImageDraw.Draw(im)
    # a ring and a rule, like a record label
    d.ellipse((60, 60, SIZE - 60, SIZE - 60), outline=acc, width=6)
    d.ellipse((90, 90, SIZE - 90, SIZE - 90), outline=acc, width=2)
    spaced(d, (SIZE / 2, 205), 'BILLBOARD', font('georgiab.ttf', 54), ink, 14)
    d.line((300, 290, SIZE - 300, 290), fill=acc, width=3)
    yf = font('georgiab.ttf', 330)
    box = d.textbbox((0, 0), str(year), font=yf)
    yw, yh = box[2] - box[0], box[3] - box[1]
    d.text(((SIZE - yw) / 2 - box[0], (SIZE - yh) / 2 - box[1] + 10), str(year), font=yf, fill=acc)
    d.line((300, SIZE - 290, SIZE - 300, SIZE - 290), fill=acc, width=3)
    spaced(d, (SIZE / 2, SIZE - 270), label, font('georgiab.ttf', 50), ink, 10)
    return im


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', help='one year')
    a = ap.parse_args()
    lib = musiclib.db()
    ac = sqlite3.connect(CACHE, timeout=300)      # artfetch may hold the cache; wait for it
    os.makedirs(ARTDIR, exist_ok=True)
    n = 0
    for folder, count in lib.execute("SELECT folder, COUNT(*) FROM files WHERE src='Era' AND kind='audio' GROUP BY folder ORDER BY folder"):
        m = re.fullmatch(r'Era/(\d{4})', folder)
        if not m or (a.only and m.group(1) != a.only):
            continue
        year = int(m.group(1))
        label = label_for(count)
        im = draw_cover(year, label)
        rel = folder.replace('/', '__') + '.jpg'
        im.save(os.path.join(ARTDIR, rel), 'JPEG', quality=92)
        ac.execute('INSERT OR IGNORE INTO art (folder) VALUES (?)', (folder,))
        ac.execute('UPDATE art SET want=1,hit=1,score=1.0,mb_album=?,mb_artist=?,url=?,local=?,new_w=?,'
                   'reason=NULL,ts=? WHERE folder=?',
                   ('Billboard Top Hits of %d' % year, 'Various Artists', 'generated:era', rel, SIZE, time.time(), folder))
        n += 1
        print('  %s  %-13s %3d songs -> %s' % (year, label, count, rel))
    ac.commit()
    print('drew', n, 'covers')


if __name__ == '__main__':
    main()
