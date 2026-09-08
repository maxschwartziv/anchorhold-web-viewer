#!/usr/bin/env python3
"""
The chart library the browser app draws from.

The browser app keeps its own charts. Everything it can draw lives under one
folder, and nothing outside that folder is consulted:

    web_charts/catalog.json     the surveys, in the order they are offered
    web_charts/<survey>/        that survey's tiles, grids, contours, legends

Charts come from a folder the pipeline has built - output/<survey> - which
already uses these file names. Files are hard-linked where the filesystem
allows, so adding a 200 MB survey to the library costs no extra disk.

    python pipeline/web_charts.py                    # menu: add or remove
    python pipeline/web_charts.py list
    python pipeline/web_charts.py add output/creve-coeur-lake2
    python pipeline/web_charts.py add output/noatak --name "Noatak River"
    python pipeline/web_charts.py remove creve-coeur-lake2
    python pipeline/web_charts.py default noatak
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import workspace                   # neutral home for builds
LIBRARY = os.path.join(ROOT, "web_charts")
CATALOG = os.path.join(LIBRARY, "catalog.json")
BUILD_DIR = workspace.output_dir()

# The file names a survey folder may hold. A chart is whatever subset of these
# is present: a survey with no substrate simply has no substrate layer.
TILES = {"bathymetry": "bathymetry.mbtiles", "sonar": "sonar.mbtiles",
         "substrate": "substrate.mbtiles", "rock": "rock.mbtiles"}
DATA = {"contours": "contours.geojson", "shallowBands": "shallow_bands.geojson",
        "boundary": "boundary.geojson", "track": "track.geojson",
        "detections": "detections.geojson"}
GRIDS = {"depthGrid": "depth_grid", "substrateGrid": "substrate_grid"}
LEGENDS = {"depth": "depth_legend.png", "substrate": "substrate_legend.png",
           "rock": "rock_legend.png"}

# Every chart carries its own description, so a chart folder copied in from
# anywhere - a zip off a release page, a USB stick - is a complete chart. The
# catalog then only decides the order they are offered in and which opens first.
CHART_JSON = "chart.json"


def chart_files() -> list:
    """Every file name the library recognises, for copying and for tidying up."""
    names = list(TILES.values()) + list(DATA.values()) + list(LEGENDS.values())
    for base in GRIDS.values():
        names += [f"{base}.bin", f"{base}.json"]
    return names


# -- The catalog ------------------------------------------------------------

def load() -> dict:
    """The library catalog; an empty one if the library does not exist yet."""
    if not os.path.isfile(CATALOG):
        return {"defaultLocationId": None, "locations": []}
    with open(CATALOG, encoding="utf-8") as fh:
        return json.load(fh)


def save(catalog: dict) -> None:
    os.makedirs(LIBRARY, exist_ok=True)
    with open(CATALOG, "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def chart_dir(loc_id: str) -> str:
    return os.path.join(LIBRARY, loc_id)


def file_path(loc_id: str, name: str):
    """
    Absolute path of one file inside a chart, or None.

    Names arrive from HTTP requests, so anything carrying a separator is
    refused rather than resolved.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    path = os.path.join(chart_dir(loc_id), name)
    return path if os.path.isfile(path) else None


