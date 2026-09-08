"""
Where the water ends.

A survey plan is only as good as its idea of the shoreline, and deriving one
from imagery is a research project. The USGS National Hydrography Dataset
already holds a polygon for essentially every lake, pond and mapped river reach
in the United States, served live, so this asks for it rather than guessing.

Two layers matter:

    12  Waterbody - Large Scale   lakes and ponds
     9  Area      - Large Scale   rivers wide enough to be drawn as polygons

Both are queried, because "the water near this pin" is as often a river as a
lake and the person dropping the pin should not have to know which.

NHD was retired on 1 October 2023 - the data stays published but is no longer
maintained, and new work moves to the 3D Hydrography Program. For a lake whose
outline has not moved that is no obstacle; a reservoir drawn down twenty feet
will not match, and nothing here can tell you that has happened.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
import urllib.request

NHD_QUERY = "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/{layer}/query"
LAYERS = ((12, "lake"), (9, "river"))
NAME_FIELDS = ("gnis_name", "GNIS_NAME", "name", "NAME")
AREA_FIELDS = ("areasqkm", "AREASQKM")
SQKM_TO_ACRES = 247.105


class ShorelineError(RuntimeError):
    pass


# -- reading a dropped pin ---------------------------------------------------

_DECIMAL = re.compile(r"(-?\d{1,3}\.\d+)[,\s]+(-?\d{1,3}\.\d+)")
_AT = re.compile(r"@(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)")
_QUERY = re.compile(r"[?&](?:q|ll|daddr|center)=(?:loc:)?(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)")
_DATA = re.compile(r"!3d(-?\d{1,3}\.\d+)!4d(-?\d{1,3}\.\d+)")
_DMS = re.compile(
    r"""(\d{1,3})[^\d]+(\d{1,2})[^\d]+([\d.]+)[^\dNSEW]*([NS])   # lat
        [^\d]+
        (\d{1,3})[^\d]+(\d{1,2})[^\d]+([\d.]+)[^\dNSEW]*([EW])""",  # lon
    re.VERBOSE)


def parse_location(text: str):
    """
    Pull a latitude and longitude out of whatever someone pasted.

    Google Maps hands out several shapes depending on how you got there - the
    URL bar, "share", right-click "what's here", the app - and none of them is
    obviously the coordinate. Rather than tell people which one to use, read
    them all, plus a plain typed pair and degrees-minutes-seconds.

    Returns (lat, lon), or raises. Shortened goo.gl/maps links carry no
    coordinate at all until they are followed, so those are refused by name.
    """
    if not text or not text.strip():
        raise ShorelineError("Nothing to read - paste a pin or type 'lat, lon'.")
    text = text.strip()

    if "goo.gl/maps" in text or "maps.app.goo.gl" in text:
        raise ShorelineError(
            "That is a shortened Google link, which holds no coordinate until "
            "it is opened. Open it in a browser, then copy the longer URL from "
            "the address bar - or right-click the pin and copy the numbers.")

    for pattern in (_DATA, _AT, _QUERY):
        hit = pattern.search(text)
        if hit:
            return _validate(float(hit.group(1)), float(hit.group(2)))

    hit = _DMS.search(text)
    if hit:
        lat = _dms(hit.group(1), hit.group(2), hit.group(3), hit.group(4))
        lon = _dms(hit.group(5), hit.group(6), hit.group(7), hit.group(8))
        return _validate(lat, lon)

    hit = _DECIMAL.search(text)
    if hit:
        return _validate(float(hit.group(1)), float(hit.group(2)))

    raise ShorelineError(
        "Could not find a coordinate in that. Paste a Google Maps URL, or type "
        "the latitude and longitude separated by a comma.")


def _dms(deg, minutes, seconds, hemisphere) -> float:
    value = float(deg) + float(minutes) / 60.0 + float(seconds) / 3600.0
    return -value if hemisphere.upper() in ("S", "W") else value


def _validate(lat: float, lon: float):
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ShorelineError(f"{lat}, {lon} is not a valid position.")
    return lat, lon


# -- asking the NHD ----------------------------------------------------------

def fetch_waterbodies(lon: float, lat: float, radius_mi: float = 1.0,
                      timeout: int = 6, attempts: int = 4,
                      cancel=None, on_retry=None) -> list:
    """
    Every NHD lake and river polygon near a point, nearest first.

    Nearest, not largest: the pin is the question, so the water it landed
    on or beside should be the first answer. Each result carries its
    distance from the pin so the caller can say why it is offering what it
    is offering.

    The service is erratic rather than slow. The same query timed three
    times running came back in 0.51 s, then 60.44 s, then 0.55 s - so the
    fix is not patience but a short leash: give each attempt `timeout`
    seconds and try again rather than waiting out a stall that a retry
    answers instantly. Six seconds is a long leash for a request that
    normally lands in under one.

    Both layers go at once. Measured over three rounds that is 0.36 s,
    0.37 s and 3.73 s against 9.07 s, 2.68 s and 14.93 s one after the
    other: whichever layer is sulking, the other is not held up by it.

    `cancel` is a threading.Event. It is checked between attempts, so a
    cancelled fetch gives up within one timeout rather than at the end.
    """
    import concurrent.futures
    deg_lat = radius_mi / 69.0
    deg_lon = deg_lat / max(0.1, abs(math.cos(math.radians(lat))))
    envelope = f"{lon-deg_lon},{lat-deg_lat},{lon+deg_lon},{lat+deg_lat}"

    found, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(LAYERS)) as pool:
        jobs = {pool.submit(_query_with_retry, layer, kind, envelope,
                            timeout, attempts, cancel, on_retry): layer
                for layer, kind in LAYERS}
        for job in concurrent.futures.as_completed(jobs):
            try:
                found += job.result()
            except Exception as exc:          # one layer down is survivable
                errors.append("layer " + str(jobs[job]) + ": " + str(exc))
    if cancel is not None and cancel.is_set():
        return []
    if not found and errors:
        raise ShorelineError("Could not reach the NHD service. "
                             + "; ".join(errors))

    for body in found:
        body["distance_ft"] = _distance_ft(body["rings"], lon, lat)
    found.sort(key=lambda b: b["distance_ft"])
    return found


def _query_with_retry(layer, kind, envelope, timeout, attempts, cancel,
                      on_retry=None):
    """One layer, retried, giving up the moment the caller cancels."""
    last = None
    for attempt in range(max(1, attempts)):
        if cancel is not None and cancel.is_set():
            return []
        try:
            return _query(layer, kind, envelope, timeout)
        except Exception as exc:
            last = exc
            if on_retry is not None and attempt + 1 < attempts:
                on_retry(layer, attempt + 2, attempts)
    raise last


def _query(layer: int, kind: str, envelope: str, timeout: int) -> list:
    params = {
        "geometry": envelope,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*", "returnGeometry": "true", "f": "geojson",
    }
    url = f"{NHD_QUERY.format(layer=layer)}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    out = []
    for feature in payload.get("features", []):
        rings = _rings_of(feature.get("geometry") or {})
        if not rings:
            continue
        props = feature.get("properties", {})
        out.append({
            "name": _first(props, NAME_FIELDS) or f"(unnamed {kind})",
            "kind": kind,
            "acres": float(_first(props, AREA_FIELDS) or 0.0) * SQKM_TO_ACRES,
            "rings": rings,
        })
    return out


def _distance_ft(rings, lon: float, lat: float) -> float:
    """Feet from the pin to the nearest point of the outline, 0 if inside."""
    from shapely.geometry import Point, Polygon

    from .geometry import LocalFrame

    frame = LocalFrame(lon, lat)
    poly = Polygon(frame.ring_to_ft(rings[0]),
                   [frame.ring_to_ft(r) for r in rings[1:]])
    if not poly.is_valid:
        poly = poly.buffer(0)
    return float(poly.distance(Point(0.0, 0.0)))


def _first(props: dict, keys):
    for key in keys:
        if props.get(key) not in (None, ""):
            return props[key]
    return None


def _rings_of(geom: dict):
    kind = geom.get("type")
    if kind == "Polygon":
        rings = geom.get("coordinates") or []
    elif kind == "MultiPolygon":
        polys = geom.get("coordinates") or []
        if not polys:
            return []
        rings = max(polys, key=lambda p: len(p[0]) if p else 0)
    else:
        return []
    return [[(float(p[0]), float(p[1])) for p in ring] for ring in rings if ring]


# -- using it ----------------------------------------------------------------

def snap_to_shore(body: dict, frame, lon: float, lat: float):
    """
    Move a marked point onto the shoreline itself.

    Access points are placed by clicking a map, and a click is never exactly on
    the line. Left where it landed, a point a few feet inland reads as being on
    dry ground and a point out in the water reads as a boat already afloat -
    neither is what "where I put in" means. Snapping to the outline makes the
    mark unambiguous.

    Returns ((lon, lat), moved_ft).
    """
    from shapely.geometry import Point
    from shapely.ops import nearest_points

    poly = to_polygon(body, frame)
    here = Point(*frame.to_ft(lon, lat))
    on_shore = nearest_points(poly.boundary, here)[0]
    return frame.to_lonlat(on_shore.x, on_shore.y), float(here.distance(on_shore))


def to_polygon(body: dict, frame):
    """The waterbody as a shapely polygon in local feet, islands included."""
    from shapely.geometry import Polygon

    rings = body["rings"]
    poly = Polygon(frame.ring_to_ft(rings[0]),
                   [frame.ring_to_ft(r) for r in rings[1:]])
    if not poly.is_valid:
        poly = poly.buffer(0)                    # self-touching rings do occur
    return poly


def save(body: dict, path: str) -> None:
    """Keep a copy so a plan can be reopened without the network."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=1)


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        body = json.load(fh)
    body["rings"] = [[(float(p[0]), float(p[1])) for p in ring]
                     for ring in body["rings"]]
    body.setdefault("kind", "lake")
    body.setdefault("distance_ft", 0.0)
    # A file saved by this program carries its access points; adopt them into
    # the local store so they are there whether the lake is opened from the
    # file or fetched fresh.
    if body.get("access"):
        try:
            save_access(body, [{"name": p.get("name", "ACCESS"),
                                "lonlat": tuple(p["lonlat"])}
                               for p in body["access"] if p.get("lonlat")])
        except OSError:
            pass
    return body


