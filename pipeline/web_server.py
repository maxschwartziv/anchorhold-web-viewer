#!/usr/bin/env python3
"""
Serves AnchorHold Web Viewer.

Charts come from this machine's web chart library - web_charts/ - and from
nowhere else. Tiles are read straight out of the MBTiles SQLite files, so a
survey is served from the same bytes it was built as, with no unpacking step.

    python pipeline/web_server.py                 # http://localhost:8000
    python pipeline/web_server.py --port 8080
    python pipeline/web_server.py --export site   # static copy, no Python needed

Charts are added and removed in the app, under Settings > Charts on this
computer, which calls the routes below. Add_Web_Charts.bat drives the
same library (pipeline/web_charts.py) from a shell, for scripts and for
when this server is not running.

Routes:
    /                              web/index.html and its assets
    /catalog.json                  the charts, with the layers actually present
    /tiles/<loc>/<layer>/z/x/y.png a tile from that chart's MBTiles
    /tiles/<loc>/index.json        every tile of that chart, for offline saving
    /data/<loc>/<name>             grid, contour or legend file for that chart
    /charts/<loc>/detections       take in a detector's findings (POST)
    /tools.json                    the workflow checklist and where it stands
    /tools/<step>/launch           start that step's program (POST, this PC)
    /tools/<step>/folder           show that step's folder (POST, this PC)
    /tools/recordings              set where recordings are kept (POST)
    /tools/<step>/done             tick or clear that step by hand (POST)

"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import chart_bundle               # same folder; owns the bundle format
import tools                      # same folder; the desktop steps
import workspace                  # same folder; where recordings live
import web_charts                 # same folder; owns the library on disk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# Where the browser's waypoints are mirrored so the pipeline can read them.
WAYPOINTS = os.path.join(workspace.output_dir(), "waypoints.json")


def shell_version() -> str:
    """
    The app's version, taken from the service worker that declares it.

    Read from disk each time rather than cached: a developer edits sw.js and
    reloads, and a version cached in a long-running server would be exactly
    the staleness this is here to prevent.
    """
    try:
        with open(os.path.join(ROOT, "web", "sw.js"), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("const VERSION"):
                    return line.split("=")[1].strip().strip("';\"")
    except OSError:
        pass
    return "0"


def stamp_assets(body: bytes) -> bytes:
    """
    Put the version in every script and stylesheet URL the page loads.

    A cached copy of js/app.js cannot answer a request for js/app.js?v=18,
    so a new version reaches the browser whatever state its service worker
    is in. Vendor files carry no version because they do not change with
    the app.
    """
    version = shell_version()
    text = body.decode("utf-8")
    out = []
    for line in text.split(chr(10)):
        if ('src="js/' in line or 'href="css/' in line) and "?v=" not in line:
            line = line.replace('.js"', f'.js?v={version}"')
            line = line.replace('.css"', f'.css?v={version}"')
        out.append(line)
    return chr(10).join(out).encode("utf-8")


def read_waypoints() -> list:
    try:
        with open(WAYPOINTS, encoding="utf-8") as fh:
            return json.load(fh).get("waypoints", [])
    except (OSError, ValueError):
        return []
WEB_DIR = os.path.join(ROOT, "web")

LAYERS = tuple(web_charts.TILES)

MIME = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".json": "application/json",
    ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
    ".geojson": "application/geo+json", ".bin": "application/octet-stream",
    ".ico": "image/x-icon", ".webmanifest": "application/manifest+json",
}

build_catalog = web_charts.build_catalog
location_asset = web_charts.file_path


def tiles_path(loc_id: str, layer: str):
    """The MBTiles file behind one layer of one chart, or None."""
    return web_charts.file_path(loc_id, web_charts.TILES[layer]) if layer in web_charts.TILES else None


def tile_index(loc_id: str) -> dict:
    """
    Every tile a chart owns, as "z/x/y" strings plus a byte total.

    The installed web app walks this to pull a whole chart into its offline
    cache, and shows the size first so nobody starts a 70 MB download by
    accident.
    """
    layers = {}
    total = 0
    for layer in LAYERS:
        path = tiles_path(loc_id, layer)
        if not path:
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT zoom_level, tile_column, tile_row, LENGTH(tile_data) FROM tiles"
            ).fetchall()
        finally:
            con.close()
        tiles = [f"{z}/{x}/{(1 << z) - 1 - ty}" for z, x, ty, _ in rows]
        size = sum(n or 0 for *_, n in rows)
        total += size
        layers[layer] = {"count": len(tiles), "bytes": size, "tiles": tiles}
    return {"layers": layers, "bytes": total}


# ── HTTP ────────────────────────────────────────────────────────────────────

class TileStore:
    """MBTiles readers, one connection per thread (sqlite3 is not shareable)."""

    def __init__(self):
        self._paths: dict[str, str | None] = {}
        self._local = threading.local()
        self._lock = threading.Lock()
        # Every connection handed out, so they can all be closed when a chart is
        # deleted. Windows will not unlink a file that is still open, and the
        # thread that opened it may be idle in the pool.
        self._open: list = []
        self._generation = 0

    def path_for(self, loc_id: str, layer: str) -> str | None:
        key = f"{loc_id}/{layer}"
        with self._lock:
            if key not in self._paths:
                self._paths[key] = tiles_path(loc_id, layer)
            return self._paths[key]

    def tile(self, loc_id: str, layer: str, z: int, x: int, y: int) -> bytes | None:
        path = self.path_for(loc_id, layer)
        if not path:
            return None
        # A release bumps the generation; threads then drop their stale handles
        # rather than using one that was closed underneath them.
        if getattr(self._local, "generation", -1) != self._generation:
            self._local.conns = {}
            self._local.generation = self._generation
        conns = self._local.conns
        con = conns.get(path)
        if con is None:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                                  check_same_thread=False)
            conns[path] = con
            with self._lock:
                self._open.append(con)
        # MBTiles rows count from the south (TMS), map tiles from the north.
        tms_y = (1 << z) - 1 - y
        try:
            row = con.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (z, x, tms_y)).fetchone()
        except sqlite3.ProgrammingError:
            return None            # closed under us by a release; the retry reopens
        return bytes(row[0]) if row else None

    def release(self) -> int:
        """Close every open MBTiles, so a chart's files can be deleted."""
        with self._lock:
            closed = 0
            for con in self._open:
                try:
                    con.close()
                    closed += 1
                except Exception:
                    pass
            self._open.clear()
            self._paths.clear()
            self._generation += 1
        return closed


