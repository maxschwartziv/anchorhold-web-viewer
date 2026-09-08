"""
Any recording PINGVerter can read, in the shape the Recording Fixer expects.

The Fixer was built on Humminbird because that is the format it can take apart
and put back together. Every other sounder PINGMapper supports can be read but
not rewritten - there is no writer for a Lowrance or a Garmin file, and there
should not be one.

So this takes the other road. PINGVerter converts the recording into a
PINGMapper project, which is a per-ping table with position, depth, speed and
a time in seconds from the start of the recording. That table is all the Fixer
needs to draw the track and judge it. What comes out is not a new recording but
a **time filter**: the stretches worth keeping, in the two columns PINGMapper's
own `time_table` accepts. The recording off the card is never touched, and the
side scan survives because nothing was rewritten to lose it.

Conversion is a full decode, so the project is kept beside the recording and
reused rather than rebuilt on every open.
"""

from __future__ import annotations

import csv
import os

import numpy as np

# extension -> the PINGVerter entry point that reads it. Its
# SUPPORTED_SONAR_EXTENSIONS, with .dat handled here too so the converted road
# can be tested against the format we actually hold recordings in.
CONVERTERS = {
    ".dat": "hum2pingmapper",
    ".sl2": "low2pingmapper",
    ".sl3": "low2pingmapper",
    ".rsd": "gar2pingmapper",
    ".jsf": "jsf2pingmapper",
    ".xtf": "xtf2pingmapper",
    ".svlog": "cerul2pingmapper",
}

# What PINGMapper's _filterTime reads. Each row is a stretch to KEEP; anything
# no row covers is dropped.
TIME_TABLE_COLUMNS = ["start_seconds", "end_seconds"]

M_TO_FT = 3.28084


