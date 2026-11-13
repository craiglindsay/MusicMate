"""Playback diagnostics (3 Nov 2026): what each device asked for and what it saw.

A phone on plane Wi-Fi played the DJ's spoken links but never a song, and the
only evidence was a few lines the page managed to post. So both ends now keep
a log, in one place:

  streams  every /api/stream and /media request, from an ASGI wrapper around
           the app: device (the page's ?d= id), address it came in on, the
           Tailscale login, range asked for, status, bytes sent, seconds to the
           first byte and to the end, and whether it finished or the client
           hung up first.
  client   the page's player events (play, waiting, stalled, error, a refused
           start, ...) with the device's address, quality and output, posted
           in batches (POST /api/clientlog).

Both go to an in-memory ring (the Playback check screen and the System page
read it through GET /api/playback/log) and to logs/playback.log as JSON
lines, rotated at 5 MB, three kept. GET /api/speedtest sends random bytes so
a device can measure its own connection to the server.
"""
import json, os, time, threading, collections
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
LOGFILE = HERE / 'logs' / 'playback.log'
MAX_BYTES = 5 * 1024 * 1024
KEEP = 3

STREAMS = collections.deque(maxlen=1500)
CLIENT = collections.deque(maxlen=3000)
OPEN = {}                  # requests still going: what has been sent so far
_lock = threading.Lock()


def _write(rec):
    try:
        with _lock:
            LOGFILE.parent.mkdir(parents=True, exist_ok=True)
            if LOGFILE.exists() and LOGFILE.stat().st_size > MAX_BYTES:
                for i in range(KEEP - 1, 0, -1):
                    a, b = LOGFILE.with_suffix('.log.%d' % i), LOGFILE.with_suffix('.log.%d' % (i + 1))
                    if a.exists():
                        os.replace(a, b)
                os.replace(LOGFILE, LOGFILE.with_suffix('.log.1'))
            with open(LOGFILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError:
        pass


def _agent(ua):
    ua = ua or ''
    for key, name in (('Sonos', 'Sonos'), ('iPhone', 'iPhone'), ('iPad', 'iPad'), ('Android', 'Android'),
                      ('Windows', 'Windows'), ('Macintosh', 'Mac'), ('CrKey', 'Chromecast')):
        if key in ua:
            return name
    return ua[:24] or '-'


class StreamLog:
    """ASGI wrapper: logs each media request once it ends, however it ends."""
    PATHS = ('/api/stream/', '/media/')

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http' or not scope.get('path', '').startswith(self.PATHS):
            return await self.app(scope, receive, send)
        h = {k.decode('latin1').lower(): v.decode('latin1') for k, v in scope.get('headers') or []}
        qs = parse_qs((scope.get('query_string') or b'').decode('latin1'))
        t0 = time.time()
        st = {'status': None, 'bytes': 0, 'first': None, 'done': False, 'err': None,
              'dev': (qs.get('d') or [''])[0][:12], 'path': scope['path'], 'range': h.get('range', ''), 't0': t0}
        OPEN[id(st)] = st

        async def send2(msg):
            if msg['type'] == 'http.response.start':
                st['status'] = msg.get('status')
            elif msg['type'] == 'http.response.body':
                n = len(msg.get('body') or b'')
                if n and st['first'] is None:
                    st['first'] = time.time() - t0
                st['bytes'] += n
                if not msg.get('more_body'):
                    st['done'] = True
            await send(msg)

        try:
            await self.app(scope, receive, send2)
        except BaseException as e:
            st['err'] = type(e).__name__
            raise
        finally:
            OPEN.pop(id(st), None)
            client = scope.get('client') or ('-', 0)
            host = h.get('host', '-')
            rec = {
                'kind': 'stream', 'at': round(t0, 2), 'time': time.strftime('%H:%M:%S', time.localtime(t0)),
                'dev': (qs.get('d') or [''])[0][:12], 'path': scope['path'], 'q': (qs.get('q') or [''])[0],
                'method': scope.get('method'), 'range': h.get('range', ''), 'status': st['status'],
                'bytes': st['bytes'], 'first_s': round(st['first'], 2) if st['first'] is not None else None,
                'secs': round(time.time() - t0, 2), 'done': st['done'], 'err': st['err'],
                'via': 'tailscale' if host.endswith('.ts.net') else host, 'ip': h.get('x-forwarded-for') or client[0],
                'who': (h.get('tailscale-user-login') or '').split('@')[0], 'agent': _agent(h.get('user-agent')),
            }
            STREAMS.append(rec)
            _write(rec)


def client_events(body, request_headers, ip):
    dev = str(body.get('dev') or '')[:12]
    info = body.get('info') or {}
    now = time.time()
    n = 0
    evs = (body.get('events') or [])[:200]
    for k, ev in enumerate(evs):
        # one batch arrives at once; keep its order (newest last) for sorting
        rec = {'kind': 'client', 'at': round(now - (len(evs) - 1 - k) * 0.001, 3), 'dev': dev, 'ev': ev, 'ip': ip,
               'who': (request_headers.get('tailscale-user-login') or '').split('@')[0]}
        if n == 0:
            rec['info'] = info                     # the device's setup, once per batch
        CLIENT.append(rec)
        _write(rec)
        n += 1
    return n


def recent(dev=None, limit=200):
    s = [r for r in STREAMS if not dev or r['dev'] == dev][-limit:]
    c = [r for r in CLIENT if not dev or r['dev'] == dev][-limit:]
    devices = {}
    for r in list(STREAMS) + list(CLIENT):
        d = r.get('dev')
        if d:
            x = devices.setdefault(d, {'dev': d, 'last': 0, 'agent': r.get('agent'), 'who': r.get('who')})
            x['last'] = max(x['last'], r['at'])
            x['agent'] = x['agent'] or r.get('agent')
    now = time.time()
    live = [{'dev': o['dev'], 'path': o['path'], 'range': o['range'], 'status': o['status'], 'bytes': o['bytes'],
             'secs': round(now - o['t0'], 1), 'first_s': round(o['first'], 2) if o['first'] is not None else None}
            for o in list(OPEN.values()) if not dev or o['dev'] == dev or not o['dev']]
    return {'streams': s, 'client': c, 'open': live,
            'devices': sorted(devices.values(), key=lambda x: -x['last'])[:20]}