STORE = TileStore()


class Handler(BaseHTTPRequestHandler):
    server_version = "AnchoringWeb/1.0"

    def log_message(self, fmt, *args):        # one line, no noisy default format
        if not QUIET:
            print(f"  {self.address_string()} {fmt % args}")

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/charts/manage.json":
                return self.send_json(self.manageable())
            if path == "/tools.json":
                return self.send_json(self.workflow())
            if path == "/catalog.json":
                catalog = build_catalog()
                catalog["canManage"] = self.from_this_machine()
                return self.send_json(catalog)
            if path == "/waypoints.json":
                return self.send_json({"waypoints": read_waypoints()})
            if path.startswith("/tiles/") and path.endswith("/index.json"):
                loc_id = path[len("/tiles/"):-len("/index.json")]
                return self.send_json(tile_index(loc_id))
            if path.startswith("/tiles/"):
                return self.send_tile(path)
            if path.startswith("/data/"):
                return self.send_data(path)
            return self.send_static(path)
        except BrokenPipeError:
            pass                              # the map cancelled a tile request
        except Exception as exc:              # never take the server down
            self.send_error(500, str(exc))

    def from_this_machine(self) -> bool:
        """
        Deleting charts is for whoever is sitting at this computer.

        The server listens on every interface so a tablet in the cockpit can
        read the charts. Reading is the whole point; deleting is not, and an
        open port on a boat's wifi is no place to accept it.
        """
        return self.client_address[0] in ("127.0.0.1", "::1", "localhost")

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/charts/import":
                return self.import_chart()
            if path == "/charts/add":
                return self.add_chart()
            if path.startswith("/charts/") and path.endswith("/default"):
                return self.set_default(
                    path[len("/charts/"):-len("/default")])
            if path.startswith("/charts/") and path.endswith("/refresh"):
                return self.refresh_chart(
                    path[len("/charts/"):-len("/refresh")])
            if path.startswith("/charts/") and path.endswith("/detections"):
                return self.add_detections(
                    path[len("/charts/"):-len("/detections")])
            if path.startswith("/charts/") and path.endswith("/detections/clear"):
                return self.clear_detections(
                    path[len("/charts/"):-len("/detections/clear")])
            if path.startswith("/charts/") and path.endswith("/remove"):
                return self.remove_chart(path[len("/charts/"):-len("/remove")])
            if path.startswith("/tools/") and path.endswith("/launch"):
                return self.launch_tool(path[len("/tools/"):-len("/launch")])
            if path == "/tools/recordings":
                return self.set_recordings_dir()
            if path.startswith("/tools/") and path.endswith("/folder"):
                return self.show_folder(path[len("/tools/"):-len("/folder")])
            if path.startswith("/tools/") and path.endswith("/done"):
                return self.mark_step(path[len("/tools/"):-len("/done")])
            if path == "/waypoints":
                return self.save_waypoints()
            self.send_error(404)
        except Exception as exc:
            self.send_error(500, str(exc))

    def save_waypoints(self):
        """
        Keep a copy of the browser's waypoints on disk.

        A waypoint marked on the water lives in one browser's localStorage,
        which means a target someone actually found cannot be quoted to anyone
        - including to the pipeline, which is the one thing able to measure it.
        This writes them where a script can read them. The browser stays the
        owner: it pushes its whole list, and this file is only ever a copy.
        """
        if not self.from_this_machine():
            return self.send_json({"ok": False,
                                   "error": "Waypoints are saved only from this computer."},
                                  status=403)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            return self.send_json({"ok": False, "error": "Too many waypoints."}, status=413)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            points = body.get("waypoints")
            if not isinstance(points, list):
                raise ValueError("waypoints must be a list")
            clean = []
            for entry in points[:2000]:
                lat, lon, label = (list(entry) + ["WP"])[:3]
                clean.append([float(lat), float(lon), str(label)[:80]])
        except (ValueError, TypeError, AttributeError) as exc:
            return self.send_json({"ok": False, "error": f"Not waypoints: {exc}"}, status=400)

        os.makedirs(os.path.dirname(WAYPOINTS) or ".", exist_ok=True)
        with open(WAYPOINTS, "w", encoding="utf-8") as fh:
            json.dump({"waypoints": clean}, fh, indent=1)
        if not QUIET:
            print(f"  saved {len(clean)} waypoint(s) -> {os.path.relpath(WAYPOINTS, ROOT)}")
        return self.send_json({"ok": True, "count": len(clean)})

    def workflow(self) -> dict:
        """
        The pipeline as a checklist, with where it has got to.

        A tablet reading the charts over the boat's wifi is shown the same
        steps but cannot start any of them, because the programs would open
        on a screen nobody is looking at.
        """
        listing = tools.listing()
        here = self.from_this_machine()
        listing["canLaunch"] = here
        if not here:
            for step in listing["steps"]:
                step["canLaunch"] = False
                step["missing"] = ("These open on the machine holding the "
                                   "charts, not on this one.")
        return listing

    def launch_tool(self, step_id: str):
        """
        Start one step's program on this machine.

        The id is looked up in a fixed table - nothing the browser sends ever
        becomes part of a path - and only this machine may ask, the same rule
        that guards adding and removing charts.
        """
        if not self.from_this_machine():
            return self.send_json(
                {"ok": False,
                 "error": "These open on the machine holding the charts."},
                status=403)
        try:
            started = tools.launch(step_id)
        except ValueError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, status=400)
        except OSError as exc:
            return self.send_json(
                {"ok": False, "error": f"Could not start it: {exc}"},
                status=500)
        if not QUIET:
            print(f"  launched {started['launcher']}")
        return self.send_json({"ok": True, **started})

    def set_recordings_dir(self):
        """
        Remember where this machine keeps its recordings.

        Every dialog in the chain opens here afterwards, so it is checked
        before it is kept: a folder that is not there would move each of
        those dialogs somewhere useless.
        """
        if not self.from_this_machine():
            return self.send_json(
                {"ok": False,
                 "error": "The recordings folder is set on the machine holding the charts."}, status=403)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 8000:
            return self.send_json({"ok": False, "error": "That is not a path."},
                                  status=413)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self.send_json({"ok": False, "error": "Not readable JSON."},
                                  status=400)
        try:
            if body.get("clear"):
                workspace.clear_recordings_dir()
                folder = workspace.recordings_dir()
            else:
                folder = workspace.set_recordings_dir(body.get("folder", ""))
        except ValueError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, status=400)
        except OSError as exc:
            return self.send_json(
                {"ok": False, "error": f"Could not save it: {exc}"}, status=500)
        if not QUIET:
            print(f"  recordings folder -> {folder or '(none)'}")
        return self.send_json({"ok": True, "folder": folder})

    def mark_step(self, step_id: str):
        """
        Tick or clear one step by hand.

        The list still reads the disk for everything else; this only says
        which steps a person has decided are behind them.
        """
        if not self.from_this_machine():
            return self.send_json(
                {"ok": False,
                 "error": "The list is ticked on the machine holding the charts."}, status=403)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4000:
            return self.send_json({"ok": False, "error": "Too much to read."},
                                  status=413)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self.send_json({"ok": False, "error": "Not readable JSON."},
                                  status=400)
        try:
            result = tools.mark(step_id, body.get("done"))
        except ValueError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, status=400)
        except OSError as exc:
            return self.send_json(
                {"ok": False, "error": f"Could not save it: {exc}"}, status=500)
        return self.send_json({"ok": True, **result})

    def show_folder(self, step_id: str):
        """Open one step's folder in the file manager on this machine."""
        if not self.from_this_machine():
            return self.send_json(
                {"ok": False,
                 "error": "Folders open on the machine holding the charts."},
                status=403)
        try:
            shown = tools.open_folder(step_id)
        except ValueError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, status=400)
        except OSError as exc:
            return self.send_json(
                {"ok": False, "error": f"Could not open it: {exc}"}, status=500)
        return self.send_json({"ok": True, **shown})

    def import_chart(self):

        """
        Take in a chart bundle uploaded from the browser.

        This is the browser app's half of getting a survey off the pipeline and
        onto something that can draw it, and it stands on its own: no Play, no
        phone, nothing but the file. The phone reads the same bundle through its
        own import screen.

        Written to disk in pieces as it arrives - a bundle is tens to hundreds of
        megabytes, and holding one in memory to check it first would be the
        largest thing this server ever did.
        """
        if not self.from_this_machine():
            return self.send_json(
                {"ok": False,
                 "error": "Charts can only be added from this computer."}, status=403)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self.send_json({"ok": False, "error": "No file was sent."},
                                  status=400)

        free = shutil.disk_usage(web_charts.LIBRARY if os.path.isdir(web_charts.LIBRARY)
                                 else ROOT).free
        # The bundle lands twice for a moment: the upload, then what it unpacks
        # to. Refusing now is kinder than filling the disk half way through.
        if free < length * 2 + 64_000_000:
            return self.send_json(
                {"ok": False,
                 "error": f"That chart needs {length * 2 / 1e6:.0f} MB of room and this "
                          f"disk has {free / 1e6:.0f} MB free."}, status=507)

        inbox = os.path.join(web_charts.LIBRARY, ".incoming")
        os.makedirs(inbox, exist_ok=True)
        name = os.path.basename(self.headers.get("X-Chart-Filename") or "chart.zip")
        temp = os.path.join(inbox, f"{os.getpid()}-{threading.get_ident()}.zip")
        received = 0
        try:
            with open(temp, "wb") as fh:
                while received < length:
                    chunk = self.rfile.read(min(1 << 20, length - received))
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)
            if received < length:
                return self.send_json(
                    {"ok": False,
                     "error": "The upload stopped early; nothing was changed."}, status=400)
            if not QUIET:
                print(f"  {name}: {received / 1e6:.1f} MB received, unpacking")
            # Let go of the MBTiles first: on Windows a chart being replaced
            # cannot be overwritten while a tile request still holds it open.
            STORE.release()
            loc_id = chart_bundle.install_web(
                temp, log=(lambda line: None) if QUIET else print)
        except SystemExit as exc:          # the bundle said no, with a reason
            return self.send_json({"ok": False, "error": str(exc)}, status=400)
        except OSError as exc:
            return self.send_json(
                {"ok": False, "error": f"Could not write the chart: {exc}"},
                status=500)
        finally:
            try:
                os.remove(temp)
            except OSError:
                pass

        entry = web_charts.describe(loc_id) or {}
        return self.send_json({"ok": True, "id": loc_id,
                               "name": entry.get("name", loc_id),
                               "mb": entry.get("mb", 0)})

    # ── managing the library ────────────────────────────────────────────────

    def manageable(self) -> dict:
        """
        What this machine can add, refresh or remove.

        Everything the chart tool knows, so the app can offer it: the charts in
        the library with whether each is the one that opens and whether it has
        fallen behind the build it came from, and the surveys sitting under
        output/ that are not in the library at all.
        """
        if not self.from_this_machine():
            return {"canManage": False, "installed": [], "available": []}
        catalog = build_catalog()
        installed = []
        for loc in catalog["locations"]:
            meta = web_charts.chart_meta(loc["id"])
            installed.append({
                "id": loc["id"], "name": loc["name"], "mb": loc["mb"],
                "layers": list(loc["layers"]),
                "default": loc["id"] == catalog["defaultLocationId"],
                "builtFrom": meta.get("builtFrom", ""),
                "behind": web_charts.stale_files(loc["id"]),
            })
        available = []
        for row in web_charts.available():
            layers = [layer for layer, filename in web_charts.TILES.items()
                      if os.path.isfile(os.path.join(row["path"], filename))]
            available.append(dict(row, layers=layers,
                                  name=web_charts.pretty_name(row["id"])))
        return {"canManage": True, "installed": installed, "available": available}

    def add_chart(self):
        """
        Put a survey the pipeline built into the library.

        The folder has to be one of the built surveys under output/: this is a
        server, and a path arriving over HTTP is not a licence to read anywhere
        on the disk.
        """
        if not self.from_this_machine():
            return self.send_json(
                {"ok": False,
                 "error": "Charts can only be added from this computer."}, status=403)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except ValueError as exc:
            return self.send_json({"ok": False, "error": f"Not a request: {exc}"},
                                  status=400)
        source = os.path.abspath(str(body.get("source") or ""))
        root = os.path.abspath(web_charts.BUILD_DIR)
        try:
            inside = os.path.commonpath([source, root]) == root
        except ValueError:                 # a path on another drive entirely
            inside = False
        if not inside or not os.path.isdir(source):
            return self.send_json(
                {"ok": False,
                 "error": "That is not one of the surveys under output/."}, status=400)
        if not web_charts.looks_like_chart(source):
            return self.send_json(
                {"ok": False,
                 "error": f"{os.path.basename(source)} holds none of the files "
                          "a chart is made of."}, status=400)
        # Re-adding over a chart that is being served means replacing files the
        # tile reader has open; let go of them first.
        STORE.release()
        try:
            loc_id = web_charts.add(source, log=(lambda line: None) if QUIET else print)
        except SystemExit as exc:
            return self.send_json({"ok": False, "error": str(exc)}, status=400)
        except OSError as exc:
            return self.send_json(
                {"ok": False, "error": f"Could not add it: {exc}"}, status=500)
        entry = web_charts.describe(loc_id) or {}
        return self.send_json({"ok": True, "id": loc_id,
                               "name": entry.get("name", loc_id),
                               "mb": entry.get("mb", 0)})

    def refresh_chart(self, loc_id: str):
        """Re-link a chart from the build it came from, after a rebuild."""
        if not self.from_this_machine():
            return self.send_json(
                {"ok": False,
                 "error": "Charts can only be refreshed from this computer."},
                status=403)
        if "/" in loc_id or "\\" in loc_id or not loc_id:
            return self.send_json({"ok": False, "error": "Not a chart name."},
                                  status=400)
        meta = web_charts.chart_meta(loc_id)
        source = meta.get("builtFrom") or os.path.join(web_charts.BUILD_DIR, loc_id)
        if not os.path.isdir(source):
            return self.send_json(
                {"ok": False,
                 "error": f"The build it came from is gone: {source}"}, status=404)
        STORE.release()
        try:
            web_charts.add(source, loc_id=loc_id, zoom=0.0,
                           log=(lambda line: None) if QUIET else print)
        except SystemExit as exc:
            return self.send_json({"ok": False, "error": str(exc)}, status=400)
        except OSError as exc:
            return self.send_json(
                {"ok": False, "error": f"Could not refresh it: {exc}"}, status=500)
        entry = web_charts.describe(loc_id) or {}
        return self.send_json({"ok": True, "id": loc_id,
                               "name": entry.get("name", loc_id),
                               "mb": entry.get("mb", 0)})

    def add_detections(self, loc_id: str):
        """
        Take in what a detector found, and file it with the chart.

        A survey is built once; the detector may be run over it a dozen times
        afterwards, on a machine with the GPU. So findings arrive on their own
        rather than inside a bundle, and are small enough to hold in memory -
        five crab pots is a few hundred bytes.
        """
        if not self.from_this_machine():
            return self.send_json(
                {"ok": False,
                 "error": "Detections can only be added from this computer."},
                status=403)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self.send_json({"ok": False, "error": "No file was sent."},
                                  status=400)
        if length > 20_000_000:
            return self.send_json(
                {"ok": False,
                 "error": f"That file is {length / 1e6:.0f} MB - detections are a "
                          "list of points, and should be a fraction of that."},
                status=413)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return self.send_json({"ok": False, "error": f"Not readable JSON: {exc}"},
                                  status=400)
        try:
            count = web_charts.write_detections(loc_id, payload)
        except ValueError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, status=400)
        except OSError as exc:
            return self.send_json(
                {"ok": False, "error": f"Could not write them: {exc}"}, status=500)
        if not QUIET:
            print(f"  {count} detection(s) -> {loc_id}")
        return self.send_json({"ok": True, "id": loc_id, "count": count})

    def clear_detections(self, loc_id: str):
        """Take the detections back off a chart, leaving the survey alone."""
        if not self.from_this_machine():
            return self.send_json(
                {"ok": False,
                 "error": "Detections are removed from this computer."}, status=403)
        try:
            had = web_charts.clear_detections(loc_id)
        except ValueError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, status=400)
        except OSError as exc:
            return self.send_json(
                {"ok": False, "error": f"Could not remove them: {exc}"}, status=500)
        return self.send_json({"ok": True, "id": loc_id, "had": had})

    def set_default(self, loc_id: str):
        """Choose the chart the app opens on."""
        if not self.from_this_machine():
            return self.send_json(
                {"ok": False,
                 "error": "The starting chart is set from this computer."}, status=403)
        try:
            web_charts.set_default(loc_id)
        except SystemExit as exc:
            return self.send_json({"ok": False, "error": str(exc)}, status=400)
        return self.send_json({"ok": True, "id": loc_id})

    def remove_chart(self, loc_id: str):
        if not self.from_this_machine():
            return self.send_json({"ok": False,
                                   "error": "Charts can only be removed from this computer."},
                                  status=403)
        if "/" in loc_id or "\\" in loc_id or not loc_id:
            return self.send_json({"ok": False, "error": "Not a chart name."}, status=400)
        if not os.path.isdir(web_charts.chart_dir(loc_id)):
            return self.send_json({"ok": False, "error": f"No chart called {loc_id}."},
                                  status=404)
        # Let go of the MBTiles first: on Windows an open file cannot be deleted,
        # and the connections are spread across the thread pool.
        closed = STORE.release()
        try:
            web_charts.remove(loc_id, log=lambda line: None)
        except SystemExit as exc:
            return self.send_json({"ok": False, "error": str(exc)}, status=400)
        except OSError as exc:
            return self.send_json(
                {"ok": False,
                 "error": f"Could not delete the files: {exc.strerror or exc}"}, status=500)
        if not QUIET:
            print(f"  removed chart {loc_id} (closed {closed} open tile file(s))")
        return self.send_json({"ok": True, "removed": loc_id})

    # /tiles/<loc>/<layer>/<z>/<x>/<y>.png
    def send_tile(self, path: str):
        parts = path[len("/tiles/"):].removesuffix(".png").split("/")
        if len(parts) != 5:
            return self.send_error(404)
        loc_id, layer, z, x, y = parts
        if layer not in LAYERS:
            return self.send_error(404)
        try:
            blob = STORE.tile(loc_id, layer, int(z), int(x), int(y))
        except (ValueError, sqlite3.Error):
            return self.send_error(404)
        if blob is None:
            return self.send_error(404)       # empty tile: MapLibre draws nothing
        self.send_bytes(blob, "image/png", cache=86400)

    # /data/<loc>/<name>
    def send_data(self, path: str):
        parts = path[len("/data/"):].split("/")
        if len(parts) != 2:
            return self.send_error(404)
        found = location_asset(parts[0], parts[1])
        if not found:
            return self.send_error(404)
        with open(found, "rb") as fh:
            body = fh.read()
        self.send_bytes(body, MIME.get(os.path.splitext(found)[1], "application/octet-stream"),
                        cache=3600)

    def send_static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        target = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not target.startswith(WEB_DIR) or not os.path.isfile(target):
            return self.send_error(404)
        with open(target, "rb") as fh:
            body = fh.read()
        if os.path.basename(target) == "index.html":
            body = stamp_assets(body)
        self.send_bytes(body, MIME.get(os.path.splitext(target)[1], "application/octet-stream"))

    def send_json(self, obj, status: int = 200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body: bytes, mime: str, cache: int = 0):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", f"public, max-age={cache}" if cache else "no-cache")
        self.end_headers()
        self.wfile.write(body)


