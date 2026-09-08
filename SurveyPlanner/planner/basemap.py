"""
What the lake looks like from above, and how to get to it.

A shoreline polygon on a blank background tells you nothing about whether you
can reach the water. Underneath it goes imagery, the roads with their names,
and every mapped car park - because "mark where you can put a boat in" is a
question about parking and access, not about geometry.

Two public services, both usable without a key:

    Esri World Imagery   satellite tiles
    OpenStreetMap        roads and parking, through Overpass

Both are courtesy services. Tiles are cached on disk so panning around a lake
does not re-fetch, and a failure here is never fatal - the plan works on a
blank background, it is just harder to read.
"""

from __future__ import annotations

import io
import json
import math
import os
import urllib.parse
import urllib.request

TILE_URL = ("https://services.arcgisonline.com/arcgis/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}")
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "SurveyPlanner/0.1 (inland sonar survey planning)"
TILE_PX = 256
MAX_TILES = 64


def cache_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "SurveyPlanner", "tiles")
    os.makedirs(path, exist_ok=True)
    return path


# -- satellite -------------------------------------------------------------

def _lonlat_to_tile(lon, lat, z):
    n = 2.0 ** z
    x = (lon + 180.0) / 360.0 * n
    rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * n
    return x, y


def _tile_to_lonlat(x, y, z):
    n = 2.0 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lon, lat


def pick_zoom(bounds, max_tiles: int = MAX_TILES) -> int:
    """The most detailed zoom that still fits inside the tile budget."""
    west, south, east, north = bounds
    for z in range(19, 8, -1):
        x0, y0 = _lonlat_to_tile(west, north, z)
        x1, y1 = _lonlat_to_tile(east, south, z)
        tiles = (math.floor(x1) - math.floor(x0) + 1) * \
                (math.floor(y1) - math.floor(y0) + 1)
        if tiles <= max_tiles:
            return z
    return 12


def fetch_satellite(bounds, zoom: int = 0, progress=None):
    """
    A stitched satellite image covering `bounds` (west, south, east, north).

    Returns (image, extent) where extent is the image's true lon/lat box - not
    the box that was asked for, because tiles land on their own grid. Returns
    (None, None) if the imagery cannot be fetched; the caller draws without it.
    """
    try:
        from PIL import Image
    except ImportError:
        return None, None

    west, south, east, north = bounds
    z = zoom or pick_zoom(bounds)
    x0, y0 = _lonlat_to_tile(west, north, z)
    x1, y1 = _lonlat_to_tile(east, south, z)
    tx0, ty0, tx1, ty1 = (math.floor(x0), math.floor(y0),
                          math.floor(x1), math.floor(y1))
    across, down = tx1 - tx0 + 1, ty1 - ty0 + 1
    if across * down > MAX_TILES * 2:
        return None, None

    canvas = Image.new("RGB", (across * TILE_PX, down * TILE_PX), (12, 26, 38))
    got = 0
    for i, tx in enumerate(range(tx0, tx1 + 1)):
        for j, ty in enumerate(range(ty0, ty1 + 1)):
            tile = _tile(tx, ty, z)
            if tile is not None:
                canvas.paste(tile, (i * TILE_PX, j * TILE_PX))
                got += 1
            if progress:
                progress(got, across * down)
    if got == 0:
        return None, None

    nw = _tile_to_lonlat(tx0, ty0, z)
    se = _tile_to_lonlat(tx1 + 1, ty1 + 1, z)
    return canvas, (nw[0], se[1], se[0], nw[1])


def _tile(x: int, y: int, z: int):
    from PIL import Image

    path = os.path.join(cache_dir(), f"{z}_{x}_{y}.jpg")
    if os.path.exists(path):
        try:
            return Image.open(path).convert("RGB")
        except Exception:
            os.remove(path)
    url = TILE_URL.format(z=z, x=x, y=y)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        image.save(path, "JPEG", quality=85)
        return image
    except Exception:
        return None


# -- roads and parking -----------------------------------------------------

