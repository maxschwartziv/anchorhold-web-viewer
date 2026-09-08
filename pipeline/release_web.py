#!/usr/bin/env python3
"""
Build the browser app's release bundle - the zips you attach to a GitHub
release.

Charts are the reason a naive bundle is unusable: one survey is 60-270 MB, and
a repository will not take files over 100 MB at all. So the release is split
the way the app is:

    anchoring-web-<version>.zip   the app and its server.  A few MB. Everyone
                                  downloads this one.
    charts-<survey>.zip           one survey's charts. Download the water you
                                  actually sail; unzip it into the app folder.

Both are release assets, which may be up to 2 GB each, so even the largest
survey travels comfortably. Nothing here needs the repository: the bundle
carries the two Python files that serve it, and Python's standard library is
the only dependency.

    python pipeline/release_web.py                    # app only
    python pipeline/release_web.py --charts all       # app + every chart
    python pipeline/release_web.py --charts noatak,westhampton-lake
    python pipeline/release_web.py --version 1.1.0 --out D:/release
"""

from __future__ import annotations

import argparse
import os
import shutil
import zipfile

import web_charts

ROOT = web_charts.ROOT
VERSION = "1.0.0"
DEFAULT_OUT = os.path.join(ROOT, "dist", "release")

# The app is served by the same two files the repo uses, so what ships is what
# was tested. They sit in pipeline/ inside the bundle for the same reason they
# do here: both work out where the library and the app are from their own path.
SERVER_FILES = ["web_server.py", "web_charts.py"]

# Already-compressed bytes gain nothing from deflate and cost minutes, so tiles
# and images go in stored. Text is where compression is worth having.
STORE_SUFFIXES = (".mbtiles", ".png", ".jpg", ".jpeg", ".zip", ".pbf", ".woff", ".woff2")

SERVE_PY = '''#!/usr/bin/env python3
"""
Start the Anchoring App on this PC.

    python serve.py            http://localhost:8000/
    python serve.py 8080       a different port

Charts come from web_charts/ next to this file. To add one, unzip a
charts-<survey>.zip here; to see what is there, run:

    python pipeline/web_charts.py list
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "pipeline"))

import web_server

port = sys.argv[1] if len(sys.argv) > 1 else "8000"
web_server.main(["--port", port, "--open"])
'''

START_BAT = '''@echo off
REM Anchoring App - charts in your browser. Needs Python 3 on this PC.
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found.
    echo Install Python 3 from https://www.python.org/downloads/ ^(tick "Add
    echo python.exe to PATH" during setup^), then run this file again.
    pause
    exit /b 1
)

python "%~dp0serve.py" 8000
pause
'''

START_SH = '''#!/bin/sh
# Anchoring App - charts in your browser. Needs Python 3.
cd "$(dirname "$0")" || exit 1
exec python3 serve.py 8000
'''

CHARTS_BAT = '''@echo off
REM Add or remove charts. With no arguments it lists what this app has.
cd /d "%~dp0"
python "%~dp0pipeline\\web_charts.py" %*
echo.
pause
'''


def zip_dir(zf: zipfile.ZipFile, folder: str, prefix: str) -> int:
    """Add a folder to a zip, choosing per file whether compressing pays."""
    count = 0
    for base, _dirs, files in os.walk(folder):
        for name in sorted(files):
            if name == "__pycache__" or name.endswith(".pyc"):
                continue
            path = os.path.join(base, name)
            arc = os.path.join(prefix, os.path.relpath(path, folder)).replace("\\", "/")
            how = (zipfile.ZIP_STORED if name.lower().endswith(STORE_SUFFIXES)
                   else zipfile.ZIP_DEFLATED)
            zf.write(path, arc, compress_type=how)
            count += 1
    return count


def mb(path: str) -> float:
    return os.path.getsize(path) / 1_000_000


def build_app(out_dir: str, version: str, log=print) -> str:
    """The zip everyone downloads: the app, its server, and how to run it."""
    target = os.path.join(out_dir, f"anchoring-web-{version}.zip")
    top = f"anchoring-web-{version}"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        files = zip_dir(zf, os.path.join(ROOT, "web"), f"{top}/web")

        for name in SERVER_FILES:
            zf.write(os.path.join(ROOT, "pipeline", name), f"{top}/pipeline/{name}")
            files += 1

        zf.writestr(f"{top}/serve.py", SERVE_PY)
        zf.writestr(f"{top}/Anchoring App.bat", START_BAT.replace("\n", "\r\n"))
        zf.writestr(f"{top}/Add Charts.bat", CHARTS_BAT.replace("\n", "\r\n"))
        zf.writestr(f"{top}/anchoring-app.sh", START_SH)
        zf.writestr(f"{top}/README.md", readme(version))
        # An empty library, so unzipping a chart pack has somewhere to land.
        zf.writestr(f"{top}/web_charts/README.txt",
                    "Unzip a charts-<survey>.zip into this folder, then reload the app.\n")
        files += 6

    log(f"  {os.path.basename(target)}   {mb(target):.1f} MB   {files} files")
    return target


