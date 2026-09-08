"""
Survey Planner - plan side scan sonar surveys of inland water.

    python survey_planner.py

Fetch a lake outline from the USGS National Hydrography Dataset, mark where you
can actually get a boat in, set the parameters that matter, and get straight
survey lines split into workable outings - exportable to GPX or QGroundControl.

Tkinter and matplotlib only, so it packages to a single executable with
PyInstaller and needs nothing installed on the machine that runs it.
"""

from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
import matplotlib.patheffects as pe
from matplotlib.collections import PolyCollection
from matplotlib.figure import Figure

from planner import (basemap, exporters, imagery, plan as planning,
                     shoreline)
from planner.geometry import LocalFrame, polyline_length_ft

MILES_TO_FEET = 5280.0


class _Cancelled(Exception):
    """Raised inside the worker to unwind a plan the user called off."""

NOTHING_FOUND = ("Nothing bright enough found on the water. Lower the"
                 " smallest-object size to look harder.")
# Said every time results are shown, because both failures are silent:
# a dark dock leaves nothing to see, and glint leaves something that
# looks exactly like a dock.
IMAGERY_CAVEAT = ("Check them: a dock in shadow reads as water and is not"
                  " here, and sun glint reads as a structure and may be.")
NEWLINE = chr(10)
# How many names a map can carry before it stops being a map. A long river
# passes thousands of them and drawing each once still hid the water.
MAX_ROAD_LABELS = 20
MAX_SLIPWAY_LABELS = 12
# One wheel notch. Small enough to creep up on something, large enough that
# crossing a lake does not take twenty of them.
ZOOM_STEP = 1.3
ELLIPSIS = "…"


