/** Depth/distance/speed formatting in the user's chosen units. */
const Units = {
  /** Depth in metres → "X.X m" / "X.X ft" / "X.X fa". */
  depth(meters, unit) {
    if (unit === 'ft') return `${(meters * 3.28084).toFixed(1)} ft`;
    if (unit === 'fa') return `${(meters / 1.8288).toFixed(1)} fa`;
    return `${meters.toFixed(1)} m`;
  },

  /** Horizontal distance in metres → "X m" / "X ft". */
  distance(meters, unit) {
    if (unit === 'ft' || unit === 'fa') return `${Math.round(meters * 3.28084)} ft`;
    return `${Math.round(meters)} m`;
  },

  /** Speed in m/s → knots. */
  speedKnots(ms) {
    return `${(ms * 1.94384).toFixed(1)} kn`;
  },

  /** Great-circle distance in metres between two WGS84 points. */
  haversine(lat1, lon1, lat2, lon2) {
    const R = 6371000;
    const toRad = Math.PI / 180;
    const dLat = (lat2 - lat1) * toRad;
    const dLon = (lon2 - lon1) * toRad;
    const a = Math.sin(dLat / 2) ** 2 +
      Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
  },

  /** "MMM d HH:mm" in the browser's local time. */
  stamp(millis) {
    return new Date(millis).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
    });
  },
};
