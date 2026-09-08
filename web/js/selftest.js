/**
 * Smoke test, off unless the page is opened with ?selftest=1. It fakes a GPS
 * feed just off the Santa Rosalía breakwater and then drives the HUD the way a
 * user would, so a headless screenshot shows the live parts working:
 *
 *   chrome --headless=new --screenshot=out.png "http://localhost:8000/?selftest=1"
 *
 * The end state is written into document.title, which is what a headless run
 * can read back without a debugger attached.
 */
(() => {
  const mode = new URLSearchParams(location.search).get('selftest');
  if (mode === null) return;

  const START = { lat: 27.3383, lon: -112.2635 };
  let tick = 0;

  navigator.geolocation.watchPosition = (success) => {
    const feed = () => {
      // Creep east so the boat eventually leaves a small watch circle.
      const pos = {
        coords: {
          latitude: START.lat,
          longitude: START.lon + tick * 0.00012,
          accuracy: 4,
          speed: 0.4,
          heading: 92,
        },
        timestamp: Date.now(),
      };
      tick += 1;
      success(pos);
    };
    feed();
    return setInterval(feed, 1000);
  };
  navigator.geolocation.getCurrentPosition = () => {};

  const $ = id => document.getElementById(id);
  const log = [];
  const step = (delay, name, fn) => setTimeout(() => {
    try { fn(); log.push(`ok ${name}`); } catch (e) { log.push(`FAIL ${name}: ${e.message}`); }
    window.__selftest = log;
  }, delay);

  step(2500, 'sonar on', () => $('toggleSonar').click());
  step(3000, 'waypoint', () => {
    $('btnWaypoint').click();
    $('waypointLabel').value = 'good holding';
    // Click the button rather than calling close(): that is what a user does,
    // and doing it by script once hid the fact that the buttons did nothing.
    $('waypointDialog').querySelector('button[value="save"]').click();
    if ($('waypointDialog').open) throw new Error('waypoint dialog did not close');
  });
  step(3500, 'drop anchor', () => {
    $('btnDropAnchor').click();
    $('anchorRadius').value = 25;
    $('anchorDialog').querySelector('button[value="arm"]').click();
    if ($('anchorDialog').open) throw new Error('anchor dialog did not close');
  });
  // Counted here rather than at the end: the run switches survey before it
  // collects, and the chart with the detections on it is the one it opened on.
  step(4100, 'detections on the opening chart', () => {
    const layer = ChartMap.map.getLayer('detections-layer');
    const source = ChartMap.map.getSource('detections-source');
    const data = source && source._data;
    window.__detectionsHome = layer ? ((data && data.features) ? data.features.length : 0) : -1;
  });

  // The POTS switch is the only control that appears and disappears with the
  // chart, so it is worth pressing rather than merely looking at.
  step(4150, 'pots switch', () => {
    const row = document.querySelector('.row.layer[data-layer="detections"]');
    if (!row) throw new Error('no pots row');
    const shown = !row.classList.contains('hidden');
    if (shown !== (window.__detectionsHome > 0)) {
      throw new Error(`pots row ${shown ? 'shown' : 'hidden'} with `
        + `${window.__detectionsHome} detections`);
    }
    if (!shown) { window.__potsSwitch = 'no detections here'; return; }
    const button = row.querySelector('.toggle');
    button.click();
    const off = ChartMap.map.getLayoutProperty('detections-layer', 'visibility');
    button.click();
    const on = ChartMap.map.getLayoutProperty('detections-layer', 'visibility');
    if (off === on) throw new Error('the pots switch changed nothing');
    window.__potsSwitch = `${off} then ${on}`;
  });

  step(4200, 'contours every 3 ft', () => {
    $('setContour').value = 5;
    $('setContour').dispatchEvent(new Event('input'));
  });
  step(4600, 'feet', () => {
    $('setUnit').value = 'ft';
    $('setUnit').dispatchEvent(new Event('change'));
  });
  step(5200, 'shallow warning', () => {
    $('seekMinDepth').value = 8;
    $('seekMinDepth').dispatchEvent(new Event('input'));
  });
  // Switching surveys tears every source and layer down and rebuilds them,
  // which is the easiest thing to break; end the run on the second survey.
  step(5600, 'settings open and close', () => {
    $('fabSettings').click();
    if (!$('settingsDialog').open) throw new Error('settings did not open');
    $('settingsDialog').querySelector('button[value="close"]').click();
    if ($('settingsDialog').open) throw new Error('Done did not close settings');
  });

  step(6500, 'switch survey', () => {
    $('fabLocations').click();
    const list = [...document.querySelectorAll('#locationList button')];
    const next = list.find(b => !b.classList.contains('current'));
    if (!next) throw new Error('no other survey to switch to');
    next.click();
  });

  // The hub: it fetches its own list, so it is opened early and read late.
  step(7200, 'workflow opens', () => {
    $('fabWorkflow').click();
    if (!$('workflowDialog').open) throw new Error('workflow did not open');
  });
  // Painted locally, so the class must have flipped by the time click()
  // returns - a redraw waiting on the server would still say the old thing.
  step(7500, 'tick repaints on the click', () => {
    const mark = $('workflowList').querySelector('.mark');
    if (!mark) throw new Error('no tick to click');
    const before = mark.classList.contains('done');
    mark.click();
    const after = mark.classList.contains('done');
    if (before === after) throw new Error('the tick did not repaint on click');
    window.__markRepaint = `${before} -> ${after}`;
  });
  // Put it back, once the first write has landed, so a self-check leaves
  // the list exactly as it found it.
  step(7800, 'tick goes back', () => {
    $('workflowList').querySelector('.mark').click();
  });

  step(7900, 'workflow lists the steps', () => {
    const rows = $('workflowList').children.length;
    if (!rows) throw new Error('workflow list is empty');
    window.__workflowRows = rows;
    $('workflowDialog').querySelector('button[value="close"]').click();
    if ($('workflowDialog').open) throw new Error('workflow did not close');
  });

  step(9000, 'collect', () => {
    const style = ChartMap.map.getStyle();
    window.__state = {
      log,
      switchedTo: ChartMap.location && ChartMap.location.name,
      styleLayers: style.layers.filter(l => l.id.includes('layer') || l.id.includes('contour')).length,
      contourSource: Boolean(ChartMap.map.getSource('contours-source')),
      // Objects found on the bottom, where a survey has been run through a
      // detector. A chart without them reports 0, which is not a failure -
      // the -1 is, because it means the layer itself never got built.
      detectionsHome: window.__detectionsHome,
      potsSwitch: window.__potsSwitch,
      workflowRows: window.__workflowRows,
      markRepaint: window.__markRepaint,
      detections: (() => {
        try {
          if (!ChartMap.map.getLayer('detections-layer')) return -1;
          const source = ChartMap.map.getSource('detections-source');
          const data = source && source._data;
          return data && data.features ? data.features.length : 0;
        } catch (e) { return -1; }
      })(),
      loc: ChartMap.location && ChartMap.location.id,
      depthRange: DepthGrid.range,
      armed: AnchorWatch.isArmed,
      breached: AnchorWatch.isBreached,
      drift: Math.round(AnchorWatch.maxDriftM),
      nav: $('navReadout').textContent,
      anchor: $('anchorInfo').textContent,
      tide: $('labelTide').textContent,
      minDepth: $('labelMinDepth').textContent,
      alarmVisible: !$('alarmBanner').hidden,
      waypoints: Prefs.waypoints.length,
      // Outlines and tracks of the other surveys should be on the map.
      previews: (() => {
        const count = id => {
          const src = ChartMap.map.getSource(id);
          return src && src._data ? (src._data.features || []).length : 0;
        };
        return `${count('preview-area-source')} areas, ${count('preview-track-source')} tracks`;
      })(),
      scale: (() => {
        const el = document.querySelector('.maplibregl-ctrl-scale');
        return el ? el.textContent : '(none)';
      })(),
      // Nothing in the HUD may run off the side of the screen.
      panel: (() => {
        const p = $('controlPanel');
        const widest = Math.max(...[...p.querySelectorAll('.row')]
          .map(r => r.scrollWidth));
        return `${p.clientWidth} wide, widest row ${widest}, viewport ${window.innerWidth}`;
      })(),
      // The key must still be its CSS size after all those redraws; if the
      // backing store ever leaks back into layout, this is where it shows.
      key: (() => {
        const c = $('depthLegend');
        const r = c.getBoundingClientRect();
        return `${Math.round(r.width)}x${Math.round(r.height)} css, ${c.width}x${c.height} px`;
      })(),
    };
    document.title = `selftest ${JSON.stringify(window.__state)}`;
  });

  // ?selftest=offline additionally exercises the installable path: the service
  // worker has to be in control, and saving the survey has to fill its cache.
  if (mode === 'offline') {
    setTimeout(async () => {
      const before = await Offline.savedCount();
      const saved = await Offline.saveSurvey(ChartMap.location, () => {});
      const registration = await navigator.serviceWorker.getRegistration();
      window.__state.offline = {
        supported: Offline.supported(),
        registered: Boolean(registration),
        controlled: Boolean(navigator.serviceWorker.controller),
        requested: saved,
        cachedBefore: before,
        cachedAfter: await Offline.savedCount(),
        caches: await caches.keys(),
        sampleHit: Boolean(await caches.match('catalog.json')),
      };
      document.title = `selftest ${JSON.stringify(window.__state)}`;
    }, 10500);
  }
})();