# -- remembering where you put in -------------------------------------------

def body_key(body: dict) -> str:
    """
    A stable name for a waterbody, so its access points can be found again.

    Built from the name and the centre of its outline rounded to about a
    hundred metres. Two different lakes will not collide; the same lake fetched
    next week will match, even though the NHD may hand back its rings in a
    different order or with a different vertex count.
    """
    import hashlib

    ring = body["rings"][0]
    lon = sum(p[0] for p in ring) / len(ring)
    lat = sum(p[1] for p in ring) / len(ring)
    seed = f"{body.get('name','?')}|{lon:.3f}|{lat:.3f}"
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()[:10]
    safe = "".join(c if c.isalnum() else "_" for c in body.get("name", "water"))[:40]
    return f"{safe}_{digest}"


def access_store() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "SurveyPlanner", "access")
    os.makedirs(path, exist_ok=True)
    return path


def save_access(body: dict, access: list) -> str:
    """
    Keep this water's access points beside the program, not in the plan.

    Where you can get a boat in is a property of the lake and of who owns the
    bank - it does not change because the survey parameters did. Tying it to
    the waterbody means a fetch next season brings back the ramps you already
    found, including the ones that turned out to be someone's garden.
    """
    path = os.path.join(access_store(), body_key(body) + ".json")
    payload = {"name": body.get("name", ""), "kind": body.get("kind", "lake"),
               "access": [{"name": p.get("name", "ACCESS"),
                           "lonlat": [float(p["lonlat"][0]), float(p["lonlat"][1])]}
                          for p in access]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    return path


def load_access(body: dict) -> list:
    """Access points remembered for this water, or an empty list."""
    path = os.path.join(access_store(), body_key(body) + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return []
    return [{"name": p.get("name", "ACCESS"),
             "lonlat": (float(p["lonlat"][0]), float(p["lonlat"][1]))}
            for p in saved.get("access", []) if p.get("lonlat")]


def no_go_store() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "SurveyPlanner", "nogo")
    os.makedirs(path, exist_ok=True)
    return path


def save_no_go(body: dict, zones: list, frame, path: str = "") -> str:
    """
    Keep this water's no-go areas, for the same reason as its ramps.

    Where the docks are is a property of the lake, not of the survey. It
    is also the slowest thing to establish - a hundred zones read off
    imagery and then corrected by somebody who has been there - and losing
    it because the line spacing changed would be indefensible.

    Stored as lon/lat rings so the file survives a different local frame,
    and beside the program unless a path is given.
    """
    target = path or os.path.join(no_go_store(), body_key(body) + ".json")
    payload = {"name": body.get("name", ""),
               "kind": body.get("kind", "lake"),
               "no_go": [_zone_to_rings(z, frame) for z in zones]}
    os.makedirs(os.path.dirname(os.path.abspath(target)) or ".",
                exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    return target


def _zone_to_rings(zone: dict, frame) -> dict:
    geom = zone["geom"]
    parts = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    rings = [[list(frame.to_lonlat(x, y)) for x, y in part.exterior.coords]
             for part in parts]
    return {"name": zone.get("name", "no-go"),
            "kind": zone.get("kind", "drawn"), "rings": rings}


def load_no_go(body: dict, frame, path: str = "") -> list:
    """No-go areas remembered for this water, or an empty list."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    target = path or os.path.join(no_go_store(), body_key(body) + ".json")
    try:
        with open(target, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for entry in saved.get("no_go", []):
        parts = []
        for ring in entry.get("rings", []):
            if len(ring) < 4:
                continue
            shape = Polygon([frame.to_ft(lon, lat) for lon, lat in ring])
            if not shape.is_valid:
                shape = shape.buffer(0)
            if not shape.is_empty:
                parts.append(shape)
        if not parts:
            continue
        out.append({"name": entry.get("name", "no-go"),
                    "kind": entry.get("kind", "drawn"),
                    "geom": parts[0] if len(parts) == 1
                            else unary_union(parts)})
    return out


def load_any(path: str) -> dict:
    """
    Read a waterbody from whatever someone points at.

    Our own saved file keeps the access points with it; a GeoJSON or shapefile
    from elsewhere carries only geometry, and its access points are looked up
    from the local store instead.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".json", ".geojson"):
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict) and "rings" in payload:
            return load(path)
        return _from_geojson(payload, path)
    if ext == ".shp":
        return _from_shapefile(path)
    raise ShorelineError(f"Cannot read {ext or 'that file'} - use .json, "
                         ".geojson or .shp.")


