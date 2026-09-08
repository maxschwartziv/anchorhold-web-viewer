#!/usr/bin/env python3
"""
Turn a BAG (Bathymetric Attributed Grid) into soundings this pipeline can build
a chart from.

A BAG is a finished hydrographic product - an HDF5 grid of elevation and
uncertainty with its datums recorded alongside - so most of the work a sonar
recording needs is already done. What is left is to get it into the pipeline's
own currency: a depth CSV of lon, lat, dep_m.

    python pipeline/bag_import.py H14003_MB_8m_MLLW_1of1.bag --out output/h14003
    python pipeline/bag_import.py survey.bag --out output/x --max-uncertainty 2
    python pipeline/bag_import.py survey.bag --out output/x --target-cells 600

Then build the chart as usual:

    python pipeline/process_data.py --csv output/h14003/bag_depth.csv \\
        --out-dir output/h14003

Three things decide whether the depths come out right, and this reads all three
off the file rather than assuming them:

  Sign      BAG stores elevation, positive up, so the seabed is negative - but
            the vertical CRS of a NOAA BAG declares AXIS["Depth",DOWN] while
            the samples are still negative. The axis is not to be trusted; the
            data's own sign is.
  Datum     A survey already reduced to MLLW (or any chart datum) must not be
            tide-corrected again by the app. The datum is read from the CRS and
            reported so the survey can be set to "no tide".
  Nodata    BAG marks empty cells 1000000.0, not NaN. Read it as data and the
            chart acquires a 1000 km trench.
"""

from __future__ import annotations

import argparse
import json
import math
import os

import numpy as np


def open_bag(path: str):
    """The BAG, with its bands identified by name rather than by position."""
    import rasterio
    src = rasterio.open(path)
    if src.driver != "BAG":
        src.close()
        raise SystemExit(f"{path} opened as {src.driver}, not BAG. Is GDAL built "
                         "with HDF5? The pipeline's own interpreter has the driver; "
                         "the PINGMapper conda environment does not.")
    if src.subdatasets:
        src.close()
        raise SystemExit(
            "This is a variable-resolution BAG - its refinement grids are "
            "subdatasets:\n  " + "\n  ".join(src.subdatasets[:4]) +
            "\nOpen the resolution you want with GDAL open options and pass that "
            "instead; the supergrid alone would throw away the detail you came for.")
    return src


def band_index(src, name: str, default: int):
    """Find a band by its description, falling back to position."""
    for i, description in enumerate(src.descriptions or (), start=1):
        if description and description.lower().startswith(name):
            return i
    return default


def vertical_datum(src) -> str:
    """The vertical datum named in the CRS, e.g. 'MLLW depth'."""
    wkt = src.crs.to_wkt() if src.crs else ""
    for key in ('VERT_CS["', 'VERTCRS["'):
        if key in wkt:
            return wkt.split(key, 1)[1].split('"', 1)[0]
    return ""


def warn_datum_conflict(path: str, datum: str, log=print) -> None:
    """
    Say so when the file name claims one datum and the CRS says another.

    Producers often leave a template vertical CRS in place, so a file called
    ..._MLLW_... can carry "Instantaneous Water Level". The difference is the
    whole tide: soundings taken at high water and never reduced read deeper
    than the water will be at low water. Nothing here can settle which is
    right - but nobody should find out later.
    """
    name = os.path.basename(path).upper()
    claimed = next((d for d in ("MLLW", "MHW", "LAT", "MSL", "CD") if d in name), "")
    if not claimed or not datum:
        return
    if claimed.lower() in datum.lower():
        return
    log(f"  !! the file name says {claimed} but the CRS says \"{datum}\"")
    log("     One of them is wrong. Depths not reduced to a chart datum read")
    log("     deeper than the water is at low tide. Check before trusting them.")


