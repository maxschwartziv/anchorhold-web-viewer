"""
Shore access, and what can be seen from it.

Two different questions get confused here, so they are kept apart:

  Access    Where a person can physically reach the water and put a boat in.
            Nothing in a shoreline polygon knows this - it is private land,
            brush, a steep bank, a fence. Only a person can mark it.
  Sight     Whether the boat is visible from a given point on the bank. That
            one is geometry, and land occludes it, so it can be computed.

A viewshed says no land blocks the line. It does not say the boat is big enough
to see at that distance, in that light, against that background. Keep the range
limit as a setting and let the operator judge it.
"""

from __future__ import annotations

import math

import numpy as np


class WaterRaster:
    """The lake on a grid, which is what visibility and routing both need."""

    def __init__(self, poly, cell_ft: float = 10.0):
        """
        `poly` may be a Polygon or a MultiPolygon.

        Several parts is the normal case, not an edge case: clipping the water
        to a region of interest splits it wherever the region crosses a neck,
        and the setback splits it wherever the water narrows. Assuming one
        exterior ring crashed the moment a region was drawn across a bend.
        """
        from matplotlib.path import Path
        from shapely.geometry import MultiPolygon

        self.cell = cell_ft
        minx, miny, maxx, maxy = poly.bounds
        self.minx, self.miny = minx, miny
        self.width = int((maxx - minx) / cell_ft) + 2
        self.height = int((maxy - miny) / cell_ft) + 2
        rows, cols = np.mgrid[0:self.height, 0:self.width]
        xs = (minx + (cols + 0.5) * cell_ft).ravel()
        ys = (miny + (rows + 0.5) * cell_ft).ravel()
        pts = np.column_stack((xs, ys))

        mask = np.zeros((self.height, self.width), dtype=bool)
        parts = poly.geoms if isinstance(poly, MultiPolygon) else [poly]
        for part in parts:
            if part.is_empty:
                continue
            inside = Path(np.asarray(part.exterior.coords)).contains_points(pts)
            mask |= inside.reshape(self.height, self.width)
            for hole in part.interiors:
                cut = Path(np.asarray(hole.coords)).contains_points(pts)
                mask &= ~cut.reshape(self.height, self.width)
        self.water = mask

    def rc(self, x: float, y: float):
        r = int((y - self.miny) / self.cell)
        c = int((x - self.minx) / self.cell)
        return (min(max(r, 0), self.height - 1), min(max(c, 0), self.width - 1))

    def xy(self, r: int, c: int):
        return (self.minx + (c + 0.5) * self.cell, self.miny + (r + 0.5) * self.cell)

    def viewshed(self, point, max_range_ft: float = 0.0, rays: int = 720):
        """
        Water visible from `point`, by marching rays until land stops them.

        max_range_ft of 0 means no distance limit - only land blocks the view.
        """
        r0, c0 = self.rc(*point)
        visible = np.zeros_like(self.water)
        limit = (int(max_range_ft / self.cell) if max_range_ft > 0
                 else int(math.hypot(self.height, self.width)))
        for angle in np.linspace(0.0, 2 * math.pi, rays, endpoint=False):
            dr, dc = math.sin(angle), math.cos(angle)
            for step in range(1, limit):
                r = int(r0 + dr * step)
                c = int(c0 + dc * step)
                if not (0 <= r < self.height and 0 <= c < self.width):
                    break
                if not self.water[r, c]:
                    break
                visible[r, c] = True
        return visible


def suggest_stations(poly, raster: WaterRaster, navigable, count: int = 0,
                     max_range_ft: float = 0.0, spacing_ft: float = 300.0,
                     coverage_target: float = 0.99):
    """
    Candidate operator positions along the bank, chosen greedily.

    Each round picks the point that reveals the most water nobody can see yet.
    These are suggestions from geometry alone - every one still has to be
    checked against whether a person can actually stand there.
    """
    from shapely.geometry import LineString

    ring = LineString(poly.exterior.coords)
    n = max(4, int(ring.length / spacing_ft))
    candidates = [ring.interpolate(i / n, normalized=True).coords[0] for i in range(n)]
    views = [raster.viewshed(p, max_range_ft) for p in candidates]

    target = navigable.copy()
    covered = np.zeros_like(target)
    chosen = []
    while True:
        best, gain = None, 0
        for i, view in enumerate(views):
            if i in chosen:
                continue
            g = int((view & target & ~covered).sum())
            if g > gain:
                best, gain = i, g
        if best is None or gain < 100:
            break
        chosen.append(best)
        covered |= views[best]
        if count and len(chosen) >= count:
            break
        if target.sum() and (target & ~covered).sum() < target.sum() * (1 - coverage_target):
            break
    total = int(target.sum()) or 1
    return [{"xy": candidates[i], "view": views[i]} for i in chosen], \
           float((target & covered).sum()) / total