OVERPASS_QUERY = """
[out:json][timeout:40];
(
  way["amenity"="parking"]({s},{w},{n},{e});
  way["highway"~"^({roads})$"]({s},{w},{n},{e});
  node["amenity"="parking"]({s},{w},{n},{e});
  way["leisure"="slipway"]({s},{w},{n},{e});
  node["leisure"="slipway"]({s},{w},{n},{e});
  way["man_made"="pier"]({s},{w},{n},{e});
  way["leisure"="marina"]({s},{w},{n},{e});
  node["leisure"="marina"]({s},{w},{n},{e});
  way["waterway"="dock"]({s},{w},{n},{e});
  way["mooring"]({s},{w},{n},{e});
);
out geom;
"""

# What is worth asking for, by how much ground is on screen. A map showing
# forty miles of river cannot render a private drive, so fetching one is a
# slower query and a worse map: asking for every class over a long river
# returned thousands of named lanes and buried the water under its own labels.
ROAD_DETAIL = (
    # up to this span, in miles      classes worth drawing
    (4.0, "motorway|trunk|primary|secondary|tertiary|residential"
          "|unclassified|service"),
    (12.0, "motorway|trunk|primary|secondary|tertiary|unclassified"),
    (40.0, "motorway|trunk|primary|secondary|tertiary"),
    (float("inf"), "motorway|trunk|primary|secondary"),
)


def span_miles(bounds) -> float:
    """The longer side of a lon/lat box, in miles."""
    west, south, east, north = bounds
    middle = math.radians((south + north) / 2.0)
    return max((north - south) * 69.0,
               (east - west) * 69.0 * math.cos(middle))


def road_classes(bounds) -> str:
    """The Overpass alternation of road classes suited to this extent."""
    span = span_miles(bounds)
    for limit, classes in ROAD_DETAIL:
        if span <= limit:
            return classes
    return ROAD_DETAIL[-1][1]


def fetch_context(bounds, timeout: int = 60) -> dict:
    """
    Roads, car parks and slipways around the water, from OpenStreetMap.

    Slipways are asked for by name because they are the single most useful
    thing on this map: a mapped slipway is somewhere a boat demonstrably goes
    in, which no amount of shoreline geometry can tell you.

    Piers, marinas and moorings are asked for as well, because they are the
    things in the water the boat has to go round. They are a starting list,
    not a survey: most private docks on a small lake are not in OSM, and one
    that is may have been gone for years.

    Only what the extent can actually show is asked for. A forty-mile river
    fetched at full detail comes back with every private drive in the county,
    which is a slow query and an unreadable map.

    Returns {"roads": [...], "parking": [...], "slipways": [...],
    "docks": [...], "span_mi": float}, empty on any failure - this is context,
    never a dependency.
    """
    west, south, east, north = bounds
    query = OVERPASS_QUERY.format(s=south, w=west, n=north, e=east,
                                  roads=road_classes(bounds))
    out = {"roads": [], "parking": [], "slipways": [], "docks": [],
           "span_mi": span_miles(bounds)}
    try:
        data = urllib.parse.urlencode({"data": query}).encode()
        request = urllib.request.Request(OVERPASS_URL, data=data,
                                         headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return out

    for element in payload.get("elements", []):
        tags = element.get("tags", {})
        name = tags.get("name", "")
        if element.get("type") == "node":
            point = [(element.get("lon"), element.get("lat"))]
        else:
            point = [(p["lon"], p["lat"]) for p in element.get("geometry", [])]
        if not point or point[0][0] is None:
            continue
        if tags.get("leisure") == "slipway":
            out["slipways"].append({"name": name or "slipway", "coords": point})
        elif (tags.get("man_made") == "pier"
                or tags.get("leisure") == "marina"
                or tags.get("waterway") == "dock"
                or "mooring" in tags):
            kind = ("pier" if tags.get("man_made") == "pier" else
                    "marina" if tags.get("leisure") == "marina" else "dock")
            out["docks"].append({"name": name or kind, "coords": point})
        elif tags.get("amenity") == "parking":
            out["parking"].append({"name": name or "parking", "coords": point})
        elif tags.get("highway"):
            out["roads"].append({"name": name, "coords": point,
                                 "kind": tags["highway"]})
    return out


def bounds_of(rings, pad_frac: float = 0.06):
    """A padded lon/lat box around a waterbody's outline."""
    pts = rings[0]
    west = min(p[0] for p in pts)
    east = max(p[0] for p in pts)
    south = min(p[1] for p in pts)
    north = max(p[1] for p in pts)
    dx = (east - west) * pad_frac
    dy = (north - south) * pad_frac
    return (west - dx, south - dy, east + dx, north + dy)