def depth_sign(values: np.ma.MaskedArray, log=print) -> float:
    """
    +1 if the file already stores depth (down), -1 if it stores elevation (up).

    Decided from the data, because the CRS cannot be trusted here: NOAA BAGs
    carry a vertical CRS that says the axis points DOWN while every sample is
    negative. A file whose values straddle zero is a survey crossing the
    shoreline, and the median still settles it.
    """
    median = float(np.ma.median(values))
    below = float((values < 0).sum())
    above = float((values > 0).sum())
    if median < 0:
        log(f"  values are negative (median {median:.1f} m): elevation, positive up "
            f"- flipping to depth")
        return -1.0
    log(f"  values are positive (median {median:.1f} m): already depth, positive down")
    if below > 0.05 * (below + above):
        log(f"  note: {below/(below+above)*100:.0f}% of cells are negative; if this "
            "survey crosses the shoreline those are land and will be dropped")
    return 1.0


def convert(path: str, out_dir: str, target_cells: int = 600,
            max_uncertainty: float = 0.0, log=print) -> dict:
    """Write a depth CSV from a BAG. Returns what was found, for the record."""
    from rasterio import warp

    src = open_bag(path)
    try:
        log(f"{os.path.basename(path)}")
        log(f"  {src.width} x {src.height} cells at {src.res[0]:g} m, "
            f"{src.count} band(s)")

        datum = vertical_datum(src)
        log(f"  vertical datum: {datum or 'not stated in the CRS'}")
        warn_datum_conflict(path, datum, log=log)

        # Decimate on the way in. A 1 m survey of a bay is 400 million cells;
        # reading it whole to throw most of it away costs gigabytes of memory
        # before the first sounding is written.
        step = max(1, int(round(max(src.width, src.height) / max(target_cells, 1))))
        shape = (int(np.ceil(src.height / step)), int(np.ceil(src.width / step)))
        elevation = src.read(band_index(src, "elev", 1), masked=True, out_shape=shape)
        if step > 1:
            log(f"  reading every {step}th cell -> {shape[1]} x {shape[0]} "
                f"(~{src.res[0]*step:.0f} m)")

        if max_uncertainty > 0 and src.count > 1:
            uncertainty = src.read(band_index(src, "uncert", 2), masked=True,
                                   out_shape=shape)
            too_rough = uncertainty.filled(np.inf) > max_uncertainty
            dropped = int((too_rough & ~elevation.mask).sum())
            elevation = np.ma.masked_where(too_rough, elevation)
            log(f"  uncertainty > {max_uncertainty} m: dropped {dropped} cells")

        sign = depth_sign(elevation, log=log)
        depth = elevation * sign

        rows, cols = np.nonzero(~np.ma.getmaskarray(depth))
        if rows.size == 0:
            raise SystemExit("Nothing left after masking - try a larger "
                             "--max-uncertainty.")

        # Cell centres, back at full-grid coordinates.
        xs, ys = src.xy(rows * step, cols * step)
        lons, lats = warp.transform(src.crs, "EPSG:4326", np.asarray(xs), np.asarray(ys))
        values = np.asarray(depth[rows, cols])

        keep = values > 0                     # land and datum-line cells are not soundings
        dropped_dry = int((~keep).sum())
        lons = np.asarray(lons)[keep]
        lats = np.asarray(lats)[keep]
        values = values[keep]
        if dropped_dry:
            log(f"  dropped {dropped_dry} cells at or above the datum (land)")

        os.makedirs(out_dir, exist_ok=True)
        csv_path = os.path.join(out_dir, "bag_depth.csv")
        # date and time are columns the pipeline requires. A gridded product has
        # no per-sounding time, so the BAG's own timestamp stands in for them.
        # It is when the grid was published, not when the water was sounded -
        # do not let it become a "surveyed on" claim downstream.
        stamp = (src.tags().get("BAG_DATETIME") or "").split("T")
        day = stamp[0] if stamp and stamp[0] else "1970-01-01"
        clock = stamp[1][:8] if len(stamp) > 1 else "00:00:00"
        with open(csv_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("lon,lat,dep_m,date,time\n")
            for lon, lat, value in zip(lons, lats, values):
                fh.write(f"{lon:.7f},{lat:.7f},{value:.2f},{day},{clock}\n")

        found = {
            "soundings": int(values.size),
            "depth_min": round(float(values.min()), 2),
            "depth_max": round(float(values.max()), 2),
            "lon": [round(float(lons.min()), 6), round(float(lons.max()), 6)],
            "lat": [round(float(lats.min()), 6), round(float(lats.max()), 6)],
            "datum": datum,
            "resolution_m": round(float(src.res[0]) * step, 1),
            "csv": csv_path,
            "bag_version": src.tags().get("BagVersion", ""),
            "bag_created": day,
        }
        log(f"  {found['soundings']} soundings, {found['depth_min']}-"
            f"{found['depth_max']} m")
        log(f"  wrote {csv_path}")
        with open(os.path.join(out_dir, "bag_import.json"), "w", encoding="utf-8") as fh:
            json.dump(found, fh, indent=2)
        return found
    finally:
        src.close()


def backscatter_to_grey(path: str, out_dir: str, low: float = 2.0, high: float = 98.0,
                        max_cells: int = 0, log=print) -> str:
    """
    Turn an acoustic backscatter GeoTIFF into the 8-bit greyscale the sonar
    layer expects, at the source's own resolution.

    A hydrographic survey publishes backscatter in decibels as float32. The
    pipeline's sonar path casts straight to uint8, which would clip every
    negative value to nothing and render the seabed black. Stretching between
    percentiles keeps the contrast that makes backscatter worth looking at,
    without letting one bright outlier flatten the rest.

    Resolution is the whole point of this layer, so nothing is thrown away:
    a 1 m mosaic of a bay is 400 million cells and will not fit in memory at
    float32, so it is converted a window at a time. max_cells > 0 decimates
    anyway, for a quick look.

    Nodata becomes 0, which the tiler already treats as transparent.
    """
    import rasterio
    from rasterio.windows import Window

    with rasterio.open(path) as src:
        log(f"{os.path.basename(path)}")
        log(f"  {src.width} x {src.height} at {src.res[0]:g} m, {src.dtypes[0]}")

        step = 1
        if max_cells > 0:
            step = max(1, int(round(max(src.width, src.height) / max_cells)))

        # Percentiles from a sample: reading 400 million cells to find two
        # numbers would cost more than the whole conversion.
        sample_step = max(1, int(round(max(src.width, src.height) / 2000)))
        sample = src.read(1, masked=True,
                          out_shape=(max(1, src.height // sample_step),
                                     max(1, src.width // sample_step)))
        valid = sample.compressed()
        if valid.size == 0:
            raise SystemExit("That backscatter raster is empty.")
        lo, hi = np.percentile(valid, [low, high])
        if hi <= lo:
            lo, hi = float(valid.min()), float(valid.max())
        log(f"  stretching {lo:.1f}..{hi:.1f} dB to 1..255 "
            f"(full range {valid.min():.1f}..{valid.max():.1f})")

        out_w = int(math.ceil(src.width / step))
        out_h = int(math.ceil(src.height / step))
        transform = src.transform if step == 1 else (
            src.transform * src.transform.scale(src.width / out_w, src.height / out_h))
        profile = dict(src.profile, dtype="uint8", count=1, nodata=0,
                       width=out_w, height=out_h, transform=transform,
                       compress="deflate", tiled=True, blockxsize=512, blockysize=512)
        if step > 1:
            log(f"  decimating to {out_w} x {out_h}")
        else:
            log(f"  keeping all {out_w} x {out_h} cells "
                f"({out_w*out_h/1e6:.0f} M) - converting a window at a time")

        os.makedirs(out_dir, exist_ok=True)
        utm_path = os.path.join(out_dir, "backscatter_grey_src.tif")
        rows_per_block = max(1, 4_000_000 // max(out_w, 1))
        with rasterio.open(utm_path, "w", **profile) as dst:
            for top in range(0, out_h, rows_per_block):
                height = min(rows_per_block, out_h - top)
                block = src.read(
                    1, masked=True,
                    window=Window(0, top * step, src.width,
                                  min(height * step, src.height - top * step)),
                    out_shape=(height, out_w))
                scaled = np.clip((block - lo) / (hi - lo), 0, 1) * 254.0 + 1.0
                grey = np.where(np.ma.getmaskarray(block), 0, scaled).astype(np.uint8)
                dst.write(grey, 1, window=Window(0, top, out_w, height))

    # The sonar tiler works in degrees. A hydrographic product arrives in UTM,
    # and handed one the tiler computes a pyramid from metres as though they
    # were degrees - it asked for 12.7 million tiles before this was caught.
    from rasterio.warp import calculate_default_transform, reproject, Resampling
    out_path = os.path.join(out_dir, "backscatter_grey.tif")
    target = "EPSG:4326"
    with rasterio.open(utm_path) as src:
        already_wgs84 = bool(src.crs) and str(src.crs).upper() == target
    if already_wgs84:
        # Outside the with-block on purpose: Windows will not rename a file
        # that is still open, and a source already in degrees needs no warp.
        os.replace(utm_path, out_path)
        log(f"  already in degrees; wrote {out_path}")
        return out_path

    with rasterio.open(utm_path) as src:
        log(f"  reprojecting {src.crs} -> {target} for the tiler")
        dst_transform, dst_w, dst_h = calculate_default_transform(
            src.crs, target, src.width, src.height, *src.bounds)
        profile = dict(src.profile, crs=target, transform=dst_transform,
                       width=dst_w, height=dst_h)
        with rasterio.open(out_path, "w", **profile) as dst:
            reproject(source=rasterio.band(src, 1), destination=rasterio.band(dst, 1),
                      src_transform=src.transform, src_crs=src.crs,
                      dst_transform=dst_transform, dst_crs=target,
                      src_nodata=0, dst_nodata=0, resampling=Resampling.bilinear)
        log(f"  {dst_w} x {dst_h} in degrees")
    os.remove(utm_path)
    log(f"  wrote {out_path}")
    log("")
    log("  Use it as the sonar layer:")
    log(f"    python pipeline/process_data.py --sonar {out_path} ...")
    return out_path


def advise(found: dict, out_dir: str, log=print) -> None:
    """What the numbers mean for the chart that comes next."""
    datum = (found.get("datum") or "").lower()
    reduced = any(k in datum for k in ("mllw", "lat", "chart", "lowest", "mean lower"))
    log("\nBefore you build the chart:")
    if reduced:
        log(f"  These depths are already reduced to {found['datum']}. Set the survey's")
        log("  tide to \"none\" - correcting again would read shallower than the water is.")
    else:
        log("  No chart datum stated in the CRS. Find out what these depths are")
        log("  referenced to before trusting them at a particular state of tide.")
    shallow = found["depth_min"]
    if shallow > 30:
        log(f"  Shallowest sounding is {shallow} m. This is not anchoring water;")
        log("  useful for testing the pipeline, not for a chart anyone would anchor on.")
    log("")
    csv = found["csv"]
    log(f"  python pipeline/process_data.py --csv {csv} --out-dir {out_dir} "
        f"--no-tide-reduction --no-track")
    log("  (--no-track because a grid has no vessel track to draw)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bag", nargs="?", default="", help="a .bag file")
    ap.add_argument("--max-backscatter-cells", type=int, default=0,
                    help="decimate the sonar mosaic to this many cells wide "
                         "(0 = keep the source resolution)")
    ap.add_argument("--backscatter", default="",
                    help="an acoustic backscatter GeoTIFF to prepare as the "
                         "sonar layer (dB float -> 8-bit grey)")
    ap.add_argument("--out", required=True, help="output folder for the CSV")
    ap.add_argument("--target-cells", type=int, default=600,
                    help="decimate to roughly this many cells on the long side")
    ap.add_argument("--max-uncertainty", type=float, default=0.0,
                    help="drop cells whose uncertainty exceeds this, in metres")
    args = ap.parse_args(argv)

    if args.backscatter:
        backscatter_to_grey(args.backscatter, args.out,
                            max_cells=args.max_backscatter_cells)
    if not args.bag:
        if not args.backscatter:
            raise SystemExit("Give a .bag file, or --backscatter, or both.")
        return

    found = convert(args.bag, args.out, target_cells=args.target_cells,
                    max_uncertainty=args.max_uncertainty)
    advise(found, args.out)


if __name__ == "__main__":
    main()
