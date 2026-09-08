/**
 * Wiring: catalog → chart, and every control on the HUD. Everything that is not
 * drawing the map starts here: layers, units, the anchor watch, waypoints,
 * settings, offline saving and the developer overlay.
 */
(() => {
  const $ = id => document.getElementById(id);

  let catalog = { locations: [] };
  // False until the server has answered once: an empty library and a
  // library nobody could fetch look identical from here, and they need
  // different advice.
  let catalogLoaded = false;
  let location = null;
  // Detections on the chart in front of you. Zero is the ordinary case -
  // most surveys have never been run through a detector - and it is what
  // hides the POTS row rather than leaving a switch that does nothing.
  let detectionCount = 0;
  let tideOffset = 0;
  let targetTimeMillis = Date.now();

  // Fence drawing state (the armed fence itself lives in AnchorWatch).
  let drawMode = false;
  let fenceVerts = [];
  let pendingWaypoint = null;

  // ── Small helpers ──────────────────────────────────────────────────────────

  let toastTimer = null;
  function toast(message) {
    const el = $('toast');
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
  }

  /** Keep the keys and toast clear of the control panel as it grows/shrinks. */
  function measurePanel() {
    const panel = $('controlPanel');
    const height = Prefs.hudVisible && window.innerWidth < 900 ? panel.offsetHeight : 0;
    document.documentElement.style.setProperty('--panel-height', `${height}px`);
  }

  const layerOf = {
    bathymetry: 'bathymetry-layer', sonar: 'sonar-layer',
    substrate: 'substrate-layer', rock: 'rock-layer',
  };
  const prefOn = { bathymetry: 'bathyOn', sonar: 'sonarOn', substrate: 'substrateOn', rock: 'rockOn' };
  const prefOpacity = {
    bathymetry: 'bathyOpacity', sonar: 'sonarOpacity',
    substrate: 'substrateOpacity', rock: 'rockOpacity',
  };
  const layerTitle = { bathymetry: 'DEPTH', sonar: 'SONAR', substrate: 'SUBSTRATE', rock: 'ROCK' };

  // ── Layers ─────────────────────────────────────────────────────────────────

  function applyControlsToChart() {
    for (const name of Object.keys(layerOf)) {
      const on = Prefs[prefOn[name]] && ChartMap.hasLayer(name);
      ChartMap.setVisible(layerOf[name], on);
      ChartMap.setOpacity(layerOf[name], Prefs[prefOpacity[name]] / 100);
    }
    applySonarImage();
    refreshImageSection();
    // Contours belong to the bathymetry chart, so they follow its toggle.
    const bathyOn = Prefs.bathyOn && ChartMap.hasLayer('bathymetry');
    ChartMap.setVisible('contours-layer', bathyOn);
    ChartMap.setVisible('contour-labels', bathyOn);
    // Pots are points, not a raster sheet, so they have a switch and no
    // opacity: a half-faded mark on the bottom is worth nothing to anyone.
    ChartMap.setVisible('detections-layer', Prefs.potsOn && detectionCount > 0);
    recomputeTide();
    refreshLayerButtons();
    updateLegends();
  }

  function refreshLayerButtons() {
    for (const name of Object.keys(layerOf)) {
      const row = document.querySelector(`.row.layer[data-layer="${name}"]`);
      const available = ChartMap.hasLayer(name);
      row.classList.toggle('hidden', !available);
      const button = row.querySelector('.toggle');
      const on = Prefs[prefOn[name]] && available;
      button.classList.toggle('on', on);
      button.textContent = `${layerTitle[name]} ${on ? 'ON' : 'OFF'}`;
      row.querySelector('.opacity').value = Prefs[prefOpacity[name]];
    }
    const pots = document.querySelector('.row.layer[data-layer="detections"]');
    pots.classList.toggle('hidden', detectionCount === 0);
    const potsOn = Prefs.potsOn && detectionCount > 0;
    const potsButton = pots.querySelector('.toggle');
    potsButton.classList.toggle('on', potsOn);
    potsButton.textContent = potsOn ? 'OBJECTS ON' : 'OBJECTS OFF';
    $('potsCount').textContent = detectionCount === 1
      ? '1 found' : `${detectionCount} found`;
    measurePanel();
  }

  // ── The sonar image ────────────────────────────────────────────────────────
  //
  // Kept per survey. A 0.5 m multibeam mosaic and a towed side-scan sheet want
  // completely different brightness, so one global setting would just get
  // re-dialled every time you switched chart.

  const IMAGE_DEFAULTS = { brightness: 0, contrast: 0, sharp: false };
  let imagePanelOpen = false;

  function imageSettings() {
    const saved = (Prefs.sonarImage || {})[location && location.id] || {};
    return { ...IMAGE_DEFAULTS, ...saved };
  }

  /** Store one field. Reassigns the whole map: the Prefs proxy saves on set,
   *  so mutating the nested object in place would never reach localStorage. */
  function setImageSetting(changes) {
    if (!location) return;
    Prefs.sonarImage = {
      ...(Prefs.sonarImage || {}),
      [location.id]: { ...imageSettings(), ...changes },
    };
    applySonarImage();
    refreshImagePanel();
  }

  /** Push the look onto the layer. Sliders are -100..100; MapLibre wants -1..1. */
  function applySonarImage() {
    const image = imageSettings();
    ChartMap.setImageAdjust(layerOf.sonar, {
      brightness: image.brightness / 100,
      contrast: image.contrast / 100,
      sharp: image.sharp,
    });
  }

  function refreshImagePanel() {
    const image = imageSettings();
    const sign = v => (v > 0 ? `+${v}` : `${v}`);
    $('imgBrightnessLabel').textContent = `Brightness: ${sign(image.brightness)}`;
    $('imgContrastLabel').textContent = `Contrast: ${sign(image.contrast)}`;
    $('seekBrightness').value = image.brightness;
    $('seekContrast').value = image.contrast;
    $('btnSharpen').classList.toggle('on', image.sharp);
    $('btnSharpen').textContent = image.sharp ? 'SHARP PIXELS' : 'SMOOTH PIXELS';
    $('imageHint').textContent = location
      ? `Saved with ${location.name}.` : 'Saved with this survey.';
  }

  /** Show the controls only while the sonar layer is on and the panel is open. */
  function refreshImageSection() {
    const open = imagePanelOpen && Prefs.sonarOn && ChartMap.hasLayer('sonar');
    $('sonarImage').hidden = !open;
    $('btnSonarImage').classList.toggle('on', open);
    // Refresh even while hidden. Otherwise the controls keep the last survey's
    // numbers until they are next opened, and the first thing you see after a
    // chart switch is the wrong reading of a correctly drawn layer.
    refreshImagePanel();
    measurePanel();
  }

  function wireImagePanel() {
    $('btnSonarImage').addEventListener('click', () => {
      imagePanelOpen = !imagePanelOpen;
      refreshImageSection();
    });
    $('seekBrightness').addEventListener('input', e =>
      setImageSetting({ brightness: Number(e.target.value) }));
    $('seekContrast').addEventListener('input', e =>
      setImageSetting({ contrast: Number(e.target.value) }));
    $('btnSharpen').addEventListener('click', () =>
      setImageSetting({ sharp: !imageSettings().sharp }));
    $('btnImageReset').addEventListener('click', () =>
      setImageSetting({ ...IMAGE_DEFAULTS }));
  }

  function updateLegends() {
    const show = Prefs.keyVisible && Prefs.hudVisible;
    const depthOn = show && Prefs.bathyOn && ChartMap.hasLayer('bathymetry');
    const depthCanvas = $('depthLegend');
    depthCanvas.hidden = !depthOn;
    if (depthOn) DepthLegend.draw(depthCanvas, Prefs.depthUnit, DepthGrid.range);

    const legends = (location && location.legends) || {};
    const substrateOn = show && Prefs.substrateOn && ChartMap.hasLayer('substrate') && legends.substrate;
    $('substrateLegend').hidden = !substrateOn;
    // Rock and substrate keys would overlap, so rock defers to substrate.
    const rockOn = show && Prefs.rockOn && !Prefs.substrateOn
      && ChartMap.hasLayer('rock') && legends.rock;
    $('rockLegend').hidden = !rockOn;
    measurePanel();
  }

  // ── Tide ───────────────────────────────────────────────────────────────────

  function recomputeTide() {
    tideOffset = TideModel.heightAboveLAT(targetTimeMillis);
    $('labelTide').textContent = TideModel.isTidal
      ? `LAT +${tideOffset.toFixed(2)} m  ${Units.stamp(targetTimeMillis)}`
      : `No tide  ${Units.stamp(targetTimeMillis)}`;
    ChartMap.applyContours(Prefs.contourIntervalFt, tideOffset, Prefs.depthUnit);
    ChartMap.setScaleUnit(Prefs.depthUnit);
    applyShallowThreshold(Prefs.minDepthProgress);
  }

  function applyShallowThreshold(progress) {
    const meters = progress * 0.5;
    $('labelMinDepth').textContent = progress === 0
      ? 'Min depth: off'
      : `Min depth: ${Units.depth(meters, Prefs.depthUnit)}`;
    ChartMap.applyShallow(meters, tideOffset);
  }

  // ── Locations ──────────────────────────────────────────────────────────────

  async function switchLocation(target) {
    location = target;
    clearEmptyNotice();     // whatever was wrong, a chart is drawing now
    Prefs.lastLocationId = target.id;
    // Keys are rendered per survey, so repoint them before anything draws.
    const legends = target.legends || {};
    $('substrateLegend').src = legends.substrate
      ? ChartUrl.data(target.id, legends.substrate) : '';
    $('rockLegend').src = legends.rock ? ChartUrl.data(target.id, legends.rock) : '';
    TideModel.configure(TideModel.fromCatalog(target.tide));
    ChartMap.setLocation(target);
    ChartMap.flyTo(target.lat, target.lon, target.zoom);
    renderPins();
    renderWaypoints();
    restoreFenceRender();
    applyControlsToChart();

    // Grids and vector overlays are megabytes; fetch them without blocking the
    // first frames, then fill the sources in as each arrives.
    DepthGrid.load(target).then(updateLegends).catch(e => console.warn(e));
    renderPreviews();
    loadOverlay(target, 'contours', ChartMap.setContourData);
    loadOverlay(target, 'shallowBands', ChartMap.setShallowData);
    // The count drives the POTS switch, so it is reset before the fetch:
    // the previous survey's pots must not label this one.
    detectionCount = 0;
    loadOverlay(target, 'detections', fc => {
      detectionCount = fc && fc.features ? fc.features.length : 0;
      ChartMap.setDetections(fc);
      applyControlsToChart();
    });
  }

  async function loadOverlay(target, key, apply) {
    const name = (target.data || {})[key];
    if (!name) { apply(null); return; }
    try {
      const res = await fetch(ChartUrl.data(target.id, name));
      const fc = await res.json();
      if (location && location.id === target.id) apply(fc);
    } catch (e) {
      console.warn(`${key} unavailable`, e);
    }
  }

  function renderPins() {
    const features = catalog.locations.map(loc => ({
      type: 'Feature',
      properties: {
        id: loc.id,
        label: loc.name,
        color: loc.id === location.id ? '#00E5FF' : '#FFC400',
        stroke: '#FFFFFF',
      },
      geometry: { type: 'Point', coordinates: [loc.lon, loc.lat] },
    }));
    ChartMap.setPins({ type: 'FeatureCollection', features });
  }

  /**
   * Outlines and tracks of the other surveys, so you can see what else is
   * charted and whether it covers the water you care about. Everything the
   * server offers is downloadable here, so "other than the one on screen" is
   * the useful cut - unless Settings asks for all of them.
   */
  async function renderPreviews() {
    const wanted = catalog.locations.filter(loc => {
      const data = loc.data || {};
      if (!data.boundary && !data.track) return false;
      return Prefs.alwaysShowPreviews || loc.id !== location.id;
    });

    const areas = [];
    const tracks = [];
    await Promise.all(wanted.map(async loc => {
      for (const [key, sink] of [['boundary', areas], ['track', tracks]]) {
        const name = (loc.data || {})[key];
        if (!name) continue;
        try {
          const fc = await (await fetch(ChartUrl.data(loc.id, name))).json();
          for (const feature of fc.features || []) {
            feature.properties = { ...(feature.properties || {}), id: loc.id };
            sink.push(feature);
          }
        } catch (e) { /* a survey without a preview simply has none */ }
      }
    }));

    ChartMap.setPreviewAreas({ type: 'FeatureCollection', features: areas });
    ChartMap.setPreviewTracks({ type: 'FeatureCollection', features: tracks });
  }

  /**
   * The charts held on the machine serving this page, with a way to delete one.
   *
   * Only offered when the server says it can manage them: a static export has
   * no way to delete anything, and a tablet reading the charts over the boat's
   * wifi is refused by the server rather than teased with a button.
   */
  /**
   * The saved waypoints, with their positions.
   *
   * Waypoints live only in this browser, so a position marked on the water is
   * otherwise impossible to quote to anyone - including back to the pipeline,
   * which is where a marked target has to go to be measured.
   */
  /**
   * Mirror the waypoints to the chart server.
   *
   * They live in localStorage, which is private to this browser - so a target
   * marked on the water cannot otherwise reach the pipeline that could measure
   * it. Best effort: the browser stays the owner of the list, and a server
   * that is not there (or is someone else's) simply does not get a copy.
   */
  async function pushWaypoints({ quiet = true } = {}) {
    if (!catalog.canManage) return false;
    try {
      const res = await fetch('waypoints', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ waypoints: Prefs.waypoints || [] }),
      });
      if (!res.ok) throw new Error(`server said ${res.status}`);
      return true;
    } catch (e) {
      // Never silent: a pin the user believes is shared, but which never left
      // the browser, is worse than one that plainly failed to save.
      if (!quiet) toast(`Waypoint not saved to this computer: ${e.message}`);
      return false;
    }
  }

  function showWaypointList() {
    const list = $('waypointList');
    list.innerHTML = '';
    const points = Prefs.waypoints || [];
    $('waypointHint').textContent = points.length
      ? 'Tap Copy to put the position on the clipboard.'
      : 'None yet - long-press the chart to drop one.';

    fetch('waypoints.json')
      .then(r => r.json())
      .then(d => {
        const n = (d.waypoints || []).length;
        $('waypointHint').textContent = points.length
          ? `${n} of ${points.length} saved on this computer for the pipeline.`
          : 'None yet - long-press the chart to drop one.';
      })
      .catch(() => { /* leave the default hint */ });

    points.forEach(([lat, lon, label]) => {
      const li = document.createElement('li');
      const name = document.createElement('span');
      name.className = 'chartName';
      name.innerHTML = `${label}<span class="sub">${lat.toFixed(6)}, ${lon.toFixed(6)}</span>`;

      const copy = document.createElement('button');
      copy.type = 'button';
      copy.textContent = 'Copy';
      copy.addEventListener('click', async () => {
        const text = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
        try {
          await navigator.clipboard.writeText(text);
          toast(`${label}: ${text}`);
        } catch (e) {
          // Clipboard needs a secure context; showing it is the fallback.
          toast(text);
        }
      });

      li.appendChild(name);
      li.appendChild(copy);
      list.appendChild(li);
    });
  }

  /**
   * The charts on the machine serving this page, and what can be done to
   * them: add one that has been built, refresh one whose build has moved on,
   * choose which opens first, remove one that is finished with.
   *
   * Everything the chart tool does, from here - so nothing about running a
   * survey needs a command line. A page served from another machine is told
   * why the buttons are absent rather than being given ones its server would
   * refuse.
   */
  // ── The workflow ───────────────────────────────────────────────────────────

  /**
   * The pipeline as a checklist, with the button that starts each step.
   *
   * Everything before this screen is a Windows program with a launcher
   * nobody remembers, run in an order nobody writes down. The viewer is
   * already open when the next one is needed, and it is talking to a server
   * on the machine they are installed on - so it is the right place to ask.
   */
  async function showWorkflow() {
    const list = $('workflowList');
    list.innerHTML = '';
    $('workflowDialog').showModal();

    let plan;
    try {
      plan = await (await fetch('tools.json')).json();
    } catch (e) {
      $('workflowHint').textContent = 'The chart server did not answer.';
      return;
    }

    $('workflowHint').textContent = plan.canLaunch
      ? 'From planning the lines to charts on this screen.'
      : 'Read-only here: these programs open on the machine holding the charts.';

    for (const step of plan.steps) {
      const li = document.createElement('li');

      // The tick is a button. What is on disk is the default answer, not
      // the only one: planning leaves nothing to find and can never tick
      // itself, and work done months ago may want ticking off by hand.
      const mark = document.createElement('button');
      mark.type = 'button';
      mark.disabled = !plan.canLaunch;
      paintMark(mark, step);
      // Painted before the request, not after it: the tick is the only
      // thing that changes, and rebuilding the list for it cost a round
      // trip and blinked every row. If the server refuses, it goes back.
      let saving = false;
      mark.onclick = async () => {
        if (saving) return;
        saving = true;
        const want = !step.done;
        step.done = want;
        paintMark(mark, step);
        const result = await postJson(
          `tools/${encodeURIComponent(step.id)}/done`,
          { done: want });
        if (!result.ok) {
          step.done = !want;
          paintMark(mark, step);
          toast(result.error || 'That did not save.');
        }
        saving = false;
      };

      const name = document.createElement('span');
      name.className = 'step';
      name.textContent = step.name;

      const what = document.createElement('span');
      what.className = 'what';
      what.textContent = step.what;

      // At most two buttons: the program, and the folder it works in.
      const actions = document.createElement('span');
      actions.className = 'stepActions';
      if (step.settings) {
        actions.appendChild(openerFor(step));
      } else if (step.canLaunch) {
        actions.appendChild(launcherFor(step));
      }
      if (step.folder && plan.canLaunch) {
        actions.appendChild(folderFor(step));
      }
      if (step.canSetFolder && plan.canLaunch) {
        actions.appendChild(chartButton(step.folder ? 'Change' : 'Set', '',
          () => askRecordingsDir(step)));
      }

      li.append(mark, name, actions, what);

      // What the disk says, which is the part a checklist usually lacks.
      if (step.detail) {
        const detail = document.createElement('span');
        detail.className = 'detail';
        detail.textContent = step.detail;
        li.appendChild(detail);
      }
      // The path itself, because the next question after a count is where.
      if (step.folder) {
        const where = document.createElement('span');
        where.className = 'where';
        where.textContent = step.folder
          + (step.canSetFolder && !step.folderIsSet ? '   (found, not set)' : '');
        where.title = 'Click to copy';
        where.onclick = () => {
          navigator.clipboard.writeText(step.folder)
            .then(() => toast('Path copied'))
            .catch(() => toast(step.folder));
        };
        li.appendChild(where);
      }
      if (!step.canLaunch && step.missing) {
        const blocked = document.createElement('span');
        blocked.className = 'blocked';
        blocked.textContent = step.missing;
        li.appendChild(blocked);
      }
      list.appendChild(li);
    }
  }

  /** The button that starts one step's program on the server's machine. */
  function launcherFor(step) {
    return chartButton('Open', '', async button => {
      button.disabled = true;
      button.textContent = 'Starting...';
      const result = await postJson(
        `tools/${encodeURIComponent(step.id)}/launch`);
      button.textContent = result.ok ? 'Opened' : 'Open';
      button.disabled = false;
      toast(result.ok
        ? `${result.name} is opening - look for its window`
        : (result.error || 'That did not start.'));
    });
  }

  /** The tick, drawn for the state the step is in. */
  function paintMark(button, step) {
    button.className = step.done ? 'mark done' : 'mark';
    button.textContent = step.done ? '\u2713' : String(step.phase);
    button.title = step.done ? 'Done - click to untick' : 'Click to tick';
    button.setAttribute('aria-pressed', String(Boolean(step.done)));
  }

  /**
   * Ask where recordings are kept, and tell every other program.
   *
   * A browser cannot hand over a folder path - picking a directory gives
   * it names, never a location on disk - so this is typed or pasted. The
   * server checks the folder exists before keeping it, because every file
   * dialog in the chain opens there afterwards.
   */
  async function askRecordingsDir(step) {
    const typed = prompt(
      'Full path to the folder you copy recordings into:', step.folder || '');
    if (typed === null) return;
    const body = typed.trim() ? { folder: typed } : { clear: true };
    const result = await postJson('tools/recordings', body);
    if (!result.ok) { toast(result.error || 'That folder was not accepted.'); return; }
    toast(result.folder
      ? `Recordings: ${result.folder}`
      : 'Recordings folder cleared');
    showWorkflow();          // redraw with the new counts
  }

  /** Show a step's folder in the file manager on the server's machine. */
  function folderFor(step) {
    return chartButton('Folder', '', async button => {
      button.disabled = true;
      const result = await postJson(
        `tools/${encodeURIComponent(step.id)}/folder`);
      button.disabled = false;
      if (!result.ok) toast(result.error || 'That folder did not open.');
    });
  }

  /** The one step that happens in here rather than in another program. */
  function openerFor(step) {
    return chartButton('Settings', '', () => {
      $('workflowDialog').close();
      openSettings();
    });
  }

  /** Say why the map is empty, in the middle of it, until it is not. */
  function explainEmpty(title, why) {
    $('noChartsTitle').textContent = title;
    $('noChartsWhy').textContent = why;
    $('noCharts').hidden = false;
    toast(title);
  }

  /**
   * Take the notice down again.
   *
   * It had no opposite: a chart loaded after a failed start drew properly
   * underneath a panel still saying it had not, which is worse than the
   * silence it replaced.
   */
  function clearEmptyNotice() {
    $('noCharts').hidden = true;
  }

  async function showChartList() {
    const list = $('chartList');
    const hint = $('chartsHint');
    list.innerHTML = '';
    if (!catalog.canManage) {
      $('chartAdd').hidden = true;
      hint.textContent = 'Charts are served from another machine, so they cannot '
        + 'be added or removed from here. Do it on that machine, in this same '
        + 'panel.';
      renderChartRows([]);
      return;
    }
    $('chartAdd').hidden = false;
    hint.innerHTML = 'Built surveys live under <code>output/</code>; adding one '
      + 'links its files here rather than copying them. A bundle is the same '
      + 'thing as a single .zip, which is also what the phone app reads.';

    let manage = { installed: [], available: [] };
    try {
      manage = await (await fetch('charts/manage.json')).json();
    } catch (e) {
      hint.textContent = 'The chart server did not answer.';
    }
    renderChartRows(manage.installed || []);
    renderAvailable(manage.available || []);
  }

  function renderChartRows(rows) {
    const list = $('chartList');
    list.innerHTML = '';
    if (!catalog.locations.length) {
      const empty = document.createElement('li');
      empty.className = 'chartName';
      empty.textContent = 'No charts yet.';
      list.appendChild(empty);
      return;
    }
    const byId = new Map(rows.map(row => [row.id, row]));

    for (const loc of catalog.locations) {
      const row = byId.get(loc.id) || {};
      const li = document.createElement('li');
      const label = document.createElement('span');
      label.className = 'chartName';
      const layers = Object.keys(loc.layers || {}).join(', ') || 'no tiles';
      const behind = (row.behind || []).length;
      const notes = [`${loc.mb || 0} MB`, layers];
      if (row.default) notes.push('opens here');
      label.innerHTML = `${loc.name}<span class="sub">${notes.join(' \u00b7 ')}</span>`;
      if (behind) {
        const warn = document.createElement('span');
        warn.className = 'sub behind';
        warn.textContent = `${behind} file${behind === 1 ? '' : 's'} behind the `
          + 'build - refresh to catch up';
        label.appendChild(warn);
      }

      const actions = document.createElement('div');
      actions.className = 'chartActions';
      if (!catalog.canManage) {
        li.append(label);
        list.appendChild(li);
        continue;
      }
      if (!row.default) {
        actions.appendChild(chartButton('Open here', '',
          button => manageChart(`charts/${encodeURIComponent(loc.id)}/default`,
                                button, `${loc.name} opens first now`)));
      }
      if (behind || row.builtFrom) {
        const refresh = chartButton(behind ? 'Refresh' : 'Re-link',
          behind ? 'current' : '',
          button => manageChart(`charts/${encodeURIComponent(loc.id)}/refresh`,
                                button, `${loc.name} refreshed`));
        actions.appendChild(refresh);
      }
      // Detections arrive separately from the chart: GhostVision is run
      // after the survey is built, and usually on the machine with the GPU.
      // This is the join between the two.
      const found = Boolean((loc.data || {}).detections);
      actions.appendChild(chartButton(found ? 'Replace objects' : 'Add objects', '',
        () => pickDetections(loc)));
      if (found) {
        actions.appendChild(chartButton('Clear objects', '',
          button => manageChart(
            `charts/${encodeURIComponent(loc.id)}/detections/clear`,
            button, `Detections cleared from ${loc.name}`)));
      }
      const remove = chartButton('Remove', 'danger',
        button => removeChart(loc, button));
      actions.appendChild(remove);
      li.append(label, actions);
      list.appendChild(li);
    }
  }

  function chartButton(text, className, onClick) {
    const button = document.createElement('button');
    button.type = 'button';
    if (className) button.className = className;
    button.textContent = text;
    button.onclick = () => onClick(button);
    return button;
  }

  function renderAvailable(rows) {
    const list = $('chartAvailable');
    const title = $('availableTitle');
    list.innerHTML = '';
    if (!rows.length) {
      title.textContent = 'Surveys ready to add';
      const li = document.createElement('li');
      li.className = 'chartName sub';
      li.textContent = 'None waiting - every built survey is already here.';
      list.appendChild(li);
      return;
    }
    title.textContent = `Surveys ready to add (${rows.length})`;
    for (const row of rows) {
      const li = document.createElement('li');
      const label = document.createElement('span');
      label.className = 'chartName';
      const layers = (row.layers || []).join(', ') || 'no tiles';
      label.innerHTML = `${row.name}<span class="sub">${row.mb || 0} MB \u00b7 ${layers}</span>`;
      const add = chartButton('Add', 'current', async button => {
        button.disabled = true;
        button.textContent = 'Adding...';
        const result = await postJson('charts/add', { source: row.path });
        if (!result.ok) {
          button.disabled = false;
          button.textContent = 'Add';
          toast(result.error || 'Could not add that survey.');
          return;
        }
        await reloadCatalog();
        toast(`${result.name} added`);
        showChartList();
      });
      li.append(label, add);
      list.appendChild(li);
    }
  }

  async function postJson(path, body) {
    try {
      const response = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      return await response.json();
    } catch (e) {
      return { ok: false, error: 'The chart server did not answer.' };
    }
  }

  async function reloadCatalog() {
    catalog = await (await fetch('catalog.json')).json();
    ChartUrl.remember(catalog);
  }

  /** One of the little chart buttons: post, report, redraw. */
  async function manageChart(path, button, done) {
    const was = button.textContent;
    button.disabled = true;
    button.textContent = '...';
    const result = await postJson(path);
    if (!result.ok) {
      button.disabled = false;
      button.textContent = was;
      toast(result.error || 'That did not work.');
      return;
    }
    await reloadCatalog();
    toast(done);
    showChartList();
    // A refreshed chart is different tiles under the same name, so redraw it.
    const fresh = catalog.locations.find(loc => loc.id === result.id);
    if (fresh && location && location.id === result.id) switchLocation(fresh);
  }

  /**
   * Take in a chart bundle: one file holding a whole survey.
   *
   * The phone reads the same bundle through its own Settings screen, and
   * neither app needs the other or anything from Play. Here the file is
   * posted to the machine serving this page, which unpacks it into the chart
   * library - so a browser reading the charts over the boat's wifi is not
   * offered a button that its server would refuse.
   */
  // Which chart the file picker is about to file its detections under.
  let detectionTarget = null;

  function wireDetectionUpload() {
    const input = $('detectionFile');
    input.addEventListener('change', () => {
      const file = input.files && input.files[0];
      input.value = '';       // so the same file can be picked again
      if (file && detectionTarget) uploadDetections(detectionTarget, file);
    });
  }

  function pickDetections(loc) {
    detectionTarget = loc;
    $('detectionFile').click();
  }

  /**
   * Take in a GhostVision run: the crab pots it found in one survey.
   *
   * The detector knows nothing about the chart library - it writes its
   * findings beside its own output, under its own name - so naming the
   * survey is the user's half of the job and cannot be guessed here. A
   * list of points is small enough to read whole and post as text; the
   * server checks it and files it with that chart.
   */
  async function uploadDetections(loc, file) {
    const status = $('potsStatus');
    const say = message => {
      status.hidden = false;
      status.textContent = message;
    };
    if (file.size > 20e6) {
      say(`${file.name} is too large to be a list of detections.`);
      return;
    }
    say(`Reading ${file.name}\u2026`);
    let text;
    try {
      text = await file.text();
    } catch (e) {
      say(`Could not read ${file.name}.`);
      return;
    }

    let result;
    try {
      const response = await fetch(
        `charts/${encodeURIComponent(loc.id)}/detections`,
        { method: 'POST', headers: { 'Content-Type': 'application/geo+json' },
          body: text });
      result = await response.json();
    } catch (e) {
      result = { ok: false, error: 'The chart server did not answer.' };
    }
    if (!result.ok) {
      say(result.error || 'That file was not accepted.');
      toast(result.error || 'Detections not accepted');
      return;
    }

    const many = result.count === 1 ? '' : 's';
    say(`${result.count} detection${many} added to ${loc.name}.`);
    await reloadCatalog();
    showChartList();
    const fresh = catalog.locations.find(entry => entry.id === loc.id);
    if (fresh && location && location.id === loc.id) switchLocation(fresh);
    toast(`${result.count} object${many} on ${loc.name}`);
  }

  function wireChartUpload() {
    const input = $('chartFile');
    $('btnAddChart').addEventListener('click', () => input.click());
    input.addEventListener('change', () => {
      const file = input.files && input.files[0];
      input.value = '';        // so the same file can be picked again
      if (file) uploadChart(file);
    });
  }

  function uploadChart(file) {
    const button = $('btnAddChart');
    const bar = $('chartUpload');
    const status = $('chartUploadStatus');
    const mb = bytes => `${Math.round(bytes / 1e6)} MB`;

    button.disabled = true;
    bar.hidden = false;
    bar.value = 0;
    status.hidden = false;
    status.textContent = `Sending ${file.name} (${mb(file.size)})...`;

    const done = (message) => {
      button.disabled = false;
      bar.hidden = true;
      status.textContent = message;
    };

    // XHR rather than fetch: a 300 MB bundle takes long enough that a
    // progress bar is the difference between working and hung, and fetch
    // still cannot report upload progress.
    const xhr = new XMLHttpRequest();
    xhr.open('POST', 'charts/import');
    xhr.setRequestHeader('Content-Type', 'application/zip');
    xhr.setRequestHeader('X-Chart-Filename', file.name.replace(/[^\w.\- ]+/g, '_'));
    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      bar.value = Math.round((e.loaded / e.total) * 100);
      status.textContent = e.loaded < e.total
        ? `Sending ${mb(e.loaded)} of ${mb(e.total)}...`
        : 'Unpacking the chart...';
    };
    xhr.onload = async () => {
      let result;
      try {
        result = JSON.parse(xhr.responseText);
      } catch (e) {
        result = { ok: false, error: `The chart server answered ${xhr.status}.` };
      }
      if (!result.ok) {
        done(result.error || 'That bundle could not be added.');
        return;
      }
      await reloadCatalog();
      showChartList();
      done(`${result.name} added (${result.mb || 0} MB).`);
      toast(`${result.name} added`);
      // A rebuilt chart of the one on screen is a different chart now: its
      // URLs carry a new revision, so redraw from them rather than leaving
      // the old tiles up.
      const fresh = catalog.locations.find(loc => loc.id === result.id);
      if (fresh && location && location.id === result.id) switchLocation(fresh);
    };
    xhr.onerror = () => done('The chart server did not answer.');
    xhr.send(file);
  }
  async function removeChart(loc, button) {
    const last = catalog.locations.length <= 1;
    const warning = last
      ? `Remove ${loc.name}? It is the only chart, so the app will have nothing `
        + 'to show until you add another.'
      : `Remove ${loc.name} (${loc.mb || 0} MB) from this computer? `
        + 'The chart files are deleted; the survey it was built from is untouched.';
    if (!confirm(warning)) return;

    button.disabled = true;
    button.textContent = 'Removing...';
    let result;
    try {
      const response = await fetch(`charts/${encodeURIComponent(loc.id)}/remove`,
                                   { method: 'POST' });
      result = await response.json();
    } catch (e) {
      result = { ok: false, error: 'The chart server did not answer.' };
    }
    if (!result.ok) {
      button.disabled = false;
      button.textContent = 'Remove';
      toast(result.error || 'Could not remove that chart.');
      return;
    }

    toast(`${loc.name} removed`);
    await reloadCatalog();
    showChartList();
    // If the chart on screen was the one deleted, move to whatever is left.
    if (location && location.id === loc.id) {
      const next = catalog.locations[0];
      if (next) switchLocation(next);
      else toast('No charts left - add one under Charts on this computer.');
    }
  }

  function showLocations() {
    const list = $('locationList');
    list.innerHTML = '';
    // There may be no current survey - the chart can fail to load, and did
    // whenever the server was down. Reading its id anyway threw on the way
    // up, which reached the user as a button that does nothing at all.
    const currentId = location ? location.id : null;
    if (!catalog.locations.length) {
      const li = document.createElement('li');
      li.className = 'empty';
      li.textContent = catalogLoaded
        ? 'No surveys in the library yet - add one in Settings.'
        : 'No answer from the chart server - start it, then reload.';
      list.appendChild(li);
    }
    for (const loc of catalog.locations) {
      const li = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = loc.id === currentId ? 'current' : '';
      const layers = Object.keys(loc.layers || {}).join(', ') || 'no tiles';
      button.innerHTML = `${loc.name}${loc.id === currentId ? ' &bull;' : ''}<span class="sub">${layers}</span>`;
      button.onclick = () => {
        $('locationsDialog').close();
        if (loc.id === currentId) ChartMap.flyTo(loc.lat, loc.lon, loc.zoom);
        else switchLocation(loc);
      };
      // A second, quieter control per survey: where its charts came from.
      const about = document.createElement('button');
      about.type = 'button';
      about.className = 'about';
      about.title = `Where ${loc.name}'s charts came from`;
      about.textContent = 'i';
      about.onclick = () => { $('locationsDialog').close(); SurveySource.show(loc); };
      li.appendChild(button);
      li.appendChild(about);
      list.appendChild(li);
    }
    $('locationsDialog').showModal();
  }

  /**
   * What the detector found here, in the detector's own terms.
   *
   * Confidence is reported as it stands. A 46% crab pot is a maybe, and
   * rounding that away would make the chart look more certain than the
   * sonar ever was - the number is the reason to go and look.
   */
  function describeDetection(pot) {
    const props = pot.properties || {};
    const parts = [String(props.class_name || 'Object').replace(/[-_]/g, ' ')];
    const confidence = Number(props.confidence);
    if (Number.isFinite(confidence)) {
      parts.push(`${Math.round(confidence * 100)}% sure`);
    }
    const charted = DepthGrid.depthAt(pot.lat, pot.lon);
    if (charted !== null) {
      parts.push(Units.depth(charted + tideOffset, Prefs.depthUnit));
    }
    parts.push(`${pot.lat.toFixed(5)}, ${pot.lon.toFixed(5)}`);
    toast(parts.join('  \u2022  '));
  }

  // ── Waypoints ──────────────────────────────────────────────────────────────

  function renderWaypoints() {
    ChartMap.setWaypoints({
      type: 'FeatureCollection',
      features: Prefs.waypoints.map(([lat, lon, label]) => ({
        type: 'Feature',
        properties: { label },
        geometry: { type: 'Point', coordinates: [lon, lat] },
      })),
    });
  }

  function askWaypoint(lat, lon) {
    pendingWaypoint = [lat, lon];
    $('waypointLabel').value = '';
    $('waypointDialog').showModal();
  }

  // ── Fence + anchor ─────────────────────────────────────────────────────────

  function restoreFenceRender() {
    if (AnchorWatch.center) {
      const [lon, lat] = AnchorWatch.center;
      ChartMap.setCircle(lat, lon, AnchorWatch.radiusM);
    } else if (AnchorWatch.ring) {
      ChartMap.setFence(AnchorWatch.ring, { closed: true });
    } else {
      ChartMap.setFence(fenceVerts, { closed: false, showVerts: drawMode });
    }
  }

  function toggleDrawFence() {
    if (drawMode) {
      drawMode = false;
      $('btnDrawFence').textContent = 'DRAW';
      if (fenceVerts.length >= 3) {
        AnchorWatch.setGeofence(fenceVerts);
        ChartMap.setFenceColor('#00C853');
        toast('Watch armed on the drawn fence');
      } else {
        fenceVerts = [];
        toast('Need at least three points');
      }
      restoreFenceRender();
    } else {
      clearFence();
      drawMode = true;
      $('btnDrawFence').textContent = 'FINISH';
      toast('Tap the chart to place fence points');
    }
    updateAnchorInfo();
  }

  function clearFence() {
    drawMode = false;
    fenceVerts = [];
    AnchorWatch.clearGeofence();
    $('btnDrawFence').textContent = 'DRAW';
    $('alarmBanner').hidden = true;
    ChartMap.setFenceColor('#00C853');
    ChartMap.clearFenceRender();
    updateAnchorInfo();
  }

  function dropAnchor() {
    const fix = AnchorWatch.lastFix;
    if (!fix) { toast('Waiting for a GPS fix…'); return; }

    // Depth-aware scope: swing radius ≈ horizontal rode (charted depth + tide,
    // through the scope ratio) + boat length. Off-survey it falls back to 30 m.
    const charted = DepthGrid.depthAt(fix.lat, fix.lon);
    const nowDepth = charted === null ? null : charted + tideOffset;
    const scope = Prefs.scopeRatio;
    const suggested = nowDepth && nowDepth > 0
      ? Math.min(150, Math.max(10,
        Math.round(nowDepth * Math.sqrt(scope * scope - 1) + Prefs.boatLengthM)))
      : 30;

    $('anchorDepthLine').textContent = nowDepth === null
      ? 'Depth unknown here — set the radius manually'
      : `Charted depth: ${Units.depth(nowDepth, Prefs.depthUnit)} (now)`
        + ` · scope ${scope}:1 + ${Math.round(Prefs.boatLengthM)} m LOA`;
    $('anchorRadius').value = suggested;
    $('anchorRadiusLabel').firstChild.textContent = `Swing radius: ${suggested} m`;
    $('anchorDialog').showModal();
  }

  function updateAnchorInfo() {
    const info = $('anchorInfo');
    if (!AnchorWatch.isArmed) { info.hidden = true; return; }
    const elapsed = AnchorWatch.anchorTimeMs
      ? Math.floor((Date.now() - AnchorWatch.anchorTimeMs) / 60000) : 0;
    const parts = [`⚓ ${Math.floor(elapsed / 60)}h${String(elapsed % 60).padStart(2, '0')}m`];
    const dist = AnchorWatch.distanceFromAnchor(AnchorWatch.lastFix);
    if (dist !== null) parts.push(`${Math.round(dist)}m / ${Math.round(AnchorWatch.radiusM)}m`);
    parts.push(`max ${Math.round(AnchorWatch.maxDriftM)}m`);
    if (AnchorWatch.isGpsLost) parts.push('⚠ GPS LOST');
    info.textContent = parts.join('  •  ');
    info.hidden = false;
  }

  // ── GPS ────────────────────────────────────────────────────────────────────

  function onFix(fix, inside) {
    if (!fix) return;
    ChartMap.setGpsDot(fix.lat, fix.lon);
    ChartMap.setTrack(AnchorWatch.trackPoints);

    const parts = [`${fix.lat.toFixed(5)}, ${fix.lon.toFixed(5)}`];
    if (fix.speed !== null) parts.push(Units.speedKnots(fix.speed));
    if (fix.heading !== null && fix.speed > 0.3) {
      parts.push(`${String(Math.round(fix.heading)).padStart(3, '0')}°`);
    }
    // Depth under the boat is "now", whatever time the chart is showing.
    const charted = DepthGrid.depthAt(fix.lat, fix.lon);
    if (charted !== null) {
      const now = charted + TideModel.heightAboveLAT(Date.now());
      parts.push(`▼ ${Units.depth(now, Prefs.depthUnit)}`);
      ChartMap.tintBoat(now);
    } else {
      ChartMap.tintBoat(null);
    }
    $('navReadout').textContent = parts.join('  ');
    $('navReadout').hidden = false;

    if (AnchorWatch.isArmed) {
      ChartMap.setFenceColor(inside ? '#00C853' : '#D50000');
      $('alarmBanner').hidden = inside && !AnchorWatch.isGpsLost;
      $('alarmText').textContent = AnchorWatch.isGpsLost
        ? '⚠ GPS LOST — position unknown'
        : '⚠ DRAGGING — outside the watch area';
    }
    updateAnchorInfo();
  }

  // ── Developer data ─────────────────────────────────────────────────────────

  let fps = 0;
  let devTimer = null;

  /**
   * Frame rate, sampled from the render loop rather than guessed: MapLibre
   * fires `render` for every frame it draws, so counting them over a second is
   * the honest number. A still map draws nothing, so the count decays to
   * zero, which is the truth: no frames were drawn.
   */
  function startFpsCounter() {
    let frames = 0;
    ChartMap.map.on('render', () => { frames += 1; });
    setInterval(() => { fps = frames; frames = 0; }, 1000);
  }

  function applyDevData() {
    const on = Prefs.devData && Prefs.hudVisible;
    $('devOverlay').hidden = !on;
    clearInterval(devTimer);
    devTimer = on ? setInterval(refreshDevOverlay, 1000) : null;
    if (on) refreshDevOverlay();
  }

  function refreshDevOverlay() {
    const map = ChartMap.map;
    if (!map) return;
    const centre = map.getCenter();
    const layersOn = Object.keys(layerOf)
      .filter(name => Prefs[prefOn[name]] && ChartMap.hasLayer(name))
      .join(',') || 'none';
    const fix = AnchorWatch.lastFix;
    const gps = fix
      ? `${fix.lat.toFixed(5)},${fix.lon.toFixed(5)}  +-${Math.round(fix.accuracy)}m`
        + `  ${Math.round((Date.now() - fix.time) / 1000)}s ago`
      : 'no fix';

    $('devOverlay').textContent = [
      `zoom ${map.getZoom().toFixed(2)}   ${ChartMap.metresPerPixel().toFixed(2)} m/px   fps ${fps}`,
      `cam  ${centre.lat.toFixed(5)}, ${centre.lng.toFixed(5)}  brg ${map.getBearing().toFixed(0)}`,
      // The overlay is switched on before the first chart is loaded, and
      // keeps running on a timer, so it has to survive having no survey.
      `survey ${location ? location.id : '(none yet)'}`,
      `layers ${layersOn}  style ${map.isStyleLoaded() ? 'ready' : 'loading'}`,
      `grid ${DepthGrid.isLoaded ? 'loaded' : 'none'}  tide ${tideOffset >= 0 ? '+' : ''}`
        + `${tideOffset.toFixed(2)}m  contours ${Prefs.contourIntervalFt}ft`,
      `gps  ${gps}`,
      `watch ${AnchorWatch.isBreached ? 'BREACHED' : AnchorWatch.isArmed ? 'armed' : 'off'}`
        + `  rec ${AnchorWatch.isRecording ? 'on' : 'off'}`,
    ].join(String.fromCharCode(10));
  }

  // ── Day/night + HUD ────────────────────────────────────────────────────────

  function applyDayNight(night) {
    document.body.classList.toggle('night', night);
    $('toggleDayNight').setAttribute('aria-pressed', String(night));
    $('toggleDayNight').innerHTML = night ? '&#9788; DAY' : '&#9789; NIGHT';
  }

  function applyHudVisible(visible) {
    document.body.classList.toggle('hud-off', !visible);
    applyDevData();          // the overlay is part of the HUD
    $('fabHud').classList.toggle('dim', !visible);
    updateLegends();
    measurePanel();
  }

  // ── Wiring ─────────────────────────────────────────────────────────────────

  function wireLayerRows() {
    for (const name of Object.keys(layerOf)) {
      const row = document.querySelector(`.row.layer[data-layer="${name}"]`);
      row.querySelector('.toggle').addEventListener('click', () => {
        Prefs[prefOn[name]] = !Prefs[prefOn[name]];
        applyControlsToChart();
      });
      row.querySelector('.opacity').addEventListener('input', e => {
        const value = Number(e.target.value);
        Prefs[prefOpacity[name]] = value;
        ChartMap.setOpacity(layerOf[name], value / 100);
      });
    }
    $('togglePots').addEventListener('click', () => {
      Prefs.potsOn = !Prefs.potsOn;
      applyControlsToChart();
    });
  }

  function wireTide() {
    $('seekMinDepth').addEventListener('input', e => {
      Prefs.minDepthProgress = Number(e.target.value);
      applyShallowThreshold(Prefs.minDepthProgress);
    });
    $('btnTideNow').addEventListener('click', () => {
      targetTimeMillis = Date.now();
      recomputeTide();
    });
    $('btnNextHigh').addEventListener('click', () => {
      if (!TideModel.isTidal) { toast('No tide model for this survey'); return; }
      targetTimeMillis = TideModel.nextExtreme(Date.now(), true);
      recomputeTide();
      toast(`Next high: ${Units.stamp(targetTimeMillis)}`);
    });
    $('btnNextLow').addEventListener('click', () => {
      if (!TideModel.isTidal) { toast('No tide model for this survey'); return; }
      targetTimeMillis = TideModel.nextExtreme(Date.now(), false);
      recomputeTide();
      toast(`Next low: ${Units.stamp(targetTimeMillis)}`);
    });
    $('btnTideTime').addEventListener('click', () => {
      const d = new Date(targetTimeMillis - new Date().getTimezoneOffset() * 60000);
      $('timeInput').value = d.toISOString().slice(0, 16);
      $('timeDialog').showModal();
    });
    $('timeDialog').addEventListener('close', () => {
      if ($('timeDialog').returnValue !== 'set') return;
      const value = $('timeInput').value;
      if (value) {
        targetTimeMillis = new Date(value).getTime();
        recomputeTide();
      }
    });
  }

  function wireAnchor() {
    $('btnDropAnchor').addEventListener('click', dropAnchor);
    $('btnDrawFence').addEventListener('click', toggleDrawFence);
    $('btnClearFence').addEventListener('click', clearFence);
    $('btnTestAlarm').addEventListener('click', () => {
      AnchorWatch.testAlarm();
      toast('Alarm test');
    });
    $('btnSilence').addEventListener('click', () => {
      AnchorWatch.silence();
      $('alarmBanner').hidden = true;
    });

    $('anchorRadius').addEventListener('input', e => {
      $('anchorRadiusLabel').firstChild.textContent = `Swing radius: ${e.target.value} m`;
    });
    $('anchorDialog').addEventListener('close', () => {
      if ($('anchorDialog').returnValue !== 'arm') return;
      const fix = AnchorWatch.lastFix;
      if (!fix) return;
      const radius = Math.max(5, Number($('anchorRadius').value));
      AnchorWatch.armCircle(fix.lat, fix.lon, radius);
      ChartMap.setCircle(fix.lat, fix.lon, radius);
      ChartMap.setFenceColor('#00C853');
      updateAnchorInfo();
      toast(`Watch armed — ${radius} m swing`);
    });

    $('btnRecord').addEventListener('click', () => {
      if (AnchorWatch.isRecording) {
        AnchorWatch.setRecording(false);
        $('btnRecord').textContent = 'RECORD TRACK';
        const count = AnchorWatch.trackPoints.length;
        if (count) {
          AnchorWatch.exportGpx();
          toast(`Recording stopped — ${count} points, GPX downloaded`);
        } else {
          toast('Recording stopped — nothing recorded');
        }
      } else {
        AnchorWatch.setRecording(true);
        $('btnRecord').textContent = 'STOP + EXPORT';
        toast('Recording started');
      }
    });

    $('btnWaypoint').addEventListener('click', () => {
      const centre = ChartMap.map.getCenter();
      askWaypoint(centre.lat, centre.lng);
    });
    $('waypointDialog').addEventListener('close', () => {
      const action = $('waypointDialog').returnValue;
      if (action === 'clear') {
        Prefs.waypoints = [];
        pushWaypoints();
        renderWaypoints();
        toast('Waypoints cleared');
      } else if (action === 'save' && pendingWaypoint) {
        const label = $('waypointLabel').value.trim() || 'WP';
        Prefs.waypoints = [...Prefs.waypoints, [pendingWaypoint[0], pendingWaypoint[1], label]];
        pushWaypoints({ quiet: false });
        renderWaypoints();
      }
      pendingWaypoint = null;
    });
  }

  function wireRail() {
    $('fabGps').addEventListener('click', () => {
      const fix = AnchorWatch.lastFix;
      if (!fix) { toast('Waiting for a GPS fix…'); AnchorWatch.start(onFix); return; }
      ChartMap.flyTo(fix.lat, fix.lon);
    });
    $('fabUpdate').addEventListener('click', () => {
      switchLocation(location);
      toast('Chart reloaded');
    });
    $('fabKey').addEventListener('click', () => {
      Prefs.keyVisible = !Prefs.keyVisible;
      $('fabKey').classList.toggle('dim', !Prefs.keyVisible);
      updateLegends();
    });
    $('fabWorkflow').addEventListener('click', showWorkflow);
    $('fabSettings').addEventListener('click', openSettings);
    wireChartUpload();
    wireDetectionUpload();
    wireSettingGroups();
    $('fabLocations').addEventListener('click', showLocations);
    $('fabHud').addEventListener('click', () => {
      Prefs.hudVisible = !Prefs.hudVisible;
      applyHudVisible(Prefs.hudVisible);
    });
    $('toggleDayNight').addEventListener('click', () => {
      Prefs.nightMode = !Prefs.nightMode;
      applyDayNight(Prefs.nightMode);
    });
  }

  const SETTING_GROUPS = ['grpGeneral', 'grpOffline', 'grpWaypoints', 'grpCharts'];

  /** Fold the settings sections the way they were left. */
  function applySettingGroups() {
    const open = Prefs.settingsOpen || {};
    for (const id of SETTING_GROUPS) {
      const group = $(id);
      if (group) group.open = Boolean(open[id]);
    }
  }

  function wireSettingGroups() {
    for (const id of SETTING_GROUPS) {
      const group = $(id);
      if (!group) continue;
      group.addEventListener('toggle', () => {
        Prefs.settingsOpen = { ...Prefs.settingsOpen, [id]: group.open };
      });
    }
  }

  function openSettings() {
    applySettingGroups();
    showChartList();
    showWaypointList();
    $('setUnit').value = Prefs.depthUnit;
    $('setScope').value = Prefs.scopeRatio;
    $('setScopeLabel').firstChild.textContent = `Default scope ratio: ${Prefs.scopeRatio}:1`;
    $('setBoat').value = Math.round(Prefs.boatLengthM);
    $('setBoatLabel').firstChild.textContent = `Boat length (LOA): ${Math.round(Prefs.boatLengthM)} m`;
    $('setContour').value = Prefs.contourIntervalFt;
    $('setContourLabel').firstChild.textContent = `Depth contours: every ${Prefs.contourIntervalFt} ft`;
    $('setEscalate').checked = Prefs.alarmEscalate;
    $('setCenterGps').checked = Prefs.startCenterOnGps;
    $('setKeepAwake').checked = Prefs.keepAwake;
    $('setPreviews').checked = Prefs.alwaysShowPreviews;
    $('setDevData').checked = Prefs.devData;
    refreshOfflineState();
    $('settingsDialog').showModal();
  }

  // ── Offline / install ──────────────────────────────────────────────────────

  async function refreshOfflineState(message) {
    const state = $('offlineState');
    const save = $('btnSaveOffline');
    const clear = $('btnClearOffline');
    const install = $('btnInstall');

    install.hidden = !Offline.canInstall;
    if (!Offline.supported()) {
      // Plain http on a LAN address is not a secure context, so the browser
      // refuses to install or cache anything. Say why rather than failing mutely.
      state.textContent = 'Offline use needs https, or opening the app on '
        + 'localhost. Over the network it works as a normal page.';
      save.disabled = true;
      clear.disabled = true;
      return;
    }
    save.disabled = Offline.isSaving;
    clear.disabled = Offline.isSaving;
    if (message) { state.textContent = message; return; }

    const saved = await Offline.savedCount();
    let size = '';
    try {
      size = ` This survey is about ${(await Offline.surveySize(location.id)).label}.`;
    } catch (e) { /* the index needs the server; skip the estimate offline */ }
    state.textContent = saved
      ? `${saved} chart files saved for offline use.${size}`
      : `No charts saved yet.${size}`;
  }

  function wireOffline() {
    document.addEventListener('offline-state', () => {
      if ($('settingsDialog').open) refreshOfflineState();
    });

    $('btnInstall').addEventListener('click', async () => {
      const accepted = await Offline.promptInstall();
      if (accepted) toast('Installed — look for Anchoring in your apps');
      refreshOfflineState();
    });

    $('btnSaveOffline').addEventListener('click', async () => {
      try {
        const result = await Offline.saveSurvey(location, (done, total) => {
          refreshOfflineState(`Saving ${location.name}: ${done} / ${total} files…`);
        });
        if (!result || result.stored === 0) {
          // Storage can be refused outright; do not pretend the charts are safe.
          const why = result && result.error ? ` (${result.error.message})` : '';
          refreshOfflineState(`This browser would not store the charts${why}. `
            + 'Check that site storage is allowed and the disk is not full.');
          return;
        }
        await refreshOfflineState();
        toast(result.stored === result.total
          ? `${location.name} saved for offline use`
          : `${location.name}: saved ${result.stored} of ${result.total} files`);
      } catch (e) {
        refreshOfflineState(`Could not save: ${e.message}`);
      }
    });

    $('btnClearOffline').addEventListener('click', async () => {
      await Offline.clear();
      refreshOfflineState('Saved charts cleared.');
    });
  }

  function wireSettings() {
    $('setUnit').addEventListener('change', e => {
      Prefs.depthUnit = e.target.value;
      recomputeTide();
      updateLegends();
      if (AnchorWatch.lastFix) onFix(AnchorWatch.lastFix, !AnchorWatch.isBreached);
    });
    $('setScope').addEventListener('input', e => {
      Prefs.scopeRatio = Number(e.target.value);
      $('setScopeLabel').firstChild.textContent = `Default scope ratio: ${Prefs.scopeRatio}:1`;
    });
    $('setBoat').addEventListener('input', e => {
      Prefs.boatLengthM = Number(e.target.value);
      $('setBoatLabel').firstChild.textContent = `Boat length (LOA): ${Math.round(Prefs.boatLengthM)} m`;
    });
    $('setContour').addEventListener('input', e => {
      Prefs.contourIntervalFt = Number(e.target.value);
      $('setContourLabel').firstChild.textContent =
        `Depth contours: every ${Prefs.contourIntervalFt} ft`;
      ChartMap.applyContours(Prefs.contourIntervalFt, tideOffset, Prefs.depthUnit);
    });
    $('setEscalate').addEventListener('change', e => { Prefs.alarmEscalate = e.target.checked; });
    $('setCenterGps').addEventListener('change', e => { Prefs.startCenterOnGps = e.target.checked; });
    $('setKeepAwake').addEventListener('change', e => { Prefs.keepAwake = e.target.checked; });
    $('setPreviews').addEventListener('change', e => {
      Prefs.alwaysShowPreviews = e.target.checked;
      renderPreviews();
    });
    $('setDevData').addEventListener('change', e => {
      Prefs.devData = e.target.checked;
      applyDevData();
    });
  }

  function wireMapGestures() {
    const map = ChartMap.map;

    map.on('click', e => {
      if (drawMode) {
        fenceVerts.push([e.lngLat.lng, e.lngLat.lat]);
        ChartMap.setFence(fenceVerts, { closed: false, showVerts: true });
        return;
      }
      const pinId = ChartMap.pinAt(e.point);
      if (pinId) {
        const target = catalog.locations.find(l => l.id === pinId);
        if (target && target.id !== location.id) {
          switchLocation(target);
          toast(target.name);
          return;
        }
        // The pin of the chart you are already on: say where it came from,
        // rather than re-centring a map that is already centred.
        if (target) { SurveySource.show(target); return; }
      }
      // A pot is a small mark on a big chart, and the depth query would
      // happily answer for the water on top of it, so ask about it first.
      const pot = ChartMap.detectionAt(e.point);
      if (pot) { describeDetection(pot); return; }
      // Tap-to-query: charted depth (tide-corrected) plus substrate.
      const charted = DepthGrid.depthAt(e.lngLat.lat, e.lngLat.lng);
      if (charted === null) return;      // ignore taps outside the survey
      const sub = DepthGrid.substrateAt(e.lngLat.lat, e.lngLat.lng);
      toast(`Depth ${Units.depth(charted + tideOffset, Prefs.depthUnit)}${sub ? `  •  ${sub}` : ''}`);
    });

    // Long-press (or right-click) drops a waypoint.
    map.on('contextmenu', e => askWaypoint(e.lngLat.lat, e.lngLat.lng));
    let pressTimer = null;
    const cancel = () => { clearTimeout(pressTimer); pressTimer = null; };
    map.on('touchstart', e => {
      if (e.points.length !== 1 || drawMode) return;
      const { lngLat } = e;
      pressTimer = setTimeout(() => askWaypoint(lngLat.lat, lngLat.lng), 650);
    });
    map.on('touchend', cancel);
    map.on('touchcancel', cancel);
    map.on('touchmove', cancel);
    map.on('movestart', cancel);
  }

  // ── Start-up ───────────────────────────────────────────────────────────────

  async function main() {
    wireLayerRows();
    wireImagePanel();
    wireTide();
    wireAnchor();
    wireRail();
    wireSettings();
    wireOffline();
    Offline.register();
    applyDayNight(Prefs.nightMode);
    applyHudVisible(Prefs.hudVisible);
    $('fabKey').classList.toggle('dim', !Prefs.keyVisible);
    $('seekMinDepth').value = Prefs.minDepthProgress;
    // The key's height is viewport-relative, and a window moved to another
    // monitor can change the pixel ratio, so redraw it whenever either shifts.
    window.addEventListener('resize', () => { measurePanel(); updateLegends(); });

    try {
      catalog = await (await fetch('catalog.json')).json();
      catalogLoaded = true;
      ChartUrl.remember(catalog);
      // Mirror on load as well as on save: pins dropped before the server
      // could store them would otherwise stay stranded in this browser.
      pushWaypoints();
    } catch (e) {
      // The layer rows hide themselves when no chart is loaded, so an app
      // that cannot reach its server looks like an app with nothing wrong
      // and no buttons on it. Say what happened, and leave it said: the
      // toast that used to carry this was gone before anyone read it.
      explainEmpty('No chart server',
        `This page loaded, but nothing answered for the charts (${e.message}).`);
      return;
    }
    if (!catalog.locations.length) {
      // Nothing to draw, but there is something to do: the panel that takes
      // a chart bundle is the whole of what this install needs next.
      explainEmpty('No charts yet',
        'The chart server is running and its library is empty.');
      openSettings();
      return;
    }

    const start = catalog.locations.find(l => l.id === Prefs.lastLocationId)
      || catalog.locations.find(l => l.id === catalog.defaultLocationId)
      || catalog.locations[0];

    // Everything below draws the chart. Unguarded, a failure here threw out
    // of an async main() nobody awaits: no chart, no message, and the rail
    // buttons still sitting there looking clickable.
    try {
      await ChartMap.init('map', start);
      wireMapGestures();
      ChartMap.setScaleUnit(Prefs.depthUnit);
      startFpsCounter();
      applyDevData();

      await switchLocation(start);
    } catch (e) {
      // The banner is for whoever is holding the tablet; the stack is for
      // whoever has to fix it, and there is no other copy of it anywhere.
      console.error('chart failed to load', e);
      explainEmpty('Chart did not load',
        `${start.name} is in the library, but did not draw (${e.message}).`);
      return;
    }

    if (AnchorWatch.restore()) {
      restoreFenceRender();
      updateAnchorInfo();
      toast('Anchor watch restored');
    }
    AnchorWatch.start(onFix);
    if (Prefs.startCenterOnGps) {
      setTimeout(() => {
        const fix = AnchorWatch.lastFix;
        if (fix) ChartMap.flyTo(fix.lat, fix.lon);
      }, 2500);
    }

    // The anchor panel counts minutes, so it needs a tick of its own.
    setInterval(updateAnchorInfo, 15000);
    measurePanel();
  }

  document.addEventListener('DOMContentLoaded', main);
})();
