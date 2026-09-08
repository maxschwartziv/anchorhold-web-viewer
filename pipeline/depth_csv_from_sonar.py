#!/usr/bin/env python3
"""
depth_csv_from_sonar.py

Runs PINGMapper on a raw sonar recording and keeps only what the charts need.

By default every export is switched off, so only the decode stage runs and the
depth CSV drops out in seconds instead of the minutes a full run takes. The two
slower products are opt-in:

    (default)          depth soundings -> <project>_depth.csv
    --sonar-mosaic     rectified side scan (WCR) mosaicked to GeoTIFF
    --substrate-map    substrate prediction, classified raster and its mosaic

Output:
    <out_dir>/<project>/meta/*_ds_*_meta.csv     PINGMapper's full metadata
    <out_dir>/<project>_depth.csv                slim lon, lat, dep_m, date, time
    <out_dir>/<project>_products.json            paths of everything produced
    <out_dir>/<project>/**/sonar_mosaic/*.tif    with --sonar-mosaic
    <out_dir>/<project>/**/substrate/...tif      with --substrate-map

The slim CSV is exactly what pipeline/process_data.py and the location GUI want
for their "depth data CSV" input; the rasters are its sonar/substrate inputs.

Examples:
    python depth_csv_from_sonar.py recordings/R00003.DAT
    python depth_csv_from_sonar.py recordings/R00003.DAT --sonar-mosaic --substrate-map
    python depth_csv_from_sonar.py recordings/ --batch --temp 18
"""

import argparse
import csv
import glob
import json
import os
import sys
import time

# Columns the downstream pipeline expects, in order.
SLIM_COLUMNS = ['lon', 'lat', 'dep_m', 'date', 'time']

