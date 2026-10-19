"""D-pad navigation regression check for the jukebox UI.

Drives the real page in headless Chromium with real key presses (Playwright)
and reads where the focus ring (`.foc`) lands after each one. Every case here
was validated by hand on 3 Sep 2026 while reviewing the TV navigation model;
re-run it after any change to tvMove / tvCandidates / route / the sheets.

    pip install playwright && python -m playwright install chromium
    python tools/tvnav_check.py                 # against http://127.0.0.1
    python tools/tvnav_check.py --base http://127.0.0.1:8730 --only nowbar prefs
    python tools/tvnav_check.py --list

A step is a key name ('ArrowDown', 'Enter', 'Escape', 'o', '/', 'PageDown'),
('wait', ms), or ('until', key, needle, max) which presses the key until the
ring description contains needle. Each step may carry an expectation: a
substring of the ring description, or '=N' meaning "the same element as after
step N" (0 is the start). The description is
    LAYER #id.class.list @x,y wxh | text
where LAYER is one of chrome, view, BAR, NP, SO, KAR, SHEET.

Album 1312 (Rumours) and track 10529 are read-model ids and change with every
etl.py rebuild; the cases only need SOME album with ten tracks and SOME track,
so pass --album / --track if those ids have moved.
"""
import sys, argparse
sys.stdout.reconfigure(encoding='utf-8')

FOC = """() => { const e = document.querySelector('.foc'); if (!e) return 'none';
  const r = e.getBoundingClientRect();
  const lay = e.closest('.sheet.on') ? 'SHEET' : e.closest('#np') ? 'NP' : e.closest('#so') ? 'SO' : e.closest('#kar') ? 'KAR' : e.closest('#nowbar') ? 'BAR' : e.closest('#view') ? 'view' : 'chrome';
  return lay + ' ' + (e.id ? '#' + e.id : '') + '.' + e.className.replace(/\\s+/g,'.') + ' @' + Math.round(r.left) + ',' + Math.round(r.top) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height) + ' | ' + (e.textContent||'').trim().replace(/\\s+/g,' ').slice(0,40); }"""

D, U, L, R = 'ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight'


