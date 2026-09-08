/**
 * The chart itself: base style, the four raster overlays, contours, shallow
 * bands, fence, boat, waypoints and survey pins - the whole layer stack, in
 * draw order, expressed in MapLibre GL JS.
 */
const ChartMap = (() => {
  const SATELLITE = 'https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}';
  const BASEMAP = 'https://tiles.openfreemap.org/planet';
  const GLYPHS = 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf';
  const FONT = ['Noto Sans Regular'];

  /** Survey lines closer together than this read as a blur, so hide them. */
  const TRACK_MIN_ZOOM = 12;

  /** Above this the pins just sit on top of the chart you are working. */
  const PIN_MAX_ZOOM = 15;
  const EMPTY = { type: 'FeatureCollection', features: [] };
  const RASTERS = ['bathymetry', 'sonar', 'substrate', 'rock'];

  let map = null;
  let current = null;
  let scaleControl = null;          // the SurveyLocation-shaped catalog entry on screen

  /**
   * Base style. Below zoom 13 there is no satellite imagery, so a light OSM
   * vector basemap (OpenFreeMap, no API key) gives you something to navigate by,
   * and hands over to the imagery the moment it starts rendering.
   */
  function baseStyle() {
    return {
      version: 8,
      glyphs: GLYPHS,
      sources: {
        'satellite-source': {
          type: 'raster', tiles: [SATELLITE], tileSize: 256, minzoom: 0, maxzoom: 18,
          attribution: 'Imagery &copy; Esri',
        },
        'basemap-source': { type: 'vector', url: BASEMAP },
      },
      layers: [
        { id: 'bg', type: 'background', paint: { 'background-color': '#000000' } },
        {
          id: 'basemap-water', type: 'fill', source: 'basemap-source', 'source-layer': 'water',
          maxzoom: 13, paint: { 'fill-color': '#0A3D62' },
        },
        {
          id: 'basemap-roads', type: 'line', source: 'basemap-source',
          'source-layer': 'transportation', minzoom: 6, maxzoom: 13,
          filter: ['in', ['get', 'class'],
            ['literal', ['motorway', 'trunk', 'primary', 'secondary', 'tertiary']]],
          paint: {
            'line-color': '#B0B0B0',
            'line-width': ['interpolate', ['linear'], ['zoom'], 6, 0.5, 10, 1.2, 13, 2.0],
          },
        },
        {
          id: 'basemap-place-labels', type: 'symbol', source: 'basemap-source',
          'source-layer': 'place', maxzoom: 13,
          filter: ['in', ['get', 'class'], ['literal', ['city', 'town', 'village']]],
          layout: {
            'text-field': ['get', 'name'], 'text-font': FONT,
            'text-size': ['match', ['get', 'class'], 'city', 15, 'town', 12, 10],
          },
          paint: { 'text-color': '#FFFFFF', 'text-halo-color': '#000000', 'text-halo-width': 1.5 },
        },
        { id: 'satellite-layer', type: 'raster', source: 'satellite-source', minzoom: 13 },
      ],
    };
  }

  function geojsonSource(id) {
    if (!map.getSource(id)) map.addSource(id, { type: 'geojson', data: EMPTY });
  }

  function setData(id, data) {
    const src = map.getSource(id);
    if (src) src.setData(data || EMPTY);
  }

  /** Add every overlay source + layer for one location, in draw order. */
  function addOverlays(location) {
    const layers = location.layers || {};
    for (const name of RASTERS) {
      const info = layers[name];
      if (!info) continue;
      map.addSource(`${name}-source`, {
        type: 'raster',
        tiles: [ChartUrl.tiles(location.id, name)],
        tileSize: 256,
        minzoom: info.minzoom,
        maxzoom: info.maxzoom,
      });
    }
    ['locations-source', 'contours-source', 'shallow-source', 'geofence-source',
      'geofence-verts', 'gps-dot-source', 'gps-track-source', 'waypoints-source',
      'preview-area-source', 'preview-track-source', 'detections-source']
      .forEach(geojsonSource);

    const add = (layer) => {
      if (layer.source.endsWith('-source') && !map.getSource(layer.source)) return;
      map.addLayer(layer);
    };

    add({
      id: 'bathymetry-layer', type: 'raster', source: 'bathymetry-source',
      paint: { 'raster-opacity': 0.8 },
    });
    add({
      id: 'sonar-layer', type: 'raster', source: 'sonar-source',
      paint: { 'raster-opacity': 1 },
    });
    add({
      id: 'substrate-layer', type: 'raster', source: 'substrate-source',
      paint: { 'raster-opacity': 0.8 },
    });
    // RockMapper habitat sits above substrate: it is the more specific read.
    add({
      id: 'rock-layer', type: 'raster', source: 'rock-source',
      paint: { 'raster-opacity': 0.8 },
    });

    // Shallow-water warning — the filter is set live from the min-depth slider.
    map.addLayer({
      id: 'shallow-layer', type: 'fill', source: 'shallow-source',
      paint: { 'fill-color': '#FF3B30', 'fill-opacity': 0.45 },
      filter: ['<', ['get', 'd'], -1],       // nothing shown until switched on
    });
    map.addLayer({
      id: 'contours-layer', type: 'line', source: 'contours-source',
      paint: { 'line-color': '#000000', 'line-width': 1.5, 'line-opacity': 0.9 },
    });
    // Objects the detector found on the bottom. Hollow rings rather than
    // filled dots: what is underneath them is the evidence, and a solid
    // marker would hide the thing it is pointing at.
    map.addLayer({
      id: 'detections-layer', type: 'circle', source: 'detections-source',
      paint: {
        'circle-radius': 7,
        'circle-color': 'rgba(0, 0, 0, 0)',
        'circle-stroke-color': '#ff4fd8',
        'circle-stroke-width': 2,
      },
    });
    map.addLayer({
      id: 'contour-labels', type: 'symbol', source: 'contours-source',
      layout: {
        'symbol-placement': 'line', 'text-field': '', 'text-font': FONT,
        'text-size': 11, 'symbol-spacing': 220,
      },
      paint: { 'text-color': '#FFFFFF', 'text-halo-color': '#000000', 'text-halo-width': 2 },
    });

    // Anchor-watch fence, under the boat. Recoloured on breach.
    map.addLayer({
      id: 'geofence-fill', type: 'fill', source: 'geofence-source',
      paint: { 'fill-color': '#00C853', 'fill-opacity': 0.18 },
    });
    map.addLayer({
      id: 'geofence-line', type: 'line', source: 'geofence-source',
      paint: { 'line-color': '#00C853', 'line-width': 2.5 },
    });
    map.addLayer({
      id: 'geofence-verts', type: 'circle', source: 'geofence-verts',
      paint: {
        'circle-color': '#FFFFFF', 'circle-radius': 5,
        'circle-stroke-color': '#00C853', 'circle-stroke-width': 2,
      },
    });
    map.addLayer({
      id: 'gps-track-layer', type: 'line', source: 'gps-track-source',
      paint: { 'line-color': '#00BFFF', 'line-width': 3 },
    });
    map.addLayer({
      id: 'gps-dot-layer', type: 'circle', source: 'gps-dot-source',
      paint: {
        'circle-color': '#00BFFF', 'circle-radius': 8,
        'circle-stroke-color': '#FFFFFF', 'circle-stroke-width': 2,
      },
    });
    map.addLayer({
      id: 'waypoints-layer', type: 'circle', source: 'waypoints-source',
      paint: {
        'circle-color': '#FF2D95', 'circle-radius': 6,
        'circle-stroke-color': '#FFFFFF', 'circle-stroke-width': 2,
      },
    });
    map.addLayer({
      id: 'waypoint-labels', type: 'symbol', source: 'waypoints-source',
      layout: {
        'text-field': ['get', 'label'], 'text-font': FONT,
        'text-size': 12, 'text-offset': [0, 1.2],
      },
      paint: { 'text-color': '#FFFFFF', 'text-halo-color': '#000000', 'text-halo-width': 1.8 },
    });
    // What the other surveys cover: outline plus the track the boat ran. Kept
    // under the pins, and dashed, so it reads as "somewhere else's data".
    map.addLayer({
      id: 'preview-area-fill', type: 'fill', source: 'preview-area-source',
      paint: { 'fill-color': '#9E9E9E', 'fill-opacity': 0.18 },
    });
    map.addLayer({
      id: 'preview-area-line', type: 'line', source: 'preview-area-source',
      paint: {
        'line-color': '#CFD8DC', 'line-width': 1.5, 'line-opacity': 0.9,
        'line-dasharray': [3, 2],
      },
    });
    map.addLayer({
      id: 'preview-track-line', type: 'line', source: 'preview-track-source',
      // Zoomed out, a lawnmower survey's lines fall closer together than a
      // pixel and smear into a solid yellow field that says nothing except
      // "there was a survey here" - which the coverage outline already says,
      // and says honestly. Below this zoom the track is hidden.
      minzoom: TRACK_MIN_ZOOM,
      paint: { 'line-color': '#FFC400', 'line-width': 1.2, 'line-opacity': 0.75 },
    });

    // Survey pins above the overlays but below nothing that matters — other
    // charts stay visible so you can see what else is surveyed and jump to it.
    map.addLayer({
      id: 'location-pins', type: 'circle', source: 'locations-source',
      maxzoom: PIN_MAX_ZOOM,
      paint: {
        'circle-color': ['get', 'color'], 'circle-radius': 7,
        'circle-stroke-color': ['get', 'stroke'], 'circle-stroke-width': 2,
      },
    });
    map.addLayer({
      id: 'location-labels', type: 'symbol', source: 'locations-source',
      maxzoom: PIN_MAX_ZOOM,
      layout: {
        'text-field': ['get', 'label'], 'text-font': FONT,
        'text-size': 12, 'text-offset': [0, -1.4],
      },
      paint: { 'text-color': '#FFFFFF', 'text-halo-color': '#000000', 'text-halo-width': 2 },
    });
  }

  function removeOverlays() {
    if (!current) return;
    const layerIds = [
      'bathymetry-layer', 'sonar-layer', 'substrate-layer', 'rock-layer',
      'shallow-layer', 'contours-layer', 'contour-labels', 'detections-layer',
      'geofence-fill', 'geofence-line', 'geofence-verts',
      'gps-track-layer', 'gps-dot-layer', 'waypoints-layer', 'waypoint-labels',
      'preview-area-fill', 'preview-area-line', 'preview-track-line',
      'location-pins', 'location-labels',
    ];
    layerIds.forEach(id => { if (map.getLayer(id)) map.removeLayer(id); });
    [...RASTERS.map(n => `${n}-source`), 'locations-source', 'contours-source',
      'shallow-source', 'detections-source', 'geofence-source',
      'geofence-verts', 'gps-dot-source',
      'gps-track-source', 'waypoints-source',
      'preview-area-source', 'preview-track-source']
      .forEach(id => { if (map.getSource(id)) map.removeSource(id); });
  }

  return {
    get map() { return map; },
    get location() { return current; },
    PIN_MAX_ZOOM,

    /** Create the map and resolve once the base style is live. */
    init(container, centre) {
      map = new maplibregl.Map({
        container,
        style: baseStyle(),
        center: [centre.lon, centre.lat],
        zoom: centre.zoom || 16,
        attributionControl: { compact: true },
        // A boat is not a flight simulator: keep the chart north-up and flat.
        pitchWithRotate: false,
        dragRotate: false,
      });
      map.touchZoomRotate.disableRotation();
      // Resolve on style.load, not load: 'load' also waits for the satellite and
      // basemap tiles, which are the first thing to go when the boat is out of
      // signal. The chart itself is local and must come up regardless.
      return new Promise(resolve => {
        const done = () => resolve(map);
        if (map.isStyleLoaded()) done();
        else map.once('style.load', done);
        setTimeout(done, 4000);          // last-resort guard, harmless if late
      });
    },

    /** Swap in another survey's layers. */
    setLocation(location) {
      removeOverlays();
      current = location;
      addOverlays(location);
    },

    hasLayer(name) {
      return Boolean(current && current.layers && current.layers[name]);
    },

    setVisible(layerId, on) {
      if (map.getLayer(layerId)) {
        map.setLayoutProperty(layerId, 'visibility', on ? 'visible' : 'none');
      }
    },

    setOpacity(layerId, fraction) {
      if (map.getLayer(layerId)) map.setPaintProperty(layerId, 'raster-opacity', fraction);
    },

    /**
     * Brightness, contrast and edge handling for a raster overlay. Fractions
     * in -1..1; `sharp` picks the resampling.
     *
     * MapLibre has no single brightness knob. raster-brightness-min/max are the
     * black and white points the tile is remapped onto, so lifting the black
     * point brightens the image and pulling the white point down darkens it -
     * which is why one slider drives two properties here.
     *
     * Resampling is the honest "sharpness": nearest keeps each mosaic cell's
     * own value and its hard edge, linear blends it into its neighbours. Zoomed
     * in past the survey's own resolution that is the whole difference between
     * reading cells and reading a smear. There is no unsharp mask - a raster
     * layer cannot run one without a custom WebGL pass.
     */
    setImageAdjust(layerId, { brightness = 0, contrast = 0, sharp = false } = {}) {
      if (!map.getLayer(layerId)) return;
      const clamp = v => Math.max(-1, Math.min(1, v || 0));
      const b = clamp(brightness);
      map.setPaintProperty(layerId, 'raster-brightness-min', b > 0 ? b : 0);
      map.setPaintProperty(layerId, 'raster-brightness-max', b < 0 ? 1 + b : 1);
      map.setPaintProperty(layerId, 'raster-contrast', clamp(contrast));
      map.setPaintProperty(layerId, 'raster-resampling', sharp ? 'nearest' : 'linear');
    },

    /**
     * Charts carry a contour every foot from 0 ft; the settings slider keeps
     * only multiples of the chosen spacing, and the label shows the depth in the
     * chosen unit at the chosen time (charted depth + tide).
     */
    applyContours(intervalFt, tideOffset, unit) {
      if (!map.getLayer('contours-layer')) return;
      const filter = intervalFt <= 1
        ? ['>=', ['get', 'ft'], 0]
        : ['==', ['%', ['get', 'ft'], intervalFt], 0];
      map.setFilter('contours-layer', filter);
      map.setFilter('contour-labels', filter);

      const factor = unit === 'ft' ? 3.28084 : unit === 'fa' ? 1 / 1.8288 : 1;
      const suffix = unit === 'ft' ? ' ft' : unit === 'fa' ? ' fa' : ' m';
      map.setLayoutProperty('contour-labels', 'text-field', [
        'concat',
        ['to-string', ['/',
          ['round', ['*', ['*', ['+', ['get', 'depth'], tideOffset], factor], 10]], 10]],
        suffix,
      ]);
    },

    /**
     * Highlight everything shallower than `meters` right now. Bands are stored
     * at chart datum, so the test is  d < threshold − tide.
     */
    applyShallow(meters, tideOffset) {
      if (!map.getLayer('shallow-layer')) return;
      if (!meters) {
        map.setLayoutProperty('shallow-layer', 'visibility', 'none');
        return;
      }
      map.setLayoutProperty('shallow-layer', 'visibility', 'visible');
      map.setFilter('shallow-layer', ['<', ['get', 'd'], meters - tideOffset]);
    },

    setPreviewAreas(fc) { setData('preview-area-source', fc); },
    setPreviewTracks(fc) { setData('preview-track-source', fc); },

    /**
     * MapLibre's own scale control, in the units the depth setting uses.
     * Recreated on a unit change: the control takes its unit at construction.
     */
    setScaleUnit(unit) {
      if (scaleControl) map.removeControl(scaleControl);
      scaleControl = new maplibregl.ScaleControl({
        maxWidth: 120,
        unit: unit === 'ft' || unit === 'fa' ? 'imperial' : 'metric',
      });
      map.addControl(scaleControl, 'bottom-right');
    },

    /** Ground distance one screen pixel covers here - what the dev overlay wants. */
    metresPerPixel() {
      const latitude = map.getCenter().lat;
      return 156543.03392 * Math.cos((latitude * Math.PI) / 180) /
        Math.pow(2, map.getZoom()) / (window.devicePixelRatio || 1);
    },

    setContourData(fc) { setData('contours-source', fc); },
    setShallowData(fc) { setData('shallow-source', fc); },
    setDetections(fc) { setData('detections-source', fc); },
    setPins(fc) { setData('locations-source', fc); },
    setWaypoints(fc) { setData('waypoints-source', fc); },

    setGpsDot(lat, lon) {
      setData('gps-dot-source', {
        type: 'FeatureCollection',
        features: [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [lon, lat] } }],
      });
    },

    setTrack(points) {
      setData('gps-track-source', points.length < 2 ? EMPTY : {
        type: 'FeatureCollection',
        features: [{
          type: 'Feature', properties: {},
          geometry: { type: 'LineString', coordinates: points },
        }],
      });
    },

    /** Colour the boat by depth under it (red shallow → cyan deep / unknown). */
    tintBoat(depth) {
      const hex = depth === null || depth === undefined ? '#00BFFF'
        : depth < 2 ? '#FF3B30'
          : depth < 4 ? '#FF9500'
            : depth < 8 ? '#FFD60A' : '#00BFFF';
      if (map.getLayer('gps-dot-layer')) map.setPaintProperty('gps-dot-layer', 'circle-color', hex);
    },

    setFenceColor(hex) {
      if (map.getLayer('geofence-fill')) map.setPaintProperty('geofence-fill', 'fill-color', hex);
      if (map.getLayer('geofence-line')) map.setPaintProperty('geofence-line', 'line-color', hex);
    },

    /** Draw the fence: open line while drawing, closed polygon once armed. */
    setFence(points, { closed = false, showVerts = false } = {}) {
      setData('geofence-verts', showVerts && points.length ? {
        type: 'FeatureCollection',
        features: [{
          type: 'Feature', properties: {},
          geometry: { type: 'MultiPoint', coordinates: points },
        }],
      } : EMPTY);

      if (points.length < 2) { setData('geofence-source', EMPTY); return; }
      const geometry = closed && points.length >= 3
        ? { type: 'Polygon', coordinates: [[...points, points[0]]] }
        : { type: 'LineString', coordinates: points };
      setData('geofence-source', {
        type: 'FeatureCollection',
        features: [{ type: 'Feature', properties: {}, geometry }],
      });
    },

    /** The swing circle as a 64-point polygon. */
    setCircle(lat, lon, radiusM) {
      const dLat = radiusM / 111320;
      const dLon = radiusM / (111320 * Math.cos((lat * Math.PI) / 180));
      const ring = [];
      for (let i = 0; i <= 64; i++) {
        const a = (2 * Math.PI * i) / 64;
        ring.push([lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]);
      }
      setData('geofence-source', {
        type: 'FeatureCollection',
        features: [{ type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [ring] } }],
      });
      setData('geofence-verts', EMPTY);
    },

    clearFenceRender() {
      setData('geofence-source', EMPTY);
      setData('geofence-verts', EMPTY);
    },

    flyTo(lat, lon, zoom) {
      map.flyTo({ center: [lon, lat], zoom: zoom || map.getZoom(), essential: true });
    },

    /** The survey pin under a tap, or null. */
    pinAt(point) {
      const box = [
        [point.x - 14, point.y - 14],
        [point.x + 14, point.y + 14],
      ];
      const hits = map.queryRenderedFeatures(box, { layers: ['location-pins', 'location-labels'] })
        .filter(f => f.properties && f.properties.id);
      return hits.length ? hits[0].properties.id : null;
    },

    /** The detection under a tap, or null. Hidden pots are not hit. */
    detectionAt(point) {
      if (!map.getLayer('detections-layer')) return null;
      const box = [
        [point.x - 14, point.y - 14],
        [point.x + 14, point.y + 14],
      ];
      const hits = map.queryRenderedFeatures(box, { layers: ['detections-layer'] });
      if (!hits.length) return null;
      const hit = hits[0];
      return {
        properties: hit.properties || {},
        lon: hit.geometry.coordinates[0],
        lat: hit.geometry.coordinates[1],
      };
    },
  };
})();
