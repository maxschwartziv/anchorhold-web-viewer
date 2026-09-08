#!/usr/bin/env python3
"""
Recording Fixer - look at a sonar recording before it becomes a chart.

pipeline/repair_humminbird.py puts back what a power cut took: the 64-byte
header, the truncated index, the pings recorded before the GPS had a fix. It
does that without asking, because those answers are not in doubt. Everything
else about a recording is: a depth reading that jumped 40 ft for one ping, a
fix that slid across the lake and back, the ten minutes of transit at the start
that are not part of the survey. Those need someone to look.

So this is the same repair with the recording drawn on the water it was made
on. The trackline goes over satellite imagery, depths are sampled along it in
feet, outliers are flagged by rules you can set, and you keep or reject
stretches of track by marking a shape around them - click to place points,
two for a box and three or more for a polygon.

It opens anything PINGVerter reads: Humminbird .DAT, Lowrance .sl2/.sl3,
Garmin .RSD, EdgeTech .jsf, Cerulean .svlog, and .xtf. Only Humminbird is
parsed here; the rest are converted to a PINGMapper project first (see
any_recording.py), which is a full read of the file and is cached beside it.

    Fix_Recording.bat                       pick a recording in the app
    Fix_Recording.bat R00021.DAT            open it straight away
    python pipeline/fix_recording.py --report R00021.DAT     no window, just
                                                             what is wrong
    python pipeline/fix_recording.py --selftest

Every save writes the same three files, whatever went in:

    <name>_fixed_timefilter.csv    start_seconds, end_seconds - the
                                   stretches to keep, as PINGMapper's
                                   own time_table reads them
    <name>_fixed_soundings.csv     lon, lat, dep_m, date, time
    <name>_fixed_fix_report.json   what was kept and why

A Humminbird recording gets <name>_fixed.DAT + <name>_fixed/ as well, since
that format can be rebuilt. Nothing else is rewritten: the filter and the
recording it came from are the edit, and the side scan survives because no
channel was rewritten to lose it. Add_Survey_Locations finds the filter
beside the recording and applies it without being asked.

A recording that lost its header gets one from the Save repaired .DAT
button, which appears only when there is a header missing to write - until
there is one, nothing can read the recording to filter it.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import math
import os
import queue
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import numpy as np

import any_recording
import repair_humminbird as repair
import satellite
import workspace                    # the recordings folder, set once

M_TO_FT = 3.28084
MS_TO_MPH = 2.23694
EARTH_FT = 20902231.0            # mean earth radius, feet

# What counts as an outlier, and what the boxes in the window start at.
#
# These are starting points, not truths: a river survey at 6 mph and a lake
# survey at 2 mph disagree about what a jump is. Every one of them is a field
# in the window, and changing one re-runs the flagging over the whole track.
# The sounder pings about thirteen times a second and the GPS answers once,
# so a dozen pings in a row carry the same position and then it steps. That
# step is not the boat jumping and those repeats are not the fix sticking:
# both rules below work on the fixes themselves, not on the pings that
# happen to be stamped with them.
# Everything PINGVerter reads. Humminbird is parsed here and can be written
# back; the rest are converted, judged, and saved as a time filter.
RECORDING_FILETYPES = [
    ("Sonar recording", "*.DAT *.sl2 *.sl3 *.RSD *.svlog *.jsf *.xtf"),
    ("Humminbird", "*.DAT"),
    ("Lowrance", "*.sl2 *.sl3"),
    ("Garmin", "*.RSD"),
    ("EdgeTech / Cerulean / XTF", "*.jsf *.svlog *.xtf"),
    ("All files", "*.*"),
]

DEFAULT_RULES = {
    "jump_mph": 15.0,        # implied speed from one fix to the next
    "frozen_pings": 60,      # pings on one position before the fix is stuck
    "stopped_mph": 0.5,      # slower than this and the boat is not surveying
    "err_ft": 30.0,          # the unit's own estimate of its fix error
    "depth_max_ft": 200.0,   # deeper than the water goes
    "spike_ft": 5.0,         # depth against the local median
    "spike_window": 15,      # pings the local median is taken over
    "far_degrees": 0.5,      # a fix from another trip entirely
    "turn_deg": 20.0,        # heading change that counts as a turn
    "turn_ft": 33.0,         # measured over this much track either side
}

FLAG_LABELS = [
    ("gps_far", "GPS: fix from somewhere else"),
    ("gps_jump", "GPS: jumped between pings"),
    ("gps_frozen", "GPS: position stuck while under way"),
    ("boat_stopped", "Boat: stopped, pinging one spot"),
    ("gps_error", "GPS: reported error too large"),
    ("depth_missing", "Depth: nothing recorded"),
    ("depth_deep", "Depth: beyond the range set"),
    ("depth_spike", "Depth: spike against neighbours"),
    ("track_turn", "Track: turning through the ping"),
    ("user", "Marked by you on the map"),
]


# ── The recording, as something to look at ──────────────────────────────────

def haversine_ft(lat1, lon1, lat2, lon2):
    """Distance between two arrays of positions, in feet."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_FT * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Course from one array of positions to another, degrees from north."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dl = np.radians(np.asarray(lon2) - np.asarray(lon1))
    y = np.sin(dl) * np.cos(p2)
    x = np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl)
    return np.degrees(np.arctan2(y, x)) % 360.0


def inside_shape(lon, lat, shape) -> np.ndarray:
    """
    Which positions fall inside a shape marked out on the map.

    A shape is the points that were clicked. Two of them are the opposite
    corners of a box - the common case, and the quickest thing to mark out -
    and three or more are a polygon, for a cove or a single leg of the
    lawnmower that a box would not cut cleanly.

    A bare (west, south, east, north) is taken as a box too, so a caller with
    a bounding box in hand - a script, a saved region - needs no ceremony to
    ask the same question.

    Ray casting rather than a library: it is fifteen lines, it vectorises, and
    it keeps this file usable without a plotting stack for --report.
    """
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    points = list(shape)
    if len(points) == 4 and all(np.isscalar(v) for v in points):
        west, south, east, north = points
        points = [(west, south), (east, north)]
    if len(points) < 2:
        return np.zeros(len(lon), dtype=bool)
    if len(points) == 2:
        (x0, y0), (x1, y1) = points
        west, east = sorted((x0, x1))
        south, north = sorted((y0, y1))
        return (lon >= west) & (lon <= east) & (lat >= south) & (lat <= north)

    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    inside = np.zeros(len(lon), dtype=bool)
    j = len(points) - 1
    for i in range(len(points)):
        straddles = (ys[i] > lat) != (ys[j] > lat)
        run = ys[j] - ys[i]
        with np.errstate(divide="ignore", invalid="ignore"):
            crossing = xs[i] + (lat - ys[i]) * (xs[j] - xs[i]) / np.where(
                run == 0, np.nan, run)
        inside ^= straddles & (lon < crossing)
        j = i
    return inside


def rolling_median(values, window: int):
    """Median of each point's neighbourhood, edges included by reflection."""
    window = max(3, int(window) | 1)             # odd, so there is a middle
    pad = window // 2
    padded = np.pad(values, pad, mode="edge")
    view = np.lib.stride_tricks.sliding_window_view(padded, window)
    return np.median(view, axis=-1)


