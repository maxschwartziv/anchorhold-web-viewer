#!/usr/bin/env python3
"""
Where a survey came from: the citation and the facts behind a chart.

A depth chart is worth exactly as much as its provenance. Was it run last
season or five years ago? Single-beam or side-scan? Reduced to LAT, or to
whatever the water was doing that afternoon? Whose work is it, and may it be
passed on? None of that is visible in a coloured raster, so it is recorded
alongside one, and both apps show it on the survey's pin.

The record travels with the chart:

    web_charts/<survey>/chart.json               -> "source": { ... }

    python pipeline/survey_source.py show noatak
    python pipeline/survey_source.py derive noatak            # from the build
    python pipeline/survey_source.py set noatak --surveyed-by "Max Schwartz" \\
        --equipment "Humminbird Helix 7 CHIRP MEGA SI" --licence "CC BY 4.0"
    python pipeline/survey_source.py set noatak --notes "..."

Nothing here is compulsory. A survey with no record simply says so in the app,
which is more honest than an empty field that looks filled in.
"""

from __future__ import annotations

import argparse
import glob
import json
import os

import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import workspace                   # neutral home for builds
BUILD_DIR = workspace.output_dir()

# The record, in the order it reads well. Each entry is (key, label, help).
FIELDS = [
    ("surveyedBy", "Surveyed by", "person or organisation that ran the survey"),
    ("surveyedOn", "Surveyed", "date or range, e.g. 2026-07-14 or 2026-07-14..16"),
    ("vessel", "Vessel", "what carried the sounder"),
    ("equipment", "Equipment", "sounder and transducer"),
    ("method", "Method", "e.g. single-beam + side-scan, lawnmower pattern"),
    ("processing", "Processing", "software and versions the charts were made with"),
    ("verticalDatum", "Depths reduced to", "e.g. LAT via harmonic model, or none"),
    ("horizontalDatum", "Positions", "e.g. WGS84 / GNSS, uncorrected"),
    ("soundings", "Soundings", "how many depth points the grid was built from"),
    ("accuracy", "Accuracy", "honest error estimate, vertical and horizontal"),
    ("licence", "Licence", "how others may use it, e.g. CC BY 4.0"),
    ("attribution", "Attribution", "the line someone re-using it should print"),
    ("url", "More", "a link to the survey, the data, or the person"),
    ("notes", "Notes", "anything a skipper should know before trusting it"),
]
KEYS = [key for key, _label, _help in FIELDS]
LABELS = {key: label for key, label, _help in FIELDS}


def blank() -> dict:
    """An empty record, for showing someone what can be filled in."""
    return {key: "" for key in KEYS}


def clean(source: dict) -> dict:
    """Drop empty fields: a record says only what is actually known."""
    return {k: v for k, v in source.items()
            if k in KEYS and str(v).strip() not in ("", "None")}


def citation(source: dict, name: str) -> str:
    """
    One line to paste into a chart note, a README or a paper.

    Falls back through what is present rather than printing empty brackets -
    a half-known survey still deserves a usable credit.
    """
    who = source.get("surveyedBy") or "Unknown surveyor"
    when = source.get("surveyedOn")
    bits = [f"{who}"]
    if when:
        bits.append(f"({when})")
    bits.append(f"*{name}* bathymetric survey.")
    if source.get("processing"):
        bits.append(f"Processed with {source['processing']}.")
    if source.get("licence"):
        bits.append(f"Licence: {source['licence']}.")
    if source.get("url"):
        bits.append(source["url"])
    return " ".join(bits)


# -- Deriving what the build already knows ----------------------------------

def derive(loc_id: str, log=print) -> dict:
    """
    Fill in what can be read off the survey itself.

    The soundings and the dates are facts about the recording, not opinions, so
    typing them by hand only invites a typo. Everything else is a judgement and
    stays the surveyor's to state.
    """
    found = {}
    folder = os.path.join(BUILD_DIR, loc_id)
    csvs = sorted(glob.glob(os.path.join(folder, "pingmapper", "*_depth.csv")))
    if csvs:
        rows, first, last = 0, None, None
        for path in csvs:
            with open(path, encoding="utf-8", errors="replace") as fh:
                header = fh.readline().strip().split(",")
                try:
                    date_at = header.index("date")
                except ValueError:
                    date_at = None
                for line in fh:
                    rows += 1
                    if date_at is None:
                        continue
                    parts = line.rstrip("\n").split(",")
                    if len(parts) > date_at:
                        day = parts[date_at].strip()
                        if day:
                            first = day if first is None or day < first else first
                            last = day if last is None or day > last else last
        if rows:
            found["soundings"] = str(rows)
            log(f"  soundings: {rows} from {len(csvs)} recording(s)")
        if first:
            found["surveyedOn"] = first if first == last else f"{first}..{last}"
            log(f"  surveyed:  {found['surveyedOn']}")
    else:
        log("  (no decoded recording in output/ - nothing to read dates from)")

    versions = _tool_versions()
    if versions:
        found["processing"] = versions
        log(f"  processing: {versions}")

    # The datums are properties of the pipeline and the survey's tide setting,
    # not opinions, so they are stated rather than asked for.
    found["horizontalDatum"] = "WGS84, from the sounder's GNSS (uncorrected)"
    tide = _tide_mode(loc_id)
    found["verticalDatum"] = (
        "not reduced - depths as recorded on the day" if tide in ("", "none")
        else f"LAT, via the harmonic model ({tide})")
    log(f"  datums:    {found['verticalDatum']}")
    return found


