/**
 * Chart URLs, stamped with the revision of the chart they point at.
 *
 * Tiles and grids are served cache-first by the service worker, because on the
 * water there is usually no network to check against and a chart that is
 * already on the device has to draw anyway. That is right for a chart that
 * never changes and wrong for one that has just been rebuilt: the URLs are
 * identical, so the browser keeps serving the tiles it already has and the new
 * survey never appears. No amount of reloading helps, because nothing in the
 * request has changed.
 *
 * The catalog carries a `rev` per chart that changes whenever that chart's
 * files change. Hanging it on the end of every chart URL makes a rebuilt chart
 * a cache miss - which is what it is - while an untouched chart keeps its URLs
 * and stays offline-ready.
 */
const ChartUrl = (() => {
  const revisions = new Map();

  const stamp = (id, path) => {
    const rev = revisions.get(id);
    return rev ? `${path}?v=${rev}` : path;
  };

  return {
    /** Take the revisions from a freshly loaded catalog. */
    remember(catalog) {
      revisions.clear();
      for (const loc of (catalog && catalog.locations) || []) {
        if (loc.rev) revisions.set(loc.id, loc.rev);
      }
    },

    revision(id) { return revisions.get(id) || ''; },

    /** The MapLibre tile template for one raster layer. */
    tiles(id, layer) { return stamp(id, `tiles/${id}/${layer}/{z}/{x}/{y}.png`); },

    /** One data file: a geojson overlay, a grid, a legend image. */
    data(id, name) { return stamp(id, `data/${id}/${name}`); },

    /** An already-built chart path that still needs the stamp. */
    stamp,
  };
})();
