/* Svampfinder service worker — offline app shell + tile caching */
const SHELL = 'svampfinder-shell-v33';
const TILES = 'svampfinder-tiles-v1';

const CORE = [
  './',
  './index.html',
  './styles.css',
  './app.js',
  './manifest.webmanifest',
  './data/occurrences.geojson',
  './data/suitability.json',
  './data/forecast.json',
  './data/kommuner.geojson',
  './vendor/leaflet.js',
  './vendor/leaflet.css',
  './vendor/marker-icon.png',
  './vendor/marker-icon-2x.png',
  './vendor/marker-shadow.png',
  './icons/icon.svg',
  './morgonsol.html',
  './morgonsol.css',
  './morgonsol.js',
  './data/morgonsol/areas.geojson',
  './data/morgonsol/sites.geojson',
  './data/morgonsol/route.geojson',
  './data/morgonsol/zones.geojson',
  './data/morgonsol/wetland.geojson',
  './data/morgonsol/huts.geojson',
  './data/morgonsol/reserve.geojson',
  './data/morgonsol/meta.json',
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
  if (/server\.arcgisonline\.com|tile\.openstreetmap\.org|tile\.opentopomap\.org/.test(url.hostname)) {
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

  // Same-origin: network-FIRST so app + data updates show on the first reload
  // (a stale-while-revalidate cache needed two reloads and hid updates). Falls
  // back to cache when offline, so the app stays fully offline-capable.
  if (url.origin === self.location.origin) {
    e.respondWith(
      caches.open(SHELL).then(async cache => {
        try {
          const res = await fetch(req);
          if (res && res.ok) cache.put(req, res.clone());
          return res;
        } catch {
          const hit = await cache.match(req);
          if (hit) return hit;
          if (req.mode !== 'navigate') return undefined;
          return cache.match(url.pathname.includes('morgonsol') ? './morgonsol.html' : './index.html');
        }
      })
    );
  }
});

async function trimCache(name, max) {
  const cache = await caches.open(name);
  const keys = await cache.keys();
  if (keys.length > max) for (let i = 0; i < keys.length - max; i++) cache.delete(keys[i]);
}
