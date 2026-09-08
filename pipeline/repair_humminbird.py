#!/usr/bin/env python3
"""
Put a Humminbird recording back together after the unit lost power mid-survey.

A Helix writes the sonar itself (`R000xx/B00*.SON`) continuously, but the
64-byte header (`R000xx.DAT`) and the ping index (`B00*.IDX`) are finished off
at the end. Pull the plug - or the battery, or the SD card - and the sonar
survives while those two do not:

    R00021.DAT     0 bytes            PINGMapper cannot open the recording
    B001.IDX       truncated          indexes the first part of the recording
    B001.SON       intact             every ping still there

Nothing in the header is unique to it: how many records, how long, how big the
largest one is, and where the recording started are all readable from the pings
themselves, so the header can be written back exactly as the unit would have.

The second thing this repairs is the opposite of missing data - a position the
unit believed and should not have. Until the GPS gets a fix, the Helix stamps
each ping with its *last known* position, which can be a different continent.
PINGMapper takes the UTM zone for the whole survey from the first ping it
reads, so a handful of stale fixes at the start put every chart in the wrong
projection. Those pings carry no usable position and are dropped.

    python pipeline/repair_humminbird.py inspect  <recording>
    python pipeline/repair_humminbird.py repair   <recording>
    python pipeline/repair_humminbird.py repair   <recording> --header-only
    python pipeline/repair_humminbird.py selftest

<recording> is the .DAT file or the folder beside it. A repair writes a new
recording next to the original (`R00021_fixed.DAT` + `R00021_fixed/`) and never
touches what came off the card, except to write a .DAT that is missing or empty
- which is a file the recording should have had all along.
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import math
import os
import shutil
import struct
import sys

# A ping record starts with this, which is what makes walking the file possible.
MAGIC = b"\xc0\xde\xab\x21"
HEADER_LEN = 67                 # Helix ping header, from pingverter's struct
DAT_LEN = 64                    # 1199 / Helix .DAT; Solix is 96 and differs
BEAMS = ("B001", "B002", "B003")

# The spheroid Humminbird stores easting/northing on (Py3Hum's International
# 1924 constants, and the same numbers pingverter uses to get back to degrees).
R_EARTH = 6378388.0

# How far from the survey a fix has to be before it is not a fix at all but the
# position the unit was last switched off at. Half a degree is ~35 miles: far
# enough that no boat did it between pings, close enough to keep a survey that
# genuinely spans a big lake.
STALE_DEGREES = 0.5

# How much of a recording counts as its beginning, for the purpose of waiting
# for the GPS. A Helix takes seconds to get a fix, not minutes, so a tenth of
# the recording is a generous window and still small enough that a dropout in
# the middle can never be mistaken for one. The floor is for short recordings,
# where a tenth would be a handful of pings: at ~13 pings a second, 200 of them
# is about a quarter of a minute either way.
WAKE_UP_FRACTION = 0.1
WAKE_UP_PINGS = 200

# Fields of the 64-byte header, as pingverter reads them.
DAT_FIELDS = [
    ("SP1", 0, 1), ("water_code", 1, 1), ("SP2", 2, 1), ("unknown_1", 3, 1),
    ("sonar_name", 4, 4), ("unknown_2", 8, 4), ("unknown_3", 12, 4),
    ("unknown_4", 16, 4), ("unix_time", 20, 4), ("utm_e", 24, 4),
    ("utm_n", 28, 4), ("unknown_5", 42, 2), ("numrecords", 44, 4),
    ("recordlens_ms", 48, 4), ("linesize", 52, 4), ("unknown_6", 56, 4),
    ("unknown_7", 60, 4),
]
SIGNED = {"utm_e", "utm_n"}

# What the unknown bytes hold when there is no sibling recording to copy from.
# Taken from a Helix 7 MEGA SI .DAT; they are spacers and model flags, and
# pingverter reads them without acting on any but water_code.
DEFAULT_FIXED = {
    "SP1": 0xC1, "water_code": 0, "SP2": 0x82, "unknown_1": 1,
    "sonar_name": 700, "unknown_2": 32, "unknown_3": 0, "unknown_4": 0,
    "unknown_5": 0, "unknown_6": 0, "unknown_7": 0x5C1D000E,
}


# -- Reading -----------------------------------------------------------------

def lat_lon(utm_e: int, utm_n: int):
    """A Humminbird easting/northing as degrees, the way pingverter does it."""
    lat = math.atan(math.tan(math.atan(math.exp(utm_n / R_EARTH)) * 2.0
                             - 1.570796326794897) * 1.0067642927) * 57.295779513082302
    lon = (utm_e * 57.295779513082302) / R_EARTH
    return lat, lon


class Ping:
    """
    One ping: where it sits in the file, and what the sounder said at it.

    A header repair only needs the first half. The second half - depth,
    speed, heading and the fix errors - is what lets a reviewer see which
    pings are worth keeping, and it costs nothing to read while the record
    chain is being walked anyway.
    """

    __slots__ = ("offset", "length", "record", "time_ms", "utm_e", "utm_n",
                 "count", "dep_dm", "speed_dm", "heading_dd", "e_err", "n_err")

    def __init__(self, offset, length, record, time_ms, utm_e, utm_n, count,
                 dep_dm=0, speed_dm=0, heading_dd=0, e_err=0, n_err=0):
        self.offset = offset
        self.length = length
        self.record = record
        self.time_ms = time_ms
        self.utm_e = utm_e
        self.utm_n = utm_n
        self.count = count
        # Tenths, as the unit writes them: depth and speed in tenths of a
        # metre and a metre per second, heading in tenths of a degree.
        self.dep_dm = dep_dm
        self.speed_dm = speed_dm
        self.heading_dd = heading_dd
        # Position error, in centimetres.
        self.e_err = e_err
        self.n_err = n_err

    @property
    def depth_m(self) -> float:
        return self.dep_dm / 10.0

    @property
    def speed_ms(self) -> float:
        return self.speed_dm / 10.0

    @property
    def heading_deg(self) -> float:
        return self.heading_dd / 10.0


def walk(son_path: str):
    """
    Every ping in a .SON, found by following the record chain from byte 0.

    The index is not consulted: it is the file most likely to be truncated, and
    the chain in the .SON is what actually says where each record begins.
    Returns (pings, trailing_bytes) - trailing bytes being a final record the
    unit did not finish writing.
    """
    with open(son_path, "rb") as fh:
        data = fh.read()
    size = len(data)
    pings = []
    at = 0
    while at + HEADER_LEN <= size:
        if data[at:at + 4] != MAGIC:
            break
        record, = struct.unpack_from(">I", data, at + 5)
        time_ms, = struct.unpack_from(">I", data, at + 10)
        utm_e, = struct.unpack_from(">i", data, at + 15)
        utm_n, = struct.unpack_from(">i", data, at + 20)
        count, = struct.unpack_from(">I", data, at + 62)
        length = HEADER_LEN + count
        if at + length > size:
            break                      # the last record was cut off mid-write
        heading, = struct.unpack_from(">H", data, at + 27)
        speed, = struct.unpack_from(">H", data, at + 32)
        depth, = struct.unpack_from(">I", data, at + 35)
        e_err = data[at + 58]
        n_err = data[at + 60]
        pings.append(Ping(at, length, record, time_ms, utm_e, utm_n, count,
                          depth, speed, heading, e_err, n_err))
        at += length
    return pings, size - at


def read_dat(path: str):
    """The header of a .DAT, or None if it is missing, empty or a Solix's."""
    if not os.path.isfile(path) or os.path.getsize(path) != DAT_LEN:
        return None
    with open(path, "rb") as fh:
        raw = fh.read()
    out = {name: int.from_bytes(raw[at:at + n], "big", signed=name in SIGNED)
           for name, at, n in DAT_FIELDS}
    out["filename"] = raw[32:42].decode("ascii", "replace").rstrip("\x00")
    return out


