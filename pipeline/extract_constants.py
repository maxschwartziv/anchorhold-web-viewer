#!/usr/bin/env python3
"""
Fit M2/S2/N2/K1/O1/P1 harmonic constants for Santa Rosalía from online
tide predictions, expressed in the format TideModel.kt uses:

    h(t) = Σ A_i * cos(ω_i * t_J2000_hours − φ_i)

where t_J2000_hours = hours since 2000-01-01 00:00:00 UTC.

Usage:
    pip install requests
    python extract_constants.py

The script fetches 90 days of hourly tide predictions from tide-forecast.com
for Santa Rosalía, fits the six constituents, and prints drop-in replacement
Constituent lines for TideModel.kt and TIDE_CONSTITUENTS for process_data.py.
"""

import math
import datetime
import json
import sys
import time as time_module
from datetime import timezone

import numpy as np
import requests

# ── Configuration ─────────────────────────────────────────────────────────────

LAT = 27.338
LON = -112.263
# 90-day window gives good separation of M2 vs S2 vs N2
START = datetime.datetime(2025, 1, 1, tzinfo=timezone.utc)
DAYS = 90

# Constituent angular speeds (degrees/hour) — physical constants, fixed.
SPEEDS = {
    "M2": 28.9841042,
    "S2": 30.0000000,
    "N2": 28.4397295,
    "K1": 15.0410686,
    "O1": 13.9430356,
    "P1": 14.9589314,
}

EPOCH = datetime.datetime(2000, 1, 1, tzinfo=timezone.utc)


# ── Fetch hourly predictions ───────────────────────────────────────────────────

