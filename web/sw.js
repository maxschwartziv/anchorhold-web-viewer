/**
 * Service worker: what makes the installed app work with no network — which,
 * on a boat, is the normal case.
 *
 *   shell   the app itself (HTML/CSS/JS/vendor/icons), precached on install
 *   charts  tiles, grids, contours and legends, cached as they are fetched —
 *           and in bulk when the user saves a survey for offline use
 *
 * The page does its own prefetching with plain fetch() calls; they pass through
 * here and land in the charts cache, so there is no message protocol to keep in
 * sync. Bump VERSION when the shell changes: the old caches are dropped on
 * activate, chart data included, so keep it stable for cosmetic edits.
 */
// v8: the crab-pot switch, the no-server panel, and the locations dialog
// that no longer throws. Bump this and CHART_CACHE in js/offline.js
// together - they name the same cache, and the activate step below
// deletes any anchoring- cache whose name it does not recognise.
const VERSION = 'v21';
const SHELL = `anchoring-shell-${VERSION}`;
const CHARTS = `anchoring-charts-${VERSION}`;   // == CHART_CACHE in js/offline.js

const SHELL_FILES = [
  '.',
  'index.html',
  'manifest.webmanifest',
  'css/app.css',
  'js/chart_url.js',
  'js/units.js',
  'js/prefs.js',
  'js/tide.js',
  'js/grid.js',
  'js/anchor.js',
  'js/legend.js',
  'js/offline.js',
  'js/map.js',
  'js/app.js',
  'js/selftest.js',
  'vendor/maplibre-gl.js',
  'vendor/maplibre-gl.css',
  'icons/icon-192.png',
  'icons/icon-512.png',
];

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL);
    // One bad URL would reject addAll and leave the app uninstallable, so each
    // file is added on its own and a miss is merely logged.
    await Promise.all(SHELL_FILES.map(async file => {
      try {
        await cache.add(new Request(file, { cache: 'reload' }));
      } catch (e) {
        console.warn('[sw] could not precache', file, e);
      }
    }));
    self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names
      .filter(n => n.startsWith('anchoring-') && n !== SHELL && n !== CHARTS)
      .map(n => caches.delete(n)));
    await self.clients.claim();
  })());
});

const isChartData = url =>
  url.pathname.includes('/tiles/') || url.pathname.includes('/data/');

async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  // ignoreSearch, because the page asks for js/app.js?v=18 while the
  // precache holds js/app.js. Without it every stamped asset misses and
  // the app stops working offline - the one thing this cache is for.
  const hit = await cache.match(request, { ignoreSearch: true });
  if (hit) return hit;
  const response = await fetch(request);
  // 404 is a normal answer for a tile outside the survey; don't store it.
  if (response.ok) cache.put(request, response.clone());
  return response;
}

async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch (e) {
    const hit = await cache.match(request, { ignoreSearch: true });
    if (hit) return hit;
    throw e;
  }
}

self.addEventListener('fetch', event => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  // Imagery and fonts come from the internet; leave them to the browser, which
  // already caches them and fails gracefully when there is no signal.
  if (url.origin !== self.location.origin) return;

  // A navigation with the server down still has to open the app.
  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        return await fetch(request);
      } catch (e) {
        const cache = await caches.open(SHELL);
        return (await cache.match('index.html')) || (await cache.match('.'));
      }
    })());
    return;
  }

  if (isChartData(url)) {
    event.respondWith(cacheFirst(request, CHARTS));
    return;
  }
  // The catalog gains surveys over time, so prefer the live one and keep a copy.
  if (url.pathname.endsWith('catalog.json')) {
    event.respondWith(networkFirst(request, CHARTS));
    return;
  }
  // The app itself: network first, cache second.
  //
  // Cache-first was wrong here and cost days of confusion. It meant a browser
  // that could reach its server perfectly well still ran whatever code it had
  // downloaded weeks ago, and the only cure was knowing to bump VERSION - so a
  // fix that was on disk, and served correctly, simply never arrived. The
  // cache stays as the offline fallback, which is what it is actually for:
  // with no server the app still opens, exactly as before.
  event.respondWith(networkFirst(request, SHELL));
});
