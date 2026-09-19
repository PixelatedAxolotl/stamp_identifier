const SHELL_CACHE = 'stamp-shell-v11';
const IMAGE_CACHE = 'stamp-images-v1';

const APP_SHELL = [
  '/',
  '/css/main.css',
  '/js/app.js',
  '/js/db.js',
  '/js/sync.js',
  '/manifest.json',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(SHELL_CACHE)
      .then(cache => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== SHELL_CACHE && k !== IMAGE_CACHE).map(k => caches.delete(k))
      ))
      .then(() => clients.claim())
  );
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);

  // Stamp images — cache-first (populated by the Sync page)
  if (url.pathname.startsWith('/api/images/')) {
    e.respondWith(
      caches.match(e.request).then(cached => {
        if (cached) return cached;
        return fetch(e.request)
          .then(response => {
            if (response.ok) {
              caches.open(IMAGE_CACHE)
                .then(cache => cache.put(e.request, response.clone()));
            }
            return response;
          })
          .catch(() => new Response('', { status: 404 }));
      })
    );
    return;
  }

  // Other API requests — network-only (JS falls back to IndexedDB on failure)
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(fetch(e.request).catch(() => new Response('', { status: 503 })));
    return;
  }

  // App shell — cache-first; gracefully return 503 for anything not cached offline
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request)
        .then(response => {
          if (response.ok) {
            caches.open(SHELL_CACHE)
              .then(cache => cache.put(e.request, response.clone()));
          }
          return response;
        })
        .catch(() => new Response('Not available offline', { status: 503 }));
    })
  );
});