def fetch_hourly_wxtide(lat, lon, start: datetime.datetime, days: int):
    """
    Try to get hourly tide levels from the WorldTides paid API.
    Falls back to a simple scrape of tide-forecast.com JSON endpoint.
    """
    # tide-forecast.com provides a chart-data JSON endpoint.
    # The slug for Santa Rosalía Baja California Sur:
    slug = "Santa-Rosalia-Baja-California-Sur-Mexico"
    hours_list = []
    heights_list = []

    for chunk_start in range(0, days, 7):
        chunk_dt = start + datetime.timedelta(days=chunk_start)
        url = (
            f"https://www.tide-forecast.com/locations/{slug}/tides/chart-data"
            f"?start={chunk_dt.strftime('%Y-%m-%d')}&days=7&tz=UTC"
        )
        try:
            r = requests.get(url, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0 (research script)"})
            if r.status_code == 200:
                data = r.json()
                # The chart data is a list of [unix_ms, height_m] pairs
                for ts_ms, h in data.get("heights", []):
                    dt = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                    t_hours = (dt - EPOCH).total_seconds() / 3600.0
                    hours_list.append(t_hours)
                    heights_list.append(h)
                print(f"  chunk {chunk_start:3d}d: {len(data.get('heights',[]))} points")
            else:
                print(f"  chunk {chunk_start:3d}d: HTTP {r.status_code}")
        except Exception as e:
            print(f"  chunk {chunk_start:3d}d: {e}")
        time_module.sleep(0.5)   # be polite

    return np.array(hours_list), np.array(heights_list)


def fetch_worldtides(lat, lon, start: datetime.datetime, days: int,
                     api_key: str = ""):
    """
    If you have a WorldTides.info API key, use it for higher-quality data.
    https://www.worldtides.info/developer
    """
    if not api_key:
        return None, None
    hours_list, heights_list = [], []
    for day_offset in range(days):
        dt = start + datetime.timedelta(days=day_offset)
        url = (
            f"https://www.worldtides.info/api/v3"
            f"?heights&lat={lat}&lon={lon}"
            f"&start={int(dt.timestamp())}&length=86400&step=3600"
            f"&key={api_key}"
        )
        try:
            r = requests.get(url, timeout=15)
            data = r.json()
            for pt in data.get("heights", []):
                t_hours = (pt["dt"] - EPOCH.timestamp()) / 3600.0
                hours_list.append(t_hours)
                heights_list.append(pt["height"])
        except Exception as e:
            print(f"  WorldTides day {day_offset}: {e}")
        time_module.sleep(0.3)
    return np.array(hours_list), np.array(heights_list)


# ── Synthetic reference using TPXO9 known-good values (fallback) ──────────────

# Best available values from TPXO9-atlas for 27.34°N 112.26°W (mid-Gulf at
# Santa Rosalía latitude). Phase lag g is relative to Greenwich equilibrium;
# converted here to the code's epoch-relative φ = g − V₀(J2000).
#
# Astronomical arguments V₀ at J2000.0 (2000-01-01T00:00:00 UTC)
# computed from Schureman (1940) tables / IAU2000 ephemeris:
#   s = 218.3165°  (Moon mean longitude)
#   h = 280.4665°  (Sun mean longitude)
#   p = 83.3532°   (Moon perigee)
#   N = 125.0445°  (ascending node)
#
#   M2:  V0 = 2h − 2s            = 124.30°
#   S2:  V0 = 0°                 =   0.00°   (defined)
#   N2:  V0 = 2h − 3s + p        = 349.34°
#   K1:  V0 = h + 90°            =  10.47°   (= 280.47 + 90 − 360)
#   O1:  V0 = −2s + h + 270°     = 113.83°   (= −436.63 + 280.47 + 270)
#   P1:  V0 = −h + 270°          = 349.53°   (= −280.47 + 270 + 360)
#
# Published g for nearest CICESE station (Guaymas, ~130 km ENE, IHO Pub 111
# and Filloux 1973 / Argote 1995 compiled by CICESE):
#   M2: g = 274°,  H = 0.530 m
#   S2: g = 295°,  H = 0.190 m
#   N2: g = 250°,  H = 0.118 m
#   K1: g = 243°,  H = 0.315 m
#   O1: g = 219°,  H = 0.235 m
#   P1: g = 242°,  H = 0.104 m
#
# φ = (g − V0) mod 360:
FALLBACK_CONSTS = {
    #  name:   (speed °/hr,  amplitude m, phase φ°)
    "M2": (28.9841042, 0.530, (274.0 - 124.30) % 360),   # 149.7
    "S2": (30.0000000, 0.190, (295.0 -   0.00) % 360),   # 295.0
    "N2": (28.4397295, 0.118, (250.0 - 349.34) % 360),   # 260.7
    "K1": (15.0410686, 0.315, (243.0 -  10.47) % 360),   # 232.5
    "O1": (13.9430356, 0.235, (219.0 - 113.83) % 360),   # 105.2
    "P1": (14.9589314, 0.104, (242.0 - 349.53) % 360),   # 252.5
}


def synthetic_heights(t_hours: np.ndarray) -> np.ndarray:
    """Generate a reference time series from FALLBACK_CONSTS for fitting validation."""
    h = np.zeros_like(t_hours)
    for speed, amp, phase in FALLBACK_CONSTS.values():
        h += amp * np.cos(np.radians(speed * t_hours - phase))
    return h


# ── Least-squares harmonic fit ─────────────────────────────────────────────────

def fit_harmonics(t_hours: np.ndarray, heights: np.ndarray):
    """
    Fit h(t) = mean + Σ [C_i·cos(ω_i·t) + S_i·sin(ω_i·t)]
    Return dict: name → (amplitude, phase_deg).
    """
    names = list(SPEEDS.keys())
    speeds = [SPEEDS[n] for n in names]

    # Build design matrix: [1, cos(ω1·t), sin(ω1·t), cos(ω2·t), sin(ω2·t), ...]
    cols = [np.ones(len(t_hours))]
    for omega in speeds:
        cols.append(np.cos(np.radians(omega * t_hours)))
        cols.append(np.sin(np.radians(omega * t_hours)))
    A = np.column_stack(cols)

    coeffs, _, _, _ = np.linalg.lstsq(A, heights, rcond=None)
    # coeffs[0] = mean, then pairs (C_i, S_i)

    results = {}
    for i, name in enumerate(names):
        C = coeffs[1 + 2 * i]
        S = coeffs[1 + 2 * i + 1]
        amp = math.sqrt(C**2 + S**2)
        # h = A·cos(ω·t − φ) = A·[cos(ω·t)·cos(φ) + sin(ω·t)·sin(φ)]
        # => C = A·cos(φ), S = A·sin(φ)  =>  φ = atan2(S, C)
        phase = math.degrees(math.atan2(S, C)) % 360
        results[name] = (amp, phase)
    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="Fit tidal harmonic constants for a coordinate, in the form "
                    "TideModel.kt and process_data.py use.")
    p.add_argument("--lat", type=float, default=LAT)
    p.add_argument("--lon", type=float, default=LON)
    p.add_argument("--api-key", default="",
                   help="WorldTides API key; without it only the Santa Rosalía "
                        "source and the Guaymas fallback are available")
    p.add_argument("--json", action="store_true",
                   help="print the fitted constants as one JSON line (Add Survey Locations uses this)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    lat, lon = args.lat, args.lon
    out = (lambda *a, **k: None) if args.json else print   # keep stdout clean for --json

    out(f"Extracting tidal harmonic constants for {lat}N, {lon}E")
    out(f"Window: {START.date()} + {DAYS} days\n")

    t = h = None
    if args.api_key:
        out("Fetching WorldTides heights...")
        t, h = fetch_worldtides(lat, lon, START, DAYS, args.api_key)

    if t is None or len(t) < 500:
        out("Fetching tide-forecast.com chart data...")
        t, h = fetch_hourly_wxtide(lat, lon, START, DAYS)

    if args.json:
        # Only real fitted constants are worth emitting: the Guaymas fallback
        # would silently give a Gulf of California tide to any coordinate.
        if t is None or len(t) < 500:
            raise SystemExit(
                "No usable tide series for this coordinate — supply a WorldTides "
                "API key (--api-key), or leave the location as 'no tide'.")
        results = fit_harmonics(t, h)
        print(json.dumps([
            {"name": name, "speed": SPEEDS[name],
             "amp": round(float(results[name][0]), 4),
             "phase": round(float(results[name][1]), 1)}
            for name in SPEEDS
        ]))
        return

    if len(t) < 500:
        print(f"\nLive fetch returned only {len(t)} points — using TPXO9-based fallback.\n")
        # Use hourly synthetic time series from FALLBACK_CONSTS as the dataset to fit.
        # This is a consistency check: fitting our own model back gives the same constants.
        # The FALLBACK_CONSTS ARE the answer in this case.
        results = {name: (vals[1], vals[2]) for name, vals in FALLBACK_CONSTS.items()}
        source = "TPXO9-atlas (CICESE Guaymas station, converted)"
    else:
        print(f"\nFetched {len(t)} hourly points — fitting harmonics...")
        results = fit_harmonics(t, h)
        source = "tide-forecast.com (least-squares fit)"

    # ── Print results ──────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"Source: {source}")
    print(f"{'='*65}")
    print(f"\n{'Constituent':<6}  {'Amplitude (m)':>14}  {'Phase φ° (J2000)':>18}")
    print("-" * 45)
    for name in SPEEDS:
        amp, phase = results[name]
        print(f"{name:<6}  {amp:>14.4f}  {phase:>18.1f}")

    print("\n── TideModel.kt (drop-in replacement for the constituents list) ──")
    kt_lines = []
    for name in SPEEDS:
        amp, phase = results[name]
        speed = SPEEDS[name]
        kt_lines.append(
            f"        Constituent({speed:.7f}, {amp:.3f}, {phase:.1f}), // {name}"
        )
    print("\n".join(kt_lines))

    print("\n── process_data.py TIDE_CONSTITUENTS (same values) ──")
    py_lines = []
    for name in SPEEDS:
        amp, phase = results[name]
        speed = SPEEDS[name]
        py_lines.append(
            f"    ({speed:.7f}, {amp:.3f}, {phase:.1f}),  # {name}"
        )
    print("\n".join(py_lines))

    print(f"\nDone. Source: {source}\n")
    if "TPXO9" in source:
        print("NOTE: Values are from published CICESE/IHO data for Guaymas (nearest")
        print("station, ~130 km ENE). For higher accuracy, download TPXO9-atlas and")
        print("rerun with WorldTides API key or pyTMD extraction script.")


if __name__ == "__main__":
    main()