def cases(album, track):
    A = '#/album/%d' % album
    NP = '?np=%d' % track
    return {
        'load-section': dict(url='#/albums', settle=900, start='pill.f.on', steps=[]),
        'albums-grid': dict(url='#/albums', steps=[
            (D, 'view'), (D, 'view .tile'), (R, 'view .tile'), (D, 'view .tile'), (L, 'view .tile'), (U, '=2')]),
        'open-album-back-to-tile': dict(url='#/albums', steps=[
            (D, None), (D, 'tile'), (R, 'tile'), ('Enter', None), (('wait', 900), 'view .spmain'), (D, 'view'),
            ('Escape', None), (('wait', 900), '=3')]),
        'home-shelf-leftmost': dict(url='#/', steps=[
            (D, 'view'), (D, 'view'), (D, 'view .tile @65'), (R, 'view .tile'), (R, 'view .tile'), (D, 'view .tile @65'), (U, '=3')]),
        'artists-rail': dict(url='#/artists', steps=[
            (D, 'view .tile'), (('until', R, 'chrome .rl', 8), 'chrome .rl'), (D, 'chrome .rl'), (D, 'chrome .rl'), (L, 'view .tile')]),
        'track-row-buttons-and-sheet': dict(url=A, steps=[
            (('until', D, 'view .tr', 6), 'view .tr'), (R, '.trmic'), (R, '.tradd'), ('Enter', None), (('wait', 500), 'SHEET .outrow'),
            (D, 'SHEET .outrow'), (D, 'SHEET .outrow'), ('Escape', None), (('wait', 400), '=3'), (L, '.trmic'), (L, '=1')]),
        'output-sheet': dict(url='#/albums', steps=[
            (D, None), (D, 'view .tile'), ('o', None), (('waitfor', 8000), 'SHEET .outrow.active'), (D, 'SHEET .outrow'),
            (('until', D, 'SHEET #outapply', 8), 'SHEET #outapply'), ('Escape', None), (('wait', 400), '=2')]),
        'now-bar': dict(url=NP, settle=1200, steps=[
            ('Escape', None), (('wait', 600), 'chrome .pill'), (('until', D, 'BAR #nowbar', 14), 'BAR #nowbar'),
            (R, 'BAR #outbtn'), (R, 'BAR #bprev'), (R, 'BAR #bplay'), (R, 'BAR #bnext'), (R, 'BAR #bnext'),
            (L, 'BAR #bplay'), (L, 'BAR #bprev'), (L, 'BAR #outbtn'), (L, 'BAR #nowbar'), (U, 'view')]),
        # opened by the URL before TV mode settled, so the first arrow steps into the layer
        'now-playing': dict(url=NP, settle=1200, steps=[
            (D, 'NP #npback'), (D, 'NP #tab-lyr'), (R, 'NP #tab-q'), (D, 'NP #seek'), (D, 'NP #npshuf'), (R, 'NP #npprev'), (R, 'NP #npplay'), (R, 'NP #npnext'), (R, 'NP #nprep'),
            (U, 'NP #seek'), (R, 'NP #seek'), ('Escape', None), (('wait', 500), 'chrome .pill')]),
        'karaoke-page': dict(url='#/karaoke', settle=1600, steps=[
            (D, 'view .karart'), (R, 'view .krow'), (D, 'view .krow'), (U, '=2'), (L, 'view .karart'), (D, 'view .karart'), (U, '=1'), (U, 'view')]),
        'karaoke-screen': dict(url='#/karaoke', settle=1600, steps=[
            (D, 'view .karart'), (R, 'view .krow'), ('Enter', None), (('wait', 3000), 'KAR #karback'), (D, 'KAR #karmapwrap'),
            (D, 'KAR #karplay'), (R, 'KAR'), (U, 'KAR #karmapwrap'), (U, 'KAR #karback'), ('Escape', None), (('wait', 900), '=2')]),
        'karaoke-artist-back': dict(url='#/karaoke', settle=1600, steps=[
            (D, 'view .karart'), (D, 'view .karart'), ('Enter', None), (('wait', 2000), 'view .spmain'), ('Escape', None), (('wait', 3500), '=2')]),
        'search': dict(url='#/artists', steps=[
            (D, 'view .tile'), (D, 'view .tile'), ('/', None), (('wait', 600), 'SO .kk'), (D, 'SO .kk'), (R, 'SO .kk'), ('Escape', None), (('wait', 400), '=2')]),
        'search-close': dict(url='#/artists', steps=[
            (D, 'view .tile'), ('/', None), (('wait', 600), 'SO .kk'), (U, 'SO .kk'), (U, 'SO soclose'), (R, 'SO soclose'),
            (L, 'SO .kk'), (U, 'SO soclose'), (D, 'SO .kk'), (('until', D, 'SO .kk mode', 6), 'SO .kk mode'), ('Enter', 'SO .kk'),
            ('Escape', None), (('wait', 400), 'view .tile')]),
        'page-down': dict(url='#/albums', steps=[
            (D, None), (D, 'view .tile'), ('PageDown', None), (('wait', 500), 'view .tile'), (D, 'view .tile'), ('PageUp', None), (('wait', 500), 'view .tile')]),
        'pill-enter': dict(url='#/', steps=[
            (R, 'chrome .pill'), (R, 'chrome .pill'), ('Enter', None), (('wait', 900), 'chrome .pill.f.on'), (D, 'view'), ('Escape', None), (('wait', 900), 'chrome .pill')]),
        'prefs': dict(url='?prefs=1', vh=720, steps=[
            (D, 'SHEET .bdopt'), (R, 'SHEET .bdopt'), (R, 'SHEET .bdopt'), (D, 'SHEET .bdopt'), (L, 'SHEET .bdopt'), ('Enter', 'foc.on'),
            # rows as of 18 Oct 2026: a 22-tile backdrop grid (eight rows; Ridges x3, Spectrum, Tunnel, Synthwave, Skyline, Planet, Helix 18 Oct; Synthwave Cover, Currents 19 Oct), Motion, Behind the karaoke lyrics, idle, keyboard, size, two links
            (('until', D, 'SHEET Full', 11), 'SHEET Full'), (R, 'SHEET Calm'), ('Enter', 'foc.on'), (D, 'SHEET Same as music'), (D, 'SHEET min'), (D, 'SHEET QWERTY'), (D, 'SHEET'), (D, 'SHEET .preflink'), (D, 'SHEET .preflink'),
            (U, 'SHEET .preflink'), (('until', U, 'SHEET .bdopt', 8), 'SHEET .bdopt'), ('Escape', None), (('wait', 400), 'chrome .pill')]),
    }