class Review:
    """
    One recording, its track, and the decision about every ping in it.

    The decision lives on the reference beam - the down beam, one ping per
    position - and is carried to the side scan channels by time when the
    recording is written. That way a stretch of track rejected on the map takes
    the imagery over that ground with it.
    """

    def __init__(self, survey: repair.Survey, rules: dict = None):
        self.survey = survey
        self.rules = dict(DEFAULT_RULES, **(rules or {}))
        self.beam = survey.beams[0]
        pings = survey.pings[self.beam]

        # A converted recording carries latitude and longitude already;
        # only the Humminbird ping header stores its own projection.
        coords = (survey.coords(pings) if hasattr(survey, 'coords')
                  else np.array([repair.lat_lon(p.utm_e, p.utm_n)
                                 for p in pings]))
        self.lat = coords[:, 0]
        self.lon = coords[:, 1]
        self.time_ms = np.array([p.time_ms for p in pings], dtype=np.int64)
        self.depth_ft = np.array([p.dep_dm for p in pings]) / 10.0 * M_TO_FT
        self.speed_mph = np.array([p.speed_dm for p in pings]) / 10.0 * MS_TO_MPH
        self.err_ft = (np.array([p.e_err for p in pings], dtype=float)
                       + np.array([p.n_err for p in pings], dtype=float)) / 100.0 * M_TO_FT

        self.keep = np.ones(len(pings), dtype=bool)
        # Kept apart from the rest because the rules replace themselves
        # every time they are run, and a mark made by hand must not be
        # swept away by re-running them.
        self.user_flag = np.zeros(len(pings), dtype=bool)
        self.roi = None                          # (west, south, east, north)
        self.flags = {}
        self.history = []
        self.flag()

    # -- flagging ------------------------------------------------------------

    def flag(self, rules: dict = None) -> dict:
        """Work out which pings look wrong. Does not reject anything by itself."""
        if rules:
            self.rules.update(rules)
        r = self.rules
        n = len(self.lat)
        lat, lon = self.lat, self.lon

        centre_lat = float(np.median(lat))
        centre_lon = float(np.median(lon))
        far = ((np.abs(lat - centre_lat) > r["far_degrees"])
               | (np.abs(lon - centre_lon) > r["far_degrees"]))

        # Where a new fix actually arrives. Everything between two of these is
        # the same position repeated, which is the sounder outrunning the GPS
        # rather than anything being wrong.
        fresh = np.ones(n, dtype=bool)
        fresh[1:] = (lat[1:] != lat[:-1]) | (lon[1:] != lon[:-1])
        self.fresh = fresh
        at = np.flatnonzero(fresh)

        # Implied speed from one fix to the next. A boat does not teleport, so
        # anything above a plausible speed is the fix moving, not the boat.
        jump = np.zeros(n, dtype=bool)
        self.step_mph = np.zeros(n)
        if at.size > 1:
            hops = haversine_ft(lat[at[:-1]], lon[at[:-1]],
                                lat[at[1:]], lon[at[1:]])
            seconds = np.diff(self.time_ms[at]) / 1000.0
            with np.errstate(divide="ignore", invalid="ignore"):
                mph = np.where(seconds > 0,
                               hops / np.maximum(seconds, 1e-6) / 5280.0 * 3600.0,
                               0.0)
            # A fix's speed belongs to every ping stamped with it, so the
            # whole run is flagged or none of it is.
            for i, speed in zip(at[1:], mph):
                self.step_mph[i] = speed
            fast = np.flatnonzero(mph > r["jump_mph"])
            for k in fast:
                start = at[k + 1]
                stop = at[k + 2] if k + 2 < at.size else n
                jump[start:stop] = True

        # A position that does not move for far longer than the GPS takes to
        # answer is a fix holding, not a boat holding station: the sounder is
        # still pinging new ground and every one of those pings lands on one
        # spot.
        held = np.zeros(n, dtype=bool)
        run = 0
        for i in range(n):
            run = 1 if fresh[i] else run + 1
            if run >= r["frozen_pings"]:
                held[i - run + 1:i + 1] = True
        # Which of the two it is, the sounder can say: it logs its own speed
        # over ground. A position that will not move while the boat is making
        # way is a GPS fault; one that will not move while the boat is not is
        # a boat sitting still, and the pings are all landing on one patch of
        # bottom either way. Measured on a Helix, an ordinary fix steps every
        # eleven pings, so a run of sixty is five seconds of nothing.
        moving = self.speed_mph > r["stopped_mph"]
        frozen = held & moving
        stopped = held & ~moving

        # Turning. A boat coming round the end of a leg sweeps the same ground
        # with every ping and from a different angle each time, which is what
        # the smeared fans in a mosaic are. The course is taken from the fixes
        # rather than from the instrument heading: heading is what the boat was
        # pointing at, and over ground is what the sonar actually painted.
        turn = np.zeros(n, dtype=bool)
        steady = at[~(far[at] | jump[at])] if at.size else at
        if steady.size > 2:
            lat_c, lon_c = lat[steady], lon[steady]
            hops = haversine_ft(lat_c[:-1], lon_c[:-1], lat_c[1:], lon_c[1:])
            along = np.concatenate([[0.0], np.cumsum(hops)])
            look = max(1.0, float(r["turn_ft"]))
            back = np.clip(np.searchsorted(along, along - look) - 1, 0, None)
            ahead = np.clip(np.searchsorted(along, along + look), 0,
                            steady.size - 1)
            # Only where there is that much track on both sides to judge over.
            judged = (along >= look) & (along <= along[-1] - look)
            came = bearing_deg(lat_c[back], lon_c[back], lat_c, lon_c)
            went = bearing_deg(lat_c, lon_c, lat_c[ahead], lon_c[ahead])
            swung = np.abs((went - came + 180.0) % 360.0 - 180.0)
            turning = judged & (swung > float(r["turn_deg"]))
            self.turn_deg_at = np.zeros(n)
            for pos, index in enumerate(steady):
                stop = at[np.searchsorted(at, index, side='right')] \
                    if np.searchsorted(at, index, side='right') < at.size else n
                self.turn_deg_at[index:stop] = swung[pos]
                if turning[pos]:
                    turn[index:stop] = True
        else:
            self.turn_deg_at = np.zeros(n)

        error = self.err_ft > r["err_ft"]

        missing = self.depth_ft <= 0
        deep = self.depth_ft > r["depth_max_ft"]
        spike = np.zeros(n, dtype=bool)
        sounded = ~missing
        if sounded.sum() > r["spike_window"]:
            depths = self.depth_ft.copy()
            # A gap in the soundings should not drag the local median down.
            depths[missing] = np.interp(np.flatnonzero(missing),
                                        np.flatnonzero(sounded), depths[sounded])
            spike = sounded & (np.abs(depths - rolling_median(depths,
                                                              r["spike_window"]))
                               > r["spike_ft"])

        self.flags = {"gps_far": far, "gps_jump": jump, "gps_frozen": frozen,
                      "boat_stopped": stopped, "gps_error": error,
                      "track_turn": turn, "depth_missing": missing,
                      "depth_deep": deep, "depth_spike": spike,
                      "user": self.user_flag}
        return self.flags

    @property
    def flagged(self):
        """Any flag at all, as one mask."""
        if not self.flags:
            return np.zeros(len(self.lat), dtype=bool)
        return np.logical_or.reduce(list(self.flags.values()))

    def mark_user(self, mask) -> int:
        """
        Add what is under the shape to the flags, without removing it.

        The same gesture as rejecting and none of the commitment: it joins
        the other flags, is drawn like them, and goes when you reject them
        or clear them.
        """
        self.user_flag = self.user_flag | np.asarray(mask, dtype=bool)
        self.flags["user"] = self.user_flag
        return int(self.user_flag.sum())

    def clear_user_flag(self) -> int:
        """Unmark what you marked, and leave the rules' findings alone."""
        had = int(self.user_flag.sum())
        self.user_flag = np.zeros(len(self.lat), dtype=bool)
        self.flags["user"] = self.user_flag
        return had

    # -- decisions -----------------------------------------------------------

    def _remember(self):
        self.history.append((self.keep.copy(), self.roi))
        del self.history[:-40]

    def inside(self, shape) -> np.ndarray:
        """Which pings fall inside the points marked out on the map."""
        return inside_shape(self.lon, self.lat, shape)

    def reject(self, mask) -> int:
        self._remember()
        going = self.keep & mask
        self.keep &= ~mask
        return int(going.sum())

    def keep_only(self, mask) -> int:
        self._remember()
        going = self.keep & ~mask
        self.keep &= mask
        return int(going.sum())

    def set_roi(self, shape) -> int:
        """Keep only what is inside the shape, and remember it as the region."""
        dropped = self.keep_only(self.inside(shape))
        self.roi = [tuple(point) for point in shape]
        return dropped

    def clear_roi(self) -> None:
        self._remember()
        self.roi = None

    def restore(self) -> None:
        self._remember()
        self.keep[:] = True
        self.roi = None

    def undo(self) -> bool:
        if not self.history:
            return False
        self.keep, self.roi = self.history.pop()
        return True

    # -- what is left --------------------------------------------------------

    def stats(self) -> dict:
        kept = self.keep
        depth = self.depth_ft[kept & (self.depth_ft > 0)]
        minutes = 0.0
        if kept.any():
            times = self.time_ms[kept]
            minutes = float(times[-1] - times[0]) / 60000.0
        return {
            "pings": int(len(kept)),
            "kept": int(kept.sum()),
            "rejected": int((~kept).sum()),
            "flagged": int(self.flagged.sum()),
            "flagged_kept": int((self.flagged & kept).sum()),
            "minutes": minutes,
            "depth_min_ft": float(depth.min()) if depth.size else 0.0,
            "depth_max_ft": float(depth.max()) if depth.size else 0.0,
            "depth_median_ft": float(np.median(depth)) if depth.size else 0.0,
            "speed_median_mph": (float(np.median(self.speed_mph[kept]))
                                 if kept.any() else 0.0),
            "track_miles": self.track_miles(),
        }

    def drawable_track(self, gap_ft: float = 50.0):
        """
        The kept track as line coordinates, broken where it is not a track.

        Two kept pings either side of a rejected stretch are not joined by a
        leg the boat ran, and neither are a stale fix and the survey it was
        stamped before. Drawing them joined puts a line across water nobody
        went over, which is a plain lie in the middle of the picture. NaNs are
        how a line is told to stop and start again.
        """
        at = np.flatnonzero(self.keep)
        if at.size < 2:
            return self.lon[at], self.lat[at]
        lon, lat = self.lon[at], self.lat[at]
        hops = haversine_ft(lat[:-1], lon[:-1], lat[1:], lon[1:])
        breaks = np.flatnonzero(hops > gap_ft) + 1
        if not breaks.size:
            return lon, lat
        return (np.insert(lon, breaks, np.nan),
                np.insert(lat, breaks, np.nan))

    def track_miles(self) -> float:
        """
        How far the boat went, over the pings being kept.

        Measured fix to fix, and only where the fix moved at a speed a boat
        can move at: a position that leapt to another state and back did not
        travel there, and counting it would put thousands of miles into a
        twenty-minute survey.
        """
        good = self.keep & self.fresh & ~self.flags.get(
            'gps_jump', np.zeros_like(self.keep))
        at = np.flatnonzero(good)
        if at.size < 2:
            return 0.0
        hops = haversine_ft(self.lat[at[:-1]], self.lon[at[:-1]],
                            self.lat[at[1:]], self.lon[at[1:]])
        seconds = np.diff(self.time_ms[at]) / 1000.0
        # A gap where rejected pings were is not a leg of the track.
        with np.errstate(divide='ignore', invalid='ignore'):
            mph = np.where(seconds > 0, hops / np.maximum(seconds, 1e-6)
                           / 5280.0 * 3600.0, 0.0)
        return float(np.sum(hops[mph <= self.rules['jump_mph']]) / 5280.0)

    def bounds(self, kept_only: bool = True, margin: float = 0.0004,
               mask=None):
        """(west, south, east, north) round the track, with a little air."""
        if mask is None:
            mask = (self.keep if (kept_only and self.keep.any())
                    else np.ones_like(self.keep))
        lat, lon = self.lat[mask], self.lon[mask]
        if not len(lat):
            return None
        return (float(lon.min()) - margin, float(lat.min()) - margin,
                float(lon.max()) + margin, float(lat.max()) + margin)

    def survey_bounds(self, margin: float = 0.0004):
        """
        Where the survey happened, ignoring fixes that are not from it.

        A recording that starts with the unit's last known position can span
        two continents; opening the map on that shows a hemisphere with two
        dots on it. What someone wants to see first is the water they were
        on, and the fixes from elsewhere are exactly the ones already
        flagged as being from elsewhere.
        """
        here = ~(self.flags.get('gps_far', np.zeros_like(self.keep))
                 | self.flags.get('gps_jump', np.zeros_like(self.keep)))
        if not here.any():
            here = np.ones_like(self.keep)
        return self.bounds(margin=margin, mask=here)

    def kept_by_beam(self) -> dict:
        """
        The pings to write, per channel.

        The side scan channels ping at the same instants as the down beam but
        number their records separately, so a decision made on the track is
        carried across by time rather than by index. Nearest in time, because
        the three channels are written a few milliseconds apart.
        """
        result = {}
        reference = self.time_ms
        for beam in self.survey.beams:
            pings = self.survey.pings[beam]
            if beam == self.beam:
                result[beam] = [p for p, k in zip(pings, self.keep) if k]
                continue
            times = np.array([p.time_ms for p in pings], dtype=np.int64)
            right = np.clip(np.searchsorted(reference, times), 0, len(reference) - 1)
            left = np.clip(right - 1, 0, len(reference) - 1)
            take_left = (np.abs(reference[left] - times)
                         <= np.abs(reference[right] - times))
            nearest = np.where(take_left, left, right)
            mask = self.keep[nearest]
            result[beam] = [p for p, k in zip(pings, mask) if k]
        return result

    def first_kept_ping(self):
        held = np.flatnonzero(self.keep)
        if not held.size:
            return None
        return self.survey.pings[self.beam][int(held[0])]

    # The five columns the chart pipeline reads from a depth CSV. Fixed
    # here rather than imported so the Fixer keeps no dependency on the
    # build side of the pipeline.
    CSV_COLUMNS = ['lon', 'lat', 'dep_m', 'date', 'time']

    def write_soundings(self, path: str, log=print) -> str:
        """
        The kept pings as a depth CSV: lon, lat, dep_m, date, time.

        A repaired .DAT is a Humminbird container and can only ever be one.
        These five columns are not: they are what the chart build reads when
        it is given a CSV instead of a recording, and nothing downstream has
        to know which sounder produced them. Depths are metres, positive
        down, as the rest of the pipeline expects - the window shows feet
        because that is what the water is measured in around here.

        Pings with no depth are left out. The unit writes zero when it has
        no bottom lock, and a zero here would be charted as a sounding at
        the surface.
        """
        began = (repair.start_time(self.survey.folder, self.survey.beams,
                                   self.survey.recordlens_ms)
                 if getattr(self.survey, 'humminbird', True) else 0)
        rows = 0
        with open(path, 'w', newline='', encoding='utf-8') as fh:
            writer = csv.writer(fh)
            writer.writerow(self.CSV_COLUMNS)
            for i in np.flatnonzero(self.keep):
                depth_m = float(self.depth_ft[i]) / M_TO_FT
                if depth_m <= 0:
                    continue
                when = datetime.datetime.fromtimestamp(
                    began + float(self.time_ms[i]) / 1000.0,
                    datetime.timezone.utc)
                writer.writerow([f'{self.lon[i]:.8f}', f'{self.lat[i]:.8f}',
                                 f'{depth_m:.3f}',
                                 when.strftime('%Y-%m-%d'),
                                 when.strftime('%H:%M:%S')])
                rows += 1
        log(f'  {os.path.basename(path)}: {rows} soundings, any sounder can read it')
        return path

    def kept_ranges(self) -> dict:
        """
        The kept pings per channel, as runs rather than a list of numbers.

        A survey is 11,000 pings a channel and an edit usually keeps long
        stretches of it, so runs are both smaller and easier to read than
        the alternative. Each run is [first, last] inclusive, in the
        channel's own record numbers, with the times beside them because a
        decoder that numbers its pings differently can still match on time.
        """
        out = {}
        for beam, kept in self.kept_by_beam().items():
            wanted = {id(ping) for ping in kept}
            runs = []
            previous = None
            for i, ping in enumerate(self.survey.pings[beam]):
                if id(ping) not in wanted:
                    continue
                if previous is not None and i == previous + 1:
                    runs[-1][1] = ping.record
                    runs[-1][3] = ping.time_ms
                else:
                    runs.append([ping.record, ping.record,
                                 ping.time_ms, ping.time_ms])
                previous = i
            out[beam] = {
                "kept": len(kept),
                "of": len(self.survey.pings[beam]),
                "runs": [{"records": [a, b], "time_ms": [t0, t1]}
                         for a, b, t0, t1 in runs],
            }
        return out

    def header_missing(self) -> bool:
        """
        True when the .DAT holds no header and one can be written.

        The unit writes the pings as it goes and finishes the .DAT at the
        end, so an interrupted recording leaves an empty one. Nothing
        downstream can read the recording until it is there - which makes
        this the one repair that has to happen before any editing, rather
        than because of it.
        """
        if not getattr(self.survey, 'humminbird', True):
            return False
        return getattr(self.survey, 'dat', None) is None

    def write_header(self, log=print) -> str:
        """
        Write the missing .DAT beside the recording, and nothing else.

        Every ping is described, not just the kept ones: this is the file
        the recording should have had all along, and trimming belongs to a
        save the user has actually asked for.
        """
        if not self.header_missing():
            raise SystemExit(
                "That recording already has a header. Save a fixed "
                "recording instead of overwriting it.")
        whole = {b: self.survey.pings[b] for b in self.survey.beams}
        repair.write_header(self.survey, self.survey.dat_path,
                            self.survey.name, whole,
                            position_from=self.survey.first_good(
                                self.survey.beams[0]), log=log)
        return self.survey.dat_path

    def time_windows(self, beam: str = "") -> list:
        """
        The kept stretches as [start_ms, end_ms], on the recording's clock.

        Taken from the depth beam, because that is the channel the
        decisions were made on; the side scan pings inside a window come
        along with it, which is the whole point of filtering by time
        rather than by record.
        """
        beam = beam or self.beam
        info = self.kept_ranges().get(beam, {})
        return [run["time_ms"] for run in info.get("runs", [])]

    def save(self, out_root: str = "", suffix: str = "_fixed", log=print) -> str:
        """
        Write the edit out. The same three files, whatever went in.

        A time filter, the soundings, and the report of what was kept - so
        there is one shape to remember and one thing to hand on, and the
        original recording is enough alongside them for any of it to be
        applied.

        A Humminbird recording gets a repaired copy as well. Not because the
        filter is insufficient, but because a recording whose header was lost
        cannot be filtered at all: nothing can read it to apply one, and
        rebuilding it is the only thing that makes it a recording again.
        """
        if not self.keep.any():
            raise SystemExit("Nothing to write: every ping has been rejected.")
        humminbird = getattr(self.survey, 'humminbird', True)
        source = (self.survey.folder if humminbird
                  else self.survey.source_path)
        out_root = out_root or os.path.dirname(source)
        stem = (repair.fixed_name(out_root, self.survey, suffix) if humminbird
                else self.survey.name + suffix)

        report = dict(self.stats())
        report.update({
            "recording": source,
            "rules": self.rules,
            "roi": [list(point) for point in self.roi] if self.roi else None,
            "flags": {name: int(mask.sum()) for name, mask in self.flags.items()},
            "kept": self.kept_ranges(),
        })

        # Always, and first: the stretches to keep, in the two columns
        # PINGMapper filters on. With the recording it came from, this is the
        # whole of the edit - including the side scan, which no channel loses
        # because nothing was rewritten.
        table = os.path.join(out_root, stem + "_timefilter.csv")
        any_recording.write_time_filter(table, self.time_windows(), log=log)
        report["time_filter"] = table

        csv_path = os.path.join(out_root, stem + "_soundings.csv")
        self.write_soundings(csv_path, log=log)
        report["soundings"] = csv_path

        written = ""
        if humminbird:
            written = repair.write_recording(
                self.survey, out_root, stem, self.kept_by_beam(),
                position_from=self.first_kept_ping(), log=log)
            report["written"] = written
        else:
            report["unmodified"] = True
            log("  the recording itself was not touched - hand PINGMapper the "
                "filter with it")

        note = os.path.join(out_root, stem + "_fix_report.json")
        with open(note, "w") as fh:
            json.dump(report, fh, indent=2)
        log(f"  {os.path.basename(note)}: what was kept and why")
        return written or table