def build_dat(fixed: dict, filename: str, unix_time: int, utm_e: int, utm_n: int,
              numrecords: int, recordlens_ms: int, linesize: int) -> bytes:
    """The 64 bytes a Helix would have written."""
    raw = bytearray(DAT_LEN)
    values = dict(fixed)
    values.update({"unix_time": unix_time, "utm_e": utm_e, "utm_n": utm_n,
                   "numrecords": numrecords, "recordlens_ms": recordlens_ms,
                   "linesize": linesize})
    for name, at, n in DAT_FIELDS:
        raw[at:at + n] = int(values[name]).to_bytes(n, "big", signed=name in SIGNED)
    name_bytes = filename.encode("ascii")[:10]
    raw[32:32 + len(name_bytes)] = name_bytes
    return bytes(raw)


# -- Locating a recording ----------------------------------------------------

def resolve(path: str):
    """(name, folder, dat path) for a recording named by either of its halves."""
    path = os.path.abspath(path.rstrip("/\\"))
    if os.path.isdir(path):
        folder = path
        name = os.path.basename(folder)
        dat = os.path.join(os.path.dirname(folder), name + ".DAT")
    else:
        name = os.path.splitext(os.path.basename(path))[0]
        folder = os.path.join(os.path.dirname(path), name)
        dat = path
    if not os.path.isdir(folder):
        raise SystemExit(
            f"No sonar folder beside {os.path.basename(dat)}." + os.linesep +
            "  A recording is a .DAT plus a folder of the same name holding "
            "B001.SON and friends; if they were separated, put them back together.")
    return name, folder, dat


