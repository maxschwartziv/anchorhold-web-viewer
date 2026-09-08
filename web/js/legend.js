/**
 * Depth colour key, drawn to a canvas so the tick labels follow the unit
 * setting. The range comes from the loaded grid, so the key shows this chart's
 * real shallow-to-deep span instead of a fixed 2–10 m.
 */
const DepthLegend = (() => {
  // Matplotlib "Blues", sampled — the same ramp process_data.py paints tiles with.
  const STOPS = ['#000033', '#003399', '#1565C0', '#42A5F5', '#BBDEFB'];

  function tickLabel(meters, unit) {
    if (unit === 'ft') return (meters * 3.28084).toFixed(0);
    if (unit === 'fa') return (meters / 1.8288).toFixed(1);
    return meters.toFixed(0);
  }

  function niceTicks(min, max) {
    // Five evenly spaced ticks, deepest first (the gradient runs dark → pale).
    const ticks = [];
    for (let i = 0; i < 5; i++) ticks.push(max - ((max - min) * i) / 4);
    return ticks;
  }

  return {
    /**
     * Redraw for the current unit and depth range ([shallow, deep] metres).
     * The canvas is sized in CSS; this only sets the backing store to match the
     * device pixel ratio. Sizing it from the attributes instead would feed the
     * scaled size back into layout and grow the key on every redraw.
     */
    draw(canvas, unit, range) {
      // Soundings can sit slightly above chart datum; a negative "depth" on the
      // key just reads as a mistake, so the scale starts at the waterline.
      const [shallow, deep] = range && range[1] > range[0]
        ? [Math.max(0, range[0]), range[1]] : [2, 10];

      const rect = canvas.getBoundingClientRect();
      const w = rect.width;
      const h = rect.height;
      if (w < 1 || h < 1) return;          // hidden: nothing to draw against
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);

      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      // Card
      ctx.fillStyle = 'rgba(255,255,255,0.93)';
      ctx.beginPath();
      ctx.roundRect(0, 0, w, h, 6);
      ctx.fill();

      // Columns: gradient bar | tick labels | rotated title, all measured off
      // the real box so the key stays legible at any size the CSS gives it.
      const padH = Math.max(5, w * 0.07);
      const padV = Math.max(8, h * 0.05);
      const titleW = 14;
      const barW = Math.max(14, Math.min(24, w * 0.26));
      const barTop = padV;
      const barBottom = h - padV;
      const labelSize = Math.max(9, Math.min(12, h * 0.055));

      const grad = ctx.createLinearGradient(0, barTop, 0, barBottom);
      STOPS.forEach((c, i) => grad.addColorStop(i / (STOPS.length - 1), c));
      ctx.fillStyle = grad;
      ctx.fillRect(padH, barTop, barW, barBottom - barTop);

      ctx.fillStyle = '#1a1a1a';
      ctx.font = `bold ${labelSize}px system-ui, sans-serif`;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      const ticks = niceTicks(shallow, deep);
      const span = ticks[0] - ticks[ticks.length - 1] || 1;
      for (const t of ticks) {
        const y = barTop + ((ticks[0] - t) / span) * (barBottom - barTop);
        // Keep the first and last labels inside the card rather than clipped.
        const clamped = Math.min(barBottom - labelSize / 2,
          Math.max(barTop + labelSize / 2, y));
        ctx.fillText(tickLabel(t, unit), padH + barW + 4, clamped);
      }

      // "Depth (unit)" reads bottom-to-top down its own column on the right.
      const abbr = unit === 'ft' ? 'ft' : unit === 'fa' ? 'fa' : 'm';
      ctx.save();
      ctx.translate(w - titleW / 2 - 2, h / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = 'center';
      ctx.font = `bold ${Math.max(9, labelSize - 1)}px system-ui, sans-serif`;
      ctx.fillText(`Depth (${abbr})`, 0, 0);
      ctx.restore();
    },
  };
})();
