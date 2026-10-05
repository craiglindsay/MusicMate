"""Move the various-artists records that were sitting under VariousArtists into
the collection folders where they belong: Compilations for mixes and
anthologies, Soundtracks for cast recordings and film songs.

Explicitly authorised by the user on 1 Sep 2026 for this list only; the
project rule that the collection folders are not touched otherwise stands.
Each file goes through musiclib._move_to (op "to-collections", undoable) and
the fetched-art cache follows the folder. Dry run by default.

    python tocollections.py            # plan
    python tocollections.py --apply
"""
import os, sys, argparse, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import musiclib

ROOT = musiclib.ROOT
OP = 'to-collections'

MOVES = {   # source folder under VariousArtists -> destination folder
    # authorised 1 Sep 2026 (evening): a swing compilation parked as an artist
    'VariousArtists/Misc Swing/UnknownAlbum': 'Compilations/Misc Swing',
    'VariousArtists/Alan Alda/Free To Be...You And Me': 'Compilations/Free To Be...You And Me',
    'VariousArtists/Blossom Dearie/The Jazz Wedding Album_ First Dances': 'Compilations/The Jazz Wedding Album - First Dances',
    'VariousArtists/Chip Davis/Day Parts_ Romance': 'Compilations/Day Parts - Romance',
    'VariousArtists/Chip Davis/Day Parts_ Romance 2': 'Compilations/Day Parts - Romance 2',
    'VariousArtists/Dennis Rowland/Jazz After Dark': 'Compilations/Jazz After Dark',
    'VariousArtists/Blank & Jones/The Mix Vol. 2': 'Compilations/Blank & Jones - The Mix Vol. 2',
    'VariousArtists/DeepDish/Global Underground 25': 'Compilations/Global Underground 25 - Deep Dish',
    'VariousArtists/DeepDish/Remixes Vol. 2': 'Compilations/Deep Dish - Remixes Vol. 2',
    'VariousArtists/Eminem/DJ Green Lantern-Invasion': 'Compilations/DJ Green Lantern - Invasion',
    'VariousArtists/Ferry Corsten/Ferry Corsten Live at Mayday 4-30-05': 'Compilations/Ferry Corsten - Live at Mayday 2005',
    'VariousArtists/Ferry Corsten/Ferry Corsten Live at Nature One 7-30-04': 'Compilations/Ferry Corsten - Live at Nature One 2004',
    'VariousArtists/Ferry Corsten/Ferry Corsten Trance Energy (2005)': 'Compilations/Ferry Corsten - Trance Energy 2005',
    'VariousArtists/Gypsy Soul/New Flamenco': 'Compilations/Gypsy Soul - New Flamenco',
    'VariousArtists/Paul van Dyk/Politics of Dancing 2': 'Compilations/Paul van Dyk - The Politics of Dancing 2',
    'VariousArtists/Phoenix/Kitsune Tabloid by Phoenix': 'Compilations/Kitsune Tabloid by Phoenix',
    'VariousArtists/Verve, The/Verve Remixed 3': 'Compilations/Verve Remixed 3',
    'VariousArtists/The Book of Mormon (Explicit)': 'Soundtracks/The Book of Mormon',
    'VariousArtists/Disney Classics': 'Soundtracks/Disney Classics',
}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--apply', action='store_true'); a = ap.parse_args()
    conn = musiclib.db()
    moves = []
    for src, dst in MOVES.items():
        rows = conn.execute("SELECT path FROM files WHERE path LIKE ?", (src + '/%',)).fetchall()
        if not rows:
            print('nothing at', src); continue
        for (path,) in rows:
            moves.append((path, dst + path[len(src):], 'collection'))
        if os.path.isdir(os.path.join(ROOT, dst.replace('/', os.sep))):
            print('destination already exists:', dst)
    by = collections.Counter(d.split('/')[0] for _, d, _ in moves)
    print(len(moves), 'files in', len(MOVES), 'albums ->', dict(by))
    n_art = musiclib.art_follow(moves, apply=False)
    print('art cache entries that will follow:', n_art)
    if not a.apply:
        print('dry run'); return
    done = 0
    for s, d, why in moves:
        got = musiclib._move_to(conn, s, d, OP, why)
        conn.execute('UPDATE files SET src=? WHERE path=?', (d.split('/')[0], got))
        done += 1
    conn.commit()
    musiclib.art_follow(moves, apply=True)
    removed = 0
    for src in MOVES:
        p = os.path.join(ROOT, src.replace('/', os.sep))
        while p != os.path.join(ROOT, 'VariousArtists') and os.path.isdir(p) and not os.listdir(p):
            os.rmdir(p); removed += 1; p = os.path.dirname(p)
    print('moved', done, 'files; removed', removed, 'empty folders')


if __name__ == '__main__':
    main()