def mbtiles_info(path: str) -> dict:
    """Zoom range and WGS84 bounds of an MBTiles file, read from its metadata."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        meta = dict(con.execute("SELECT name, value FROM metadata").fetchall())
        zmin, zmax = con.execute(
            "SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles").fetchone()
    finally:
        con.close()
    info = {"minzoom": int(zmin or 0), "maxzoom": int(zmax or 0)}
    if meta.get("bounds"):
        try:
            info["bounds"] = [float(v) for v in meta["bounds"].split(",")]
        except ValueError:
            pass
    return info


def chart_meta(loc_id: str) -> dict:
    """
    What a chart says about itself: name, centre, zoom, tide.

    Read from the chart's own chart.json where it has one, and otherwise worked
    out from the files - so a chart folder from anywhere still opens somewhere
    sensible instead of at 0N 0E.
    """
    meta = {}
    path = file_path(loc_id, CHART_JSON)
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = {}
    meta.setdefault("name", pretty_name(loc_id))
    meta.setdefault("zoom", 16.0)
    meta.setdefault("tide", {"mode": "none"})
    if not meta.get("center"):
        centre = derive_center(chart_dir(loc_id))
        meta["center"] = ({"lat": round(centre[0], 6), "lon": round(centre[1], 6)}
                          if centre else {"lat": 0.0, "lon": 0.0})
    return meta


def write_chart_meta(loc_id: str, entry: dict) -> None:
    """
    Leave the description inside the chart, where it travels with the files.

    The survey record goes in too when there is one: provenance left behind
    in a catalog would be lost the moment the chart was copied to another
    machine - exactly when someone wants to know where the depths are from.
    """
    keep = {"id": loc_id, "name": entry.get("name"), "center": entry.get("center"),
            "zoom": entry.get("zoom"), "tide": entry.get("tide")}
    if entry.get("source"):
        keep["source"] = entry["source"]
    if entry.get("builtFrom"):
        # Where the files came from, so a rebuild there can be noticed.
        keep["builtFrom"] = entry["builtFrom"]
    if entry.get("source"):
        keep["source"] = entry["source"]
    with open(os.path.join(chart_dir(loc_id), CHART_JSON), "w", encoding="utf-8") as fh:
        json.dump(keep, fh, indent=2, ensure_ascii=False)
        fh.write(chr(10))


def describe(loc_id: str, entry: dict = None):
    """
    One chart as the browser wants it - only the layers actually on disk - or
    None if the chart has nothing left to draw.
    """
    if not os.path.isdir(chart_dir(loc_id)):
        return None
    entry = entry or chart_meta(loc_id)

    layers = {}
    for layer, name in TILES.items():
        path = file_path(loc_id, name)
        if path:
            layers[layer] = mbtiles_info(path)

    data = {}
    for key, name in DATA.items():
        if file_path(loc_id, name):
            data[key] = name
    for key, base in GRIDS.items():
        if file_path(loc_id, f"{base}.bin") and file_path(loc_id, f"{base}.json"):
            data[key] = base

    legends = {key: name for key, name in LEGENDS.items() if file_path(loc_id, name)}

    if not layers and not data:
        return None

    centre = entry.get("center") or {}
    return {
        "id": loc_id,
        "name": entry.get("name", loc_id),
        "lat": centre.get("lat"),
        "lon": centre.get("lon"),
        "zoom": entry.get("zoom", 16),
        "tide": entry.get("tide") or {"mode": "none"},
        "source": entry.get("source") or {},
        "mb": folder_mb(chart_dir(loc_id)),
        "rev": chart_revision(loc_id),
        "layers": layers,
        "data": data,
        "legends": legends,
    }


def chart_revision(loc_id: str) -> str:
    """
    A short stamp that changes whenever this chart's files do.

    The browser caches tiles by URL and serves them cache-first, because on the
    water there is usually no network to check against. That is right for a
    chart that never changes and wrong for one that has just been rebuilt: the
    URLs are identical, so the old tiles are served for ever and the rebuild is
    invisible. Hanging this on the end of every chart URL makes a rebuilt chart
    a cache miss, which is what it actually is.
    """
    stamp = hashlib.md5()
    for name in sorted(os.listdir(chart_dir(loc_id))):
        path = os.path.join(chart_dir(loc_id), name)
        if os.path.isfile(path) and name != CHART_JSON:
            info = os.stat(path)
            stamp.update(f"{name}:{info.st_size}:{int(info.st_mtime)}".encode())
    return stamp.hexdigest()[:8]


def build_catalog() -> dict:
    """
    The whole library, in the order the app should offer it.

    A chart whose files were deleted by hand drops out here, rather than
    reaching the app as something that cannot be drawn.
    """
    catalog = load()
    locations = []
    for entry in catalog.get("locations", []):
        found = describe(entry["id"], entry)
        if found:
            locations.append(found)

    # Charts dropped into the library by hand come last, in name order.
    known = {loc["id"] for loc in locations}
    for loc_id in sorted(os.listdir(LIBRARY) if os.path.isdir(LIBRARY) else []):
        # A dot folder is the library's own scratch space - an upload being
        # received, say - and never a chart.
        if loc_id.startswith(".") or loc_id in known:
            continue
        if not os.path.isdir(chart_dir(loc_id)):
            continue
        adopted = describe(loc_id)
        if adopted:
            locations.append(adopted)

    default = catalog.get("defaultLocationId")
    if not any(loc["id"] == default for loc in locations):
        default = locations[0]["id"] if locations else None
    return {"defaultLocationId": default, "locations": locations}


# -- Adding and removing ----------------------------------------------------


def _unlink(path: str, attempts: int = 12, pause: float = 0.5) -> None:
    """
    Delete a file, waiting out whatever is briefly holding it.

    On Windows a file written seconds ago is often still open - a virus scanner
    reading it, an indexer, a chart server that has not quite let go. The hold
    lasts a moment; failing the whole operation because of it makes adding a
    freshly built chart a coin toss.
    """
    for attempt in range(attempts):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(pause)


def link_or_copy(src: str, dst: str) -> str:
    """
    Hard-link a chart file into the library, copying only if that fails.

    A link means the library costs nothing beyond the build it came from, and
    deleting either side leaves the other intact. Links need a single
    filesystem, so a build on another drive falls back to a copy.
    """
    if os.path.exists(dst):
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
            return "already there"       # never delete the file being added
        _unlink(dst)
    try:
        os.link(src, dst)
        return "linked"
    except OSError:
        shutil.copy2(src, dst)
        return "copied"


def _centre_is_on_the_chart(centre, folder: str) -> bool:
    """Whether a recorded centre still falls inside the chart's own tiles."""
    if not centre:
        return False
    for name in TILES.values():
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        try:
            bounds = mbtiles_info(path).get("bounds")
        except sqlite3.Error:
            continue
        if bounds and len(bounds) == 4:
            west, south, east, north = bounds
            return (west <= centre.get("lon", 0.0) <= east
                    and south <= centre.get("lat", 0.0) <= north)
    return True                     # no tiles to judge against; leave it alone


