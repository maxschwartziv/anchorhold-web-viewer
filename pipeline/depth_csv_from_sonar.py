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

Two ways to avoid paying for a decode that has already happened:

    --reuse-decode     keep the decoded project and redo only the stages the
                       changed settings actually reach
    --swatch N         rectify N chunks instead of the whole survey, so a
                       toning can be looked at in seconds

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
import hashlib
import json
import os
import shutil
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

    # -- Reusing work already done ------------------------------------------
    r = p.add_argument_group('reusing an earlier run')
    r.add_argument("--reuse-decode", action="store_true",
                   help="keep the decoded project on disk and redo only what "
                        "the changed settings reach. Most of the mosaic panel "
                        "is applied while a chunk is warped and never touches "
                        "the decode, so this turns a rebuild from minutes into "
                        "about a minute. Refused when a setting that decides "
                        "which pings exist has changed")
    r.add_argument("--swatch", type=int, nargs='?', const=3, default=0,
                   metavar="N",
                   help="rectify N chunks (default 3) instead of the whole "
                        "survey and write them to <project>_swatch/ with a PNG "
                        "to look at. Survey-wide statistics are kept, so the "
                        "swatch is toned exactly as the full mosaic would be. "
                        "Implies --reuse-decode and --sonar-mosaic")
    r.add_argument("--swatch-at", type=float, default=0.5, metavar="F",
                   help="where along the survey the swatch is taken, 0 to 1 "
                        "(default 0.5, the middle)")
    r.add_argument("--swatch-label", default="", metavar="NAME",
                   help="put this swatch in <project>_swatch/<NAME>/ instead "
                        "of the folder itself, and leave its siblings alone. "
                        "What lets several tonings of the same water be cut "
                        "one after another and then looked at together")

    args = p.parse_args(argv)
    if args.swatch:
        # A swatch is a look at a toning, which means it needs the survey-wide
        # numbers an earlier decode already worked out. There is nothing to
        # look at without the mosaic stage either.
        args.reuse_decode = True
        args.sonar_mosaic = True
        args.swatch_at = min(1.0, max(0.0, args.swatch_at))
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


def time_filter_digest(path) -> str:
    """
    What a time filter says, rather than where it lives.

    A Fixer edit travels as a CSV beside the recording, and editing it again
    changes which pings are decoded without changing its name. Comparing the
    contents is the only way the settings check can see that.
    """
    if not path or not os.path.isfile(path):
        return ''
    digest = hashlib.sha1()
    with open(path, 'rb') as f:
        digest.update(f.read())
    return digest.hexdigest()[:16]


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
        "timeFilter": time_filter_digest(args.time_filter),
        "speedCorrect": bool(args.speed_correct),
        "minSpeed": args.min_speed,
        "maxHeadingDeviation": args.max_heading_deviation,
        "maxHeadingDistance": args.max_heading_distance,
    }


# ── What a changed setting actually costs ────────────────────────────────────
#
# PINGMapper reads the recording, then rectifies, then maps substrate. The
# settings above are not all at the same depth in that: most of them are
# applied while a chunk is being warped and never touch the decode at all.
# Knowing which is which is the difference between a fifteen minute rebuild
# and a forty second one.
#
# Measured on Indian Hills Lake, 32,640 pings: reading 128 s of which the EGN
# statistics are 39 s, rectify and mosaic 50 s, substrate 377 s.

# Applied while the chunk is warped. rectify_master_func sets the CLAHE and dB
# ones straight onto the rectObj from the run parameters, so they need nothing
# but a rectify; tone_gamma and tone_gain it does not set, so those are written
# into the pickled sonObj first (see prime_project_for_reuse). pix_res_son is
# handled by read_master_func's own project_mode 2 branch.
RECTIFY_STAGE_SETTINGS = frozenset({
    'dbTransform', 'clahe', 'claheClip', 'toneGamma', 'toneGain', 'pixResSon',
})

