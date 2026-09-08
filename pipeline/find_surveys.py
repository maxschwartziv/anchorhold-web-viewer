#!/usr/bin/env python3
"""
Find surveys in a public archive that this pipeline could actually use.

Archives hold a great deal of side-scan data and almost none of it in the
formats PINGMapper reads. Searching by hand, one dataset page at a time, hides
that; asking the catalog directly makes it obvious in a few seconds.

Currently searches the Marine Geoscience Data System (MGDS), whose whole
catalog is one REST call. The catalog is cached locally, so only the first
search waits for the download.

    python pipeline/find_surveys.py                  # what the pipeline can read
    python pipeline/find_surveys.py --formats        # what the archive holds
    python pipeline/find_surveys.py --format MBSystem --limit 20
    python pipeline/find_surveys.py --data-type Bathymetry --any-format

Licence, before you use anything you find: MGDS data is CC BY-NC-SA 3.0 US -
attribution to both the original scientists and MGDS, non-commercial, and
share-alike on anything derived. Their terms also say the data is not to be
used for navigation. NOAA/NCEI holdings are US public domain and a better
source for anything you intend to ship.
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import workspace                   # neutral home for builds
CACHE = os.path.join(workspace.output_dir(), "mgds_catalog.xml")
CATALOG_URL = "https://www.marine-geo.org/services/search/datasets"
CACHE_MAX_AGE_DAYS = 30

# What PINGVerter can be pointed at. Kept here rather than imported so this
# runs without the pipeline's conda environment.
PIPELINE_FORMATS = ("XTF", "JSF")


def catalog(refresh: bool = False, log=print) -> str:
    """The archive's dataset catalog, downloaded once and cached."""
    fresh = (os.path.isfile(CACHE)
             and (time.time() - os.path.getmtime(CACHE)) < CACHE_MAX_AGE_DAYS * 86400)
    if fresh and not refresh:
        return open(CACHE, encoding="utf-8", errors="replace").read()

    log(f"Fetching the MGDS catalog ({CATALOG_URL}) ...")
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with urllib.request.urlopen(CATALOG_URL, timeout=300) as response:
        text = response.read().decode("utf-8", "replace")
    with open(CACHE, "w", encoding="utf-8") as fh:
        fh.write(text)
    log(f"  {len(text)/1e6:.1f} MB cached at {os.path.relpath(CACHE, ROOT)}")
    return text


def datasets(text: str):
    """Each dataset as (attributes, xml block)."""
    for block in re.split(r"(?=<data_set )", text):
        match = re.match(r"<data_set ([^>]*)>", block)
        if match:
            yield dict(re.findall(r'(\w+)="([^"]*)"', match.group(1))), block


def detail(block: str) -> dict:
    """The bits worth showing that live inside the dataset, not on it."""
    device = re.search(r'<device[^>]*make="([^"]*)"[^>]*model="([^"]*)"', block)
    entry = re.search(r'<ds_entry[^>]*start_date="([^"]*)"', block)
    platform = re.search(r'platform="([^"]*)"', block)
    return {
        "device": f"{device.group(1)} {device.group(2)}" if device else "",
        "start": entry.group(1) if entry else "",
        "platform": platform.group(1) if platform else "",
    }


def formats_report(text: str, data_type: str) -> None:
    """
    What the archive actually holds for this data type.

    Worth looking at before hunting for a specific format: an archive's
    dominant format tells you which tool is the way in.
    """
    counts = collections.Counter()
    total = 0
    for attrs, _block in datasets(text):
        if data_type and data_type.lower() not in attrs.get("data_type", "").lower():
            continue
        total += 1
        for name in attrs.get("file_formats", "").split(","):
            if name.strip():
                counts[name.strip()] += 1

    print(f"\n{total} '{data_type}' datasets, by format:\n")
    for name, count in counts.most_common(15):
        readable = " <- this pipeline reads it" if name.upper() in PIPELINE_FORMATS else ""
        print(f"   {count:>6}  {name}{readable}")


def search(text: str, data_type: str, wanted, raw_only: bool, limit: int) -> int:
    rows = []
    for attrs, block in datasets(text):
        if data_type and data_type.lower() not in attrs.get("data_type", "").lower():
            continue
        if raw_only and attrs.get("has_raw") != "true":
            continue
        formats = [f.strip() for f in attrs.get("file_formats", "").split(",") if f.strip()]
        if wanted and not any(f.upper() in wanted for f in formats):
            continue
        rows.append((attrs, detail(block), formats))

    if not rows:
        print(f"\nNothing in this archive matches: {data_type or 'any type'}, "
              f"formats {', '.join(sorted(wanted)) if wanted else 'any'}"
              f"{', raw only' if raw_only else ''}.")
        return 0

    print(f"\n{len(rows)} matching dataset(s):\n")
    print(f"   {'uid':<8} {'start':<11} {'platform':<20} {'formats':<14} device")
    for attrs, info, formats in rows[:limit]:
        print(f"   {attrs.get('uids',''):<8} {info['start']:<11} "
              f"{info['platform'][:18]:<20} {','.join(formats)[:12]:<14} {info['device'][:34]}")
    if len(rows) > limit:
        print(f"   ... and {len(rows)-limit} more (--limit to see them)")

    print("\nDataset pages:")
    for attrs, _info, _f in rows[:limit]:
        print(f"   {attrs.get('url','')}")
    return len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-type", default="Sidescan",
                    help="archive data type to match, e.g. Sidescan, Bathymetry")
    ap.add_argument("--format", default="",
                    help="format to require (default: the ones this pipeline reads)")
    ap.add_argument("--any-format", action="store_true", help="do not filter on format")
    ap.add_argument("--include-processed", action="store_true",
                    help="also list datasets with no raw data")
    ap.add_argument("--formats", action="store_true",
                    help="report which formats this archive holds, and stop")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--refresh", action="store_true", help="re-download the catalog")
    args = ap.parse_args(argv)

    text = catalog(refresh=args.refresh)
    count = re.search(r"<count>(\d+)</count>", text)
    print(f"MGDS catalog: {count.group(1) if count else '?'} datasets")

    if args.formats:
        formats_report(text, args.data_type)
        return

    if args.any_format:
        wanted = set()
    elif args.format:
        wanted = {f.strip().upper() for f in args.format.split(",") if f.strip()}
    else:
        wanted = set(PIPELINE_FORMATS)

    found = search(text, args.data_type, wanted, not args.include_processed, args.limit)
    if not found and wanted:
        print("\nTry --formats to see what this archive does hold for that data type.")
    print("\nMGDS data is CC BY-NC-SA 3.0 US: attribute the original scientists and\n"
          "MGDS, non-commercial, share-alike. Their terms also say it is not for\n"
          "navigation. Record what you use with pipeline/survey_source.py.")


if __name__ == "__main__":
    main()
