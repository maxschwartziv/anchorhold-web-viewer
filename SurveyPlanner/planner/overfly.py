"""
Points the survey has to pass directly over.

Side scan looks sideways and leaves a blind strip under the boat; down imaging
looks straight down and sees only that strip. So the two answer different
questions, and anything you actually want a downward look at - a snag, a wreck,
a thing GhostVision found and you want to see properly - has to be driven over,
not past.

The grid does not do that on its own. Lines are laid for coverage at a spacing,
and whether one happens to cross a particular object is chance. So a marked
point gets its own line, parallel to the rest of the survey and clipped to the
same water, placed to run through it.

A line rather than a nudge of the nearest one, deliberately. Nudging a grid
line moves the spacing either side of it and the effect spreads across the
block; an added line is local, visible in the plan, and costs exactly what it
looks like it costs.

Coordinates here are local feet, as everywhere else in the planner.
"""

from __future__ import annotations

import math

# A down-imaging transducer's nominal cone, which is what decides how wide a
# strip it actually sees. PINGMapper reads the same 45 degrees for the Down
# Imaging channel (see down_sonar.py in the pipeline).
DOWN_CONE_DEG = 45.0

# What counts as "over" when nothing better is known. The footprint is
# 2 * depth * tan(cone/2), so 8 ft of half-width is the beam at about 19 ft of
# water: conservative in deeper water, honest in the shallows where the strip
# genuinely is that narrow.
DEFAULT_TOLERANCE_FT = 8.0

# How far either side of the mark each pass runs. The point is not the length
# of line but the state of the boat over the mark: it has to be straight and
# settled before it gets there, and the sonar needs sea floor either side of
# the object to read it against. 150 ft is about 30 seconds at survey speed.
DEFAULT_RUN_FT = 150.0


def beam_half_width_ft(depth_ft: float, cone_deg: float = DOWN_CONE_DEG) -> float:
    """
    Half the ground strip a down beam sees at this depth.

    The width is the reason a marked point needs its own line at all: in 10 ft
    of water the strip is about 8 ft across, so passing 20 ft away misses it
    entirely however good the sonar is.
    """
    return max(0.0, float(depth_ft)) * math.tan(math.radians(cone_deg / 2.0))


def covered(point, lines, tolerance_ft: float) -> bool:
    """Whether some line already runs near enough to this point."""
    from shapely.geometry import Point

    p = Point(point[0], point[1])
    return any(line["geom"].distance(p) <= tolerance_ft for line in lines)


def line_through(point, bearing_deg: float, region, min_line_ft: float = 0.0):
    """
    One survey line through `point`, on `bearing_deg`, clipped to the water.

    Returns the clipped piece that actually contains the point, or None if the
    point is not in navigable water - which is the useful answer rather than an
    error: a detection on the bank, or inside the shore setback, cannot be
    driven over and the plan should say so rather than quietly bend.
    """
    from shapely.geometry import LineString, MultiPolygon, Point

    theta = math.radians(bearing_deg)
    along = (math.sin(theta), math.cos(theta))
    parts = region.geoms if isinstance(region, MultiPolygon) else [region]
    here = Point(point[0], point[1])

    # Long enough to cross any part of the water from anywhere in it.
    reach = max(p.bounds[2] - p.bounds[0] for p in parts) + \
            max(p.bounds[3] - p.bounds[1] for p in parts)
    sweep = LineString([
        (point[0] - along[0] * reach, point[1] - along[1] * reach),
        (point[0] + along[0] * reach, point[1] + along[1] * reach),
    ])

    best = None
    for part in parts:
        clipped = sweep.intersection(part)
        pieces = (list(clipped.geoms) if clipped.geom_type == "MultiLineString"
                  else ([clipped] if not clipped.is_empty else []))
        for piece in pieces:
            if piece.length < min_line_ft:
                continue
            # The piece that holds the point, not merely the longest: a sweep
            # across a lake with arms comes back in several bits and only one
            # of them is the water the point sits in.
            if piece.distance(here) <= 1.0:
                if best is None or piece.length > best.length:
                    best = piece
    return best


def crossing_lines(points, bearing_deg: float, region,
                   run_ft: float = DEFAULT_RUN_FT, log=None):
    """
    Two passes over every marked point, at right angles to each other.

    Twice, and crossed, because one pass is one look. Down imaging sees a
    strip a few feet wide and side scan throws its shadow one way; crossing
    the same object on the survey bearing and again across it gives two
    aspects and two shadow directions, which is the difference between
    knowing something is there and knowing what it is.

    Each pass runs `run_ft` either side of the mark so the boat is straight
    and settled over it, and clipped to navigable water - a pass that would
    run onto the bank is shortened, not moved, because the mark is the point
    and the mark must stay under the track.

    Returns (lines, report). Report is one entry per point:
    {point, status, lines} with status

        'crossed'     two passes laid
        'one pass'    only one direction fitted in the water available
        'unreachable' the mark is not in navigable water at all
    """
    from shapely.geometry import Point

    out, report = [], []
    for point in points:
        here = Point(point[0], point[1])
        if not region.contains(here):
            report.append({"point": point, "status": "unreachable", "lines": []})
            if log:
                log("      a mark at %.0f, %.0f ft is not in navigable water"
                    % (point[0], point[1]))
            continue
        made = []
        for heading in (bearing_deg, bearing_deg + 90.0):
            geom = _run_through(point, heading, region, run_ft)
            if geom is not None:
                made.append({"kind": "overfly", "bearing": heading % 180.0,
                             "geom": geom})
        out += made
        report.append({"point": point,
                       "status": "crossed" if len(made) == 2 else
                                 ("one pass" if made else "unreachable"),
                       "lines": made})
    return out, report


