#!/usr/bin/env python3
"""
Turn a GSF (Generic Sensor Format) multibeam file into soundings this pipeline
can build a chart from.

GSF is what a hydrographic survey records before it is gridded: every ping,
every beam, with the vessel's position and heading. That is closer to the sea
floor than any product made from it, and it is the same currency this pipeline
already speaks - lon, lat, dep_m.

    python pipeline/gsf_import.py survey.gsf --out output/survey
    python pipeline/gsf_import.py survey.gsf --out output/x --ping-step 4 --beam-step 2

Then build the chart as usual (a GSF has a real vessel track, so unlike a
gridded BAG the trackline is worth drawing):

    python pipeline/process_data.py --csv output/survey/gsf_depth.csv \\
        --out-dir output/survey --no-tide-reduction

Only what is needed is decoded: the ping header, the scale factors, and the
depth and across/along-track arrays. Every other subrecord is skipped by
length. That is deliberate - the vendor-specific imagery blocks are where
general-purpose readers fall over, and none of them are needed to place a
sounding on a chart.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct

import numpy as np

# Record identifiers, from the GSF specification.
SWATH_BATHYMETRY = 2

# Ping subrecords. Anything not listed is skipped by its own length.
SUBRECORD_SCALE_FACTORS = 100
SUBRECORD_DEPTH = 1
SUBRECORD_ACROSS_TRACK = 2
SUBRECORD_ALONG_TRACK = 3
SUBRECORD_AMPLITUDE = 7          # mean relative amplitude, in dB

# Which arrays hold negative values, and so are stored two's-complement.
# Backscatter is decibels below a reference and so always negative: read it
# unsigned and every beam comes back as about 65500 instead of about -28.
SIGNED_SUBRECORDS = {SUBRECORD_ACROSS_TRACK, SUBRECORD_ALONG_TRACK,
                     SUBRECORD_AMPLITUDE}

# Ping header: time, position, geometry, attitude. Big-endian throughout.
PING_HEADER = ">llll5hlH3h2Hlllh"
PING_HEADER_LEN = struct.calcsize(PING_HEADER)


def read_ping_header(raw: bytes) -> dict:
    """The fields of a swath bathymetry ping that place its beams on the earth."""
    s = struct.unpack(PING_HEADER, raw)
    return {
        "time": s[0],
        "lon": s[2] / 1e7,
        "lat": s[3] / 1e7,
        "beams": s[4],
        "heading": s[10] / 100.0,
    }


def read_scale_factors(fh) -> dict:
    """
    id -> (multiplier, offset).

    Arrays are stored as integers; the true value is raw / multiplier - offset.
    A ping without its own scale factors reuses the last ones seen, which is
    how GSF avoids repeating them on every ping.
    """
    count = struct.unpack(">l", fh.read(4))[0]
    factors = {}
    for _ in range(count):
        field, multiplier, offset = struct.unpack(">lll", fh.read(12))
        factors[(field & 0xFF000000) >> 24] = (multiplier, offset)
    return factors


def read_array(fh, size: int, beams: int, scale, dtype_size: int,
               signed: bool = False) -> np.ndarray:
    """
    One per-beam array, decoded through its scale factor.

    Signedness is not guessable from the record: depth is always positive and
    stored unsigned, while the across-track offset is negative to port and
    stored two's-complement. Read the latter as unsigned and every port beam
    lands about 655 m (65535 / 100) to starboard - far enough to put a swath
    on dry land, which is how this was caught.
    """
    raw = fh.read(size)
    kinds = {1: ">i1", 2: ">i2", 4: ">i4"} if signed else {1: ">u1", 2: ">u2", 4: ">u4"}
    kind = kinds.get(dtype_size)
    if kind is None or beams <= 0:
        return np.zeros(0)
    values = np.frombuffer(raw[: beams * dtype_size], dtype=kind).astype(np.float64)
    multiplier, offset = scale
    if not multiplier:
        return np.zeros(0)
    return values / multiplier - offset


def soundings(path: str, ping_step: int = 1, beam_step: int = 1, log=print):
    """
    Walk the file and yield (lon, lat, depth) for every beam kept.

    Beam positions come from the ping's own position plus its across/along
    track offsets, rotated by the vessel heading. That is the geometry GSF is
    built around: the arrays are in metres relative to the sonar, not on the
    earth, so a reader that ignores heading lays every swath out pointing north.
    """
    size = os.path.getsize(path)
    header_fmt = ">LL"
    header_len = struct.calcsize(header_fmt)
    scale = {}
    pings = kept = 0
    lon_out, lat_out, dep_out, time_out = [], [], [], []
    amp_out, dist_out, track = [], [], []
    first_time = last_time = None

    with open(path, "rb") as fh:
        while True:
            head = fh.read(header_len)
            if len(head) < header_len:
                break
            data_size, identifier = struct.unpack(header_fmt, head)
            has_checksum = identifier & 0x80000000
            record = identifier & 0x003FFFFF
            start = fh.tell()
            if has_checksum:
                fh.read(4)
            if data_size == 0 or start + data_size > size:
                break

            if record != SWATH_BATHYMETRY:
                fh.seek(start + data_size)
                continue

            pings += 1
            header = read_ping_header(fh.read(PING_HEADER_LEN))
            beams = header["beams"]
            depth = across = along = amplitude = None

            while fh.tell() < start + data_size:
                field = fh.read(4)
                if len(field) < 4:
                    break
                value = struct.unpack(">l", field)[0]
                sub_id = (value & 0xFF000000) >> 24
                sub_size = value & 0x00FFFFFF
                if sub_id == SUBRECORD_SCALE_FACTORS:
                    scale = read_scale_factors(fh)
                    continue
                if sub_id in (SUBRECORD_DEPTH, SUBRECORD_ACROSS_TRACK,
                              SUBRECORD_ALONG_TRACK,
                              SUBRECORD_AMPLITUDE) and beams and sub_id in scale:
                    per_beam = sub_size // beams
                    array = read_array(fh, sub_size, beams, scale[sub_id], per_beam,
                                       signed=sub_id in SIGNED_SUBRECORDS)
                    if sub_id == SUBRECORD_DEPTH:
                        depth = array
                    elif sub_id == SUBRECORD_ACROSS_TRACK:
                        across = array
                    elif sub_id == SUBRECORD_ALONG_TRACK:
                        along = array
                    else:
                        amplitude = array
                    continue
                fh.seek(sub_size, 1)          # anything else: skip it whole

            fh.seek(start + data_size)
            if pings % max(1, ping_step) != 0:
                continue
            if depth is None or across is None or len(depth) == 0:
                continue
            if along is None or len(along) != len(depth):
                along = np.zeros_like(depth)

            good = depth > 0
            if not good.any():
                continue
            idx = np.nonzero(good)[0][:: max(1, beam_step)]
            if idx.size == 0:
                continue

            # Rotate the beam offsets into the earth frame and convert to degrees.
            theta = math.radians(header["heading"])
            east = across[idx] * math.cos(theta) + along[idx] * math.sin(theta)
            north = -across[idx] * math.sin(theta) + along[idx] * math.cos(theta)
            lat_deg = header["lat"] + north / 111320.0
            scale_lon = math.cos(math.radians(header["lat"])) or 1e-9
            lon_deg = header["lon"] + east / (111320.0 * scale_lon)

            lon_out.append(lon_deg)
            lat_out.append(lat_deg)
            dep_out.append(depth[idx])
            amp_out.append(amplitude[idx] if amplitude is not None
                           and len(amplitude) == len(depth) else np.full(idx.size, np.nan))
            # How far off the trackline each beam landed. Kept because where two
            # swaths cover the same ground the nearer one is the better look.
            dist_out.append(np.abs(across[idx]))
            track.append((header["lon"], header["lat"]))
            kept += idx.size
            first_time = header["time"] if first_time is None else first_time
            last_time = header["time"]
            time_out.append(header["time"])

            if pings % 20000 == 0:
                log(f"    {pings:,} pings, {kept:,} soundings ...")

    if not lon_out:
        raise SystemExit("No usable soundings - are the depth arrays present?")
    return (np.concatenate(lon_out), np.concatenate(lat_out),
            np.concatenate(dep_out), np.concatenate(amp_out),
            np.concatenate(dist_out), pings, first_time, last_time, track)


def write_backscatter(lons, lats, amps, dists, out_dir: str, cell_m: float = 2.0,
                      mode: str = "nadir", tolerance_m: float = 3.0,
                      log=print) -> str:
    """
    Grid the per-beam amplitudes into a backscatter mosaic.

    Every beam carries a decibel value as well as a depth, so the same pass that
    produces soundings produces an image of the sea floor. Where two swaths
    cover the same ground, something has to decide which one you see:

      nadir  the swath nearest its own trackline wins (the default). An outer
             beam at 60 degrees smears its footprint over five times the ground
             an inner beam does, and lands there with several times the position
             error. Averaging the two throws the good look away to rescue the
             bad one. This is what MB-System's mbmosaic does with a priority
             table, for the same reason.
      mean   average everything that falls in the cell, whatever its angle.

    "Nearest wins" is applied with a tolerance, and that matters more than it
    sounds. At 0.5 m cells about five beams land in every cell - three across
    the swath and two pings' worth along it - and they are all the same look at
    the same ground. Keeping only the single closest throws four of them away
    and doubles the speckle: measured on this survey, neighbouring-pixel
    variation went from 8.1 to 15.3 grey levels and the seabed texture stopped
    being readable. So every beam within `tolerance` metres of the closest one
    is kept and averaged. Same-swath redundancy survives; a second pass that
    covered this ground from 40 m out does not.

    One honest cost remains: the specular return directly under the boat is
    brighter than the seabed warrants, so a favoured centreline reads as a
    bright ribbon. The image controls in both apps exist partly for that.

    The result is float dB in WGS84. Feed it to bag_import.py --backscatter to
    stretch it to greyscale and on to process_data.py --sonar.
    """
    import rasterio
    from rasterio.transform import from_bounds

    good = np.isfinite(amps)
    if not good.any():
        log("  (no per-beam amplitude in this file - no sonar image)")
        return ""
    lons, lats, amps = lons[good], lats[good], amps[good]
    dists = dists[good] if dists is not None and len(dists) == len(good) else None

    mid_lat = float(lats.mean())
    d_lat = cell_m / 111320.0
    d_lon = cell_m / (111320.0 * math.cos(math.radians(mid_lat)))
    west, east = float(lons.min()), float(lons.max())
    south, north = float(lats.min()), float(lats.max())
    cols = max(1, int(math.ceil((east - west) / d_lon)))
    rows = max(1, int(math.ceil((north - south) / d_lat)))

    col = np.clip(((lons - west) / d_lon).astype(int), 0, cols - 1)
    row = np.clip(((north - lats) / d_lat).astype(int), 0, rows - 1)
    flat = row * cols + col

    if mode == "nadir" and dists is not None:
        # The closest beam in each cell, found by writing the cells farthest
        # from the trackline first and letting nearer beams overwrite them.
        # One sort, and the last writer into each cell is the nearest.
        order = np.argsort(-dists, kind="stable")
        nearest = np.full(rows * cols, np.inf, dtype="float32")
        nearest[flat[order]] = dists[order]
        keep = dists <= nearest[flat] + tolerance_m
        dropped = int((~keep).sum())
        log(f"  nearest-to-trackline wins: dropped {dropped:,} of {dists.size:,} "
            f"beams that another swath covered from closer "
            f"(within {tolerance_m:g} m kept and averaged)")
        flat, amps = flat[keep], amps[keep]

    total = np.bincount(flat, weights=amps, minlength=rows * cols)
    count = np.bincount(flat, minlength=rows * cols)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(count > 0, total / np.maximum(count, 1), np.nan)
    grid = mean.reshape(rows, cols).astype("float32")

    out_path = os.path.join(out_dir, "gsf_backscatter.tif")
    profile = {
        "driver": "GTiff", "height": rows, "width": cols, "count": 1,
        "dtype": "float32", "crs": "EPSG:4326", "nodata": float("nan"),
        "transform": from_bounds(west, south, east, north, cols, rows),
        "compress": "deflate",
    }
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(grid, 1)
    filled = int(np.isfinite(grid).sum())
    log(f"  backscatter: {cols} x {rows} at {cell_m:g} m, {filled/grid.size*100:.0f}% filled, "
        f"{np.nanmin(grid):.1f}..{np.nanmax(grid):.1f} dB")
    log(f"  wrote {out_path}")
    return out_path


def convert(path: str, out_dir: str, ping_step: int = 1, beam_step: int = 1,
            cell_m: float = 2.0, mosaic: str = "nadir",
            tolerance_m: float = 3.0, log=print) -> dict:
    import datetime

    log(f"{os.path.basename(path)}  ({os.path.getsize(path)/1e6:.0f} MB)")
    lons, lats, deps, amps, dists, pings, first, last, track = soundings(
        path, ping_step=ping_step, beam_step=beam_step, log=log)

    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "gsf_depth.csv")
    stamp = datetime.datetime.fromtimestamp(first or 0, datetime.timezone.utc)
    day, clock = stamp.strftime("%Y-%m-%d"), stamp.strftime("%H:%M:%S")
    with open(csv_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("lon,lat,dep_m,date,time\n")
        for lon, lat, dep in zip(lons, lats, deps):
            fh.write(f"{lon:.7f},{lat:.7f},{dep:.2f},{day},{clock}\n")

    # The trackline, from where the vessel actually was. Built here rather than
    # by the pipeline: the CSV is one row per beam, so joining its rows in order
    # zigzags across the swath and draws a fan the boat never sailed.
    if track:
        step = max(1, len(track) // 800)
        points = [[round(lon, 6), round(lat, 6)] for lon, lat in track[::step]]
        with open(os.path.join(out_dir, "track.geojson"), "w", encoding="utf-8") as fh:
            json.dump({"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {},
                 "geometry": {"type": "LineString", "coordinates": points}}]}, fh)
        log(f"  track.geojson: {len(points)} ping positions")

    backscatter = write_backscatter(lons, lats, amps, dists, out_dir, cell_m=cell_m,
                                    mode=mosaic, tolerance_m=tolerance_m, log=log)

    found = {
        "pings": pings,
        "backscatter": backscatter,
        "soundings": int(lons.size),
        "depth_min": round(float(deps.min()), 2),
        "depth_max": round(float(deps.max()), 2),
        "lon": [round(float(lons.min()), 6), round(float(lons.max()), 6)],
        "lat": [round(float(lats.min()), 6), round(float(lats.max()), 6)],
        "surveyed_on": day,
        "csv": csv_path,
    }
    log(f"  {pings:,} pings -> {found['soundings']:,} soundings, "
        f"{found['depth_min']}-{found['depth_max']} m")
    log(f"  {found['lat'][0]:.4f}..{found['lat'][1]:.4f} N, "
        f"{found['lon'][0]:.4f}..{found['lon'][1]:.4f} E")
    log(f"  wrote {csv_path}")
    with open(os.path.join(out_dir, "gsf_import.json"), "w", encoding="utf-8") as fh:
        json.dump(found, fh, indent=2)
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gsf")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ping-step", type=int, default=1,
                    help="keep one ping in N (a survey line is many more pings "
                         "than a chart needs)")
    ap.add_argument("--beam-step", type=int, default=1, help="keep one beam in N")
    ap.add_argument("--cell", type=float, default=2.0,
                    help="backscatter mosaic cell size, in metres")
    ap.add_argument("--mosaic", choices=("nadir", "mean"), default="nadir",
                    help="where swaths overlap: show the one nearest the "
                         "trackline (default), or average them all")
    ap.add_argument("--nadir-tolerance", type=float, default=3.0,
                    help="metres: beams this much farther out than the closest "
                         "one in a cell are still averaged in, so a single "
                         "swath keeps its own redundancy (default 3)")
    args = ap.parse_args(argv)

    found = convert(args.gsf, args.out, ping_step=args.ping_step,
                    beam_step=args.beam_step, cell_m=args.cell,
                    mosaic=args.mosaic, tolerance_m=args.nadir_tolerance)
    print("")
    print(f"  python pipeline/process_data.py --csv {found['csv']} "
          f"--out-dir {args.out} --no-tide-reduction --no-track")
    print("  (--no-track: the trackline is already written from the ping positions)")
    if found.get("backscatter"):
        print("")
        print("  Then the sonar image from the same pass:")
        print(f"    python pipeline/bag_import.py --backscatter {found['backscatter']} "
              f"--out {args.out}")


if __name__ == "__main__":
    main()
