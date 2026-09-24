#!/usr/bin/env python3
"""
The down-looking sonar as a waterfall the app can scroll along the track.

PINGMapper rectifies and mosaics the side scan pair and nothing else: a down
beam is one fan pointed straight down, whose samples are range only, with the
two sides of the boat folded onto each other. There is no across-track to put
on a map, so it cannot join the mosaic. What it does show - weed standing off
the bottom, fish, a soft layer over a hard one, a ledge the boat went over -
is exactly what the side scan's nadir gap hides. So it is kept the way the
unit shows it, as an echogram, and tied to the track ping by ping.

The pings are read straight out of the channel's .SON file. PINGMapper has
already decoded every ping header into its metadata CSV - the byte offset of
each ping, its sample count, the metres per sample and its position - which is
all that is needed to lift the samples out without PINGMapper itself.

Two chart files come out, flat beside the others:

    downscan.png    the echogram, cut into strips of STRIP_WIDTH pings stacked
                    top to bottom so no side exceeds what a browser will decode
    downscan.json   per ping: position, time, bottom depth; plus the geometry
                    needed to find a ping's column in the image, and the beam
                    width that turns depth into the swath on the map

Channel preference is down imaging first, then the 200 kHz and 83 kHz beams,
so a recording made with Down Imaging switched on uses it without being asked.

    python pipeline/down_sonar.py <pingmapper project> <recording.DAT> <out dir>
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np

# (label, channel file, PINGMapper metadata name, beam width in degrees),
# best first. The widths are Humminbird's nominal cone angles - what sets how
# wide a strip of bottom each ping covers, so the app can draw the swath. The
# unit publishes no per-recording figure, so these are typical, not measured.
CHANNELS = [
    ("Down imaging", "B004.SON", "B004_ds_vhighfreq_meta.csv", 45.0),
    ("200 kHz down", "B001.SON", "B001_ds_highfreq_meta.csv", 20.0),
    ("83 kHz down", "B000.SON", "B000_ds_lowfreq_meta.csv", 60.0),
]

IMAGE_NAME = "downscan.png"
INDEX_NAME = "downscan.json"

# A browser decodes a 4096 x 16384 greyscale PNG without complaint; well past
# that some refuse outright. Surveys too long to fit are thinned along track,
# which at survey speed still leaves several pings per metre.
STRIP_WIDTH = 4096
MAX_HEIGHT = 16384
MAX_ROWS = 384

# Humminbird ping headers start with this marker; a ping that does not is an
# offset from some other file, and reading it would paint noise.
PING_MAGIC = b"\xc0\xde\xab\x21"

# A recording under this many pings is a start-stop, not a survey.
MIN_PINGS = 50

# Half-width of the moving average that takes GPS jitter out of the track.
SMOOTH_SECONDS = 1.5


def pick_channel(meta_dir: str, son_dir: str):
    """The best down channel present in both the metadata and the recording."""
    import pandas as pd
    for label, son_name, meta_name, beam in CHANNELS:
        meta_path = os.path.join(meta_dir, meta_name)
        son_path = os.path.join(son_dir, son_name)
        if not (os.path.isfile(meta_path) and os.path.isfile(son_path)):
            continue
        meta = pd.read_csv(meta_path)
        if len(meta) >= MIN_PINGS:
            return label, son_name[:4], son_path, meta, beam
    return None


def son_dir_for(recording: str) -> str:
    """A .DAT is only a header; its channels sit in a folder of the same name."""
    if os.path.isdir(recording):
        return recording
    return os.path.splitext(recording)[0]


def smooth_positions(t, lon, lat):
    """
    Spread each GPS fix over the pings between it and the next one.

    The unit stamps every ping with the latest fix, so ten pings share one
    position and the track advances in steps. On the map that puts the cursor
    in the same place for a second at a time while the waterfall scrolls
    underneath it. Interpolating between the pings where the fix changes
    gives each ping its own place on the line.
    """
    changed = np.ones(len(t), dtype=bool)
    changed[1:] = (np.diff(lon) != 0) | (np.diff(lat) != 0)
    anchors = np.flatnonzero(changed)
    if len(anchors) < 2:
        return lon, lat
    lon = np.interp(t, t[anchors], lon[anchors])
    lat = np.interp(t, t[anchors], lat[anchors])

    # Then average out the fix-to-fix jitter. A metre of wander each second
    # is nothing on the map, but summed ping to ping it makes the track -
    # and every distance measured along it - read long. A second and a half
    # either side brings along-track within 1% of straight-line distance on
    # the straight runs at Indian Hills, and still follows a turn.
    lo = np.searchsorted(t, t - SMOOTH_SECONDS, side="left")
    hi = np.searchsorted(t, t + SMOOTH_SECONDS, side="right")
    def window_mean(v):
        total = np.concatenate(([0.0], np.cumsum(v)))
        return (total[hi] - total[lo]) / (hi - lo)
    return window_mean(lon), window_mean(lat)


def read_pings(son_path: str, meta, rows: int, metres_per_row: float, log=print):
    """
    Each ping's samples, averaged down to `rows` bins of `metres_per_row`.

    Returns (image rows x pings uint8 before stretch, kept mask).
    """
    with open(son_path, "rb") as fh:
        son = fh.read()

    offsets = meta["index"].to_numpy(np.int64)
    headers = meta["son_offset"].to_numpy(np.int64)
    counts = meta["ping_cnt"].to_numpy(np.int64)
    pix = meta["pixM"].to_numpy(np.float64)

    out = np.zeros((rows, len(meta)), dtype=np.float32)
    kept = np.zeros(len(meta), dtype=bool)
    for i in range(len(meta)):
        start = offsets[i]
        if son[start:start + 4] != PING_MAGIC:
            continue
        begin = start + headers[i]
        samples = np.frombuffer(son, np.uint8, counts[i], begin).astype(np.float32)
        if not len(samples) or not pix[i] > 0:
            continue
        # Bin edges in samples; beyond the ping's own range the column stays 0.
        edges = np.floor(np.arange(rows + 1) * metres_per_row / pix[i]).astype(np.int64)
        edges = np.clip(edges, 0, len(samples))
        live = edges[1:] > edges[:-1]
        if not live.any():
            continue
        total = np.concatenate(([0.0], np.cumsum(samples, dtype=np.float64)))
        sums = total[edges[1:]] - total[edges[:-1]]
        out[live, i] = sums[live] / (edges[1:] - edges[:-1])[live]
        kept[i] = True

    bad = len(meta) - int(kept.sum())
    if bad:
        log(f"  {bad} of {len(meta)} pings did not read cleanly and were left out")
    return out, kept


def stretch(image):
    """Percentile stretch to 8 bits, measured on the water rather than the padding."""
    live = image[image > 0]
    if not live.size:
        return image.astype(np.uint8)
    lo, hi = np.percentile(live, [2.0, 99.8])
    if hi <= lo:
        hi = lo + 1.0
    scaled = (image - lo) / (hi - lo) * 255.0
    return np.clip(scaled, 0, 255).astype(np.uint8)


def build(project_dir: str, recording: str, out_dir: str, log=print):
    """
    Write downscan.png and downscan.json into out_dir.

    Returns the index dict, or None when the recording has no down channel.
    """
    from PIL import Image

    meta_dir = os.path.join(project_dir, "meta")
    found = pick_channel(meta_dir, son_dir_for(recording))
    if not found:
        log("  no down-looking channel in this recording - skipping the down sonar")
        return None
    label, channel, son_path, meta, beam = found

    if "filter" in meta.columns and meta["filter"].astype(bool).any():
        meta = meta[meta["filter"].astype(bool)]
    meta = meta.sort_values("time_s").reset_index(drop=True)

    depth = meta["dep_m"].to_numpy(np.float64) if "dep_m" in meta else \
        meta["inst_dep_m"].to_numpy(np.float64)
    pix = float(np.median(meta["pixM"]))
    ping_range = float(np.median(meta["ping_cnt"])) * pix

    # Show the water down to a little past the deepest bottom rather than the
    # full range the unit was set to: a sounder set to 20 m over 6 m of water
    # spends two thirds of its picture on nothing.
    good_depth = depth[np.isfinite(depth) & (depth > 0)]
    deep = float(np.percentile(good_depth, 99)) if good_depth.size else ping_range
    range_m = min(ping_range, max(deep * 1.35, 1.0))
    rows = int(min(MAX_ROWS, max(32, math.ceil(range_m / pix))))
    metres_per_row = range_m / rows

    # Thin along track only when the survey would not fit in one image.
    max_pings = (MAX_HEIGHT // rows) * STRIP_WIDTH
    stride = max(1, math.ceil(len(meta) / max_pings))
    if stride > 1:
        log(f"  {len(meta)} pings - keeping every {stride}th to fit one image")
        meta = meta.iloc[::stride].reset_index(drop=True)
        depth = depth[::stride]

    log(f"  {label} ({channel}): {len(meta)} pings, "
        f"{range_m:.1f} m shown in {rows} rows of {metres_per_row * 100:.1f} cm")

    columns, kept = read_pings(son_path, meta, rows, metres_per_row, log)
    if kept.sum() < MIN_PINGS:
        raise RuntimeError(
            f"Only {int(kept.sum())} pings of {channel} could be read - the metadata "
            f"may belong to a different copy of the recording than {son_path}")
    meta = meta[kept].reset_index(drop=True)
    depth = depth[kept]
    columns = stretch(columns[:, kept])

    count = columns.shape[1]
    strips = math.ceil(count / STRIP_WIDTH)
    width = min(count, STRIP_WIDTH)
    atlas = np.zeros((strips * rows, width), dtype=np.uint8)
    for s in range(strips):
        chunk = columns[:, s * STRIP_WIDTH:(s + 1) * STRIP_WIDTH]
        atlas[s * rows:(s + 1) * rows, :chunk.shape[1]] = chunk

    os.makedirs(out_dir, exist_ok=True)
    Image.fromarray(atlas, mode="L").save(os.path.join(out_dir, IMAGE_NAME),
                                          optimize=True)

    t = meta["time_s"].to_numpy(np.float64)
    lon, lat = smooth_positions(t, meta["lon"].to_numpy(np.float64),
                                meta["lat"].to_numpy(np.float64))
    # Distance along the track, summed here at full precision: the positions
    # below are rounded to 11 cm, about what the boat moves between pings, and
    # summing rounded steps turns a straight run into a staircase.
    kx = np.cos(np.radians(np.mean(lat))) * 111320.0
    steps = np.hypot(np.diff(lon) * kx, np.diff(lat) * 110540.0)
    along = np.concatenate(([0.0], np.cumsum(steps)))

    start = ""
    if "date" in meta and "time" in meta:
        start = f"{meta['date'].iloc[0]}T{str(meta['time'].iloc[0])[:8]}"

    # Integers keep a 100k-ping index to a few MB: microdegrees are 11 cm,
    # centimetres and tenths of a second are finer than the data.
    index = {
        "format": 1,
        "channel": channel,
        "label": label,
        "beamDeg": beam,
        "count": count,
        "stride": stride,
        "rows": rows,
        "metresPerRow": round(metres_per_row, 5),
        "rangeM": round(range_m, 3),
        "stripWidth": STRIP_WIDTH,
        "strips": strips,
        "start": start,
        "lon": np.round(lon * 1e6).astype(np.int64).tolist(),
        "lat": np.round(lat * 1e6).astype(np.int64).tolist(),
        "t": np.round((t - t[0]) * 10).astype(np.int64).tolist(),
        "along": np.round(along * 100).astype(np.int64).tolist(),
        "depth": np.round(np.nan_to_num(depth, nan=-1) * 100).astype(np.int64).tolist(),
    }
    with open(os.path.join(out_dir, INDEX_NAME), "w", encoding="utf-8") as fh:
        json.dump(index, fh, separators=(",", ":"))
        fh.write("\n")

    mb = (os.path.getsize(os.path.join(out_dir, IMAGE_NAME))
          + os.path.getsize(os.path.join(out_dir, INDEX_NAME))) / 1e6
    log(f"  downscan.png {width} x {strips * rows}, {strips} strip(s), {mb:.1f} MB with index")
    return index


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", help="PINGMapper project folder (holds meta/)")
    ap.add_argument("recording", help="the .DAT, or the folder of .SON files beside it")
    ap.add_argument("out_dir", help="the survey's build folder")
    args = ap.parse_args(argv)
    if build(args.project, args.recording, args.out_dir) is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
