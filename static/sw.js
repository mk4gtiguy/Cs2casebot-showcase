const CACHE = 'cs2casebot-v2';
const STATIC_ASSETS = [
  '/',
  '/games',
  '/static/sound.js',
  '/static/balance-animation.js',
  '/static/page-transitions.js',
  '/static/autoplay.js',
  '/static/dashboard.js',
  '/static/dashboard.css',
  '/static/games-shared.css',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((cache) => {
      cache.addAll(STATIC_ASSETS);
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // Skip external URLs — let the browser handle them natively
  if (url.origin !== self.location.origin) {
    return;
  }

  // API calls — network only, never cache sensitive data
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(fetch(e.request).catch(() => new Response('{"error":"offline"}', {
      status: 503, headers: {'Content-Type': 'application/json'}
    })));
    return;
  }

  // Icons/manifest change rarely, if ever — cache-first is safe and fast.
  if (url.pathname.startsWith('/static/icons/') || url.pathname === '/static/manifest.json') {
    e.respondWith(
      caches.match(e.request).then((cached) => {
        const fetchPromise = fetch(e.request).then((res) => {
          const clone = res.clone();
          caches.open(CACHE).then((cache) => cache.put(e.request, clone));
          return res;
        });
        return cached || fetchPromise;
      })
    );
    return;
  }

  // Pages, JS, CSS we actively develop — network-first so code changes show
  // up on the very next reload instead of being masked by a stale
  // service-worker cache (cache-first here previously meant every deploy
  // needed two reloads before users would see it). Cache is purely an
  // offline fallback.
  if (
    url.pathname.startsWith('/static/') ||
    url.pathname === '/' ||
    url.pathname === '/games' ||
    url.pathname === '/market' ||
    url.pathname === '/tournament' ||
    url.pathname === '/privacy' ||
    url.pathname === '/terms'
  ) {
    e.respondWith(
      fetch(e.request).then((res) => {
        const clone = res.clone();
        caches.open(CACHE).then((cache) => cache.put(e.request, clone));
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // Everything else — network only
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
