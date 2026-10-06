#!/usr/bin/env python3
"""
places_to_objects.py

Turn a list of places into the objects layer AnchorHold Web Viewer imports.

The viewer's "bottom objects" layer was built for GhostVision - crab pots it
found in a survey - but what it actually takes is a GeoJSON FeatureCollection
of points, which is as good a way to carry dive sites, hazards, moorings or
anything else worth a mark on the chart.

What survives the import is narrow, and knowing that up front is the whole
point of this script. web_charts.write_detections keeps three properties and
throws the rest away:

    tracker_id    40 characters
    class_name    60 characters   <- the only text the chart ever shows
    confidence    12 characters

Tapping a mark shows class_name with underscores turned into spaces, then the
depth read off that chart's own depth grid, then the position. Confidence is
only shown when it parses as a number, so this writes it empty: a dive site is
not 80% sure of anything, and a stray number would put "80% sure" on the chart.

A longer description cannot be imported. It has nowhere to go. So the note
column here stays in the CSV, which is worth keeping beside the chart as the
thing people actually read.

    python pipeline/places_to_objects.py places.csv
    python pipeline/places_to_objects.py places.csv -o bull_shoals.geojson

The CSV wants a header row with at least name, lat and lon:

    name,lat,lon,depth_ft,note
    Iowa Farm,36.xxxxx,-92.xxxxx,35,Sunken tractor and combine

Rows with no position are reported and skipped rather than guessed at.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

# The server's own limits, repeated here so a file is rejected on this side
# with something readable rather than over HTTP with something terse.
ID_CHARS = 40
LABEL_CHARS = 60


def slug(text: str) -> str:
    """A short, stable id from a name."""
    out = re.sub(r'[^a-z0-9]+', '-', str(text).lower()).strip('-')
    return out[:ID_CHARS] or 'place'


def label(name: str, depth: str) -> str:
    """
    The one line the chart shows, with the depth in it when there is one.

    Depth is in the label rather than left to the chart because the chart can
    only read a depth it surveyed. Somewhere with no AnchorHold survey behind
    it has no depth grid, and the mark would say nothing about how deep it is.
    """
    name = ' '.join(str(name or '').split())
    depth = str(depth or '').strip()
    text = f"{name}, {depth} ft" if depth else name
    if len(text) > LABEL_CHARS:
        # Keep the name whole and drop the depth before truncating a word.
        text = name[:LABEL_CHARS].rstrip(' ,-')
    return text


def read_places(path: str):
    """Every row of the CSV, as (row number, dict)."""
    with open(path, newline='', encoding='utf-8-sig') as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SystemExit(f"{path} has no header row.")
        lower = {name.strip().lower() for name in reader.fieldnames}
        for needed in ('name', 'lat', 'lon'):
            if needed not in lower:
                raise SystemExit(
                    f"{path} needs a '{needed}' column; it has: "
                    + ', '.join(reader.fieldnames))
        for i, row in enumerate(reader, start=2):
            yield i, {(k or '').strip().lower(): (v or '').strip()
                      for k, v in row.items()}


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Turn a CSV of places into the objects layer AnchorHold "
                    "Web Viewer imports.")
    p.add_argument("csv_file", help="name, lat, lon, and optionally depth_ft and note")
    p.add_argument("-o", "--out", default=None,
                   help="output path (default: the CSV's name with .geojson)")
    args = p.parse_args(argv)

    if not os.path.isfile(args.csv_file):
        raise SystemExit(f"Not found: {args.csv_file}")
    out = args.out or os.path.splitext(args.csv_file)[0] + '.geojson'

    features, missing, bad = [], [], []
    for line, row in read_places(args.csv_file):
        name = row.get('name', '')
        if not name:
            continue
        if not row.get('lat') or not row.get('lon'):
            missing.append((line, name))
            continue
        try:
            lat, lon = float(row['lat']), float(row['lon'])
        except ValueError:
            bad.append((line, name, "not a number"))
            continue
        # The same check the server makes. A position outside the globe is a
        # projection mistake, and it is better found here.
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            bad.append((line, name, f"{lat}, {lon} is off the globe"))
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "tracker_id": slug(name),
                "class_name": label(name, row.get('depth_ft', '')),
                # Deliberately empty: anything numeric here shows as "% sure".
                "confidence": "",
            },
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
        })

    for line, name, why in bad:
        print(f"  line {line}: {name} - {why}", file=sys.stderr)
    for line, name in missing:
        print(f"  line {line}: {name} - no position, not written")
    if not features:
        raise SystemExit(
            "\nNothing to write: no row had a position in it. Fill in lat and "
            "lon\nand run this again - the file is no use to a diver without "
            "them.")

    with open(out, 'w', encoding='utf-8') as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh, indent=1)
        fh.write("\n")

    print(f"\n{out}")
    print(f"  {len(features)} place(s) written"
          + (f", {len(missing)} still without a position" if missing else ""))
    print("\nImport it from the machine running web_server.py: Charts on this "
          "computer,\npick the chart, then Add objects. The viewer refuses the "
          "upload from anywhere else.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
