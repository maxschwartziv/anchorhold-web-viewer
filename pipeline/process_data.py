#!/usr/bin/env python3
"""
Phase 1 pipeline: CSV + GeoTIFF sonar tiles → bathymetry.mbtiles + sonar.mbtiles
Pure Python — no external GDAL CLI tools required.

Dependencies: numpy, pandas, scipy, rasterio, matplotlib, mercantile, Pillow
"""

import argparse
import io
import math
import os
import sqlite3
import sys
import time

# build_preview lives beside this file and is imported, not shelled out to.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime, timezone

import mercantile
import numpy as np
import pandas as pd
from PIL import Image, ImageFilter
from scipy.interpolate import griddata
import matplotlib.cm as cm
import matplotlib.colors as mcolors
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling

# ── Config ────────────────────────────────────────────────────────────────────

CSV_PATH = "resources/depth/B001_ds_highfreq_meta.csv"

SONAR_TILES = [
    "resources/sonar mosaic/santaRosalia_rect_wcr_mosaic_0.tif",
    "resources/sonar mosaic/santaRosalia_rect_wcr_mosaic_1.tif",
    "resources/sonar mosaic/santaRosalia_rect_wcr_mosaic_2.tif",
]

SUBSTRATE_TIF = "resources/substrate/santarosaliaa2_map_substrate_raster_mosaic_0.tif"

OUT_DIR   = "output"
ZOOM_MIN  = 12
ZOOM_MAX  = 17
TILE_SIZE = 256

# --zoom-min lowest: tile out to the zoom where the survey is still this many
# pixels across. Further out it is a speck, and a speck tile is wasted work.
LOWEST_ZOOM_MIN_PX = 8

# --zoom-max highest: tile each layer until a tile pixel is about as fine as
# its own source pixel. Stopping when a tile pixel is within this factor of it
# saves a whole zoom level (4x the tiles) for detail that is not there - a
# 0.44 m depth grid is z18.1, and z19 would draw each cell as 1.5 tile pixels.
HIGHEST_ZOOM_SLACK = 0.3            # in zoom levels: 2**0.3 = 1.23x
HIGHEST_ZOOM_CAP = 22

# Timezone the sonar logs its timestamps in (used to reduce soundings to LAT).
SURVEY_TIMEZONE = "America/Hermosillo"

FT_PER_M = 0.3048

# Spacing (whole feet, counted from 0 ft) of the baked contour lines; the app
# thins them further with its contour-density setting.
CONTOUR_INTERVAL_FT = 1
# The shallow-water warning slider stops here (web: max=20 half-metres, and the
# phone matches), so bands deeper than this can never be displayed.
SHALLOW_BAND_MAX_M = 12.0

# The floor under a derived coverage radius, in metres: 200 ft, the distance
# from real soundings beyond which the chart is asked to go transparent.
#
# A floor is not optional. The derivation measures along-track spacing - under
# a metre for any echo sounder - and that is not the scale that matters. What
# leaves an unsounded hole in a chart is the gap BETWEEN survey lines, which is
# tens of metres. Deriving the radius from the along-track figure alone blanks
# the water between the lines of a perfectly good lawnmower survey, and when
# the sounder outruns its GPS the figure is zero and the whole chart goes.
MIN_COVERAGE_RADIUS_M = 60.96

DEPTH_COLORMAP = "Blues"

# Sonar transparency ramp (backscatter intensity → alpha).
# Only genuine no-data / off-survey pixels fade out; all real backscatter — even
# dark acoustic shadows — stays fully opaque so the imagery is not washed out.
#   gray <= LO  -> transparent (no return / outside footprint)
#   gray >= HI  -> fully opaque
SONAR_ALPHA_LO = 2
SONAR_ALPHA_HI = 12

# Sonar contrast stretch (raw DN -> display). Maps the useful intensity range to
# full 0-255 black/white, matching the auto-stretch a GIS viewer applies. Values
# are ~p2/p98 of the backscatter histogram; raw data is otherwise midtone-heavy
# (mean ~112) and renders flat/grey without this.
SONAR_STRETCH_LO = 20
SONAR_STRETCH_HI = 230

# ── Tide model (MUST stay in sync with app/.../TideModel.kt) ──────────────────
# Harmonic constituents: (speed °/hr, amplitude m, phase φ° rel. J2000 epoch).
# h(t) = Σ A·cos(ω·t_J2000_hours − φ)
#
# Source: IHO/CICESE published harmonic constants for Guaymas, Sonora
# (nearest major tide-gauge station, ~130 km ENE of Santa Rosalia across the
# Gulf of California). Phase lags g converted via φ = (g − V0(J2000)) mod 360
# using Schureman (1940) equilibrium arguments at 2000-01-01T00:00:00 UTC.
#   M2: g=274° V0=124.3° → φ=149.7°    K1: g=243° V0=10.5° → φ=232.5°
#   S2: g=295° V0=0°     → φ=295.0°    O1: g=219° V0=113.8° → φ=105.2°
#   N2: g=250° V0=349.3° → φ=260.7°    P1: g=242° V0=349.5° → φ=252.5°
TIDE_CONSTITUENTS = [
    (28.9841042, 0.530, 149.7),  # M2  g=274°
    (30.0000000, 0.190, 295.0),  # S2  g=295°
    (28.4397295, 0.118, 260.7),  # N2  g=250°
    (15.0410686, 0.315, 232.5),  # K1  g=243°
    (13.9430356, 0.235, 105.2),  # O1  g=219°
    (14.9589314, 0.104, 252.5),  # P1  g=242°
]
TIDE_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)

# ── Nodal modulation (Foreman 1977, matches TideModel.kt) ─────────────────────
# N(t) = ascending lunar node longitude (degrees)
# N at J2000: 125.0445°, decreasing 0.05295377°/day (18.6-year cycle).

def _nodal_n(dt: datetime) -> float:
    """Ascending node longitude N (degrees) at an aware UTC datetime."""
    days = (dt - TIDE_EPOCH).total_seconds() / 86400.0
    return (125.0445 - 0.05295377 * days) % 360.0

def _nodal_f(speed: float, N_deg: float) -> float:
    """Nodal amplitude factor f for constituent with given speed (deg/hr)."""
    nr = np.radians(N_deg)
    if speed in (30.0, 14.9589314):          # S2, P1
        return 1.0
    elif abs(speed - 15.0410686) < 1e-6:     # K1
        return 1.0060 + 0.1150 * np.cos(nr)
    elif abs(speed - 13.9430356) < 1e-6:     # O1
        return 1.0089 + 0.1871 * np.cos(nr)
    else:                                     # M2, N2
        return 1.0004 - 0.0373 * np.cos(nr)

def _nodal_u(speed: float, N_deg: float) -> float:
    """Nodal phase correction u (degrees) for constituent with given speed."""
    nr = np.radians(N_deg)
    if speed in (30.0, 14.9589314):          # S2, P1
        return 0.0
    elif abs(speed - 15.0410686) < 1e-6:     # K1
        return -8.86 * np.sin(nr)
    elif abs(speed - 13.9430356) < 1e-6:     # O1
        return 10.80 * np.sin(nr)
    else:                                     # M2, N2
        return -2.14 * np.sin(nr)


def tide_height(dt: datetime) -> float:
    """Predicted tide height (m, model mean = 0) at an aware UTC datetime,
    with nodal modulation applied."""
    hours = (dt - TIDE_EPOCH).total_seconds() / 3600.0
    N = _nodal_n(dt)
    return float(sum(
        _nodal_f(speed, N) * A * np.cos(np.radians(speed * hours - phase + _nodal_u(speed, N)))
        for speed, A, phase in TIDE_CONSTITUENTS
    ))


def compute_lat() -> float:
    """Lowest Astronomical Tide = minimum of the nodal-corrected model over 3 years
    (2024-01-01 to 2027-01-01, hourly). Matches TideModel.kt LAT computation."""
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    n_hours = 3 * 365 * 24
    hrs = np.arange(0, n_hours, dtype=np.float64)

    # Node angle at each hourly sample (changes ~0.00221°/hr)
    N_base = _nodal_n(t0)
    N_arr  = (N_base - 0.05295377 / 24.0 * hrs) % 360.0   # deg, vectorised
    base_hours = (t0 - TIDE_EPOCH).total_seconds() / 3600.0 + hrs

    total = np.zeros(n_hours)
    for speed, A, phase in TIDE_CONSTITUENTS:
        nr = np.radians(N_arr)
        f = _nodal_f(speed, 0.0)   # placeholder; compute vectorised below
        # Vectorised nodal factors
        if speed in (30.0, 14.9589314):
            fv = np.ones(n_hours)
            uv = np.zeros(n_hours)
        elif abs(speed - 15.0410686) < 1e-6:
            fv = 1.0060 + 0.1150 * np.cos(nr)
            uv = -8.86 * np.sin(nr)
        elif abs(speed - 13.9430356) < 1e-6:
            fv = 1.0089 + 0.1871 * np.cos(nr)
            uv = 10.80 * np.sin(nr)
        else:
            fv = 1.0004 - 0.0373 * np.cos(nr)
            uv = -2.14 * np.sin(nr)
        total += fv * A * np.cos(np.radians(speed * base_hours - phase + uv))

    return float(total.min())

# ── Utilities ─────────────────────────────────────────────────────────────────

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def init_mbtiles(con: sqlite3.Connection, name: str, wgs84_bounds=None):
    con.execute("""
        CREATE TABLE IF NOT EXISTS tiles (
            zoom_level  INTEGER NOT NULL,
            tile_column INTEGER NOT NULL,
            tile_row    INTEGER NOT NULL,
            tile_data   BLOB    NOT NULL,
            PRIMARY KEY (zoom_level, tile_column, tile_row)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            name  TEXT NOT NULL,
            value TEXT NOT NULL
        )
    """)
    meta = {
        "name":     name,
        "format":   "png",
        "minzoom":  str(ZOOM_MIN),
        "maxzoom":  str(ZOOM_MAX),
        "type":     "overlay",
    }
    # bounds/center are what let the app place an imported chart on its own:
    # without them a standalone .mbtiles has no idea where in the world it is.
    if wgs84_bounds:
        west, south, east, north = wgs84_bounds
        meta["bounds"] = f"{west:.6f},{south:.6f},{east:.6f},{north:.6f}"
        meta["center"] = (f"{(west + east) / 2:.6f},{(south + north) / 2:.6f},"
                          f"{min(ZOOM_MAX, ZOOM_MIN + 3)}")
    con.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", meta.items())


class Progress:
    """
    A heartbeat for long loops.

    Tiling a big mosaic runs for minutes; without a pulse the log looks frozen
    and there is no telling a slow run from a hung one. Reports on a timer
    rather than every N items, so news arrives at the same rate whether a
    survey has 200 tiles or 20,000.
    """

    def __init__(self, total, label, every_seconds=2.0):
        self.total = max(1, total)
        self.label = label
        self.every = every_seconds
        self.count = 0
        self.start = time.time()
        self.last = self.start

    def step(self, n=1):
        self.count += n
        now = time.time()
        if now - self.last < self.every:
            return
        self.last = now
        elapsed = now - self.start
        rate = self.count / elapsed if elapsed else 0
        share = self.count / self.total

        # The first tiles are the slow ones - a zoom-12 tile resamples the whole
        # mosaic, a zoom-22 tile a few pixels of it - so an estimate drawn from
        # the opening rate would read hours for a ten-minute job. Wait until the
        # expensive end is behind us before guessing.
        if share >= 0.1 and rate:
            eta = f"  ~{(self.total - self.count) / rate / 60:.0f} min left"
        else:
            eta = "  (early tiles are the slow ones)" if share < 0.1 else ""
        print(f"    {self.label}: {self.count:,}/{self.total:,} ({100 * share:.0f}%)  "
              f"{elapsed:.0f}s elapsed{eta}", flush=True)

    def finish(self):
        print(f"    {self.label}: {self.count:,} done in "
              f"{time.time() - self.start:.0f}s", flush=True)


