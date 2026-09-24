"""
What the boat needs besides the mission: an ArduPilot fence, and the chart.

**The fence.** One inclusion polygon - the water - and one exclusion polygon for
every island and no-go area. ArduPilot Rover keeps fence points in a fixed
corner of its parameter storage: on a Pixhawk1 or any other board with 16 KB
of storage that is 672 bytes, about 84 points across every polygon together
(a 4-byte header, 2 bytes plus 8 per point for each polygon, 1 byte to end).
An NHD outline has hundreds of points, so the polygons are simplified until
they fit, and always in the safe direction: the water shrinks, the no-go areas
grow. Fitting the budget can cost the boat water it could have used; it can
never hand it water it should not have.

**The chart.** The depth grid, masked to where it was actually sounded, in the
same format AnchorHold writes. shoal_guard.lua reads it from the SD card a few
cells at a time and looks ahead along the route - see planner/depthgrid.py.
"""

from __future__ import annotations

import json
import os

FENCE_BYTES_PIXHAWK1 = 672
MAV_CMD_NAV_FENCE_POLYGON_VERTEX_INCLUSION = 5001
MAV_CMD_NAV_FENCE_POLYGON_VERTEX_EXCLUSION = 5002


def fence_bytes(polygons) -> int:
    return 4 + sum(2 + 8 * (len(p.exterior.coords) - 1) for p in polygons) + 1


def _parts(geom):
    if geom.is_empty:
        return []
    return list(geom.geoms) if geom.geom_type in ("MultiPolygon", "GeometryCollection") else [geom]


def build_fence(water, no_go, launches=(), launch_radius_ft: float = 40.0,
                budget_bytes: int = FENCE_BYTES_PIXHAWK1, area=None):
    """
    (inclusion, exclusions, tolerance_ft) sized to fit `budget_bytes`.

    `water` is the lake polygon in local feet; its islands become exclusions,
    because an ArduPilot inclusion polygon cannot have holes. `launches` are
    access points in local feet: a boat is armed at the bank, and Rover will
    not arm outside an inclusion fence, so each launch keeps a circle of
    `launch_radius_ft` inside the fence however hard the outline is simplified,
    and clear of every exclusion - launches sit in the shallows more often than
    not, and a boat inside an exclusion will not arm either.

    `area` limits the fence to the part of the lake a plan actually uses. A
    306-acre NHD outline squeezed into 84 points loses the better part of a
    hundred feet all round - more than the shore setback - so fencing only the
    surveyed water keeps the simplification smaller than the setback.
    """
    from shapely.geometry import Point, Polygon
    from shapely.ops import unary_union

    outer = Polygon(water.exterior)
    if area is not None:
        outer = Polygon(max(_parts(outer.intersection(area)), key=lambda p: p.area).exterior)
    holes = [Polygon(r) for r in water.interiors]
    zones = [z["geom"] if isinstance(z, dict) else z for z in no_go]
    keep = [Point(p).buffer(launch_radius_ft) for p in launches]
    tol = 1.0
    while True:
        inc = outer.buffer(-tol, join_style=2).simplify(tol, preserve_topology=True)
        if keep:
            inc = unary_union([inc] + [k.intersection(outer).simplify(tol) for k in keep])
            inc = inc.simplify(tol, preserve_topology=True)
        inc = max(_parts(inc), key=lambda p: p.area)
        inc = Polygon(inc.exterior)
        exc = []
        for z in holes + zones:
            e = z.buffer(tol, join_style=2)
            if keep:
                # a launch is usually shallow, and Rover will not arm inside an
                # exclusion either: the launch circle stays clear of no-go too
                e = e.difference(unary_union(keep))
            exc += _parts(e.simplify(tol, preserve_topology=True))
        exc = [e for e in exc if not e.is_empty and e.intersects(inc)]
        if fence_bytes([inc] + exc) <= budget_bytes or tol > 5000:
            return inc, exc, tol
        tol *= 1.3