def beams_of(folder: str):
    return [b for b in BEAMS if os.path.isfile(os.path.join(folder, b + ".SON"))]


def template_dat(dat_path: str):
    """
    A sibling recording's header, to copy the unknown bytes from.

    Those bytes are model and spacer flags rather than anything about this
    particular outing, so the nearest recording off the same unit is a better
    source for them than a constant in this file. The newest is preferred: it
    is the most likely to have been written by the unit in its current state.
    """
    folder = os.path.dirname(dat_path)
    own = []
    others = []
    for entry in os.listdir(folder):
        if not entry.upper().endswith(".DAT"):
            continue
        candidate = os.path.join(folder, entry)
        if os.path.abspath(candidate) == os.path.abspath(dat_path):
            continue
        header = read_dat(candidate)
        if not header:
            continue
        # A header naming a different recording than the file it sits in was
        # rebuilt or renamed by someone; the unknown bytes in it came from
        # wherever that person got them. One that still names itself came off
        # the unit, so it is asked first.
        stem = os.path.splitext(entry)[0]
        wrote_itself = header["filename"].upper() == (stem + ".SON").upper()
        (own if wrote_itself else others).append((candidate, header))
    ranked = sorted(own, key=lambda c: os.path.getmtime(c[0]), reverse=True) + \
        sorted(others, key=lambda c: os.path.getmtime(c[0]), reverse=True)
    return ranked[0] if ranked else None


# -- Working out what the header should say ----------------------------------

