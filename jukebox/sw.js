/* MusicMate service worker: offline play on phones (31 Oct 2026).

   It only ever runs on the Tailscale https address; browsers refuse a service
   worker on a plain-http LAN page, so the TV (Chrome 74, http) never sees it.

   Three caches:
     mm-shell  the page itself and its web fonts, so the app opens with no server
     mm-dl     downloaded songs, keyed by their stream path (/api/stream/<id>),
               and the page's download index (/dl/index.json); written by ui.html
     mm-art    covers of downloaded albums, keyed by /api/art/<id>

   A downloaded song is answered from mm-dl even when the server is reachable:
   it saves mobile data, and the file is the same one. Everything else goes to
   the network as before. */

const SHELL = 'mm-shell', DL = 'mm-dl', ART = 'mm-art';

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.add(new Request('/', { cache: 'reload' }))).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', e => { e.waitUntil(self.clients.claim()); });

/* The page: the network's copy when it answers within a few seconds (and keep
   it), the kept copy otherwise. Away from home with no signal, Tailscale can
   leave a connection hanging rather than failing, hence the timer. */
async function page(req) {
  const c = await caches.open(SHELL);
  try {
    const res = await Promise.race([
      fetch(req),
      new Promise((_, no) => setTimeout(() => no(new Error('slow')), 5000)),
    ]);
    if (res.ok) c.put('/', res.clone());
    return res;
  } catch (e) {
    const kept = await c.match('/');
    if (kept) return kept;
    throw e;
  }
}

/* An <audio> element asks for byte ranges, and iOS insists on a 206 reply. */
async function ranged(req, res) {
  const blob = await res.blob();
  const type = res.headers.get('content-type') || 'audio/mpeg';
  const total = blob.size;
  const m = /bytes=(\d*)-(\d*)/.exec(req.headers.get('range') || '');
  if (!m) {
    return new Response(blob, { status: 200, headers: {
      'Content-Type': type, 'Content-Length': String(total), 'Accept-Ranges': 'bytes' } });
  }
  let start = m[1] ? +m[1] : 0, end = m[2] ? +m[2] : total - 1;
  if (!m[1] && m[2]) { start = Math.max(0, total - +m[2]); end = total - 1; }   // bytes=-N: the last N
  end = Math.min(end, total - 1);
  if (start > end) {
    return new Response(null, { status: 416, headers: { 'Content-Range': 'bytes */' + total } });
  }
  return new Response(blob.slice(start, end + 1, type), { status: 206, headers: {
    'Content-Type': type, 'Content-Length': String(end - start + 1),
    'Content-Range': 'bytes ' + start + '-' + end + '/' + total, 'Accept-Ranges': 'bytes' } });
}

async function kept(cacheName, path) {
  const c = await caches.open(cacheName);
  return c.match(path);
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  if (url.origin === location.origin) {
    if (req.mode === 'navigate' && url.pathname === '/') { e.respondWith(page(req)); return; }
    let m = /^\/api\/stream\/(\d+)$/.exec(url.pathname);
    if (m) {
      e.respondWith(kept(DL, url.pathname).then(r => r ? ranged(req, r) : fetch(req)));
      return;
    }
    m = /^\/api\/art\/(\d+)$/.exec(url.pathname);
    if (m) {
      e.respondWith(kept(ART, url.pathname).then(r => r || fetch(req)));
      return;
    }
    if (url.pathname === '/manifest.webmanifest' || /^\/icon-\d+\.png$/.test(url.pathname)) {
      e.respondWith(fetch(req).then(res => {
        const copy = res.clone(); caches.open(SHELL).then(c => c.put(url.pathname, copy)); return res;
      }).catch(() => kept(SHELL, url.pathname).then(r => r || Response.error())));
    }
    return;
  }

  /* web fonts: whatever was kept, refreshed behind the scenes */
  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    e.respondWith(caches.open(SHELL).then(c => c.match(req).then(hit => {
      const net = fetch(req).then(res => { if (res.ok || res.type === 'opaque') c.put(req, res.clone()); return res; })
        .catch(() => hit || Response.error());
      return hit || net;
    })));
  }
});