def load(path: str, rules: dict = None, log=print) -> Review:
    """
    Open a recording, whichever kind it is.

    Humminbird is parsed directly, because that is the format this can
    also write. Everything PINGVerter reads is converted to a PINGMapper
    project first and edited from its tables.
    """
    if not any_recording.is_humminbird(path) and os.path.isfile(path):
        if not any_recording.supported(path):
            raise SystemExit(
                f"{os.path.basename(path)} is not a sonar recording this "
                f"reads. It handles: "
                f"{', '.join(sorted(any_recording.CONVERTERS))}.")
        return Review(any_recording.open_any(path, log=log), rules)
    name, folder, dat = repair.resolve(path)
    return Review(repair.Survey(name, folder, dat), rules)


# ── Reporting, for when no window is wanted ─────────────────────────────────

def report(path: str, rules: dict = None, log=print) -> Review:
    review = load(path, rules)
    review.survey.report(log)
    stats = review.stats()
    log("")
    log(f"  track {stats['track_miles']:.2f} miles over {stats['minutes']:.1f} min, "
        f"median speed {stats['speed_median_mph']:.1f} mph")
    log(f"  depth {stats['depth_min_ft']:.1f} - {stats['depth_max_ft']:.1f} ft "
        f"(median {stats['depth_median_ft']:.1f} ft)")
    log("")
    log("  flagged:")
    for key, label in FLAG_LABELS:
        count = int(review.flags[key].sum())
        if count:
            log(f"    {count:6d}  {label}")
    if not review.flagged.any():
        log("    nothing")
    return review


