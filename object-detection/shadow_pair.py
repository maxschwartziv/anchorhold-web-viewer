"""
shadow_pair.py — classical highlight/shadow pairing detector for side-scan sonar.

Operates on UNRECTIFIED waterfall data: a 2-D array of shape (n_pings, n_samples)
for a single channel (port or starboard), where sample index increases with slant
range away from nadir. Do not run this on a slant-range-corrected mosaic; the
resampling smears the shadow edge that carries most of the information.

Pipeline
    1. empirical gain normalisation (removes beam pattern + TVG residual)
    2. nadir / altitude tracking (or use logged depth)
    3. threshold for highlights and shadows separately
    4. pair each highlight with a shadow lying immediately outboard of it
    5. estimate object height from shadow length, score, filter
    6. optionally convert (ping, sample) -> lat/lon

No training data required. Intended as a baseline to measure a CNN against.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional, Sequence

import numpy as np
from scipy.ndimage import median_filter, uniform_filter, uniform_filter1d
from skimage.measure import label, regionprops
from skimage.morphology import closing, opening


# --------------------------------------------------------------------------- #
# 1. gain normalisation
# --------------------------------------------------------------------------- #

def egn_normalize(wf: np.ndarray, ping_block: int = 400, eps: float = 1e-6) -> np.ndarray:
    """Empirical gain normalisation.

    Divides each sample column by a slowly-varying along-track median of that
    column, so a flat bottom sits at ~1.0 regardless of range, gain settings or
    substrate brightness. Returns a float array in units of "times the local
    background".

    ping_block: along-track window (in pings) over which the reference curve is
    estimated. Long enough to average out targets, short enough to follow real
    changes in bottom type / altitude. 300-600 is usually right.
    """
    wf = np.asarray(wf, dtype=np.float32)
    n_pings, n_samples = wf.shape

    # block-wise median is much cheaper than a true rolling median and is
    # indistinguishable in practice once interpolated.
    edges = np.arange(0, n_pings, ping_block)
    if len(edges) < 2:
        ref_blocks = np.median(wf, axis=0)[None, :]
        centres = np.array([n_pings / 2.0])
    else:
        ref_blocks = np.stack(
            [np.median(wf[a:a + ping_block], axis=0) for a in edges]
        )
        centres = edges + ping_block / 2.0

    # smooth each reference curve across range to kill sample-to-sample noise
    ref_blocks = uniform_filter1d(ref_blocks, size=15, axis=1, mode="nearest")

    if len(centres) == 1:
        ref = np.repeat(ref_blocks, n_pings, axis=0)
    else:
        ping_idx = np.arange(n_pings, dtype=np.float32)
        ref = np.empty_like(wf)
        for s in range(n_samples):
            ref[:, s] = np.interp(ping_idx, centres, ref_blocks[:, s])

    return wf / np.maximum(ref, eps)


# --------------------------------------------------------------------------- #
# 2. altitude / nadir
# --------------------------------------------------------------------------- #

def track_nadir(
    wf: np.ndarray,
    range_per_sample: float,
    min_sample: int = 5,
    frac: float = 0.35,
    smooth_pings: int = 31,
) -> np.ndarray:
    """First-bottom-return sample index per ping.

    Crude but adequate when no logged depth is available: walk out from the
    transducer and take the first sample exceeding `frac` of that ping's peak.
    If you have per-ping depth from the recording (Humminbird logs it), use
    that instead -- it is far more reliable in weed, thermoclines and wakes.
    """
    wf = np.asarray(wf, dtype=np.float32)
    sm = uniform_filter1d(wf, size=5, axis=1, mode="nearest")
    peak = sm.max(axis=1, keepdims=True)
    hot = sm > (frac * np.maximum(peak, 1e-6))
    hot[:, :min_sample] = False
    idx = np.argmax(hot, axis=1).astype(np.float32)
    idx[~hot.any(axis=1)] = min_sample
    if smooth_pings > 1:
        idx = median_filter(idx, size=smooth_pings, mode="nearest")
    return idx


def altitude_from_nadir(nadir_idx: np.ndarray, range_per_sample: float) -> np.ndarray:
    """Transducer height above bottom, metres, one value per ping."""
    return nadir_idx * range_per_sample


# --------------------------------------------------------------------------- #
# 3-5. detection
# --------------------------------------------------------------------------- #

@dataclass
class Detection:
    ping: float             # along-track centre (ping index)
    sample: float           # across-track centre of the highlight (sample index)
    ping_min: int
    ping_max: int
    hl_sample_min: int
    hl_sample_max: int
    sh_sample_min: int
    sh_sample_max: int
    ground_range_m: float   # to the highlight
    shadow_len_m: float
    height_m: float         # estimated object height above bottom
    length_m: float         # along-track extent of the highlight
    width_m: float          # across-track extent of the highlight, ground range
    hl_contrast: float      # mean normalised intensity of the highlight
    sh_contrast: float      # mean normalised intensity of the shadow
    overlap: float          # along-track overlap fraction, highlight vs shadow
    score: float

    def as_dict(self):
        return asdict(self)


def _ground_range(sample: np.ndarray | float, range_per_sample: float, altitude: float):
    r = np.asarray(sample, dtype=np.float64) * range_per_sample
    return np.sqrt(np.maximum(r * r - altitude * altitude, 0.0))


def detect(
    wf: np.ndarray,
    range_per_sample: float,
    ping_spacing: float,
    altitude: Optional[np.ndarray | float] = None,
    *,
    hi_thresh: float = 1.7,
    lo_thresh: float = 0.45,
    min_hl_px: int = 12,
    min_sh_px: int = 25,
    nadir_pad: float = 1.15,
    max_range_frac: float = 0.97,
    smooth: tuple = (5, 3),
    gap_max_samples: int = 12,
    overlap_min: float = 0.30,
    height_range: tuple = (0.15, 12.0),
    shadow_len_range: tuple = (0.20, 40.0),
    ping_block: int = 400,
) -> tuple[list[Detection], np.ndarray]:
    """Find highlight/shadow pairs.

    range_per_sample : metres of slant range per sample
    ping_spacing     : metres travelled per ping (speed / ping rate); only used
                       to convert along-track pixel extents to metres
    altitude         : transducer height above bottom in metres, scalar or one
                       value per ping. If None it is estimated from the data.

    Returns (detections, normalised_image).
    """
    wf = np.asarray(wf, dtype=np.float32)
    n_pings, n_samples = wf.shape

    norm = egn_normalize(wf, ping_block=ping_block)
    if smooth is not None:
        # multi-look averaging: speckle in a single ping is Rayleigh-distributed
        # and will trip any fixed threshold. Averaging a few pings x a few
        # samples drops the background variance without blurring the shadow
        # edge appreciably.
        norm = uniform_filter(norm, size=smooth, mode="nearest")

    if altitude is None:
        nadir_idx = track_nadir(wf, range_per_sample)
        alt = altitude_from_nadir(nadir_idx, range_per_sample)
    else:
        alt = np.broadcast_to(np.asarray(altitude, dtype=np.float32), (n_pings,)).copy()
        nadir_idx = alt / range_per_sample

    # ---- mask water column, nadir shoulder, and the far-range fade ---------
    valid = np.ones((n_pings, n_samples), dtype=bool)
    sample_grid = np.arange(n_samples)[None, :]
    valid &= sample_grid >= (nadir_idx[:, None] * nadir_pad)
    valid &= sample_grid <= (n_samples * max_range_frac)

    hi = (norm > hi_thresh) & valid
    lo = (norm < lo_thresh) & valid

    # shadows are along-track-coherent; close small gaps, drop speckle
    hi = opening(hi, np.ones((3, 3), bool))
    lo = closing(lo, np.ones((5, 3), bool))

    hl_props = [p for p in regionprops(label(hi), intensity_image=norm)
                if p.area >= min_hl_px]
    sh_props = [p for p in regionprops(label(lo), intensity_image=norm)
                if p.area >= min_sh_px]

    dets: list[Detection] = []

    for h in hl_props:
        hp0, hs0, hp1, hs1 = h.bbox          # max indices are exclusive
        h_alt = float(np.median(alt[hp0:hp1])) if hp1 > hp0 else float(alt[hp0])
        best = None

        for s in sh_props:
            sp0, ss0, sp1, ss1 = s.bbox

            # the shadow must begin at or just outboard of the highlight
            if ss0 < hs1 - 3 or ss0 > hs1 + gap_max_samples:
                continue

            ov = max(0, min(hp1, sp1) - max(hp0, sp0))
            denom = max(1, min(hp1 - hp0, sp1 - sp0))
            frac = ov / denom
            if frac < overlap_min:
                continue

            g_start = float(_ground_range(ss0, range_per_sample, h_alt))
            g_end = float(_ground_range(ss1, range_per_sample, h_alt))
            shadow_len = g_end - g_start
            if not (shadow_len_range[0] <= shadow_len <= shadow_len_range[1]):
                continue

            # similar triangles, flat bottom:  h = H * Ls / R_end
            height = h_alt * shadow_len / max(g_end, 1e-6)
            if not (height_range[0] <= height <= height_range[1]):
                continue

            hl_c = float(h.intensity_mean)
            sh_c = float(s.intensity_mean)
            score = (
                min(hl_c / hi_thresh, 3.0) * 0.4
                + min(lo_thresh / max(sh_c, 1e-3), 3.0) * 0.3
                + frac * 0.3
            )
            cand = (score, s, frac, shadow_len, height, hl_c, sh_c, g_start)
            if best is None or score > best[0]:
                best = cand

        if best is None:
            continue

        score, s, frac, shadow_len, height, hl_c, sh_c, _ = best
        sp0, ss0, sp1, ss1 = s.bbox
        g_hl_near = float(_ground_range(hs0, range_per_sample, h_alt))
        g_hl_far = float(_ground_range(hs1, range_per_sample, h_alt))

        dets.append(
            Detection(
                ping=float(h.centroid[0]),
                sample=float(h.centroid[1]),
                ping_min=int(hp0),
                ping_max=int(hp1),
                hl_sample_min=int(hs0),
                hl_sample_max=int(hs1),
                sh_sample_min=int(ss0),
                sh_sample_max=int(ss1),
                ground_range_m=0.5 * (g_hl_near + g_hl_far),
                shadow_len_m=float(shadow_len),
                height_m=float(height),
                length_m=float((hp1 - hp0) * ping_spacing),
                width_m=float(g_hl_far - g_hl_near),
                hl_contrast=float(hl_c),
                sh_contrast=float(sh_c),
                overlap=float(frac),
                score=float(score),
            )
        )

    dets.sort(key=lambda d: -d.score)
    return dets, norm


# --------------------------------------------------------------------------- #
# 6. geo-referencing
# --------------------------------------------------------------------------- #

def to_latlon(
    dets: Sequence[Detection],
    lat: np.ndarray,
    lon: np.ndarray,
    heading_deg: np.ndarray,
    side: str,
    range_per_sample: float,
    altitude: np.ndarray | float,
) -> list[dict]:
    """Attach lat/lon to each detection.

    lat/lon/heading_deg are per-ping arrays from the nav record. `side` is
    'port' or 'starboard'. Uses a flat-earth offset, which is exact enough at
    these ranges. Heading is the vehicle track over ground, not compass heading,
    unless you have corrected for crab.
    """
    lat = np.asarray(lat, dtype=np.float64)
    lon = np.asarray(lon, dtype=np.float64)
    hdg = np.deg2rad(np.asarray(heading_deg, dtype=np.float64))
    alt = np.broadcast_to(np.asarray(altitude, dtype=np.float64), lat.shape)

    sign = 1.0 if side.lower().startswith("star") else -1.0
    out = []
    for d in dets:
        i = int(round(d.ping))
        i = min(max(i, 0), len(lat) - 1)
        g = float(_ground_range(d.sample, range_per_sample, float(alt[i])))
        bearing = hdg[i] + sign * (np.pi / 2.0)
        dn = g * np.cos(bearing)
        de = g * np.sin(bearing)
        dlat = dn / 111_320.0
        dlon = de / (111_320.0 * max(np.cos(np.deg2rad(lat[i])), 1e-6))
        rec = d.as_dict()
        rec["lat"] = float(lat[i] + dlat)
        rec["lon"] = float(lon[i] + dlon)
        rec["side"] = side
        out.append(rec)
    return out


def to_geojson(records: Sequence[dict]) -> dict:
    """FeatureCollection of point targets. Write with json.dump, or read with
    geopandas.read_file for shapefile export / waypoint conversion."""
    feats = []
    for r in records:
        props = {k: v for k, v in r.items() if k not in ("lat", "lon")}
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": props,
            }
        )
    return {"type": "FeatureCollection", "features": feats}


# --------------------------------------------------------------------------- #
# quick look
# --------------------------------------------------------------------------- #

def overlay(norm: np.ndarray, dets: Sequence[Detection], path: str = "detections.png"):
    """Waterfall with highlight (solid) and shadow (dashed) boxes drawn."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    fig, ax = plt.subplots(figsize=(10, 14))
    ax.imshow(norm, cmap="gray", vmin=0, vmax=2.2, aspect="auto",
              interpolation="nearest")
    for d in dets:
        ax.add_patch(Rectangle(
            (d.hl_sample_min, d.ping_min),
            d.hl_sample_max - d.hl_sample_min, d.ping_max - d.ping_min,
            fill=False, edgecolor="yellow", linewidth=1.0))
        ax.add_patch(Rectangle(
            (d.sh_sample_min, d.ping_min),
            d.sh_sample_max - d.sh_sample_min, d.ping_max - d.ping_min,
            fill=False, edgecolor="cyan", linewidth=0.8, linestyle="--"))
        ax.text(d.hl_sample_min, d.ping_min - 4, f"{d.height_m:.1f}m",
                color="yellow", fontsize=7)
    ax.set_xlabel("sample (slant range)")
    ax.set_ylabel("ping")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path