# Depth columns PINGMapper may write, best first.
DEPTH_CANDIDATES = ['dep_m', 'dep_m_smth', 'inst_dep_m']


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Run PINGMapper on a sonar recording and keep only the depth "
                    "+ lat/lon CSV (no sonar imagery, substrate or mosaics).")
    p.add_argument("recording",
                   help="sonar recording (.DAT/.sl2/.sl3/.RSD/.svlog/.jsf/.xtf), "
                        "or a folder when --batch is given")
    p.add_argument("--out-dir", default=None,
                   help="where the PINGMapper project is written (default: next to the recording)")
    p.add_argument("--project", default=None,
                   help="project name (default: the recording's file name)")
    p.add_argument("--csv-out", default=None,
                   help="path for the slim depth CSV (default: <out-dir>/<project>_depth.csv)")
    p.add_argument("--batch", action="store_true",
                   help="treat the input as a folder and process every recording in it")
    p.add_argument("--temp", type=float, default=10.0,
                   help="water temperature °C, used for the speed of sound (default 10)")
    p.add_argument("--depth-source", default="sensor",
                   choices=["sensor", "auto"],
                   help="'sensor' uses the depth the sounder logged (fast); "
                        "'auto' re-picks the bed with PINGMapper's model (slower)")
    p.add_argument("--smooth", action="store_true",
                   help="also write PINGMapper's smoothed depth column")
    p.add_argument("--depth-column", default=None, choices=DEPTH_CANDIDATES,
                   help="which depth column to copy into the slim CSV")
    p.add_argument("--threads", type=float, default=0.5,
                   help="thread count; <1 is a fraction of the cores (default 0.5)")
    p.add_argument("--keep-existing", action="store_true",
                   help="skip the PINGMapper run and just rebuild the slim CSV from "
                        "an existing project folder")
    p.add_argument("--sonar-mosaic", action="store_true",
                   help="also rectify the side scan and mosaic it (slow: minutes)")
    p.add_argument("--substrate-map", action="store_true",
                   help="also predict substrate and mosaic the classified raster (slow)")

    # -- Mosaic image quality ------------------------------------------------
    #
    # PINGMapper ships with all of this off, which is the conservative default
    # for a research tool but not what you want to look at. The knobs are
    # separate so a run can be backed off one at a time when the combination
    # over-cooks.
    q = p.add_argument_group("sonar mosaic image quality")
    q.add_argument("--time-filter", default="",
                   help="a time_table CSV of stretches to keep "
                        "(start_seconds, end_seconds), as the Recording "
                        "Fixer writes beside a repaired recording")
    q.add_argument("--pix-res-son", type=float, default=0.0,
                   help="mosaic pixel size in metres; 0 keeps the recording's "
                        "own resolution and skips the resample (default 0)")
    q.add_argument("--egn", action="store_true",
                   help="empirical gain normalisation: divide every ping by a "
                        "per-range mean taken over the whole survey. Removes the "
                        "bright nadir ribbon and the dark outer edges - the one "
                        "setting that most changes how readable a mosaic is")
    q.add_argument("--egn-stretch", default="percent",
                   choices=["none", "minmax", "percent"],
                   help="contrast stretch applied after EGN (default percent)")
    q.add_argument("--egn-stretch-factor", type=float, default=0.5,
                   help="percent-clip amount for --egn-stretch percent (default 0.5)")
    q.add_argument("--db-transform", action="store_true",
                   help="20*log10 before the 8-bit mapping; lifts low-amplitude "
                        "detail out of the dark end")
    q.add_argument("--clahe", action="store_true",
                   help="adaptive local contrast (CLAHE) after the dB transform")
    q.add_argument("--clahe-clip", type=float, default=0.02,
                   help="CLAHE clip limit; higher is punchier (default 0.02)")
    q.add_argument("--tone-gamma", type=float, default=1.0,
                   help="post-EGN gamma; <1 brightens mid-tones, >1 darkens "
                        "(default 1 = off)")
    q.add_argument("--tone-gain", type=float, default=1.0,
                   help="post-EGN gain; <1 darkens overall (default 1 = off)")
    q.add_argument("--speed-correct", action="store_true",
                   help="resample along-track for boat speed, so the image is "
                        "not stretched where the boat slowed")
    q.add_argument("--min-speed", type=float, default=0.0,
                   help="drop pings slower than this (m/s); a boat stopped or "
                        "turning smears the same ground over many pings")
    q.add_argument("--max-heading-deviation", type=float, default=0.0,
                   help="drop pings whose heading swings more than this many "
                        "degrees over --max-heading-distance metres")
    q.add_argument("--max-heading-distance", type=float, default=0.0,
                   help="distance over which --max-heading-deviation is judged")
    q.add_argument("--best-image", action="store_true",
                   help="EGN + CLAHE at native resolution. Measured against a "
                        "hand-tuned Indian Hills run: +72%% neighbouring-pixel "
                        "detail, +0.85 bits entropy, +22%% coverage")
    # -- Substrate map -------------------------------------------------------
    #
    # PINGMapper predicts a class per pixel and then has to decide what to do
    # with the softmax: take the most likely class everywhere, or lean on the
    # hard-bottom classes where they are plausible at all. Which is right
    # depends on the lake and on what the map is for, so it is a choice rather
    # than a default.
    s = p.add_argument_group('substrate map')
    s.add_argument('--substrate-class', default='max', choices=['max', 'thresh'],
                   help="'max' takes the most likely class for every pixel; "
                        "'thresh' promotes gravel and cobble/boulder wherever "
                        "their probability clears a threshold, which finds more "
                        "hard bottom at the cost of some false positives "
                        "(default max)")
    s.add_argument('--substrate-res', type=float, default=0.0,
                   help='substrate map pixel size in metres; 0 keeps the '
                        "recording's own resolution (PINGMapper's own default "
                        'is 0.25) (default 0)')
    s.add_argument('--substrate-polygons', action='store_true',
                   help='also export the classified map as polygons '
                        '(shapefiles beside the raster)')

    args = p.parse_args(argv)
    if args.best_image:
        # EGN and CLAHE, deliberately WITHOUT the dB transform.
        #
        # Measured on Indian Hills Lake, sampled on one 24 x 34 m window, with
        # detail = mean absolute difference between vertically adjacent pixels:
        #
        #   baseline (EGN + dB + tone)  detail 11.36  entropy 6.95
        #   EGN only                     7.17          6.70
        #   dB only                     10.26          7.83
        #   CLAHE only                  17.24          7.74
        #   EGN + CLAHE                 19.54          7.80   <- this
        #   EGN + dB + CLAHE             2.21          5.06   <- collapses
        #
        # All three stacked is far worse than any one of them: three successive
        # normalisations compound until the seabed is black and only the nadir
        # survives. dB is the one to leave out - it is the weakest on its own
        # and it is what makes the stack fail.
        args.egn = args.clahe = True
        args.db_transform = False
        args.pix_res_son = 0.0
    return args


