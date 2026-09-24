/**
 * The down sonar: a line on the chart where the down beam was recorded, and a
 * waterfall of what it saw, tied together ping by ping.
 *
 * The pipeline leaves two files per survey. downscan.json holds each ping's
 * position, time and bottom depth; downscan.png holds the echogram, cut into
 * strips of `stripWidth` pings stacked top to bottom, `rows` rows each, so no
 * side of it is too long for a browser to decode. Ping i lives in strip
 * floor(i / stripWidth), column i % stripWidth.
 *
 * The index is small and is fetched with the chart, because the line on the
 * map comes from it. The image is megabytes and waits until the waterfall is
 * first opened.
 */
const DownScan = (() => {
  const $ = id => document.getElementById(id);

  // Screen pixels per ping. At survey speed the unit pings ten to fifteen
  // times a second, so 1 px a ping is about a minute across a laptop panel.
  const ZOOMS = [0.25, 0.5, 1, 2, 4, 8];
  const DEFAULT_ZOOM = 2;

  // A gap this long or this far between pings is the boat stopping the
  // recording, not a line to draw across the water.
  const BREAK_SECONDS = 5;
  const BREAK_METRES = 25;

  // MapLibre is happy with tens of thousands of vertices, not hundreds.
  const MAX_TRACK_VERTICES = 20000;

  // Nominal cone angles, for an index written before it carried its own.
  const BEAM_DEG = { B004: 45, B001: 20, B000: 60 };
  // The swath is drawn as one quad per this much track: finer than the GPS
  // can place a ping, coarse enough to stay a few thousand shapes.
  const SWATH_STEP_M = 1;

  let index = null;          // the parsed downscan.json, arrays made numeric
  let locationId = null;
  let imageName = null;
  let image = null;          // HTMLImageElement once loaded
  let imageFailed = false;
  let open = false;
  let centre = 0;            // ping at the middle of the canvas (fractional)
  let looked = false;        // whether the waterfall has been opened on this chart
  let zoom = DEFAULT_ZOOM;
  let hoverY = null;         // pointer height over the canvas, for the range readout
  let along = null;          // metres of track from the first ping to each ping
  let measuring = false;     // the measure tool is on: drags mark, not scroll
  let measureA = null;       // the two pings a measurement runs between
  let measureB = null;
  let frame = 0;

  // ── Data ───────────────────────────────────────────────────────────────────

  function reset() {
    index = null;
    image = null;
    imageFailed = false;
    imageName = null;
    along = null;
    close();
    ChartMap.setDownTrack(null);
    ChartMap.setDownSwath(null);
  }

  /**
   * Fetch the index for a chart and draw its track. Resolves to the number of
   * pings, 0 when the chart has no down sonar.
   */
  async function load(location) {
    reset();
    locationId = location.id;
    const data = location.data || {};
    if (!data.downscan || !data.downscanImage) return 0;
    imageName = data.downscanImage;
    let raw;
    try {
      raw = await (await fetch(ChartUrl.data(location.id, data.downscan))).json();
    } catch (e) {
      console.warn('down sonar index unavailable', e);
      return 0;
    }
    if (locationId !== location.id) return 0;     // switched chart meanwhile
    const n = raw.count;
    const lon = new Float64Array(n);
    const lat = new Float64Array(n);
    const t = new Float64Array(n);
    const depth = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      lon[i] = raw.lon[i] / 1e6;
      lat[i] = raw.lat[i] / 1e6;
      t[i] = raw.t[i] / 10;
      depth[i] = raw.depth[i] / 100;
    }
    index = { ...raw, lon, lat, t, depth };
    along = new Float64Array(n);
    // Centimetres from the pipeline, summed before its positions were rounded;
    // an older index without it is summed here, which reads a little long.
    if (raw.along) {
      for (let i = 0; i < n; i++) along[i] = raw.along[i] / 100;
    } else {
      for (let i = 1; i < n; i++) along[i] = along[i - 1] + metresBetween(i - 1, i);
    }
    centre = 0;
    looked = false;
    ChartMap.setDownTrack(trackFeatures());
    ChartMap.setDownSwath(swathFeatures());
    return n;
  }

  function trackFeatures() {
    const n = index.count;
    const step = Math.max(1, Math.ceil(n / MAX_TRACK_VERTICES));
    const features = [];
    let line = [];
    const flush = () => {
      if (line.length > 1) {
        features.push({ type: 'Feature', properties: {},
          geometry: { type: 'LineString', coordinates: line } });
      }
      line = [];
    };
    let last = -1;
    for (let i = 0; i < n; i += step) {
      if (last >= 0 && (index.t[i] - index.t[last] > BREAK_SECONDS * step
          || metresBetween(last, i) > BREAK_METRES * step)) flush();
      line.push([index.lon[i], index.lat[i]]);
      last = i;
    }
    flush();
    return { type: 'FeatureCollection', features };
  }

  /**
   * The strip of bottom the beam covered, as quads along the track.
   *
   * A down beam is a cone, so the width it paints on the bottom is
   * 2 x depth x tan(half the beam angle) - about a third of the depth for the
   * 200 kHz beam. Each quad spans SWATH_STEP_M of track and is as wide as the
   * cone at the depth there. Separate quads rather than one outline, because
   * an outline of a track that turns back on itself crosses over and fills
   * with holes.
   */
  function swathFeatures() {
    const beam = index.beamDeg || BEAM_DEG[index.channel] || 20;
    const spread = Math.tan((beam / 2) * Math.PI / 180);
    const n = index.count;
    const step = Math.max(SWATH_STEP_M, along[n - 1] / MAX_TRACK_VERTICES);

    // Ping indices a step apart, in runs broken at gaps and at lost bottom.
    const runs = [];
    let run = [];
    for (let i = 0; i < n; i++) {
      const gap = i > 0 && (index.t[i] - index.t[i - 1] > BREAK_SECONDS
        || along[i] - along[i - 1] > BREAK_METRES);
      const bottom = index.depth[i] > 0;
      if (gap || !bottom) {
        if (run.length > 1) runs.push(run);
        run = [];
        if (!bottom) continue;
      }
      if (!run.length || along[i] - along[run[run.length - 1]] >= step) run.push(i);
    }
    if (run.length > 1) runs.push(run);

    const quads = [];
    for (const pts of runs) {
      const edge = pts.map((i, j) => {
        const a = pts[Math.max(0, j - 1)];
        const b = pts[Math.min(pts.length - 1, j + 1)];
        const kx = Math.cos((index.lat[i] * Math.PI) / 180) * 111320;
        const ky = 110540;
        const dx = (index.lon[b] - index.lon[a]) * kx;
        const dy = (index.lat[b] - index.lat[a]) * ky;
        const len = Math.hypot(dx, dy) || 1;
        const half = index.depth[i] * spread;
        // Left of the direction of travel is (-dy, dx).
        const ox = (-dy / len) * half / kx;
        const oy = (dx / len) * half / ky;
        return [[index.lon[i] + ox, index.lat[i] + oy], [index.lon[i] - ox, index.lat[i] - oy]];
      });
      for (let j = 1; j < edge.length; j++) {
        const [l0, r0] = edge[j - 1];
        const [l1, r1] = edge[j];
        quads.push([[l0, l1, r1, r0, l0]]);
      }
    }
    return {
      type: 'FeatureCollection',
      features: quads.length ? [{ type: 'Feature', properties: {},
        geometry: { type: 'MultiPolygon', coordinates: quads } }] : [],
    };
  }

  function metresBetween(a, b) {
    const k = Math.cos((index.lat[a] * Math.PI) / 180) * 111320;
    const dx = (index.lon[b] - index.lon[a]) * k;
    const dy = (index.lat[b] - index.lat[a]) * 110540;
    return Math.hypot(dx, dy);
  }

  /** The ping nearest a position, if one is within `maxMetres` of it; else -1. */
  function nearest(lon, lat, maxMetres) {
    if (!index) return -1;
    const kx = Math.cos((lat * Math.PI) / 180) * 111320;
    const ky = 110540;
    let best = -1;
    let bestD = maxMetres * maxMetres;
    for (let i = 0; i < index.count; i++) {
      const dx = (index.lon[i] - lon) * kx;
      const dy = (index.lat[i] - lat) * ky;
      const d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = i; }
    }
    return best;
  }

  function ensureImage() {
    if (image || imageFailed || !imageName) return;
    const img = new Image();
    const forId = locationId;
    img.onload = () => {
      if (forId !== locationId) return;
      image = img;
      $('downHint').hidden = true;
      render();
    };
    img.onerror = () => {
      imageFailed = true;
      $('downHint').textContent = 'The waterfall image could not be loaded.';
    };
    $('downHint').hidden = false;
    $('downHint').textContent = 'Loading the waterfall…';
    img.src = ChartUrl.data(locationId, imageName);
  }

  // ── Panel ──────────────────────────────────────────────────────────────────

  function show(ping) {
    if (!index) return;
    open = true;
    $('downPanel').hidden = false;
    $('downTitle').textContent = index.label || 'Down sonar';
    if (Number.isInteger(ping)) {
      centre = ping;
    } else if (!looked) {
      // Opened from the button with nowhere chosen yet: start at the part of
      // the track on screen, not at the first ping of the recording.
      const middle = ChartMap.map.getCenter();
      const near = nearest(middle.lng, middle.lat, Infinity);
      if (near >= 0) centre = near;
    }
    looked = true;
    ensureImage();
    sizeCanvas();
    moved({ follow: true });
  }

  function close() {
    open = false;
    setMeasuring(false);
    const panel = $('downPanel');
    if (panel) panel.hidden = true;
    ChartMap.setDownCursor(null, null);
  }

  function sizeCanvas() {
    const canvas = $('downCanvas');
    const ratio = window.devicePixelRatio || 1;
    const w = Math.round(canvas.clientWidth * ratio);
    const h = Math.round(canvas.clientHeight * ratio);
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
  }

  /** The cursor followed the waterfall: move it on the map and redraw. */
  function moved({ follow = false } = {}) {
    centre = Math.max(0, Math.min(index.count - 1, centre));
    const i = Math.round(centre);
    ChartMap.setDownCursor(index.lon[i], index.lat[i]);
    if (follow) {
      const map = ChartMap.map;
      if (!map.getBounds().contains([index.lon[i], index.lat[i]])) {
        map.easeTo({ center: [index.lon[i], index.lat[i]], duration: 300 });
      }
    }
    render();
  }

  function render() {
    if (!open || frame) return;
    frame = requestAnimationFrame(() => { frame = 0; draw(); });
  }

  function draw() {
    const canvas = $('downCanvas');
    sizeCanvas();
    const ctx = canvas.getContext('2d');
    const W = canvas.width;
    const H = canvas.height;
    const ratio = window.devicePixelRatio || 1;
    const ppp = zoom * ratio;                   // backing pixels per ping
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, W, H);

    // Newest on the right, like the unit itself scrolls.
    const first = Math.max(0, Math.floor(centre - W / 2 / ppp));
    const last = Math.min(index.count, Math.ceil(centre + W / 2 / ppp) + 1);
    if (image) {
      ctx.imageSmoothingEnabled = zoom < 1;
      const sw = index.stripWidth;
      for (let p = first; p < last;) {
        const strip = Math.floor(p / sw);
        const end = Math.min(last, (strip + 1) * sw);
        ctx.drawImage(image,
          p - strip * sw, strip * index.rows, end - p, index.rows,
          W / 2 + (p - centre) * ppp, 0, (end - p) * ppp, H);
        p = end;
      }
      if (document.body.classList.contains('night')) {
        ctx.globalCompositeOperation = 'multiply';
        ctx.fillStyle = '#ff2a1a';
        ctx.fillRect(0, 0, W, H);
        ctx.globalCompositeOperation = 'source-over';
      }
    }

    drawMeasure(ctx, W, H, ratio, ppp);
    drawScale(ctx, W, H, ratio);

    // The ping on the map is the one under this line.
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.9)';
    ctx.lineWidth = Math.max(1, ratio);
    ctx.setLineDash([4 * ratio, 4 * ratio]);
    ctx.beginPath();
    ctx.moveTo(Math.round(W / 2) + 0.5, 0);
    ctx.lineTo(Math.round(W / 2) + 0.5, H);
    ctx.stroke();
    ctx.setLineDash([]);

    if (hoverY !== null) {
      const y = hoverY * ratio;
      ctx.strokeStyle = 'rgba(255, 213, 79, 0.8)';
      ctx.beginPath();
      ctx.moveTo(0, Math.round(y) + 0.5);
      ctx.lineTo(W, Math.round(y) + 0.5);
      ctx.stroke();
    }

    readout();
  }

  /** The measured span: shaded, edged in amber, its length written above it. */
  function drawMeasure(ctx, W, H, ratio, ppp) {
    if (measureA === null) return;
    const xa = W / 2 + (Math.min(measureA, measureB) - centre) * ppp;
    const xb = W / 2 + (Math.max(measureA, measureB) - centre) * ppp;
    ctx.fillStyle = 'rgba(255, 213, 79, 0.16)';
    ctx.fillRect(xa, 0, Math.max(1, xb - xa), H);
    ctx.strokeStyle = '#FFD54F';
    ctx.lineWidth = 2 * ratio;
    for (const x of [xa, xb]) {
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, H);
      ctx.stroke();
    }
    if (measureA === measureB) return;
    const text = distance(Math.abs(along[measureB] - along[measureA]));
    ctx.font = `bold ${12 * ratio}px system-ui, sans-serif`;
    ctx.textBaseline = 'top';
    const tw = ctx.measureText(text).width;
    const x = Math.max(4 * ratio, Math.min(W - tw - 4 * ratio, (xa + xb) / 2 - tw / 2));
    ctx.lineWidth = 3 * ratio;
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.85)';
    ctx.strokeText(text, x, 4 * ratio);
    ctx.fillStyle = '#FFD54F';
    ctx.fillText(text, x, 4 * ratio);
  }

  /** A horizontal distance to a tenth below a hundred, whole units above. */
  function distance(metres) {
    const feet = Prefs.depthUnit === 'ft' || Prefs.depthUnit === 'fa';
    const value = feet ? metres * 3.28084 : metres;
    return `${value < 100 ? value.toFixed(1) : Math.round(value)} ${feet ? 'ft' : 'm'}`;
  }

  // ── Measuring ──────────────────────────────────────────────────────────────

  function setMeasuring(on) {
    measuring = on;
    measureA = null;
    measureB = null;
    const button = $('btnDownMeasure');
    if (button) button.classList.toggle('on', on);
    const canvas = $('downCanvas');
    if (canvas) canvas.classList.toggle('measuring', on);
    const hint = $('downHint');
    if (hint && image) {
      hint.hidden = !on;
      hint.textContent = 'Drag across the waterfall to measure along the track.';
    }
    ChartMap.setDownMeasure(null);
    render();
  }

  /** Draw the measured stretch of track on the map. */
  function measured() {
    if (measureA === null || measureA === measureB) {
      ChartMap.setDownMeasure(null);
      return;
    }
    const a = Math.min(measureA, measureB);
    const b = Math.max(measureA, measureB);
    const step = Math.max(1, Math.ceil((b - a) / 2000));
    const points = [];
    for (let i = a; i < b; i += step) points.push([index.lon[i], index.lat[i]]);
    points.push([index.lon[b], index.lat[b]]);
    ChartMap.setDownMeasure(points);
  }

  /** Depth ticks down the left edge, in whatever unit the chart is in. */
  function drawScale(ctx, W, H, ratio) {
    const unit = Prefs.depthUnit;
    const perMetre = unit === 'ft' ? 3.28084 : unit === 'fa' ? 1 / 1.8288 : 1;
    const span = index.rangeM * perMetre;
    const steps = [0.5, 1, 2, 5, 10, 20, 50, 100];
    const step = steps.find(s => span / s <= 8) || 100;
    const suffix = unit === 'ft' ? ' ft' : unit === 'fa' ? ' fa' : ' m';
    ctx.font = `${11 * ratio}px system-ui, sans-serif`;
    ctx.textBaseline = 'middle';
    for (let v = step; v < span; v += step) {
      const y = Math.round((v / span) * H) + 0.5;
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.35)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(10 * ratio, y);
      ctx.stroke();
      const text = `${v}${suffix}`;
      ctx.lineWidth = 3 * ratio;
      ctx.strokeStyle = 'rgba(0, 0, 0, 0.8)';
      ctx.strokeText(text, 13 * ratio, y);
      ctx.fillStyle = '#fff';
      ctx.fillText(text, 13 * ratio, y);
    }
  }

  function clock(ping) {
    if (!index.start) return `+${Math.round(index.t[ping])} s`;
    const at = new Date(`${index.start}`);
    if (Number.isNaN(at.getTime())) return `+${Math.round(index.t[ping])} s`;
    at.setMilliseconds(at.getMilliseconds() + index.t[ping] * 1000);
    return at.toTimeString().slice(0, 8);
  }

  function readout() {
    const i = Math.round(centre);
    const unit = Prefs.depthUnit;
    const parts = [clock(i)];
    if (measureA !== null && measureA !== measureB) {
      const a = Math.min(measureA, measureB);
      const b = Math.max(measureA, measureB);
      parts.unshift(`↔ ${distance(along[b] - along[a])} along track, `
        + `${distance(metresBetween(a, b))} straight, `
        + `${(index.t[b] - index.t[a]).toFixed(1)} s`);
    }
    if (index.depth[i] > 0) parts.push(`bottom ${Units.depth(index.depth[i], unit)}`);
    if (hoverY !== null) {
      const h = $('downCanvas').clientHeight;
      parts.push(`at ${Units.depth((hoverY / h) * index.rangeM, unit)}`);
    }
    $('downReadout').textContent = parts.join('  •  ');
  }

  // ── Gestures ───────────────────────────────────────────────────────────────

  function wire() {
    const canvas = $('downCanvas');
    let dragFrom = null;

    let marking = false;
    const pingAt = clientX => {
      const box = canvas.getBoundingClientRect();
      const ping = centre + (clientX - box.left - box.width / 2) / zoom;
      return Math.max(0, Math.min(index.count - 1, Math.round(ping)));
    };

    canvas.addEventListener('pointerdown', e => {
      canvas.setPointerCapture(e.pointerId);
      if (measuring) {
        marking = true;
        measureA = measureB = pingAt(e.clientX);
        measured();
        render();
        return;
      }
      dragFrom = { x: e.clientX, centre };
    });
    canvas.addEventListener('pointermove', e => {
      const box = canvas.getBoundingClientRect();
      hoverY = Math.max(0, Math.min(box.height, e.clientY - box.top));
      if (marking) {
        measureB = pingAt(e.clientX);
        measured();
        render();
      } else if (dragFrom) {
        centre = dragFrom.centre - (e.clientX - dragFrom.x) / zoom;
        moved();
      } else {
        render();
      }
    });
    const end = () => {
      if (dragFrom) moved({ follow: true });
      dragFrom = null;
      marking = false;
    };
    canvas.addEventListener('pointerup', end);
    canvas.addEventListener('pointercancel', end);
    canvas.addEventListener('pointerleave', () => {
      if (!dragFrom) { hoverY = null; render(); }
    });
    // A wheel or two-finger swipe runs along the track.
    canvas.addEventListener('wheel', e => {
      e.preventDefault();
      const delta = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      centre += delta / zoom;
      moved();
    }, { passive: false });

    const setZoom = next => {
      zoom = ZOOMS[Math.max(0, Math.min(ZOOMS.length - 1, ZOOMS.indexOf(zoom) + next))];
      render();
    };
    $('btnDownZoomIn').addEventListener('click', () => setZoom(1));
    $('btnDownZoomOut').addEventListener('click', () => setZoom(-1));
    $('btnDownClose').addEventListener('click', close);
    $('btnDownMeasure').addEventListener('click', () => setMeasuring(!measuring));
    window.addEventListener('resize', render);
  }

  return {
    wire,
    load,
    nearest,
    show,
    close,
    render,
    get available() { return Boolean(index); },
    get isOpen() { return open; },
    get label() { return index ? index.label : ''; },
    /** For the self-test: where the waterfall is looking. */
    get centre() { return Math.round(centre); },
  };
})();
