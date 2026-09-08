"""
Getting a plan onto the water.

Two formats, because neither is enough on its own:

  GPX   Chartplotters, handhelds, phone navigation apps. Universal, no schema
        to argue with. QGroundControl will not read it.
  .plan QGroundControl's own format. Written here as plain waypoint items
        rather than Survey complex items: the Survey item's schema shifts
        between QGC releases and is strictly validated - a file that loads on
        one version is rejected on the next for a missing key. A list of
        MAV_CMD_NAV_WAYPOINT items has almost nothing to get wrong, and it
        flies the lines as planned instead of regenerating its own.
"""

from __future__ import annotations

import json
import os
import xml.sax.saxutils as saxutils

MPH_TO_MS = 0.44704
MAV_CMD_NAV_WAYPOINT = 16
MAV_FRAME_GLOBAL_RELATIVE_ALT = 3
MAV_TYPE_SURFACE_BOAT = 11
MAV_AUTOPILOT_ARDUPILOTMEGA = 3


def write_gpx(path: str, day, frame, name: str = "survey",
              access_points=None) -> str:
    """One day as GPX routes, with any access points as waypoints."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="SurveyPlanner" '
             'xmlns="http://www.topografix.com/GPX/1/1">']
    for point in (access_points or []):
        lon, lat = point["lonlat"]
        label = saxutils.escape(str(point.get("name", "ACCESS")))
        lines.append(f'  <wpt lat="{lat:.7f}" lon="{lon:.7f}">'
                     f'<name>{label}</name></wpt>')
    for i, leg in enumerate(day, start=1):
        if leg.get("transit"):
            lines.append(f'  <rte><name>{saxutils.escape(name)}-{i}-transit</name>')
            for x, y in leg["transit"]:
                lon, lat = frame.to_lonlat(x, y)
                lines.append(f'    <rtept lat="{lat:.7f}" lon="{lon:.7f}"></rtept>')
            lines.append("  </rte>")
        kind = leg.get("kind", "line")
        lines.append(f'  <rte><name>{saxutils.escape(name)}-{i}-{kind}</name>')
        for x, y in leg["coords"]:
            lon, lat = frame.to_lonlat(x, y)
            lines.append(f'    <rtept lat="{lat:.7f}" lon="{lon:.7f}"></rtept>')
        lines.append("  </rte>")
    lines.append("</gpx>")
    _write(path, "\n".join(lines))
    return path


def write_qgc_plan(path: str, day, frame, speed_mph: float = 3.0,
                   home_lonlat=None) -> str:
    """One day as a QGroundControl .plan of plain waypoints."""
    items, jump = [], 1
    for leg in day:
        for segment in (leg.get("transit") or [], leg["coords"]):
            for x, y in segment:
                lon, lat = frame.to_lonlat(x, y)
                items.append({
                    "AMSLAltAboveTerrain": None,
                    "Altitude": 0,
                    "AltitudeMode": 1,
                    "autoContinue": True,
                    "command": MAV_CMD_NAV_WAYPOINT,
                    "doJumpId": jump,
                    "frame": MAV_FRAME_GLOBAL_RELATIVE_ALT,
                    "params": [0, 0, 0, None, lat, lon, 0],
                    "type": "SimpleItem",
                })
                jump += 1
    if not items:
        raise ValueError("nothing to export")
    if home_lonlat is None:
        home = [items[0]["params"][4], items[0]["params"][5], 0]
    else:
        home = [home_lonlat[1], home_lonlat[0], 0]
    speed = round(speed_mph * MPH_TO_MS, 2)
    plan = {
        "fileType": "Plan",
        "geoFence": {"circles": [], "polygons": [], "version": 2},
        "groundStation": "QGroundControl",
        "mission": {
            "cruiseSpeed": speed,
            "firmwareType": MAV_AUTOPILOT_ARDUPILOTMEGA,
            "globalPlanAltitudeMode": 1,
            "hoverSpeed": speed,
            "items": items,
            "plannedHomePosition": home,
            "vehicleType": MAV_TYPE_SURFACE_BOAT,
            "version": 2,
        },
        "rallyPoints": {"points": [], "version": 2},
        "version": 1,
    }
    _write(path, json.dumps(plan, indent=4))
    return path


def write_geojson(path: str, days, frame, access_points=None) -> str:
    """The whole plan for GIS, with the day number on every line."""
    features = []
    for n, day in enumerate(days, start=1):
        for leg in day:
            features.append({
                "type": "Feature",
                "properties": {"day": n, "kind": leg.get("kind", "line")},
                "geometry": {"type": "LineString",
                             "coordinates": [list(frame.to_lonlat(x, y))
                                             for x, y in leg["coords"]]},
            })
    for point in (access_points or []):
        features.append({
            "type": "Feature",
            "properties": {"kind": "access", "name": point.get("name", "ACCESS")},
            "geometry": {"type": "Point", "coordinates": list(point["lonlat"])},
        })
    _write(path, json.dumps({"type": "FeatureCollection", "features": features}))
    return path


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_access_gpx(path: str, access: list) -> str:
    """
    Access points alone, as GPX waypoints.

    So the person driving to the lake can put them in a handheld or a phone
    without carrying the survey plan as well.
    """
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="SurveyPlanner" '
             'xmlns="http://www.topografix.com/GPX/1/1">']
    for point in access:
        lon, lat = point["lonlat"]
        label = saxutils.escape(str(point.get("name", "ACCESS")))
        lines.append(f'  <wpt lat="{lat:.7f}" lon="{lon:.7f}">'
                     f'<name>{label}</name><sym>Anchor</sym></wpt>')
    lines.append("</gpx>")
    _write(path, "\n".join(lines))
    return path
