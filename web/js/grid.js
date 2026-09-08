/**
 * The bundled depth + substrate grids (from pipeline/process_data.py), and point
 * queries against them.
 *
 * Depths are metres below LAT chart datum; add the tide offset for the depth
 * right now. Queries outside coverage return null rather than guessing.
 *
 * Layout of both grids: row 0 = south, longitude ascending with column,
 * row-major. depth_grid.bin is float32 LE (NaN = nodata), substrate_grid.bin is
 * uint8 (255 = nodata).
 */
const DepthGrid = (() => {
  /** Substrate class index → label, matching the pipeline colormap. */
  const SUBSTRATE_NAMES = {
    1: 'Fines Ripple', 2: 'Fines Flat', 3: 'Cobble Boulder',
    4: 'Hard Bottom', 5: 'Wood', 6: 'Other', 7: 'Shadow',
  };

  let depthHdr = null;
  let depth = null;
  let subHdr = null;
  let sub = null;
  let loadedId = null;
  let range = null;      // [shallowest, deepest] in metres, for the depth key

  async function fetchJson(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url}: ${res.status}`);
    return res.json();
  }

  async function fetchBuffer(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url}: ${res.status}`);
    return res.arrayBuffer();
  }

  return {
    get loadedId() { return loadedId; },
    get isLoaded() { return depth !== null; },
    /** Actual surveyed depth range, so the key shows this chart's numbers. */
    get range() { return range; },

    /** Load one location's grids, replacing whatever was held before. */
    async load(location) {
      if (loadedId === location.id) return;
      depthHdr = depth = subHdr = sub = range = null;
      loadedId = location.id;

      const names = location.data || {};
      const at = name => ChartUrl.data(location.id, name);

      if (names.depthGrid) {
        try {
          depthHdr = await fetchJson(at(`${names.depthGrid}.json`));
          depth = new Float32Array(await fetchBuffer(at(`${names.depthGrid}.bin`)));
          let lo = Infinity;
          let hi = -Infinity;
          for (let i = 0; i < depth.length; i++) {
            const v = depth[i];
            if (Number.isNaN(v)) continue;
            if (v < lo) lo = v;
            if (v > hi) hi = v;
          }
          range = Number.isFinite(lo) ? [lo, hi] : null;
        } catch (e) {
          console.warn('depth grid unavailable', e);
          depthHdr = depth = null;
        }
      }

      if (names.substrateGrid) {
        try {
          subHdr = await fetchJson(`${base}/${names.substrateGrid}.json`);
          sub = new Uint8Array(await fetchBuffer(`${base}/${names.substrateGrid}.bin`));
        } catch (e) {
          console.warn('substrate grid unavailable', e);
          subHdr = sub = null;
        }
      }
    },

    /** Charted depth (m, LAT datum), bilinearly interpolated; null off-survey. */
    depthAt(lat, lon) {
      const g = depthHdr;
      const d = depth;
      if (!g || !d) return null;
      const fc = (lon - g.lonMin) / g.dLon;
      const fr = (lat - g.latMin) / g.dLat;
      if (fc < 0 || fr < 0 || fc > g.cols - 1 || fr > g.rows - 1) return null;
      const c0 = Math.floor(fc);
      const r0 = Math.floor(fr);
      const c1 = Math.min(c0 + 1, g.cols - 1);
      const r1 = Math.min(r0 + 1, g.rows - 1);
      const tx = fc - c0;
      const ty = fr - r0;
      const v00 = d[r0 * g.cols + c0];
      const v10 = d[r0 * g.cols + c1];
      const v01 = d[r1 * g.cols + c0];
      const v11 = d[r1 * g.cols + c1];
      if (Number.isNaN(v00) || Number.isNaN(v10) || Number.isNaN(v01) || Number.isNaN(v11)) return null;
      const top = v00 * (1 - tx) + v10 * tx;
      const bot = v01 * (1 - tx) + v11 * tx;
      return top * (1 - ty) + bot * ty;
    },

    /** Substrate class name at a point (nearest cell); null off-survey. */
    substrateAt(lat, lon) {
      const g = subHdr;
      const s = sub;
      if (!g || !s) return null;
      const c = Math.round((lon - g.lonMin) / g.dLon);
      const r = Math.round((lat - g.latMin) / g.dLat);
      if (c < 0 || r < 0 || c >= g.cols || r >= g.rows) return null;
      return SUBSTRATE_NAMES[s[r * g.cols + c]] || null;
    },
  };
})();
