#!/usr/bin/env python3
"""
Build the lightweight preview a survey shows before its charts are downloaded:
where it was surveyed, and where the boat went.

    boundary.geojson   outline of the area the survey actually covers
    track.geojson      the vessel's trackline through it

Both are a few kilobytes, so they ship inside the app rather than in the
on-demand pack. That way an un-downloaded survey is still worth looking at -
you can see whether its coverage includes the water you care about before
committing to 70 MB of tiles.

The outline comes from the depth grid, not a hull around the soundings: a hull
would claim coverage across water the boat never ran, which is exactly the
mistake that matters here.

    python pipeline/build_preview.py output/creve-coeur-lake2
    python pipeline/build_preview.py output/creve-coeur-lake2 --csv path/to/depth.csv
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np


def _rdp(points, tolerance):
    """
    Ramer-Douglas-Peucker, iteratively so a long ring cannot blow the stack.

    Written out rather than pulled from shapely: the pipeline's interpreter has
    rasterio and numpy but not shapely, and this is the only geometry it needs.
    """
    if len(points) < 3:
        return points
    keep = np.zeros(len(points), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        start, end = points[first], points[last]
        segment = end - start
        length = np.hypot(*segment)
        chunk = points[first + 1:last]
        if length == 0:
            distances = np.hypot(*(chunk - start).T)
        else:
            # Perpendicular distance from each point to the start-end line.
            # Written out rather than np.cross, which deprecated 2-D vectors.
            offset = chunk - start
            distances = np.abs(segment[0] * offset[:, 1] - segment[1] * offset[:, 0]) / length
        index = int(np.argmax(distances))
        if distances[index] > tolerance:
            split = first + 1 + index
            keep[split] = True
            stack.append((first, split))
            stack.append((split, last))
    return points[keep]


def _ring_area(ring):
    """Shoelace area, for throwing away specks."""
    x, y = ring[:, 0], ring[:, 1]
    return abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1))) / 2.0


def coverage_polygons(grid_json: str, grid_bin: str, target_cells: int = 256):
    """
    Outline of the covered cells in a depth grid, in WGS84.

    The grid is float32 with NaN outside coverage; polygonising the "has data"
    mask gives the true footprint, holes and all. The mask is block-reduced
    first and the rings simplified after, which is what keeps a 672 x 852 grid
    down to a few kilobytes of outline.
    """
    from rasterio.features import shapes
    from rasterio.transform import from_bounds

    with open(grid_json, encoding="utf-8") as fh:
        head = json.load(fh)
    values = np.fromfile(grid_bin, dtype=np.float32).reshape(head["rows"], head["cols"])

    # The grid runs south-to-north; rasterio wants row 0 at the north.
    covered = (~np.isnan(values))[::-1]

    # Block-reduce: a block counts as covered if any cell in it is.
    block = max(1, int(round(max(head["cols"], head["rows"]) / target_cells)))
    if block > 1:
        rows = (covered.shape[0] // block) * block
        cols = (covered.shape[1] // block) * block
        covered = covered[:rows, :cols].reshape(
            rows // block, block, cols // block, block).any(axis=(1, 3))

    west = head["lonMin"]
    south = head["latMin"]
    east = west + head["dLon"] * head["cols"]
    north = south + head["dLat"] * head["rows"]
    transform = from_bounds(west, south, east, north,
                            covered.shape[1], covered.shape[0])

    cell_lon = (east - west) / covered.shape[1]
    cell_lat = (north - south) / covered.shape[0]
    tolerance = max(cell_lon, cell_lat) * 0.75
    smallest = (max(cell_lon, cell_lat) ** 2) * 6      # a few blocks, not a speck

    mask = covered.astype(np.uint8)
    polygons = []
    for geom, _value in shapes(mask, mask=covered, transform=transform):
        rings = []
        for index, ring in enumerate(geom["coordinates"]):
            points = _rdp(np.asarray(ring, dtype=float), tolerance)
            if len(points) < 4:
                continue
            if index == 0 and _ring_area(points) < smallest:
                rings = []
                break                                  # outer ring too small: drop it
            rings.append([[round(x, 6), round(y, 6)] for x, y in points])
        if rings:
            polygons.append({"type": "Polygon", "coordinates": rings})
    return polygons, (west, south, east, north)


def track_line(csv_path: str, max_points: int = 800):
    """The vessel's track from the sounding positions, thinned for size."""
    import pandas as pd

    frame = pd.read_csv(csv_path, usecols=["lon", "lat"]).dropna()
    if frame.empty:
        return None
    step = max(1, len(frame) // max_points)
    thinned = frame.iloc[::step]
    return [[round(float(lon), 6), round(float(lat), 6)]
            for lon, lat in zip(thinned["lon"], thinned["lat"])]


def build(out_dir: str, csv_path: str = "", log=print) -> dict:
    """Write boundary.geojson and track.geojson into [out_dir]; returns what was made."""
    made = {}

    grid_json = os.path.join(out_dir, "depth_grid.json")
    grid_bin = os.path.join(out_dir, "depth_grid.bin")
    if os.path.isfile(grid_json) and os.path.isfile(grid_bin):
        polygons, bounds = coverage_polygons(grid_json, grid_bin)
        path = os.path.join(out_dir, "boundary.geojson")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"type": "FeatureCollection",
                       "features": [{"type": "Feature", "properties": {}, "geometry": g}
                                    for g in polygons]}, fh)
        made["boundary"] = path
        log(f"      Saved: {path}  ({len(polygons)} polygon(s), "
            f"{os.path.getsize(path) / 1000:.0f} kB)")
    else:
        log("      (no depth grid - skipping the coverage outline)")

    if csv_path and os.path.isfile(csv_path):
        points = track_line(csv_path)
        if points:
            path = os.path.join(out_dir, "track.geojson")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"type": "FeatureCollection",
                           "features": [{"type": "Feature", "properties": {},
                                         "geometry": {"type": "LineString",
                                                      "coordinates": points}}]}, fh)
            made["track"] = path
            log(f"      Saved: {path}  ({len(points)} points, "
                f"{os.path.getsize(path) / 1000:.0f} kB)")
    else:
        log("      (no depth CSV - skipping the trackline)")

    return made


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir", help="a survey's output folder")
    ap.add_argument("--csv", default="", help="depth CSV for the trackline")
    args = ap.parse_args(argv)

    csv_path = args.csv
    if not csv_path:
        # A recording decoded in place leaves its CSV under pingmapper/.
        import glob
        candidates = sorted(glob.glob(os.path.join(args.out_dir, "pingmapper", "*_depth.csv")))
        csv_path = candidates[0] if candidates else ""

    print(f"Preview for {args.out_dir}")
    made = build(args.out_dir, csv_path)
    if not made:
        raise SystemExit("Nothing built - is that a survey output folder?")


if __name__ == "__main__":
    main()