def route_between(raster: WaterRaster, a, b, navigable=None):
    """
    A path from a to b that stays on water.

    The straight hop between two lines will cut a headland the moment the lake
    has one. Least-cost path over the navigable cells will not.
    """
    from shapely.geometry import LineString
    from skimage.graph import route_through_array

    grid = navigable if navigable is not None else raster.water

    # Remembered per raster. Fitting a day to the hours is a binary search
    # that re-measures the same trip out over and over, and rebalancing
    # neighbouring days re-measures whole windows of them: one plan asked
    # for 10,986 routes and took 164 seconds, and they are not 10,986
    # different routes. Keyed to a tenth of a foot, which is far below any
    # distance that matters here.
    key = (round(a[0], 1), round(a[1], 1),
           round(b[0], 1), round(b[1], 1), id(grid))
    cache = getattr(raster, "_routes", None)
    if cache is None:
        cache = raster._routes = {}
    remembered = cache.get(key)
    if remembered is not None:
        return list(remembered)

    cost = np.where(grid, 1.0, 1e7)
    start = _nearest_true(grid, raster.rc(*a))
    end = _nearest_true(grid, raster.rc(*b))
    if start is None or end is None or start == end:
        return _remember(cache, key, [tuple(a), tuple(b)])
    path, _ = route_through_array(cost, start, end, fully_connected=True,
                                  geometric=True)
    coords = [raster.xy(r, c) for r, c in path]
    # The routed path is a staircase of cell centres and wants smoothing,
    # but simplify's tolerance is licence to leave the routed line - at two
    # cells, 24 ft of licence to chord across the headland the routing just
    # went round. So smooth as much as holds and no more: try a tolerance,
    # check the result is still on water, and halve it until it is.
    smoothed = coords
    tolerance = raster.cell * 2.0
    while tolerance >= raster.cell / 4.0:
        candidate = list(LineString(coords).simplify(tolerance).coords)
        if _stays_on(candidate, raster, grid):
            smoothed = candidate
            break
        tolerance /= 2.0
    # The path is built from cell centres, so it starts and ends up to
    # half a cell from where it was asked to. Pin both ends: otherwise the
    # caller silently gets an unrouted stub joining the gap.
    a, b = tuple(a), tuple(b)
    if smoothed[0] != a:
        smoothed.insert(0, a)
    if smoothed[-1] != b:
        smoothed.append(b)
    return _remember(cache, key, smoothed)


def _remember(cache, key, path):
    """Keep a route for next time, and hand back a copy.

    A copy because callers own what they are given and some of them append
    to it; the cache would otherwise grow a tail each time it is read.
    """
    if len(cache) < 40000:
        cache[key] = list(path)
    return list(path)


def _stays_on(points, raster, grid) -> bool:
    """Is every foot of this polyline on a navigable cell?

    Sampled at half a cell, so no strip of land narrower than six feet can
    slip between two samples.
    """
    for i in range(len(points) - 1):
        (x0, y0), (x1, y1) = points[i], points[i + 1]
        span = math.hypot(x1 - x0, y1 - y0)
        steps = max(2, int(span / (raster.cell * 0.5)) + 1)
        for k in range(steps + 1):
            t = k / steps
            r, c = raster.rc(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
            if not grid[r, c]:
                return False
    return True


def _nearest_true(grid, rc):
    r, c = rc
    if grid[r, c]:
        return (r, c)
    for k in range(1, 80):
        r0, r1 = max(0, r - k), min(grid.shape[0], r + k + 1)
        c0, c1 = max(0, c - k), min(grid.shape[1], c + k + 1)
        window = grid[r0:r1, c0:c1]
        if window.any():
            idx = np.argwhere(window)
            d = np.hypot(idx[:, 0] + r0 - r, idx[:, 1] + c0 - c)
            pick = idx[d.argmin()]
            return (int(pick[0] + r0), int(pick[1] + c0))
    return None