# The EGN statistics are a pass over the whole survey, kept on the sonObj. A
# change here has to redo that pass before the rectify stage can use it.
EGN_STAGE_SETTINGS = frozenset({'egn', 'egnStretch', 'egnStretchFactor'})

# Everything else decides which pings exist at all, which is the decode.
# Nothing downstream of it can be reused.
READ_STAGE_SETTINGS = frozenset({
    'timeFilter',
    'speedCorrect', 'minSpeed', 'maxHeadingDeviation', 'maxHeadingDistance',
})


def plan_rerun(cached, wanted):
    """
    How much of a run a change to the image settings needs.

    Returns {'stage': 'none'|'rectify'|'egn'|'read', 'changed': [keys]}.

    A key in neither table counts as 'read'. Adding a setting and forgetting
    to classify it then costs time rather than correctness, which is the right
    way round: the failure this path must not have is a setting that appears
    to do nothing.
    """
    if not isinstance(cached, dict):
        return {'stage': 'read', 'changed': ['(no record of the earlier run)']}
    # Belt and braces: callers should have read the manifest through
    # cached_image_settings, but a raw dict must not be read as "everything
    # changed" just because it predates a key.
    cached = fill_image_defaults(cached)
    changed = sorted(k for k in set(cached) | set(wanted)
                     if cached.get(k) != wanted.get(k))
    if not changed:
        return {'stage': 'none', 'changed': []}
    known = RECTIFY_STAGE_SETTINGS | EGN_STAGE_SETTINGS
    stage = 'rectify'
    for key in changed:
        if key not in known:
            return {'stage': 'read', 'changed': changed}
        if key in EGN_STAGE_SETTINGS:
            stage = 'egn'
    return {'stage': stage, 'changed': changed}


# Settings that were added after manifests started being written, with what
# a manifest's silence about them should be read as. Without this every
# survey on disk differs from every set of settings by the new key alone,
# and rebuilds once for no reason.
IMAGE_SETTING_DEFAULTS = {'timeFilter': ''}


def fill_image_defaults(image):
    """A manifest's image settings, with anything it predates filled in."""
    if not isinstance(image, dict):
        return image
    filled = dict(image)
    for key, value in IMAGE_SETTING_DEFAULTS.items():
        filled.setdefault(key, value)
    return filled