def write_fence_files(folder, frame, inclusion, exclusions, prefix: str = "") -> list:
    """fence.waypoints (Mission Planner), fence.plan (QGroundControl), fence.geojson."""
    os.makedirs(folder, exist_ok=True)
    polys = [(True, inclusion)] + [(False, e) for e in exclusions]

    rows, i = ["QGC WPL 110"], 0
    for inclusion_ring, poly in polys:
        ring = list(poly.exterior.coords)[:-1]
        cmd = (MAV_CMD_NAV_FENCE_POLYGON_VERTEX_INCLUSION if inclusion_ring
               else MAV_CMD_NAV_FENCE_POLYGON_VERTEX_EXCLUSION)
        for x, y in ring:
            lon, lat = frame.to_lonlat(x, y)
            rows.append("%d\t0\t0\t%d\t%d\t0\t0\t0\t%.8f\t%.8f\t0\t1" % (i, cmd, len(ring), lat, lon))
            i += 1
    wpl = os.path.join(folder, prefix + "fence.waypoints")
    with open(wpl, "w") as f:
        f.write("\n".join(rows) + "\n")

    plan = os.path.join(folder, prefix + "fence.plan")
    with open(plan, "w") as f:
        json.dump({"fileType": "Plan", "groundStation": "QGroundControl", "version": 1,
                   "geoFence": qgc_geofence(frame, inclusion, exclusions),
                   "mission": {"items": [], "version": 2, "firmwareType": 3, "vehicleType": 11,
                               "plannedHomePosition": list(reversed(frame.to_lonlat(
                                   *list(inclusion.exterior.coords)[0]))) + [0],
                               "cruiseSpeed": 1, "hoverSpeed": 1, "globalPlanAltitudeMode": 1},
                   "rallyPoints": {"points": [], "version": 2}}, f, indent=2)

    gj = os.path.join(folder, prefix + "fence.geojson")
    with open(gj, "w") as f:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"role": "inclusion" if inc else "exclusion"},
             "geometry": {"type": "Polygon",
                          "coordinates": [[list(frame.to_lonlat(x, y)) for x, y in p.exterior.coords]]}}
            for inc, p in polys]}, f)
    return [wpl, plan, gj]


def qgc_geofence(frame, inclusion, exclusions) -> dict:
    """The geoFence block of a QGroundControl .plan."""
    def ring(p):
        return [list(reversed(frame.to_lonlat(x, y))) for x, y in list(p.exterior.coords)[:-1]]
    return {"circles": [], "version": 2,
            "polygons": [{"inclusion": True, "polygon": ring(inclusion), "version": 1}] +
                        [{"inclusion": False, "polygon": ring(e), "version": 1} for e in exclusions]}


def write_parm(path, speed_mph: float, shallow_ft: float, with_chart: bool) -> str:
    """ArduPilot parameters for the fence, Dijkstra and shoal_guard's chart."""
    lines = [
        "# Survey Planner boat package - load in Mission Planner (Config > Full Parameter List > Load)",
        "# OA_TYPE needs a reboot. SHOAL_* exist once shoal_guard.lua is running.",
        "# No-go areas were drawn where the chart is shallower than %.1f ft (%.2f m);"
        % (shallow_ft, shallow_ft / 3.280839895),
        "# set SHOAL_DRAFT + SHOAL_MARGIN for your boat to about that.",
        "FENCE_ENABLE 1",
        "FENCE_TYPE 4",            # Rover: inclusion/exclusion polygons
        "FENCE_ACTION 6",          # Loiter or Hold
        "FENCE_MARGIN 1",
        "OA_TYPE 2",               # Dijkstra around the exclusions
        "OA_MARGIN_MAX 2",
        "WP_SPEED %.2f" % (speed_mph * 0.44704),
    ]
    if with_chart:
        lines += ["SHOAL_CHT_ENABLE 1", "SHOAL_CHT_NAN 1"]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def plan_area(tracks, margin_ft: float):
    """The water a plan uses: every track, grown by `margin_ft`."""
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    lines = [LineString(t) for t in tracks if len(t) > 1]
    return unary_union(lines).buffer(margin_ft) if lines else None


