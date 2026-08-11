/* Svampfinder service worker — offline app shell + tile caching */
const SHELL = 'svampfinder-shell-v1';
const TILES = 'svampfinder-tiles-v1';

const CORE = [
  './',
  './index.html',
  './styles.css',
  './app.js',
  './manifest.webmanifest',
  './data/occurrences.geojson',
  './vendor/leaflet.js',
  './vendor/leaflet.css',
  './vendor/marker-icon.png',
  './vendor/marker-icon-2x.png',
  './vendor/marker-shadow.png',
  './icons/icon.svg',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(CORE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== SHELL && k !== TILES).map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Map tiles: stale-while-revalidate, capped cache.
  if (/tile\.opentopomap\.org|tile\.openstreetmap\.org/.test(url.hostname)) {
    e.respondWith(
      caches.open(TILES).then(async cache => {
        const hit = await cache.match(req);
        const net = fetch(req).then(res => {
          if (res.ok) { cache.put(req, res.clone()); trimCache(TILES, 400); }
          return res;
        }).catch(() => hit);
        return hit || net;
      })
    );
    return;
  }

  // Same-origin: cache-first, fall back to network, then index for navigations.
  if (url.origin === self.location.origin) {
    e.respondWith(
      caches.match(req).then(hit => hit || fetch(req).catch(() => {
        if (req.mode === 'navigate') return caches.match('./index.html');
      }))
    );
  }
});

async function trimCache(name, max) {
  const cache = await caches.open(name);
  const keys = await cache.keys();
  if (keys.length > max) for (let i = 0; i < keys.length - max; i++) cache.delete(keys[i]);
}