def _from_geojson(payload: dict, path: str) -> dict:
    features = payload.get("features") or ([payload] if payload.get("geometry")
                                           else [])
    best, best_rings = None, None
    for feature in features:
        rings = _rings_of(feature.get("geometry") or {})
        if not rings:
            continue
        size = len(rings[0])
        if best is None or size > best:
            best, best_rings = size, rings
            props = feature.get("properties") or {}
    if not best_rings:
        raise ShorelineError("No polygon in that file.")
    return {"name": _first(props, NAME_FIELDS) or os.path.basename(path),
            "kind": "lake", "acres": 0.0, "rings": best_rings,
            "distance_ft": 0.0}


def _from_shapefile(path: str) -> dict:
    try:
        import shapefile                     # pyshp
    except ImportError as exc:
        raise ShorelineError(
            "Reading .shp needs the pyshp package:  pip install pyshp\n"
            "Or export the layer as GeoJSON, which needs nothing extra.") from exc
    reader = shapefile.Reader(path)
    best, best_shape = 0, None
    for shape in reader.shapes():
        if shape.shapeType not in (5, 15, 25):        # polygon variants
            continue
        if len(shape.points) > best:
            best, best_shape = len(shape.points), shape
    if best_shape is None:
        raise ShorelineError("That shapefile holds no polygons.")
    parts = list(best_shape.parts) + [len(best_shape.points)]
    rings = [[(float(x), float(y)) for x, y in best_shape.points[a:b]]
             for a, b in zip(parts, parts[1:]) if b - a >= 4]
    if not rings:
        raise ShorelineError("That shapefile holds no usable rings.")
    rings.sort(key=len, reverse=True)
    return {"name": os.path.splitext(os.path.basename(path))[0], "kind": "lake",
            "acres": 0.0, "rings": rings, "distance_ft": 0.0}