def write_package(folder, frame, water, no_go, grids=(), launches=(), speed_mph: float = 2.0,
                  shallow_ft: float = 3.0, budget_bytes: int = FENCE_BYTES_PIXHAWK1,
                  tracks=(), track_margin_ft: float = 60.0) -> dict:
    """
    Fence files, chart and parameters in one folder, plus a README for the SD card.

    `tracks` are the plan's mission tracks, one per day (local feet). With
    tracks, each day gets its own fence covering only that day's water: a
    306-acre outline in 84 points is simplified by ~70 ft, more than the shore
    setback, while one day's corner of it fits in a few feet. The day's fence
    is uploaded with the day's mission. Every track is checked against its
    fence, because a line the fence cuts trips FENCE_ACTION mid-survey.
    Without tracks there is one fence for the whole lake.
    """
    from planner import depthgrid
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    zones = [z["geom"] if isinstance(z, dict) else z for z in no_go]
    runs = [("day_%02d_" % (i + 1), [t]) for i, t in enumerate(tracks) if len(t) > 1] \
        if tracks else [("", [])]
    fences, files = [], []
    for prefix, day_tracks in runs:
        area = plan_area(day_tracks, track_margin_ft) if day_tracks else None
        inclusion, exclusions, tol = build_fence(water, no_go, launches,
                                                 budget_bytes=budget_bytes, area=area)
        files += write_fence_files(folder, frame, inclusion, exclusions, prefix)
        allowed = inclusion.difference(unary_union(exclusions)) if exclusions else inclusion
        usable = water if area is None else water.intersection(area)
        usable = usable.difference(unary_union(zones)) if zones else usable
        fences.append({
            "name": prefix.rstrip("_") or "lake",
            "vertices": sum(len(p.exterior.coords) - 1 for p in [inclusion] + exclusions),
            "bytes": fence_bytes([inclusion] + exclusions), "tolerance_ft": tol,
            "exclusions": len(exclusions),
            # usable water the simplification gave up to fit the storage
            "water_lost_pct": 100.0 * max(0.0, 1 - allowed.area / max(usable.area, 1.0)),
            "outside_ft": sum(LineString(t).difference(allowed).length
                              for t in day_tracks if len(t) > 1),
            "inclusion_geom": inclusion, "exclusion_geoms": exclusions, "allowed": allowed,
        })

    chart = None
    if grids:
        chart = depthgrid.write_boat_chart(os.path.join(folder, "chart"), grids)
        files.append(chart)
    files.append(write_parm(os.path.join(folder, "ardupilot.parm"), speed_mph, shallow_ft, bool(grids)))

    worst = max(fences, key=lambda f: f["tolerance_ft"])
    info = {
        "fences": fences, "files": files, "budget": budget_bytes,
        "vertices": max(f["vertices"] for f in fences),
        "bytes": max(f["bytes"] for f in fences),
        "tolerance_ft": worst["tolerance_ft"],
        "exclusions": max(f["exclusions"] for f in fences),
        "outside_ft": sum(f["outside_ft"] for f in fences),
        "fenced": ("one fence per day, around that day's water" if tracks
                   else "the whole lake"),
    }
    with open(os.path.join(folder, "README.txt"), "w") as f:
        f.write(
            "Boat package from Survey Planner\n\n"
            + ("day_NN_fence.*   one fence per day - upload it with that day's mission\n"
               if tracks else "")
            + "*fence.waypoints Mission Planner: Plan > Fence > Load (inclusion + exclusions)\n"
            "*fence.plan      QGroundControl: Plan > Open, then Upload (fence only)\n"
            "*fence.geojson   the same polygons for GIS\n"
            + ("chart/           copy BOTH files to the SD card as APM/scripts/chart/\n"
               "                 (shoal_guard.lua reads it for the lookahead)\n" if chart else "")
            + "ardupilot.parm   FENCE_*, OA_* and SHOAL_* settings\n\n"
            "Fences (%d of %d bytes each at most; water shrunk, no-go grown to fit):\n"
            % (info["bytes"], budget_bytes)
            + "".join("  %-7s %3d points, %3d bytes, simplified %4.0f ft, track outside %4.0f ft\n"
                      % (fc["name"], fc["vertices"], fc["bytes"], fc["tolerance_ft"],
                         fc["outside_ft"]) for fc in fences))
    return info
