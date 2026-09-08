/**
 * Everything that survives a reload: layer state, settings, the armed watch and
 * saved waypoints. Backed by localStorage, so a tab crash on the water loses
 * nothing.
 */
const Prefs = (() => {
  const KEY = 'anchoring';
  const DEFAULTS = {
    bathyOn: true, sonarOn: false, substrateOn: false, rockOn: false,
    // Crab pots default on: a chart only has them because someone went
    // looking, and the whole reason to open that chart is to see them.
    potsOn: true,

    bathyOpacity: 100, sonarOpacity: 100, substrateOpacity: 80, rockOpacity: 80,
    // How each sonar mosaic is drawn, not what it contains. Keyed by survey id,
    // because the right brightness for a 0.5 m multibeam mosaic is the wrong
    // one for a side-scan sheet - one global setting would be re-dialled on
    // every switch. { <id>: { brightness, contrast, sharp } }, -100..100 each.
    sonarImage: {},
    // Which settings sections are folded open. Remembered because the one
    // you use every day should be the one that opens.
    settingsOpen: { grpGeneral: true, grpOffline: false,
                    grpWaypoints: false, grpCharts: true },
    minDepthProgress: 6,

    nightMode: false, keyVisible: true, hudVisible: true,
    // Outlines and tracks of the other surveys; devData is the diagnostics overlay.
    alwaysShowPreviews: false, devData: false,

    // Feet: these are US lake surveys, and the depth finder, the paper
    // charts and the boat's own readout are all imperial.
    depthUnit: 'ft', scopeRatio: 5, boatLengthM: 10,
    alarmEscalate: true, startCenterOnGps: false, keepAwake: true,
    contourIntervalFt: 1,
    lastLocationId: null,

    // Armed anchor watch: "none" | "circle" | "polygon".
    watchMode: 'none', anchorLat: 0, anchorLon: 0, anchorRadiusM: 0, anchorTimeMs: 0,
    watchRing: [],
    waypoints: [],
  };

  let state;
  try {
    state = { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || '{}') };
  } catch (e) {
    state = { ...DEFAULTS };
  }

  const save = () => {
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* private mode */ }
  };

  // Reading and writing go through a proxy so call sites read like the Kotlin.
  const prefs = new Proxy(state, {
    get: (target, name) => (name === 'clearWatch' ? clearWatch : target[name]),
    set: (target, name, value) => { target[name] = value; save(); return true; },
  });

  function clearWatch() {
    state.watchMode = 'none';
    state.anchorTimeMs = 0;
    state.watchRing = [];
    save();
  }

  return prefs;
})();
