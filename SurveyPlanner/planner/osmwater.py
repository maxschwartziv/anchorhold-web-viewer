"""
Water bodies from OpenStreetMap, for when the NHD service will not answer.

The NHD is the better dataset where it has an opinion - it is the national
hydrography layer, surveyed, with reservoir arms drawn properly. But it was
retired on 1 October 2023, it is published rather than maintained, and it
goes down: both of its layers timed out at 45 seconds the day this was
written, which is a planner that cannot start.

So this is the fallback, not the replacement. OpenStreetMap is a different
bargain: it answers in a second or two rather than tens, it covers the whole
world rather than the United States, and its outlines are drawn by whoever
drew them. For a lake you are about to survey that is usually fine, because
you are going to look at the outline on satellite imagery before running
anything anyway.

Measured the day this was written:

    Indian Hills Lake, MO    1.6 s   431-point way
    Bull Shoals Lake, AR     3.1 s   17,759-point relation, named
    NHD, the same bounds     45 s    timed out, both layers

What it does not do is open coast. Sea shoreline in OSM is `natural=coastline`,
a global network of unclosed ways rather than polygons, and asking for it over
a coastal bbox pulls ways thousands of kilometres long - 504 Gateway Timeout
is what comes back. The NHD does not cover Baja either, so this is not a
regression; it is the same boundary both sources have.

Returns the same shape planner.shoreline uses: {name, kind, acres, rings},
rings being [outer, inner, inner...] in lon/lat.
"""

from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request

# The main instance first, then a mirror. Overpass rate-limits a client that
# hammers it - 429 - and the mirror is a different queue rather than a faster
# one, so it is a second chance and not a way around the limit.
MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

USER_AGENT = "AnchorHold-SurveyPlanner/1.0 (+https://github.com/maxschwartziv)"

# natural=water covers lakes, reservoirs and ponds; waterway=riverbank is the
# older tagging for a river drawn as an area, which is still widespread.
# Coastline is deliberately absent - see the note above.
QUERY = """
[out:json][timeout:{timeout}];
(
  way["natural"="water"]({s},{w},{n},{e});
  relation["natural"="water"]({s},{w},{n},{e});
  way["waterway"="riverbank"]({s},{w},{n},{e});
);
out geom;
"""

SQM_TO_ACRES = 0.000247105


class OsmWaterError(RuntimeError):
    pass


def fetch(lon: float, lat: float, radius_mi: float = 1.0, timeout: int = 25,
          attempts: int = 3, cancel=None):
    """
    Water bodies near a point, nearest first. Raises OsmWaterError.

    Erratic rather than slow, exactly as the NHD is. Measured on one bbox,
    three requests to the same endpoint one after the other: 504 in 9.5 s,
    then 1.5 s and an answer, then 504 in 8.6 s. The failures come back
    faster than the successes, so they are a refusal and not a queue - and
    the fix is a short leash and another go, not a longer timeout. A stall
    that a retry answers in a second is not worth waiting sixty for.

    Mirrors are cycled rather than ranked: whichever is sulking, the other
    may not be. `cancel` is a threading.Event, checked between attempts.
    """
    deg_lat = radius_mi / 69.0
    deg_lon = deg_lat / max(0.1, abs(math.cos(math.radians(lat))))
    query = QUERY.format(timeout=timeout, s=lat - deg_lat, w=lon - deg_lon,
                         n=lat + deg_lat, e=lon + deg_lon)

    errors = []
    payload = None
    for attempt in range(attempts):
        for url in MIRRORS:
            if cancel is not None and cancel.is_set():
                return []
            try:
                data = urllib.parse.urlencode({"data": query}).encode()
                request = urllib.request.Request(
                    url, data=data, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except Exception as exc:
                errors.append("%s try %d: %s"
                              % (urllib.parse.urlparse(url).netloc,
                                 attempt + 1, exc))
        if payload is not None:
            break
    if payload is None:
        raise OsmWaterError("Could not reach OpenStreetMap. "
                            + "; ".join(errors[-4:]))

    bodies = []
    for element in payload.get("elements", []):
        rings = (_relation_rings(element) if element.get("type") == "relation"
                 else _way_rings(element))
        if not rings:
            continue
        tags = element.get("tags") or {}
        bodies.append({
            "name": tags.get("name") or "(unnamed water)",
            "kind": _kind_of(tags),
            # OSM carries no area attribute, so it is measured off the ring.
            # Only used for sorting and for telling two candidates apart.
            "acres": _acres(rings[0]),
            "rings": rings,
            "source": "OpenStreetMap",
        })
    return bodies


def _kind_of(tags) -> str:
    water = (tags.get("water") or "").lower()
    if water in ("river", "canal") or tags.get("waterway") == "riverbank":
        return "river"
    return "lake"


def _way_rings(element):
    """A closed way is one outer ring. An unclosed one is not a water body."""
    geom = element.get("geometry") or []
    ring = [(float(p["lon"]), float(p["lat"])) for p in geom
            if p.get("lon") is not None]
    if len(ring) < 4:
        return []
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return [ring]


def _relation_rings(element):
    """
    A multipolygon relation: outer ways stitched into a ring, inner ones kept.

    The inner rings are the point of doing this properly rather than taking
    the longest outer way and stopping - an island in a lake is an inner ring,
    and the planner turns every inner ring into a no-go area. Lose them and
    the survey plans lines straight over the island.

    Members arrive as fragments in no particular order, so same-role ways are
    joined end to end until they close.
    """
    outer, inner = [], []
    for member in element.get("members") or []:
        geom = member.get("geometry") or []
        line = [(float(p["lon"]), float(p["lat"])) for p in geom
                if p.get("lon") is not None]
        if len(line) < 2:
            continue
        (inner if member.get("role") == "inner" else outer).append(line)

    outer_rings = _stitch(outer)
    if not outer_rings:
        return []
    # The biggest outer ring is the water body; any others are separate ponds
    # in the same relation and are left to their own entry.
    biggest = max(outer_rings, key=_ring_area)
    return [biggest] + [r for r in _stitch(inner) if _ring_area(r) > 0]


def _stitch(lines):
    """Join fragments end to end into closed rings; drop what will not close."""
    pending = [list(line) for line in lines]
    rings = []
    while pending:
        ring = pending.pop(0)
        joined = True
        while joined and ring[0] != ring[-1]:
            joined = False
            for i, other in enumerate(pending):
                if ring[-1] == other[0]:
                    ring += other[1:]
                elif ring[-1] == other[-1]:
                    ring += other[::-1][1:]
                elif ring[0] == other[-1]:
                    ring = other[:-1] + ring
                elif ring[0] == other[0]:
                    ring = other[::-1][:-1] + ring
                else:
                    continue
                pending.pop(i)
                joined = True
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def _ring_area(ring) -> float:
    """Shoelace area in square degrees - for ordering, not for reporting."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _acres(ring) -> float:
    """The ring's area, with longitude scaled for latitude so it means acres."""
    if len(ring) < 4:
        return 0.0
    lat0 = sum(p[1] for p in ring) / len(ring)
    scale = math.cos(math.radians(lat0))
    m_per_deg = 111_320.0
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        total += (x1 * scale) * y2 - (x2 * scale) * y1
    return abs(total) / 2.0 * (m_per_deg ** 2) * SQM_TO_ACRES
