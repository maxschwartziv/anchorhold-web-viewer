#!/usr/bin/env python3
"""
rock_map.py

Runs RockMapper (https://github.com/PINGEcosystem/RockMapper) over a survey's
side scan mosaics to predict rocky benthic habitat - fines, gravel,
boulder/cobble and bedrock - and returns the raster the app overlays.

RockMapper lives in its own conda environment (`rockmapper`, created by
`python -m pinginstaller rockmapper`) because it pulls in its own torch/model
stack. This script is what runs *inside* that environment; the pipeline invokes
it with that interpreter, the same way depth_csv_from_sonar.py is run inside the
PINGMapper env.

    python rock_map.py --sonar-dir <mosaics> --out-dir <out> --project <name>

Outputs land in <out>/<project>/ as RockMapper writes them, and the paths of the
raster and shapefile are echoed as a JSON line plus written to
<out>/<project>_rock.json for the caller to pick up.
"""

import argparse
import glob
import json
import os
import shutil
import sys

# RockMapper's classes, in the order its raster encodes them, taken from the
# model config's MY_CLASS_NAMES. Six, not four: the older four-class list
# ('Fines', 'Gravel', 'Boulder/Cobble', 'Bedrock') was shifted by one against
# what the model actually writes, so a raster that is 93% "Other" - ordinary
# soft bottom, which is most of any lake - read as 93% "Gravel".
ROCK_CLASSES = ['NoData', 'Shadow', 'Other', 'Gravel', 'Cobble Boulder', 'Bedrock']


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Predict rocky habitat from sonar mosaics.")
    p.add_argument("--sonar-dir", default="",
                   help="folder holding the side scan mosaic GeoTIFFs")
    p.add_argument("--sonar", nargs="+", default=[],
                   help="individual mosaic GeoTIFFs, when they are not a whole folder "
                        "(they are staged into one before RockMapper reads them)")
    p.add_argument("--out-dir", required=True, help="where RockMapper writes its project")
    p.add_argument("--project", required=True, help="project name (subfolder under --out-dir)")
    p.add_argument("--epsg", type=int, default=0,
                   help="output EPSG; 0 = take it from the first mosaic")
    p.add_argument("--model-dir", default=os.environ.get("ROCKMAPPER_MODEL_DIR", ""),
                   help="Segmentation Gym model folder; downloaded on first use if absent")
    p.add_argument("--model", default=DEFAULT_SEG_MODEL,
                   help="model name to fetch when --model-dir is not given")
    p.add_argument("--window-m", type=float, default=18.0,
                   help="prediction window size in metres (square)")
    p.add_argument("--window-stride", type=int, default=6)
    p.add_argument("--min-area-percent", type=float, default=0.75)
    p.add_argument("--min-patch-size", type=int, default=5)
    p.add_argument("--smooth-tol-m", type=float, default=0.3)
    p.add_argument("--batch-size", type=int, default=30)
    p.add_argument("--threads", type=float, default=0.75)
    p.add_argument("--keep-intermediate", action="store_true")
    p.add_argument("--no-raster", action="store_true",
                   help="only write the shapefile (the app overlay needs the raster)")
    return p.parse_args(argv)


# Segmentation model RockMapper predicts with. The package ships no weights;
# they come from the project's GitHub releases, same URL its own code uses.
DEFAULT_SEG_MODEL = 'RockMapper_20251117_v2'
MODEL_URL = 'https://github.com/PINGEcosystem/RockMapper/releases/download/models/{model}.zip'


def default_model_root():
    """Where to keep downloaded weights: alongside the package, else in the user profile."""
    try:
        import rockmapper
        base = os.path.join(os.path.dirname(os.path.abspath(rockmapper.__file__)), 'models')
        os.makedirs(base, exist_ok=True)
        return base
    except Exception:
        base = os.path.join(os.path.expanduser('~'), '.rockmapper_models')
        os.makedirs(base, exist_ok=True)
        return base