def _tide_mode(loc_id: str) -> str:
    """How this survey's depths were reduced, according to its chart."""
    try:
        return (_web().chart_meta(loc_id).get("tide") or {}).get("mode", "none")
    except Exception:
        return "none"


def _tool_versions() -> str:
    """The pipeline's own version string, best effort."""
    names = []
    for module, label in (("pingmapper", "PINGMapper"), ("pingverter", "PINGVerter")):
        try:
            mod = __import__(module)
            version = getattr(mod, "__version__", "")
            names.append(f"{label} {version}".strip())
        except Exception:
            pass
    names.append("Anchoring App pipeline")
    return ", ".join(names)


# -- Reading and writing, per app -------------------------------------------

def _web():
    import sys
    sys.path.insert(0, HERE)
    import web_charts
    return web_charts


def read_web(loc_id: str) -> dict:
    wc = _web()
    if not os.path.isdir(wc.chart_dir(loc_id)):
        raise SystemExit(f"{loc_id} is not in the browser app's chart library.")
    return clean(wc.chart_meta(loc_id).get("source") or {})


def write_web(loc_id: str, source: dict) -> None:
    wc = _web()
    if not os.path.isdir(wc.chart_dir(loc_id)):
        raise SystemExit(f"{loc_id} is not in the browser app's chart library.")
    meta = wc.chart_meta(loc_id)
    meta["source"] = clean(source)
    wc.write_chart_meta(loc_id, meta)
    # The catalog carries the name and centre; the record travels in chart.json.
    catalog = wc.load()
    for entry in catalog["locations"]:
        if entry["id"] == loc_id:
            entry["source"] = clean(source)
    wc.save(catalog)


# -- Console ----------------------------------------------------------------

def show(loc_id: str) -> None:
    try:
        source = read_web(loc_id)
    except SystemExit as exc:
        print(f"\n{exc}")
        return
    print(f"\n{loc_id}")
    if not source:
        print("  (no survey record; the app will say the source is not recorded)")
        return
    width = max(len(LABELS[k]) for k in source)
    for key in KEYS:
        if key in source:
            print(f"  {LABELS[key]:<{width}} : {source[key]}")
    print(f"\n  Citation: {citation(source, loc_id)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["show", "set", "derive", "fields"])
    ap.add_argument("id", nargs="?", default="", help="survey id")
    for key, label, helptext in FIELDS:
        flag = "--" + "".join("-" + c.lower() if c.isupper() else c for c in key)
        ap.add_argument(flag, dest=key, default=None, help=f"{label}: {helptext}")
    args = ap.parse_args(argv)

    if args.command == "fields":
        width = max(len(label) for _k, label, _h in FIELDS)
        for _key, label, helptext in FIELDS:
            print(f"  {label:<{width}} : {helptext}")
        return
    if not args.id:
        raise SystemExit("Which survey? e.g. survey_source.py show noatak")

    if args.command == "show":
        show(args.id)
        return

    if args.command == "derive":
        print(f"Reading {args.id} from its build ...")
        found = derive(args.id)
        if not found:
            raise SystemExit("Nothing could be derived - is the survey built?")
        merged = dict(read_web(args.id))
        merged.update(found)              # derived facts win over stale ones
        write_web(args.id, merged)
        print("  written to the chart")
        show(args.id)
        return

    given = {key: getattr(args, key) for key in KEYS if getattr(args, key) is not None}
    if not given:
        raise SystemExit("Nothing to set. Run 'fields' to see what can be recorded.")
    merged = dict(read_web(args.id))
    merged.update(given)
    write_web(args.id, merged)
    print("  written to the chart")
    show(args.id)


if __name__ == "__main__":
    main()
