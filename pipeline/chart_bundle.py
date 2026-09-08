#!/usr/bin/env python3
"""
One built survey, in one file, that either app can take on its own.

The browser app used to get its charts from
a folder on the machine serving it, which assumes something the person
holding the recording may not have: a shell on the server.
So a survey the pipeline has just built is packed into a single file instead -

    dist/charts/indian-hills-lake-chart.zip

- and the app knows how to take one in by itself:

    Browser    Settings > Charts > Add chart, pick the file

The bundle is a plain zip and the app reads it directly.

What is inside is exactly what `output/<survey>/` holds - the same file names
the pipeline already writes - plus a chart.json saying what the survey is:

    chart.json            id, name, centre, zoom, tide, survey record, contents
    bathymetry.mbtiles    the tile sets that were built (any subset)
    sonar.mbtiles         stored uncompressed: PNG tiles do not deflate, and
    substrate.mbtiles     storing them lets both apps copy at disk speed
    rock.mbtiles
    contours.geojson      the vector overlays and query grids, deflated
    shallow_bands.geojson
    boundary.geojson
    track.geojson
    depth_grid.bin/.json
    substrate_grid.bin/.json
    *_legend.png

Usage:

    python pipeline/chart_bundle.py build output/indian-hills-lake
    python pipeline/chart_bundle.py build output/noatak --name "Noatak River"
    python pipeline/chart_bundle.py build --all
    python pipeline/chart_bundle.py inspect dist/charts/noatak-chart.zip
    python pipeline/chart_bundle.py install-web dist/charts/noatak-chart.zip
    python pipeline/chart_bundle.py send dist/charts/noatak-chart.zip
    python pipeline/chart_bundle.py selftest
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import web_charts                  # owns the file names a chart is made of
import workspace                   # neutral home for builds

BUILD_DIR = workspace.output_dir()
BUNDLE_DIR = os.path.join(ROOT, "dist", "charts")

MANIFEST = "chart.json"
FORMAT = 1

# The bundle is read by three different programs; keeping the size honest
# matters more than saving a few percent. MBTiles hold PNG tiles that are
# already compressed, so they go in uncompressed and copy at disk speed.
STORED_SUFFIXES = (".mbtiles", ".png")


# -- What a bundle holds -----------------------------------------------------

def chart_files() -> list:
    """Every file name a chart can be made of, in a stable order."""
    return list(web_charts.chart_files())


def present_files(folder: str) -> list:
    """The chart files that folder actually has, largest last for tidy logs."""
    return [name for name in chart_files()
            if os.path.isfile(os.path.join(folder, name))]


def looks_like_chart(folder: str) -> bool:
    return bool(present_files(folder))


def _web_entry(loc_id: str) -> dict:
    """What the browser library says about this survey, if it holds it."""
    path = os.path.join(web_charts.LIBRARY, loc_id, MANIFEST)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _derive_center(folder: str) -> dict:
    """Middle of the charted water, from the grid header or the tiles."""
    header = os.path.join(folder, "depth_grid.json")
    if os.path.isfile(header):
        try:
            with open(header, encoding="utf-8") as fh:
                grid = json.load(fh)
            lat = grid["latMin"] + grid["dLat"] * (grid["rows"] - 1) / 2
            lon = grid["lonMin"] + grid["dLon"] * (grid["cols"] - 1) / 2
            return {"lat": round(lat, 6), "lon": round(lon, 6)}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    centre = web_charts.derive_center(folder)
    if centre:
        return {"lat": round(centre[0], 6), "lon": round(centre[1], 6)}
    return {}


def describe(folder: str, loc_id: str = "", name: str = "", zoom: float = 0.0,
             tide: dict = None, source: dict = None) -> dict:
    """
    The manifest for one built survey: what it is, and what is in the box.

    Description is looked for where the pipeline actually writes it - the
    chart's own chart.json, then the browser library, before anything is
    guessed from the files. A survey that has been named
    once keeps that name wherever it is bundled from.
    """
    folder = os.path.abspath(folder)
    loc_id = loc_id or os.path.basename(folder.rstrip("/\\"))
    files = present_files(folder)
    if not files:
        raise SystemExit(f"{folder} holds none of the files a chart is made of.")

    known = {}
    for candidate in (os.path.join(folder, MANIFEST),):
        try:
            with open(candidate, encoding="utf-8") as fh:
                known = json.load(fh)
        except (OSError, ValueError):
            known = {}
    if not known:
        known = _web_entry(loc_id)

    entries = []
    total = 0
    for filename in files:
        size = os.path.getsize(os.path.join(folder, filename))
        entries.append({"name": filename, "bytes": size})
        total += size

    layers = {}
    for layer, filename in web_charts.TILES.items():
        path = os.path.join(folder, filename)
        if os.path.isfile(path):
            try:
                layers[layer] = web_charts.mbtiles_info(path)
            except Exception:                       # a half-written tile set
                layers[layer] = {}

    data = {}
    for key, filename in web_charts.DATA.items():
        if os.path.isfile(os.path.join(folder, filename)):
            data[key] = filename
    for key, base in web_charts.GRIDS.items():
        if (os.path.isfile(os.path.join(folder, f"{base}.bin"))
                and os.path.isfile(os.path.join(folder, f"{base}.json"))):
            data[key] = base

    legends = {key: filename for key, filename in web_charts.LEGENDS.items()
               if os.path.isfile(os.path.join(folder, filename))}

    centre = known.get("center") or _derive_center(folder)
    stamp = hashlib.md5()
    for entry in entries:
        info = os.stat(os.path.join(folder, entry["name"]))
        stamp.update(f"{entry['name']}:{entry['bytes']}:{int(info.st_mtime)}".encode())

    manifest = {
        "format": FORMAT,
        "id": loc_id,
        "name": name or known.get("name") or web_charts.pretty_name(loc_id),
        "center": centre or {"lat": 0.0, "lon": 0.0},
        "zoom": zoom or known.get("zoom") or 16.0,
        "tide": tide or known.get("tide") or {"mode": "none"},
        "source": source or known.get("source") or {},
        "built": datetime.datetime.now(datetime.timezone.utc)
                 .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "rev": stamp.hexdigest()[:8],
        "bytes": total,
        "files": entries,
        "layers": layers,
        "data": data,
        "legends": legends,
    }
    return manifest


# -- Building ----------------------------------------------------------------

def bundle_name(loc_id: str) -> str:
    return f"{loc_id}-chart.zip"


def build(folder: str, out_dir: str = BUNDLE_DIR, loc_id: str = "", name: str = "",
          zoom: float = 0.0, tide: dict = None, source: dict = None,
          log=print) -> str:
    """
    Pack one built survey into a bundle. Returns the path written.

    Written to a temporary name and renamed at the end, so a bundle that exists
    is a bundle that finished: half a chart on a phone is worse than none.
    """
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise SystemExit(f"No such folder: {folder}")
    manifest = describe(folder, loc_id=loc_id, name=name, zoom=zoom, tide=tide,
                        source=source)

    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, bundle_name(manifest["id"]))
    partial = target + ".part"

    log(f"Packing {manifest['name']} ({manifest['id']})")
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED,
                         allowZip64=True) as zf:
        # The manifest goes in first so a reader can learn what it is holding
        # before it has read a hundred megabytes of tiles.
        zf.writestr(MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        for entry in manifest["files"]:
            filename = entry["name"]
            stored = filename.endswith(STORED_SUFFIXES)
            zf.write(os.path.join(folder, filename), filename,
                     compress_type=zipfile.ZIP_STORED if stored
                     else zipfile.ZIP_DEFLATED)
            log(f"  {filename}  {entry['bytes'] / 1e6:.1f} MB"
                f"{'' if stored else ' (compressed)'}")

    if os.path.exists(target):
        web_charts._unlink(target)
    os.replace(partial, target)
    log(f"\n{target}  {os.path.getsize(target) / 1e6:.1f} MB")
    log("  Browser: Settings > Charts > Add chart")
    return target


def build_all(out_dir: str = BUNDLE_DIR, log=print) -> list:
    """Every survey under output/ that has chart files, bundled."""
    made = []
    for entry in sorted(os.listdir(BUILD_DIR) if os.path.isdir(BUILD_DIR) else []):
        folder = os.path.join(BUILD_DIR, entry)
        if os.path.isdir(folder) and looks_like_chart(folder):
            made.append(build(folder, out_dir=out_dir, log=log))
            log("")
    if not made:
        log(f"No built surveys in {BUILD_DIR}.")
    return made


# -- Reading -----------------------------------------------------------------

def safe_name(name: str) -> str:
    """
    The file name a zip entry may be extracted as, or "".

    Bundles arrive from wherever the person got them, so an entry naming a path
    - a separator, a drive, a parent - is refused rather than resolved. Only
    the flat file names a chart is made of are accepted.
    """
    if not name or name != os.path.basename(name.replace("\\", "/")):
        return ""
    if name.startswith(".") or ":" in name:
        return ""
    return name if name in (chart_files() + [MANIFEST]) else ""


def read_manifest(bundle: str) -> dict:
    """The manifest of a bundle, or a SystemExit saying why it is not one."""
    try:
        with zipfile.ZipFile(bundle) as zf:
            with zf.open(MANIFEST) as fh:
                manifest = json.loads(fh.read().decode("utf-8"))
    except KeyError:
        raise SystemExit(f"{os.path.basename(bundle)} has no {MANIFEST}: "
                         "it is a zip, but not a chart bundle.")
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        raise SystemExit(f"Could not read {os.path.basename(bundle)}: {exc}")
    if not manifest.get("id"):
        raise SystemExit(f"{os.path.basename(bundle)} names no survey.")
    if manifest.get("format", 1) > FORMAT:
        raise SystemExit(
            f"{os.path.basename(bundle)} was written by a newer pipeline "
            f"(format {manifest['format']}); this one reads up to {FORMAT}.")
    return manifest


def verify(bundle: str, log=print) -> dict:
    """Check a bundle end to end: manifest, member names, sizes, CRCs."""
    manifest = read_manifest(bundle)
    with zipfile.ZipFile(bundle) as zf:
        bad = zf.testzip()
        if bad:
            raise SystemExit(f"{os.path.basename(bundle)} is damaged at {bad}.")
        held = {info.filename: info.file_size for info in zf.infolist()}
    for entry in manifest.get("files", []):
        name = safe_name(entry["name"])
        if not name:
            raise SystemExit(f"{os.path.basename(bundle)} names a file it may "
                             f"not carry: {entry['name']}")
        if name not in held:
            raise SystemExit(f"{os.path.basename(bundle)} is missing {name}.")
        if held[name] != entry["bytes"]:
            raise SystemExit(f"{name} is {held[name]} bytes, not {entry['bytes']}.")
    log(f"{manifest['name']}: {len(manifest.get('files', []))} files, "
        f"{manifest.get('bytes', 0) / 1e6:.1f} MB, ok")
    return manifest


def extract(bundle: str, folder: str, log=print) -> dict:
    """
    Unpack a bundle into a folder, replacing what is there. Returns the manifest.

    Only the files the manifest declares are written, under the names a chart
    is made of; anything else in the zip is left where it is.
    """
    manifest = read_manifest(bundle)
    os.makedirs(folder, exist_ok=True)
    with zipfile.ZipFile(bundle) as zf:
        for entry in manifest.get("files", []):
            name = safe_name(entry["name"])
            if not name:
                log(f"  (skip {entry['name']} - not a chart file)")
                continue
            target = os.path.join(folder, name)
            if os.path.exists(target):
                web_charts._unlink(target)
            with zf.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            log(f"  {name}  {entry['bytes'] / 1e6:.1f} MB")
    return manifest


# -- Into the browser app ----------------------------------------------------

def install_web(bundle: str, make_default: bool = False, log=print) -> str:
    """
    Put a bundle into the browser app's chart library. Returns the chart id.

    Unpacked straight into the library folder rather than into a temporary one
    and copied: a 110 MB sonar set is slow enough to move once.
    """
    manifest = read_manifest(bundle)
    loc_id = manifest["id"]
    folder = web_charts.chart_dir(loc_id)
    log(f"Installing {manifest['name']} into the browser library")
    extract(bundle, folder, log=log)
    # Leave the bundle's own description in the chart folder before recording it:
    # the library reads a chart's name, centre, zoom and tide from there, so the
    # ones the survey was built with are the ones the browser app shows rather
    # than a centre re-derived from the tile bounds.
    web_charts.write_chart_meta(loc_id, dict(manifest, id=loc_id))
    # add() sees the files are already in place, so all that is left is the
    # catalog entry saying where this chart comes in the list.
    web_charts.add(folder, loc_id=loc_id, zoom=0.0, make_default=make_default,
                   log=log)
    if manifest.get("source"):
        # Re-adding a chart keeps the catalog entry that was already there, and
        # that entry may predate the survey record. Provenance travels with the
        # chart, so put it back rather than let the older entry drop it.
        entry = dict(web_charts.chart_meta(loc_id), id=loc_id)
        entry.setdefault("source", manifest["source"])
        web_charts.write_chart_meta(loc_id, entry)
    log(f"Reload the browser app to see {manifest['name']}.")
    return loc_id


# -- Listing -----------------------------------------------------------------

def print_bundles(out_dir: str = BUNDLE_DIR) -> None:
    if not os.path.isdir(out_dir):
        print(f"No bundles yet. Build one with:{chr(10)}"
              f"  python pipeline/chart_bundle.py build output/<survey>")
        return
    names = sorted(n for n in os.listdir(out_dir) if n.endswith(".zip"))
    if not names:
        print(f"No bundles in {out_dir}.")
        return
    print(f"{out_dir}:")
    for name in names:
        path = os.path.join(out_dir, name)
        try:
            manifest = read_manifest(path)
        except SystemExit as exc:
            print(f"  {name:<40} {exc}")
            continue
        layers = ", ".join(manifest.get("layers", {})) or "no tiles"
        print(f"  {name:<40} {os.path.getsize(path) / 1e6:7.1f} MB  "
              f"{manifest['name']} ({layers})")


def print_inspection(bundle: str) -> None:
    manifest = verify(bundle)
    centre = manifest.get("center") or {}
    print(f"  id       {manifest['id']}")
    print(f"  name     {manifest['name']}")
    print(f"  centre   {centre.get('lat')}, {centre.get('lon')}  zoom "
          f"{manifest.get('zoom')}")
    print(f"  tide     {(manifest.get('tide') or {}).get('mode', 'none')}")
    print(f"  built    {manifest.get('built')}  rev {manifest.get('rev')}")
    for layer, info in (manifest.get("layers") or {}).items():
        print(f"  tiles    {layer:<11} z{info.get('minzoom')}-{info.get('maxzoom')}")
    for key in (manifest.get("data") or {}):
        print(f"  data     {key}")
    if manifest.get("source"):
        print(f"  survey   {manifest['source'].get('surveyedBy', 'recorded')}")


# -- Self-check --------------------------------------------------------------

def _tiny_png() -> bytes:
    """One transparent pixel: the smallest thing a tile route can carry."""
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body)))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
            + chunk(b"IEND", b""))


def _tiny_mbtiles(path: str, tile: bytes) -> None:
    """An MBTiles holding one tile, in the layout both apps read."""
    import sqlite3

    con = sqlite3.connect(path)
    try:
        con.executescript(
            "CREATE TABLE metadata (name text, value text);"
            "CREATE TABLE tiles (zoom_level integer, tile_column integer, "
            "tile_row integer, tile_data blob);")
        con.execute("INSERT INTO metadata VALUES ('bounds', ?)",
                    ("-90.5,38.7,-90.49,38.705",))
        # Rows count from the south in MBTiles; the servers flip them.
        con.execute("INSERT INTO tiles VALUES (14, 4000, 6000, ?)", (tile,))
        con.commit()
    finally:
        con.close()


def _upload_round_trip(bundle: str):
    """
    Serve the temporary library, upload a bundle to it, and read it back.

    The browser app's half of this only works if the server it is talking to
    will take a file and unpack it, so the check is made against a real
    server rather than against install_web() alone.
    """
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    import web_server

    web_server.QUIET = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web_server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_port
        with open(bundle, "rb") as fh:
            body = fh.read()
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        con.request("POST", "/charts/import", body=body, headers={
            "Content-Type": "application/zip",
            "Content-Length": str(len(body)),
            "X-Chart-Filename": os.path.basename(bundle)})
        answer = json.loads(con.getresponse().read().decode("utf-8"))

        def get(path: str) -> bytes:
            con.request("GET", path)
            response = con.getresponse()
            return response.read() if response.status == 200 else b""

        catalog = json.loads(get("/catalog.json") or b"{}")
        # z14, and the y a map asks for is the flip of the row that was stored.
        served = get(f"/tiles/test-lake/bathymetry/14/4000/{(1 << 14) - 1 - 6000}.png")
        grid = json.loads(get("/data/test-lake/depth_grid.json") or b"{}")
        con.close()
        return answer, catalog, served, grid
    finally:
        httpd.shutdown()
        httpd.server_close()
        # Windows will not delete an MBTiles a tile request still holds,
        # and the connections are spread across the thread pool.
        web_server.STORE.release()


def selftest(log=print) -> bool:
    """
    Build a bundle from a made-up survey, read it back, and install it.

    Runs against a temporary library so it can be run on a working machine
    without touching the charts that are actually installed.
    """
    import tempfile

    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        log(f"  {'PASS' if passed else 'FAIL'}  {name}"
            f"{'  ' + detail if detail else ''}")

    with tempfile.TemporaryDirectory() as tmp:
        survey = os.path.join(tmp, "test-lake")
        os.makedirs(survey)
        # A chart is whatever subset of the file names is present, so a survey
        # with a grid, contours and one tile set is a legitimate small one.
        with open(os.path.join(survey, "depth_grid.json"), "w") as fh:
            json.dump({"lonMin": -90.5, "latMin": 38.7, "dLon": 1e-4,
                       "dLat": 1e-4, "cols": 101, "rows": 51}, fh)
        with open(os.path.join(survey, "depth_grid.bin"), "wb") as fh:
            fh.write(b"\x00" * (101 * 51 * 4))
        with open(os.path.join(survey, "contours.geojson"), "w") as fh:
            json.dump({"type": "FeatureCollection", "features": []}, fh)
        tile = _tiny_png()
        _tiny_mbtiles(os.path.join(survey, "bathymetry.mbtiles"), tile)

        manifest = describe(survey)
        check("centre from the grid header",
              abs(manifest["center"]["lat"] - 38.7025) < 1e-6
              and abs(manifest["center"]["lon"] + 90.495) < 1e-6,
              f"{manifest['center']}")
        check("name from the folder", manifest["name"] == "Test Lake",
              manifest["name"])
        check("grid seen as queryable data",
              manifest["data"].get("depthGrid") == "depth_grid")

        bundle = build(survey, out_dir=os.path.join(tmp, "dist"), log=lambda *_: None)
        check("bundle written", os.path.isfile(bundle))
        again = verify(bundle, log=lambda *_: None)
        check("reads back", again["id"] == "test-lake" and again["rev"] == manifest["rev"])

        # A bundle naming a file outside itself must not be able to write there.
        check("refuses a path in a member name", safe_name("../evil.geojson") == "")
        check("refuses an unknown member", safe_name("payload.exe") == "")
        check("accepts a chart file", safe_name("contours.geojson") == "contours.geojson")

        out = os.path.join(tmp, "unpacked")
        extract(bundle, out, log=lambda *_: None)
        check("unpacks every file",
              all(os.path.isfile(os.path.join(out, e["name"]))
                  for e in manifest["files"]),
              f"{len(manifest['files'])} files")
        check("grid survives the round trip",
              os.path.getsize(os.path.join(out, "depth_grid.bin")) == 101 * 51 * 4)

        # Installing into the browser library, with the library moved aside.
        library, catalog = web_charts.LIBRARY, web_charts.CATALOG
        try:
            web_charts.LIBRARY = os.path.join(tmp, "library")
            web_charts.CATALOG = os.path.join(web_charts.LIBRARY, "catalog.json")
            install_web(bundle, log=lambda *_: None)
            described = web_charts.describe("test-lake")
            check("browser library takes it",
                  described is not None and described["name"] == "Test Lake")
            check("browser sees the depth grid",
                  bool(described) and described["data"].get("depthGrid") == "depth_grid")

            # The route the browser app itself uses: post the bundle to the
            # chart server, then read a tile back out of what it unpacked.
            answer, catalog, served, grid = _upload_round_trip(bundle)
            check("chart server takes an upload", answer.get("ok") is True,
                  str(answer)[:120])
            check("the upload reaches the catalog",
                  any(loc["id"] == "test-lake" for loc in catalog.get("locations", [])))
            check("a tile comes back whole", served == tile,
                  f"{len(served)} bytes")
            check("the query grid is served", grid.get("cols") == 101)
        finally:
            web_charts.LIBRARY, web_charts.CATALOG = library, catalog

    log("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    return ok


# -- CLI ---------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command")

    p_build = sub.add_parser("build", help="pack a built survey into a bundle")
    p_build.add_argument("source", nargs="?", default="",
                         help="a built survey folder, e.g. output/noatak")
    p_build.add_argument("--all", action="store_true",
                         help="bundle every survey under output/")
    p_build.add_argument("--out", default=BUNDLE_DIR, help="where to write it")
    p_build.add_argument("--id", default="", help="chart id (default: folder name)")
    p_build.add_argument("--name", default="", help="name shown in the apps")
    p_build.add_argument("--zoom", type=float, default=0.0)
    p_build.add_argument("--tide", default="", help="tide model, e.g. guaymas")

    sub.add_parser("list", help="the bundles built so far")

    p_inspect = sub.add_parser("inspect", help="what is in a bundle")
    p_inspect.add_argument("bundle")

    p_web = sub.add_parser("install-web", help="install a bundle into the browser app")
    p_web.add_argument("bundle")
    p_web.add_argument("--default", action="store_true",
                       help="open the browser app on this chart")

    sub.add_parser("selftest", help="check the bundle format end to end")

    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.command == "build":
        if args.all:
            build_all(out_dir=args.out)
        elif args.source:
            build(args.source, out_dir=args.out, loc_id=args.id, name=args.name,
                  zoom=args.zoom, tide={"mode": args.tide} if args.tide else None)
        else:
            raise SystemExit("Name a survey folder, or pass --all.")
    elif args.command == "list":
        print_bundles()
    elif args.command == "inspect":
        print_inspection(args.bundle)
    elif args.command == "install-web":
        install_web(args.bundle, make_default=args.default)
    elif args.command == "selftest":
        raise SystemExit(0 if selftest() else 1)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
