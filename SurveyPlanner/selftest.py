"""
Prove the engine works without opening a window.

Runs the whole chain against a real lake - fetch, plan, verify, export - so a
failure points at one stage instead of "the app doesn't work". The GUI is a
view over these calls; if this passes, anything still broken is in the window.

    python selftest.py                 # Indian Lake, Missouri
    python selftest.py 44.6 -93.2      # anywhere else
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from planner import exporters, plan as planning, shoreline
from planner.geometry import LocalFrame, polyline_length_ft

DEFAULT_LAT, DEFAULT_LON = 38.10535, -91.45444
MILES = 5280.0
failures = []


def step(name):
    print(f"\n[{name}]")


def check(label, ok, detail=""):
    print(f"   {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)
    return ok


def main(lat=DEFAULT_LAT, lon=DEFAULT_LON) -> int:
    print(f"Survey Planner self test  -  {lat}, {lon}")

    step("1/5 dependencies")
    for module in ("shapely", "numpy", "matplotlib", "skimage"):
        try:
            __import__(module)
            check(module, True)
        except ImportError as exc:
            check(module, False, str(exc))
    if failures:
        print("\nInstall them with:  pip install -r requirements.txt")
        return 1

    step("2/5 fetch shoreline from NHD")
    try:
        bodies = shoreline.fetch_waterbodies(lon, lat)
    except shoreline.ShorelineError as exc:
        check("reach the NHD service", False, str(exc))
        print("\n   (a network failure here is not a bug in the planner)")
        return 1
    if not check("waterbody found", bool(bodies)):
        return 1
    body = bodies[0]
    print(f"          {body['name']}, {body['acres']:,.0f} acres, "
          f"{len(body['rings'])} ring(s)")
    frame = LocalFrame.centred_on(body["rings"][0])
    poly = shoreline.to_polygon(body, frame)
    check("polygon is valid", poly.is_valid)
    check("polygon has area", poly.area > 0, f"{poly.area/43560:,.0f} acres")

    step("3/5 build a plan")
    settings = planning.PlanSettings(spacing_ft=40, setback_ft=50, speed_mph=3.0,
                                     day_hours=2.0, day_min_hours=1.5)
    lines, bearing = planning.build_lines(poly, settings)
    if not check("lines generated", bool(lines), f"{len(lines)} lines"):
        return 1
    settings.square_blocks = False
    settings.require_line_of_sight = False
    days, _plan_info = planning.build_plan(poly, settings)
    total = sum(polyline_length_ft(l["coords"]) for d in days for l in d)
    print(f"          bearing {bearing:.0f} deg, {len(days)} days, "
          f"{total/MILES:.1f} mi, {total/MILES/settings.speed_mph:.1f} h")

    clearance = planning.check_clearance(days, poly, settings)
    check("stays clear of the bank", clearance["ok"],
          f"{clearance['min_clearance_ft']:.1f} ft (need "
          f"{clearance['required_ft']:.0f})")
    # The route, not just the survey lines. A plan that crosses a dock
    # between two legs used to pass every check here, because the
    # offending segment is the join between legs and belongs to neither.
    from shapely.geometry import LineString
    over_land = 0.0
    for day in days:
        track = planning.mission_track(day)
        if len(track) > 1:
            over_land += LineString(track).difference(poly).length
    check("the route stays on the water", over_land < 1.0,
          format(over_land, ",.0f") + " ft over land")
    turns = planning.check_turns(days)
    check("lines are straight", turns["max_turn_deg"] < 1.0,
          f"max turn {turns['max_turn_deg']:.1f} deg")
    rows = planning.summarise(days, settings)
    long_ = [r for r in rows if r["hours"] > settings.day_hours + 0.01]
    check("no day runs past the hours allowed", not long_,
          (str(len(long_)) + " day(s) over " if long_ else "worst ")
          + format(max(r["hours"] for r in rows), ".2f") + " h")
    short = [r for r in rows[:-1] if r["hours"] < settings.day_min_hours - 0.01]
    check("every day but the last meets the minimum", not short,
          f"{len(short)} short" if short else
          f"{min(r['hours'] for r in rows):.2f}-{max(r['hours'] for r in rows):.2f} h")

    shore = planning.PlanSettings(spacing_ft=40, setback_ft=50, speed_mph=3.0,
                                  day_hours=2.0, day_min_hours=1.5,
                                  square_blocks=False,
                                  require_line_of_sight=False, shore_pass=True)
    shore_days, shore_info = planning.build_plan(poly, shore)
    shore_turns = planning.check_turns(shore_days)
    shore_only = [d for d in shore_days
                  if any(l.get("kind") == "shore" for l in d)
                  and not any(l.get("kind") not in ("shore", "return")
                              for l in d)]
    with_shore = sum(1 for d in shore_days
                     if any(l.get("kind") == "shore" for l in d))
    check("the shore transect is worked into the days", not shore_only,
          f"{with_shore} of {len(shore_days)} days include shore work, "
          f"{len(shore_only)} are shore only")
    check("the shore transect obeys the turn filter",
          shore_turns["max_shore_turn_deg"] <= shore.turn_limit_deg + 0.5,
          f"{shore_turns['max_shore_turn_deg']:.0f} of "
          f"{shore.turn_limit_deg:.0f} deg over 10 m")
    check("the grid stays straight beside it",
          shore_turns["max_turn_deg"] < 1.0,
          f"{shore_turns['max_turn_deg']:.1f} deg")

    step("4/5 orthogonal pass")
    ortho = planning.PlanSettings(spacing_ft=40, setback_ft=50, speed_mph=3.0,
                                  orthogonal=True, orthogonal_spacing_ft=80.0)
    olines, _ = planning.build_lines(poly, ortho)
    kinds = {}
    for line in olines:
        kinds[line["kind"]] = kinds.get(line["kind"], 0) + 1
    check("both passes present", len(kinds) == 2, str(kinds))

    step("5/5 export")
    out = tempfile.mkdtemp(prefix="surveyplanner_")
    access = [{"lonlat": (lon, lat), "name": "TEST ACCESS"}]
    try:
        gpx = exporters.write_gpx(os.path.join(out, "day_01.gpx"), days[0],
                                  frame, "day1", access)
        qgc = exporters.write_qgc_plan(os.path.join(out, "day_01.plan"), days[0],
                                       frame, settings.speed_mph)
        gj = exporters.write_geojson(os.path.join(out, "plan.geojson"), days,
                                     frame, access)
        shoreline.save(body, os.path.join(out, "shoreline.json"))
    except Exception:
        check("export ran", False)
        traceback.print_exc()
        return 1
    check("gpx written", os.path.getsize(gpx) > 200, f"{os.path.getsize(gpx):,} bytes")
    payload = json.load(open(qgc))
    check("plan is valid json", payload.get("fileType") == "Plan")
    check("plan has waypoints", len(payload["mission"]["items"]) > 0,
          f"{len(payload['mission']['items'])} items")
    check("plan is a boat mission", payload["mission"]["vehicleType"] == 11)
    check("geojson written", len(json.load(open(gj))["features"]) > 0)
    reloaded = shoreline.load(os.path.join(out, "shoreline.json"))
    check("shoreline round-trips", reloaded["name"] == body["name"])
    print(f"\n   files in {out}")

    step("extra: depth grids and boat package")
    depth_grid_checks(frame, poly, out)

    print("\n" + "=" * 58)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: " + ", ".join(failures))
        return 1
    print("ALL CHECKS PASSED")
    return 0


def depth_grid_checks(frame, poly, out):
    """
    A synthetic AnchorHold depth grid over the middle of the lake - 20 ft of
    water with one 2 ft shoal - through no-go, planning and the boat package.
    Synthetic so the check runs anywhere; the format is the pipeline's own.
    """
    import math

    import numpy as np
    from shapely.geometry import LineString, Point
    from planner import boat, depthgrid

    cx, cy = poly.representative_point().coords[0]
    lon0, lat0 = frame.to_lonlat(cx - 300, cy - 300)
    lon1, lat1 = frame.to_lonlat(cx + 300, cy + 300)
    cols = rows = 121                                       # 5 ft cells
    grid = os.path.join(out, "chart_synthetic")
    os.makedirs(grid, exist_ok=True)
    header = {"lonMin": lon0, "latMin": lat0, "dLon": (lon1 - lon0) / (cols - 1),
              "dLat": (lat1 - lat0) / (rows - 1), "cols": cols, "rows": rows, "nodata": "nan"}
    ys, xs = np.mgrid[0:rows, 0:cols]
    depth_ft = np.where(np.hypot(xs - 60, ys - 60) * 5.0 < 40, 2.0, 20.0)
    with open(os.path.join(grid, "depth_grid.json"), "w") as f:
        json.dump(header, f)
    (depth_ft / 3.280839895).astype("<f4").tofile(os.path.join(grid, "depth_grid.bin"))

    try:
        zones, grids, notes = depthgrid.shallow_no_go([grid], frame, poly, 3.0, 0)
    except Exception:
        check("depth grid read", False)
        traceback.print_exc()
        return
    if not check("one shallow area found", len(zones) == 1, notes[0]):
        return
    shoal = zones[0]["geom"]
    check("its size matches the shoal", 0.8 < shoal.area / (math.pi * 40 * 40) < 1.3,
          f"{shoal.area:,.0f} sq ft")
    check("centred on the shoal", shoal.contains(Point(cx, cy)))

    settings = planning.PlanSettings(spacing_ft=40, setback_ft=50, speed_mph=3.0,
                                     square_blocks=False, require_line_of_sight=False,
                                     no_go=[shoal])
    days, _ = planning.build_plan(poly, settings)
    lines = [LineString(l["coords"]) for d in days for l in d
             if l.get("kind") not in ("return",) and len(l["coords"]) > 1]
    hit = sum(l.intersection(shoal).length for l in lines)
    check("survey lines stay out of the shoal", hit < 1.0, f"{hit:.0f} ft inside")

    tracks = [planning.mission_track(d) for d in days]
    launch = [tracks[0][0]] if tracks and tracks[0] else []
    info = boat.write_package(os.path.join(out, "boat"), frame, poly, zones, grids,
                              launches=launch, tracks=tracks)
    check("fence fits Pixhawk1 storage", info["bytes"] <= info["budget"],
          f"{info['vertices']} points, {info['bytes']} of {info['budget']} bytes, "
          f"simplified {info['tolerance_ft']:.0f} ft around {info['fenced']}")
    outside = sum(LineString(l["coords"]).difference(fence["allowed"]).length
                  for day, fence in zip(days, info["fences"]) for l in day
                  if l.get("kind") != "return" and len(l["coords"]) > 1)
    check("each day's survey lines are inside its fence", outside < 1.0,
          f"{len(info['fences'])} fence(s), {outside:.0f} ft outside")
    print(f"          whole route outside the fence: {info['outside_ft']:.0f} ft"
          " (transits between lines are reported, not failed)")
    chart = os.path.join(out, "boat", "chart", "depth_grid.bin")
    check("chart written for the SD card", os.path.getsize(chart) == cols * rows * 4)

    recording_checks(frame, poly, out)


def _fake_recording(folder, name, frame, start_xy, pings):
    """
    A Helix recording written ping by ping: `pings` is a list of
    (dx_ft, dy_ft, depth_m) from `start_xy`, or None for a ping stamped with
    no fix. Only the header fields the planner reads are filled in.
    """
    import math
    import struct
    from planner import humminbird

    son_dir = os.path.join(folder, name)
    os.makedirs(son_dir, exist_ok=True)
    with open(os.path.join(folder, name + ".DAT"), "wb") as fh:
        fh.write(bytes(64))
    body = bytearray()
    for i, ping in enumerate(pings):
        if ping is None:
            e = n = 0
            depth_dm = 60
        else:
            dx, dy, depth_m = ping
            lon, lat = frame.to_lonlat(start_xy[0] + dx, start_xy[1] + dy)
            # the inverse of humminbird.lat_lon
            phi = math.atan(math.tan(math.radians(lat)) / 1.0067642927)
            n = int(round(humminbird.R_EARTH * math.log(math.tan((phi + math.pi / 2) / 2))))
            e = int(round(math.radians(lon) * humminbird.R_EARTH))
            depth_dm = int(round(depth_m * 10))
        head = bytearray(humminbird.HEADER_LEN)
        head[0:4] = humminbird.MAGIC
        struct.pack_into(">I", head, 5, i)
        struct.pack_into(">I", head, 10, i * 75)
        struct.pack_into(">i", head, 15, e)
        struct.pack_into(">i", head, 20, n)
        struct.pack_into(">I", head, 35, depth_dm)
        struct.pack_into(">I", head, 62, 16)
        body += head + bytes(16)
    with open(os.path.join(son_dir, "B001.SON"), "wb") as fh:
        fh.write(body)
    return os.path.join(folder, name + ".DAT")


def recording_checks(frame, poly, out):
    """
    A Humminbird recording through the same road: 300 pings across the
    lake at 2 ft spacing in 20 ft of water, with a 2 ft shoal in the
    middle, three pings stamped before the GPS had a fix, one ping with no
    bottom and one ping that read 270 m.
    """
    from planner import depthgrid

    cx, cy = poly.representative_point().coords[0]
    pings = [None, None, None]
    for i in range(300):
        depth = 0.6 if 130 <= i < 170 else 6.1
        pings.append((i * 2.0 - 300.0, 0.0, depth))
    pings[50] = (pings[50][0], 0.0, 0.0)          # no bottom
    pings[80] = (pings[80][0], 0.0, 270.0)        # a spike
    dat = _fake_recording(os.path.join(out, "recording"), "R00099", frame, (cx, cy), pings)
    try:
        zones, grids, notes = depthgrid.shallow_no_go([dat], frame, poly, 3.0, 15.0)
    except Exception:
        check("recording read", False)
        traceback.print_exc()
        return
    check("recording gives one shallow area", len(zones) == 1, notes[0])
    check("pings without a fix or a bottom are dropped",
          "3 without a fix" in notes[0] and "1 without a bottom" in notes[0])
    import numpy as np
    deepest = float(np.nanmax(grids[0].depth_m))
    check("a 270 m spike does not chart deep water", deepest < 7.0,
          f"deepest cell {deepest:.1f} m")
    if zones:
        from shapely.geometry import Point
        check("the shoal is where the recording found it",
              zones[0]["geom"].contains(Point(cx, cy)))


if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        if len(args) >= 2:
            sys.exit(main(float(args[0]), float(args[1])))
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
