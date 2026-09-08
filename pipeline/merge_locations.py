#!/usr/bin/env python3
"""
Merge finished surveys into one, so their charts show together under one pin.

This is the cheap sibling of "combine". Combine goes back to the raw soundings
and mosaics and re-derives a single chart from them - better cartography, but it
needs the source data and takes minutes. Merge works on what has already been
built: it stitches the MBTiles, the contour and shallow-band GeoJSON, and the
depth and substrate grids of two or more surveys into one set of assets. Nothing
is re-tiled, so it takes seconds, and each survey keeps the resolution it was
tiled at.

The result is an ordinary survey as far as the app is concerned - one entry, one
pin, one set of layers - so no app change is needed to view several surveys at
once.

    python pipeline/merge_locations.py --name "creve coeur" creve-coeur-lake creve-coeur-lake2
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import sqlite3
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import workspace                   # neutral home for builds
OUTPUT_ROOT = workspace.output_dir()

TILE_LAYERS = ("bathymetry", "sonar", "substrate", "rock")
VECTOR_FILES = ("contours.geojson", "shallow_bands.geojson")
GRIDS = (("depth_grid", "float32"), ("substrate_grid", "uint8"))


# ── tiles ───────────────────────────────────────────────────────────────────

def _tile_bounds(con) -> tuple | None:
    """WGS84 bounds of an MBTiles, worked out from its own tile coordinates."""
    row = con.execute("SELECT MAX(zoom_level) FROM tiles").fetchone()
    if not row or row[0] is None:
        return None
    z = row[0]
    x0, x1, y0, y1 = con.execute(
        "SELECT MIN(tile_column), MAX(tile_column), MIN(tile_row), MAX(tile_row) "
        "FROM tiles WHERE zoom_level=?", (z,)).fetchone()
    n = 1 << z

    def lon(x):
        return x / n * 360.0 - 180.0

    def lat(y):                      # y counts from the south in MBTiles
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (n - y) / n))))

    return (lon(x0), lat(y1 + 1), lon(x1 + 1), lat(y0))


def _composite(older: bytes, newer: bytes) -> bytes:
    """Lay one tile over another, so overlapping surveys blend at the seam."""
    try:
        from PIL import Image
    except ImportError:
        return newer
    try:
        base = Image.open(io.BytesIO(older)).convert("RGBA")
        top = Image.open(io.BytesIO(newer)).convert("RGBA")
        if base.size != top.size:
            return newer
        base.alpha_composite(top)
        buffer = io.BytesIO()
        base.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return newer                 # a tile we cannot read is not worth failing over


def merge_mbtiles(sources: list, out_path: str, layer: str, log=print) -> str | None:
    """
    Copy every source's tiles into one MBTiles. Where two surveys cover the same
    tile the later one is composited over the earlier, which matters at the
    overlap: last-one-wins would punch a hole in the survey underneath.
    """
    sources = [p for p in sources if p and os.path.isfile(p)]
    if not sources:
        return None

    if os.path.exists(out_path):
        os.remove(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    out = sqlite3.connect(out_path)
    out.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    out.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                "tile_row INTEGER, tile_data BLOB, "
                "PRIMARY KEY (zoom_level, tile_column, tile_row))")

    bounds = None
    copied = overlapped = 0
    for path in sources:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for z, x, y, blob in con.execute(
                    "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"):
                existing = out.execute(
                    "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? "
                    "AND tile_row=?", (z, x, y)).fetchone()
                if existing:
                    blob = _composite(bytes(existing[0]), bytes(blob))
                    overlapped += 1
                out.execute("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)",
                            (z, x, y, sqlite3.Binary(blob)))
                copied += 1
            box = _tile_bounds(con)
        finally:
            con.close()
        if box:
            bounds = box if bounds is None else (
                min(bounds[0], box[0]), min(bounds[1], box[1]),
                max(bounds[2], box[2]), max(bounds[3], box[3]))

    zooms = out.execute("SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles").fetchone()
    meta = {"name": layer, "type": "overlay", "version": "1", "format": "png",
            "minzoom": str(zooms[0]), "maxzoom": str(zooms[1])}
    if bounds:
        meta["bounds"] = ",".join(f"{v:.6f}" for v in bounds)
        meta["center"] = (f"{(bounds[0] + bounds[2]) / 2:.6f},"
                          f"{(bounds[1] + bounds[3]) / 2:.6f},{zooms[1]}")
    out.executemany("INSERT INTO metadata VALUES (?,?)", list(meta.items()))
    out.commit()
    out.close()

    note = f", {overlapped} composited where they overlap" if overlapped else ""
    log(f"  {layer}: {copied} tiles from {len(sources)} surveys{note}")
    return out_path


# ── vector overlays ─────────────────────────────────────────────────────────

def merge_geojson(sources: list, out_path: str, log=print) -> str | None:
    """Concatenate FeatureCollections; each survey's lines keep their own values."""
    sources = [p for p in sources if p and os.path.isfile(p)]
    if not sources:
        return None
    features = []
    for path in sources:
        with open(path, encoding="utf-8") as fh:
            features += json.load(fh).get("features", [])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh)
    log(f"  {os.path.basename(out_path)}: {len(features)} features")
    return out_path