def _run_through(point, bearing_deg: float, region, run_ft: float):
    """
    One straight pass over the point, `run_ft` either side, clipped to water.

    The clipped piece has to still contain the mark. A sweep across a lake
    with arms comes back in several bits and only the one holding the mark is
    the pass that drives over it; the others are somewhere else entirely.
    """
    from shapely.geometry import LineString, MultiPolygon, Point

    theta = math.radians(bearing_deg)
    along = (math.sin(theta), math.cos(theta))
    here = Point(point[0], point[1])
    sweep = LineString([
        (point[0] - along[0] * run_ft, point[1] - along[1] * run_ft),
        (point[0] + along[0] * run_ft, point[1] + along[1] * run_ft),
    ])
    parts = region.geoms if isinstance(region, MultiPolygon) else [region]
    best = None
    for part in parts:
        clipped = sweep.intersection(part)
        pieces = (list(clipped.geoms) if clipped.geom_type == "MultiLineString"
                  else ([clipped] if not clipped.is_empty else []))
        for piece in pieces:
            if piece.length < 1.0 or piece.distance(here) > 1.0:
                continue
            if best is None or piece.length > best.length:
                best = piece
    return best


def summarise(report) -> str:
    """One line about what the marks cost, for the plan summary."""
    crossed = sum(1 for r in report if r["status"] == "crossed")
    single = sum(1 for r in report if r["status"] == "one pass")
    lost = sum(1 for r in report if r["status"] == "unreachable")
    extra_ft = sum(line["geom"].length for r in report for line in r["lines"])
    bits = []
    if crossed:
        bits.append(f"{crossed} crossed twice")
    if single:
        bits.append(f"{single} with room for one pass only")
    if lost:
        bits.append(f"{lost} not in navigable water")
    if extra_ft:
        bits.append(f"{extra_ft / 5280.0:.2f} miles of run-in")
    return "; ".join(bits) if bits else "none"


# ── Detections ───────────────────────────────────────────────────────────────

def read_points(path: str) -> list:
    """
    A list of places, from whichever of the two files it arrived in.

    Two shapes reach this, and neither is worth making anyone convert:

      GeoJSON FeatureCollection of points - what GhostVision writes, what the
      chart server keeps beside a chart, and what places_to_objects.py builds
      from a CSV. Properties are read if present and nothing is required.

      {"waypoints": [[lat, lon, label], ...]} - the viewer's own waypoints,
      dropped by hand on the chart and mirrored to output/waypoints.json for
      the pipeline to read. Note the order: latitude first, which is the
      opposite of GeoJSON and the reason this reads them rather than hoping.

    Returns [{lon, lat, name, cls, confidence}] either way, so everything
    downstream stops caring which file it came from. confidence is None when
    the file does not say, or does not say a number.
    """
    import json

    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError("That file is not a list of places.")

    if payload.get("type") != "FeatureCollection":
        if isinstance(payload.get("waypoints"), list):
            return _read_waypoints(payload["waypoints"])
        raise ValueError("That is neither a GeoJSON FeatureCollection nor a "
                         "waypoints file.")

    out = []
    for feature in payload.get("features") or []:
        geometry = (feature or {}).get("geometry") or {}
        if geometry.get("type") != "Point":
            continue
        coords = geometry.get("coordinates") or []
        if len(coords) < 2:
            continue
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            continue
        props = feature.get("properties") or {}
        try:
            confidence = float(props.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        out.append({
            "lon": lon, "lat": lat,
            "name": str(props.get("tracker_id", "") or "")[:40],
            "cls": str(props.get("class_name", "Object") or "Object")[:60],
            "confidence": confidence,
        })
    if not out:
        raise ValueError("No point features in that file.")
    return out


def _read_waypoints(rows) -> list:
    """The viewer's waypoints: [lat, lon, label] triples, latitude first."""
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            lat, lon = float(row[0]), float(row[1])
        except (TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        label = str(row[2]).strip() if len(row) > 2 and row[2] is not None else ""
        out.append({"lon": lon, "lat": lat, "name": label[:40],
                    # One class, so the chooser still works the same way and a
                    # mixed import can tell waypoints from detections.
                    "cls": "Waypoint", "confidence": None})
    if not out:
        raise ValueError("No usable waypoints in that file.")
    return out


# The name this had when it only read detections.
read_detections = read_points


def classes_in(detections) -> list:
    """The distinct class names present, most common first."""
    counts = {}
    for d in detections:
        counts[d["cls"]] = counts.get(d["cls"], 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def select(detections, classes=None, min_confidence: float = 0.0) -> list:
    """
    The subset worth driving over.

    Both filters are opt-in because a detector's output is not a list of
    things to visit: it is a list of maybes, and which maybes are worth a
    line is a judgement about this survey. A confidence floor of 0 keeps
    everything, including the detections that carry no confidence at all -
    dropping those silently would hide every hand-made file.
    """
    keep = []
    for d in detections:
        if classes is not None and d["cls"] not in classes:
            continue
        if min_confidence > 0.0 and (d["confidence"] is None
                                     or d["confidence"] < min_confidence):
            continue
        keep.append(d)
    return keep