def cached_image_settings(out_dir, project):
    """The image settings the project on disk was last built with, or None."""
    manifest = os.path.join(out_dir, "%s_products.json" % project)
    try:
        with open(manifest) as f:
            return fill_image_defaults(json.load(f).get('imageSettings'))
    except (OSError, ValueError, AttributeError):
        return None


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
    # Reusing the decode means project_mode 2, which loads the pickled sonObjs
    # instead of rebuilding them. Two things then become waste rather than
    # work: the sonogram tile export, which the rectify stage does not read
    # (it loads intensities straight from the .SON), and the substrate
    # prediction, whose raster is still sitting in the project untouched.
    reuse = bool(getattr(args, 'reuse_decode', False))
    return {
        "project_mode": 2 if reuse else 1,  # 2 = update, 1 = overwrite
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

        "wcp": False, "wcm": False,
        "wcr": bool(args.sonar_mosaic) and not reuse, "wco": False,
        "waterfall_ss_image": False, "waterfall_ss_video": False,
        "waterfall_di_image": False, "waterfall_di_video": False,
        "tileFile": ".jpg",

        "rect_wcp": False, "rect_wcr": bool(args.sonar_mosaic),
        "rubberSheeting": True, "rectMethod": "COG", "rectInterpDist": 50,

        # Substrate: prediction feeds the classified raster, which feeds its mosaic.
        "pred_sub": bool(args.substrate_map) and not reuse, "pltSubClass": False,
        "map_sub": bool(args.substrate_map) and not reuse,
        "map_class_method": args.substrate_class,
        "export_poly": bool(args.substrate_polygons), "map_predict": 0,

        "mosaic": 1 if args.sonar_mosaic else 0,
        "map_mosaic": 0 if reuse else (1 if args.substrate_map else 0),
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


def prime_project_for_reuse(project_dir, args, plan):
    """
    Write into the pickled sonObjs the settings a project_mode 2 run would not
    otherwise pick up.

    rectify_master_func sets the CLAHE and dB settings onto the rectObj from
    the run parameters, but not tone_gamma and tone_gain; and read_master_func
    skips the EGN block entirely when the sonObj already says EGN matches. In
    project_mode 2 both of those therefore come off the pickle, and a changed
    gamma or a changed stretch would be silently ignored. That is the one
    failure this whole path must not have, so the values go in first.
    """
    import pickle

    metas = sorted(glob.glob(os.path.join(project_dir, 'meta', '*.meta')))
    if not metas:
        raise SystemExit("--reuse-decode needs an already-decoded project; "
                         "none at %s" % project_dir)

    force_egn = plan['stage'] == 'egn'
    # The CLAHE normalisation bounds are sampled across the whole survey and
    # then cached on the sonObj with no invalidation of any kind. They are
    # taken after the dB transform, so that setting and only that setting
    # makes them stale.
    drop_clahe = 'dbTransform' in plan['changed']

    for path in metas:
        with open(path, 'rb') as f:
            son = pickle.load(f)
        son.tone_gamma = float(args.tone_gamma)
        son.tone_gain = float(args.tone_gain)
        if force_egn:
            # read_master_func skips the EGN pass when son.egn already equals
            # what was asked for. Clearing it is what makes a changed stretch
            # recompute - and is also the only thing that turns EGN off,
            # since in project_mode 2 nothing else writes son.egn.
            son.egn = False
        if drop_clahe:
            son._sonar_clahe_global_bounds = None
        # PINGMapper's smoothTrackline returns the filenames it wrote, except
        # when the sonObj already carries smthTrkFile - then it prints "Using
        # existing smoothed trackline" and falls off the end returning None,
        # and rectify_master_func does `beam in None` two lines later. So the
        # attribute goes and the trackline is smoothed again: a second and a
        # half, against reaching into site-packages to fix a bug an upgrade
        # would undo anyway.
        if hasattr(son, 'smthTrkFile'):
            del son.smthTrkFile
        # And the depth pick has to be redone, cheap as it is to skip.
        #
        # project_mode 2 re-derives the ping metadata CSVs from the recording
        # before it decides what to skip, so dep_m and the columns beside it
        # are wiped on the way in - and then the depth step, the only thing
        # that writes them, is skipped because the sonObj says depths were
        # exported already. The next thing to ask for dep_m is _interpTrack,
        # in the rectify stage, and it dies on a KeyError. Clearing detectDep
        # stops that skip firing; the step writes the real value back itself.
        son.detectDep = -1
        # Through a temporary file: a half-written pickle is a dead project,
        # and this runs on every rebuild.
        tmp = path + '.new'
        with open(tmp, 'wb') as f:
            pickle.dump(son, f)
        os.replace(tmp, path)

    note = "  primed %d sonObj(s): gamma %s, gain %s, depth pick to redo" % (
        len(metas), args.tone_gamma, args.tone_gain)
    if force_egn:
        note += ", EGN statistics to recompute"
    if drop_clahe:
        note += ", CLAHE bounds dropped"
    print(note)
    if args.depth_source != 'sensor':
        print("  (the depth pick is 'auto', so redoing it is the slow part of "
              "this run)")


def clear_rect_outputs(project_dir):
    """
    Delete the rectified tiles and the sonar mosaic before a reuse run.

    _createMosaic globs the rect_wcr folder rather than taking the chunks it
    just wrote, so a tile left over from a previous toning would be mosaicked
    in beside the new ones and nothing would say so. The mosaics themselves
    are named by index, so a shorter run leaves the tail of a longer one
    behind. Substrate is not touched: that is the output being reused.
    """
    targets = sorted(glob.glob(os.path.join(project_dir, '*', 'rect_wc*')))
    targets.append(os.path.join(project_dir, 'sonar_mosaic'))
    removed = 0
    for path in targets:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    if removed:
        print("  cleared %d stale rectified output folder(s)" % removed)


def limit_rectify_to_chunks(count, at=0.5):
    """
    Make the rectify stage work on a short run of chunks instead of all of them.

    The point of a swatch is to answer "does this toning look right" without
    waiting for the whole survey, and the only honest way to do that is to
    keep every survey-wide number - the EGN range means, the CLAHE bounds -
    exactly as the full run computes them, and simply warp less of it. So the
    limit goes on rectObj._getChunkID, which is what the rectify stage
    iterates, and nowhere near the read stage, which is where those numbers
    come from. A swatch toned off swatch-sized statistics would look nothing
    like the mosaic it is supposed to predict.

    Port and starboard are windowed by chunk id rather than by position in
    their own lists, so the two beams cover the same water even when filtering
    has left them with different chunks.

    Returns a dict that gains a 'span' key once the first beam has been asked.
    """
    import numpy as np
    from pingmapper.class_rectObj import rectObj

    original = getattr(rectObj, '_getChunkID_unlimited', rectObj._getChunkID)
    window = {}

    def limited(self):
        chunks = original(self)
        ids = sorted(int(c) for c in chunks)
        if not ids:
            return chunks
        if 'span' not in window:
            if len(ids) <= count:
                window['span'] = (ids[0], ids[-1])
            else:
                start = int(round(at * (len(ids) - count)))
                start = max(0, min(start, len(ids) - count))
                window['span'] = (ids[start], ids[start + count - 1])
        lo, hi = window['span']
        kept = np.array([int(c) for c in chunks if lo <= int(c) <= hi], dtype=int)
        return kept if kept.size else chunks

    rectObj._getChunkID_unlimited = original
    rectObj._getChunkID = limited
    return window


def write_png_preview(tif_path, png_path, width=1400):
    """A look at the swatch that opens in anything, beside the GeoTIFF."""
    try:
        from osgeo import gdal
        src = gdal.Open(tif_path)
        if src is None:
            return ''
        scale = min(1.0, width / float(src.RasterXSize or 1))
        gdal.Translate(png_path, src, format='PNG', outputType=gdal.GDT_Byte,
                       width=max(1, int(src.RasterXSize * scale)),
                       height=max(1, int(src.RasterYSize * scale)))
        src = None
        return png_path if os.path.isfile(png_path) else ''
    except Exception as exc:
        print("  (no PNG preview: %s)" % exc)
        return ''


def forget_full_mosaic(out_dir, project):
    """
    Record in the manifest that the full mosaic is gone.

    A swatch rectifies over the tiles the full mosaic was built from and then
    clears them. Leaving the manifest pointing at mosaics that no longer exist
    would be merely untidy - cached_products drops missing files - but the
    manifest's whole job is to be true about what is on disk.
    """
    manifest = os.path.join(out_dir, "%s_products.json" % project)
    try:
        with open(manifest) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not data.get('sonar'):
        return
    data['sonar'] = []
    data['sonarClearedBy'] = 'swatch'
    with open(manifest, 'w') as f:
        json.dump(data, f, indent=2)
    print("  products manifest: full mosaic marked gone, a swatch took its tiles")


def run_swatch(args, project_dir, out_dir, project, plan):
    """
    Rectify a short run of chunks and leave the result somewhere it can be seen.

    Nothing here is allowed to look like a finished mosaic. A swatch is a few
    chunks of one survey - which is what makes it quick, and what makes it the
    wrong thing to ship - so it goes in its own folder, never into the
    products manifest, and the manifest is told the full mosaic it overwrote
    is gone.
    """
    print("Swatch: %d chunk(s) at %d%% along the survey\n"
          % (args.swatch, round(args.swatch_at * 100)))
    prime_project_for_reuse(project_dir, args, plan)
    clear_rect_outputs(project_dir)
    window = limit_rectify_to_chunks(args.swatch, args.swatch_at)

    run_pingmapper(args, project_dir)

    sonar, _substrate = find_products(project_dir)
    # A labelled swatch is one of a set being compared, so it gets its own
    # folder and leaves the others alone. An unlabelled one is the only
    # swatch there is, and replaces whatever was there.
    root = os.path.join(out_dir, "%s_swatch" % project)
    swatch_dir = os.path.join(root, args.swatch_label) if args.swatch_label else root
    if os.path.isdir(swatch_dir):
        shutil.rmtree(swatch_dir, ignore_errors=True)
    elif not args.swatch_label and os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)
    os.makedirs(swatch_dir, exist_ok=True)

    moved = []
    for path in sonar:
        dest = os.path.join(swatch_dir, os.path.basename(path))
        shutil.move(path, dest)
        moved.append(dest)

    preview = ''
    if moved:
        preview = write_png_preview(
            moved[0], os.path.join(swatch_dir, "%s_swatch.png" % project))

    with open(os.path.join(swatch_dir, 'settings.json'), 'w') as f:
        json.dump({'imageSettings': image_settings(args),
                   'label': args.swatch_label,
                   'chunks': args.swatch,
                   'swatchAt': args.swatch_at,
                   'chunkIds': list(window.get('span', ())),
                   'rerunStage': plan['stage']}, f, indent=2)

    clear_rect_outputs(project_dir)
    forget_full_mosaic(out_dir, project)

    if not moved:
        raise SystemExit("The swatch run produced no mosaic - see the log above.")

    print("\nSwatch: %s" % swatch_dir)
    for path in moved:
        print("  %s" % path)
    if preview:
        print("  %s   <- open this one" % preview)
    print("\nThat is a few chunks, not a mosaic to ship. The full mosaic was "
          "cleared to make it,\nso the survey needs building again once the "
          "settings are right.")
    return preview or moved[0]