def raster_to_mbtiles(tif_path: str, out_mbtiles: str, name: str):
    """Reproject + crop a GeoTIFF into a TMS MBTiles file."""
    print(f"\n  Tiling {os.path.basename(tif_path)} -> {os.path.basename(out_mbtiles)}")

    WEB_MERCATOR = CRS.from_epsg(3857)

    with rasterio.open(tif_path) as src:
        # Reproject bounds to WGS84 to get tile list
        from rasterio.warp import transform_bounds
        wgs84_bounds = transform_bounds(src.crs, CRS.from_epsg(4326), *src.bounds)
        west, south, east, north = wgs84_bounds
        band_count = src.count
        src_dtype  = src.dtypes[0]

    if os.path.exists(out_mbtiles):
        os.remove(out_mbtiles)

    con = sqlite3.connect(out_mbtiles)
    init_mbtiles(con, name, (west, south, east, north))

    total_tiles = sum(
        1 for _ in mercantile.tiles(west, south, east, north,
                                    zooms=list(range(ZOOM_MIN, ZOOM_MAX + 1)))
    )
    print(f"  Generating {total_tiles:,} tiles at zoom {ZOOM_MIN}–{ZOOM_MAX}...")
    pulse = Progress(total_tiles, "tiles")

    processed = 0
    with rasterio.open(tif_path) as src:
        for tile in mercantile.tiles(west, south, east, north,
                                     zooms=list(range(ZOOM_MIN, ZOOM_MAX + 1))):
            xy = mercantile.xy_bounds(tile)   # Web Mercator bounds of this tile

            dst_transform = from_bounds(
                xy.left, xy.bottom, xy.right, xy.top,
                TILE_SIZE, TILE_SIZE
            )

            dst_data = np.zeros(
                (band_count, TILE_SIZE, TILE_SIZE), dtype=src_dtype
            )

            reproject(
                source=rasterio.band(src, list(range(1, band_count + 1))),
                destination=dst_data,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dst_transform,
                dst_crs=WEB_MERCATOR,
                resampling=Resampling.average,
            )

            # Skip fully transparent/empty tiles
            if band_count == 4 and dst_data[3].max() == 0:
                continue
            if band_count < 4 and dst_data.max() == 0:
                continue

            # Convert bands → PIL image
            if band_count == 4:
                img = Image.fromarray(
                    np.moveaxis(dst_data, 0, -1).astype(np.uint8), mode="RGBA"
                )
            elif band_count == 3:
                img = Image.fromarray(
                    np.moveaxis(dst_data, 0, -1).astype(np.uint8), mode="RGB"
                )
            else:
                img = Image.fromarray(dst_data[0].astype(np.uint8), mode="L")

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png_bytes = buf.getvalue()

            # TMS row convention: flip y
            tms_y = (1 << tile.z) - 1 - tile.y

            con.execute(
                "INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)",
                (tile.z, tile.x, tms_y, png_bytes)
            )

            processed += 1
            pulse.step()
            if processed % 500 == 0:
                con.commit()

    pulse.finish()
    con.commit()
    tile_count = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    con.close()
    size_mb = os.path.getsize(out_mbtiles) / 1_048_576
    print(f"  Saved: {out_mbtiles}  ({tile_count:,} tiles, {size_mb:.1f} MB)")


# ── Step 1: Bathymetry raster ─────────────────────────────────────────────────