def derive_center(folder: str):
    """Middle of the charted water, from whichever tile set is present."""
    for name in TILES.values():
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        try:
            bounds = mbtiles_info(path).get("bounds")
        except sqlite3.Error:
            continue
        if bounds and len(bounds) == 4:
            west, south, east, north = bounds
            return (south + north) / 2, (west + east) / 2
    return None


def pretty_name(loc_id: str) -> str:
    return re.sub(r"[-_]+", " ", loc_id).strip().title()


def looks_like_chart(folder: str) -> bool:
    return any(os.path.isfile(os.path.join(folder, name)) for name in chart_files())


def add(source: str, loc_id: str = "", name: str = "", zoom: float = 16.0,
        tide=None, make_default: bool = False, log=print) -> str:
    """
    Put one built survey into the library. Returns its id.

    Re-adding a survey refreshes its files in place and keeps its catalog
    entry, so rebuilding a chart does not mean re-typing its name.
    """
    source = os.path.abspath(source)
    if not os.path.isdir(source):
        raise SystemExit(f"No such folder: {source}")
    if not looks_like_chart(source):
        raise SystemExit(f"{source} holds none of the files a chart is made of.")

    loc_id = loc_id or os.path.basename(source.rstrip("/\\"))
    folder = chart_dir(loc_id)
    os.makedirs(folder, exist_ok=True)

    # Adding a chart that is already unpacked in the library - from a release
    # zip, say - only records it; there is nothing to move.
    in_place = os.path.normcase(source) == os.path.normcase(os.path.abspath(folder))

    copied = linked = 0
    for filename in chart_files():
        src = os.path.join(source, filename)
        if not os.path.isfile(src):
            continue
        how = link_or_copy(src, os.path.join(folder, filename))
        linked += how == "linked"
        copied += how == "copied"
        log(f"    {filename}  ({how})")
    if not linked and not copied and not in_place:
        raise SystemExit("Nothing to add - that folder has no chart files.")

    catalog = load()
    existing = next((e for e in catalog["locations"] if e["id"] == loc_id), None)
    # An unpacked chart already says what it is; keep that unless told otherwise.
    entry = existing or dict(chart_meta(loc_id), id=loc_id)
    entry["name"] = name or entry.get("name") or pretty_name(loc_id)
    entry.setdefault("tide", {"mode": "none"})
    if tide:
        entry["tide"] = tide
    entry["zoom"] = zoom or entry.get("zoom", 16.0)
    centre = derive_center(folder)
    if centre and not _centre_is_on_the_chart(entry.get("center"), folder):
        # Re-adding a chart that was rebuilt somewhere else - a different
        # recording, a trimmed one - would otherwise keep the centre the old
        # one had, and the app would open next to the survey instead of on it.
        if entry.get("center"):
            log(f"    centre moved to {centre[0]:.6f}, {centre[1]:.6f} "
                "(the old one is not on this chart)")
        entry["center"] = {"lat": round(centre[0], 6), "lon": round(centre[1], 6)}
    entry.setdefault("center", {"lat": 0.0, "lon": 0.0})

    if not in_place:
        entry["builtFrom"] = source
    if not existing:
        catalog["locations"].append(entry)
    if make_default or not catalog.get("defaultLocationId"):
        catalog["defaultLocationId"] = loc_id
    save(catalog)
    write_chart_meta(loc_id, entry)

    log(f"  {entry['name']}: {linked} linked, {copied} copied"
        if not in_place else f"  {entry['name']}: already in the library")
    return loc_id


