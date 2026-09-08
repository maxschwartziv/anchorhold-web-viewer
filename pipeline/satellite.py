#!/usr/bin/env python3
"""
Satellite imagery to put a survey on, fetched a tile at a time and cached.

Esri's World Imagery, in the ordinary XYZ tile scheme, stitched into one image
with the lon/lat box it actually covers. Tiles land on their own grid, so what
comes back is never exactly the box that was asked for - the caller draws the
image at the extent returned, not the extent requested, or everything on top of
it sits in the wrong place.

Tiles are cached under %LOCALAPPDATA% and never expire: the point of a chart
plotter is that it works on the water, and imagery of a lake does not change
between one survey and the next.
"""

from __future__ import annotations

import io
import math
import os
import urllib.request

TILE_URL = ("https://services.arcgisonline.com/arcgis/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}")
USER_AGENT = "AnchoringApp/1.0 (survey recording review)"
TILE_PX = 256

# A screenful of imagery, and the ceiling on what one fetch will ask for. The
# budget is what keeps a zoomed-out view from asking for a thousand tiles.
MAX_TILES = 64
HARD_MAX_TILES = 160


def cache_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "AnchoringApp", "tiles")
    os.makedirs(path, exist_ok=True)
    return path


def lonlat_to_tile(lon: float, lat: float, z: int):
    n = 2.0 ** z
    x = (lon + 180.0) / 360.0 * n
    rad = math.radians(max(-85.05, min(85.05, lat)))
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * n
    return x, y


def tile_to_lonlat(x: float, y: float, z: int):
    n = 2.0 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lon, lat


def pick_zoom(bounds, max_tiles: int = MAX_TILES) -> int:
    """The most detailed zoom whose tile count still fits the budget."""
    west, south, east, north = bounds
    for z in range(19, 8, -1):
        x0, y0 = lonlat_to_tile(west, north, z)
        x1, y1 = lonlat_to_tile(east, south, z)
        tiles = ((math.floor(x1) - math.floor(x0) + 1)
                 * (math.floor(y1) - math.floor(y0) + 1))
        if tiles <= max_tiles:
            return z
    return 12


def fetch(bounds, zoom: int = 0, cancel=None, progress=None):
    """
    A stitched image for `bounds` (west, south, east, north).

    Returns (PIL image, (west, south, east, north)) for what was actually
    covered, or (None, None) when the imagery cannot be had - no network, a
    blocked host, a box too big to be worth fetching. A caller that gets None
    draws the survey on a plain background, which is worse to look at and just
    as correct.
    """
    try:
        from PIL import Image
    except ImportError:
        return None, None

    west, south, east, north = bounds
    if east <= west or north <= south:
        return None, None
    z = zoom or pick_zoom(bounds)
    x0, y0 = lonlat_to_tile(west, north, z)
    x1, y1 = lonlat_to_tile(east, south, z)
    tx0, ty0 = math.floor(x0), math.floor(y0)
    tx1, ty1 = math.floor(x1), math.floor(y1)
    across, down = tx1 - tx0 + 1, ty1 - ty0 + 1
    if across * down > HARD_MAX_TILES:
        return None, None

    canvas = Image.new("RGB", (across * TILE_PX, down * TILE_PX), (14, 26, 34))
    wanted = across * down
    got = 0
    for i, tx in enumerate(range(tx0, tx1 + 1)):
        for j, ty in enumerate(range(ty0, ty1 + 1)):
            if cancel is not None and cancel.is_set():
                return None, None
            tile = _tile(tx, ty, z)
            if tile is not None:
                canvas.paste(tile, (i * TILE_PX, j * TILE_PX))
                got += 1
            if progress:
                progress(got, wanted)
    if got == 0:
        return None, None

    nw = tile_to_lonlat(tx0, ty0, z)
    se = tile_to_lonlat(tx1 + 1, ty1 + 1, z)
    return canvas, (nw[0], se[1], se[0], nw[1])


def _tile(x: int, y: int, z: int):
    from PIL import Image

    path = os.path.join(cache_dir(), f"{z}_{x}_{y}.jpg")
    if os.path.isfile(path):
        try:
            return Image.open(path).convert("RGB")
        except Exception:
            os.remove(path)                      # a half-written tile from last time
    try:
        request = urllib.request.Request(TILE_URL.format(z=z, x=x, y=y),
                                         headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        image.save(path, "JPEG", quality=85)
        return image
    except Exception:
        return None