def supported(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in CONVERTERS


def is_humminbird(path: str) -> bool:
    return os.path.splitext(path)[1].lower() == ".dat"


class MetaPing:
    """
    One ping, named the way the Fixer's Humminbird pings are named.

    Same field names and the same units, so nothing downstream has to ask
    which sounder it came from: depth in decimetres, speed in decimetres per
    second, position error in centimetres.
    """

    __slots__ = ("record", "time_ms", "lat", "lon", "dep_dm", "speed_dm",
                 "heading_dd", "e_err", "n_err")

    def __init__(self, record, time_ms, lat, lon, dep_dm, speed_dm,
                 heading_dd, e_err, n_err):
        self.record = record
        self.time_ms = time_ms
        self.lat = lat
        self.lon = lon
        self.dep_dm = dep_dm
        self.speed_dm = speed_dm
        self.heading_dd = heading_dd
        self.e_err = e_err
        self.n_err = n_err

    @property
    def depth_m(self):
        return self.dep_dm / 10.0


def _number(row, *names, scale=1.0, default=0.0):
    """The first of these columns that holds a number, scaled."""
    for name in names:
        text = row.get(name)
        if text in (None, "", "nan"):
            continue
        try:
            return float(text) * scale
        except ValueError:
            continue
    return default


def _read_meta(path: str) -> list:
    """One beam's pings, from a PINGMapper meta table."""
    pings = []
    with open(path, newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            lat = _number(row, "lat", default=float("nan"))
            lon = _number(row, "lon", default=float("nan"))
            if not (np.isfinite(lat) and np.isfinite(lon)):
                continue
            pings.append(MetaPing(
                record=int(_number(row, "record_num", default=i)),
                time_ms=int(round(_number(row, "time_s") * 1000.0)),
                lat=lat, lon=lon,
                dep_dm=_number(row, "inst_dep_m", "dep_m", scale=10.0),
                speed_dm=_number(row, "speed_ms", scale=10.0),
                heading_dd=_number(row, "instr_heading", scale=10.0),
                e_err=_number(row, "e_err_m", scale=100.0),
                n_err=_number(row, "n_err_m", scale=100.0),
            ))
    return pings


def project_dir(path: str) -> str:
    """Where the converted project for this recording is kept."""
    folder = os.path.dirname(os.path.abspath(path))
    name = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(folder, name + "_pingverter")


def convert(path: str, log=print, rebuild: bool = False) -> str:
    """
    Decode a recording into a PINGMapper project, once.

    This is a full read of the file - the same work PINGMapper does at the
    start of a build - so the result is kept beside the recording and reused.
    Delete the folder, or pass rebuild, to do it again.
    """
    extension = os.path.splitext(path)[1].lower()
    if extension not in CONVERTERS:
        raise SystemExit(
            f"{extension or 'that file'} is not one PINGVerter reads. "
            f"It handles: {', '.join(sorted(CONVERTERS))}.")

    out_dir = project_dir(path)
    meta_dir = os.path.join(out_dir, "meta")
    if os.path.isdir(meta_dir) and not rebuild:
        if any(n.endswith("_meta.csv") for n in os.listdir(meta_dir)):
            log(f"  using the project already beside it: "
                f"{os.path.basename(out_dir)}")
            return out_dir

    try:
        import pingverter
    except ImportError as exc:
        raise SystemExit(
            "PINGVerter is needed to read anything but a Humminbird "
            "recording, and it lives in the conda environment PINGMapper is "
            f"installed in. Run this from that environment. ({exc})")

    converter = getattr(pingverter, CONVERTERS[extension], None)
    if converter is None:
        raise SystemExit(
            f"This PINGVerter has no {CONVERTERS[extension]}; it may be older "
            "than the format needs.")

    log(f"  decoding {os.path.basename(path)} with PINGVerter - "
        "this is a full read of the file")
    os.makedirs(out_dir, exist_ok=True)
    converter(path, out_dir)
    if not os.path.isdir(meta_dir):
        raise SystemExit(f"PINGVerter wrote no meta tables into {out_dir}.")
    return out_dir


class ConvertedSurvey:
    """
    A converted recording, wearing the same surface as a Humminbird one.

    The Fixer asks a survey for its beams and their pings and nothing else, so
    this answers those and adds what a converted recording knows that a parsed
    one does not: where the original file is, and that it must not be rewritten.
    """

    humminbird = False

    def __init__(self, source: str, out_dir: str):
        self.source_path = os.path.abspath(source)
        self.folder = out_dir
        self.name = os.path.splitext(os.path.basename(source))[0]
        self.dat_path = self.source_path

        meta_dir = os.path.join(out_dir, "meta")
        self.pings = {}
        for entry in sorted(os.listdir(meta_dir)):
            if not entry.endswith("_meta.csv") or entry.startswith("DAT"):
                continue
            beam = entry.split("_", 1)[0]
            pings = _read_meta(os.path.join(meta_dir, entry))
            if pings:
                self.pings[beam] = pings
        if not self.pings:
            raise SystemExit(
                f"{os.path.basename(source)} converted, but its tables hold no "
                "positioned pings.")
        self.beams = sorted(self.pings)

    @property
    def recordlens_ms(self):
        return max(p.time_ms for b in self.beams for p in self.pings[b])

    def coords(self, pings) -> np.ndarray:
        """Latitude and longitude straight off the table, no projection."""
        return np.array([[p.lat, p.lon] for p in pings], dtype=float)

    def healthy(self) -> bool:
        return True


def open_any(path: str, log=print) -> ConvertedSurvey:
    """Convert if it has not been converted, then read it."""
    return ConvertedSurvey(path, convert(path, log=log))


def write_time_filter(path: str, windows, log=print) -> str:
    """
    The stretches to keep, as PINGMapper's time_table.

    Two columns, `start_seconds` and `end_seconds`, each row a stretch to keep,
    measured from the start of the recording. PINGMapper drops everything no
    row covers, so this and the untouched recording together are the repair -
    with the side scan intact, because nothing was rewritten.
    """
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(TIME_TABLE_COLUMNS)
        for start_ms, end_ms in windows:
            writer.writerow([f"{start_ms / 1000.0:.3f}", f"{end_ms / 1000.0:.3f}"])
    log(f"  {os.path.basename(path)}: {len(windows)} stretch"
        f"{'' if len(windows) == 1 else 'es'} to keep, for PINGMapper's "
        "time_table")
    return path
