/**
 * Installing and offline charts.
 *
 * Registering the service worker is what turns this from a page into something
 * you can install on a PC and open with no server running. Saving a survey
 * writes every one of its tiles into the same cache the worker serves from.
 *
 * Storage can be refused — a browser with storage disabled, a full disk, a
 * locked-down profile — so saving reports how much it actually stored rather
 * than assuming, and the caller says so plainly when the answer is "none".
 */
const Offline = (() => {
  // Must match CHARTS in sw.js: the worker serves what this stores.
  // Must be the same name the service worker reads from: it had drifted to
  // v1 against the worker's v7, so every survey saved for offline use went
  // into a cache nothing served and the worker then deleted.
  const CHART_CACHE = 'anchoring-charts-v21';

  const CHART_CACHE_PREFIX = 'anchoring-charts-';
  const PARALLEL = 8;          // enough to saturate a local server, not swamp it

  let installPrompt = null;    // Chrome/Edge hand us the prompt to fire later
  let saving = false;

  /** Service workers need a secure context: https, or localhost. */
  const supported = () => 'serviceWorker' in navigator && window.isSecureContext;

  function register() {
    if (!supported()) return;
    navigator.serviceWorker.register('sw.js').catch(e => {
      console.warn('offline support unavailable', e);
    });
  }

  window.addEventListener('beforeinstallprompt', e => {
    e.preventDefault();
    installPrompt = e;
    document.dispatchEvent(new CustomEvent('offline-state'));
  });

  window.addEventListener('appinstalled', () => {
    installPrompt = null;
    document.dispatchEvent(new CustomEvent('offline-state'));
  });

  function mb(bytes) {
    return bytes >= 1_000_000
      ? `${Math.round(bytes / 1_000_000)} MB`
      : `${Math.max(1, Math.round(bytes / 1000))} kB`;
  }

  const chartCache = () => caches.open(CHART_CACHE);

  return {
    register,
    supported,
    get canInstall() { return installPrompt !== null; },
    get isSaving() { return saving; },
    /** True when the app is running in its own window rather than a tab. */
    get isInstalled() {
      return window.matchMedia('(display-mode: standalone)').matches
        || window.navigator.standalone === true;
    },

    async promptInstall() {
      if (!installPrompt) return false;
      installPrompt.prompt();
      const { outcome } = await installPrompt.userChoice;
      installPrompt = null;
      return outcome === 'accepted';
    },

    /** How many chart responses are cached right now. */
    async savedCount() {
      if (!('caches' in window)) return 0;
      try {
        const cache = await chartCache();
        return (await cache.keys()).length;
      } catch (e) {
        return 0;
      }
    },

    /** Size of a survey's tiles, for the "this will cost you N MB" line. */
    async surveySize(locationId) {
      const res = await fetch(`tiles/${locationId}/index.json`);
      if (!res.ok) throw new Error('no tile index');
      const index = await res.json();
      return { bytes: index.bytes, label: mb(index.bytes), index };
    },

    /**
     * Pull one survey into the offline cache: every tile, plus its grids,
     * contours and legends. `onProgress(done, total)` drives the UI.
     * Resolves to {stored, total} — stored can be 0 if the browser refuses.
     */
    async saveSurvey(location, onProgress) {
      if (saving) return;
      saving = true;
      try {
        const { index } = await this.surveySize(location.id);
        const urls = [];
        for (const [layer, info] of Object.entries(index.layers)) {
          for (const tile of info.tiles) {
            urls.push(ChartUrl.stamp(location.id,
              `tiles/${location.id}/${layer}/${tile}.png`));
          }
        }
        for (const [key, base] of Object.entries(location.data || {})) {
          urls.push(...(key.endsWith('Grid')
            ? [ChartUrl.data(location.id, `${base}.bin`),
               ChartUrl.data(location.id, `${base}.json`)]
            : [ChartUrl.data(location.id, base)]));
        }
        for (const name of Object.values(location.legends || {})) {
          urls.push(ChartUrl.data(location.id, name));
        }
        urls.push('catalog.json');

        // Write into the cache from here rather than leaning on the service
        // worker to notice the traffic: cache.add() fetches and stores as one
        // step, and a failure is visible instead of silently not-cached.
        const cache = await chartCache();
        let done = 0;
        let stored = 0;
        let next = 0;
        let lastError = null;
        const worker = async () => {
          while (next < urls.length) {
            const url = urls[next++];
            // A miss must not abort the run: one absent tile is not a failure.
            try { await cache.add(url); stored += 1; } catch (e) { lastError = e; }
            done += 1;
            if (done % 25 === 0 || done === urls.length) onProgress(done, urls.length);
          }
        };
        onProgress(0, urls.length);
        await Promise.all(Array.from({ length: PARALLEL }, worker));
        return { stored, total: urls.length, error: stored ? null : lastError };
      } finally {
        saving = false;
      }
    },

    /** Drop the saved charts, keeping the app itself installed. */
    async clear() {
      const names = await caches.keys();
      await Promise.all(names
        .filter(n => n.startsWith(CHART_CACHE_PREFIX))
        .map(n => caches.delete(n)));
    },
  };
})();