# ── Static export ───────────────────────────────────────────────────────────

def export(dest: str):
    """Write a self-contained copy that any static host (or file server) can serve."""
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(WEB_DIR):
        src = os.path.join(WEB_DIR, name)
        dst = os.path.join(dest, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)

    catalog = build_catalog()
    with open(os.path.join(dest, "catalog.json"), "w", encoding="utf-8") as fh:
        json.dump(catalog, fh)

    for loc in catalog["locations"]:
        loc_id = loc["id"]
        # Data files: grids come in .bin/.json pairs, the rest are single files.
        wanted = []
        for key, base in loc["data"].items():
            wanted += [f"{base}.bin", f"{base}.json"] if key.endswith("Grid") else [base]
        wanted += list(loc["legends"].values())
        out_data = os.path.join(dest, "data", loc_id)
        os.makedirs(out_data, exist_ok=True)
        for name in wanted:
            found = location_asset(loc_id, name)
            if found:
                shutil.copy2(found, os.path.join(out_data, name))

        out_tiles = os.path.join(dest, "tiles", loc_id)
        os.makedirs(out_tiles, exist_ok=True)
        with open(os.path.join(out_tiles, "index.json"), "w", encoding="utf-8") as fh:
            json.dump(tile_index(loc_id), fh)

        for layer in loc["layers"]:
            path = tiles_path(loc_id, layer)
            if not path:
                continue
            count = explode(path, os.path.join(out_tiles, layer))
            print(f"  {loc_id}/{layer}: {count} tiles")

    print(f"Exported to {dest} - serve that folder with any web server.")