# ── query grids ─────────────────────────────────────────────────────────────

def merge_grids(sources: list, out_base: str, dtype: str, log=print) -> str | None:
    """
    Lay several query grids into one covering all of them.

    The union is built at the finest cell size in play and filled by nearest
    lookup, so a coarse survey does not blur a fine one and tap-to-query works
    anywhere either survey reached. Cells no survey covers stay nodata.
    """
    pairs = [(f"{p}.json", f"{p}.bin") for p in sources]
    pairs = [(j, b) for j, b in pairs if os.path.isfile(j) and os.path.isfile(b)]
    if not pairs:
        return None

    heads = []
    for json_path, bin_path in pairs:
        with open(json_path, encoding="utf-8") as fh:
            head = json.load(fh)
        values = np.fromfile(bin_path, dtype=np.float32 if dtype == "float32" else np.uint8)
        head["values"] = values.reshape(head["rows"], head["cols"])
        heads.append(head)

    d_lon = min(h["dLon"] for h in heads)
    d_lat = min(h["dLat"] for h in heads)
    lon_min = min(h["lonMin"] for h in heads)
    lat_min = min(h["latMin"] for h in heads)
    lon_max = max(h["lonMin"] + h["dLon"] * (h["cols"] - 1) for h in heads)
    lat_max = max(h["latMin"] + h["dLat"] * (h["rows"] - 1) for h in heads)

    cols = int(round((lon_max - lon_min) / d_lon)) + 1
    rows = int(round((lat_max - lat_min) / d_lat)) + 1
    if cols * rows > 80_000_000:
        raise RuntimeError(
            f"Merged grid would be {cols} x {rows} cells - the surveys are too far "
            f"apart to share one grid")

    nodata = np.nan if dtype == "float32" else 255
    merged = np.full((rows, cols), nodata,
                     dtype=np.float32 if dtype == "float32" else np.uint8)

    for head in heads:
        src = head["values"]
        # Where each of this survey's cells lands in the union.
        col0 = int(round((head["lonMin"] - lon_min) / d_lon))
        row0 = int(round((head["latMin"] - lat_min) / d_lat))
        src_cols = (np.arange(head["cols"]) * head["dLon"] / d_lon).round().astype(int) + col0
        src_rows = (np.arange(head["rows"]) * head["dLat"] / d_lat).round().astype(int) + row0
        src_cols = np.clip(src_cols, 0, cols - 1)
        src_rows = np.clip(src_rows, 0, rows - 1)

        block = src[np.arange(head["rows"])[:, None], np.arange(head["cols"])[None, :]]
        target = merged[src_rows[:, None], src_cols[None, :]]
        keep = ~np.isnan(block) if dtype == "float32" else block != 255
        # Only write where this survey has data, so it cannot erase its neighbour.
        merged[src_rows[:, None], src_cols[None, :]] = np.where(keep, block, target)

    merged.tofile(f"{out_base}.bin")
    with open(f"{out_base}.json", "w", encoding="utf-8") as fh:
        json.dump({"lonMin": lon_min, "latMin": lat_min, "dLon": d_lon, "dLat": d_lat,
                   "cols": cols, "rows": rows,
                   "nodata": "nan" if dtype == "float32" else 255}, fh)
    covered = int(np.count_nonzero(~np.isnan(merged) if dtype == "float32"
                                   else merged != 255))
    log(f"  {os.path.basename(out_base)}: {cols} x {rows} cells, {covered:,} with data")
    return out_base