def write_detections(loc_id: str, payload: dict) -> int:
    """
    File a detector's findings with the chart they belong to.

    GhostVision writes its detections wherever it ran, which is rarely this
    machine and never the chart library. Everything about the file is checked
    here rather than trusted: it arrives over HTTP, and a chart is only as
    good as the worst thing anyone ever put into it.

    Returns how many detections were kept.
    """
    if not loc_id or "/" in loc_id or "\\" in loc_id or loc_id.startswith("."):
        raise ValueError("That is not a chart name.")
    folder = chart_dir(loc_id)
    if not os.path.isdir(folder):
        raise ValueError(f"There is no chart called {loc_id}.")
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise ValueError("That is not a GeoJSON FeatureCollection.")
    features = payload.get("features")
    if not isinstance(features, list):
        raise ValueError("That GeoJSON has no features in it.")

    kept = []
    for feature in features[:20000]:
        geometry = (feature or {}).get("geometry") or {}
        if geometry.get("type") != "Point":
            continue
        coords = geometry.get("coordinates") or []
        if len(coords) < 2:
            continue
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        # A detector fed the wrong projection produces coordinates that are
        # numbers and nothing else. Off the globe is off the chart.
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            continue
        props = feature.get("properties") or {}
        kept.append({
            "type": "Feature",
            "properties": {
                "tracker_id": str(props.get("tracker_id", len(kept)))[:40],
                "class_name": str(props.get("class_name", "Object"))[:60],
                "confidence": str(props.get("confidence", ""))[:12],
            },
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
        })
    if not kept:
        raise ValueError("No point detections in that file.")

    path = os.path.join(folder, DATA["detections"])
    # Charts are hard-linked from the build that made them, so writing
    # through this name would edit the pipeline's own output as well.
    # Break the link first and write a new file into the library.
    if os.path.exists(path):
        _unlink(path)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": kept}, fh)
        fh.write("\n")
    return len(kept)


def clear_detections(loc_id: str) -> bool:
    """Take a detector's findings back off a chart; False if it had none."""
    if not loc_id or "/" in loc_id or "\\" in loc_id:
        raise ValueError("That is not a chart name.")
    path = os.path.join(chart_dir(loc_id), DATA["detections"])
    if not os.path.isfile(path):
        return False
    _unlink(path)
    return True


