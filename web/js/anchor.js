/**
 * The anchor watch: GPS, geofence, alarm and track recording.
 *
 * A tab cannot run as a background service, so the watch lives as long as the
 * page does — it holds a
 * screen wake lock while armed, and the armed fence is persisted so a reload
 * (or a crash) picks the watch straight back up.
 */
const AnchorWatch = (() => {
  const GPS_TIMEOUT_MS = 30000;      // no fix this long while armed → alarm
  const WATCHDOG_PERIOD_MS = 10000;

  let watchId = null;
  let lastFix = null;
  let lastFixTime = 0;

  let anchorCenter = null;           // [lon, lat]
  let radiusM = 0;
  let armedRing = null;              // [[lon, lat], ...]
  let anchorTimeMs = 0;

  let breached = false;
  let silenced = false;
  let gpsLost = false;
  let maxDriftM = 0;

  let recording = false;
  const trackPoints = [];            // [[lon, lat], ...]

  let listener = null;               // (fix, inside) => void
  let wakeLock = null;

  // ── Alarm (Web Audio; a plain <audio> tag would need a bundled sound file) ──

  const Alarm = (() => {
    let ctx = null;
    let osc = null;
    let gain = null;
    let rampTimer = null;
    let playing = false;

    function build() {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      gain = ctx.createGain();
      gain.gain.value = Prefs.alarmEscalate ? 0.15 : 0.7;
      osc = ctx.createOscillator();
      osc.type = 'square';
      osc.frequency.value = 880;
      osc.connect(gain).connect(ctx.destination);
      // Warble between two pitches so it reads as an alarm, not a test tone.
      let high = true;
      rampTimer = setInterval(() => {
        high = !high;
        if (osc) osc.frequency.setValueAtTime(high ? 880 : 660, ctx.currentTime);
        if (gain && Prefs.alarmEscalate) {
          gain.gain.value = Math.min(1, gain.gain.value + 0.05);
        }
      }, 500);
      osc.start();
    }

    function stop() {
      if (!playing) return;
      playing = false;
      clearInterval(rampTimer);
      rampTimer = null;
      try { osc.stop(); } catch (e) { /* already stopped */ }
      osc = null;
      gain = null;
      if (navigator.vibrate) navigator.vibrate(0);
    }

    return {
      get isPlaying() { return playing; },
      start() {
        if (playing) return;
        playing = true;
        try { build(); } catch (e) { console.warn('no audio', e); }
        if (navigator.vibrate) navigator.vibrate([600, 300, 600, 300, 600]);
      },
      /** Short burst so the user can confirm the alarm works before dark. */
      test() {
        if (playing) return;
        this.start();
        setTimeout(stop, 2500);
      },
      stop,
    };
  })();

  // ── Wake lock ──────────────────────────────────────────────────────────────

  async function acquireWakeLock() {
    if (!Prefs.keepAwake || wakeLock || !navigator.wakeLock) return;
    try {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => { wakeLock = null; });
    } catch (e) { /* denied or unsupported; the watch still runs */ }
  }

  function releaseWakeLock() {
    if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
  }

  // A tab that goes to the background can lose its wake lock; take it back.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && isArmed()) acquireWakeLock();
  });

  // ── Geometry ───────────────────────────────────────────────────────────────

  function pointInPolygon(lng, lat, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i];
      const [xj, yj] = ring[j];
      if ((yi > lat) !== (yj > lat) && lng < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) {
        inside = !inside;
      }
    }
    return inside;
  }

  function isArmed() {
    return anchorCenter !== null || armedRing !== null;
  }

  function distanceFromAnchor(fix) {
    if (!anchorCenter || !fix) return null;
    return Units.haversine(fix.lat, fix.lon, anchorCenter[1], anchorCenter[0]);
  }

  // ── Core ───────────────────────────────────────────────────────────────────

  function onPosition(pos) {
    const c = pos.coords;
    lastFix = {
      lat: c.latitude, lon: c.longitude,
      accuracy: c.accuracy,
      speed: Number.isFinite(c.speed) && c.speed !== null ? c.speed : null,
      heading: Number.isFinite(c.heading) && c.heading !== null ? c.heading : null,
      time: pos.timestamp,
    };
    lastFixTime = Date.now();
    if (gpsLost) { gpsLost = false; if (!breached) Alarm.stop(); }

    if (recording) trackPoints.push([lastFix.lon, lastFix.lat]);

    let inside = true;
    if (anchorCenter) {
      const dist = distanceFromAnchor(lastFix) || 0;
      if (dist > maxDriftM) maxDriftM = dist;
      inside = dist <= radiusM;
    } else if (armedRing) {
      inside = pointInPolygon(lastFix.lon, lastFix.lat, armedRing);
    }

    if (isArmed()) {
      breached = !inside;
      if (!inside) {
        if (!silenced && !Alarm.isPlaying) Alarm.start();
      } else {
        if (Alarm.isPlaying && !gpsLost) Alarm.stop();
        silenced = false;
      }
    }
    if (listener) listener(lastFix, inside);
  }

  function onError(err) {
    console.warn('geolocation', err.message);
    if (listener) listener(null, true);
  }

  /** A stale or refused GPS while armed must not look like "all clear". */
  setInterval(() => {
    if (!isArmed()) return;
    if (Date.now() - lastFixTime > GPS_TIMEOUT_MS) {
      if (!gpsLost) {
        gpsLost = true;
        if (!silenced && !Alarm.isPlaying) Alarm.start();
        if (listener) listener(lastFix, !breached);
      }
    }
  }, WATCHDOG_PERIOD_MS);

  function persist() {
    if (anchorCenter) {
      Prefs.watchMode = 'circle';
      Prefs.anchorLon = anchorCenter[0];
      Prefs.anchorLat = anchorCenter[1];
      Prefs.anchorRadiusM = radiusM;
      Prefs.anchorTimeMs = anchorTimeMs;
    } else if (armedRing) {
      Prefs.watchMode = 'polygon';
      Prefs.watchRing = armedRing;
      Prefs.anchorTimeMs = anchorTimeMs;
    } else {
      Prefs.clearWatch();
    }
  }

  return {
    get lastFix() { return lastFix; },
    get isArmed() { return isArmed(); },
    get isBreached() { return breached; },
    get isGpsLost() { return gpsLost; },
    get maxDriftM() { return maxDriftM; },
    get anchorTimeMs() { return anchorTimeMs; },
    get radiusM() { return radiusM; },
    get center() { return anchorCenter; },
    get ring() { return armedRing; },
    get isRecording() { return recording; },
    get trackPoints() { return trackPoints; },
    get alarmPlaying() { return Alarm.isPlaying; },

    distanceFromAnchor,

    /** Start (or restart) the GPS feed. Safe to call repeatedly. */
    start(onFix) {
      listener = onFix || listener;
      if (watchId !== null || !navigator.geolocation) return;
      lastFixTime = Date.now();     // don't cry "GPS lost" before the first fix
      watchId = navigator.geolocation.watchPosition(onPosition, onError, {
        enableHighAccuracy: true,
        maximumAge: 1000,
        timeout: 20000,
      });
    },

    armCircle(lat, lon, radius) {
      armedRing = null;
      anchorCenter = [lon, lat];
      radiusM = radius;
      anchorTimeMs = Date.now();
      breached = false;
      silenced = false;
      maxDriftM = 0;
      persist();
      acquireWakeLock();
    },

    setGeofence(ring) {
      anchorCenter = null;
      armedRing = ring.slice();
      anchorTimeMs = Date.now();
      breached = false;
      silenced = false;
      maxDriftM = 0;
      persist();
      acquireWakeLock();
    },

    clearGeofence() {
      anchorCenter = null;
      armedRing = null;
      radiusM = 0;
      anchorTimeMs = 0;
      breached = false;
      silenced = false;
      gpsLost = false;
      maxDriftM = 0;
      Alarm.stop();
      Prefs.clearWatch();
      releaseWakeLock();
    },

    /** Silence the current alarm but keep the watch armed — a re-breach rings again. */
    silence() {
      silenced = true;
      Alarm.stop();
    },

    testAlarm() { Alarm.test(); },

    setRecording(on) {
      recording = on;
      if (on && lastFix) trackPoints.push([lastFix.lon, lastFix.lat]);
    },

    clearTrack() { trackPoints.length = 0; },

    /** Restore an armed watch after a reload, exactly where it was left. */
    restore() {
      if (Prefs.watchMode === 'circle' && Prefs.anchorRadiusM > 0) {
        anchorCenter = [Prefs.anchorLon, Prefs.anchorLat];
        radiusM = Prefs.anchorRadiusM;
        anchorTimeMs = Prefs.anchorTimeMs;
        acquireWakeLock();
        return true;
      }
      if (Prefs.watchMode === 'polygon' && (Prefs.watchRing || []).length >= 3) {
        armedRing = Prefs.watchRing;
        anchorTimeMs = Prefs.anchorTimeMs;
        acquireWakeLock();
        return true;
      }
      return false;
    },

    /** Serialise the recorded track as GPX and hand it to the browser. */
    exportGpx() {
      if (!trackPoints.length) return;
      const rows = trackPoints
        .map(([lon, lat]) => `    <trkpt lat="${lat.toFixed(7)}" lon="${lon.toFixed(7)}"></trkpt>`)
        .join('\n');
      const gpx = `<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="AnchorHold Web Viewer" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Anchoring track</name><trkseg>
${rows}
  </trkseg></trk>
</gpx>
`;
      const url = URL.createObjectURL(new Blob([gpx], { type: 'application/gpx+xml' }));
      const a = document.createElement('a');
      a.href = url;
      a.download = `track_${Date.now()}.gpx`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    },
  };
})();