# ── the whole survey ────────────────────────────────────────────────────────

def survey_files(candidates: list) -> str | None:
    """
    The first of [candidates] that actually holds a survey's built charts.

    Callers pass every folder worth trying, most specific first - a renamed
    survey keeps its pipeline output under the old id, so guessing from the
    current one finds nothing.
    """
    for folder in candidates:
        if folder and os.path.isdir(folder) and any(
                os.path.isfile(os.path.join(folder, f"{layer}.mbtiles"))
                for layer in TILE_LAYERS):
            return folder
    return None


def merge_surveys(folders: list, name: str, out_dir: str, log=print) -> dict:
    """
    Merge the built assets in [folders] into [out_dir]; returns what was made,
    keyed the way the catalog names things.
    """
    folders = [f for f in folders if f]
    if len(folders) < 2:
        raise RuntimeError("Need at least two surveys with built charts to merge")
    for folder in folders:
        log(f"  using {folder}")

    os.makedirs(out_dir, exist_ok=True)
    made = {"tiles": {}, "grids": {}, "contours": None, "shallowBands": None}

    log(f"\nMerging {len(folders)} surveys into {name}:")
    for layer in TILE_LAYERS:
        sources = [os.path.join(f, f"{layer}.mbtiles") for f in folders]
        if merge_mbtiles(sources, os.path.join(out_dir, f"{layer}.mbtiles"), layer, log):
            made["tiles"][layer] = f"{layer}.mbtiles"

    for name_json in VECTOR_FILES:
        sources = [os.path.join(f, name_json) for f in folders]
        if merge_geojson(sources, os.path.join(out_dir, name_json), log):
            key = "contours" if name_json.startswith("contours") else "shallowBands"
            made[key] = name_json

    for base, dtype in GRIDS:
        sources = [os.path.join(f, base) for f in folders]
        if merge_grids(sources, os.path.join(out_dir, base), dtype, log):
            made["grids"]["depth" if base.startswith("depth") else "substrate"] = base

    # A legend from any member describes the merged map well enough; they share
    # the class scheme, and the depth key is redrawn from the data anyway.
    for legend in ("depth_legend.png", "substrate_legend.png", "rock_legend.png"):
        for folder in folders:
            source = os.path.join(folder, legend)
            if os.path.isfile(source):
                import shutil
                shutil.copy2(source, os.path.join(out_dir, legend))
                break

    log("")
    return made


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="+", help="survey ids to merge")
    ap.add_argument("--name", required=True, help="name for the merged survey")
    ap.add_argument("--out-dir", default="", help="defaults to output/<slug of name>")
    args = ap.parse_args(argv)

    slug = args.name.lower().replace(" ", "-")
    out_dir = args.out_dir or os.path.join(OUTPUT_ROOT, slug)
    folders = []
    for entry_id in args.ids:
        found = survey_files([os.path.join(OUTPUT_ROOT, entry_id)])
        if not found:
            raise SystemExit(f"No built charts under output/{entry_id} - build it first, "
                             f"or merge from the location GUI, which also looks in the "
                             f"installed asset packs.")
        folders.append(found)
    made = merge_surveys(folders, args.name, out_dir)
    print(json.dumps(made, indent=2))
    print(f"\nMerged into {out_dir}")


if __name__ == "__main__":
    sys.exit(main())
