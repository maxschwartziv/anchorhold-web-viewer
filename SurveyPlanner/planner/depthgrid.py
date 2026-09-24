"""
Shallow water from AnchorHold depth grids, and the chart the boat carries.

A depth grid is what anchorhold-web-viewer's pipeline writes beside every
chart: `depth_grid.json` (lonMin, latMin, dLon, dLat, cols, rows) and
`depth_grid.bin`, float32 little-endian, row 0 the southern row, NaN where
nothing was sounded. Depths are metres below chart datum; a lake has no tide,
so that is metres below the water surface on the day of the survey.

**Only charted shallow water becomes a no-go area.** Water that was never
sounded is exactly what a survey plan is for, so it is left alone here. The
boat's own copy of the chart is where unsounded water counts as dry.

**Interpolation is not measurement.** The pipeline fills the whole convex hull
of the soundings and masks only beyond a 200 ft floor, so a corner the boat
never crossed can carry a depth invented from a sounding a hundred feet away -
Indian Hills Lake has one, a fan painted from a single 18.5 m reading at the
launch. When the chart folder holds its `track.geojson`, cells farther than
`trust_within_ft` from the track are treated as unsounded. Half the line
spacing of the survey that made the chart is about right.

**A Humminbird recording works too.** Its pings carry a position and the depth
the unit read (planner/humminbird.py); they are gridded here the way AnchorHold
would, except that a cell only gets a depth from soundings within
`trust_within_ft` of it - nothing is interpolated across open water - and a
cell keeps the shallowest sounding that fell in it.

Polygons come back in the planner's local feet, like every other no-go area.
"""

from __future__ import annotations

import json
import math
import os

FT_PER_M = 3.280839895


class DepthGrid:
    """One AnchorHold depth grid, optionally masked to its survey track."""

    @classmethod
    def from_array(cls, header: dict, depth_m, name: str) -> "DepthGrid":
        """A grid built here rather than read from disk (from a recording)."""
        grid = cls.__new__(cls)
        grid.header, grid.depth_m = dict(header), depth_m
        grid.folder, grid.name, grid.masked = "", name, 0
        return grid

    def __init__(self, folder: str):
        head = os.path.join(folder, "depth_grid.json")
        if not os.path.exists(head):
            raise ValueError(folder + " has no depth_grid.json - pick the chart folder"
                             " AnchorHold wrote (output/<name>).")
        import numpy as np
        with open(head) as f:
            self.header = json.load(f)
        h = self.header
        data = np.fromfile(os.path.join(folder, "depth_grid.bin"), "<f4")
        if data.size != h["rows"] * h["cols"]:
            raise ValueError(folder + ": depth_grid.bin does not match its header")
        self.depth_m = data.reshape(h["rows"], h["cols"]).astype(float)
        self.folder = folder
        self.name = os.path.basename(os.path.normpath(folder))
        self.masked = 0

    # cell (r, c) centre, lon/lat
    def lonlat(self, r, c):
        h = self.header
        return h["lonMin"] + c * h["dLon"], h["latMin"] + r * h["dLat"]

    def cell_size_ft(self):
        h = self.header
        m_lat = h["dLat"] * 111320.0
        m_lon = h["dLon"] * 111320.0 * math.cos(math.radians(h["latMin"]))
        return m_lon * FT_PER_M, m_lat * FT_PER_M

    def mask_to_track(self, trust_within_ft: float) -> int:
        """Blank cells farther than trust_within_ft from the survey track."""
        import numpy as np
        from scipy import ndimage
        if not self.folder:
            return 0                    # built from soundings, already trimmed
        path = os.path.join(self.folder, "track.geojson")
        if trust_within_ft <= 0 or not os.path.exists(path):
            return 0
        with open(path) as f:
            features = json.load(f).get("features", [])
        h = self.header
        rows, cols = self.depth_m.shape
        on_track = np.zeros((rows, cols), bool)
        for feat in features:
            g = feat.get("geometry") or {}
            parts = ([g["coordinates"]] if g.get("type") == "LineString" else
                     g["coordinates"] if g.get("type") == "MultiLineString" else [])
            for part in parts:
                for (lon0, lat0), (lon1, lat1) in zip(part[:-1], part[1:]):
                    n = 2 + int(max(abs(lon1 - lon0) / h["dLon"], abs(lat1 - lat0) / h["dLat"]) * 3)
                    c = np.round((np.linspace(lon0, lon1, n) - h["lonMin"]) / h["dLon"]).astype(int)
                    r = np.round((np.linspace(lat0, lat1, n) - h["latMin"]) / h["dLat"]).astype(int)
                    ok = (r >= 0) & (r < rows) & (c >= 0) & (c < cols)
                    on_track[r[ok], c[ok]] = True
        if not on_track.any():
            return 0
        fx, fy = self.cell_size_ft()
        dist = ndimage.distance_transform_edt(~on_track, sampling=(fy, fx))
        drop = (dist > trust_within_ft) & np.isfinite(self.depth_m)
        self.depth_m[drop] = np.nan
        self.masked = int(drop.sum())
        return self.masked

    def polygons(self, mask, frame):
        """Shapely polygons (local feet) around the True cells of `mask`."""
        import numpy as np
        from functools import reduce
        from shapely.geometry import Polygon
        from skimage import measure

        padded = np.pad(mask.astype(float), 1)
        rings = []
        for contour in measure.find_contours(padded, 0.5):
            pts = [frame.to_ft(*self.lonlat(r - 1, c - 1)) for r, c in contour]
            if len(pts) >= 4:
                ring = Polygon(pts)
                if not ring.is_valid:
                    ring = ring.buffer(0)
                if not ring.is_empty:
                    rings.append(ring)
        if not rings:
            return []
        # even-odd: a ring inside another is a hole in it
        shape = reduce(lambda a, b: a.symmetric_difference(b), rings)
        return list(shape.geoms) if shape.geom_type == "MultiPolygon" else [shape]


