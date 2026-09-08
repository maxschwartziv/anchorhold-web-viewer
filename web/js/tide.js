/**
 * Harmonic tide model — the constants mirror TIDE_CONSTITUENTS in
 * pipeline/process_data.py. Keep the two in step: the charts are baked to LAT
 * with these constants, so a divergence here shows up as depths that are
 * quietly wrong.
 *
 *   h(t) = Σ fᵢ·Aᵢ·cos(ωᵢ·t − φᵢ + uᵢ),  t = hours since J2000
 */
const TideModel = (() => {
  const EPOCH = Date.UTC(2000, 0, 1, 0, 0, 0);
  const DEG = Math.PI / 180;

  // IHO/CICESE Guaymas station constants converted to J2000 epoch phases.
  const GUAYMAS = [
    { speed: 28.9841042, amp: 0.530, phase: 149.7 }, // M2 principal lunar semidiurnal
    { speed: 30.0000000, amp: 0.190, phase: 295.0 }, // S2 principal solar semidiurnal
    { speed: 28.4397295, amp: 0.118, phase: 260.7 }, // N2 larger lunar elliptic
    { speed: 15.0410686, amp: 0.315, phase: 232.5 }, // K1 lunisolar diurnal
    { speed: 13.9430356, amp: 0.235, phase: 105.2 }, // O1 principal lunar diurnal
    { speed: 14.9589314, amp: 0.104, phase: 252.5 }, // P1 solar diurnal
  ];

  // No tide by default: inland water is shown exactly as surveyed, and a
  // location opts in to a model through its catalog entry.
  let constituents = [];
  let latCache = null;

  /** Ascending lunar node longitude (deg): N = 125.0445 − 0.05295377·days. */
  function nodalN(millis) {
    const days = (millis - EPOCH) / 86400000;
    return ((125.0445 - 0.05295377 * days) % 360 + 360) % 360;
  }

  /** Foreman (1977) nodal amplitude factor, keyed by constituent speed. */
  function nodalF(speed, N) {
    const nr = N * DEG;
    if (speed === 30.0 || speed === 14.9589314) return 1.0;                 // S2, P1
    if (speed === 15.0410686) return 1.0060 + 0.1150 * Math.cos(nr);        // K1
    if (speed === 13.9430356) return 1.0089 + 0.1871 * Math.cos(nr);        // O1
    return 1.0004 - 0.0373 * Math.cos(nr);                                  // M2, N2
  }

  /** Foreman (1977) nodal phase correction u (deg). */
  function nodalU(speed, N) {
    const nr = N * DEG;
    if (speed === 30.0 || speed === 14.9589314) return 0.0;                 // S2, P1
    if (speed === 15.0410686) return -8.86 * Math.sin(nr);                  // K1
    if (speed === 13.9430356) return 10.80 * Math.sin(nr);                  // O1
    return -2.14 * Math.sin(nr);                                            // M2, N2
  }

  function heightAt(millis) {
    if (!constituents.length) return 0;
    const hours = (millis - EPOCH) / 3600000;
    const N = nodalN(millis);
    let h = 0;
    for (const c of constituents) {
      const angle = (c.speed * hours - c.phase + nodalU(c.speed, N)) * DEG;
      h += nodalF(c.speed, N) * c.amp * Math.cos(angle);
    }
    return h;
  }

  /**
   * Lowest Astronomical Tide: the model minimum over three years at hourly
   * steps, computed exactly as the pipeline does so the app's height-above-LAT
   * lines up with the LAT datum baked into the tiles.
   */
  function lat() {
    if (latCache !== null) return latCache;
    if (!constituents.length) return (latCache = 0);
    const start = Date.UTC(2024, 0, 1);
    let lo = Infinity;
    for (let i = 0, n = 3 * 365 * 24; i < n; i++) {
      const h = heightAt(start + i * 3600000);
      if (h < lo) lo = h;
    }
    return (latCache = lo);
  }

  return {
    /** Point the model at a location's constants; [] means "no tide here". */
    configure(list) {
      constituents = list || [];
      latCache = null;
    },

    /** Catalog entry → constants. Anything unrecognised means no tide. */
    fromCatalog(tide) {
      if (!tide) return [];
      if (tide.mode === 'guaymas') return GUAYMAS.slice();
      if (tide.mode === 'constituents' && Array.isArray(tide.constituents)) {
        return tide.constituents
          .filter(c => Array.isArray(c) && c.length >= 3)
          .map(c => ({ speed: c[0], amp: c[1], phase: c[2] }));
      }
      return [];
    },

    get isTidal() { return constituents.length > 0; },
    get LAT() { return lat(); },

    heightAt,

    /** Water level above LAT (always ≥ 0); displayed depth = charted + this. */
    heightAboveLAT(millis) { return heightAt(millis) - lat(); },

    /**
     * Next tidal extreme after `from`. Scans at 5-minute steps for the turning
     * point, then bisects the bracket for ~10-second precision.
     */
    nextExtreme(from, high) {
      if (!constituents.length) return from;
      const step = 5 * 60 * 1000;
      const end = from + 48 * 3600000;
      let prevT = from;
      let prev = heightAt(prevT);
      let risingBefore = null;
      for (let t = from + step; t <= end; t += step) {
        const cur = heightAt(t);
        const rising = cur > prev;
        if (risingBefore !== null && risingBefore !== rising) {
          const isPeak = risingBefore && !rising;
          if (high === isPeak) {
            let lo = prevT;
            let hi = t;
            for (let i = 0; i < 10; i++) {
              const mid = (lo + hi) / 2;
              if (high === (heightAt(mid) > heightAt(lo))) lo = mid; else hi = mid;
            }
            return Math.round((lo + hi) / 2);
          }
        }
        risingBefore = rising;
        prevT = t;
        prev = cur;
      }
      return from;
    },
  };
})();