def remove(loc_id: str, log=print) -> None:
    """Take a chart out of the library, files and all."""
    catalog = load()
    before = len(catalog["locations"])
    catalog["locations"] = [e for e in catalog["locations"] if e["id"] != loc_id]
    folder = chart_dir(loc_id)
    if before == len(catalog["locations"]) and not os.path.isdir(folder):
        raise SystemExit(f"No chart called {loc_id} in the library.")
    if os.path.isdir(folder):
        # Renamed first, deleted after. A file another program holds open
        # cannot be renamed either, so this finds out before anything is gone:
        # deleting file by file and stopping at the first locked one leaves a
        # chart with its tiles and no contours, still listed, drawing wrongly.
        # Each name is reported individually rather than hiding behind
        # "the process cannot access the file".
        moved = []
        stuck = []
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            staged = path + ".removing"
            try:
                os.replace(path, staged)
                moved.append(staged)
            except OSError as exc:
                stuck.append(f"{name} ({exc.strerror or exc})")
        if stuck:
            for staged in moved:
                os.replace(staged, staged[: -len(".removing")])
            raise OSError(
                "these files are open in another program: " + "; ".join(stuck) +
                chr(10) +
                "  Stop the chart server (run_web_app.bat) and try again, or "
                "remove it in the app itself: Settings > Charts, which asks the "
                "server to let go first. Nothing has been deleted."
                )
        for staged in moved:
            _unlink(staged)
        os.rmdir(folder)
    if catalog.get("defaultLocationId") == loc_id:
        catalog["defaultLocationId"] = (catalog["locations"][0]["id"]
                                        if catalog["locations"] else None)
    save(catalog)
    log(f"Removed {loc_id}.")


def set_default(loc_id: str, log=print) -> None:
    catalog = load()
    if not any(e["id"] == loc_id for e in catalog["locations"]):
        raise SystemExit(f"No chart called {loc_id} in the library.")
    catalog["defaultLocationId"] = loc_id
    save(catalog)
    log(f"{loc_id} is the chart the app opens on.")


# -- What is where ----------------------------------------------------------

def stale_files(loc_id: str) -> list:
    """
    Chart files that no longer match the build they were taken from.

    The library hard-links what the pipeline wrote, so a rebuild that writes
    a file in place - the geojson and the grids - reaches the library on its
    own, while one that replaces a file - every MBTiles - leaves the library
    holding the old inode. Half a chart updates silently, which is worse than
    none of it: the depths are new and the sonar under them is not.
    """
    meta = chart_meta(loc_id)
    source = meta.get("builtFrom")
    if not source or not os.path.isdir(source):
        return []
    behind = []
    for name in chart_files():
        here = os.path.join(chart_dir(loc_id), name)
        there = os.path.join(source, name)
        if not os.path.isfile(here) or not os.path.isfile(there):
            continue
        if (os.path.getsize(here) != os.path.getsize(there)
                or int(os.path.getmtime(here)) != int(os.path.getmtime(there))):
            behind.append(name)
    return behind


def chart_mb(folder: str) -> int:
    """
    How much of a folder would become a chart, in MB.

    Only the chart files themselves are counted. A built survey folder also
    holds the whole PINGMapper project it came from - thousands of files and
    gigabytes of intermediates - and walking that to put a number beside a
    button took long enough to look like a hang.
    """
    total = 0
    for name in chart_files():
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            total += os.path.getsize(path)
    return max(0, round(total / 1e6))