def grid_from_soundings(lons, lats, depths_m, name: str, trust_within_ft: float = 25.0,
                        cell_m: float = 1.0, smooth_pings: int = 9) -> "DepthGrid":
    """
    A depth grid from point soundings, in AnchorHold's format.

    Soundings are run through a rolling median over `smooth_pings` first: a
    sounder that loses the bottom for a ping reads 0.2 m or 270 m, and one
    ping of 270 m in a cell of its own would chart deep water that is not
    there. Each cell keeps the shallowest sounding in it; empty cells within
    `trust_within_ft` take the nearest sounding, the rest stay unsounded.
    """
    import numpy as np
    from scipy import ndimage

    lons = np.asarray(lons, float)
    lats = np.asarray(lats, float)
    depths = np.asarray(depths_m, float)
    if smooth_pings > 1 and depths.size >= smooth_pings:
        depths = ndimage.median_filter(depths, size=smooth_pings, mode="nearest")
    lat0 = float(lats.mean())
    d_lat = cell_m / 111320.0
    d_lon = cell_m / (111320.0 * math.cos(math.radians(lat0)))
    pad = trust_within_ft / FT_PER_M
    lon_min = float(lons.min()) - pad / (111320.0 * math.cos(math.radians(lat0)))
    lat_min = float(lats.min()) - pad / 111320.0
    cols = int((lons.max() - lon_min) / d_lon + pad / cell_m) + 2
    rows = int((lats.max() - lat_min) / d_lat + pad / cell_m) + 2
    if rows * cols > 25_000_000:
        raise ValueError("%s covers too much water for %.1f m cells" % (name, cell_m))
    r = np.round((lats - lat_min) / d_lat).astype(int)
    c = np.round((lons - lon_min) / d_lon).astype(int)
    grid = np.full((rows, cols), np.inf)
    np.minimum.at(grid, (r, c), depths)
    have = np.isfinite(grid)
    dist, (ri, ci) = ndimage.distance_transform_edt(~have, sampling=cell_m, return_indices=True)
    grid = grid[ri, ci]
    grid[dist * FT_PER_M > trust_within_ft] = np.nan
    header = {"lonMin": lon_min, "latMin": lat_min, "dLon": d_lon, "dLat": d_lat,
              "cols": cols, "rows": rows, "nodata": "nan"}
    return DepthGrid.from_array(header, grid, name)


def load_source(path: str, trust_within_ft: float = 25.0):
    """
    A DepthGrid from whatever was picked: an AnchorHold chart (its folder or
    its depth_grid.json) or a Humminbird recording (its .DAT or its folder).
    Returns (grid, note).
    """
    from planner import humminbird

    path = os.path.abspath(path)
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    base = os.path.basename(path).lower()
    if base == "depth_grid.json" or (os.path.isdir(path)
                                     and os.path.exists(os.path.join(path, "depth_grid.json"))):
        grid = DepthGrid(folder)
        masked = grid.mask_to_track(trust_within_ft)
        return grid, ("%d interpolated cell(s) far from the track ignored" % masked
                      if masked else "")
    if base.endswith(".dat") or (os.path.isdir(path) and any(
            f.upper().endswith(".SON") for f in os.listdir(path))):
        lons, lats, depths, info = humminbird.soundings(path)
        grid = grid_from_soundings(lons, lats, depths, info["name"], trust_within_ft)
        dropped = []
        if info["stale"]:
            dropped.append("%d without a fix" % info["stale"])
        if info["no_bottom"]:
            dropped.append("%d without a bottom" % info["no_bottom"])
        return grid, ("%s, %d pings%s" % (info["beam"], info["used"],
                                          " (" + ", ".join(dropped) + " dropped)" if dropped else ""))
    raise ValueError(os.path.basename(path) + " is neither an AnchorHold depth grid"
                     " (depth_grid.json) nor a Humminbird recording (.DAT).")