def ensure_model(model_dir):
    """
    Make sure [model_dir] holds a Segmentation Gym model, downloading it if not.

    The layout the predictor wants is a config .json plus weights .h5; the release
    zip already has that shape.
    """
    import zipfile
    import requests

    if os.path.isdir(model_dir) and os.listdir(model_dir):
        return model_dir

    model = os.path.basename(model_dir.rstrip(os.sep))
    url = MODEL_URL.format(model=model)
    os.makedirs(model_dir, exist_ok=True)
    archive = model_dir.rstrip(os.sep) + '.zip'
    print(f"Downloading model {model}\n  {url}")

    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(archive, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
    if not zipfile.is_zipfile(archive):
        head = open(archive, 'rb').read(200)
        os.remove(archive)
        raise SystemExit(f"Model download was not a zip (server said: {head!r})")
    with zipfile.ZipFile(archive) as z:
        z.extractall(model_dir)
    os.remove(archive)
    print(f"  extracted to {model_dir}")
    return model_dir


def epsg_of(tif):
    """EPSG of a mosaic, so RockMapper predicts in the survey's own projection."""
    try:
        import rasterio
        with rasterio.open(tif) as src:
            if src.crs is not None:
                code = src.crs.to_epsg()
                if code:
                    return int(code)
    except Exception as exc:
        print(f"  could not read CRS from {os.path.basename(tif)}: {exc}")
    return 0


def find_model_dir(model_name=DEFAULT_SEG_MODEL):
    """Path of the segmentation model, fetching the weights on first use."""
    return ensure_model(os.path.join(default_model_root(), model_name))


def find_outputs(project_dir):
    rasters = sorted(glob.glob(os.path.join(project_dir, "**", "*.tif"), recursive=True))
    shapes = sorted(glob.glob(os.path.join(project_dir, "**", "*.shp"), recursive=True))
    return rasters, shapes


def stage_mosaics(paths, out_dir, project):
    """
    Gather named mosaics into one folder for RockMapper, which only takes a
    directory. Hard links first so a 100 MB mosaic set costs nothing and no time;
    a copy is the fallback when the target sits on another volume.

    Returns the staged folder.
    """
    staged = os.path.join(out_dir, f"{project}_mosaics")
    if os.path.isdir(staged):
        shutil.rmtree(staged)
    os.makedirs(staged, exist_ok=True)

    for number, path in enumerate(paths):
        source = os.path.abspath(path)
        if not os.path.isfile(source):
            raise SystemExit(f"No such mosaic: {source}")
        target = os.path.join(staged, os.path.basename(source))
        # Distinct folders can hold same-named mosaics; keep them apart.
        if os.path.exists(target):
            stem, ext = os.path.splitext(os.path.basename(source))
            target = os.path.join(staged, f"{stem}_{number}{ext}")
        try:
            os.link(source, target)
        except OSError:
            try:
                os.symlink(source, target)
            except OSError:
                shutil.copy2(source, target)
    print(f"Staged {len(paths)} mosaic(s) in {staged}")
    return staged


def main(argv=None):
    args = parse_args(argv)

    if args.sonar:
        # RockMapper reads a folder, so named files are staged into one of their
        # own. That also keeps unrelated .tif files sitting in the same folder
        # out of the run - only what was asked for is predicted on.
        sonar_dir = stage_mosaics(args.sonar, os.path.abspath(args.out_dir), args.project)
    elif args.sonar_dir:
        sonar_dir = os.path.abspath(args.sonar_dir)
    else:
        raise SystemExit("Pass --sonar-dir or --sonar")
    if not os.path.isdir(sonar_dir):
        raise SystemExit(f"No such folder: {sonar_dir}")
    mosaics = sorted(glob.glob(os.path.join(sonar_dir, "*.tif")))
    if not mosaics:
        raise SystemExit(f"No .tif mosaics in {sonar_dir}")

    epsg = args.epsg or epsg_of(mosaics[0])
    if not epsg:
        raise SystemExit("Could not determine an EPSG; pass --epsg explicitly.")

    model_dir = args.model_dir or find_model_dir(args.model)
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    print(f"RockMapper: {len(mosaics)} mosaic(s) from {sonar_dir}")
    print(f"  project {args.project}  epsg {epsg}  window {args.window_m} m")
    print(f"  model   {model_dir or '(package default)'}\n")

    from rockmapper.rock_mapper import do_work

    do_work(
        inDir=sonar_dir,
        outDirTop=out_dir,
        projName=args.project,
        mapRast=not args.no_raster,
        mapShp=True,
        epsg=epsg,
        windowSize_m=(args.window_m, args.window_m),
        window_stride=args.window_stride,
        minArea_percent=args.min_area_percent,
        threadCnt=args.threads,
        mosaicFileType='.tif',
        modelDir=model_dir,
        predBatchSize=args.batch_size,
        deleteIntData=not args.keep_intermediate,
        minPatchSize=args.min_patch_size,
        smoothShp=True,
        smoothTol_m=args.smooth_tol_m,
    )

    project_dir = os.path.join(out_dir, args.project)
    rasters, shapes = find_outputs(project_dir)
    print(f"\nRock raster : {len(rasters)}")
    for f in rasters:
        print(f"  {f}")
    print(f"Rock polygons: {len(shapes)}")
    for f in shapes:
        print(f"  {f}")

    manifest = os.path.join(out_dir, f"{args.project}_rock.json")
    payload = {"raster": rasters[0] if rasters else "",
               "rasters": rasters, "shapefiles": shapes,
               "classes": ROCK_CLASSES, "epsg": epsg}
    with open(manifest, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nRock manifest: {manifest}")

    if not rasters:
        raise SystemExit("RockMapper produced no raster - rerun without --no-raster, "
                         "or check its log above.")


if __name__ == '__main__':
    sys.exit(main())