def folder_mb(folder: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return round(total / 1048576)


def installed() -> list:
    """Charts in the library, with the layers each one can draw."""
    catalog = build_catalog()
    return [{"id": loc["id"], "name": loc["name"],
             "layers": list(loc["layers"]),
             "mb": folder_mb(chart_dir(loc["id"])),
             "default": loc["id"] == catalog["defaultLocationId"]}
            for loc in catalog["locations"]]


def available() -> list:
    """Built surveys not yet in the library."""
    if not os.path.isdir(BUILD_DIR):
        return []
    have = {e["id"] for e in load()["locations"]}
    rows = []
    for name in sorted(os.listdir(BUILD_DIR)):
        folder = os.path.join(BUILD_DIR, name)
        if name in have or not os.path.isdir(folder) or not looks_like_chart(folder):
            continue
        rows.append({"id": name, "path": folder, "mb": chart_mb(folder)})
    return rows


def print_library() -> None:
    rows = installed()
    if not rows:
        print("\nThe browser app has no charts yet.")
        return
    print("\nCharts in the browser app:")
    for row in rows:
        star = "   (opens here)" if row["default"] else ""
        layers = ", ".join(row["layers"]) or "no tiles"
        print(f"   {row['name']}  [{row['id']}]  {row['mb']} MB  -  {layers}{star}")


# -- Console ----------------------------------------------------------------

def interactive() -> None:
    """The menu the launcher opens: what is here, and what could be added."""
    print("AnchorHold Web Viewer - chart library")
    print_library()

    ready = available()
    if not ready:
        print("\nNothing new to add. Build a survey first, then run this again.")
        print(f"Built surveys are looked for in {BUILD_DIR}")
        return

    print("\nBuilt surveys that could be added:")
    for index, row in enumerate(ready, 1):
        print(f"   {index}) {row['id']}  ({row['mb']} MB)")
    print("\n   a) all of them        r) remove a chart        Enter) quit")

    choice = input("\nWhich? ").strip()
    if not choice:
        return
    if choice.lower() == "r":
        target = input("Remove which chart (its id)? ").strip()
        if target:
            try:
                remove(target)
            except OSError as exc:
                print()
                print(f"Could not remove {target}: {exc}")
            except SystemExit as exc:
                print()
                print(f"{exc}")
        return

    picks = list(ready) if choice.lower() == "a" else []
    if not picks:
        for token in re.split(r"[,\s]+", choice):
            if token.isdigit() and 1 <= int(token) <= len(ready):
                picks.append(ready[int(token) - 1])
            elif token:
                print(f'Ignoring "{token}" - not one of the numbers listed.')
    if not picks:
        return

    for row in picks:
        print(f"\nAdding {row['id']} ...")
        name = input(f"  Name to show [{pretty_name(row['id'])}]: ").strip()
        add(row["path"], loc_id=row["id"], name=name)
    print_library()
    print("\nReload the browser tab to see them.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command")

    sub.add_parser("list", help="what the browser app has")

    p_add = sub.add_parser("add", help="add a built survey to the browser app")
    p_add.add_argument("source", help="a built survey folder, e.g. output/noatak")
    p_add.add_argument("--id", default="", help="chart id (default: the folder name)")
    p_add.add_argument("--name", default="", help="name shown in the app")
    p_add.add_argument("--zoom", type=float, default=16.0)
    p_add.add_argument("--tide", default="", help="tide model, e.g. guaymas")
    p_add.add_argument("--default", action="store_true",
                       help="open the app on this chart")

    p_rm = sub.add_parser("remove", help="delete a chart from the browser app")
    p_rm.add_argument("id")

    p_def = sub.add_parser("default", help="choose the chart the app opens on")
    p_def.add_argument("id")

    args = ap.parse_args(argv)

    # Chart names carry accents; a console in a codepage that cannot show one
    # should print a placeholder, not fall over mid-listing.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.command == "list":
        print_library()
    elif args.command == "add":
        add(args.source, loc_id=args.id, name=args.name, zoom=args.zoom,
            tide={"mode": args.tide} if args.tide else None,
            make_default=args.default)
    elif args.command == "remove":
        try:
            remove(args.id)
        except OSError as exc:
            raise SystemExit(f"Could not remove {args.id}: {exc}")
    elif args.command == "default":
        set_default(args.id)
    else:
        interactive()


if __name__ == "__main__":
    main()