def shallow_no_go(sources, frame, water=None, shallower_than_ft: float = 3.0,
                  trust_within_ft: float = 25.0, min_area_ft2: float = 50.0):
    """
    No-go areas for every stretch charted shallower than `shallower_than_ft`.

    `sources` are paths (chart folders, depth_grid.json, Humminbird .DAT) or
    DepthGrids. Returns (zones, grids, notes): zones as {name, kind, geom,
    min_depth_ft} ready for the no-go list, the grids (kept for the boat
    export), and a line of text per source for the status bar.
    """
    import numpy as np
    from scipy import ndimage
    from shapely.ops import unary_union

    zones, grids, notes = [], [], []
    for source in sources:
        if isinstance(source, DepthGrid):
            grid, extra = source, ""
        else:
            grid, extra = load_source(source, trust_within_ft)
        grids.append(grid)
        depth_ft = grid.depth_m * FT_PER_M
        shallow = np.isfinite(depth_ft) & (depth_ft < shallower_than_ft)
        labels, count = ndimage.label(shallow)
        found = 0
        for k in range(1, count + 1):
            cells = labels == k
            for part in grid.polygons(cells, frame):
                if water is not None:
                    part = part.intersection(water)
                if part.is_empty or part.area < min_area_ft2:
                    continue
                zones.append({"kind": "shallow", "geom": part,
                              "min_depth_ft": float(np.nanmin(depth_ft[cells])),
                              "source": grid.name})
                found += 1
        valid = depth_ft[np.isfinite(depth_ft)]
        notes.append("%s: %d shallow area(s) under %.1f ft; charted %.1f-%.1f ft%s" % (
            grid.name, found, shallower_than_ft,
            valid.min() if valid.size else 0, valid.max() if valid.size else 0,
            (", " + extra) if extra else ""))

    # grids that overlap find the same shoal twice: merge touching zones
    merged = []
    for zone in sorted(zones, key=lambda z: z["min_depth_ft"]):
        for m in merged:
            if m["geom"].intersects(zone["geom"]):
                m["geom"] = unary_union([m["geom"], zone["geom"]])
                m["min_depth_ft"] = min(m["min_depth_ft"], zone["min_depth_ft"])
                break
        else:
            merged.append(dict(zone))
    for i, zone in enumerate(merged, start=1):
        zone["name"] = "shallow %d (%.1f ft)" % (i, zone["min_depth_ft"])
    return merged, grids, notes


def write_boat_chart(folder: str, grids) -> str:
    """
    The chart the boat reads from its SD card (APM/scripts/chart/).

    Same AnchorHold format, so the onboard script needs no second parser. One
    grid is copied as masked; several are resampled onto one grid at the finest
    of their cell sizes, keeping the shallowest charted depth wherever they
    overlap - disagreeing surveys are resolved toward caution.
    """
    import numpy as np

    os.makedirs(folder, exist_ok=True)
    if not grids:
        raise ValueError("no depth grids loaded")
    if len(grids) == 1:
        header, depth = dict(grids[0].header), grids[0].depth_m
    else:
        d_lon = min(g.header["dLon"] for g in grids)
        d_lat = min(g.header["dLat"] for g in grids)
        west = min(g.header["lonMin"] for g in grids)
        south = min(g.header["latMin"] for g in grids)
        east = max(g.header["lonMin"] + (g.header["cols"] - 1) * g.header["dLon"] for g in grids)
        north = max(g.header["latMin"] + (g.header["rows"] - 1) * g.header["dLat"] for g in grids)
        cols = int(round((east - west) / d_lon)) + 1
        rows = int(round((north - south) / d_lat)) + 1
        lon = west + np.arange(cols) * d_lon
        lat = south + np.arange(rows) * d_lat
        depth = np.full((rows, cols), np.nan)
        for g in grids:
            h = g.header
            c = np.round((lon - h["lonMin"]) / h["dLon"]).astype(int)
            r = np.round((lat - h["latMin"]) / h["dLat"]).astype(int)
            ci = (c >= 0) & (c < h["cols"])
            ri = (r >= 0) & (r < h["rows"])
            sample = np.full((rows, cols), np.nan)
            sample[np.ix_(ri, ci)] = g.depth_m[np.ix_(r[ri], c[ci])]
            depth = np.fmin(depth, sample)
        header = {"lonMin": west, "latMin": south, "dLon": d_lon, "dLat": d_lat,
                  "cols": cols, "rows": rows, "nodata": "nan"}
    with open(os.path.join(folder, "depth_grid.json"), "w") as f:
        json.dump(header, f)
    np.asarray(depth, dtype="<f4").tofile(os.path.join(folder, "depth_grid.bin"))
    return folder