def build_bathymetry_raster(csv_path: str, out_tif: str,
                           reduce_to_datum: bool = True,
                           max_grid_cells: int = 12000,
                           coverage_radius_m: float = 0.0,
                           with_track: bool = True,
                           contour_smooth_cells: float = 0.0):
    print("\n[1/3] Building bathymetry raster from depth soundings...")

    df = pd.read_csv(csv_path, usecols=["lon", "lat", "dep_m", "date", "time"]).dropna()
    df = df[df["dep_m"] > 0]
    print(f"      {len(df):,} valid soundings")

    lons   = df["lon"].values
    lats   = df["lat"].values

    # Reduce each sounding to LAT (chart datum) using its own timestamp:
    #   depth_LAT = measured_depth - (tide_height(t) - LAT)
    # so the baked bathymetry is the conservative chart-datum baseline.
    if not reduce_to_datum:
        # A hydrographic product (a BAG, say) arrives already reduced to a chart
        # datum. Reducing it again would subtract the tide twice and read
        # shallower than the water ever is - the one error on a depth chart that
        # is both silent and dangerous.
        depths = df["dep_m"].values
        print("      depths taken as already reduced to chart datum "
              "(no tide correction applied)")
        lons_ok = True
    else:
        lons_ok = False
    lat_datum = compute_lat()
    # Sonar instrument logs LOCAL time (America/Hermosillo = UTC−7, no DST).
    # Parse as naive, localize to Hermosillo, then convert to UTC for tide calc.
    ts = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str),
                        errors="coerce", format="mixed")
    ts = ts.dt.tz_localize(SURVEY_TIMEZONE).dt.tz_convert("UTC")
    unix_s = (ts - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds().values
    hours = (unix_s - TIDE_EPOCH.timestamp()) / 3600.0
    h = np.zeros(len(df))
    for speed, A, phase in TIDE_CONSTITUENTS:
        h += A * np.cos(np.radians(speed * hours - phase))
    if not lons_ok:
        reduction = h - lat_datum        # water above LAT at each sounding's time
        depths = df["dep_m"].values - reduction
        print(f"      LAT datum = {lat_datum:.3f} m; mean reduction "
              f"{reduction.mean():.2f} m  -> depths reduced to LAT baseline")

    grid_res = 0.000005  # ~0.5 m resolution
    lon_min, lon_max = lons.min(), lons.max()
    lat_min, lat_max = lats.min(), lats.max()

    # That resolution suits a harbour. Handed a survey tens of kilometres across
    # it asks for tens of gigabytes and the run dies on an allocation error that
    # names a number rather than the reason. Coarsen instead, and say so.
    span = max((lon_max - lon_min), (lat_max - lat_min))
    if span / grid_res > max_grid_cells:
        grid_res = span / max_grid_cells
        print(f"      Survey spans {span:.3f}deg - too wide for a 0.5 m grid; "
              f"using ~{grid_res * 111320:.0f} m cells "
              f"({max_grid_cells} across)")

    grid_lons = np.arange(lon_min, lon_max, grid_res)
    # North-to-south row order so rasterio transform is correct (row 0 = north)
    grid_lats = np.arange(lat_max, lat_min, -grid_res)
    glon, glat = np.meshgrid(grid_lons, grid_lats)
    print(f"      Grid: {glon.shape[1]} x {glon.shape[0]} px "
          f"({glon.size / 1e6:.1f} Mpx)", flush=True)
    print(f"      Interpolating {len(df):,} soundings onto it "
          f"(one long step, no output until it finishes) ...", flush=True)
    started = time.time()

    grid_depth = griddata(
        points=(lons, lats), values=depths,
        xi=(glon, glat), method="linear",
    )
    print(f"      Interpolated in {time.time() - started:.0f}s", flush=True)

    grid_depth = mask_to_coverage(grid_depth, glon, glat, lons, lats,
                                  coverage_radius_m)

    norm    = mcolors.Normalize(vmin=np.nanmin(depths), vmax=np.nanmax(depths))
    import matplotlib
    rgba    = matplotlib.colormaps[DEPTH_COLORMAP](norm(grid_depth))
    rgba[np.isnan(grid_depth), 3] = 0.0

    r = (rgba[:, :, 0] * 255).astype(np.uint8)
    g = (rgba[:, :, 1] * 255).astype(np.uint8)
    b = (rgba[:, :, 2] * 255).astype(np.uint8)
    a = (rgba[:, :, 3] * 255).astype(np.uint8)

    # from_bounds with north-to-south lats: top=lat_max, bottom=lat_min
    transform = from_bounds(lon_min, lat_min, lon_max, lat_max,
                             glon.shape[1], glon.shape[0])

    with rasterio.open(
        out_tif, "w", driver="GTiff",
        height=glon.shape[0], width=glon.shape[1],
        count=4, dtype=np.uint8,
        crs=CRS.from_epsg(4326), transform=transform,
    ) as dst:
        dst.write(r, 1); dst.write(g, 2)
        dst.write(b, 3); dst.write(a, 4)

    print(f"      Saved: {out_tif}")

    build_shallow_bands(grid_depth, transform,
                        os.path.join(OUT_DIR, "shallow_bands.geojson"))

    vmin, vmax = float(np.nanmin(depths)), float(np.nanmax(depths))
    build_contours(glon, glat, grid_depth, vmin, vmax,
                   os.path.join(OUT_DIR, "contours.geojson"),
                   smooth_cells=contour_smooth_cells)
    build_depth_legend(norm, vmin, vmax,
                       os.path.join(OUT_DIR, "depth_legend.png"))
    # Queryable depth grid (LAT datum) for the app's depth-under-boat / tap-to-query.
    build_depth_grid(grid_lons, grid_lats, grid_depth,
                     os.path.join(OUT_DIR, "depth_grid.bin"),
                     os.path.join(OUT_DIR, "depth_grid.json"))
    # After the grid, not before: the coverage outline is polygonised from it,
    # so running first meant a new survey silently got no boundary at all and an
    # old one got an outline of its previous run.
    build_survey_preview(csv_path, with_track=with_track)


def mask_to_coverage(grid_depth, glon, glat, lons, lats, radius_m: float):
    """
    Blank grid cells that sit too far from any real sounding.

    Linear interpolation fills the whole convex hull of the soundings, so a
    survey that ran lines around islands comes back with seabed drawn over the
    islands, and gaps between lines are painted as though they had been
    sounded. A dense lawnmower survey never shows this; a hydrographic survey
    following navigable channels shows it everywhere.

    radius_m < 0 disables the mask. The default is derived from the data: the
    median spacing between neighbouring soundings, doubled, which leaves a
    fully covered survey untouched and trims a sparse one back to what it
    actually measured.

    That derivation has to be done on DISTINCT positions. A Humminbird pings at
    about 14 Hz while its GPS fixes at 1 Hz, so fourteen consecutive soundings
    carry the identical lon/lat; the median nearest-neighbour distance over the
    raw rows is then exactly zero, the radius collapses to zero, and every cell
    in the chart is blanked - which is precisely what happened to Indian Hills
    Lake. A floor is kept underneath as well, because no derivation from data
    should ever be able to erase the whole survey.
    """
    from scipy.spatial import cKDTree

    if radius_m is not None and radius_m < 0:
        return grid_depth

    mid_lat = float(np.nanmean(lats))
    lon_scale = math.cos(math.radians(mid_lat))
    points = np.column_stack((lons * lon_scale, lats)) * 111320.0
    tree = cKDTree(points)

    if not radius_m:
        # Nearest-neighbour spacing between the positions the boat actually
        # occupied. k=2 because the nearest point to a sounding is itself.
        fixes = np.unique(points, axis=0)
        sample = fixes[:: max(1, len(fixes) // 20000)]
        spacing, _ = tree.query(sample, k=2) if len(fixes) < 2 else             cKDTree(fixes).query(sample, k=2)
        spread = float(np.median(spacing[:, 1])) if len(fixes) > 1 else 0.0
        radius_m = max(spread * 2.0, MIN_COVERAGE_RADIUS_M)
        note = "" if spread * 2.0 >= MIN_COVERAGE_RADIUS_M else             f" (floor; {len(points)-len(fixes):,} soundings share a fix)"
        print(f"      Coverage mask: fixes sit {spread:.1f} m apart, "
              f"blanking cells over {radius_m:.0f} m from one{note}")

    cells = np.column_stack((glon.ravel() * lon_scale, glat.ravel())) * 111320.0
    distance, _ = tree.query(cells, k=1)
    beyond = (distance > radius_m).reshape(grid_depth.shape)
    dropped = int(beyond.sum() - np.isnan(grid_depth)[beyond].sum())
    grid_depth = np.where(beyond, np.nan, grid_depth)
    covered = np.count_nonzero(~np.isnan(grid_depth))
    print(f"      Coverage mask: dropped {dropped:,} interpolated cells; "
          f"{covered/grid_depth.size*100:.0f}% of the grid is surveyed")
    return grid_depth


def build_survey_preview(csv_path: str, with_track: bool = True):
    """
    The outline and trackline an un-downloaded survey shows on the map.

    Runs after the depth grid exists, since the outline is drawn from it. Kept
    in its own module because it is also useful on its own, for surveys built
    before previews existed.
    """
    import build_preview

    print("      Building the coverage outline"
          + (" and trackline ..." if with_track else " (no trackline) ..."), flush=True)
    build_preview.build(OUT_DIR, csv_path if with_track else "")


def build_shallow_bands(grid_depth, transform, out_geojson,
                        max_depth_m: float = SHALLOW_BAND_MAX_M):
    """
    Polygonize the depth grid into 0.5 m bands tagged with their lower-bound
    depth `d`. The app shows bands where d < the user's threshold, highlighting
    everything shallower than the minimum depth they chose.

    Only the water the warning can reach is banded. The slider stops at
    SHALLOW_BAND_MAX_M, so banding a 120 m survey all the way down produced
    245 bands and 700,000 polygons - a quarter of a gigabyte describing depths
    no threshold could ever select, which then had to be left out of the chart
    entirely, taking the working part of the warning with it.
    """
    import json
    from rasterio.features import shapes

    # 0.5 m bands: store band index in half-metres (NaN -> -1, masked out),
    # then expose the lower-edge depth `d` (e.g. 1.0, 1.5, 2.0 ...) per polygon.
    half = np.where(np.isnan(grid_depth), -1, np.floor(grid_depth * 2)).astype(np.int32)
    mask = half >= 0
    if max_depth_m > 0:
        deep = grid_depth > max_depth_m
        mask = mask & ~np.nan_to_num(deep, nan=False)

    features = []
    for geom, val in shapes(half, mask=mask, transform=transform):
        features.append({
            "type": "Feature",
            "properties": {"d": int(val) / 2.0},
            "geometry": geom,
        })

    fc = {"type": "FeatureCollection", "features": features}
    with open(out_geojson, "w") as f:
        json.dump(fc, f)
    size = os.path.getsize(out_geojson) / 1e6
    print(f"      Saved: {out_geojson}  ({len(features)} depth-band polygons "
          f"to {max_depth_m:g} m, {size:.1f} MB)")


def build_contours(glon, glat, grid_depth, vmin, vmax, out_geojson,
                   smooth_cells: float = 0.0):
    """
    Generate isobath contour lines as GeoJSON LineStrings tagged with depth.

    Levels sit on whole feet counted from 0 ft, spaced CONTOUR_INTERVAL_FT apart
    (1 ft by default). Each feature carries both `depth` (metres, what the labels
    are computed from) and `ft` (whole feet, an exact integer the app filters on
    for its contour-density setting).

    smooth_cells blurs the grid before tracing - contours only, never the depth
    grid the app queries. Raw per-ping soundings carry the sounder's own noise,
    and a contour traced through it wanders around every wobble: the lines come
    out as black scribble that hides the chart underneath instead of describing
    the shape of the bottom. Smoothing changes the drawing, not the depths.
    """
    import json
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if smooth_cells > 0:
        from scipy.ndimage import gaussian_filter
        # NaN-aware blur: weight by where there is data, so the smoothing does
        # not drag the edges of a swath inward towards nothing.
        present = np.isfinite(grid_depth).astype(float)
        filled = np.where(np.isfinite(grid_depth), grid_depth, 0.0)
        blurred = gaussian_filter(filled, smooth_cells)
        weight = gaussian_filter(present, smooth_cells)
        with np.errstate(invalid="ignore", divide="ignore"):
            grid_depth = np.where(present > 0, blurred / weight, np.nan)
        print(f"      Contours smoothed over {smooth_cells:g} cells "
              f"(the depth grid itself is untouched)")

    step_ft = CONTOUR_INTERVAL_FT if CONTOUR_INTERVAL_FT > 0 else 1
    feet = np.arange(0, int(np.floor(vmax / FT_PER_M)) + 1, step_ft)
    feet = feet[feet * FT_PER_M >= vmin]        # drop levels above the shallowest sounding
    levels = feet * FT_PER_M
    if len(levels) < 2:
        levels = np.array([vmin, vmax])
        feet = np.round(levels / FT_PER_M).astype(int)
    print(f"      Contours every {step_ft} ft from 0 ft: {feet.tolist()} ft")

    fig = plt.figure()
    cs = plt.contour(glon, glat, grid_depth, levels=levels)
    plt.close(fig)

    features = []
    for level, segs in zip(cs.levels, cs.allsegs):
        for seg in segs:
            if len(seg) < 2:
                continue
            coords = [[float(x), float(y)] for x, y in seg]
            features.append({
                "type": "Feature",
                "properties": {
                    "depth": float(level),
                    "ft": int(round(level / FT_PER_M)),
                },
                "geometry": {"type": "LineString", "coordinates": coords},
            })

    fc = {"type": "FeatureCollection", "features": features}
    with open(out_geojson, "w") as f:
        json.dump(fc, f)
    print(f"      Saved: {out_geojson}  ({len(features)} contour segments)")


def build_depth_legend(norm, vmin, vmax, out_png):
    """Render a vertical colorbar legend matching the depth colormap/range."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe
    from matplotlib.cm import ScalarMappable

    # White text with a black outline for legibility over any map background.
    outline = [pe.withStroke(linewidth=3.0, foreground="black")]

    fig, ax = plt.subplots(figsize=(2.0, 4.4), dpi=100)
    fig.subplots_adjust(left=0.04, right=0.38, top=0.96, bottom=0.07)

    sm = ScalarMappable(norm=norm, cmap=DEPTH_COLORMAP)
    cb = fig.colorbar(sm, cax=ax)
    cb.set_label("Depth (m)", color="white", fontsize=18, fontweight="bold")
    cb.ax.yaxis.label.set_path_effects(outline)
    cb.ax.yaxis.set_tick_params(color="white", labelcolor="white", labelsize=16)
    for t in cb.ax.get_yticklabels():
        t.set_path_effects(outline)
    cb.outline.set_edgecolor("black")
    cb.outline.set_linewidth(1.4)

    fig.patch.set_alpha(0.0)
    ax.patch.set_alpha(0.0)
    fig.savefig(out_png, transparent=True)
    plt.close(fig)
    print(f"      Saved: {out_png}  (range {vmin:.1f}-{vmax:.1f} m)")


def build_depth_grid(grid_lons, grid_lats, grid_depth, out_bin, out_json,
                     downsample=2):
    """Export a compact, queryable depth grid (metres, LAT datum) for the app.

    Format:
      depth_grid.json header: {lonMin, latMin, dLon, dLat, cols, rows, nodata}
      depth_grid.bin: float32 row-major, row 0 = SOUTH (lat ascending), NaN=nodata.
    The app does bilinear interpolation; NaN corners -> "outside coverage".
    """
    import json
    # grid_lons ascending; grid_lats descending (row 0 = north). Flip to south-first.
    d = np.asarray(grid_depth, dtype=np.float32)[::-1]          # row 0 -> south
    d = d[::downsample, ::downsample]
    lons = np.asarray(grid_lons)[::downsample]
    lats = np.asarray(grid_lats)[::-1][::downsample]            # ascending
    header = {
        "lonMin": float(lons[0]),
        "latMin": float(lats[0]),
        "dLon":   float(lons[1] - lons[0]),
        "dLat":   float(lats[1] - lats[0]),
        "cols":   int(d.shape[1]),
        "rows":   int(d.shape[0]),
        "nodata": "nan",
    }
    d.astype("<f4").tofile(out_bin)
    with open(out_json, "w") as f:
        json.dump(header, f)
    mb = os.path.getsize(out_bin) / 1_048_576
    print(f"      Saved: {out_bin}  ({header['cols']}x{header['rows']}, {mb:.2f} MB)")


def build_substrate_grid(tif_path, out_bin, out_json, target_res_m=1.5):
    """Export a queryable substrate-class grid (uint8 class index; 255 = none)
    sampled onto a regular WGS84 grid, for the app's tap-to-query."""
    import json
    if not os.path.exists(tif_path):
        print(f"      WARNING: {tif_path} not found — skipping substrate grid.")
        return
    from rasterio.warp import transform_bounds, reproject, Resampling
    with rasterio.open(tif_path) as src:
        w, s, e, n = transform_bounds(src.crs, CRS.from_epsg(4326), *src.bounds)
        # ~target_res_m at this latitude
        import math
        dlat = target_res_m / 111_000.0
        dlon = target_res_m / (111_000.0 * math.cos(math.radians((s + n) / 2)))
        cols = max(1, int((e - w) / dlon))
        rows = max(1, int((n - s) / dlat))
        dst = np.full((rows, cols), 255, dtype=np.uint8)
        dst_transform = from_bounds(w, s, e, n, cols, rows)   # row 0 = north
        reproject(
            source=rasterio.band(src, 1), destination=dst,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=dst_transform, dst_crs=CRS.from_epsg(4326),
            resampling=Resampling.nearest, dst_nodata=255,
        )
    dst = dst[::-1]   # flip to south-first to match depth grid convention
    header = {
        "lonMin": float(w), "latMin": float(s),
        "dLon": float((e - w) / cols), "dLat": float((n - s) / rows),
        "cols": int(cols), "rows": int(rows), "nodata": 255,
    }
    dst.tofile(out_bin)
    with open(out_json, "w") as f:
        json.dump(header, f)
    print(f"      Saved: {out_bin}  ({cols}x{rows} substrate classes)")


# Substrate class index -> (label, RGB). Indices match the substrate raster's
# embedded colormap. 0 (NoData) and 8 (Water-NoData) are transparent on the map
# and omitted from the legend.
SUBSTRATE_CLASS_LABELS = [
    ("Fines Ripple",   (220, 57, 18)),
    ("Fines Flat",     (255, 153, 0)),
    ("Cobble Boulder", (16, 150, 24)),
    ("Hard Bottom",    (153, 0, 153)),
    ("Wood",           (0, 153, 198)),
    ("Other",          (221, 68, 119)),
    ("Shadow",         (102, 170, 0)),
]


# Classes the shipped RockMapper model (RockMapper_20251117_v2) predicts, with the
# colours it bakes into its own raster. Note the README advertises four classes
# (fines/gravel/boulder/bedrock) but the model config declares six, 0 = NoData:
#   {'0': 'NoData', '1': 'Shadow', '2': 'Other', '3': 'Gravel',
#    '4': 'Cobble Boulder', '5': 'Bedrock'}
ROCK_CLASS_LABELS = [
    ("Shadow",         (220, 57, 18)),
    ("Other",          (255, 153, 0)),
    ("Gravel",         (16, 150, 24)),
    ("Cobble Boulder", (153, 0, 153)),
    ("Bedrock",        (0, 153, 198)),
]


def ensure_rock_colormap(tif_path: str):
    """
    RockMapper writes a plain classified raster; the tiler needs a colormap to
    turn class values into pixels. Attach ours to a temp copy when the raster has
    none, leaving the original untouched. Returns (path, is_temp).
    """
    with rasterio.open(tif_path) as src:
        try:
            if src.colormap(1):
                return tif_path, False
        except Exception:
            pass
        profile = src.profile.copy()
        data = src.read(1)

    profile.update(count=1, dtype='uint8', nodata=0)
    tmp_path = os.path.splitext(tif_path)[0] + "__coloured.tif"
    cmap = {0: (0, 0, 0, 0)}          # class 0 = no prediction -> transparent
    for i, (_label, rgb) in enumerate(ROCK_CLASS_LABELS, start=1):
        cmap[i] = (*rgb, 255)

    with rasterio.open(tmp_path, 'w', **profile) as dst:
        dst.write(data.astype(np.uint8), 1)
        dst.write_colormap(1, cmap)
    print(f"      Applied rock palette -> {os.path.basename(tmp_path)}")
    return tmp_path, True


def build_rock_mbtiles(tif_path: str, out_mbtiles: str):
    """Tile RockMapper's habitat raster into MBTiles for the app's rock overlay."""
    print(f"\n[3c] Building rock MBTiles from {os.path.basename(tif_path)}...")
    if not os.path.exists(tif_path):
        print(f"      WARNING: {tif_path} not found - skipping rock layer.")
        return
    coloured, is_temp = ensure_rock_colormap(tif_path)
    try:
        # Same classified-raster tiler the substrate layer uses.
        build_substrate_mbtiles(coloured, out_mbtiles, layer_name="rock")
    finally:
        if is_temp and os.path.exists(coloured):
            os.remove(coloured)


def build_rock_legend(out_png, tif_path=None):
    """
    Render the rock habitat key, styled like the substrate one so the two read as
    a set: swatch plus outlined label per class, transparent background.

    When the habitat raster is available its own colormap wins, so the key can
    never drift from what the model actually painted.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import matplotlib.patheffects as pe

    labels = list(ROCK_CLASS_LABELS)
    if tif_path and os.path.exists(tif_path):
        try:
            with rasterio.open(tif_path) as src:
                cmap = src.colormap(1)
            labels = [(name, cmap[i][:3]) for i, (name, _rgb)
                      in enumerate(ROCK_CLASS_LABELS, start=1) if i in cmap]
        except Exception:
            pass

    outline = [pe.withStroke(linewidth=3.0, foreground="black")]
    n = len(labels)
    fig, ax = plt.subplots(figsize=(3.4, 0.42 * n + 0.3), dpi=110)
    ax.set_xlim(0, 1); ax.set_ylim(0, n); ax.axis("off")
    for i, (label, (r, g, b)) in enumerate(labels):
        y = n - 1 - i
        ax.add_patch(mpatches.Rectangle((0.03, y + 0.18), 0.14, 0.64,
                     facecolor=(r/255, g/255, b/255), edgecolor="black", linewidth=1.2))
        t = ax.text(0.22, y + 0.5, label, va="center", ha="left",
                    fontsize=15, fontweight="bold", color="white")
        t.set_path_effects(outline)
    fig.patch.set_alpha(0.0); ax.patch.set_alpha(0.0)
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.01)
    fig.savefig(out_png, transparent=True)
    plt.close(fig)
    print(f"      Saved: {out_png}  ({n} classes)")


def build_substrate_legend(out_png):
    """Render the categorical substrate class key (swatch + label per class)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import matplotlib.patheffects as pe

    outline = [pe.withStroke(linewidth=3.0, foreground="black")]
    n = len(SUBSTRATE_CLASS_LABELS)
    fig, ax = plt.subplots(figsize=(3.4, 0.42 * n + 0.3), dpi=110)
    ax.set_xlim(0, 1); ax.set_ylim(0, n); ax.axis("off")
    for i, (label, (r, g, b)) in enumerate(SUBSTRATE_CLASS_LABELS):
        y = n - 1 - i
        ax.add_patch(mpatches.Rectangle((0.03, y + 0.18), 0.14, 0.64,
                     facecolor=(r/255, g/255, b/255), edgecolor="black", linewidth=1.2))
        t = ax.text(0.22, y + 0.5, label, va="center", ha="left",
                    fontsize=15, fontweight="bold", color="white")
        t.set_path_effects(outline)
    fig.patch.set_alpha(0.0); ax.patch.set_alpha(0.0)
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.01)
    fig.savefig(out_png, transparent=True)
    plt.close(fig)
    print(f"      Saved: {out_png}  ({n} classes)")


# ── Step 2: Merge sonar tiles ─────────────────────────────────────────────────

def usable_mosaics(tif_paths: list) -> list:
    """
    Drop mosaic tiles whose georeferencing is broken.

    A track segment with bad GPS makes PINGMapper write a tile stretched over
    thousands of kilometres at metres-per-pixel instead of centimetres. Merging
    one of those with the good tiles asks rasterio for their union at the fine
    resolution - petabytes - and the run dies on an allocation error that says
    nothing about the cause. Judge each tile against the median pixel size and
    leave the outliers out.
    """
    sizes = []
    for path in tif_paths:
        with rasterio.open(path) as src:
            sizes.append((path, src.res[0], tuple(src.bounds)))
    median = sorted(r for _p, r, _b in sizes)[len(sizes) // 2]

    keep, dropped = [], []
    for entry in sizes:
        # 10x coarser than its siblings is not a resolution difference, it is a
        # tile that landed somewhere else entirely.
        (keep if entry[1] <= median * 10 else dropped).append(entry)

    for path, res, bounds in dropped:
        print(f"      Skipping {os.path.basename(path)}: {res:.4g} m/px against "
              f"a median of {median:.4g} - its georeferencing is broken")
        print(f"        bounds {tuple(round(v) for v in bounds)}")
    if not keep:
        raise RuntimeError("Every sonar mosaic looks mis-georeferenced; nothing to merge")
    return [path for path, _res, _bounds in keep]


# ── Merging overlapping mosaics by which look is the better one ──────────────

# Where side scan is worth trusting, as a fraction of the swath's own max range.
SONAR_PLATEAU_END = 0.6     # past this the return weakens and the footprint grows
SONAR_FAR_FLOOR = 0.35      # what the outermost ground range is still worth
SONAR_QUALITY_CELL_M = 1.0  # quality varies over metres, not pixels

# How wide the poor zone under the boat is, per metre of height above the
# bottom. Slant-range correction turns a sample at slant range s into ground
# range g = sqrt(s^2 - h^2), so one slant sample is stretched over 1/sin(theta)
# of ground, theta being the angle off vertical: 1.4x at g = h, 2.2x at h/2,
# 4.1x at h/4. 1.0 puts the edge at 45 degrees, where that stretch drops under
# root two. A transducer tilted further down sees texture closer in and wants
# less; one that leaves a wide dark band under the boat wants more.
SONAR_NADIR_PER_DEPTH = 1.0
# Depth sets the nadir width ping by ping, smoothed along the pass first: a
# running median over this many pings, so one bad bottom pick does not notch
# the scores.
SONAR_DEPTH_WINDOW_PINGS = 51

# Working memory the merge holds at once, however large the survey: it works
# through the output a window at a time and never holds the whole grid.
SONAR_MERGE_BUDGET_MB = 512
SONAR_MERGE_BLOCK = 512     # output tile size; windows are whole multiples of it
# What the merge holds per pixel of its window: the winning value, score and
# pass (7 bytes), one pass's read and validity (2), its score and the
# interpolation temporaries behind it (~25), and the quality grid that is
# interpolated from - at most a quarter the window's pixel count, at ~80 bytes
# a cell (20).
SONAR_MERGE_BYTES_PER_PX = 56

# Blending is off. The reasoning below is kept because the idea is sound and
# the implementation worked; what failed was the premise.
#
# The claim was that averaging two passes sheds speckle, the way multi-look
# averaging has always worked in radar and sonar. Measured over the whole
# overlap of an 8-pass Indian Hills survey, it does not:
#
#                      native (0.019 m)   0.15 m    stdev
#   first-file-wins            21.419     26.291     65.1
#   best look wins             21.411     26.024     65.2
#   blended                    18.862     22.161     57.5
#   blended vs order           -11.9%     -15.7%    -11.7%
#
# Shedding speckle means pixel-scale variation falling FASTER than texture.
# It fell slower: the high-to-low ratio went from 0.815 to 0.851, up 4.5%.
# Everything dropped together, so what the blend did was flatten contrast,
# and it took slightly more texture than noise with it.
#
# The likely reason is in the note at the end of merge_sonar_by_quality: the
# residual registration error between passes is one to four metres, which at
# 0.0188 m/px is 53 to 213 pixels. Two views of the same ground offset by
# that much do not average into a cleaner look at it; they blur.
#
# What would make it worth revisiting is registration. Cross-correlate the
# overlap and shift one pass onto the other before averaging, and the premise
# becomes true again - the looks would then actually be of the same ground.
# Until then the merge keeps the single best look, which the same measurement
# shows is where the real win is: quality picks a different pass from file
# order on 76.4% of the overlap, by 54 of 255 on average.
#
# SONAR_BLEND_POWER = 3.0   # cubed, a nadir look at 0.2 carried 0.008


def transect_tracks(meta_csv: str, log=print) -> dict:
    """
    Where the boat was on each pass, and what each ping could see, from
    PINGMapper's own ping metadata.

    With any track filter set, PINGMapper mosaics per transect - one file per
    continuous run of pings that survived the filter - and numbers the transects
    in the order it writes the mosaics. So transect i's ping positions are the
    trackline of mosaic i, which is what lets a pixel be turned back into a
    ground range from the boat that recorded it.

    Nadir and range are kept per ping rather than as one figure for the pass. A
    pass that runs from 3 m of water into 12 m has a nadir zone four times as
    wide at one end as the other, and a range changed mid-pass moves where the
    far field starts; a pass-wide median got both wrong for most of the pass.
    Each pixel takes the figures of the ping its ground range is measured from.

    max_range is the slant range the unit was set to. The mosaics have the
    water column removed and are in ground range, where that reach is
    sqrt(range^2 - depth^2).

    Returns {transect: {points (N,2) in the mosaic CRS, tree over them,
    nadir (N,) and far (N,) in metres, reach - the furthest far}}.
    """
    from scipy.spatial import cKDTree

    df = pd.read_csv(meta_csv)
    missing = {"transect", "e", "n", "max_range"} - set(df.columns)
    if missing:
        raise KeyError(f"{os.path.basename(meta_csv)} has no {sorted(missing)} column(s)")
    # PINGMapper's own bottom pick where it made one, the unit's otherwise.
    depth_col = next((c for c in ("dep_m", "inst_dep_m") if c in df.columns), None)
    order = next((c for c in ("time_s", "record_num") if c in df.columns), None)

    tracks, summary = {}, []
    for transect, group in df.groupby("transect"):
        group = group.dropna(subset=["e", "n"])
        if group.empty:
            continue
        if order:
            group = group.sort_values(order)
        if depth_col:
            # 0 and below are failed picks, not depths: the median steps over
            # them, and a run too long for it is filled from either side.
            depth = group[depth_col].where(group[depth_col] > 0)
            depth = (depth.rolling(SONAR_DEPTH_WINDOW_PINGS, center=True, min_periods=1)
                     .median().interpolate(limit_direction="both")
                     .fillna(0.0).to_numpy(dtype=float))
        else:
            depth = np.zeros(len(group))
        slant = group["max_range"].fillna(group["max_range"].median()).to_numpy(dtype=float)
        far = np.nan_to_num(np.sqrt(np.maximum(slant ** 2 - depth ** 2, 0.0)))
        points = np.column_stack((group["e"].to_numpy(dtype=float),
                                  group["n"].to_numpy(dtype=float)))
        tracks[int(transect)] = {
            "points": points,
            "tree": cKDTree(points),
            "nadir": depth * SONAR_NADIR_PER_DEPTH,
            "far": far,
            "reach": float(far.max()),
        }
        summary.append(f"#{int(transect)} {len(points):,} pings "
                       f"{depth.min():.1f}-{depth.max():.1f} m deep "
                       f"@{np.nanmax(slant):.0f} m")
    log(f"      {len(tracks)} transect(s): {', '.join(summary[:4])}"
        + (" ..." if len(summary) > 4 else ""))
    return tracks


def sonar_quality(ground_range, nadir_m, max_range_m):
    """
    How far to trust a side-scan pixel sitting `ground_range` metres off its own
    trackline. Near 0 = only if nothing else covers this ground, 1 = the good
    part.

    Side scan is poor under the boat and poor at the far edge and good in
    between, so this is a ramp, a plateau and a decline:

      below nadir_m     The nadir zone. With the water column removed, the few
                        near-vertical samples get stretched over the widest
                        patch of ground in the swath, and the return is
                        specular rather than textural. Scored 0..1 across the
                        zone rather than 0 outright, because it is poor data
                        and not absent data: it still has to be able to win
                        where no other pass covers that ground at all - which
                        is also why right under the boat scores just above 0
                        rather than at it.
      to 0.6 of range   The good part - grazing angle low enough to throw
                        readable shadows, footprint still small.
      beyond that       Declines to SONAR_FAR_FLOOR at max range: weaker
                        return, bigger footprint, more noise.

    nadir_m and max_range_m are each a number or an array matching
    ground_range, so every pixel can carry the figures of its own ping. See
    SONAR_NADIR_PER_DEPTH for how depth becomes a nadir width.
    """
    r = np.asarray(ground_range, dtype="float32")
    nadir = np.maximum(np.asarray(nadir_m, dtype="float32"), 0.5)
    far = np.maximum(np.asarray(max_range_m, dtype="float32"), nadir * 2.0)
    plateau = far * SONAR_PLATEAU_END

    # far is at least twice nadir, so the plateau starts past the nadir zone
    # and the ramp and the decline never overlap: the lower of the two is
    # whichever one applies.
    ramp = np.clip(r / nadir, 1e-3, 1.0)
    decline = 1.0 - (1.0 - SONAR_FAR_FLOOR) * np.clip(
        (r - plateau) / np.maximum(far - plateau, 1e-3), 0.0, 1.0)
    return np.minimum(ramp, decline).astype("float32")


def quality_cell(res_m: float) -> float:
    """
    The quality grid's cell for a mosaic of res_m metres a pixel.

    A metre, unless the mosaic is coarser than half that: the grid then follows
    the pixels, so it is never more than a quarter their count and its memory
    stays inside what SONAR_MERGE_BYTES_PER_PX allows for it.
    """
    return max(SONAR_QUALITY_CELL_M, 2.0 * float(res_m))


def quality_for(track, xs, ys, cell: float):
    """
    Quality of every pixel centred at xs (columns) by ys (rows) for one pass.

    Solved on a coarse grid and interpolated per pixel rather than solved per
    pixel: a KD-tree query for each of a hundred million pixels costs minutes
    and buys nothing, because ground range does not change appreciably within
    a metre. Each grid cell takes the nadir and range of the ping nearest it,
    the same ping its ground range is measured from.

    The grid is anchored at the CRS origin rather than at any raster's corner,
    so a pixel scores the same whichever window it is read in. The merge works
    a window at a time, and a grid that moved with the windows would put a
    seam at every window edge.

    Interpolating rather than taking the nearest cell is what keeps the seam
    between two passes smooth. The chart is 0.019 m/px, so rounding to the
    nearest 1 m cell quantises the boundary into 53-pixel treads - and a pixel
    one tread over gets handed to the worse pass for no reason but the
    rounding. Interpolating moves no pixel value and averages no look: the
    winner is still one pass's sample, untouched. It only puts the boundary
    where the two scores genuinely cross.
    """
    fx = np.asarray(xs, dtype=float) / cell - 0.5
    fy = np.asarray(ys, dtype=float) / cell - 0.5
    i0, j0 = int(np.floor(fx.min())), int(np.floor(fy.min()))
    # One cell past the last floor on each axis, so every pixel has a cell on
    # both sides of it and nothing is clamped at the edge of the grid.
    gx = (np.arange(i0, int(np.floor(fx.max())) + 2) + 0.5) * cell
    gy = (np.arange(j0, int(np.floor(fy.max())) + 2) + 0.5) * cell
    mesh_x, mesh_y = np.meshgrid(gx, gy)
    # Cells beyond the swath score the floor whichever ping is nearest, so the
    # tree can give up on them early. Those misses come back as distance inf
    # and an index one past the end.
    distance, nearest = track["tree"].query(
        np.column_stack((mesh_x.ravel(), mesh_y.ravel())), k=1,
        distance_upper_bound=track["reach"] + 2.0 * cell)
    nearest = np.minimum(nearest, len(track["nadir"]) - 1)
    grid = sonar_quality(distance, track["nadir"][nearest],
                         track["far"][nearest]).reshape(len(gy), len(gx))

    # The grid is separable - one row index per row, one column index per
    # column - so this is four outer-product gathers rather than a resample.
    fr, fc = fy - j0, fx - i0
    r0, c0 = np.floor(fr).astype(int), np.floor(fc).astype(int)
    wr = (fr - r0).astype("float32")[:, None]
    wc = (fc - c0).astype("float32")[None, :]
    return ((1 - wr) * (1 - wc) * grid[np.ix_(r0, c0)]
            + (1 - wr) * wc * grid[np.ix_(r0, c0 + 1)]
            + wr * (1 - wc) * grid[np.ix_(r0 + 1, c0)]
            + wr * wc * grid[np.ix_(r0 + 1, c0 + 1)]).astype("float32")


def work_windows(width: int, height: int, budget_mb: float = SONAR_MERGE_BUDGET_MB):
    """
    Cover a width x height grid in windows that fit the memory budget.

    Full-width bands wherever a band one block high fits, because a GeoTIFF
    stored in strips - the default - decompresses a whole strip to read any
    part of it, and a full-width band reads each strip once where narrower
    windows would read it once per window across. Narrower windows only when
    the survey is too wide for that. Either way the edges fall on whole
    blocks, so no output tile is written twice.
    """
    from rasterio.windows import Window

    block = SONAR_MERGE_BLOCK
    pixels = max(int(budget_mb * 2 ** 20) // SONAR_MERGE_BYTES_PER_PX, block * block)
    if width * block <= pixels:
        cols, rows = width, pixels // width // block * block
    else:
        cols, rows = pixels // block // block * block, block
    for row in range(0, height, rows):
        for col in range(0, width, cols):
            yield Window(col, row, min(cols, width - col), min(rows, height - row))


def mask_mosaics_to_plateau(tif_paths: list, out_dir: str, tracks: dict,
                            min_quality: float = 1.0, log=print) -> list:
    """
    Copy each mosaic with everything outside the quality plateau blanked.

    Habitat models - RockMapper, and PINGMapper's own substrate prediction -
    read a folder of mosaics and never see the ping metadata, so they have no
    idea where the boat was. That matters more than it sounds: measured on
    Indian Hills, RockMapper's "Bedrock" class spikes to 22.8% in the 20-30 ft
    across-track band and falls to 1.5% beyond 49 ft, and PINGMapper's "Hard
    Bottom" peaks at 47.7% in that same band. A lake bed does not change
    composition according to how far the boat was from it; both models are
    reading the sonar's own brightness falloff as substrate.

    Neither tool can filter that itself - RockMapper's whole parameter set is
    window size, stride, patch-size cleanup and smoothing, none of which touch
    a stripe tens of feet wide. So it has to be done to the input: hand them
    only the band where the imagery is radiometrically consistent, which is the
    same plateau the mosaic merge already scores at 1.0.

    The cost is coverage from any single pass, but at plateau-width line
    spacing a neighbouring line covers what this one loses.
    """
    import shutil

    os.makedirs(out_dir, exist_ok=True)
    out_paths, kept_total, seen_total = [], 0, 0
    for index, path in enumerate(tif_paths):
        track = tracks.get(index)
        out_path = os.path.join(out_dir, os.path.basename(path))
        if track is None:
            log(f"      [{index+1}/{len(tif_paths)}] no transect {index} - copied unmasked")
            shutil.copy2(path, out_path)
            out_paths.append(out_path)
            continue

        with rasterio.open(path) as src:
            profile = src.profile.copy()
            res_x, res_y = src.res
            cell = quality_cell(max(res_x, res_y))
            with rasterio.open(out_path, "w", **profile) as dst:
                for window in work_windows(src.width, src.height):
                    data = src.read(1, window=window)
                    valid = data > 0
                    if not valid.any():
                        dst.write(data, 1, window=window)
                        continue
                    ys = src.bounds.top - (np.arange(
                        window.row_off, window.row_off + data.shape[0]) + 0.5) * res_y
                    xs = src.bounds.left + (np.arange(
                        window.col_off, window.col_off + data.shape[1]) + 0.5) * res_x
                    keep = valid & (quality_for(track, xs, ys, cell) >= min_quality)
                    seen_total += int(valid.sum())
                    kept_total += int(keep.sum())
                    dst.write(np.where(keep, data, 0).astype(data.dtype), 1,
                              window=window)
        out_paths.append(out_path)
        log(f"      [{index+1}/{len(tif_paths)}] {os.path.basename(path)} masked", flush=True)

    share = kept_total / max(seen_total, 1) * 100
    log(f"      kept {kept_total:,} of {seen_total:,} px ({share:.0f}%) at "
        f"quality >= {min_quality}")
    return out_paths


def merge_sonar_by_quality(tif_paths: list, out_tif: str, tracks: dict,
                           log=print, budget_mb: float = SONAR_MERGE_BUDGET_MB):
    """
    Merge overlapping mosaics by quality: at each pixel, keep the look from
    the pass that saw that ground best.

    What this replaces is rasterio's default, where the first file in the list
    that happens to hold data wins. That settles a real question - which pass
    saw this ground better - by reference to a sort order, which is no answer
    at all.

    This is where the win is, and it is a large one: over an 8-pass Indian
    Hills survey, 54.4% of the charted ground is seen by two or more passes,
    and on 76.4% of that overlap the best-scoring pass is not the one file
    order would have picked - differing by 54 of 255 on average.

    A pass with no track in `tracks` scores 0.5 everywhere. With no tracks at
    all every pass scores the same, the strict comparison hands each pixel to
    the first file holding data, and that is rasterio's merge exactly - so a
    survey without ping metadata comes through here too, rather than through
    rasterio.merge, which builds the whole mosaic in memory.

    Memory is budget_mb whatever the size of the survey. The output is worked
    through a window at a time (see work_windows), each window written as it
    is finished, so nothing ever spans the whole grid. Holding the winning
    value and score for the whole grid at once costs five bytes a pixel of the
    survey's bounding box: 27 GB for 2 km by 1 km at 0.019 m.

    Each pass is read through a WarpedVRT on the output grid. Where the passes
    share a CRS and pixel size that is a straight copy; where one does not, it
    is still put in the right place, which placing it by rounded offset - the
    way this used to work - did not do.

    There was a second mode that averaged the overlapping looks together,
    weighted by score. It is commented out above and below rather than
    deleted: the idea is sound, the code worked, and only the premise failed.
    See the note on SONAR_BLEND_POWER for the measurement that retired it.

    The residual registration error between passes is around one to four metres
    (from the coarse-scale correlation above, and consistent with the metadata's
    own e_err_m/n_err_m of up to 1.2 m). That is fine for seabed texture, which
    has no fixed phase to smear, and it is the reason this is not carried any
    further: a discrete target seen twice will thicken slightly rather than
    sharpen.
    """
    from rasterio.vrt import WarpedVRT
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window

    srcs = [rasterio.open(p) for p in tif_paths]
    vrts = []
    try:
        res_x, res_y = srcs[0].res
        crs = srcs[0].crs
        bounds = [tuple(s.bounds) if s.crs == crs
                  else transform_bounds(s.crs, crs, *s.bounds) for s in srcs]
        west = min(b[0] for b in bounds)
        north = max(b[3] for b in bounds)
        width = int(round((max(b[2] for b in bounds) - west) / res_x))
        height = int(round((north - min(b[1] for b in bounds)) / res_y))
        transform = rasterio.transform.from_origin(west, north, res_x, res_y)
        cell = quality_cell(max(res_x, res_y))
        windows = list(work_windows(width, height, budget_mb))
        log(f"      Output grid {width:,} x {height:,} px at {res_x:.4f} m, "
            f"in {len(windows):,} window(s) of at most "
            f"{windows[0].width:,} x {windows[0].height:,}", flush=True)
        if tracks:
            log("      best look wins", flush=True)
            for index in range(len(srcs)):
                if index not in tracks:
                    log(f"      [{index+1}/{len(srcs)}] no transect {index} in the "
                        f"metadata - scoring it flat")
        else:
            log("      nothing to judge the passes by - first file wins", flush=True)

        # 0 is nodata in these mosaics, coming in and going out.
        vrts = [WarpedVRT(s, crs=crs, transform=transform, width=width,
                          height=height, resampling=Resampling.nearest,
                          src_nodata=0, nodata=0) for s in srcs]

        won = np.zeros(len(srcs), dtype=np.int64)
        pulse = Progress(len(windows), "merge windows")
        with rasterio.open(out_tif, "w", driver="GTiff", height=height, width=width,
                           count=1, dtype="uint8", crs=crs, transform=transform,
                           compress="lzw", tiled=True,
                           blockxsize=SONAR_MERGE_BLOCK, blockysize=SONAR_MERGE_BLOCK,
                           BIGTIFF="IF_SAFER", sparse_ok=True) as dst:
            for window in windows:
                top, left = int(window.row_off), int(window.col_off)
                # The running best value, the score that won it, and which
                # pass it came from - for this window only.
                # (Blending needed the first two as a weighted sum and its
                # weight, with best as float32.)
                best = np.zeros((int(window.height), int(window.width)), dtype="uint8")
                best_q = np.zeros(best.shape, dtype="float32")
                who = np.zeros(best.shape, dtype="uint16")
                xs = west + (np.arange(left, left + best.shape[1]) + 0.5) * res_x
                ys = north - (np.arange(top, top + best.shape[0]) + 0.5) * res_y

                for index, (vrt, b) in enumerate(zip(vrts, bounds)):
                    # Only the part of the window this pass reaches: a narrow
                    # pass crossing a wide window costs its own area, not the
                    # window's.
                    c0 = max(left, int(math.floor((b[0] - west) / res_x)))
                    c1 = min(left + best.shape[1], int(math.ceil((b[2] - west) / res_x)))
                    r0 = max(top, int(math.floor((north - b[3]) / res_y)))
                    r1 = min(top + best.shape[0], int(math.ceil((north - b[1]) / res_y)))
                    if c0 >= c1 or r0 >= r1:
                        continue
                    data = vrt.read(1, window=Window(c0, r0, c1 - c0, r1 - r0))
                    valid = data > 0
                    if not valid.any():
                        continue
                    rows = slice(r0 - top, r1 - top)
                    cols = slice(c0 - left, c1 - left)

                    track = tracks.get(index)
                    if track is None:
                        q = np.full(data.shape, 0.5, dtype="float32")
                    else:
                        q = quality_for(track, xs[cols], ys[rows], cell)

                    view_v = best[rows, cols]
                    view_q = best_q[rows, cols]
                    view_w = who[rows, cols]
                    # The blended alternative, kept for whoever registers the
                    # passes properly and makes averaging worth doing:
                    #
                    #   w = np.where(valid, q ** SONAR_BLEND_POWER, 0.0).astype("float32")
                    #   view_v += w * data
                    #   view_q += w
                    wins = valid & (q > view_q)
                    view_v[wins] = data[wins]
                    view_q[wins] = q[wins]
                    view_w[wins] = index

                # Blending divided the weighted sum by its weight here. 0 is
                # the nodata value, so an uncovered pixel had to stay 0 and a
                # covered one had to stay off it - rounding up into 1 rather
                # than down to nothing, or the faintest real backscatter reads
                # as no survey at all:
                #
                #   with np.errstate(invalid="ignore", divide="ignore"):
                #       best = np.where(best_q > 0, best / np.maximum(best_q, 1e-6), 0.0)
                #   best = np.where(best_q > 0, np.maximum(np.rint(best), 1), 0).astype("uint8")

                covered = best_q > 0
                # A window no pass reaches is left unwritten; the file is
                # sparse and reads it back as 0.
                if covered.any():
                    dst.write(best, 1, window=window)
                    won += np.bincount(who[covered], minlength=len(srcs))
                pulse.step()
        pulse.finish()
    finally:
        for v in vrts:
            v.close()
        for s in srcs:
            s.close()

    for index, path in enumerate(tif_paths):
        log(f"      [{index+1}/{len(srcs)}] {os.path.basename(path)}: "
            f"{int(won[index]):,} px kept")
    how = "quality" if tracks else "file order"
    log(f"      {int(won.sum()):,} px carry data, decided by {how}")
    log(f"      Saved: {out_tif}  ({os.path.getsize(out_tif) / 1e6:,.0f} MB)")


def find_sonar_meta(tif_paths: list) -> str:
    """
    PINGMapper's ping metadata for these mosaics, if it is where it normally
    sits: <project>/meta/ beside <project>/sonar_mosaic/.

    Looked up rather than asked for, so an ordinary run gets quality-based
    merging without anyone having to know a flag exists.
    """
    if not tif_paths:
        return ""
    project = os.path.dirname(os.path.dirname(os.path.abspath(tif_paths[0])))
    for name in ("B002_ss_port_meta.csv", "B003_ss_star_meta.csv"):
        candidate = os.path.join(project, "meta", name)
        if os.path.isfile(candidate):
            return candidate
    return ""


def merge_sonar(tif_paths: list, out_tif: str, meta_csv: str = ""):
    print("\n[2/3] Merging sonar mosaic tiles...")
    for p in tif_paths:
        if not os.path.exists(p):
            raise FileNotFoundError(f"Missing: {p}")

    tif_paths = usable_mosaics(tif_paths)
    total_mb = sum(os.path.getsize(p) for p in tif_paths) / 1e6
    print(f"      {len(tif_paths)} mosaic(s), {total_mb:,.0f} MB on disk", flush=True)

    # Overlapping passes: keep the better look, not the earlier file. This needs
    # the ping metadata to know where each pass ran; with no way to judge
    # quality, the merge falls back to first-file-wins - through the same
    # windowed merge, so a big survey without metadata does not run out of
    # memory either.
    tracks = {}
    if len(tif_paths) > 1:
        meta_csv = meta_csv or find_sonar_meta(tif_paths)
        if meta_csv:
            print(f"      sonar quality: nadir {SONAR_NADIR_PER_DEPTH:g} x depth, "
                  f"best to {SONAR_PLATEAU_END:g} of range, {SONAR_FAR_FLOOR:g} at "
                  f"the edge, depth smoothed over {SONAR_DEPTH_WINDOW_PINGS} pings")
            try:
                found = transect_tracks(meta_csv)
            except (KeyError, OSError, ValueError) as exc:
                print(f"      ({exc} - falling back to first-file-wins)")
            else:
                if len(found) >= len(tif_paths):
                    tracks = found
                else:
                    print(f"      ({len(found)} transects for {len(tif_paths)} mosaics "
                          f"- cannot match them up, falling back to first-file-wins)")
        else:
            print("      (no ping metadata found - merging first-file-wins)")

    merge_sonar_by_quality(tif_paths, out_tif, tracks)


# ── Step 2b: Sonar MBTiles with correct per-tile alpha ────────────────────────

def build_sonar_mbtiles(tif_path: str, out_mbtiles: str):
    """
    Tile the merged sonar grayscale TIF into MBTiles with correct transparency.

    Problem with the naive approach (raster_to_mbtiles on a pre-built RGBA TIF):
      Resampling.average on the alpha channel mixes transparent (water) and
      opaque (backscatter) pixels into a uniform mid-alpha wash at low zoom,
      blocking the satellite background everywhere.

    Fix: reproject ONLY the grayscale intensity, then recompute the graduated
    alpha from that intensity at each tile. Result: water areas stay fully
    transparent at every zoom level; only genuine backscatter is opaque.

    Resampling strategy:
      * Native zooms (z >= SONAR_NATIVE_ZOOM): the source 5 cm pixels are NOT
        collapsed — at z22 a tile pixel is ~3.3 cm, finer than the 5 cm source,
        so nearest-neighbour preserves the exact source pixel values.
      * Overview zooms (z < SONAR_NATIVE_ZOOM): must downsample to fit; Lanczos
        keeps edges/contrast sharp and an unsharp mask crisps the result.

    Zoom range: ZOOM_MIN–22. At z22 (3.3 cm/tile-px) the 5 cm source is fully
    resolved with zero collapse. NOTE: this makes the sonar mbtiles ~100 MB.
    """
    SONAR_ZOOM_MAX = 22     # z22 = 3.3 cm/tile-px → 5 cm source fully preserved
    SONAR_NATIVE_ZOOM = 20  # at/above this, use nearest (no pixel collapse)
    print(f"\n[2b] Building sonar MBTiles from {os.path.basename(tif_path)}...")

    WEB_MERCATOR = CRS.from_epsg(3857)
    with rasterio.open(tif_path) as src:
        from rasterio.warp import transform_bounds
        wgs84_bounds = transform_bounds(src.crs, CRS.from_epsg(4326), *src.bounds)
        west, south, east, north = wgs84_bounds

        # Those zoom constants suit a 5 cm Humminbird mosaic over a harbour.
        # A hydrographic backscatter mosaic is metres per pixel across tens of
        # kilometres, and tiling that to z22 asked for 12.7 million tiles -
        # hours of work to invent detail the source never had. Tile only as
        # deep as the source resolves, and no deeper.
        mid_lat = math.radians((south + north) / 2)
        src_res_m = ((east - west) * 111320.0 * math.cos(mid_lat)) / max(src.width, 1)
        if src_res_m > 0:
            resolved = math.log2(156543.03392 * math.cos(mid_lat) / src_res_m)
            capped = max(ZOOM_MIN, min(SONAR_ZOOM_MAX, int(math.ceil(resolved))))
            if capped < SONAR_ZOOM_MAX:
                print(f"      source is {src_res_m:.2f} m/px - tiling to z{capped}, "
                      f"not z{SONAR_ZOOM_MAX}")
                SONAR_NATIVE_ZOOM = min(SONAR_NATIVE_ZOOM, capped)
                SONAR_ZOOM_MAX = capped

    if os.path.exists(out_mbtiles):
        os.remove(out_mbtiles)

    con = sqlite3.connect(out_mbtiles)
    init_mbtiles(con, "sonar", (west, south, east, north))
    # Override zoom metadata for sonar
    con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", ("maxzoom", str(SONAR_ZOOM_MAX)))

    total_tiles = sum(
        1 for _ in mercantile.tiles(west, south, east, north,
                                    zooms=list(range(ZOOM_MIN, SONAR_ZOOM_MAX + 1)))
    )
    print(f"      Generating {total_tiles:,} tiles at zoom {ZOOM_MIN}–{SONAR_ZOOM_MAX}...")
    pulse = Progress(total_tiles, "sonar tiles")

    processed = 0
    with rasterio.open(tif_path) as src:
        for tile in mercantile.tiles(west, south, east, north,
                                     zooms=list(range(ZOOM_MIN, SONAR_ZOOM_MAX + 1))):
            xy = mercantile.xy_bounds(tile)
            dst_transform = from_bounds(
                xy.left, xy.bottom, xy.right, xy.top, TILE_SIZE, TILE_SIZE)

            # Native zooms: nearest-neighbour preserves exact source pixels (no
            # collapse). Overview zooms: Lanczos for sharp downsampling.
            native = tile.z >= SONAR_NATIVE_ZOOM
            resampling = Resampling.nearest if native else Resampling.lanczos
            gray_dst = np.zeros((1, TILE_SIZE, TILE_SIZE), dtype=np.float32)
            reproject(
                source=rasterio.band(src, 1),
                destination=gray_dst,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dst_transform,
                dst_crs=WEB_MERCATOR,
                resampling=resampling,
            )

            raw = gray_dst[0]   # raw intensity (float)

            # Skip tiles with no sonar data (all zeros = no source pixels).
            if raw.max() == 0:
                continue

            # Alpha from the RAW averaged intensity: only near-zero (no return /
            # off-survey) fades out. All real backscatter, including dark shadows,
            # stays opaque — this is what keeps the imagery from washing out.
            alpha = np.clip(
                (raw - SONAR_ALPHA_LO) /
                (SONAR_ALPHA_HI - SONAR_ALPHA_LO) * 255.0,
                0, 255
            ).astype(np.uint8)

            # Skip tiles that are entirely transparent.
            if alpha.max() == 0:
                continue

            # Contrast stretch the display intensity to full black/white range,
            # matching the source's auto-stretched appearance.
            disp = np.clip(
                (raw - SONAR_STRETCH_LO) /
                (SONAR_STRETCH_HI - SONAR_STRETCH_LO) * 255.0,
                0, 255
            ).astype(np.uint8)

            # Unsharp mask only on the downsampled overview zooms, to crisp up
            # features that survive downsampling. Native zooms are left untouched
            # so the true 5 cm source pixels are preserved exactly.
            if not native:
                disp_img = Image.fromarray(disp, mode="L").filter(
                    ImageFilter.UnsharpMask(radius=1.5, percent=120, threshold=2))
                disp = np.asarray(disp_img)

            rgba = np.stack([disp, disp, disp, alpha], axis=-1)
            img = Image.fromarray(rgba, mode="RGBA")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png_bytes = buf.getvalue()

            tms_y = (1 << tile.z) - 1 - tile.y
            con.execute("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)",
                        (tile.z, tile.x, tms_y, png_bytes))

            processed += 1
            pulse.step()
            if processed % 500 == 0:
                con.commit()

    pulse.finish()
    con.commit()
    tile_count = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    con.close()
    size_mb = os.path.getsize(out_mbtiles) / 1_048_576
    print(f"      Saved: {out_mbtiles}  ({tile_count:,} tiles, {size_mb:.1f} MB)")


# ── Step 3: Substrate overlay ─────────────────────────────────────────────────

def build_substrate_mbtiles(tif_path: str, out_mbtiles: str, layer_name: str = "substrate"):
    """
    Tile the substrate classification raster into MBTiles.

    The TIF is a single-band classified raster (values 0–7) with an embedded
    RGBA colormap.  Class 0 is the water/background and already has alpha=0 in
    the colormap — all other classes are opaque substrate types.  We apply the
    colormap directly so transparency is correct by construction.
    """
    print(f"\n[3b] Building substrate MBTiles from {os.path.basename(tif_path)}...")
    if not os.path.exists(tif_path):
        print(f"      WARNING: {tif_path} not found — skipping substrate layer.")
        return

    WEB_MERCATOR = CRS.from_epsg(3857)

    with rasterio.open(tif_path) as src:
        from rasterio.warp import transform_bounds
        wgs84_bounds = transform_bounds(src.crs, CRS.from_epsg(4326), *src.bounds)
        west, south, east, north = wgs84_bounds
        # Build a lookup table: class value → (R, G, B, A) uint8 tuple
        try:
            cmap = src.colormap(1)   # dict: class int → (R, G, B, A)
        except Exception:
            cmap = {}
        # LUT for values 0–255
        lut = np.zeros((256, 4), dtype=np.uint8)
        for k, rgba in cmap.items():
            if 0 <= k < 256:
                lut[k] = rgba

    if os.path.exists(out_mbtiles):
        os.remove(out_mbtiles)

    con = sqlite3.connect(out_mbtiles)
    init_mbtiles(con, layer_name, (west, south, east, north))

    total_tiles = sum(
        1 for _ in mercantile.tiles(west, south, east, north,
                                    zooms=list(range(ZOOM_MIN, ZOOM_MAX + 1)))
    )
    print(f"      Generating {total_tiles:,} tiles at zoom {ZOOM_MIN}–{ZOOM_MAX}...")
    pulse = Progress(total_tiles, "tiles")

    processed = 0
    with rasterio.open(tif_path) as src:
        for tile in mercantile.tiles(west, south, east, north,
                                     zooms=list(range(ZOOM_MIN, ZOOM_MAX + 1))):
            xy = mercantile.xy_bounds(tile)
            dst_transform = from_bounds(
                xy.left, xy.bottom, xy.right, xy.top, TILE_SIZE, TILE_SIZE)

            # Reproject single-band class raster (nearest = correct for classes)
            dst_data = np.zeros((1, TILE_SIZE, TILE_SIZE), dtype=np.uint8)
            reproject(
                source=rasterio.band(src, 1),
                destination=dst_data,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dst_transform,
                dst_crs=WEB_MERCATOR,
                resampling=Resampling.nearest,
            )

            classes = dst_data[0]   # (TILE_SIZE, TILE_SIZE) uint8

            # Apply colormap LUT: index each pixel into the RGBA table
            rgba = lut[classes]     # (TILE_SIZE, TILE_SIZE, 4) uint8

            # Skip tiles that are entirely transparent (all background)
            if rgba[:, :, 3].max() == 0:
                continue

            img = Image.fromarray(rgba, mode="RGBA")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png_bytes = buf.getvalue()

            tms_y = (1 << tile.z) - 1 - tile.y
            con.execute("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)",
                        (tile.z, tile.x, tms_y, png_bytes))

            processed += 1
            if processed % 200 == 0:
                con.commit()
                print(f"    {processed:,}/{total_tiles:,} tiles written...")

    pulse.finish()
    con.commit()
    tile_count = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    con.close()
    size_mb = os.path.getsize(out_mbtiles) / 1_048_576
    print(f"      Saved: {out_mbtiles}  ({tile_count:,} tiles, {size_mb:.1f} MB)")


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Turn a survey (depth CSV + sonar mosaic + substrate raster) into "
                    "the MBTiles/GeoJSON/grid files the app bundles. With no arguments "
                    "it processes the Santa Rosalia survey in resources/.")
    p.add_argument("--csv", default=CSV_PATH,
                   help="depth soundings CSV (needs lon, lat, dep_m, date, time)")
    p.add_argument("--sonar", nargs="*", default=SONAR_TILES,
                   help="sonar mosaic GeoTIFF(s); pass none to skip the sonar layer")
    p.add_argument("--sonar-best-only", action="store_true",
                   help="accepted and ignored: showing the single best look "
                        "where passes overlap is now the only behaviour. "
                        "Blending was measured to flatten contrast rather "
                        "than shed speckle and has been switched off")
    p.add_argument("--sonar-meta", default="",
                   help="PINGMapper ping metadata CSV (…/meta/B002_ss_port_meta.csv). "
                        "Lets overlapping mosaics be merged by which pass imaged the "
                        "ground better rather than by file order; found automatically "
                        "when the mosaics sit in a PINGMapper project")
    p.add_argument("--sonar-nadir-per-depth", type=float, default=SONAR_NADIR_PER_DEPTH,
                   help="width of the poor strip under the boat, as a multiple of "
                        f"the depth at each ping (default {SONAR_NADIR_PER_DEPTH})")
    p.add_argument("--sonar-plateau-end", type=float, default=SONAR_PLATEAU_END,
                   help="where the best part of the swath ends, as a fraction of "
                        f"the range (default {SONAR_PLATEAU_END})")
    p.add_argument("--sonar-far-floor", type=float, default=SONAR_FAR_FLOOR,
                   help="what a pixel at max range scores, 0-1 "
                        f"(default {SONAR_FAR_FLOOR})")
    p.add_argument("--sonar-depth-window", type=int, default=SONAR_DEPTH_WINDOW_PINGS,
                   help="pings in the running median that smooths depth before it "
                        f"sets the nadir width (default {SONAR_DEPTH_WINDOW_PINGS})")
    p.add_argument("--substrate", default=SUBSTRATE_TIF,
                   help="substrate raster GeoTIFF; pass an empty string to skip it")
    p.add_argument("--no-tide-reduction", action="store_true",
                   help="the CSV is already reduced to a chart datum (e.g. from a "
                        "BAG); skip the harmonic tide correction")
    p.add_argument("--max-grid-cells", type=int, default=12000,
                   help="widest the interpolation grid may be, in cells")
    p.add_argument("--contour-smooth", type=float, default=0.0,
                   help="blur the grid over this many cells before tracing "
                        "contours; raw soundings need it, a gridded product "
                        "usually does not")
    p.add_argument("--no-track", action="store_true",
                   help="do not draw a trackline. A gridded source (a BAG) has "
                        "no track: its rows are grid cells in raster order, and "
                        "joining them draws lines the vessel never sailed")
    p.add_argument("--coverage-radius", type=float, default=0.0,
                   help="blank grid cells further than this many metres from a "
                        f"sounding (0 = derive, never below "
                        f"{MIN_COVERAGE_RADIUS_M:.0f} m / 200 ft; "
                        "-1 = no mask, interpolate across gaps)")
    p.add_argument("--rock", default="",
                   help="RockMapper habitat raster GeoTIFF (see rock_map.py); "
                        "adds the rock overlay layer")
    p.add_argument("--out-dir", default=OUT_DIR, help="where to write the outputs")
    p.add_argument("--zoom-min", type=zoom_min_arg, default=ZOOM_MIN,
                   help="first tile zoom, or 'lowest' for the furthest-out zoom "
                        "at which the survey is still visible "
                        f"({LOWEST_ZOOM_MIN_PX} px across)")
    p.add_argument("--zoom-max", type=zoom_max_arg, default=ZOOM_MAX,
                   help="last tile zoom, or 'highest' to tile each layer as "
                        "deep as its own resolution reaches")
    p.add_argument("--timezone", default=SURVEY_TIMEZONE,
                   help="timezone the CSV timestamps are in (IANA name)")
    p.add_argument("--contour-interval-ft", type=int, default=CONTOUR_INTERVAL_FT,
                   help="feet between baked contour lines, counted from 0 ft")
    p.add_argument("--vectors-only", action="store_true",
                   help="rebuild only contours/shallow bands/depth grid, skipping "
                        "the slow MBTiles tiling")
    p.add_argument("--tide", choices=["none", "guaymas"], default="none",
                   help="tide model used to reduce soundings to chart datum. "
                        "Default 'none': depths are baked exactly as surveyed, "
                        "which is what inland water wants. 'guaymas' applies the "
                        "Gulf of California constants (the Santa Rosalía survey).")
    p.add_argument("--no-tide", action="store_true",
                   help="alias for --tide none")
    return p.parse_args(argv)


def zoom_min_arg(value: str):
    """A zoom number, or 'lowest' to have it worked out from the survey."""
    if value.strip().lower() == "lowest":
        return "lowest"
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a zoom level or 'lowest': {value!r}")


def zoom_max_arg(value: str):
    """A zoom number, or 'highest' to take each layer to its own resolution."""
    if value.strip().lower() == "highest":
        return "highest"
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a zoom level or 'highest': {value!r}")


def native_zoom(tif_path: str) -> int:
    """
    The zoom at which a tile pixel is about as fine as the raster's own.

    Deeper than this, tiling only enlarges what is already there; the app
    overzooms the last level by itself, for free. Measured the same way the
    sonar tiler caps its depth, and bounded to [ZOOM_MIN, HIGHEST_ZOOM_CAP].
    """
    from rasterio.warp import transform_bounds
    with rasterio.open(tif_path) as src:
        west, south, east, north = transform_bounds(src.crs, CRS.from_epsg(4326),
                                                    *src.bounds)
        width = max(src.width, 1)
    mid_lat = math.radians((south + north) / 2)
    res_m = (east - west) * 111320.0 * math.cos(mid_lat) / width
    if res_m <= 0:
        return HIGHEST_ZOOM_CAP
    exact = math.log2(156543.03392 * math.cos(mid_lat) / res_m)
    zoom = math.ceil(exact - HIGHEST_ZOOM_SLACK)
    return int(max(ZOOM_MIN, min(HIGHEST_ZOOM_CAP, zoom)))


def lowest_zoom(rasters: list, zoom_max: int) -> int:
    """
    The furthest-out zoom at which the survey still shows.

    Every layer's extent is pooled, since the sonar swath reaches past the
    soundings, and the survey's larger side in web mercator is found as a
    fraction of the world. A world tile is 256 px, so at zoom z that side is
    fraction * 256 * 2**z pixels; the answer is the first z at which that
    reaches LOWEST_ZOOM_MIN_PX. Never above zoom_max: a survey too small to
    show even there still gets its tiles.
    """
    from rasterio.warp import transform_bounds
    west = south = math.inf
    east = north = -math.inf
    for path in rasters:
        if not path or not os.path.isfile(path):
            continue
        with rasterio.open(path) as src:
            w, s, e, n = transform_bounds(src.crs, CRS.from_epsg(4326), *src.bounds)
        west, south = min(west, w), min(south, s)
        east, north = max(east, e), max(north, n)
    if not math.isfinite(west):
        return min(12, zoom_max)

    def merc_y(lat):
        lat = max(-85.0511, min(85.0511, lat))
        return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) / (2 * math.pi)

    fraction = max((east - west) / 360.0, merc_y(north) - merc_y(south), 1e-12)
    zoom = math.ceil(math.log2(LOWEST_ZOOM_MIN_PX / (fraction * TILE_SIZE)))
    return int(max(0, min(zoom_max, zoom)))


def main(argv=None):
    global OUT_DIR, ZOOM_MIN, ZOOM_MAX, SURVEY_TIMEZONE, CONTOUR_INTERVAL_FT, TIDE_CONSTITUENTS
    global SONAR_NADIR_PER_DEPTH, SONAR_PLATEAU_END, SONAR_FAR_FLOOR, SONAR_DEPTH_WINDOW_PINGS
    args = parse_args(argv)
    # Checked here rather than left to fail inside the merge, where a plateau
    # past the edge of range shows up as nothing worse than a strange chart.
    for ok, what in ((args.sonar_nadir_per_depth >= 0, "--sonar-nadir-per-depth must be 0 or more"),
                     (0 < args.sonar_plateau_end < 1, "--sonar-plateau-end must be between 0 and 1"),
                     (0 <= args.sonar_far_floor <= 1, "--sonar-far-floor must be from 0 to 1"),
                     (args.sonar_depth_window >= 1, "--sonar-depth-window must be 1 or more")):
        if not ok:
            raise SystemExit(what)
    SONAR_NADIR_PER_DEPTH = args.sonar_nadir_per_depth
    SONAR_PLATEAU_END = args.sonar_plateau_end
    SONAR_FAR_FLOOR = args.sonar_far_floor
    SONAR_DEPTH_WINDOW_PINGS = args.sonar_depth_window
    OUT_DIR = args.out_dir
    ZOOM_MIN, ZOOM_MAX = args.zoom_min, args.zoom_max
    SURVEY_TIMEZONE = args.timezone
    CONTOUR_INTERVAL_FT = args.contour_interval_ft
    if args.no_tide or args.tide == "none":
        # No constituents -> tide height is 0 everywhere, so depth_LAT = measured.
        TIDE_CONSTITUENTS = []
        print("No tide model: depths are baked exactly as surveyed. "
              "Pass --tide guaymas for the Gulf of California survey.")

    ensure_dir(OUT_DIR)

    bathy_tif        = os.path.join(OUT_DIR, "bathymetry.tif")
    sonar_tif        = os.path.join(OUT_DIR, "sonar_merged.tif")
    bathy_mbtiles    = os.path.join(OUT_DIR, "bathymetry.mbtiles")
    sonar_mbtiles    = os.path.join(OUT_DIR, "sonar.mbtiles")
    substrate_mbtiles = os.path.join(OUT_DIR, "substrate.mbtiles")
    rock_mbtiles     = os.path.join(OUT_DIR, "rock.mbtiles")

    sonar_tiles = [t for t in (args.sonar or []) if t]
    substrate_tif = args.substrate or ""
    rock_tif = args.rock or ""

    build_bathymetry_raster(args.csv, bathy_tif,
                            reduce_to_datum=not args.no_tide_reduction,
                            max_grid_cells=args.max_grid_cells,
                            coverage_radius_m=args.coverage_radius,
                            with_track=not args.no_track,
                            contour_smooth_cells=args.contour_smooth)

    if args.vectors_only:
        print("\nVectors only: contours, shallow bands, depth grid and legend "
              "rebuilt; MBTiles left as they are.")
        if substrate_tif:
            build_substrate_grid(substrate_tif,
                                 os.path.join(OUT_DIR, "substrate_grid.bin"),
                                 os.path.join(OUT_DIR, "substrate_grid.json"))
        return

    if sonar_tiles:
        merge_sonar(sonar_tiles, sonar_tif, meta_csv=args.sonar_meta)

    highest = ZOOM_MAX == "highest"
    if ZOOM_MIN == "lowest":
        ZOOM_MIN = lowest_zoom([bathy_tif, sonar_tif if sonar_tiles else "",
                                substrate_tif, rock_tif],
                               HIGHEST_ZOOM_CAP if highest else ZOOM_MAX)
        print(f"\nLowest tile zoom: z{ZOOM_MIN} - the survey is at least "
              f"{LOWEST_ZOOM_MIN_PX} px across from there in")

    def set_zoom_max(tif, name):
        """With 'highest', each layer goes as deep as its own pixels reach."""
        global ZOOM_MAX
        if highest:
            ZOOM_MAX = native_zoom(tif)
            print(f"  {name}: tiling to z{ZOOM_MAX}, its own resolution")

    print("\n[3/3] Generating MBTiles...")
    set_zoom_max(bathy_tif, "depth")
    raster_to_mbtiles(bathy_tif,  bathy_mbtiles, "bathymetry")
    if sonar_tiles:
        build_sonar_mbtiles(sonar_tif, sonar_mbtiles)
    else:
        print("  (no sonar mosaic given - skipping sonar layer)")
    if substrate_tif:
        set_zoom_max(substrate_tif, "substrate")
        build_substrate_mbtiles(substrate_tif, substrate_mbtiles)
        build_substrate_legend(os.path.join(OUT_DIR, "substrate_legend.png"))
        build_substrate_grid(substrate_tif,
                             os.path.join(OUT_DIR, "substrate_grid.bin"),
                             os.path.join(OUT_DIR, "substrate_grid.json"))
    else:
        print("  (no substrate raster given - skipping substrate layer)")

    if rock_tif:
        set_zoom_max(rock_tif, "rock")
        build_rock_mbtiles(rock_tif, rock_mbtiles)
        build_rock_legend(os.path.join(OUT_DIR, "rock_legend.png"), rock_tif)
    else:
        print("  (no rock raster given - skipping rock layer)")

    print("\nDone. Pipeline complete.")
    print(f"  -> {bathy_mbtiles}")
    if sonar_tiles:
        print(f"  -> {sonar_mbtiles}")
    if substrate_tif:
        print(f"  -> {substrate_mbtiles}")
    if rock_tif:
        print(f"  -> {rock_mbtiles}")
    print("\nAdd the survey in AnchorHold Web Viewer: Settings, then Charts on")
    print("this computer, then Add. Add Survey Locations runs this and the rest.")


if __name__ == "__main__":
    main()