def _app_dir() -> str:
    """Where this program keeps things between runs."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "SurveyPlanner")
    os.makedirs(path, exist_ok=True)
    return path


def _clock(seconds: float) -> str:
    """A duration a person can read at a glance."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return str(int(seconds)) + "s"
    return str(int(seconds // 60)) + "m " + str(int(seconds % 60)) + "s"


CARET_OPEN ="\u25be"
CARET_SHUT = "\u25b8"
DAY_COLOURS = ["#00e5a0", "#ffc400", "#ff6969", "#78c8ff", "#d782ff",
               "#96ff78", "#ff9632", "#50dcdc", "#ff5faf", "#b9b9ff"]


class SurveyPlannerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Survey Planner")
        self.geometry("1280x860")

        self.body = None            # the chosen waterbody, lon/lat rings
        self.frame = None           # LocalFrame for this lake
        self.poly = None            # shapely polygon, local feet
        self.days = []              # list of days, each a list of legs
        self.access = []            # user-marked access points
        self.settings = planning.PlanSettings()
        self.selected = None        # (day index, leg index)
        self.pin = None             # the lon/lat that was pasted
        self._choice_box = None     # view while picking a waterbody
        self.stations = []          # watching positions used by the last plan
        self.basemap = None         # (image, extent) satellite underlay
        self.context = None         # roads, parking, slipways from OSM
        self.candidates = []        # waterbodies found, waiting to be picked
        self.roi_pts = []           # region of interest, lon/lat while drawing
        self.roi = None             # shapely polygon in local feet, or None
        self._picking_access = tk.BooleanVar(value=False)
        self._drawing_roi = tk.BooleanVar(value=False)
        self.no_go = []             # [{name, kind, geom}] in local feet
        self.no_go_pts = []         # corners of the one being drawn
        self._drawing_no_go = tk.BooleanVar(value=False)
        self._view = None           # held xlim/ylim once the map has been moved
        self._drag_from = None      # where a middle-button pan started
        self._basemap_zoom = None   # tile zoom the imagery was fetched at
        self._sharpen_after = None  # pending after() id for a refetch
        self._sharpening = False

        self._build_ui()
        self._draw()

    # ---- interface ---------------------------------------------------------

    def _section(self, parent, title, opened=True):
        """
        A sidebar box that folds away.

        Six sections stacked is taller than a laptop screen, and pack() clips
        what does not fit rather than scrolling it - the export buttons were
        simply not there. Fold the ones you are done with and they come back.
        """
        head = ttk.Frame(parent)
        head.pack(fill="x", pady=(6, 0))
        body = ttk.Frame(parent)
        caret = ttk.Label(head, font=("", 10, "bold"), cursor="hand2")
        caret.pack(anchor="w")
        state = {"open": opened}

        def toggle(_event=None):
            state["open"] = not state["open"]
            if state["open"]:
                body.pack(fill="x", after=head)
            else:
                body.pack_forget()
            caret.configure(text=(CARET_OPEN if state["open"] else CARET_SHUT)
                            + "  " + title)

        caret.bind("<Button-1>", toggle)
        state["open"] = not opened
        toggle()
        self._sections.append((title, state, body, head, toggle))
        return body

    def _scrolling_column(self, width=300):
        """
        The sidebar itself, scrollable.

        Folding sections is the tidy answer; a scrollbar is the honest one.
        With every box open the column is still taller than some screens, and a
        control you cannot reach is a control that does not exist.
        """
        outer = ttk.Frame(self)
        outer.pack(side="left", fill="y")
        canvas = tk.Canvas(outer, width=width, highlightthickness=0,
                           borderwidth=0, takefocus=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="y", expand=True)
        inner = ttk.Frame(canvas, padding=8)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def fit(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window, width=canvas.winfo_width())

        inner.bind("<Configure>", fit)
        canvas.bind("<Configure>", fit)

        def wheel(event):
            canvas.yview_scroll(-1 * (event.delta // 120), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _fold_all(self, opened):
        for _title, state, _body, _head, toggle in self._sections:
            if state["open"] != opened:
                toggle()

    def _build_ui(self):
        self._sections = []
        left = self._scrolling_column()
        right = ttk.Frame(self)
        right.pack(side="right", fill="both", expand=True)

        fold = ttk.Frame(left)
        fold.pack(fill="x")
        ttk.Button(fold, text="Collapse all",
                   command=lambda: self._fold_all(False)).pack(
                       side="left", expand=True, fill="x")
        ttk.Button(fold, text="Expand all",
                   command=lambda: self._fold_all(True)).pack(
                       side="left", expand=True, fill="x")
        ttk.Button(fold, text="Reset view",
                   command=self.on_reset_view).pack(
                       side="left", expand=True, fill="x")

        self.vars = {}
        water = self._section(left, "1. Water")
        find = ttk.Frame(water); find.pack(fill="x", pady=(2, 8))
        ttk.Label(find, text="Paste a Google Maps pin, or type lat, lon",
                  foreground="#555").pack(anchor="w")
        self.pin_var = tk.StringVar(value="38.10535, -91.45444")
        entry = ttk.Entry(find, textvariable=self.pin_var, width=34)
        entry.pack(fill="x", pady=2)
        entry.bind("<Return>", lambda _e: self.on_fetch())
        ttk.Button(find, text="Find nearest lake or river",
                   command=self.on_fetch).pack(fill="x", pady=2)
        shore = ttk.Frame(find); shore.pack(fill="x")
        ttk.Button(shore, text="Open shoreline...",
                   command=self.on_open_shore).pack(side="left", expand=True, fill="x")
        ttk.Button(shore, text="Save shoreline...",
                   command=self.on_save_shore).pack(side="left", expand=True, fill="x")
        ttk.Button(find, text="Re-compute outline from imagery",
                   command=self.on_refine_shoreline).pack(fill="x", pady=(2, 0))
        self.body_label = ttk.Label(water, text="No waterbody loaded",
                                    foreground="#666", wraplength=260,
                                    justify="left")
        self.body_label.pack(anchor="w", pady=(0, 4))

        access = self._section(left, "2. Shore access")
        ttk.Checkbutton(access, text="Click the map to mark access",
                        variable=self._picking_access).pack(anchor="w")
        self.access_list = tk.Listbox(access, height=4)
        self.access_list.pack(fill="x", pady=2)
        acc = ttk.Frame(access); acc.pack(fill="x")
        ttk.Button(acc, text="Remove", command=self.on_remove_access).pack(side="left")
        ttk.Button(acc, text="Remove all", command=self.on_clear_access).pack(side="left", padx=4)
        acc2 = ttk.Frame(access); acc2.pack(fill="x", pady=(2, 4))
        ttk.Button(acc2, text="Save access points...",
                   command=self.on_save_access).pack(side="left", expand=True, fill="x")
        ttk.Button(acc2, text="Load...",
                   command=self.on_load_access).pack(side="left", expand=True, fill="x")
        ttk.Label(access, text="Right-click the map undoes the last one",
                  foreground="#777").pack(anchor="w")

        region = self._section(left, "3. Region of interest")
        ttk.Checkbutton(region, text="Click to draw a region (right-click closes)",
                        variable=self._drawing_roi,
                        command=self._roi_mode_changed).pack(anchor="w")
        roi = ttk.Frame(region); roi.pack(fill="x", pady=(0, 4))
        ttk.Button(roi, text="Clear region", command=self.on_clear_roi).pack(side="left")
        self.roi_label = ttk.Label(roi, text="whole lake", foreground="#666")
        self.roi_label.pack(side="left", padx=6)

        no_go = self._section(left, "4. No-go areas")
        ttk.Checkbutton(no_go, text="Click to draw a no-go area"
                        " (right-click closes)",
                        variable=self._drawing_no_go,
                        command=self._no_go_mode_changed).pack(anchor="w")
        self.no_go_list = tk.Listbox(no_go, height=4)
        self.no_go_list.pack(fill="x", pady=2)
        ng = ttk.Frame(no_go); ng.pack(fill="x", pady=(0, 4))
        ttk.Button(ng, text="Find in imagery",
                   command=self.on_detect_no_go).pack(side="left")
        ttk.Button(ng, text="Remove",
                   command=self.on_remove_no_go).pack(side="left", padx=4)
        ttk.Button(ng, text="Remove all",
                   command=self.on_clear_no_go).pack(side="left")
        ngio = ttk.Frame(no_go); ngio.pack(fill="x", pady=(2, 4))
        ttk.Button(ngio, text="Save no-go areas...",
                   command=self.on_save_no_go).pack(
                       side="left", expand=True, fill="x")
        ttk.Button(ngio, text="Load...",
                   command=self.on_load_no_go).pack(
                       side="left", expand=True, fill="x")
        size = ttk.Frame(no_go); size.pack(fill="x")
        ttk.Label(size, text="Smallest object to find (sq ft)",
                  width=26).pack(side="left")
        self.min_object_var = tk.StringVar(value="250")
        ttk.Entry(size, textvariable=self.min_object_var,
                  width=8).pack(side="left")
        margin = ttk.Frame(no_go); margin.pack(fill="x")
        ttk.Label(margin, text="Min distance from no-go (ft)",
                  width=26).pack(side="left")
        self.vars["no_go_margin_ft"] = tk.StringVar(value="25")
        ttk.Entry(margin, textvariable=self.vars["no_go_margin_ft"],
                  width=8).pack(side="left")
        ttk.Label(no_go, text="Find in imagery marks bright objects sitting"
                  " on the water. Check them.",
                  foreground="#777", wraplength=260,
                  justify="left").pack(anchor="w")

        params = self._section(left, "5. Parameters")
        for key, label, default in (
            ("spacing_ft", "Line spacing (ft)", 40.0),
            ("setback_ft", "Min distance from shore (ft)", 50.0),
            ("speed_mph", "Boat speed (mph)", 3.0),
            ("day_hours", "Max hours per day", 2.0),
            ("day_min_hours", "Min hours per day", 1.5),
            ("bearing_deg", "Bearing (blank = long axis)", ""),
            ("coverage_pct", "Cover this % of the water", 100.0),
        ):
            row = ttk.Frame(params); row.pack(fill="x")
            ttk.Label(row, text=label, width=26).pack(side="left")
            var = tk.StringVar(value=str(default))
            ttk.Entry(row, textvariable=var, width=8).pack(side="left")
            self.vars[key] = var
        self.blocks_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(params, text="Square blocks, nearest launch first",
                        variable=self.blocks_var).pack(anchor="w", pady=(4, 0))
        self.los_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(params, text="Only water in sight from shore",
                        variable=self.los_var).pack(anchor="w")
        row = ttk.Frame(params); row.pack(fill="x")
        ttk.Label(row, text="Sight range ft (0 = no limit)", width=26).pack(side="left")
        self.vars["sight_range_ft"] = tk.StringVar(value="0")
        ttk.Entry(row, textvariable=self.vars["sight_range_ft"],
                  width=8).pack(side="left")
        self.shore_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(params, text="Shore-following first transect",
                        variable=self.shore_var).pack(anchor="w",
                                                      pady=(4, 0))
        row = ttk.Frame(params); row.pack(fill="x")
        ttk.Label(row, text="Turn limit (deg per 10 m)",
                  width=26).pack(side="left")
        self.vars["turn_limit_deg"] = tk.StringVar(value="50")
        ttk.Entry(row, textvariable=self.vars["turn_limit_deg"],
                  width=8).pack(side="left")
        self.ortho_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(params, text="Add orthogonal pass",
                        variable=self.ortho_var).pack(anchor="w", pady=(4, 0))
        row = ttk.Frame(params); row.pack(fill="x")
        ttk.Label(row, text="Orthogonal spacing (ft)", width=26).pack(side="left")
        self.vars["orthogonal_spacing_ft"] = tk.StringVar(value="80.0")
        ttk.Entry(row, textvariable=self.vars["orthogonal_spacing_ft"],
                  width=8).pack(side="left")
        ttk.Button(params, text="Compute plan",
                   command=self.on_compute).pack(fill="x", pady=(8, 2))
        self.estimate_label = ttk.Label(
            params, text="", foreground="#8fb8d8", wraplength=260,
            justify="left")
        self.estimate_label.pack(anchor="w", pady=(0, 6))
        # Every parameter changes the answer, so the estimate follows
        # them rather than waiting to be asked for.
        for var in list(self.vars.values()) + [
                self.blocks_var, self.los_var, self.ortho_var,
                self.shore_var]:
            var.trace_add("write", lambda *_a: self._estimate_soon())

        plan = self._section(left, "6. Plan")
        self.tree = ttk.Treeview(plan, columns=("day", "lines", "mi", "hrs"),
                                 show="headings", height=8)
        for col, text, width in (("day", "day", 40), ("lines", "lines", 50),
                                 ("mi", "miles", 60), ("hrs", "hours", 60)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="e")
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._draw())
        ttk.Button(plan, text="Delete selected line",
                   command=self.on_delete_line).pack(fill="x", pady=(4, 0))

        export = self._section(left, "7. Export")
        ttk.Button(export, text="GPX (per day)...",
                   command=lambda: self.on_export("gpx")).pack(fill="x")
        ttk.Button(export, text="QGroundControl .plan (per day)...",
                   command=lambda: self.on_export("plan")).pack(fill="x", pady=2)
        ttk.Button(export, text="GeoJSON (whole plan)...",
                   command=lambda: self.on_export("geojson")).pack(fill="x")

        self.status = ttk.Label(left, text="Ready", foreground="#444",
                                wraplength=250, justify="left")
        self.status.pack(anchor="w", pady=(10, 0))

        self.figure = Figure(figsize=(7, 8), dpi=100)
        self.ax = self.figure.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.figure, master=right)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.canvas, right).update()
        self.canvas.mpl_connect("button_press_event", self.on_click_map)
        # Wheel to zoom, middle-drag to pan. The toolbar's own pan and zoom
        # tools still work; these are here because reaching for a toolbar to
        # look at the next cove is not how anyone reads a map.
        self.canvas.mpl_connect("scroll_event", self.on_scroll_map)
        self.canvas.mpl_connect("button_press_event", self._drag_start)
        self.canvas.mpl_connect("motion_notify_event", self._drag_move)
        self.canvas.mpl_connect("button_release_event", self._drag_end)

    # ---- actions -----------------------------------------------------------

    def _busy(self, title: str, detail: str = "", on_cancel=None):
        """A modal note that something is happening.

        Fetching a shoreline goes over the network and can take several
        seconds. Without this the window simply sits there and the honest
        reading is that the button did nothing.

        Pass `on_cancel` and the dialog grows a Cancel button. Anything
        that waits on somebody else's server needs one: the NHD stalls for
        a minute at a time for no reason a caller can see, and a modal box
        with no way out is the wrong thing to be looking at when it does.
        """
        self._done()
        win = tk.Toplevel(self)
        win.title("Working")
        win.transient(self)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        ttk.Label(win, text=title, font=("", 10, "bold"),
                  padding=(16, 12, 16, 2)).pack(anchor="w")
        self._busy_detail = tk.StringVar(value=detail)
        ttk.Label(win, textvariable=self._busy_detail, foreground="#555",
                  padding=(16, 0, 16, 2), wraplength=320).pack(anchor="w")
        self._busy_note = tk.StringVar(value="")
        ttk.Label(win, textvariable=self._busy_note, foreground="#888",
                  padding=(16, 0, 16, 8), wraplength=320).pack(anchor="w")
        bar = ttk.Progressbar(win, mode="indeterminate", length=320)
        bar.pack(padx=16, pady=(0, 6 if on_cancel else 14))
        bar.start(12)
        self._busy_bar = bar
        self._busy_started = time.time()
        self._busy_determinate = False
        if on_cancel is not None:
            def cancelled():
                self._done()
                on_cancel()

            ttk.Button(win, text="Cancel", command=cancelled).pack(
                padx=16, pady=(0, 12))
            win.protocol("WM_DELETE_WINDOW", cancelled)
            win.bind("<Escape>", lambda _e: cancelled())
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 3
        win.geometry("+" + str(max(x, 0)) + "+" + str(max(y, 0)))
        try:
            win.grab_set()
        except tk.TclError:
            pass
        self._busy_win = win
        self.update()

    def _later(self, work, on_done, every_ms=120):
        """Run `work()` off the main thread, deliver its result on it.

        The one-shot form of _pump, for background context that nothing
        waits on. Same rule: the worker returns a value and never
        touches a widget. A failure is swallowed because this is only
        ever used for the map underlay, which the plan does not need.
        """
        mailbox = queue.Queue()

        def run():
            try:
                mailbox.put(("done", work()))
            except Exception as exc:
                mailbox.put(("failed", exc))

        def poll():
            try:
                kind, payload = mailbox.get_nowait()
            except queue.Empty:
                self.after(every_ms, poll)
                return
            if kind == "done":
                on_done(payload)

        threading.Thread(target=run, daemon=True).start()
        self.after(every_ms, poll)

    def report_callback_exception(self, kind, value, trace):
        """Show the failure rather than dying quietly.

        Tk prints an unhandled callback exception to stderr, and a
        windowed program has no stderr anyone is reading - the window
        simply stops working, which is indistinguishable from a crash
        and impossible to report usefully. This puts the traceback on
        screen and appends it to a log, so a fault arrives with its
        cause attached.
        """
        import traceback

        text = "".join(traceback.format_exception(kind, value, trace))
        path = os.path.join(_app_dir(), "errors.log")
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + NEWLINE
                         + text + NEWLINE)
        except OSError:
            path = "(could not be written)"
        self._imagery_busy = False
        try:
            self._done()
            self._say("Something went wrong: " + str(value))
        except Exception:
            pass
        messagebox.showerror(
            "Survey Planner",
            str(value) + NEWLINE + NEWLINE + text.strip().splitlines()[-1]
            + NEWLINE + NEWLINE + "The full details were written to"
            + NEWLINE + path)

    def _pump(self, mailbox, stop, on_done, on_failed, every_ms=80):
        """Drain a worker's mailbox on the Tk thread.

        Tkinter is not thread-safe, and `after` is not the exception -
        it calls createcommand, which from a background thread raises
        "main thread is not in main loop" and takes the window with it.
        A progress callback firing four hundred times during a tile
        fetch made that a near certainty, and that was the crash.

        So the worker only ever puts messages in a queue. This runs on
        the main thread, reschedules itself, and is the only thing that
        touches a widget. It also drops all but the newest progress
        message, because rendering four hundred of them is wasted work
        for a label nobody can read that fast.
        """
        latest, outcome = None, None
        try:
            while True:
                kind, payload = mailbox.get_nowait()
                if kind == "progress":
                    latest = payload
                else:
                    outcome = (kind, payload)
        except queue.Empty:
            pass
        if stop.is_set():
            return
        if latest is not None:
            self._busy_step(*latest)
        if outcome is None:
            self.after(every_ms, lambda: self._pump(
                mailbox, stop, on_done, on_failed, every_ms))
            return
        if outcome[0] == "failed":
            on_failed(outcome[1])
        else:
            on_done(outcome[1])

    def _busy_step(self, fraction, stage: str, detail: str = ""):
        """Advance the bar and say what is happening, in words.

        A spinner on a job that takes minutes is indistinguishable from
        a hang, which is exactly how this read. A percentage, the stage
        by name, and the elapsed time make the difference between
        "working" and "stuck" visible without having to guess.
        """
        if getattr(self, "_busy_win", None) is None:
            return
        bar = getattr(self, "_busy_bar", None)
        if bar is not None and fraction is not None:
            if not self._busy_determinate:
                bar.stop()
                bar.configure(mode="determinate", maximum=1000)
                self._busy_determinate = True
            bar.configure(value=max(0.0, min(1.0, fraction)) * 1000)
        elapsed = time.time() - getattr(self, "_busy_started", time.time())
        line = stage.capitalize() if stage else ""
        if fraction is not None:
            line += "  " + format(fraction * 100, ".0f") + "%"
        self._busy_detail.set(line + ELLIPSIS)
        note = detail
        if elapsed > 3.0:
            note += ("  -  " if note else "") + _clock(elapsed) + " elapsed"
            # Only guess at what is left once there is enough of the job
            # behind us for the guess to mean anything.
            # ...and stop guessing once it is nearly done, rather than
            # promising "about 0s to go".
            if fraction and 0.08 < fraction < 0.97:
                left = elapsed * (1.0 - fraction) / fraction
                note += ", about " + _clock(left) + " to go"
        self._busy_note.set(note)
        # update_idletasks, not update: this already runs from the event
        # loop, and reprocessing the queue from inside it invites the
        # callback to re-enter itself.
        self.update_idletasks()

    def _busy_say(self, text: str):
        if getattr(self, "_busy_win", None) is not None:
            self._busy_detail.set(text)
            self.update()

    def _done(self):
        win = getattr(self, "_busy_win", None)
        if win is not None:
            try:
                win.grab_release()
            except tk.TclError:
                pass
            win.destroy()
            self._busy_win = None
            self._busy_bar = None
            self._busy_determinate = False

    def _say(self, text: str):
        self.status.config(text=text)
        self.update_idletasks()

    def on_fetch(self):
        try:
            lat, lon = shoreline.parse_location(self.pin_var.get())
        except shoreline.ShorelineError as exc:
            return messagebox.showerror("Survey Planner", str(exc))
        self.pin = (lon, lat)
        stop = threading.Event()
        self._fetch_stop = stop
        self._busy("Searching the National Hydrography Dataset",
                   "Looking for lakes and rivers near "
                   + format(lat, '.5f') + ", " + format(lon, '.5f'),
                   on_cancel=lambda: self._fetch_cancelled(stop))

        mailbox = queue.Queue()

        def retry_note(_layer, attempt, total):
            # Into the queue, never at a widget: see _pump.
            mailbox.put(("progress", (None, "",
                                      "The map service is slow. Trying"
                                      " again (" + str(attempt) + " of "
                                      + str(total) + ").")))

        def work():
            try:
                bodies = shoreline.fetch_waterbodies(
                    lon, lat, cancel=stop, on_retry=retry_note)
            except shoreline.ShorelineError as exc:
                mailbox.put(("failed", str(exc)))
                return
            mailbox.put(("done", bodies))

        threading.Thread(target=work, daemon=True).start()
        self._pump(mailbox, stop,
                   lambda bodies: self._fetch_done(bodies, stop),
                   lambda message: self._fetch_failed(message, stop))

    def _fetch_cancelled(self, stop):
        """Give up on the fetch.

        The request itself cannot be torn out of urllib mid-read, so the
        worker is told to stop and its answer is discarded. It gives up
        within one attempt timeout; the window comes back now.
        """
        stop.set()
        self._say("Search cancelled.")

    def _fetch_failed(self, message: str, stop=None):
        if stop is not None and stop.is_set():
            return                       # cancelled; the failure is moot
        self._done()
        self._say("Fetch failed.")
        messagebox.showerror("Survey Planner", message)

    def _fetch_done(self, bodies, stop=None):
        if stop is not None and stop.is_set():
            return                       # cancelled; do not redraw the map
        self._done()
        if not bodies:
            self._say("No waterbody found there.")
            return messagebox.showinfo("Survey Planner",
                                       "The NHD has no waterbody within a mile "
                                       "of that point.")
        near = bodies[0]
        if len(bodies) == 1 and near["distance_ft"] < 1.0:
            return self._use_body(near)
        self._offer_choice(bodies)

    def _offer_choice(self, bodies):
        """Draw every waterbody that was found and let one be clicked.

        A list of names is a poor way to answer 'which water did you
        mean': lakes near each other often share a name, an unnamed pond
        has none at all, and the arm of a reservoir looks like a separate
        body until you see it. On the map the question answers itself.
        """
        self.candidates = bodies
        self.body = None
        self.days = []
        self.basemap, self.context = None, None
        west = min(p[0] for b in bodies for p in b["rings"][0])
        east = max(p[0] for b in bodies for p in b["rings"][0])
        south = min(p[1] for b in bodies for p in b["rings"][0])
        north = max(p[1] for b in bodies for p in b["rings"][0])
        pad_x = (east - west) * 0.08 or 0.002
        pad_y = (north - south) * 0.08 or 0.002
        self._choice_box = (west - pad_x, south - pad_y, east + pad_x, north + pad_y)
        self.body_label.config(text=str(len(bodies)) + " nearby - click one",
                               foreground="#000")
        small = sum(1 for b in bodies if b["acres"] < 5.0)
        extra = (" " + str(small) + " are ponds under 5 acres - unlabelled,"
                 " but still clickable." if small else "")
        self._say("Click the outline you want to survey - anywhere inside it,"
                  " or nearest it if it is small. Each outline is one shape"
                  " the National Hydrography Dataset publishes, labelled with"
                  " its name, area, and how far it is from your pin; the one"
                  " your pin landed inside says so." + extra
                  + " Right-click does nothing here - a left click picks."
                  + " If none of them is the water you meant, edit the pin"
                  " and search again.")
        self._draw()

        box = self._choice_box
        self._later(lambda: basemap.fetch_satellite(box),
                    lambda got: self._choice_imagery(*got))

    def _choice_imagery(self, image, extent):
        if image is not None and self.candidates:
            self.basemap = (image, extent)
            self._draw()

    def _pick_candidate(self, lon, lat):
        """Whichever outline was clicked, or the nearest to the click."""
        from shapely.geometry import Point, Polygon

        best, best_d = None, float("inf")
        for body in self.candidates:
            frame = LocalFrame.centred_on(body["rings"][0])
            poly = Polygon(frame.ring_to_ft(body["rings"][0]),
                           [frame.ring_to_ft(r) for r in body["rings"][1:]])
            if not poly.is_valid:
                poly = poly.buffer(0)
            here = Point(*frame.to_ft(lon, lat))
            d = 0.0 if poly.contains(here) else poly.distance(here)
            if d < best_d:
                best, best_d = body, d
        if best is None:
            return
        self.candidates = []
        self._choice_box = None
        self._use_body(best)

    def _choose_body(self, bodies):
        win = tk.Toplevel(self); win.title("Which water?"); win.transient(self)
        ttk.Label(win, text="More than one waterbody is near that point:",
                  padding=8).pack(anchor="w")
        listbox = tk.Listbox(win, width=56, height=min(10, len(bodies)))
        for b in bodies:
            where = ("the pin is inside it" if b["distance_ft"] < 1.0
                     else f"{b['distance_ft']:,.0f} ft away")
            listbox.insert("end", f"{b['name']}  [{b['kind']}]  "
                                  f"{b['acres']:,.0f} acres  -  {where}")
        listbox.selection_set(0)
        listbox.pack(padx=8)

        def pick():
            sel = listbox.curselection()
            if sel:
                self._use_body(bodies[sel[0]])
            win.destroy()

        ttk.Button(win, text="Use this one", command=pick).pack(pady=8)

    def _use_body(self, body):
        self.body = body
        self.frame = LocalFrame.centred_on(body["rings"][0])
        self.poly = shoreline.to_polygon(body, self.frame)
        self.days, self.selected = [], None
        # Where you can put a boat in belongs to the lake, not to the plan,
        # so it comes back with the lake.
        self.access = shoreline.load_access(body)
        self.access_list.delete(0, "end")
        for point in self.access:
            lon, lat = point["lonlat"]
            self.access_list.insert("end",
                                    point["name"] + "  "
                                    + format(lat, '.5f') + ", "
                                    + format(lon, '.5f'))
        self.body_label.config(
            text=f"{body['name']} ({body.get('kind','lake')}) — "
                 f"{self.poly.area/43560:,.0f} acres, "
                 f"{len(body['rings'])-1} island(s)",
            foreground="#000")
        self.basemap, self.context = None, None
        # A new lake means the previous no-go areas are meaningless.
        # A new waterbody starts framed on itself; a zoom held over from
        # the last lake would land somewhere off this one entirely.
        self._view = None
        self._outline_key, self._outline_cache = 'x', None
        self.no_go, self.no_go_pts = [], []
        self.no_go_list.delete(0, "end")
        remembered = (" " + str(len(self.access)) + " remembered access point(s)."
                      if self.access else "")
        # Big water gets the warning now rather than after a long
        # Compute: on a sixty-mile river the plan is 745 miles and the
        # routing grid does not fit in memory, and finding that out at
        # the end of a wait is the worst time to find it out.
        oversize = planning.oversized(self.poly)
        big = (" This is large water - zoom in and draw a region of interest"
               " (section 3) over the part you want, or Compute will plan"
               " the whole thing." if oversize else "")
        self._say("Shoreline loaded." + remembered + big
                  + " Fetching imagery and roads…")
        self._load_basemap()
        self._refresh_tree()
        self._draw()
        self._estimate_soon(delay_ms=50)

    def _load_basemap(self):
        """Satellite, roads and parking underneath the outline.

        Fetched on a worker thread and drawn when it arrives - it is
        context, so nothing waits on it and a failure just leaves a plain
        background.
        """
        bounds = basemap.bounds_of(self.body["rings"])

        zoom = basemap.pick_zoom(bounds)

        def work():
            image, extent = basemap.fetch_satellite(bounds, zoom)
            return image, extent, basemap.fetch_context(bounds), zoom

        self._later(work, lambda got: self._basemap_ready(*got))

    def _basemap_ready(self, image, extent, context, zoom=None):
        self.basemap = (image, extent) if image is not None else None
        self._basemap_zoom = zoom if self.basemap else None
        self.context = context
        found = []
        if self.basemap:
            found.append("imagery")
        if context:
            if context["slipways"]:
                found.append(str(len(context["slipways"])) + " slipway(s)")
            if context["parking"]:
                found.append(str(len(context["parking"])) + " car park(s)")
            if context["roads"]:
                found.append(str(len(context["roads"])) + " road(s)")
        self._say(("Loaded " + ", ".join(found) + ".") if found
                  else "No imagery available here - the outline still works.")
        self._draw()

    def on_open_shore(self):
        path = filedialog.askopenfilename(filetypes=[
            ("Shoreline or polygon", "*.json *.geojson *.shp"),
            ("All files", "*.*")])
        if not path:
            return
        try:
            self._use_body(shoreline.load_any(path))
        except Exception as exc:
            messagebox.showerror("Survey Planner", f"Could not read that file: {exc}")

    def _roi_mode_changed(self):
        if self._drawing_roi.get():
            self._picking_access.set(False)
            self._say("Click to place corners; right-click to close the region.")

    def on_save_shore(self):
        if self.body is None:
            return messagebox.showinfo("Survey Planner",
                                       "Nothing to save yet.")
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile=shoreline.body_key(self.body) + ".json",
            filetypes=[("Shoreline JSON", "*.json")])
        if not path:
            return
        body = dict(self.body)
        body["access"] = [{"name": p["name"],
                             "lonlat": list(p["lonlat"])}
                            for p in self.access]
        try:
            shoreline.save(body, path)
            shoreline.save_access(self.body, self.access)
        except OSError as exc:
            return messagebox.showerror("Survey Planner", str(exc))
        self._say("Saved the outline and " + str(len(self.access))
                  + " access point(s) to " + path)

    def on_click_map(self, event):
        if event.xdata is None or event.ydata is None:
            return
        if self.candidates:
            return self._pick_candidate(event.xdata, event.ydata)
        if self.frame is None:
            return
        if self._drawing_no_go.get():
            return self._no_go_click(event)
        if self._drawing_roi.get():
            return self._roi_click(event)
        if self._picking_access.get():
            if event.button == 3:            # right-click undoes the last mark
                return self._undo_access()
            return self._access_click(event)

    def _access_click(self, event):
        """
        Put the mark on the shoreline, not where the click landed.

        A click is never exactly on the line. Left alone, a point a few feet
        inland reads as dry ground and one a few feet out reads as a boat
        already afloat - neither is what "where I put in" means.
        """
        (lon, lat), moved = shoreline.snap_to_shore(self.body, self.frame,
                                                    event.xdata, event.ydata)
        name = f"ACCESS {len(self.access)+1}"
        self.access.append({"lonlat": (lon, lat), "name": name})
        self.access_list.insert("end", f"{name}  {lat:.5f}, {lon:.5f}")
        self._say(f"{name} snapped {moved:.0f} ft onto the shoreline.")
        self._remember_access()
        self._draw()

    def _undo_access(self):
        if not self.access:
            return self._say("No access points to undo.")
        gone = self.access.pop()
        self.access_list.delete(len(self.access))
        self._say(gone["name"] + " removed.")
        self._remember_access()
        self._draw()

    def _roi_click(self, event):
        if event.button == 3:                     # right-click closes the ring
            return self._close_roi()
        self.roi_pts.append((event.xdata, event.ydata))
        self._say(f"{len(self.roi_pts)} corner(s) - right-click to close.")
        self._draw()

    def _close_roi(self):
        from shapely.geometry import Polygon
        if len(self.roi_pts) < 3:
            self._say("A region needs at least three corners.")
            return
        ring = [self.frame.to_ft(lon, lat) for lon, lat in self.roi_pts]
        poly = Polygon(ring)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if self.poly is not None:
            overlap = poly.intersection(self.poly)
            if overlap.is_empty:
                self._say("That region does not touch the water.")
                return
            poly = overlap
        self.roi = poly
        self._drawing_roi.set(False)
        self._estimate_soon(delay_ms=50)
        self.roi_label.config(text=f"{poly.area/43560:,.0f} acres", foreground="#000")
        self._say(f"Region set: {poly.area/43560:,.0f} acres of water. Compute to plan it.")
        self._draw()

    def _no_go_mode_changed(self):
        if self._drawing_no_go.get():
            self._drawing_roi.set(False)
            self._picking_access.set(False)
            self._say("Click the corners of the area to avoid,"
                      " right-click to close it.")
        self.no_go_pts = []
        self._draw()

    def _no_go_click(self, event):
        if event.button == 3:
            return self._close_no_go()
        self.no_go_pts.append((event.xdata, event.ydata))
        self._say(str(len(self.no_go_pts))
                  + " corner(s) - right-click to close.")
        self._draw()

    def _close_no_go(self):
        from shapely.geometry import Polygon
        if len(self.no_go_pts) < 3:
            self._say("A no-go area needs at least three corners.")
            return
        ring = [self.frame.to_ft(lon, lat) for lon, lat in self.no_go_pts]
        zone = Polygon(ring)
        if not zone.is_valid:
            zone = zone.buffer(0)
        self.no_go_pts = []
        self._drawing_no_go.set(False)
        self._add_no_go({"name": "drawn " + str(len(self.no_go) + 1),
                         "kind": "drawn", "geom": zone})

    def _add_no_go(self, zone, redraw: bool = True):
        """Add one no-go area.

        `redraw` exists because the imagery finds them by the hundred.
        Redrawing after each one is quadratic - every redraw draws every
        zone added so far - and 265 docks took 131 seconds with the
        window frozen throughout, which reads as a crash and is not
        meaningfully different from one. Add in bulk, draw once.
        """
        self.no_go.append(zone)
        self.no_go_list.insert("end", zone["name"] + "  -  "
                               + format(zone["geom"].area / 43560.0, ",.2f")
                               + " acres")
        if redraw:
            self._say(str(len(self.no_go)) + " no-go area(s). Compute to"
                      " plan around them.")
            self._draw()

    def _detect_from_imagery(self, then):
        """Threshold the imagery for bright objects sitting on the water.

        Several hundred tiles and a pass over all of them takes tens of
        seconds, so it runs on a worker with the dialog cancellable. `then`
        is handed the zones back on the Tk thread.
        """
        if self.poly is None or self.body is None:
            return messagebox.showinfo("Survey Planner",
                                       "Load a shoreline first.")
        # Two runs at once means two workers, two dialogs of which only
        # the last can be cancelled, and every zone added twice.
        if getattr(self, "_imagery_busy", False):
            return
        self._imagery_busy = True
        try:
            floor = float(self.min_object_var.get())
        except ValueError:
            floor = imagery.MIN_AREA_FT2
        stop = threading.Event()
        self._busy("Reading the imagery", "Fetching tiles…",
                   on_cancel=lambda: self._imagery_cancelled(stop))
        bounds = basemap.bounds_of(self.body["rings"], pad_frac=0.02)
        poly, frame = self.poly, self.frame

        mailbox = queue.Queue()

        def note(fraction, stage="", detail=""):
            if stop.is_set():
                raise imagery.ImageryError("cancelled")
            mailbox.put(("progress", (fraction, stage, detail)))

        def work():
            try:
                zones = imagery.find_obstructions(poly, frame, bounds,
                                                  min_area_ft2=floor,
                                                  progress=note)
            except Exception as exc:
                mailbox.put(("failed", str(exc)))
                return
            mailbox.put(("done", zones))

        threading.Thread(target=work, daemon=True).start()
        self._pump(mailbox, stop,
                   lambda zones: self._imagery_done(zones, stop, then),
                   lambda message: self._imagery_failed(message, stop))

    def _imagery_cancelled(self, stop):
        self._imagery_busy = False
        stop.set()
        self._say("Imagery search cancelled.")

    def _imagery_failed(self, message, stop):
        self._imagery_busy = False
        if stop.is_set():
            return
        self._done()
        self._say("Imagery search failed.")
        messagebox.showerror("Survey Planner", message)

    def _imagery_done(self, zones, stop, then):
        self._imagery_busy = False
        if stop.is_set():
            return
        self._done()
        then(zones)

    def on_detect_no_go(self):
        """Find things sitting in the water and list them as no-go."""
        def keep(zones):
            # A piece the size of a housing estate against the bank is the
            # drawn outline being wrong, not something to steer around. Those
            # belong to "Re-compute outline", which fixes the shape instead of
            # decorating it with a no-go area the size of a cove.
            wrong = [z for z in zones if z["kind"] == "shoreline correction"]
            zones = [z for z in zones if z["kind"] != "shoreline correction"]
            if not zones:
                return self._say(NOTHING_FOUND)
            for zone in zones:
                self._add_no_go(zone, redraw=False)
            self._draw()
            note = ""
            if wrong:
                acres = sum(z["area_ft2"] for z in wrong) / 43560.0
                note = (" " + str(len(wrong)) + " larger piece(s), "
                        + format(acres, ".1f") + " acres, look like the outline"
                        " claiming dry land - use Re-compute outline for those.")
            self._say(self._imagery_summary(zones) + " Added as no-go areas."
                      + note + " " + IMAGERY_CAVEAT)

        self._detect_from_imagery(keep)

    def on_refine_shoreline(self):
        """Cut what the imagery found out of the shoreline itself.

        A dock reaching in from the bank becomes a notch in the outline; an
        island or a moored raft becomes a hole. From there everything
        downstream - the setback, the blocks, the routing - treats it as
        shore, which is what it is.
        """
        def rebuild(zones):
            if not zones:
                return self._say(NOTHING_FOUND + " The outline is unchanged.")
            before = self.poly.area
            refined = imagery.refine_polygon(self.poly, zones)
            lost = (before - refined.area) / before
            if lost > 0.05:
                # Losing a twentieth of a lake is not a lake with a lot of
                # docks, it is a threshold that went wrong.
                return messagebox.showwarning(
                    "Survey Planner",
                    "That would remove " + format(lost * 100, ".0f")
                    + "% of the water, which is too much to be docks. The"
                    + " imagery has probably caught glint or sediment, so"
                    + " the outline has been left alone.")
            self.poly = refined
            self.body["rings"] = imagery.polygon_to_rings(refined, self.frame)
            self.days = []
            self._refresh_tree()
            self._draw()
            self._say(self._imagery_summary(zones) + " Outline rebuilt: "
                      + format(refined.area / 43560.0, ",.1f") + " acres, "
                      + str(len(refined.interiors)) + " hole(s). Fetch or open"
                      + " the shoreline again to undo. " + IMAGERY_CAVEAT)

        self._detect_from_imagery(rebuild)

    def _imagery_summary(self, zones) -> str:
        kinds = {}
        for zone in zones:
            kinds[zone["kind"]] = kinds.get(zone["kind"], 0) + 1
        area = sum(z["area_ft2"] for z in zones) / 43560.0
        return ("Found " + ", ".join(str(n) + " " + k
                                     for k, n in sorted(kinds.items()))
                + " - " + format(area, ".2f") + " acres.")

    def on_save_no_go(self):
        """Keep the no-go areas with the water they belong to.

        Both places at once, deliberately. The store beside the program
        is what makes fetching the same lake next season bring the docks
        back without being asked; the file is what lets you hand them to
        somebody else. Neither is much use on its own.
        """
        if not self.no_go:
            return messagebox.showinfo("Survey Planner",
                                       "There are no no-go areas to save.")
        if self.body is None or self.frame is None:
            return messagebox.showinfo("Survey Planner",
                                       "Load a shoreline first.")
        remembered = shoreline.save_no_go(self.body, self.no_go, self.frame)
        path = filedialog.asksaveasfilename(
            title="Save no-go areas",
            defaultextension=".json",
            initialfile=shoreline.body_key(self.body) + "_nogo.json",
            filetypes=[("No-go areas", "*.json"), ("All files", "*.*")])
        if path:
            shoreline.save_no_go(self.body, self.no_go, self.frame, path)
            self._say(str(len(self.no_go)) + " no-go area(s) saved to "
                      + os.path.basename(path)
                      + ", and remembered for this water.")
        else:
            self._say(str(len(self.no_go)) + " no-go area(s) remembered"
                      " for this water: " + remembered)

    def on_load_no_go(self):
        """Read no-go areas back from a file."""
        if self.frame is None:
            return messagebox.showinfo("Survey Planner",
                                       "Load a shoreline first.")
        path = filedialog.askopenfilename(
            title="Open no-go areas",
            filetypes=[("No-go areas", "*.json"), ("All files", "*.*")])
        if not path:
            return
        zones = shoreline.load_no_go(self.body or {}, self.frame, path)
        if not zones:
            return messagebox.showinfo(
                "Survey Planner",
                "No no-go areas could be read from that file.")
        for zone in zones:
            self._add_no_go(zone, redraw=False)
        self._draw()
        self._say("Loaded " + str(len(zones)) + " no-go area(s) from "
                  + os.path.basename(path) + ".")

    def on_remove_no_go(self):
        picked = self.no_go_list.curselection()
        if not picked:
            return
        index = picked[0]
        self.no_go.pop(index)
        self.no_go_list.delete(index)
        self._draw()

    def on_clear_no_go(self):
        if not self.no_go:
            return
        count = len(self.no_go)
        self.no_go.clear()
        self.no_go_pts = []
        self.no_go_list.delete(0, "end")
        self._say("Removed " + str(count) + " no-go area(s).")
        self._draw()

    def on_clear_roi(self):
        self.roi, self.roi_pts = None, []
        self._drawing_roi.set(False)
        self._estimate_soon(delay_ms=50)
        self.roi_label.config(text="whole lake", foreground="#666")
        self._draw()

    def on_clear_access(self):
        if not self.access:
            return
        count = len(self.access)
        self.access.clear()
        self.access_list.delete(0, "end")
        self.stations = []
        self._say(str(count) + " access point(s) removed.")
        self._remember_access()
        self._draw()

    def on_remove_access(self):
        sel = self.access_list.curselection()
        if not sel:
            return
        self.access.pop(sel[0])
        self.access_list.delete(sel[0])
        self._draw()

    def on_save_access(self):
        """Write the access points to a file, and to the local store.

        They are already kept automatically against this waterbody, so the
        button is really about the file: something to put in a shared drive,
        hand to whoever is running the boat, or keep with the survey records.
        """
        if self.body is None:
            return messagebox.showinfo("Survey Planner",
                                       "Load a shoreline first.")
        if not self.access:
            return messagebox.showinfo("Survey Planner",
                                       "There are no access points to save.")
        path = filedialog.asksaveasfilename(
            title="Save shore access points",
            defaultextension=".json",
            initialfile=shoreline.body_key(self.body) + "_access.json",
            filetypes=[("Access points", "*.json"),
                       ("GPX waypoints", "*.gpx")])
        if not path:
            return
        try:
            if path.lower().endswith(".gpx"):
                exporters.write_access_gpx(path, self.access)
            else:
                shoreline.save_access_file(self.body, self.access, path)
            shoreline.save_access(self.body, self.access)
        except OSError as exc:
            return messagebox.showerror("Survey Planner", str(exc))
        self._say("Saved " + str(len(self.access))
                  + " access point(s) to " + path
                  + ", and kept them with this water.")

    def on_load_access(self):
        """Read access points back from a file, replacing what is here."""
        if self.body is None:
            return messagebox.showinfo("Survey Planner",
                                       "Load a shoreline first.")
        path = filedialog.askopenfilename(
            title="Open shore access points",
            filetypes=[("Access points", "*.json"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            points = shoreline.load_access_file(path)
        except (OSError, ValueError) as exc:
            return messagebox.showerror("Survey Planner",
                                        "Could not read that: " + str(exc))
        if not points:
            return messagebox.showinfo("Survey Planner",
                                       "No access points in that file.")
        self.access = points
        self.access_list.delete(0, "end")
        for point in self.access:
            lon, lat = point["lonlat"]
            self.access_list.insert("end", point["name"] + "  "
                                    + format(lat, '.5f') + ", "
                                    + format(lon, '.5f'))
        self._remember_access()
        self._say("Loaded " + str(len(points)) + " access point(s).")
        self._draw()

    def _remember_access(self):
        """Keep this water's access points for next time.

        Stored beside the program and keyed on the waterbody, not written
        into the plan: finding a usable ramp is slow work done once, and it
        should not have to be repeated because the line spacing changed.
        """
        if self.body is None:
            return
        try:
            shoreline.save_access(self.body, self.access)
        except OSError as exc:
            self._say("Could not save access points: " + str(exc))

    def _read_settings(self) -> planning.PlanSettings:
        def number(key, fallback):
            text = self.vars[key].get().strip()
            if text == "":
                return None if key == "bearing_deg" else fallback
            try:
                return float(text)
            except ValueError:
                return fallback
        return planning.PlanSettings(
            spacing_ft=number("spacing_ft", 40.0),
            setback_ft=number("setback_ft", 50.0),
            speed_mph=number("speed_mph", 3.0),
            day_hours=number("day_hours", 2.0),
            day_min_hours=number("day_min_hours", 1.5),
            bearing_deg=number("bearing_deg", None),
            orthogonal=self.ortho_var.get(),
            orthogonal_spacing_ft=number("orthogonal_spacing_ft", 80.0),
            square_blocks=self.blocks_var.get(),
            require_line_of_sight=self.los_var.get(),
            sight_range_ft=number("sight_range_ft", 0.0),
            coverage_pct=number("coverage_pct", 100.0),
            no_go=[z["geom"] for z in self.no_go],
            no_go_margin_ft=number("no_go_margin_ft", 25.0),
            shore_pass=self.shore_var.get(),
            turn_limit_deg=number("turn_limit_deg", 50.0),
        )

    def on_compute(self):
        """Build the plan on a worker thread.

        It used to run here, on the Tk thread, which froze the window for
        as long as it took - and "as long as it took" was minutes on a
        lake with several launches. A frozen window with a progress bar
        that cannot animate is indistinguishable from a crash, and there
        was no way to call it off.
        """
        if self.poly is None:
            return messagebox.showinfo("Survey Planner",
                                       "Load a shoreline first.")
        if getattr(self, "_compute_busy", False):
            return
        self.settings = self._read_settings()
        self._compute_busy = True
        stop = threading.Event()
        self._say("Building the plan…")
        self._busy("Calculating route",
                   "Cutting the water into blocks…",
                   on_cancel=lambda: self._compute_cancelled(stop))

        mailbox = queue.Queue()
        poly, settings = self.poly, self.settings
        roi, frame = self._effective_roi(), self.frame
        self._planned_view = roi is not None and self.roi is None
        access = list(self.access) or None

        def note(text):
            if stop.is_set():
                raise _Cancelled()
            mailbox.put(("progress", (None, "", text)))

        def work():
            try:
                built = planning.build_plan(poly, settings, roi=roi,
                                            access_points=access,
                                            frame=frame, log=note)
            except _Cancelled:
                return
            except Exception as exc:
                mailbox.put(("failed", str(exc)))
                return
            mailbox.put(("done", built))

        threading.Thread(target=work, daemon=True).start()
        self._pump(mailbox, stop,
                   lambda built: self._compute_done(built, stop),
                   lambda message: self._compute_failed(message, stop))

    def _compute_cancelled(self, stop):
        self._compute_busy = False
        stop.set()
        self._say("Plan cancelled.")

    def _compute_failed(self, message, stop):
        self._compute_busy = False
        if stop.is_set():
            return
        self._done()
        self._say("Could not build the plan.")
        messagebox.showerror("Survey Planner", message)

    def _compute_done(self, built, stop):
        self._compute_busy = False
        if stop.is_set():
            return
        self._done()
        self.days, info = built
        self.stations = info.get("stations", [])
        if not self.days:
            # A refusal that knows why says so. The generic hint sent
            # people looking for a setback problem when the answer was
            # that the water is bigger than one plan can hold.
            hint = info.get("error")
            if not hint:
                hint = ("Nothing fits inside that region. Try a bigger region, a "
                        "smaller setback, or closer line spacing."
                        if self.roi is not None else
                        "Nothing fits. Try a smaller setback or closer line spacing.")
            self._say(hint)
            return messagebox.showinfo("Survey Planner", hint)
        bearing = info["bearing_deg"]
        # Each day is laid parallel to its own stretch of bank, so one
        # bearing is the whole truth only when there is no shore pass.
        used = sorted({round(leg["bearing"]) for day in self.days
                       for leg in day if leg.get("bearing") is not None})
        bearing_text = (format(bearing, ".0f") + "°"
                        if len(used) <= 1
                        else str(min(used)) + "-" + str(max(used))
                        + "° by day")
        launches = [self.frame.to_ft(*p["lonlat"]) for p in self.access]
        clearance = planning.check_clearance(self.days, self.poly,
                                             self.settings, launches)
        turns = planning.check_turns(self.days)
        total = sum(polyline_length_ft(l["coords"]) for d in self.days for l in d)
        self._refresh_tree()
        self.selected = None
        self._draw()
        verdict = "OK" if clearance["ok"] else "TOO CLOSE TO SHORE"
        shore_note = ""
        if self.settings.shore_pass and not info.get("shore_track_ft"):
            # Silence here reads as the feature not working. It usually
            # means the water is too narrow to turn in: the smoothing
            # erodes by the turn radius, and a channel narrower than
            # twice that has nothing left after the setback.
            shore_note = (" No shore transect fitted: after the "
                          + format(self.settings.setback_ft, ".0f")
                          + " ft setback the water is too narrow to hold"
                          " a curve inside "
                          + format(self.settings.turn_limit_deg, ".0f")
                          + " deg per 10 m. Raise the turn limit or lower"
                          " the setback.")
        elif info.get("shore_track_ft"):
            limit = self.settings.turn_limit_deg
            worst = turns.get("max_shore_turn_deg", 0.0)
            shore_note = (" Shore transect "
                          + format(info["shore_track_ft"] / MILES_TO_FEET,
                                   ".1f")
                          + " mi, worst turn " + format(worst, ".0f")
                          + " deg of " + format(limit, ".0f") + " allowed"
                          + (" - OVER" if worst > limit + 0.5 else "")
                          + ".")
        if not clearance.get("track_on_water", True):
            # Almost always the walk from a launch that is not at the
            # water's edge, which is unavoidable and not a fault in the
            # plan. Saying how far lets that be told from a survey line
            # actually crossing a spit.
            verdict += (" - " + format(clearance.get("over_land_ft", 0), ",.0f")
                        + " ft of the route is over land, most likely the approach"
                        " from a launch set back from the bank")
        note = ""
        if self.settings.require_line_of_sight:
            note = ("\n" + str(len(self.stations)) + " watching position(s), "
                    + format(info.get("coverage", 0) * 100, ".0f")
                    + "% of the water in sight.")
        if info.get("skipped_acres", 0) > 0.05:
            note += (" " + format(info["skipped_acres"], '.0f')
                     + " acres left out to meet the coverage target.")
        if info.get("short_days"):
            note += (" " + str(info["short_days"]) + " day(s) fall below the minimum:"
                     " the water they cover does not touch another day's, so it"
                     " cannot be folded in without splitting an outing in two.")
        if getattr(self, '_planned_view', False):
            note += (" Planned the water on screen - zoom out and"
                     " compute again for more, or draw a region to fix"
                     " it in place.")
        note += shore_note
        if info.get("over_budget"):
            note += (" " + str(info["over_budget"])
                     + " day(s) run over - the trip out is the cost.")
        if self.settings.require_line_of_sight:
            if info.get("unseen_acres", 0) > 0.05:
                note += (" " + format(info["unseen_acres"], ".1f")
                         + " acres are not visible from anywhere marked"
                         + " and were left out.")
        self._say(f"{len(self.days)} days, {total/MILES_TO_FEET:.1f} mi of line, "
                  f"{total/MILES_TO_FEET/self.settings.speed_mph:.1f} h at "
                  f"{self.settings.speed_mph:g} mph.\n"
                  f"Bearing {bearing_text}. Closest to shore "
                  f"{clearance['min_clearance_ft']:.0f} ft ({verdict}). "
                  f"Max turn {turns['max_turn_deg']:.0f}°." + note)

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for row in planning.summarise(self.days, self.settings):
            self.tree.insert("", "end", iid=str(row["day"]),
                             values=(row["day"], row["lines"],
                                     f"{row['line_mi']:.2f}",
                                     f"{row['hours']:.2f}"))

    def on_delete_line(self):
        if self.selected is None:
            return messagebox.showinfo("Survey Planner",
                                       "Click a survey line on the map first.")
        di, li = self.selected
        del self.days[di][li]
        if not self.days[di]:
            del self.days[di]
        self.selected = None
        self._refresh_tree()
        self._draw()
        self._say("Line removed. Day lengths in the table are updated.")

    def _selected_day(self):
        sel = self.tree.selection()
        return int(sel[0]) - 1 if sel else None

    def on_export(self, kind: str):
        if not self.days:
            return messagebox.showinfo("Survey Planner", "Compute a plan first.")
        folder = filedialog.askdirectory(title="Where should the files go?")
        if not folder:
            return
        written = []
        try:
            if kind == "geojson":
                written.append(exporters.write_geojson(
                    os.path.join(folder, "survey_plan.geojson"),
                    self.days, self.frame, self.access))
            else:
                for i, day in enumerate(self.days, start=1):
                    name = f"day_{i:02d}.{'gpx' if kind == 'gpx' else 'plan'}"
                    path = os.path.join(folder, name)
                    if kind == "gpx":
                        written.append(exporters.write_gpx(
                            path, day, self.frame, f"day{i}", self.access))
                    else:
                        home = self.access[0]["lonlat"] if self.access else None
                        written.append(exporters.write_qgc_plan(
                            path, day, self.frame, self.settings.speed_mph, home))
            if self.body:
                shoreline.save(self.body, os.path.join(folder, "shoreline.json"))
        except Exception as exc:
            return messagebox.showerror("Survey Planner", f"Export failed: {exc}")
        self._say(f"Wrote {len(written)} file(s) to {folder}")

    # ---- drawing -----------------------------------------------------------

    def on_pick_line(self, event):
        pass

    def _draw_choice(self):
        """Every candidate outline, labelled, over imagery if it arrived."""
        if self.basemap is not None:
            image, extent = self.basemap
            west, south, east, north = extent
            self.ax.imshow(image, extent=(west, east, south, north),
                           origin="upper", interpolation="bilinear", zorder=0)
        # A wide search turns up every farm pond for miles. They stay on
        # the map and stay clickable, but only water worth surveying gets a
        # label - twenty overlapping captions answer nothing.
        worth = sorted(self.candidates,
                       key=lambda b: (-b["acres"], b["distance_ft"]))[:6]
        named = {id(b) for b in worth
                 if b["acres"] >= 5.0 or b["distance_ft"] < 1.0}
        for i, body in enumerate(self.candidates):
            colour = DAY_COLOURS[i % len(DAY_COLOURS)]
            ring = body["rings"][0]
            big = id(body) in named
            self.ax.fill([p[0] for p in ring], [p[1] for p in ring],
                         facecolor=colour, alpha=0.32 if big else 0.55,
                         edgecolor=colour, linewidth=1.8 if big else 1.0,
                         zorder=3)
            lon = sum(p[0] for p in ring) / len(ring)
            lat = sum(p[1] for p in ring) / len(ring)
            if not big:
                self.ax.plot(lon, lat, ".", color=colour, markersize=4,
                             zorder=5)
                continue
            where = ("pin is inside" if body["distance_ft"] < 1.0
                     else format(body["distance_ft"] / 5280.0, ',.1f')
                     + " mi away")
            label = (body["name"] + "\n" + format(body["acres"], ',.0f')
                     + " acres - " + where)
            self.ax.annotate(label, (lon, lat), color="white", fontsize=7,
                             ha="center", zorder=6,
                             path_effects=[pe.withStroke(linewidth=2.5,
                                                         foreground="#000000")])
        if self.pin:
            self.ax.plot(self.pin[0], self.pin[1], "x", color="#ff4d4d",
                         markersize=11, markeredgewidth=2.5, zorder=7)
        if self._choice_box:
            west, south, east, north = self._choice_box
            self.ax.set_xlim(west, east)
            self.ax.set_ylim(south, north)
            mid = (south + north) / 2
            self.ax.set_aspect(1.0 / max(0.1, math.cos(math.radians(mid))))
        self.ax.set_xticks([]); self.ax.set_yticks([])
        self.figure.tight_layout()
        self.canvas.draw_idle()

    def _draw_context(self):
        """Roads with their names, car parks and slipways.

        Slipways are drawn largest because they answer the question the
        access step is really asking: somewhere a boat demonstrably goes in.

        Names are rationed. One per road was not enough of a ration: a long
        river runs past thousands of distinct names - every County Road and
        private lane in the county - and drawing each once still buried the
        water under its own labels. So the longest roads get named and the
        rest are drawn unlabelled, which is what a paper chart does.
        """
        view = self._view_box()
        roads = [r for r in self.context.get("roads", [])
                 if len(r["coords"]) >= 2
                 and self._overlaps(r["coords"], view)]
        for road in roads:
            pts = road["coords"]
            major = road.get("kind") in ("motorway", "trunk",
                                            "primary", "secondary")
            self.ax.plot([p[0] for p in pts], [p[1] for p in pts],
                         color="#e8c17a" if major else "#b9a377",
                         linewidth=1.6 if major else 0.9, alpha=0.85, zorder=1)

        for road in self._roads_worth_naming(roads):
            pts = road["coords"]
            mid = pts[len(pts) // 2]
            self.ax.annotate(road["name"], mid, color="#ffe9b0", fontsize=6,
                             zorder=2, ha="center",
                             path_effects=[pe.withStroke(linewidth=2,
                                                         foreground="#000000")])
        for lot in self.context.get("parking", []):
            pts = lot["coords"]
            if len(pts) >= 3:
                self.ax.fill([p[0] for p in pts], [p[1] for p in pts],
                             facecolor="#6fa8ff", alpha=0.35,
                             edgecolor="#9ec7ff", linewidth=0.8, zorder=2)
            else:
                self.ax.plot(pts[0][0], pts[0][1], "s", color="#6fa8ff",
                             markersize=5, zorder=2)
        ramps = self.context.get("slipways", [])
        # The star says "a boat goes in here" on its own. Repeating the word
        # forty times down a river says nothing the stars did not.
        name_them = len(ramps) <= MAX_SLIPWAY_LABELS
        for ramp in ramps:
            pts = ramp["coords"]
            mid = pts[len(pts) // 2]
            self.ax.plot(mid[0], mid[1], "*", color="#65ff9a",
                         markersize=13, markeredgecolor="black", zorder=4)
            if name_them:
                self.ax.annotate("slipway", mid, color="#65ff9a", fontsize=7,
                                 xytext=(6, 4), textcoords="offset points",
                                 zorder=4,
                                 path_effects=[pe.withStroke(
                                     linewidth=2, foreground="#000000")])

    def _roads_worth_naming(self, roads):
        """
        The few roads whose names fit on the map.

        Sorted by class and then by how much of the map the road actually
        crosses, because a name is only useful if the thing it names is
        findable. Ties are broken by name so the labels do not jump around
        between redraws of the same map.
        """
        rank = {"motorway": 0, "trunk": 1, "primary": 2, "secondary": 3,
                "tertiary": 4}

        def extent(road):
            xs = [p[0] for p in road["coords"]]
            ys = [p[1] for p in road["coords"]]
            return max(max(xs) - min(xs), max(ys) - min(ys))

        named, best = {}, []
        for road in roads:
            name = road.get("name")
            if not name:
                continue
            # One entry per name, kept at its longest run.
            if name not in named or extent(road) > extent(named[name]):
                named[name] = road
        best = sorted(named.values(),
                      key=lambda r: (rank.get(r.get("kind"), 9), -extent(r),
                                     r["name"]))
        return best[:MAX_ROAD_LABELS]

    def _draw(self):
        self.ax.clear()
        self.ax.set_facecolor("#0a1620")
        if self.body is None and self.candidates:
            return self._draw_choice()
        if self.body is None:
            self.ax.text(0.5, 0.5, "Fetch a shoreline to begin",
                         ha="center", va="center", color="#8899a6",
                         transform=self.ax.transAxes)
            self.ax.set_xticks([]); self.ax.set_yticks([])
            self.canvas.draw_idle()
            return

        # Imagery first, then roads, then the water - so the outline reads
        # against the ground it actually sits on and a car park beside the
        # bank is visible when deciding where to put in.
        view = self._view_box()
        if self.basemap is not None:
            # Drawn whole, deliberately. Cropping it to the view was the
            # obvious saving and is the wrong way round: a small array
            # has to be upsampled to fill the canvas, and that costs
            # more than downsampling the large one. Measured zoomed in,
            # 208 ms cropped against 134 ms whole.
            image, extent = self.basemap
            # imshow wants (left, right, bottom, top); the tile fetcher speaks
            # the geographic order (west, south, east, north). Passing one for
            # the other puts the imagery somewhere it cannot be seen.
            west, south, east, north = extent
            self.ax.imshow(image, extent=(west, east, south, north),
                           origin="upper", interpolation="bilinear", zorder=0)

        if self.context:
            self._draw_context()

        water_fill = "none" if self.basemap is not None else "#123449"
        hole_fill = "none" if self.basemap is not None else "#0a1620"
        for outer, holes in self._visible_outline(view):
            self.ax.fill([p[0] for p in outer], [p[1] for p in outer],
                         facecolor=water_fill, edgecolor="#5fd0ff",
                         linewidth=1.6, zorder=3)
            for hole in holes:
                self.ax.fill([p[0] for p in hole], [p[1] for p in hole],
                             facecolor=hole_fill, edgecolor="#5fd0ff",
                             linewidth=1.0, zorder=3)

        only = self._selected_day()
        for di, day in enumerate(self.days):
            if only is not None and di != only:
                continue
            colour = DAY_COLOURS[di % len(DAY_COLOURS)]
            for leg in day:
                # The run out and home, and the hop between lines, are
                # travel. Drawn in the day's colour they read as survey
                # and make a plan look as though it jumps about.
                if leg.get("transit"):
                    hop = [self.frame.to_lonlat(x, y) for x, y in leg["transit"]]
                    self.ax.plot([p[0] for p in hop], [p[1] for p in hop],
                                 ":", color="#9fb4c4", linewidth=1.0,
                                 alpha=0.8, zorder=4)
                lonlat = [self.frame.to_lonlat(x, y) for x, y in leg["coords"]]
                if not self._overlaps(lonlat, view):
                    continue
                if leg.get("is_return"):
                    self.ax.plot([p[0] for p in lonlat], [p[1] for p in lonlat],
                                 ":", color="#9fb4c4", linewidth=1.0,
                                 alpha=0.8, zorder=4)
                    continue
                kind = leg.get("kind")
                if kind == "shore":
                    # Heavier, and outlined in white. It is a curve among
                    # straight lines, but only where the bank actually
                    # bends - along a straight stretch it is just another
                    # green line and there is no telling it apart.
                    self.ax.plot([p[0] for p in lonlat],
                                 [p[1] for p in lonlat],
                                 "-", color=colour, linewidth=3.0,
                                 zorder=6,
                                 path_effects=[pe.withStroke(
                                     linewidth=5.0,
                                     foreground="#ffffff")])
                    continue
                width = 2.4 if kind == "orthogonal" else 1.4
                style = "--" if kind == "orthogonal" else "-"
                self.ax.plot([p[0] for p in lonlat], [p[1] for p in lonlat],
                             style, color=colour, linewidth=width, zorder=5)

        if self.roi is not None:
            ring = self.frame.ring_to_lonlat(self.roi.exterior.coords)
            self.ax.plot([p[0] for p in ring] + [ring[0][0]],
                         [p[1] for p in ring] + [ring[0][1]],
                         "-", color="#ffd24d", linewidth=1.4, alpha=0.9)
        elif self.roi_pts:
            self.ax.plot([p[0] for p in self.roi_pts], [p[1] for p in self.roi_pts],
                         "o--", color="#ffd24d", linewidth=1.0, markersize=4)

        # No-go areas in red, filled, over everything the boat may use, so
        # a line that should not be there is obvious rather than inferred.
        #
        # One collection rather than one artist per zone: imagery on a
        # lake full of docks hands back hundreds, and hundreds of separate
        # fills make every later redraw - a pan, a zoom, clicking a day -
        # slow as well.
        rings = []
        for zone in self.no_go:
            geom = zone["geom"]
            parts = (geom.geoms if geom.geom_type == "MultiPolygon"
                     else [geom])
            for part in parts:
                if part.is_empty:
                    continue
                ring = self.frame.ring_to_lonlat(part.exterior.coords)
                if not self._overlaps(ring, view):
                    continue
                rings.append(ring)
        if rings:
            self.ax.add_collection(PolyCollection(
                rings, facecolors="#e24a4a", alpha=0.35,
                edgecolors="#ff6b6b", linewidths=1.2, zorder=5))
        if self.no_go_pts:
            self.ax.plot([p[0] for p in self.no_go_pts],
                         [p[1] for p in self.no_go_pts],
                         "o--", color="#ff6b6b", linewidth=1.0, markersize=4,
                         zorder=6)

        for xy in self.stations:
            lon, lat = self.frame.to_lonlat(*xy)
            self.ax.plot(lon, lat, "^", color="#ffd24d", markersize=7,
                         markeredgecolor="black", zorder=6)

        for point in self.access:
            lon, lat = point["lonlat"]
            self.ax.plot(lon, lat, "o", color="white", markersize=6,
                         markeredgecolor="black", zorder=7)
            self.ax.annotate(point["name"], (lon, lat), color="white",
                             fontsize=7, xytext=(4, 3), textcoords="offset points")

        # Frame the water. Left to autoscale, the axes follow the imagery,
        # which is deliberately padded wider than the lake.
        #
        # Unless the view has been moved. Every redraw came through here and
        # reset the limits, so a zoom lasted until the next thing that redrew
        # - marking an access point, ticking a box - which made zooming look
        # broken rather than temporary.
        # Framed on the whole waterbody, not on the clipped piece that was
        # just drawn - otherwise Reset view would frame whatever happened
        # to be on screen when it was pressed.
        whole = self.body["rings"][0]
        west = min(p[0] for p in whole); east = max(p[0] for p in whole)
        south = min(p[1] for p in whole); north = max(p[1] for p in whole)
        padx = (east - west) * 0.04 or 0.001
        pady = (north - south) * 0.04 or 0.001
        mid = sum(p[1] for p in whole) / len(whole)
        if self._view is not None:
            self.ax.set_xlim(self._view[0])
            self.ax.set_ylim(self._view[1])
        else:
            self.ax.set_xlim(west - padx, east + padx)
            self.ax.set_ylim(south - pady, north + pady)
        self.ax.set_aspect(1.0 / max(0.1, math.cos(math.radians(mid))))
        self.ax.set_xticks([]); self.ax.set_yticks([])
        self.figure.tight_layout()
        self.canvas.draw_idle()

    # ---- getting around the map -------------------------------------------

    def _estimate_soon(self, delay_ms: int = 400):
        """Refresh the estimate once typing stops.

        A parameter is edited a character at a time and each keystroke
        fires a trace, so estimating on every one would shrink the
        water and re-buffer the polygon four times while somebody types
        a spacing."""
        if getattr(self, '_estimate_after', None) is not None:
            try:
                self.after_cancel(self._estimate_after)
            except Exception:
                pass
        self._estimate_after = self.after(delay_ms, self._refresh_estimate)

    def _refresh_estimate(self):
        """
        What Compute is about to cost, worked out from areas alone.

        Fitted to seven real runs, and honest about it: days land within
        about a fifth and the time within about a quarter. Close enough
        to decide whether to draw a region first, which is the decision
        it exists to support.
        """
        self._estimate_after = None
        if not hasattr(self, 'estimate_label'):
            return
        if self.poly is None:
            self.estimate_label.config(text="")
            return
        try:
            settings = self._read_settings()
            roi = self._effective_roi()
            guess = planning.estimate(self.poly, settings, roi=roi,
                                      access_points=self.access or None,
                                      frame=self.frame)
        except Exception:
            self.estimate_label.config(text="")
            return
        if guess.get('error'):
            self.estimate_label.config(
                text="Cannot plan this: " + guess["error"],
                foreground="#ff9b9b")
            return
        where = (" for the region drawn" if self.roi is not None
                 else " for the water on screen" if roi is not None
                 else " for the whole waterbody")
        text = ("Estimate: about " + str(guess['days']) + " day(s), "
                + format(guess['line_mi'], ',.0f') + " mi of line"
                + where + ". Compute should take roughly "
                + _clock(guess['seconds']) + ".")
        if guess.get('caveat'):
            text += " " + guess['caveat'].capitalize() + "."
        self.estimate_label.config(text=text, foreground="#8fb8d8")

    def _effective_roi(self):
        """
        The region to plan: the one drawn, or else what is on screen.

        Zooming in is how anyone says which part of a river they mean,
        and it is a good deal quicker than drawing a polygon around it.
        So the view counts as a region when none has been drawn - which
        also keeps a sixty-mile river plannable, because the part you
        are looking at is never sixty miles.

        Returns None when the view already holds the whole waterbody,
        so zoomed out nothing changes.
        """
        from shapely.geometry import box

        if self.roi is not None:
            return self.roi
        if self.poly is None or self.frame is None:
            return None
        seen = self._view_box(margin=0.0)
        if seen is None:
            return None
        west, south, east, north = seen
        corners = [self.frame.to_ft(west, south), self.frame.to_ft(east, south),
                   self.frame.to_ft(east, north), self.frame.to_ft(west, north)]
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        window = box(min(xs), min(ys), max(xs), max(ys))
        if window.contains(self.poly):
            return None                # the whole thing is on screen
        clipped = window.intersection(self.poly)
        return None if clipped.is_empty else clipped

    def _visible_outline(self, view):
        """
        The shoreline, cut to what is on screen.

        Handing matplotlib the whole outline and letting it clip is
        slower than clipping first, not faster: a sixteen-thousand
        vertex river took 100 ms a redraw zoomed out and 259 ms zoomed
        in, because clipping that path to a small window is work in
        itself. Shapely does the cut once, in C, and the result is kept
        until the view moves.
        """
        key = None if view is None else tuple(round(v, 7) for v in view)
        if (getattr(self, '_outline_key', 'x') == key
                and getattr(self, '_outline_cache', None) is not None):
            return self._outline_cache

        from shapely.geometry import MultiPolygon, Polygon, box

        rings = self.body["rings"]
        shape = Polygon(rings[0], rings[1:])
        if not shape.is_valid:
            shape = shape.buffer(0)
        if view is not None:
            shape = shape.intersection(box(*view))
        parts = (shape.geoms if isinstance(shape, MultiPolygon)
                 else [shape])
        out = []
        for part in parts:
            if part.is_empty or part.geom_type != "Polygon":
                continue
            out.append((list(part.exterior.coords),
                        [list(h.coords) for h in part.interiors]))
        self._outline_key, self._outline_cache = key, out
        return out

    def _view_box(self, margin: float = 0.15):
        """
        The lon/lat box on screen, with a margin.

        Everything drawn is checked against this first. A river carries
        sixteen thousand vertices and ninety rings; zoomed into one
        bend, all but a handful of them are off screen, and drawing
        them anyway is most of what a redraw costs.

        The margin keeps lines that leave the screen looking like they
        leave it, rather than stopping at the edge.
        """
        if self._view is not None:
            (x0, x1), (y0, y1) = self._view
        else:
            (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        if not (x1 > x0 and y1 > y0):
            return None
        dx, dy = (x1 - x0) * margin, (y1 - y0) * margin
        return (x0 - dx, y0 - dy, x1 + dx, y1 + dy)

    @staticmethod
    def _overlaps(points, box):
        """Whether a run of lon/lat points could touch the view.

        A bounding-box test, not a real intersection: it is run on every
        line of every day and has to cost less than drawing the line
        would."""
        if box is None or not points:
            return True
        west, south, east, north = box
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return not (max(xs) < west or min(xs) > east
                    or max(ys) < south or min(ys) > north)

    def _remember_view(self):
        """Hold the current limits so the next redraw does not throw them away."""
        self._view = (self.ax.get_xlim(), self.ax.get_ylim())

    def on_reset_view(self):
        """Back to the whole waterbody, at the overview imagery."""
        self._view = None
        self._draw()
        self._say("View reset to the whole waterbody.")
        if self.body is not None and self._basemap_zoom is not None:
            overview = basemap.pick_zoom(
                basemap.bounds_of(self.body["rings"]))
            if overview != self._basemap_zoom:
                self._basemap_zoom = None      # let it refetch wide
                self._sharpen_soon(delay_ms=50)

    def on_scroll_map(self, event):
        """
        Wheel zooms about the cursor.

        About the cursor, not the centre: zooming to the middle of the screen
        means chasing the thing you are looking at back into view after every
        step.
        """
        if event.xdata is None or event.ydata is None:
            return
        step = 1 / ZOOM_STEP if event.button == "up" else ZOOM_STEP
        (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        self.ax.set_xlim(event.xdata + (x0 - event.xdata) * step,
                         event.xdata + (x1 - event.xdata) * step)
        self.ax.set_ylim(event.ydata + (y0 - event.ydata) * step,
                         event.ydata + (y1 - event.ydata) * step)
        self._remember_view()
        self.canvas.draw_idle()
        self._sharpen_soon()
        self._estimate_soon()

    def _drag_start(self, event):
        # Only the middle button, and only outside a marking mode: left-click
        # is how access points and regions get placed, and stealing it would
        # cost more than a drag-pan is worth.
        if event.button == 2 and event.xdata is not None:
            self._drag_from = (event.xdata, event.ydata)

    def _drag_move(self, event):
        if self._drag_from is None or event.xdata is None:
            return
        dx = self._drag_from[0] - event.xdata
        dy = self._drag_from[1] - event.ydata
        (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        self.ax.set_xlim(x0 + dx, x1 + dx)
        self.ax.set_ylim(y0 + dy, y1 + dy)
        self._remember_view()
        self.canvas.draw_idle()

    def _drag_end(self, _event):
        self._drag_from = None
        self._sharpen_soon()

    def _sharpen_soon(self, delay_ms: int = 450):
        """
        Ask for better imagery once the map stops moving.

        Not on every wheel notch: a spin of the wheel is a dozen view
        changes in a second, and each would queue its own fetch of
        several hundred tiles. Waiting for the view to settle makes it
        one.
        """
        if self._sharpen_after is not None:
            try:
                self.after_cancel(self._sharpen_after)
            except Exception:
                pass
        self._sharpen_after = self.after(delay_ms, self._sharpen_basemap)

    def _sharpen_basemap(self):
        """
        Refetch imagery for what is on screen, if that would sharpen it.

        The first fetch covers the whole waterbody, and pick_zoom drops
        the zoom until the tile count is affordable - so on a long
        reservoir the overview is necessarily coarse, and zooming in
        only magnifies those pixels. Once a smaller patch is on screen
        the same tile budget buys a far finer zoom, so it is worth
        asking again.

        Only ever sharpens. Zooming back out keeps the finer tiles
        rather than throwing away imagery already in hand; Reset view
        reloads the overview.
        """
        self._sharpen_after = None
        if self.body is None or self._sharpening or self.candidates:
            return
        west, east = self.ax.get_xlim()
        south, north = self.ax.get_ylim()
        if not (east > west and north > south):
            return
        # A little wider than the screen, so a small pan does not run
        # straight off the edge of what was just fetched.
        padx, pady = (east - west) * 0.15, (north - south) * 0.15
        bounds = (west - padx, south - pady, east + padx, north + pady)
        zoom = basemap.pick_zoom(bounds)
        # Refetch when the view would be sharper, and also when it has
        # simply moved off what was fetched - the sharpened patch covers
        # the screen and a margin, so panning past that runs into blank
        # background at the very moment the map is being read.
        finer = self._basemap_zoom is None or zoom > self._basemap_zoom
        covered = True
        if self.basemap is not None:
            have = self.basemap[1]
            covered = (have[0] <= west and have[1] <= south
                       and have[2] >= east and have[3] >= north)
        if not finer and covered:
            return
        if not finer:
            zoom = self._basemap_zoom     # a pan, not a zoom: same detail
        self._sharpening = True
        self._say(("Fetching sharper imagery" if finer
                   else "Fetching imagery for this view") + ELLIPSIS)

        def work():
            image, extent = basemap.fetch_satellite(bounds, zoom)
            return image, extent, zoom

        self._later(work, self._sharpened)

    def _sharpened(self, got):
        image, extent, zoom = got
        self._sharpening = False
        if image is None:
            self._say("No sharper imagery available here.")
            return
        self.basemap = (image, extent)
        self._basemap_zoom = zoom
        self._say("Imagery sharpened to zoom " + str(zoom) + ".")
        self._draw()


def main():
    SurveyPlannerApp().mainloop()


if __name__ == "__main__":
    main()