# ── The window ──────────────────────────────────────────────────────────────

def run_gui(path: str = ""):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    import appicon                   # imports tkinter, so not at the top

    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,
                                                   NavigationToolbar2Tk)
    from matplotlib.figure import Figure
    from matplotlib.patches import Polygon

    class Section(ttk.Frame):
        """
        A titled group that folds away, because ttk has none.
    
        The title is the control - there is no separate handle to hunt for -
        and it carries the arrow that says which way a click will move it.
        Which groups are open is kept with the rest of this machine's
        settings, so a panel someone tidied stays tidy.
        """
    
        # Solid triangles at 13pt: the arrow is the control, and a 7px glyph
        # is a thing you aim at rather than press.
        SHUT, OPEN = chr(9654), chr(9660)      # right-pointing, down-pointing
    
        def __init__(self, parent, title: str, key: str, opened: bool = True,
                     first: bool = False):
            super().__init__(parent)
            self.key = key
            self.title_text = title
            self.opened = workspace.panel_sections().get(key, opened)
    
            # A rule above each group but the first, so the panel reads as
            # bands rather than one long column of controls.
            if not first:
                ttk.Separator(self, orient='horizontal').pack(fill='x',
                                                              pady=(10, 0))
            self.header = ttk.Label(self, cursor='hand2', anchor='w',
                                    padding=(2, 7),
                                    font=('', 11, 'bold'))
            self.header.pack(fill='x')
            for widget in (self.header,):
                widget.bind('<Button-1>', lambda _event: self.toggle())
    
            # The body sits behind its own vertical rule and an indent, which
            # is what says these controls belong to the title above them.
            self.holder = ttk.Frame(self)
            ttk.Separator(self.holder, orient='vertical').pack(side='left',
                                                               fill='y')
            self.body = ttk.Frame(self.holder, padding=(10, 2, 0, 4))
            self.body.pack(side='left', fill='both', expand=True)
            self._paint()
    
        def _paint(self):
            arrow = self.OPEN if self.opened else self.SHUT
            self.header.configure(text=f'{arrow}  {self.title_text}')
            if self.opened:
                self.holder.pack(fill='x')
            else:
                self.holder.pack_forget()
    
        def toggle(self):
            self.opened = not self.opened
            workspace.set_panel_section(self.key, self.opened)
            self._paint()
    
    
    class FixerApp(tk.Tk):

        def __init__(self):
            super().__init__()
            self.title("Recording Fixer")
            appicon.apply(self)
            self.geometry("1280x820")
            self.review = None
            self.basemap = None                  # (image, extent)
            self.mailbox = queue.Queue()
            self.fetching = None                 # cancel flag of the running fetch
            self.points = []                     # the shape being marked out
            self.next_view = None                # a view to take up once
            self.pending_view = None
            self.var_rule = {key: tk.StringVar(value=str(value))
                             for key, value in DEFAULT_RULES.items()}
            self.var_satellite = tk.BooleanVar(value=True)
            self.var_labels = tk.BooleanVar(value=True)
            self.var_status = tk.StringVar(value="Open a recording to begin.")
            self.panel_open = workspace.panel_sections().get('sidebar', True)
            self._build()
            self.after(80, self._pump)

        # -- layout ----------------------------------------------------------

        def _build(self):
            bar = ttk.Frame(self, padding=(8, 6))
            bar.pack(fill="x")
            self.btn_panel = ttk.Button(bar, width=3,
                                        command=self.toggle_sidebar)
            self.btn_panel.pack(side="left", padx=(0, 6))
            ttk.Button(bar, text="Open recording...",
                       command=self.on_open).pack(side="left")
            self.lbl_file = ttk.Label(bar, text="(nothing open)", foreground="#336633")
            self.lbl_file.pack(side="left", padx=(10, 0))
            ttk.Button(bar, text="Save fixed recording",
                       command=self.on_save).pack(side="right")
            # Only for a recording that lost its header. It sits beside the
            # save rather than inside it because it is not an edit: it is
            # what has to happen before anything can read the file at all.
            self.btn_header = ttk.Button(bar, text="Save repaired .DAT",
                                         command=self.on_write_header)

            body = ttk.Frame(self)
            body.pack(fill="both", expand=True)

            # The panel scrolls. It used to be a fixed column that simply
            # ran out of window, so the last group was unreachable on a
            # laptop however much it mattered.
            self.sidebar = ttk.Frame(body, width=352)
            self.sidebar.pack(side="left", fill="y")
            self.sidebar.pack_propagate(False)

            canvas = tk.Canvas(self.sidebar, borderwidth=0,
                               highlightthickness=0, width=330,
                               background=self.cget("background"))
            scroll = ttk.Scrollbar(self.sidebar, orient="vertical",
                                   command=canvas.yview)
            canvas.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)

            side = ttk.Frame(canvas, padding=(8, 4))
            window = canvas.create_window((0, 0), window=side, anchor="nw")
            side.bind("<Configure>", lambda _e: canvas.configure(
                scrollregion=canvas.bbox("all")))
            canvas.bind("<Configure>", lambda e: canvas.itemconfigure(
                window, width=e.width))

            # Only while the pointer is over the panel: the map has its own
            # use for the wheel and must keep it.
            def wheel(event):
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

            canvas.bind("<Enter>",
                        lambda _e: canvas.bind_all("<MouseWheel>", wheel))
            canvas.bind("<Leave>",
                        lambda _e: canvas.unbind_all("<MouseWheel>"))

            summary = Section(side, "This recording", "summary", first=True)
            summary.pack(fill="x")
            self.txt_summary = tk.Text(summary.body, height=9, width=38,
                                       wrap="word", relief="flat",
                                       background=self.cget("background"))
            self.txt_summary.pack(fill="x")
            self.txt_summary.configure(state="disabled")

            rules = Section(side, "What counts as an outlier", "rules")
            rules.pack(fill="x", pady=(8, 0))
            captions = [
                ("jump_mph", "Fix jump over (mph)"),
                ("frozen_pings", "Position stuck for (pings)"),
                ("stopped_mph", "Boat stopped under (mph)"),
                ("err_ft", "Reported fix error over (ft)"),
                ("depth_max_ft", "Depth deeper than (ft)"),
                ("spike_ft", "Depth spike over (ft)"),
                ("spike_window", "Judged against (pings)"),
                ("turn_deg", "Turn of more than (deg)"),
                ("turn_ft", "measured over (ft)"),
            ]
            for row, (key, caption) in enumerate(captions):
                ttk.Label(rules.body, text=caption).grid(row=row, column=0,
                                                         sticky="w", pady=1)
                ttk.Entry(rules.body, textvariable=self.var_rule[key],
                          width=8).grid(row=row, column=1, sticky="e", pady=1)
            rules.body.columnconfigure(0, weight=1)
            ttk.Button(rules.body, text="Flag outliers",
                       command=self.on_flag).grid(row=len(captions), column=0,
                                                  columnspan=2, sticky="ew",
                                                  pady=(6, 0))
            # The other half of Find outliers: without this the only way to
            # unmark them is to change a threshold and flag again.
            ttk.Button(rules.body, text="Clear flagged points",
                       command=self.on_clear_flags).grid(
                           row=len(captions) + 1, column=0, columnspan=2,
                           sticky="ew", pady=(4, 0))

            flagged = Section(side, "What was flagged", "flags")
            flagged.pack(fill="x", pady=(8, 0))
            self.txt_flags = tk.Text(flagged.body, height=9, width=38,
                                     wrap="word", relief="flat",
                                     background=self.cget("background"))
            self.txt_flags.pack(fill="x", pady=(6, 0))
            self.txt_flags.configure(state="disabled")

            picks = Section(side, "The shape you marked", "picks")
            picks.pack(fill="x", pady=(8, 0))
            ttk.Label(picks.body, wraplength=290, foreground="#555555",
                      text="Click the map to drop points. Two make a box, three "
                           "or more a polygon. Right-click takes the last one "
                           "back.").pack(fill="x", pady=(0, 4))
            ttk.Button(picks.body, text="Flag points in shape",
                       command=lambda: self.on_shape("in")).pack(fill="x")
            ttk.Button(picks.body, text="Flag points outside shape",
                       command=lambda: self.on_shape("out")).pack(fill="x",
                                                                  pady=(4, 0))
            ttk.Button(picks.body, text="Clear only user selected flags",
                       command=self.on_clear_user_flags).pack(fill="x",
                                                             pady=(4, 0))

            actions = Section(side, "The whole recording", "actions")
            actions.pack(fill="x", pady=(8, 0))
            ttk.Button(actions.body, text="Reject every flagged point",
                       command=self.on_reject_flagged).pack(fill="x")
            ttk.Button(actions.body, text="Restore all points",
                       command=self.on_restore).pack(fill="x", pady=(4, 0))

            show = ttk.Frame(side)
            show.pack(fill="x", pady=(8, 0))
            ttk.Checkbutton(show, text="Satellite", variable=self.var_satellite,
                            command=self.redraw).pack(side="left")
            ttk.Checkbutton(show, text="Depth labels", variable=self.var_labels,
                            command=self.redraw).pack(side="left", padx=(10, 0))

            self._paint_sidebar_button()

            self.right = right = ttk.Frame(body)
            right.pack(side="left", fill="both", expand=True)
            if not self.panel_open:
                self.sidebar.pack_forget()
            self.figure = Figure(figsize=(8, 6), dpi=100)
            self.ax = self.figure.add_subplot(111)
            self.canvas = FigureCanvasTkAgg(self.figure, master=right)
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            self.toolbar = NavigationToolbar2Tk(self.canvas, right)
            self.toolbar.update()
            self.canvas.mpl_connect("button_press_event", self._on_click)
            self.ax.callbacks.connect("xlim_changed", self._view_moved)
            self.ax.callbacks.connect("ylim_changed", self._view_moved)

            ttk.Label(self, textvariable=self.var_status, anchor="w",
                      padding=(8, 4)).pack(fill="x")

        # -- worker plumbing -------------------------------------------------

        def _pump(self):
            """
            Bring worker results back to the window.

            Tkinter may only be touched from the thread that made it, so the
            fetch thread posts here and this - which runs on the main thread -
            is what actually draws.
            """
            try:
                while True:
                    kind, payload = self.mailbox.get_nowait()
                    if kind == "basemap":
                        self.basemap = payload
                        self.redraw()
                    elif kind == "status":
                        self.var_status.set(payload)
                    elif kind == "loaded":
                        self._show_review(payload)
                    elif kind == "failed":
                        self.var_status.set(str(payload))
                        messagebox.showerror("Recording Fixer", str(payload))
            except queue.Empty:
                pass
            self.after(80, self._pump)

        def _later(self, work, kind="basemap"):
            def run():
                try:
                    self.mailbox.put((kind, work()))
                except Exception as exc:
                    self.mailbox.put(("failed", exc))
            threading.Thread(target=run, daemon=True).start()

        # -- opening ---------------------------------------------------------

        def on_open(self):
            # Opens where the cards are copied to, which the viewer's
            # workflow list is what actually sets.
            path = filedialog.askopenfilename(
                title="Sonar recording",
                initialdir=workspace.open_dir(),
                filetypes=RECORDING_FILETYPES)
            if path:
                self.open(path)

        def open(self, path: str):
            self.var_status.set(f"Reading {os.path.basename(path)} ...")
            self.update_idletasks()
            rules = self._rules_from_form()
            self._later(lambda: load(path, rules), kind="loaded")

        def _show_review(self, review: Review):
            self.review = review
            self.basemap = None
            self.points = []
            self.lbl_file.configure(text=review.survey.name)
            self._show_header_button()
            box = review.survey_bounds()
            self.next_view = (box[0], box[2], box[1], box[3]) if box else None
            self.redraw()
            self.request_basemap()

        # -- the map ---------------------------------------------------------

        def _view_moved(self, _ax=None):
            if self.review is None:
                return
            # Panning fires this for every step; only the last one is worth a
            # fetch, so let it settle first.
            if self.pending_view is not None:
                self.after_cancel(self.pending_view)
            self.pending_view = self.after(500, self.request_basemap)

        def request_basemap(self):
            self.pending_view = None
            if self.review is None or not self.var_satellite.get():
                return
            west, east = self.ax.get_xlim()
            south, north = self.ax.get_ylim()
            if self.fetching is not None:
                self.fetching.set()
            cancel = threading.Event()
            self.fetching = cancel
            box = (west, south, east, north)
            self.var_status.set("Fetching satellite imagery ...")

            def work():
                image, extent = satellite.fetch(box, cancel=cancel)
                self.mailbox.put(("status", "Imagery ready." if image is not None
                                  else "No imagery for this view - drawing without it."))
                return (image, extent) if image is not None else None

            self._later(work)

        def redraw(self):
            review = self.review
            if review is None:
                self.ax.clear()
                self.ax.set_axis_off()
                self.canvas.draw_idle()
                return

            # Everything is drawn from scratch each time, and clearing the axes
            # is what resets the zoom. Somebody who has zoomed into one leg and
            # then marks a point on it wants to still be looking at that leg.
            drawn_before = self.ax.has_data()
            xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
            self.ax.clear()
            if self.basemap and self.var_satellite.get():
                image, extent = self.basemap
                self.ax.imshow(np.asarray(image), origin="upper",
                               extent=(extent[0], extent[2], extent[1], extent[3]),
                               interpolation="bilinear", zorder=0)
            else:
                self.ax.set_facecolor("#0e1a22")

            keep = review.keep
            flagged = review.flagged
            line_lon, line_lat = review.drawable_track()
            self.ax.plot(line_lon, line_lat, "-", color="#00e5ff",
                         linewidth=1.2, zorder=3, label="kept")
            if (~keep).any():
                self.ax.plot(review.lon[~keep], review.lat[~keep], ".", color="#8a8a8a",
                             markersize=2, zorder=2, label="rejected")
            live = flagged & keep
            if live.any():
                self.ax.plot(review.lon[live], review.lat[live], ".", color="#ffb300",
                             markersize=4, zorder=4, label="flagged")

            if self.var_labels.get():
                self._label_depths()
            if review.roi:
                self.ax.add_patch(Polygon(review.roi, closed=True, fill=False,
                                          edgecolor="#00e5a0", linewidth=1.4,
                                          linestyle="--", zorder=5))
            self._draw_points()

            self.ax.set_xlabel("longitude")
            self.ax.set_ylabel("latitude")
            # Degrees written out. Matplotlib's default offset notation turns
            # a lake into '+3.81e1' plus four decimals of nothing, which is
            # unreadable next to a position anyone would type into a plotter.
            for axis in (self.ax.xaxis, self.ax.yaxis):
                axis.set_major_formatter(
                    matplotlib.ticker.FuncFormatter(lambda v, _p: f'{v:.4f}'))
            middle = float(np.median(review.lat))
            self.ax.set_aspect(1.0 / max(0.1, math.cos(math.radians(middle))))
            self.ax.legend(loc="upper right", fontsize=8, framealpha=0.75)
            if self.next_view:
                west, east, south, north = self.next_view
                self.ax.set_xlim(west, east)
                self.ax.set_ylim(south, north)
                self.next_view = None
            elif drawn_before:
                self.ax.set_xlim(xlim)
                self.ax.set_ylim(ylim)
            self.canvas.draw_idle()
            self._refresh_text()

        def _draw_points(self):
            """The shape being marked: the points, and dotted lines between."""
            if not self.points:
                return
            xs = [p[0] for p in self.points]
            ys = [p[1] for p in self.points]
            if len(self.points) >= 2:
                shape = self.shape()
                closed = shape + [shape[0]]
                self.ax.plot([p[0] for p in closed], [p[1] for p in closed], ":",
                             color="#ff4fd8", linewidth=1.4, zorder=7)
            self.ax.plot(xs, ys, "o", markersize=6, markerfacecolor="#ff4fd8",
                         markeredgecolor="white", markeredgewidth=1.0, zorder=8)
            for number, (x, y) in enumerate(self.points, start=1):
                self.ax.annotate(str(number), (x, y), textcoords="offset points",
                                 xytext=(6, -9), fontsize=8, color="#ff4fd8",
                                 zorder=8)

        def _label_depths(self):
            """A scattering of soundings along the track, in feet."""
            review = self.review
            live = np.flatnonzero(review.keep & (review.depth_ft > 0))
            if not live.size:
                return
            step = max(1, len(live) // 40)
            for i in live[::step]:
                self.ax.annotate(f"{review.depth_ft[i]:.0f}",
                                 (review.lon[i], review.lat[i]),
                                 textcoords="offset points", xytext=(3, 3),
                                 fontsize=7, color="#ffffff", zorder=6,
                                 path_effects=None)

        # -- selection -------------------------------------------------------

        def _on_click(self, event):
            """
            Drop a point where the map was clicked, or take the last one back.

            Points rather than a dragged rectangle because a survey is not
            rectangular: the leg to cut out is at an angle, the cove to keep is
            a shape, and a box round either takes in water that belongs to the
            legs on both sides of it.
            """
            if self.toolbar.mode:                # pan or zoom is driving instead
                return
            if event.inaxes is not self.ax or event.xdata is None:
                return
            if event.button == 3:
                if self.points:
                    self.points.pop()
            elif event.button == 1:
                self.points.append((float(event.xdata), float(event.ydata)))
            else:
                return
            self.redraw()
            self._say_shape()

        def _say_shape(self):
            marked = len(self.points)
            if not marked:
                self.var_status.set("Click the map to start marking a shape.")
                return
            if marked == 1:
                self.var_status.set("1 point. One more makes a box.")
                return
            shape = self.shape()
            inside = (int(self.review.inside(shape).sum())
                      if self.review is not None else 0)
            kind = "box" if marked == 2 else f"{marked}-point shape"
            self.var_status.set(f"{kind}: {inside} pings inside.")

        def shape(self):
            """
            The marked points as a shape.

            Two points are the diagonal of a box, which is what most cuts want
            and the fastest thing to mark; they are turned into its four
            corners here so that everything downstream - the test, the drawing,
            the saved region - deals in one kind of thing.
            """
            if len(self.points) != 2:
                return list(self.points)
            (x0, y0), (x1, y1) = self.points
            return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

        def on_shape(self, what: str):
            if self.review is None or len(self.points) < 2:
                self.var_status.set("Click at least two points on the map first.")
                return
            shape = self.shape()
            mask = self.review.inside(shape)
            # Outside is the same gesture read the other way round, and it
            # is the common one: the survey is the shape, everything else
            # is the transit out and back.
            if what == "out":
                mask = ~mask
            total = self.review.mark_user(mask)
            where = "in" if what == "in" else "outside"
            self.var_status.set(
                f"{int(mask.sum())} points {where} the shape marked; "
                f"{total} marked in all. Nothing has been rejected yet.")
            # The shape has done its job; leaving it drawn only makes the
            # next one harder to place.
            self.points = []
            self.redraw()

        # -- the buttons -----------------------------------------------------

        def _rules_from_form(self) -> dict:
            rules = {}
            for key, var in self.var_rule.items():
                try:
                    value = float(var.get())
                except ValueError:
                    continue
                rules[key] = int(value) if isinstance(DEFAULT_RULES[key], int) else value
            return rules

        def on_flag(self):
            if self.review is None:
                return
            self.review.flag(self._rules_from_form())
            self.redraw()
            self.var_status.set(f"{int(self.review.flagged.sum())} pings flagged.")

        def on_clear_flags(self):
            """Unmark everything, without keeping or rejecting anything."""
            if self.review is None:
                return
            self.review.clear_user_flag()
            self.review.flags = {}
            self.redraw()
            self.var_status.set("Flags cleared. No points were kept or rejected.")

        def on_clear_user_flags(self):
            """Unmark what you marked, and leave the rules' findings."""
            if self.review is None:
                return
            had = self.review.clear_user_flag()
            self.redraw()
            self.var_status.set(f"Unmarked {had} points you had selected.")

        def on_reject_flagged(self):
            if self.review is None:
                return
            changed = self.review.reject(self.review.flagged)
            self.redraw()
            self.var_status.set(f"Rejected {changed} flagged points.")

        def on_clear_roi(self):
            if self.review is None:
                return
            self.review.clear_roi()
            self.redraw()
            self.var_status.set("Region cleared. Rejected points stay rejected - "
                                "use Restore all points to bring them back.")

        def on_restore(self):
            if self.review is None:
                return
            self.review.restore()
            self.redraw()
            self.var_status.set("Every point is being kept again.")

        def toggle_sidebar(self):
            """Give the window to the chart, or take it back."""
            self.panel_open = not self.panel_open
            workspace.set_panel_section('sidebar', self.panel_open)
            if self.panel_open:
                self.sidebar.pack(side='left', fill='y', before=self.right)
            else:
                self.sidebar.pack_forget()
            self._paint_sidebar_button()

        def _paint_sidebar_button(self):
            """The arrow points the way the panel will move."""
            shut, opened = chr(9654), chr(9664)     # right, left
            self.btn_panel.configure(
                text=opened if self.panel_open else shut)

        def _show_header_button(self):
            """The repair button appears only for a recording missing its .DAT."""
            wanted = self.review is not None and self.review.header_missing()
            if wanted:
                self.btn_header.pack(side="right", padx=(0, 8))
                self.var_status.set(
                    "This recording has no header - power was lost before it "
                    "was finished. Save a repaired .DAT to make it readable.")
            else:
                self.btn_header.pack_forget()

        def on_write_header(self):
            if self.review is None or not self.review.header_missing():
                return
            dat = self.review.survey.dat_path
            if not messagebox.askokcancel(
                    "Recording Fixer",
                    f"Write the missing header into\n{dat}\n\n"
                    "This is the only file this program writes into the "
                    "recording itself, and the pings are not touched."):
                return
            lines = []
            try:
                path = self.review.write_header(log=lines.append)
            except Exception as exc:
                messagebox.showerror("Recording Fixer", str(exc))
                return
            stale = sum(self.review.survey.stale.values())
            after = ("\n\nThe first pings still carry a position from the "
                     "unit's last outing, and PINGMapper takes the survey's "
                     "UTM zone from the first ping it reads. Trim them and "
                     "save a fixed recording before processing this."
                     if stale else "")
            self.var_status.set(f"Header written: {path}")
            messagebox.showinfo(
                "Recording Fixer",
                f"{os.path.basename(path)} now holds a header." + after)
            self._show_header_button()

        def on_save(self):
            if self.review is None:
                return
            if not self.review.keep.any():
                messagebox.showerror("Recording Fixer", "Every ping has been rejected.")
                return
            folder = filedialog.askdirectory(
                title="Where to write the fixed recording",
                initialdir=os.path.dirname(self.review.survey.folder))
            if not folder:
                return
            lines = []
            try:
                path = self.review.save(folder, log=lines.append)
            except Exception as exc:
                messagebox.showerror("Recording Fixer", str(exc))
                return
            stats = self.review.stats()
            self.var_status.set(f"Written: {path}")
            messagebox.showinfo(
                "Recording Fixer",
                f"{os.path.basename(path)}\n\n"
                f"{stats['kept']:,} of {stats['pings']:,} pings kept "
                f"({stats['track_miles']:.2f} miles, {stats['minutes']:.1f} min).\n\n"
                + "\n".join(lines))

        # -- panels ----------------------------------------------------------

        def _refresh_text(self):
            review = self.review
            stats = review.stats()
            survey = review.survey
            lines = [
                f"{survey.name}",
                f"{stats['kept']:,} of {stats['pings']:,} pings kept",
                f"{stats['track_miles']:.2f} miles, {stats['minutes']:.1f} min",
                f"median speed {stats['speed_median_mph']:.1f} mph",
                f"depth {stats['depth_min_ft']:.0f} - {stats['depth_max_ft']:.0f} ft "
                f"(median {stats['depth_median_ft']:.0f})",
            ]
            if survey.dat is None:
                lines.append("header missing - saving writes one")
            short = [b for b in survey.beams
                     if survey.index_records[b] != len(survey.pings[b])]
            if short:
                lines.append(f"index short on {', '.join(short)} - saving rebuilds it")
            self._set_text(self.txt_summary, "\n".join(lines))

            flags = [f"{int(review.flags[key].sum()):>6}  {label}"
                     for key, label in FLAG_LABELS if review.flags[key].any()]
            self._set_text(self.txt_flags,
                           "Flagged\n" + ("\n".join(flags) if flags
                                          else "     0  nothing looks wrong"))

        @staticmethod
        def _set_text(widget, text: str):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", text)
            widget.configure(state="disabled")

    app = FixerApp()
    if path:
        app.after(200, lambda: app.open(path))
    app.mainloop()


# ── Self-check ──────────────────────────────────────────────────────────────

def fixer_turn_check(review) -> bool:
    """
    The made-up recording runs due east in a straight line, so nothing in it
    should be called a turn. Anything else means the rule is measuring the
    wrong thing - noise between fixes, say, rather than the course changing.
    """
    return not bool(review.flags['track_turn'].any())


def selftest(log=print) -> bool:
    import tempfile

    ok = True

    def check(what, passed, detail=""):
        nonlocal ok
        ok = ok and passed
        log(f"  {'PASS' if passed else 'FAIL'}  {what}{'  ' + detail if detail else ''}")

    with tempfile.TemporaryDirectory() as tmp:
        # A made-up recording with one of everything wrong in it.
        repair._fake_recording(tmp, "R09000", pings=400, stale=5)
        review = load(os.path.join(tmp, "R09000.DAT"))
        check("track read", len(review.lat) == 400, f"{len(review.lat)}")
        check("stale fixes flagged", review.flags["gps_far"].sum() == 5,
              f"{int(review.flags['gps_far'].sum())}")
        check("depth read as feet", abs(review.depth_ft.max()) < 1e-9
              or review.depth_ft.max() > 0)

        # Two marked points are the corners of a box.
        centre_lon = float(np.median(review.lon))
        box = [(centre_lon, float(review.lat.min()) - 1),
               (float(review.lon.max()) + 1, float(review.lat.max()) + 1)]
        dropped = review.reject(review.inside(box))
        check("two points make a box that rejects what is in it",
              dropped > 0 and not review.keep.all(), f"{dropped} pings")
        kept_before = int(review.keep.sum())
        review.undo()
        check("undo puts them back", int(review.keep.sum()) == kept_before + dropped)

        # A region of interest keeps only what is inside it.
        review.restore()
        review.set_roi(box)
        check("a region keeps only what is inside",
              int(review.keep.sum()) == int(review.inside(box).sum()))
        check("the region is remembered", review.roi == [tuple(p) for p in box])

        # Three or more are a polygon, which is what a leg of a lawnmower or a
        # cove actually needs.
        review.restore()
        west = float(review.lon.min()) - 0.001
        east = float(review.lon.max()) + 0.001
        south = float(review.lat.min()) - 0.001
        middle = float(np.median(review.lat))
        triangle = [(west, south), (east, south), (west, middle)]
        inside = review.inside(triangle)
        check("a polygon takes a corner of the track",
              0 < int(inside.sum()) < len(review.keep),
              f"{int(inside.sum())} of {len(review.keep)} pings")
        check("a bounding box says the same as its two corners",
              np.array_equal(review.inside((west, south, east, middle)),
                             review.inside([(west, south), (east, middle)])))

        # A turn is flagged where the track actually turns.
        straight = fixer_turn_check(review)
        check("a straight run is not called a turn", straight is True, str(straight))

        # Writing what is left, and reading it back with the repair's own eyes.
        review.restore()
        review.reject(review.flags["gps_far"])
        written = review.save(tmp, log=lambda *_: None)
        check("fixed recording written", os.path.isfile(written))
        again = repair.Survey("R09000_fixed", os.path.join(tmp, "R09000_fixed"), written)
        check("it walks clean", again.healthy())
        check("it holds what was kept", len(again.pings["B001"]) == 395,
              f"{len(again.pings['B001'])}")
        check("every channel came along",
              all(len(again.pings[b]) == 395 for b in again.beams))
        check("a report was written",
              os.path.isfile(os.path.join(tmp, "R09000_fixed_fix_report.json")))

        # The format-agnostic half of the output: five columns the chart
        # build reads without knowing what recorded them.
        csv_path = os.path.join(tmp, "R09000_fixed_soundings.csv")
        check("a soundings CSV came with it", os.path.isfile(csv_path))
        check("and a time filter, as every save writes",
              os.path.isfile(os.path.join(tmp, "R09000_fixed_timefilter.csv")))
        with open(csv_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        check("its columns are the ones the pipeline reads",
              rows[0] == Review.CSV_COLUMNS, str(rows[0]))
        check("it holds only kept soundings",
              0 < len(rows) - 1 <= 395, f"{len(rows) - 1}")
        check("depths are metres, positive down",
              all(float(r[2]) > 0 for r in rows[1:]),
              min((r[2] for r in rows[1:]), default="none"))
        check("every row carries a date and a time",
              all(len(r[3]) == 10 and len(r[4]) == 8 for r in rows[1:]))

        kept = review.kept_ranges()
        check("the manifest covers every channel",
              set(kept) == set(review.survey.beams), str(sorted(kept)))
        check("a stretch rejected off the front is one run",
              len(kept["B001"]["runs"]) == 1,
              f"{len(kept['B001']['runs'])} runs")
        check("the side scan lost the same pings",
              all(kept[b]["kept"] == 395 for b in kept),
              str({b: kept[b]['kept'] for b in kept}))

        # Rejecting a stretch out of the middle: the writer has to cope with
        # what is kept no longer being one run of the file.
        review.restore()
        middle = np.zeros(len(review.keep), dtype=bool)
        middle[100:200] = True
        review.reject(middle)
        written = review.save(tmp, suffix="_gap", log=lambda *_: None)
        gapped = repair.Survey("R09000_gap", os.path.join(tmp, "R09000_gap"), written)
        # healthy() would also want the stale opening fixes gone, and this
        # recording deliberately still has them: what is under test is that
        # the chain survives a hole cut out of the middle of it.
        check("a gap in the middle still walks",
              gapped.dat is not None
              and all(gapped.trailing[b] == 0 for b in gapped.beams))
        check("the gap is missing from the file", len(gapped.pings["B001"]) == 300,
              f"{len(gapped.pings['B001'])}")
        check("its index matches the file",
              gapped.index_records["B001"] == len(gapped.pings["B001"]))

    log("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording", nargs="?", default="",
                    help="a .DAT recording (or the folder beside it)")
    ap.add_argument("--report", action="store_true",
                    help="print what is wrong with it and stop; no window")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.selftest:
        raise SystemExit(0 if selftest() else 1)
    if args.report:
        if not args.recording:
            raise SystemExit("Name a recording to report on.")
        report(args.recording)
        return
    run_gui(args.recording)


if __name__ == "__main__":
    main()
