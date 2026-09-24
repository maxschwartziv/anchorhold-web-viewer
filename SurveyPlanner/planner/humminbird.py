"""
Depth soundings straight out of a Humminbird recording.

A Helix recording is `R000xx.DAT` (64 bytes) beside a folder of the same name
holding one `.SON` per beam. Every ping in a `.SON` starts with the magic
`C0 DE AB 21` and a fixed header carrying, among other things, the position
and the depth the unit read at that ping - so the soundings can be had by
walking the record chain, with no sonar decoding and no PINGMapper.

The layout and the stale-fix rule are the ones AnchorHold's
`pipeline/repair_humminbird.py` uses (from pingverter's structs); they are
repeated here so this repository keeps standing alone.

**Depth is below the transducer**, in tenths of a metre, and 0 when the unit
had no bottom lock. **Position is Humminbird's own** spheroid easting and
northing, and until the GPS gets a fix the unit stamps pings with wherever it
was last switched off - possibly another continent - so fixes far from the
recording's centre are dropped.
"""

from __future__ import annotations

import math
import os
import struct

MAGIC = b"\xc0\xde\xab\x21"
HEADER_LEN = 67                 # Helix ping header
R_EARTH = 6378388.0             # International 1924, as pingverter uses
STALE_DEGREES = 0.5             # farther than this from the centre is not a fix
# 2D downward beams first: they are what the depth was read from, and they are
# far smaller than the side scan files, which carry the same depth anyway.
BEAMS = ("B001", "B000", "B002", "B003")


class RecordingError(ValueError):
    pass


def lat_lon(utm_e: int, utm_n: int):
    """A Humminbird easting/northing as degrees, the way pingverter does it."""
    lat = math.atan(math.tan(math.atan(math.exp(utm_n / R_EARTH)) * 2.0
                             - 1.570796326794897) * 1.0067642927) * 57.295779513082302
    lon = (utm_e * 57.295779513082302) / R_EARTH
    return lat, lon


def resolve(path: str):
    """(name, sonar folder) for a recording named by its .DAT or its folder."""
    path = os.path.abspath(path.rstrip("/\\"))
    if os.path.isdir(path):
        folder = path
    else:
        folder = os.path.join(os.path.dirname(path),
                              os.path.splitext(os.path.basename(path))[0])
    if not os.path.isdir(folder):
        raise RecordingError(
            "No sonar folder beside " + os.path.basename(path) + ". A recording is"
            " a .DAT plus a folder of the same name holding B001.SON and friends;"
            " if they were separated, put them back together.")
    return os.path.basename(folder), folder


def soundings(path: str):
    """
    (lons, lats, depths_m, info) for every ping with a fix and a bottom.

    Reads one beam - the first of B001, B000, B002, B003 present - by
    following the record chain, which is also what survives a recording the
    unit never finished writing.
    """
    name, folder = resolve(path)
    son = next((os.path.join(folder, b + ".SON") for b in BEAMS
                if os.path.isfile(os.path.join(folder, b + ".SON"))), None)
    if son is None:
        raise RecordingError(folder + " holds no B00*.SON.")
    with open(son, "rb") as fh:
        data = fh.read()
    raw = []
    at, size = 0, len(data)
    while at + HEADER_LEN <= size and data[at:at + 4] == MAGIC:
        utm_e, = struct.unpack_from(">i", data, at + 15)
        utm_n, = struct.unpack_from(">i", data, at + 20)
        depth_dm, = struct.unpack_from(">I", data, at + 35)
        count, = struct.unpack_from(">I", data, at + 62)
        if at + HEADER_LEN + count > size:
            break                               # cut off mid-write
        raw.append((utm_e, utm_n, depth_dm))
        at += HEADER_LEN + count
    if not raw:
        raise RecordingError(os.path.basename(son) + " has no pings.")

    fixes = [lat_lon(e, n) for e, n, _ in raw]
    # the centre from the second half, when the GPS is certainly up
    tail = fixes[len(fixes) // 2:]
    c_lat = sorted(f[0] for f in tail)[len(tail) // 2]
    c_lon = sorted(f[1] for f in tail)[len(tail) // 2]
    lons, lats, depths = [], [], []
    stale = no_bottom = 0
    for (e, n, dm), (lat, lon) in zip(raw, fixes):
        if (e == 0 and n == 0) or abs(lat - c_lat) > STALE_DEGREES or abs(lon - c_lon) > STALE_DEGREES:
            stale += 1
            continue
        if dm <= 0:
            no_bottom += 1
            continue
        lons.append(lon)
        lats.append(lat)
        depths.append(dm / 10.0)
    info = {"name": name, "beam": os.path.basename(son)[:4], "pings": len(raw),
            "stale": stale, "no_bottom": no_bottom, "used": len(depths)}
    if not depths:
        raise RecordingError(name + ": no ping has both a fix and a bottom.")
    return lons, lats, depths, info