# A chunk left with fewer pings than this is folded into the one before it.
MIN_CHUNK_PINGS = 10


def _fold_sliver_chunks_into_their_neighbours():
    """
    Stop PINGMapper cutting a transect into a chunk of one or two pings.

    PINGMapper splits each transect into chunks of nchunk (500) pings and
    gives the remainder a chunk of its own, however small. A transect of 1501
    pings ends in a one-ping chunk, and the rubber-sheet rectifier then asks
    qhull to triangulate two points - "QH6214 not enough points to construct
    initial simplex" - and the whole decode fails with nothing to show for
    it. Whether a recording hits this depends only on where its filters
    happen to break the track.

    The remainder is folded into the chunk before it in the same transect, so
    that chunk runs to at most nchunk + MIN_CHUNK_PINGS - 1 pings. Every ping
    is kept. Chunk numbers stay consecutive: everything after the fold moves
    down by one, which is what PINGMapper would have numbered them had the
    sliver never existed. Harmless if PINGMapper fixes it upstream: with no
    sliver to fold, nothing changes.
    """
    try:
        from pingmapper.class_sonObj import sonObj
    except ImportError:
        return
    original = sonObj._reassignChunks
    if getattr(original, '_folds_slivers', False):
        return

    def reassign(self, sonDF):
        sonDF = original(self, sonDF)
        if sonDF.empty or 'chunk_id' not in sonDF or 'transect' not in sonDF:
            return sonDF
        sizes = sonDF.groupby('chunk_id').size()
        first_of_transect = sonDF.groupby('transect')['chunk_id'].min()
        renumber = {}
        shift = 0
        for chunk in sorted(sizes.index):
            transect = sonDF.loc[sonDF['chunk_id'] == chunk, 'transect'].iloc[0]
            if (sizes[chunk] < MIN_CHUNK_PINGS
                    and chunk != first_of_transect[transect]):
                shift += 1
                renumber[chunk] = chunk - shift        # the previous chunk's new id
            else:
                renumber[chunk] = chunk - shift
        if shift:
            print(f"  (folded {shift} chunk(s) of under {MIN_CHUNK_PINGS} pings "
                  "into the chunk before - PINGMapper cannot rectify them)")
            sonDF['chunk_id'] = sonDF['chunk_id'].map(renumber).astype('int64')
        return sonDF

    reassign._folds_slivers = True
    sonObj._reassignChunks = reassign