def explode(mbtiles: str, out_dir: str) -> int:
    """Write an MBTiles out as an {z}/{x}/{y}.png pyramid, flipping TMS rows."""
    con = sqlite3.connect(f"file:{mbtiles}?mode=ro", uri=True)
    written = 0
    try:
        for z, x, tms_y, blob in con.execute(
                "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"):
            y = (1 << z) - 1 - tms_y
            folder = os.path.join(out_dir, str(z), str(x))
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, f"{y}.png"), "wb") as fh:
                fh.write(blob)
            written += 1
    finally:
        con.close()
    return written


QUIET = False


def main(argv=None):
    global QUIET
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0",
                    help="0.0.0.0 (default) also serves other devices on this wifi")
    ap.add_argument("--export", metavar="DIR",
                    help="write a static copy instead of serving")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--open", action="store_true",
                    help="open the app in a browser once the server is up")
    args = ap.parse_args(argv)
    QUIET = args.quiet

    # What matters is whether there are charts, not whether a catalog file
    # exists: a chart pack unzipped into the library describes itself.
    catalog = build_catalog()

    if args.export:
        if not catalog["locations"]:
            raise SystemExit(f"No charts in {web_charts.LIBRARY}\n"
                             "There would be nothing in the static copy.")
        export(args.export)
        return

    if not catalog["locations"]:
        # Serve anyway. The app can take a chart bundle in through its own
        # Settings screen, so an empty library is where someone holding a
        # bundle starts - refusing to run is what would strand them.
        print(f"No charts in {web_charts.LIBRARY} yet.")
        print("  Add one in the app: Settings > Charts > Add chart from a "
              "bundle,")
        print("  or from here with Add_Web_Charts.bat / Make_Chart_Bundle.bat.\n")

    print("AnchorHold Web Viewer")
    for loc in catalog["locations"]:
        print(f"  {loc['name']}: {', '.join(loc['layers']) or 'no tiles'}")
    print(f"\n  http://localhost:{args.port}/")
    if args.host == "0.0.0.0":
        print("  (reachable from any device on this network at this machine's IP)")
    print("  Ctrl-C to stop\n")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    if args.open:
        import webbrowser
        webbrowser.open(f"http://localhost:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