def survey_centre(pings):
    """
    Where the recording actually happened.

    The median of the last half of the fixes: by then the GPS is certainly
    working, and a median ignores whatever wandered in before it.
    """
    tail = pings[len(pings) // 2:] or pings
    lats = sorted(lat_lon(p.utm_e, p.utm_n)[0] for p in tail)
    lons = sorted(lat_lon(p.utm_e, p.utm_n)[1] for p in tail)
    return lats[len(lats) // 2], lons[len(lons) // 2]


def is_far(ping, centre) -> bool:
    """A fix that cannot belong to this survey."""
    lat, lon = lat_lon(ping.utm_e, ping.utm_n)
    return (abs(lat - centre[0]) > STALE_DEGREES
            or abs(lon - centre[1]) > STALE_DEGREES)


def stale_prefix(pings, centre) -> int:
    """
    How many pings at the start carry a position from somewhere else.

    Counts to the *last* bad one in the opening stretch rather than the
    first: a GPS coming up alternates between the stale fix and the new one
    for a few seconds, and a ping between two bad ones is no more use than
    they are.

    Only the opening stretch, though. A recording that loses its fix in the
    middle has one bad patch, not a bad beginning, and trimming everything up
    to it would throw away the survey to fix a glitch. Those are reported and
    left where they are.
    """
    window = max(WAKE_UP_PINGS, int(len(pings) * WAKE_UP_FRACTION))
    last_bad = -1
    for i, ping in enumerate(pings[:window]):
        if is_far(ping, centre):
            last_bad = i
    return last_bad + 1


def late_bad(pings, centre, after: int) -> int:
    """Fixes far from the survey that are not part of the opening stretch."""
    return sum(1 for ping in pings[after:] if is_far(ping, centre))


def start_time(folder: str, beams, duration_ms: int) -> int:
    """
    When the recording started, as the .DAT states it.

    The unit writes the .SON files as it goes, so the newest of them was last
    touched when the recording stopped; the start is that moment less the length
    of the recording. Humminbird stores the local wall clock in a field a reader
    will call UTC, and this keeps that convention - checked against recordings
    the unit finished writing itself, where it reproduces their headers exactly.
    """
    ended = max(os.path.getmtime(os.path.join(folder, b + ".SON")) for b in beams)
    began = datetime.datetime.fromtimestamp(ended - duration_ms / 1000.0)
    return calendar.timegm(began.timetuple())


def stamp_of(unix_time: int) -> str:
    """
    The wall clock a .DAT time field means.

    Humminbird writes local time into a field a reader will treat as UTC, so
    it is read back as UTC to get the clock the survey actually happened on.
    """
    return (datetime.datetime.fromtimestamp(unix_time, datetime.timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S"))


class Survey:
    """What one recording's pings say, once they have all been read."""

    def __init__(self, name, folder, dat_path):
        self.name = name
        self.folder = folder
        self.dat_path = dat_path
        self.beams = beams_of(folder)
        if not self.beams:
            raise SystemExit(f"{folder} holds no B00*.SON files.")
        self.pings = {}
        self.trailing = {}
        self.index_records = {}
        for beam in self.beams:
            pings, trailing = walk(os.path.join(folder, beam + ".SON"))
            self.pings[beam] = pings
            self.trailing[beam] = trailing
            idx = os.path.join(folder, beam + ".IDX")
            self.index_records[beam] = (os.path.getsize(idx) // 8
                                        if os.path.isfile(idx) else 0)
        self.dat = read_dat(dat_path)
        self.dat_size = os.path.getsize(dat_path) if os.path.isfile(dat_path) else -1
        self.centre = survey_centre(self.pings[self.beams[0]])
        self.stale = {b: stale_prefix(self.pings[b], self.centre) for b in self.beams}
        self.late = {b: late_bad(self.pings[b], self.centre, self.stale[b])
                     for b in self.beams}

    # -- what a header would have to say about it --

    @property
    def numrecords(self):
        return max(p.record for b in self.beams for p in self.pings[b]) + 1

    @property
    def recordlens_ms(self):
        return max(p.time_ms for b in self.beams for p in self.pings[b])

    @property
    def linesize(self):
        return max(p.count for b in self.beams for p in self.pings[b]) + HEADER_LEN

    def first_good(self, beam):
        return self.pings[beam][self.stale[beam]]

    def report(self, log=print):
        log(f"{self.name}")
        log(f"  .DAT: " + ("missing" if self.dat_size < 0 else f"{self.dat_size} bytes")
            + ("" if self.dat else "   <- has to be rebuilt"))
        if self.dat:
            log(f"    says: {self.dat['numrecords']} records, "
                f"{self.dat['recordlens_ms'] / 60000:.1f} min, "
                f"linesize {self.dat['linesize']}, name {self.dat['filename']}")
        for beam in self.beams:
            pings = self.pings[beam]
            indexed = self.index_records[beam]
            note = ""
            if indexed != len(pings):
                note = f"   <- indexes {indexed}, {len(pings) - indexed} short"
            log(f"  {beam}: {len(pings)} pings, index {indexed}{note}")
            if self.trailing[beam]:
                log(f"      {self.trailing[beam]} trailing bytes: a record cut off "
                    "mid-write")
        lat, lon = self.centre
        log(f"  survey centre: {lat:.5f}, {lon:.5f}")
        for beam in self.beams:
            n = self.stale[beam]
            if not n:
                continue
            first = self.pings[beam][0]
            good = self.first_good(beam)
            log(f"  {beam}: {n} pings ({good.time_ms / 1000:.1f} s) before the GPS "
                f"caught up, stamped {lat_lon(first.utm_e, first.utm_n)[0]:.5f}, "
                f"{lat_lon(first.utm_e, first.utm_n)[1]:.5f}   <- would set the "
                "projection for the whole survey")
        for beam in self.beams:
            if self.late[beam]:
                log(f"  {beam}: {self.late[beam]} fixes later in the recording are also far"
                    " from the survey. Those are a dropout, not a slow start, and are"
                    " left alone - check the track before trusting them.")
        log(f"  a rebuilt header would say: numrecords={self.numrecords} "
            f"recordlens_ms={self.recordlens_ms} ({self.recordlens_ms / 60000:.1f} min) "
            f"linesize={self.linesize}")

    def healthy(self) -> bool:
        return (self.dat is not None
                and all(self.index_records[b] == len(self.pings[b]) for b in self.beams)
                and not any(self.stale.values()))


# -- Repair ------------------------------------------------------------------

def write_header(survey: Survey, path: str, name: str, kept: dict,
                 position_from=None, log=print) -> bytes:
    """
    Write the .DAT describing [kept] - the pings the new recording will hold.

    The name field holds ten bytes, which is what a recording off the card
    needs and no more, so a repaired copy's longer name would be cut off in
    it. It keeps the name of the survey it came from instead: the header is
    describing that survey either way, and a reader that shows the field gets
    something true rather than something truncated.

    [position_from] is the ping whose fix the header states. That is normally
    the first one being kept, but a header written beside pings that are
    staying put should still say where the survey was rather than repeat a fix
    known to belong to another trip.
    """
    template = template_dat(survey.dat_path)
    fixed = dict(DEFAULT_FIXED)
    if template:
        fixed.update({key: template[1][key] for key in DEFAULT_FIXED})
        log(f"  unknown bytes copied from {os.path.basename(template[0])}")
    else:
        log("  no sibling recording to copy the unknown bytes from; using defaults")

    held = [p for b in kept for p in kept[b]]
    if not held:
        raise SystemExit("Nothing left to write: every ping was rejected.")
    first = position_from or kept[survey.beams[0]][0]
    numrecords = max(p.record for p in held) + 1
    recordlens = max(p.time_ms for p in held)
    linesize = max(p.count for p in held) + HEADER_LEN
    unix_time = start_time(survey.folder, survey.beams, recordlens)

    stated = (name if len(name + '.SON') <= 10 else survey.name)[:6]
    raw = build_dat(fixed, stated + ".SON", unix_time, first.utm_e, first.utm_n,
                    numrecords, recordlens, linesize)
    with open(path, "wb") as fh:
        fh.write(raw)
    log(f"  {os.path.basename(path)}: {numrecords} records, "
        f"{recordlens / 60000:.1f} min, linesize {linesize}, "
        f"start {stamp_of(unix_time)} (local), "
        f"position {lat_lon(first.utm_e, first.utm_n)[0]:.5f}, "
        f"{lat_lon(first.utm_e, first.utm_n)[1]:.5f}")
    return raw


def write_beam(survey: Survey, beam: str, out_folder: str, kept: list, log=print):
    """
    Write out one beam's kept pings, and the index that finds them.

    Copied record by record rather than as one span, because what is kept need
    not be a single stretch of the recording: rejecting a bad patch in the
    middle leaves two. Runs that are contiguous in the source are copied in one
    read, which is the ordinary case and by far the common one.
    """
    if not kept:
        raise SystemExit(f"{beam}: every ping was rejected.")
    source = os.path.join(survey.folder, beam + ".SON")
    son_out = os.path.join(out_folder, beam + ".SON")
    idx_out = os.path.join(out_folder, beam + ".IDX")

    offsets = []
    written = 0
    with open(source, "rb") as src, open(son_out, "wb") as dst:
        run_start = kept[0].offset
        run_end = run_start
        for ping in kept:
            if ping.offset != run_end:
                _copy_span(src, dst, run_start, run_end - run_start)
                written += run_end - run_start
                run_start = ping.offset
            offsets.append(written + (ping.offset - run_start))
            run_end = ping.offset + ping.length
        _copy_span(src, dst, run_start, run_end - run_start)
        written += run_end - run_start

    with open(idx_out, "wb") as fh:
        for ping, offset in zip(kept, offsets):
            fh.write(struct.pack(">II", ping.time_ms, offset))
    dropped = len(survey.pings[beam]) - len(kept)
    log(f"  {beam}: {len(kept)} pings kept"
        + (f", {dropped} dropped" if dropped else "")
        + f", index rebuilt ({len(kept) * 8} bytes)")


def _copy_span(src, dst, start: int, length: int) -> None:
    """Copy [length] bytes from [start], a megabyte at a time."""
    src.seek(start)
    remaining = length
    while remaining > 0:
        chunk = src.read(min(1 << 20, remaining))
        if not chunk:
            break
        dst.write(chunk)
        remaining -= len(chunk)


def fixed_name(out_root: str, survey: "Survey", suffix: str = "_fixed") -> str:
    """
    What to call the repaired copy.

    Fixing something already called <x>_fixed should not leave <x>_fixed_fixed
    behind, so the suffix is replaced rather than stacked. What it must never
    do is land on the recording being read: the writer clears the destination
    before it copies into it, and the destination would be the source.
    """
    stem = survey.name
    if stem.endswith(suffix):
        stem = stem[:-len(suffix)]
    candidate = stem + suffix
    number = 2
    while os.path.normcase(os.path.abspath(os.path.join(out_root, candidate))) \
            == os.path.normcase(os.path.abspath(survey.folder)):
        candidate = f"{stem}{suffix}{number}"
        number += 1
    return candidate


def write_recording(survey: Survey, out_root: str, out_name: str, kept: dict,
                    position_from=None, log=print) -> str:
    """
    Write a whole recording - .DAT, .SON files and indexes - from [kept].

    [kept] is beam -> the pings to keep, in order. This is the one writer: the
    command-line repair and the interactive fixer both come through here, so a
    recording built either way is built the same.
    """
    out_folder = os.path.join(out_root, out_name)
    out_dat = os.path.join(out_root, out_name + ".DAT")
    if os.path.normcase(os.path.abspath(out_folder)) == \
            os.path.normcase(os.path.abspath(survey.folder)):
        raise SystemExit(f"{out_name} is the recording being read; writing there would delete it.")
    if os.path.exists(out_folder):
        shutil.rmtree(out_folder)
    os.makedirs(out_folder)
    log(f"Writing {out_name}:")
    for beam in survey.beams:
        write_beam(survey, beam, out_folder, kept[beam], log=log)
    write_header(survey, out_dat, out_name, kept, position_from=position_from,
                 log=log)
    return out_dat


def repair(path: str, out_dir: str = "", suffix: str = "_fixed",
           keep_start: bool = False, header_only: bool = False, log=print) -> str:
    """
    Repair a recording. Returns the path of the .DAT to hand to PINGMapper.

    With --header-only the missing .DAT is written beside the original and
    nothing else is touched. Otherwise a whole repaired recording is written
    alongside, and the original is left exactly as it came off the card.
    """
    name, folder, dat_path = resolve(path)
    survey = Survey(name, folder, dat_path)
    survey.report(log)
    log("")

    if survey.healthy():
        log("Nothing to repair: the header is there, the index is complete, and "
            "every ping has a believable position.")
        return dat_path

    if header_only:
        if survey.dat is not None:
            raise SystemExit(f"{os.path.basename(dat_path)} already holds a header; "
                             "repair the recording instead of overwriting it.")
        log(f"Writing the missing header beside the recording:")
        whole = {b: survey.pings[b] for b in survey.beams}
        write_header(survey, dat_path, name, whole,
                     position_from=survey.first_good(survey.beams[0]), log=log)
        if any(survey.stale.values()):
            log("")
            log("  The header now says where the survey was, but the first pings "
                "still carry" + os.linesep +
                "  the position from the unit's last outing, and PINGMapper takes "
                "the survey's" + os.linesep +
                "  UTM zone from the first ping it reads. Run a full repair before "
                "processing this.")
        return dat_path

    out_root = out_dir or os.path.dirname(folder)
    out_name = fixed_name(out_root, survey, suffix)
    drop = {b: (0 if keep_start else survey.stale[b]) for b in survey.beams}
    kept = {b: survey.pings[b][drop[b]:] for b in survey.beams}
    out_dat = write_recording(survey, out_root, out_name, kept, log=log)
    out_folder = os.path.join(out_root, out_name)

    # Read back what was written rather than trusting that it was: a repair that
    # cannot be walked is worse than the break it was meant to fix.
    check = Survey(out_name, out_folder, out_dat)
    ok = check.healthy()
    log("")
    log(f"Checked {out_name}: " + ("walks clean, index complete, every fix on site."
                                   if ok else "STILL BROKEN - see above."))
    if not ok:
        check.report(log)
        raise SystemExit("The repaired copy did not come out right; nothing was "
                         "changed in the original.")
    return out_dat


# -- Self-check --------------------------------------------------------------

def _fake_recording(folder: str, name: str, pings: int = 40, stale: int = 5):
    """A miniature Humminbird recording: three beams, a few pings, no header."""
    os.makedirs(os.path.join(folder, name), exist_ok=True)
    here_e, here_n = -10181000, 4568000          # a lake in Missouri
    away_e, away_n = -12404960, 3025785          # where the unit was last switched off
    for beam_i, beam in enumerate(BEAMS):
        son = os.path.join(folder, name, beam + ".SON")
        with open(son, "wb") as fh:
            for i in range(pings):
                count = 100 + (i % 7)
                utm_e, utm_n = (away_e, away_n) if i < stale else (here_e + i, here_n)
                head = bytearray(HEADER_LEN)
                head[0:4] = MAGIC
                struct.pack_into(">I", head, 5, i * 3 + beam_i)
                struct.pack_into(">I", head, 10, i * 40)
                struct.pack_into(">i", head, 15, utm_e)
                struct.pack_into(">i", head, 20, utm_n)
                # A depth that moves a little and spikes nowhere, so the fake
                # recording exercises anything reading soundings without
                # tripping the flags that look for jumps in them.
                struct.pack_into(">I", head, 35, 60 + (i % 10))
                struct.pack_into(">I", head, 62, count)
                fh.write(bytes(head))
                fh.write(bytes(count))
        # A truncated index, as a recording that lost power actually has.
        with open(os.path.join(folder, name, beam + ".IDX"), "wb") as fh:
            fh.write(b"\x00" * 8 * (pings // 2))
    open(os.path.join(folder, name + ".DAT"), "wb").close()


def selftest(log=print) -> bool:
    import tempfile

    ok = True

    def check(what, passed, detail=""):
        nonlocal ok
        ok = ok and passed
        log(f"  {'PASS' if passed else 'FAIL'}  {what}{'  ' + detail if detail else ''}")

    with tempfile.TemporaryDirectory() as tmp:
        _fake_recording(tmp, "R09999", pings=40, stale=5)
        survey = Survey("R09999", os.path.join(tmp, "R09999"),
                        os.path.join(tmp, "R09999.DAT"))
        check("empty .DAT is seen as missing", survey.dat is None)
        check("all pings walked from the chain", len(survey.pings["B001"]) == 40,
              f"{len(survey.pings['B001'])}")
        check("short index noticed", survey.index_records["B001"] == 20)
        check("stale prefix found", survey.stale["B001"] == 5,
              f"{survey.stale['B001']}")
        check("record count from the pings", survey.numrecords == 40 * 3 - 1 + 1,
              f"{survey.numrecords}")
        check("largest record measured",
              survey.linesize == 100 + 6 + HEADER_LEN, f"{survey.linesize}")

        out = repair(os.path.join(tmp, "R09999.DAT"), log=lambda *_: None)
        check("repaired recording written", os.path.isfile(out))
        fixed = Survey("R09999_fixed", os.path.join(tmp, "R09999_fixed"), out)
        check("header reads back", fixed.dat is not None)
        check("stale pings gone", not any(fixed.stale.values()))
        check("the rest kept", len(fixed.pings["B001"]) == 35,
              f"{len(fixed.pings['B001'])}")
        check("index matches the file",
              all(fixed.index_records[b] == len(fixed.pings[b]) for b in fixed.beams))
        check("header names the survey it came from",
              fixed.dat["filename"] == "R09999.SON", fixed.dat["filename"])
        check("header position is the first good fix",
              fixed.dat["utm_e"] == -10181000 + 5, f"{fixed.dat['utm_e']}")
        check("repaired recording reports healthy", fixed.healthy())
        check("original left alone",
              os.path.getsize(os.path.join(tmp, "R09999", "B001.SON")) >
              os.path.getsize(os.path.join(tmp, "R09999_fixed", "B001.SON")))

        # A recording with nothing wrong should be left alone.
        _fake_recording(tmp, "R09998", pings=12, stale=0)
        whole = Survey("R09998", os.path.join(tmp, "R09998"),
                       os.path.join(tmp, "R09998.DAT"))
        write_header(whole, os.path.join(tmp, "R09998.DAT"), "R09998",
                     {b: whole.pings[b] for b in whole.beams},
                     log=lambda *_: None)
        with open(os.path.join(tmp, "R09998", "B001.IDX"), "wb") as fh:
            for ping in whole.pings["B001"]:
                fh.write(struct.pack(">II", ping.time_ms, ping.offset))
        for beam in ("B002", "B003"):
            with open(os.path.join(tmp, "R09998", beam + ".IDX"), "wb") as fh:
                for ping in whole.pings[beam]:
                    fh.write(struct.pack(">II", ping.time_ms, ping.offset))
        again = Survey("R09998", os.path.join(tmp, "R09998"),
                       os.path.join(tmp, "R09998.DAT"))
        check("a sound recording is left alone", again.healthy())

    log("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    return ok


# -- CLI ---------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command")

    p_look = sub.add_parser("inspect", help="say what is wrong with a recording")
    p_look.add_argument("recording")

    p_fix = sub.add_parser("repair", help="write a repaired copy beside it")
    p_fix.add_argument("recording")
    p_fix.add_argument("--out", default="", help="where to write it")
    p_fix.add_argument("--suffix", default="_fixed", help="name of the copy")
    p_fix.add_argument("--keep-start", action="store_true",
                       help="keep the pings from before the GPS had a fix")
    p_fix.add_argument("--header-only", action="store_true",
                       help="only write the missing .DAT, in place")

    sub.add_parser("selftest", help="check the repair against a made-up recording")

    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.command == "inspect":
        name, folder, dat = resolve(args.recording)
        Survey(name, folder, dat).report()
    elif args.command == "repair":
        out = repair(args.recording, out_dir=args.out, suffix=args.suffix,
                     keep_start=args.keep_start, header_only=args.header_only)
        if args.header_only:
            print(f"{os.linesep}Header written: {out}")
        else:
            print(f"{os.linesep}Process this one: {out}")
    elif args.command == "selftest":
        raise SystemExit(0 if selftest() else 1)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
