#!/usr/bin/env python3
"""
Build a standalone copy of the web app for another PC.

The result is a folder that carries its own charts and its own little server:
copy it to a laptop, double-click "Anchoring App.bat", and the viewer opens at
http://localhost:8000/ with no repo, no pipeline and no MBTiles in sight. From
there Chrome or Edge can install it, after which it opens in its own window and
keeps working with the server stopped (see web/README.md).

    python pipeline/package_web.py                    # -> dist/anchoring-web/
    python pipeline/package_web.py --out D:/stick     # somewhere else
    python pipeline/package_web.py --zip              # also make the .zip

Only the charts already in the web library are included; add one with
add one in the app - Settings, then Charts on this computer - or with
Add_Web_Charts.bat.
"""

from __future__ import annotations

import argparse
import os
import shutil

import web_server            # same folder; does the export and knows the catalog

ROOT = web_server.ROOT
DEFAULT_OUT = os.path.join(ROOT, "dist", "anchoring-web")

# A static server small enough to read in one sitting, so nobody has to trust a
# binary to run the charts. Stdlib only: any Python 3.8+ can serve it.
SERVE_PY = '''#!/usr/bin/env python3
"""Serve AnchorHold Web Viewer from this folder. Usage: python serve.py [port]"""

import http.server
import os
import sys
import webbrowser

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static files, with the content types the app depends on."""

    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".json": "application/json",
        ".geojson": "application/geo+json",
        ".webmanifest": "application/manifest+json",
        ".png": "image/png",
        ".bin": "application/octet-stream",
        "": "application/octet-stream",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HERE, **kwargs)

    def end_headers(self):
        # The service worker must never be served stale, or an update to the
        # app can never reach a machine that has already run it.
        if self.path.endswith("sw.js"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass                       # a chart server does not need a request log


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    url = f"http://localhost:{PORT}/"
    print(f"AnchorHold Web Viewer  ->  {url}")
    print("Other devices on this wifi: http://<this-pc-ip>:%d/" % PORT)
    print("Ctrl-C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\\nStopped.")
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

python serve.py 8000
pause
'''

START_SH = '''#!/bin/sh
# Anchoring App - charts in your browser. Needs Python 3.
cd "$(dirname "$0")" || exit 1
exec python3 serve.py 8000
'''


def build(out_dir: str, make_zip: bool) -> None:
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    print(f"Exporting charts into {out_dir}")
    web_server.export(out_dir)

    with open(os.path.join(out_dir, "serve.py"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(SERVE_PY)
    with open(os.path.join(out_dir, "Anchoring App.bat"), "w",
              encoding="utf-8", newline="\r\n") as fh:
        fh.write(START_BAT)
    start_sh = os.path.join(out_dir, "anchoring-app.sh")
    with open(start_sh, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(START_SH)
    os.chmod(start_sh, 0o755)

    readme = os.path.join(ROOT, "web", "README.md")
    if os.path.isfile(readme):
        shutil.copy2(readme, os.path.join(out_dir, "README.md"))

    total = sum(
        os.path.getsize(os.path.join(base, name))
        for base, _dirs, files in os.walk(out_dir) for name in files
    )
    files = sum(len(files) for _base, _dirs, files in os.walk(out_dir))
    print(f"\n{files} files, {total / 1_000_000:.0f} MB in {out_dir}")

    if make_zip:
        archive = shutil.make_archive(out_dir, "zip", out_dir)
        print(f"Zipped: {archive} ({os.path.getsize(archive) / 1_000_000:.0f} MB)")

    print('Copy the folder to the other PC and run "Anchoring App.bat".')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"output folder (default {DEFAULT_OUT})")
    ap.add_argument("--zip", action="store_true", dest="make_zip",
                    help="also write <out>.zip next to the folder")
    args = ap.parse_args()
    build(args.out, args.make_zip)


if __name__ == "__main__":
    main()