def run_pingmapper(args, project_dir):
    from pingmapper.doWork import doWork

    _teach_pingmapper_logger_to_be_a_stream()
    _fold_sliver_chunks_into_their_neighbours()

    params = depth_only_params(args)
    recording = os.path.abspath(args.recording)
    out_dir = os.path.dirname(project_dir)
    proj_name = os.path.basename(project_dir)

    print(f"PINGMapper: {recording}")
    print(f"  project : {project_dir}")
    print(f"  mode    : {'update (reusing the decode)' if params['project_mode'] == 2 else 'full decode'}")
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

    # Gamma and gain are applied inside the EGN stretch, so without one they
    # are dead controls. Easy to miss at the best of times and very easy to
    # miss while turning a swatch round in seconds.
    if (args.tone_gamma != 1.0 or args.tone_gain != 1.0) and (
            not args.egn or args.egn_stretch == 'none'):
        print("Note: gamma and gain are applied inside the EGN stretch. With "
              "EGN off or the\n      stretch set to 'none' they do nothing.\n")

    plan = None
    if args.reuse_decode:
        cached = cached_image_settings(out_dir, project)
        if not os.path.isdir(os.path.join(project_dir, 'meta')):
            raise SystemExit("--reuse-decode needs an already-decoded project; "
                             "none at %s" % project_dir)
        plan = plan_rerun(cached, image_settings(args))
        if plan['stage'] == 'read':
            if args.swatch:
                # A swatch never refuses here, because refusing is the one
                # thing it cannot usefully do: it exists to answer "does this
                # toning look right", and the toning does not depend on which
                # pings were decoded. What it shows is the right toning on the
                # ping set the project already has - so say that plainly and
                # recompute the EGN statistics, which is what the toning does
                # depend on. A build still refuses, because a build ships.
                print("Heads up: %s\n"
                      "decide which pings are decoded, and this swatch is cut "
                      "from the decode already\non disk. The toning is what it "
                      "will be; the coverage is not.\n"
                      % ", ".join(plan['changed']))
                plan = {'stage': 'egn', 'changed': plan['changed']}
            else:
                raise SystemExit(
                    "Those settings change which pings are decoded (" +
                    ", ".join(plan['changed']) + "), so there is nothing to "
                    "reuse.\nRun without --reuse-decode.")
        if plan['stage'] == 'none':
            # Nothing changed - but "nothing to do" only holds if what was
            # asked for is on disk. A swatch has to rectify whatever the
            # settings say, and a build whose mosaic a swatch took still owes
            # that mosaic. In both cases the decode under it is untouched.
            if args.swatch or (args.sonar_mosaic
                               and not find_products(project_dir)[0]):
                plan = {'stage': 'rectify', 'changed': []}
        print("Reusing the decode: from the %s stage onwards%s\n"
              % (plan['stage'],
                 (" (%s changed)" % ", ".join(plan['changed'])
                  if plan['changed'] else "")))
        if args.substrate_map:
            _s, existing = find_products(project_dir)
            if not existing:
                raise SystemExit(
                    "--reuse-decode --substrate-map, but the project has no "
                    "substrate raster to reuse.\nRun without --reuse-decode.")
            print("  reusing %d substrate raster(s) already in the project"
                  % len(existing))

    if args.swatch:
        run_swatch(args, project_dir, out_dir, project, plan)
        return

    if args.keep_existing:
        if not os.path.isdir(project_dir):
            raise SystemExit(f"--keep-existing given but no project at {project_dir}")
    elif args.reuse_decode and plan['stage'] == 'none':
        print("Nothing changed and the mosaic is already on disk - it is "
              "already what these settings make.")
    else:
        if args.reuse_decode:
            prime_project_for_reuse(project_dir, args, plan)
            clear_rect_outputs(project_dir)
        run_pingmapper(args, project_dir)

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