def save_access_file(body: dict, access: list, path: str) -> str:
    """
    Write access points to a file of the caller's choosing.

    The waterbody's name and key travel with them, so a file picked up months
    later says which water it belongs to rather than being a bare list of
    coordinates.
    """
    payload = {
        "waterbody": body.get("name", ""),
        "kind": body.get("kind", "lake"),
        "key": body_key(body),
        "access": [{"name": p.get("name", "ACCESS"),
                    "lonlat": [float(p["lonlat"][0]), float(p["lonlat"][1])]}
                   for p in access],
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    return path


def load_access_file(path: str) -> list:
    """
    Read access points from a file this program wrote, or from a plain list.

    Deliberately forgiving about which waterbody they came from: someone
    sharing ramps for a lake you fetched yourself should not be turned away
    because the key does not match.
    """
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    items = payload.get("access") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("no access points in that file")
    out = []
    for i, entry in enumerate(items, start=1):
        if isinstance(entry, dict) and entry.get("lonlat"):
            lon, lat = entry["lonlat"][0], entry["lonlat"][1]
            name = entry.get("name") or f"ACCESS {i}"
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            lon, lat = entry[0], entry[1]
            name = f"ACCESS {i}"
        else:
            continue
        out.append({"name": str(name), "lonlat": (float(lon), float(lat))})
    return out
