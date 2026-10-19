"""A QR code for one short text, as SVG -- enough for the karaoke join link.

Byte mode, error correction level M, versions 1-10 (up to 213 bytes). Written
here rather than added as a dependency (the app stays FastAPI + uvicorn +
mutagen + Pillow + numpy). Follows ISO/IEC 18004 the way Project Nayuki's
reference encoder lays it out; checked module-for-module against segno for
every mask and versions 1-10 (17 Oct 2026; segno appends a spare zero byte
after the terminator, which decoders ignore, so it was patched out for that).
"""

# level M, per version: (ec codewords per block, [data codewords of each block])
_BLOCKS = {
    1: (10, [16]), 2: (16, [28]), 3: (26, [44]), 4: (18, [32, 32]),
    5: (24, [43, 43]), 6: (16, [27] * 4), 7: (18, [31] * 4),
    8: (22, [38, 38, 39, 39]), 9: (22, [36, 36, 36, 37, 37]),
    10: (26, [43, 43, 43, 43, 44]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
          7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
_MASKS = [
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_ecc(data, degree):
    div = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            div[j] = _gf_mul(div[j], root)
            if j + 1 < degree:
                div[j] ^= div[j + 1]
        root = _gf_mul(root, 2)
    res = [0] * degree
    for b in data:
        f = b ^ res.pop(0)
        res.append(0)
        for i, c in enumerate(div):
            res[i] ^= _gf_mul(c, f)
    return res


def _codewords(data: bytes):
    for ver in range(1, 11):
        ecl, blocks = _BLOCKS[ver]
        cap = sum(blocks)
        nbits = 4 + (8 if ver < 10 else 16) + 8 * len(data)
        if nbits <= cap * 8:
            break
    else:
        raise ValueError('too long for a version 1-10 QR code')
    bits = [0, 1, 0, 0]
    bits += [(len(data) >> i) & 1 for i in range((8 if ver < 10 else 16) - 1, -1, -1)]
    for b in data:
        bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, cap * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    cw = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(cw) < cap:
        cw.append(pad)
        pad ^= 0xEC ^ 0x11
    parts, at = [], 0
    for n in blocks:
        parts.append(cw[at:at + n])
        at += n
    eccs = [_rs_ecc(p, ecl) for p in parts]
    out = []
    for i in range(max(blocks)):
        out += [p[i] for p in parts if i < len(p)]
    for i in range(ecl):
        out += [e[i] for e in eccs]
    return ver, out


def _penalty(m):
    n = len(m)
    score = 0
    for grid in (m, [list(c) for c in zip(*m)]):
        for row in grid:
            run, prev = 0, None
            for v in row:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run, prev = 1, v
            if run >= 5:
                score += 3 + run - 5
            s = ''.join('1' if v else '0' for v in row)
            for pat in ('10111010000', '00001011101'):
                i = s.find(pat)
                while i >= 0:
                    score += 40
                    i = s.find(pat, i + 1)
    for y in range(n - 1):
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, m))
    score += 10 * (abs(dark * 20 - n * n * 10) // (n * n))
    return score


def matrix(text: str, mask: int = None):
    """The QR code for `text` as rows of booleans (True = dark), no quiet zone."""
    ver, data = _codewords(text.encode('utf-8'))
    n = ver * 4 + 17
    m = [[False] * n for _ in range(n)]
    fn = [[False] * n for _ in range(n)]

    def put(x, y, dark):
        m[y][x] = dark
        fn[y][x] = True

    for i in range(n):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < n and 0 <= y < n:
                    put(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    al = _ALIGN[ver]
    for i, ax in enumerate(al):
        for j, ay in enumerate(al):
            if (i, j) in ((0, 0), (0, len(al) - 1), (len(al) - 1, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def format_bits(mk):
        d = mk                                   # level M is 00
        r = d
        for _ in range(10):
            r = (r << 1) ^ ((r >> 9) * 0x537)
        b = ((d << 10) | r) ^ 0x5412
        bit = lambda i: (b >> i) & 1 == 1
        for i in range(6):
            put(8, i, bit(i))
        put(8, 7, bit(6)); put(8, 8, bit(7)); put(7, 8, bit(8))
        for i in range(9, 15):
            put(14 - i, 8, bit(i))
        for i in range(8):
            put(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            put(8, n - 15 + i, bit(i))
        put(8, n - 8, True)

    format_bits(0)                               # reserve the area before data goes in
    if ver >= 7:
        r = ver
        for _ in range(12):
            r = (r << 1) ^ ((r >> 11) * 0x1F25)
        b = (ver << 12) | r
        for i in range(18):
            a, c = n - 11 + i % 3, i // 3
            put(a, c, (b >> i) & 1 == 1)
            put(c, a, (b >> i) & 1 == 1)

    i, right = 0, n - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(n):
            for j in range(2):
                x = right - j
                y = n - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not fn[y][x] and i < len(data) * 8:
                    m[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2

    def masked(mk):
        f = _MASKS[mk]
        return [[m[y][x] ^ (not fn[y][x] and f(x, y)) for x in range(n)] for y in range(n)]

    if mask is None:
        mask = min(range(8), key=lambda k: _penalty(masked(k)))
    m = masked(mask)                             # put() below writes into this copy
    format_bits(mask)
    return m


def svg(text: str, dark: str = '#000', light: str = '#fff') -> str:
    """The QR code as a standalone SVG with the four-module quiet zone."""
    m = matrix(text)
    n = len(m) + 8
    path = []
    for y, row in enumerate(m):
        x = 0
        while x < len(row):
            if row[x]:
                s = x
                while x < len(row) and row[x]:
                    x += 1
                path.append(f'M{s + 4} {y + 4}h{x - s}v1h-{x - s}z')
            else:
                x += 1
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" '
            f'shape-rendering="crispEdges"><rect width="{n}" height="{n}" fill="{light}"/>'
            f'<path fill="{dark}" d="{"".join(path)}"/></svg>')
