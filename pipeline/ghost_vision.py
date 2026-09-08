#!/usr/bin/env python3
"""
ghost_vision.py

Runs GhostVision (https://github.com/PINGEcosystem/GhostVision) over a sonar
recording to find objects sitting on the bottom - it was trained on derelict
crab pots - and returns the detections as points the apps can draw.

GhostVision lives in its own conda environment (`ghostvision`, created from the
`ghostvision_install.yml` in its repo) because it pulls in its own torch and
model stack. This script is what runs *inside* that environment; the pipeline
invokes it with that interpreter, the same way rock_map.py is run inside the
`rockmapper` env and depth_csv_from_sonar.py inside `ping`.

It does what GhostVision's own window does once you press Submit, minus the
window: build a PINGMapper project with side scan exported the way the detector
wants it, run the detector over moving windows of each sonogram, track what it
finds across the overlaps, and write the survivors out. Note that this is a
*second* decode of the recording - the detector needs 16-bit water-column-
present tiles, which is not what the chart build makes.

    python ghost_vision.py --recording R00021.DAT --out-dir out --project lake

Outputs land in <out-dir>/ as GhostVision writes them, plus:

    <out-dir>/<project>_detections.geojson   the points, WGS84, one per object
    <out-dir>/<project>_ghost.json           where everything ended up
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

# The detector was trained on crab pots in Delaware coastal water. Everything it
# finds elsewhere is "an object on the bottom that looks like one", which is
# worth saying out loud wherever the results are shown.
DEFAULT_MODEL = ""              # empty: whichever model GhostVision offers first

USER_DIR = os.path.expanduser("~")
GV_UTILS_DIR = os.path.join(USER_DIR, ".ghostvision")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Find bottom objects in a sonar recording.")
    p.add_argument("--recording", required=True,
                   help="the sonar recording (.DAT/.sl2/.sl3/.RSD/.svlog/.jsf/.xtf)")
    p.add_argument("--out-dir", required=True, help="where the project is written")
    p.add_argument("--project", required=True, help="project name (a folder under --out-dir)")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help="model alias to detect with (default: GhostVision's first)")

    d = p.add_argument_group("detection")
    d.add_argument("--confidence", type=float, default=0.5,
                   help="how sure the model must be to call something a detection "
                        "(0-1, default 0.5)")
    d.add_argument("--iou-threshold", type=float, default=0.1,
                   help="how much two boxes may overlap before they are treated as "
                        "one object (default 0.1)")
    d.add_argument("--no-track", action="store_true",
                   help="do not track objects across overlapping windows; every "
                        "window's detections stand on their own")
    d.add_argument("--track-count", type=int, default=17,
                   help="how many windows in a row must see an object before it "
                        "counts (default 17)")
    d.add_argument("--alpha", type=float, default=0.45,
                   help="weight given to a track's best look against its average "
                        "when scoring it (0-1, default 0.45)")
    d.add_argument("--window-stride", type=float, default=0.05,
                   help="how far the moving window steps, as a fraction of its own "
                        "length (default 0.05)")
    d.add_argument("--no-moving-window", action="store_true",
                   help="one pass of non-overlapping windows instead")

    t = p.add_argument_group("what the recording is worth reading")
    t.add_argument("--min-speed", type=float, default=0.0,
                   help="drop pings slower than this (m/s)")
    t.add_argument("--max-speed", type=float, default=0.0)
    t.add_argument("--max-heading-deviation", type=float, default=0.0,
                   help="drop pings whose heading swings more than this (deg)")
    t.add_argument("--max-heading-distance", type=float, default=0.0,
                   help="distance the heading change is judged over (m)")
    t.add_argument("--crop-range", type=float, default=0.0,
                   help="crop the range to this many metres; 0 keeps it all")
    t.add_argument("--no-egn", action="store_true",
                   help="skip the gain normalisation before detection")

    p.add_argument("--images", action="store_true",
                   help="also write the detection images")
    p.add_argument("--video", action="store_true",
                   help="also write the detection videos (implies --images)")
    p.add_argument("--gpx-to-card", action="store_true",
                   help="write the waypoints back beside the recording, for the "
                        "sounder's SD card")
    p.add_argument("--waypoint-prefix", default="GV",
                   help="prefix for the exported waypoint names (default GV)")
    p.add_argument("--threads", type=float, default=0.5)
    return p.parse_args(argv)


# ── GhostVision's own plumbing ──────────────────────────────────────────────

def pingmapper_dowork():
    """
    GhostVision's handle on PINGMapper, however this install exposes it.

    Its own detect.py has a private helper for this because the import moved
    between versions; asking it first means following it wherever it went, and
    the direct import is the fallback for an install that no longer has it.
    """
    try:
        from ghostvision.detect import _get_pingmapper_dowork
        return _get_pingmapper_dowork()
    except (ImportError, AttributeError):
        from pingmapper.doWork import doWork
        return doWork


def available_models():
    """Model aliases GhostVision can detect with, fetching them on first use."""
    from ghostvision.detect import get_avail_models
    return get_avail_models()


def pick_model(alias: str):
    models = available_models()
    if not models:
        raise SystemExit("GhostVision has no detection models available. Run "
                         "`python -m ghostvision rf-download` in its environment.")
    if alias and alias in models:
        return alias, models[alias]
    if alias:
        raise SystemExit(f"No model called {alias}. Available: "
                         + ", ".join(sorted(models)))
    first = sorted(models)[0]
    return first, models[first]


def sonar_params(args) -> dict:
    """
    The PINGMapper parameters GhostVision's own window would have set.

    Copied from its detect.py rather than invented: the detector wants side
    scan only, water column present, 16-bit tiles written as .tif, and the
    rectification forced - a project built any other way gives it nothing to
    look at.
    """
    return {
        "project_mode": 1,
        "nchunk": 500,
        "cropRange": float(args.crop_range),
        "threadCnt": args.threads,
        "aoi": False,
        "max_heading_deviation": float(args.max_heading_deviation),
        "max_heading_distance": float(args.max_heading_distance),
        "min_speed": float(args.min_speed),
        "max_speed": float(args.max_speed),
        "time_table": False,
        "x_offset": 0.0,
        "y_offset": 0.0,
        "wcp": True,
        "export_16bit": True,
        "export_colormap_uint8": True,
        "tileFile": ".tif",
        "egn": not args.no_egn,
        "egn_stretch": 2,
        "egn_stretch_factor": 0.5,
        "rectMethod": "COG",
        "force_rectify": True,
        "side_scan_only": True,
        "detectDep": 0,
    }


def detection_params(args, project_dir: str, recording: str, model) -> dict:
    """What crabpots_master_func wants, on top of the sonar parameters."""
    nchunk = 500
    params = dict(sonar_params(args))
    params.update({
        "projDir": project_dir,
        "inFile": recording,
        "rf_model": model,
        "gpxToHum": bool(args.gpx_to_card),
        "sdDir": os.path.dirname(os.path.abspath(recording)),
        "confidence": float(args.confidence),
        "alpha": float(args.alpha),
        "iou_threshold": float(args.iou_threshold),
        "wptPrefix": args.waypoint_prefix,
        "stride": int(float(args.window_stride) * nchunk),
        "moving_window": not args.no_moving_window,
        "window_stride": float(args.window_stride),
        "export_vid": bool(args.video),
        "export_image": bool(args.images or args.video),
        "inference_track": not args.no_track,
        "tracker_cnt": max(1, int(args.track_count)),
    })
    if params["export_vid"] and not args.images:
        params["delete_image"] = True
    return params


# ── Results ─────────────────────────────────────────────────────────────────

def final_results(out_dir: str):
    """The shapefile, csv and gpx GhostVision collates at the end, if they exist."""
    folder = os.path.join(out_dir, "0_GhostVision_FinalResults")
    found = {}
    for key, pattern in (("shapefile", "*.shp"), ("csv", "*.csv"), ("gpx", "*.gpx")):
        hits = sorted(glob.glob(os.path.join(folder, pattern)))
        if hits:
            found[key] = hits[0]
    return found


def to_geojson(shapefile: str, out_path: str) -> dict:
    """
    The detections as GeoJSON in WGS84, which is what the apps draw.

    Converted here, inside the environment that has geopandas, so nothing
    downstream needs a geo stack to read a handful of points.
    """
    import geopandas as gpd

    gdf = gpd.read_file(shapefile)
    if gdf.crs is not None and str(gdf.crs).upper() != "EPSG:4326":
        gdf = gdf.to_crs("EPSG:4326")
    # Times and other objects do not survive JSON; the app wants the score, the
    # class and something to call each one.
    keep = [c for c in ("name", "tracker_id", "class_name", "class", "confidence",
                        "score", "conf", "quality", "quality_tier", "depth_m")
            if c in gdf.columns]
    slim = gdf[keep + ["geometry"]].copy()
    for column in keep:
        slim[column] = slim[column].astype(str)
    slim.to_file(out_path, driver="GeoJSON")
    return {"count": int(len(slim)), "fields": keep}


def main(argv=None):
    args = parse_args(argv)
    recording = os.path.abspath(args.recording)
    if not os.path.isfile(recording):
        raise SystemExit(f"No such recording: {recording}")
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    alias, model = pick_model(args.model)
    print(f"GhostVision on {os.path.basename(recording)}")
    print(f"  model      {alias}")
    print(f"  confidence {args.confidence}  iou {args.iou_threshold}"
          + ("" if args.no_track else f"  seen in {args.track_count} windows"))
    print(f"  project    {os.path.join(out_dir, args.project)}")
    print("")

    doWork = pingmapper_dowork()
    runs = doWork(in_file=recording, in_dir=None, in_files=None, out_dir=out_dir,
                  proj_name=args.project, prefix="", suffix="", batch=False,
                  preserve_subdirs=False, params=sonar_params(args),
                  script_path=os.path.abspath(__file__))

    from ghostvision.main_crabDetect import crabpots_master_func, export_final_results

    detected = 0
    for run in runs:
        if not run.get("success"):
            print(f"  PINGMapper could not read {run.get('inFile')}")
            continue
        project_dir = run.get("projDir")
        if not project_dir or not os.path.isdir(project_dir):
            continue
        crabpots_master_func(**detection_params(args, project_dir, recording, model))
        detected += 1

    if not detected:
        raise SystemExit("Nothing was detected on: PINGMapper produced no project.")

    export_final_results(out_dir, os.path.basename(out_dir))
    results = final_results(out_dir)

    manifest_path = os.path.join(out_dir, f"{args.project}_ghost.json")
    payload = {"recording": recording, "project": args.project,
               "model": alias, "confidence": args.confidence,
               "tracked": not args.no_track, "results": results}
    if "shapefile" in results:
        geojson = os.path.join(out_dir, f"{args.project}_detections.geojson")
        payload.update(to_geojson(results["shapefile"], geojson))
        payload["geojson"] = geojson
        print(f"\nDetections: {payload['count']}")
        print(f"  {geojson}")
    else:
        payload["count"] = 0
        print("\nNo detections were exported - nothing cleared the filters.")

    with open(manifest_path, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