# What one substrate worker needs to itself. Shadow detection - which
# PINGMapper switches on for any substrate run, whatever remShadow says -
# loads a segmentation model in every parallel worker, so the memory cost is
# per worker rather than per run. This is a budget, not a measurement: it is
# set where eight workers on a 17 GB machine, which is what a bare
# --threads 0.5 asks for, comes down to something that fits.
SUBSTRATE_GB_PER_WORKER = 2.5


def available_gb() -> float:
    """Memory this machine could actually give a worker, or 0 if unknowable."""
    try:
        import psutil
        return psutil.virtual_memory().available / 1e9
    except Exception:
        pass
    try:                                      # Windows, without psutil
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return status.ullAvailPhys / 1e9
    except Exception:
        return 0.0


def resolve_threads(args) -> int:
    """
    How many workers to ask PINGMapper for.

    The fraction is resolved here rather than left to PINGMapper, because a
    substrate run then has to be judged against memory as well as cores: eight
    shadow-detection workers, each with its own model, is what killed a run on
    this machine with 3.5 GB free. The worker that dies takes the whole run
    with it, an hour in, with nothing but a TerminatedWorkerError to say why.
    """
    cpus = os.cpu_count() or 4
    if args.threads <= 0:
        wanted = cpus
    elif args.threads < 1:
        wanted = max(1, int(cpus * args.threads))
    else:
        wanted = int(args.threads)
    wanted = max(1, min(wanted, cpus))
    if not args.substrate_map:
        return wanted

    free = available_gb()
    if free <= 0:
        return wanted                          # nothing to judge against
    fits = max(1, int(free // SUBSTRATE_GB_PER_WORKER))
    if fits < wanted:
        print(f"  substrate mapping: {fits} worker(s), not {wanted} - "
              f"{free:.1f} GB free and each one loads its own model")
    return min(wanted, fits)


def image_settings(args) -> dict:
    """
    The settings that decide what a mosaic looks like, in one dict.

    Written into the products manifest so that whatever asks for a rebuild
    later can tell whether the mosaic already on disk was made the way it is
    now asking for. Without it, changing the image settings and pressing
    build again silently reuses the old mosaic, which is indistinguishable
    from the settings not working.
    """
    return {
        "pixResSon": args.pix_res_son,
        "egn": bool(args.egn),
        "egnStretch": args.egn_stretch,
        "egnStretchFactor": args.egn_stretch_factor,
        "dbTransform": bool(args.db_transform),
        "clahe": bool(args.clahe),
        "claheClip": args.clahe_clip,
        "toneGamma": args.tone_gamma,
        "toneGain": args.tone_gain,
        "speedCorrect": bool(args.speed_correct),
        "minSpeed": args.min_speed,
        "maxHeadingDeviation": args.max_heading_deviation,
        "maxHeadingDistance": args.max_heading_distance,
    }


def substrate_settings(args) -> dict:
    """
    The settings that decide what the substrate map says.

    Recorded beside the image settings and for the same reason: a substrate
    raster already on disk was classified some particular way, and reusing it
    for a build that asked for another way is a silent wrong answer.
    """
    return {
        "classMethod": args.substrate_class,
        "pixResMap": args.substrate_res,
        "polygons": bool(args.substrate_polygons),
    }


def depth_only_params(args):
    """PINGMapper parameters with everything except the depth decode switched off."""
    return {
        "project_mode": 1,          # overwrite an existing project of the same name
        "threadCnt": resolve_threads(args),
        "tempC": args.temp,
        "nchunk": 500,
        "cropRange": 0,
        "exportUnknown": False,
        "fixNoDat": False,

        # Depth detection. PINGMapper wants ints here (its GUI is what turns
        # "Sensor"/"Auto" into 0/1), and a string silently blows up mid-run.
        "detectDep": 0 if args.depth_source == 'sensor' else 1,
        "smthDep": bool(args.smooth),
        "adjDep": 0,
        "pltBedPick": False,
        "remShadow": 0,

        # Sonar imagery: rectified side scan with the water column removed, then
        # mosaicked to GeoTIFF (mosaic: 0=off, 1=GTiff, 2=VRT).
        # Image quality. EGN is the substantive one: PINGMapper divides each
        # ping by a per-range-bin mean built from the whole survey, then
        # rescales globally, which is the range-attenuation and beam-pattern
        # correction. The dB transform and CLAHE are cosmetic stretches on top.
        # All three reach the rectified export that feeds the mosaic, applied
        # in the order EGN -> stretch -> dB -> CLAHE -> 8-bit.
        "pix_res_son": args.pix_res_son,
        "egn": bool(args.egn),
        "egn_stretch": {"none": 0, "minmax": 1, "percent": 2}[args.egn_stretch],
        "egn_stretch_factor": args.egn_stretch_factor,
        "sonar_db_transform": bool(args.db_transform),
        "sonar_clahe": bool(args.clahe),
        # Global bounds, always. Per-chunk bounds give every chunk its own
        # stretch and the mosaic seams become visible at each boundary.
        "sonar_clahe_global": True,
        "sonar_clahe_clip_limit": args.clahe_clip,
        "tone_gamma": args.tone_gamma,
        "tone_gain": args.tone_gain,
        "spdCor": bool(args.speed_correct),
        # Track filtering. A boat that stopped or turned paints the same ground
        # over and over from different angles; those pings are the smeared
        # fans in a mosaic, and dropping them is pure gain.
        "min_speed": args.min_speed,
        "max_speed": 0.0,
        "max_heading_deviation": args.max_heading_deviation,
        "max_heading_distance": args.max_heading_distance,
        "filter_table": False,
        # The stretches to keep, from a Fixer session. PINGMapper reads
        # start_seconds/end_seconds and drops every ping no row covers -
        # which is how an edit survives without the recording being
        # rewritten, side scan included.
        "time_table": args.time_filter or False,
        "pix_res_map": args.substrate_res,
        "maxCrop": False,
        "son_colorMap": "gist_gray",

        "wcp": False, "wcm": False, "wcr": bool(args.sonar_mosaic), "wco": False,
        "waterfall_ss_image": False, "waterfall_ss_video": False,
        "waterfall_di_image": False, "waterfall_di_video": False,
        "tileFile": ".jpg",

        "rect_wcp": False, "rect_wcr": bool(args.sonar_mosaic),
        "rubberSheeting": True, "rectMethod": "COG", "rectInterpDist": 50,

        # Substrate: prediction feeds the classified raster, which feeds its mosaic.
        "pred_sub": bool(args.substrate_map), "pltSubClass": False,
        "map_sub": bool(args.substrate_map),
        "map_class_method": args.substrate_class,
        "export_poly": bool(args.substrate_polygons), "map_predict": 0,

        "mosaic": 1 if args.sonar_mosaic else 0,
        "map_mosaic": 1 if args.substrate_map else 0,
        "mosaic_nchunk": 0,
        "banklines": False, "coverage": False,
    }


def _teach_pingmapper_logger_to_be_a_stream():
    """
    Make PINGMapper's Logger answer the questions a stream is asked.

    It replaces sys.stdout to tee output into a log file, and implements
    write and flush - which is enough for print and not enough for anyone
    else. Keras reads sys.stdout.encoding before printing a progress line,
    and tqdm asks whether it is a terminal; either raises AttributeError
    against the Logger and takes the whole run down with it.

    Each attribute is delegated to the real stream it already wraps, so
    this adds nothing and changes nothing - it just stops the lookup
    failing. Harmless if PINGMapper fixes it upstream.
    """
    try:
        from pingmapper import funcs_common
    except ImportError:
        return
    logger = getattr(funcs_common, 'Logger', None)
    if logger is None:
        return

    def passthrough(name, fallback):
        def get(self):
            return getattr(self.terminal, name, fallback)
        return property(get)

    for name, fallback in (('encoding', 'utf-8'), ('errors', 'replace')):
        if not hasattr(logger, name):
            setattr(logger, name, passthrough(name, fallback))
    if not hasattr(logger, 'isatty'):
        logger.isatty = lambda self: False
    if not hasattr(logger, 'fileno'):
        def fileno(self):
            return self.terminal.fileno()
        logger.fileno = fileno


def run_pingmapper(args, project_dir):
    from pingmapper.doWork import doWork

    _teach_pingmapper_logger_to_be_a_stream()

    params = depth_only_params(args)
    recording = os.path.abspath(args.recording)
    out_dir = os.path.dirname(project_dir)
    proj_name = os.path.basename(project_dir)

    print(f"PINGMapper: {recording}")
    print(f"  project : {project_dir}")
    print(f"  depth   : {args.depth_source}{' (smoothed)' if args.smooth else ''}, "
          f"{args.temp} °C\n")

    started = time.time()
    if args.batch:
        results = doWork(in_dir=recording, out_dir=out_dir, proj_name=proj_name,
                         batch=True, params=params)
    else:
        results = doWork(in_file=recording, out_dir=out_dir, proj_name=proj_name,
                         batch=False, params=params)
    print(f"\nDecode finished in {time.time() - started:.0f} s")

    # doWork swallows exceptions per recording, so check what it reports rather
    # than assuming a CSV that happens to exist is complete.
    failed = [r for r in (results or []) if not r.get('success')]
    if failed:
        for r in failed:
            print(f"\nPINGMapper failed on {r.get('inFile')}", file=sys.stderr)
            if r.get('logfilename'):
                print(f"  log: {r['logfilename']}", file=sys.stderr)
        if len(failed) == len(results or []):
            raise SystemExit("PINGMapper could not process the recording(s); "
                             "see the log above.")
        print("Continuing with the recordings that did decode.", file=sys.stderr)


def find_products(project_dir):
    """Locate the sonar and substrate rasters a fuller run leaves behind."""
    sonar = sorted(glob.glob(os.path.join(project_dir, "**", "sonar_mosaic", "*.tif"),
                             recursive=True))
    substrate = sorted(glob.glob(
        os.path.join(project_dir, "**", "substrate", "map_substrate_mosaic", "*.tif"),
        recursive=True))
    return sonar, substrate


def find_meta_csv(project_dir):
    """The downward-beam metadata CSV, which is where depth and position live."""
    meta_dir = os.path.join(project_dir, 'meta')
    if not os.path.isdir(meta_dir):
        # Batch runs put each recording in its own subfolder.
        nested = sorted(glob.glob(os.path.join(project_dir, '*', 'meta')))
        if not nested:
            raise FileNotFoundError(f"No meta folder under {project_dir}")
        meta_dir = nested[0]

    candidates = sorted(glob.glob(os.path.join(meta_dir, '*_meta.csv')))
    if not candidates:
        raise FileNotFoundError(f"No *_meta.csv in {meta_dir}")
    for name in ('ds_highfreq', 'ds_lowfreq', 'ds_vhighfreq', '_ds_'):
        for path in candidates:
            if name in os.path.basename(path):
                return path
    return candidates[0]


def write_slim_csv(meta_csv, out_csv, depth_column=None):
    """Copy lon/lat/depth (+ timestamp) out of PINGMapper's metadata."""
    with open(meta_csv, newline='') as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        depth = depth_column or next((c for c in DEPTH_CANDIDATES if c in header), None)
        if depth is None:
            raise RuntimeError(f"No depth column in {meta_csv} (looked for {DEPTH_CANDIDATES})")
        if 'lon' not in header or 'lat' not in header:
            raise RuntimeError(f"No lon/lat columns in {meta_csv}")

        rows, skipped = [], 0
        for row in reader:
            try:
                lon, lat, dep = float(row['lon']), float(row['lat']), float(row[depth])
            except (TypeError, ValueError):
                skipped += 1
                continue
            if dep <= 0:
                skipped += 1
                continue
            rows.append({'lon': lon, 'lat': lat, 'dep_m': dep,
                         'date': row.get('date', ''), 'time': row.get('time', '')})

    if not rows:
        raise RuntimeError(f"No usable soundings in {meta_csv}")

    os.makedirs(os.path.dirname(os.path.abspath(out_csv)) or '.', exist_ok=True)
    with open(out_csv, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=SLIM_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    lons = [r['lon'] for r in rows]
    lats = [r['lat'] for r in rows]
    deps = [r['dep_m'] for r in rows]
    print(f"\nDepth CSV: {out_csv}")
    print(f"  {len(rows):,} soundings from column '{depth}'"
          + (f" ({skipped:,} rows skipped)" if skipped else ""))
    print(f"  depth  {min(deps):.2f} - {max(deps):.2f} m")
    print(f"  extent lon {min(lons):.6f}..{max(lons):.6f}  lat {min(lats):.6f}..{max(lats):.6f}")
    return out_csv


def main(argv=None):
    args = parse_args(argv)

    recording = os.path.abspath(args.recording)
    if not os.path.exists(recording):
        raise SystemExit(f"Not found: {recording}")

    project = args.project or os.path.splitext(os.path.basename(recording.rstrip('/\\')))[0]
    out_dir = os.path.abspath(args.out_dir or os.path.dirname(recording) or '.')
    project_dir = os.path.join(out_dir, project)

    if not args.keep_existing:
        run_pingmapper(args, project_dir)
    elif not os.path.isdir(project_dir):
        raise SystemExit(f"--keep-existing given but no project at {project_dir}")

    meta_csv = find_meta_csv(project_dir)
    print(f"\nMetadata: {meta_csv}")
    out_csv = args.csv_out or os.path.join(out_dir, f"{project}_depth.csv")
    write_slim_csv(meta_csv, out_csv, args.depth_column)
    sonar, substrate = find_products(project_dir)
    if args.sonar_mosaic:
        print(f"\nSonar mosaic: {len(sonar)} tile(s)")
        for f in sonar:
            print(f"  {f}")
    if args.substrate_map:
        print(f"\nSubstrate map: {len(substrate)} raster(s)")
        for f in substrate:
            print(f"  {f}")

    # Machine-readable summary so the location GUI can pick the products up.
    manifest = os.path.join(out_dir, f"{project}_products.json")
    with open(manifest, "w") as f:
        # The settings go under their own names. Calling the substrate ones
        # "substrate" put them where the list of substrate rasters lives, and
        # the later key in a dict literal simply wins: the paths vanished and
        # whatever read them back got three setting names where files should
        # have been.
        json.dump({"csv": os.path.abspath(out_csv),
                   "sonar": [os.path.abspath(x) for x in sonar],
                   "substrate": [os.path.abspath(x) for x in substrate],
                   "imageSettings": image_settings(args),
                   "substrateSettings": substrate_settings(args)}, f, indent=2)
    print(f"\nProducts manifest: {manifest}")
    print("\nUse the CSV as the depth data in Add Survey Locations "
          "(or process_data.py --csv).")


if __name__ == '__main__':
    sys.exit(main())