def build_chart(out_dir: str, loc_id: str, log=print) -> str:
    """One survey's charts, ready to unzip into an installed app."""
    folder = web_charts.chart_dir(loc_id)
    if not os.path.isdir(folder):
        raise SystemExit(f"No chart called {loc_id} in the library.")
    target = os.path.join(out_dir, f"charts-{loc_id}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        count = zip_dir(zf, folder, f"web_charts/{loc_id}")
    log(f"  {os.path.basename(target)}   {mb(target):.1f} MB   {count} files")
    return target


def readme(version: str) -> str:
    """Install and use instructions, written for someone who has only the zip."""
    return README_TEMPLATE.replace("{VERSION}", version)


README_TEMPLATE = """# Anchoring App {VERSION}

Surveyed bathymetry, side-scan sonar, substrate and rock charts in your
browser, with a tide-corrected depth readout and an anchor watch. It runs from
this folder, works with no internet once the charts are saved, and installs as
a desktop app if you want it to.

## Install

1. **Python 3.8 or newer** must be on the PC. Windows: install from
   [python.org](https://www.python.org/downloads/) and tick *Add python.exe to
   PATH*. macOS and Linux almost always have it already (`python3 --version`).
2. **Unzip this folder** anywhere you like - Desktop, a USB stick, wherever.
3. **Add at least one chart.** Charts ship separately because they are large.
   Download a `charts-<survey>.zip` from the same release page and unzip it
   into this folder. It will merge into `web_charts/`, so you end up with:

   ```
   anchoring-web-{VERSION}/
     Anchoring App.bat
     web_charts/
       noatak/            <- the chart you just unzipped
   ```

4. **Start it.**

   | | |
   |---|---|
   | Windows | double-click **`Anchoring App.bat`** |
   | macOS / Linux | `./anchoring-app.sh` |
   | any | `python serve.py` |

   Your browser opens at <http://localhost:8000/>. Leave the black window open
   while you use the app; closing it stops the server.

Nothing is installed system-wide and nothing phones home. Satellite imagery is
fetched from the internet when you have it; the charts themselves are local.

## Install it as a desktop app (optional)

In Chrome or Edge, click the install icon in the address bar, or use the
**Install as a desktop app** button in the app's Settings. It then opens in its
own window with no browser furniture.

To make it work with the server stopped, open **Settings -> Save this survey
offline** first. That copies the current chart into the browser's cache -
tiles, contours, grids and all - so the app keeps working with no server and no
network. Do it once per chart, while the server is running.

## Using it

**The chart.** Pinch or scroll to zoom, drag to pan. The buttons down the side
turn the layers on and off - depth shading, sonar, substrate, rock - and each
has an opacity slider under Settings. Tap anywhere on the water to read the
charted depth there.

**Depth is tide-corrected.** The readout shows the depth *now*, not the depth
at chart datum. Use the time control to ask what it will be later - at the top
of a tide, or at 03:00 when you might be leaving.

**Anchor watch.** Drop the pin where your anchor is, set a swing radius (the
app suggests one from the depth and your scope setting), and arm it. If the
boat leaves the circle, the alarm sounds. Keep the window in front and the
screen awake: a browser tab cannot run in the background, and this is a helper,
not a substitute for a proper alarm on a boat you are asleep on.

**Waypoints and tracks.** Save a labelled waypoint anywhere; record a track and
export it as GPX.

**Units, day and night.** Metres, feet or fathoms. Night mode dims everything
for a dark cockpit. The HUD hides entirely if you want the chart alone.

## Charts

Each `charts-<survey>.zip` holds one surveyed area: its tiles, depth grid,
contours and colour keys. Unzip as many as you want into this folder - they
merge, and the app offers all of them in its locations menu.

To see what is installed, or to remove one:

| | |
|---|---|
| Windows | **`Add Charts.bat`** (with no arguments it lists them) |
| any | `python pipeline/web_charts.py list` |
| | `python pipeline/web_charts.py remove <survey>` |

## Position and privacy

The boat marker uses your browser's geolocation, which asks permission first
and which browsers only offer over `localhost` or `https` - that is why the app
is served rather than opened as a file. A desktop PC usually has no GPS
receiver, so the position may come from wifi positioning or not at all; a
phone or tablet will do better. Nothing you do here leaves the machine: no
account, no telemetry, no upload.

## If something is wrong

**"Python was not found."** Install Python 3 and tick *Add python.exe to PATH*,
then run the launcher again.

**"No charts yet."** Unzip a `charts-<survey>.zip` into this folder and reload
the page.

**Port 8000 already in use.** `python serve.py 8081`, then open
<http://localhost:8081/>.

**Blank black map, buttons work.** No internet, so no satellite imagery. The
surveyed charts are local and still draw.

**No position.** Check the browser allowed location for this site, and that you
opened `localhost` rather than a file path.

**An update does not appear.** The app caches itself to work offline. Close
every window of it and reopen, or clear the saved charts from Settings.
"""


NOTES_TEMPLATE = """## Anchoring App {VERSION} - browser version

Surveyed bathymetry, side-scan sonar, substrate and rock charts in a browser,
with tide-corrected depths and an anchor watch. Runs from a folder on your own
PC. No account, no telemetry, no internet needed once the charts are saved.

### Download

Take the app, plus whichever water you sail:

| Asset | Size | What it is |
|---|---|---|
{ASSET_ROWS}

### Install

1. Install [Python 3](https://www.python.org/downloads/) if the PC has none.
   On Windows, tick *Add python.exe to PATH*.
2. Unzip **{APP_ZIP}** anywhere.
3. Unzip one or more **charts-*.zip** into that same folder. They merge into
   `web_charts/`, and the app offers all of them.
4. Windows: double-click **`Anchoring App.bat`**. macOS/Linux:
   `./anchoring-app.sh`. Your browser opens at <http://localhost:8000/>.

Full instructions, including installing it as a desktop app and saving charts
for offline use, are in the `README.md` inside the app zip.

### Requirements

Python 3.8+, and a current Chrome, Edge, Firefox or Safari. Roughly the size of
the chart packs you install, in free disk. Position needs a device with GPS -
a desktop PC usually has none, and the charts work fine without it.

### Note

The anchor watch needs the app open and the screen awake; a browser tab cannot
run in the background. Treat it as a helper, not as a substitute for a proper
alarm on a boat you are asleep on.
"""


def write_notes(out_dir: str, version: str, assets: list) -> str:
    """
    Release notes describing every zip in the output folder.

    Not just the ones built this run: charts are built a few at a time, and
    notes that listed only the last batch would send people looking for assets
    the release does not have - or hide ones it does.
    """
    app_zip = os.path.basename(assets[0])
    everything = sorted(
        (os.path.join(out_dir, name) for name in os.listdir(out_dir)
         if name.endswith(".zip")),
        key=lambda p: (os.path.basename(p) != app_zip, os.path.basename(p)))
    rows = []
    for path in everything:
        name = os.path.basename(path)
        if name == app_zip:
            what = "The app and its server. Everyone needs this."
        else:
            loc_id = name[len("charts-"):-len(".zip")]
            row = next((r for r in web_charts.installed() if r["id"] == loc_id), None)
            layers = ", ".join(row["layers"]) if row else "charts"
            what = f"{row['name'] if row else loc_id} - {layers}"
        rows.append(f"| `{name}` | {mb(path):.1f} MB | {what} |")

    text = (NOTES_TEMPLATE.replace("{VERSION}", version)
            .replace("{ASSET_ROWS}", "\n".join(rows))
            .replace("{APP_ZIP}", app_zip))
    target = os.path.join(out_dir, "RELEASE_NOTES.md")
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return target


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT, help="where to write the zips")
    ap.add_argument("--version", default=VERSION)
    ap.add_argument("--charts", default="",
                    help='"all", or a comma-separated list of chart ids')
    ap.add_argument("--clean", action="store_true",
                    help="empty the output folder first")
    args = ap.parse_args(argv)

    if args.clean and os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out, exist_ok=True)

    have = [row["id"] for row in web_charts.installed()]
    if args.charts == "all":
        wanted = have
    elif args.charts:
        wanted = [c.strip() for c in args.charts.split(",") if c.strip()]
        missing = [c for c in wanted if c not in have]
        if missing:
            raise SystemExit(f"Not in the library: {', '.join(missing)}\n"
                             f"Have: {', '.join(have) or 'nothing'}")
    else:
        wanted = []

    print(f"Anchoring App {args.version} -> {args.out}\n")
    assets = [build_app(args.out, args.version)]
    for loc_id in wanted:
        assets.append(build_chart(args.out, loc_id))

    notes = write_notes(args.out, args.version, assets)

    total = sum(mb(path) for path in assets)
    print(f"\n{len(assets)} release assets, {total:.0f} MB total")
    print(f"  {os.path.basename(notes)}   the release description")
    if not wanted:
        print("No charts bundled. Add some with --charts all, or name them:")
        print(f"   --charts {','.join(have[:2]) or '<survey>'}")
    over = [a for a in assets if mb(a) > 2000]
    if over:
        print("\nToo large for a GitHub release asset (2 GB):")
        for path in over:
            print(f"   {os.path.basename(path)}  {mb(path):.0f} MB")

    print("\nUpload them with:")
    print(f'   gh release create v{args.version} "{args.out}"/*.zip \\')
    print(f'      --title "Anchoring App {args.version}" --notes-file '
          f'"{os.path.join(args.out, "RELEASE_NOTES.md")}"')


if __name__ == "__main__":
    main()