def describe(page):
    return page.evaluate(FOC)


def matches(desc, expect, history):
    if expect is None:
        return True
    if expect.startswith('='):
        want = history[int(expect[1:])]
        return desc.split(' @')[0] == want.split(' @')[0] and desc.split('| ')[-1] == want.split('| ')[-1]
    return all(tok in desc for tok in expect.split(' '))


def run_case(browser, base, name, c, verbose):
    url = (base + '/?tv=1' + c['url']) if c['url'].startswith('#') else (base + '/' + c['url'] + '&tv=1')
    page = browser.new_page(viewport={'width': 1280, 'height': c.get('vh', 800)})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(url, wait_until='networkidle')
    page.wait_for_timeout(c.get('settle', 900))
    history = [describe(page)]
    fails = []
    if c.get('start') and not matches(history[0], c['start'], history):
        fails.append('start: expected %r, got %r' % (c['start'], history[0]))
    for i, (step, expect) in enumerate(c['steps'], 1):
        if isinstance(step, tuple) and step[0] == 'wait':
            page.wait_for_timeout(step[1])
        elif isinstance(step, tuple) and step[0] == 'waitfor':
            # poll until the expectation holds (a sheet that waits on the network), up to step[1] ms
            for _n in range(max(1, step[1] // 200)):
                if matches(describe(page), expect, history):
                    break
                page.wait_for_timeout(200)
        elif isinstance(step, tuple) and step[0] == 'until':
            _, key, needle, mx = step
            for _n in range(mx):
                page.keyboard.press(key); page.wait_for_timeout(300)
                if all(tok in describe(page) for tok in needle.split(' ')):
                    break
        else:
            page.keyboard.press(step); page.wait_for_timeout(300)
        desc = describe(page)
        history.append(desc)
        ok = matches(desc, expect, history)
        if verbose or not ok:
            print('     %2d %-22s %s %s' % (i, step if isinstance(step, str) else str(step), 'ok ' if ok else 'XX ', desc))
        if not ok:
            fails.append('step %d %s: expected %r, got %r' % (i, step, expect, desc))
    page.close()
    if errors:
        fails.append('page errors: ' + '; '.join(errors)[:300])
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='http://127.0.0.1')
    ap.add_argument('--album', type=int, default=1312)
    ap.add_argument('--track', type=int, default=10529)
    ap.add_argument('--only', nargs='*')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args()
    allc = cases(a.album, a.track)
    if a.list:
        for k in allc: print(k)
        return 0
    from playwright.sync_api import sync_playwright
    names = a.only or list(allc)
    # A karaoke screen opened here takes over the phones following the room
    # (the newest screen owns them), so while a real one is singing that case
    # is skipped rather than run (17 Oct 2026).
    import json, urllib.request
    try:
        live = json.load(urllib.request.urlopen(a.base + '/api/karaoke/live', timeout=3)).get('live')
    except Exception:
        live = None
    if live and 'karaoke-screen' in names:
        print('SKIP karaoke-screen (a karaoke screen is live: %s)' % (live.get('title') or live.get('id')))
        names = [n for n in names if n != 'karaoke-screen']
    bad = 0
    with sync_playwright() as p:
        b = p.chromium.launch()
        for name in names:
            fails = run_case(b, a.base, name, allc[name], a.verbose)
            print('%s %s' % ('PASS' if not fails else 'FAIL', name))
            for f in fails:
                print('     ', f)
            bad += bool(fails)
        b.close()
    print('%d of %d cases passed' % (len(names) - bad, len(names)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
