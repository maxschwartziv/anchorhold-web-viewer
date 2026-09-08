"""
Places the boat must not go.

Two kinds, and they are not equally trustworthy.

**Islands** come from the shoreline polygon itself - every interior ring is
land the survey has to keep away from, exactly as the outer ring is. Nothing is
being guessed here; if the NHD outline is right, these are right.

**Docks, piers and marinas** come from OpenStreetMap, and are a suggestion.
A mapped pier is somewhere a structure was mapped at some point. It may have
been removed, it may have grown, and the great majority of private docks on a
small lake are not in OSM at all. Treat the suggestions as a starting list to
correct, never as a survey of what is in the water.

Both are returned as shapely polygons in local feet with a margin already
applied, because the thing to avoid is not the structure but the water around
it - the pilings, the moored boats and the shallow ground they sit in.
"""

from __future__ import annotations

DOCK_MARGIN_FT = 60.0
ISLAND_MARGIN_FT = 0.0          # the setback already applies to island shores


def from_islands(poly, margin_ft: float = ISLAND_MARGIN_FT) -> list:
    """
    One no-go polygon per island in the shoreline.

    The setback already holds the boat off an island's shore, because an island
    is a hole in the polygon and shrinking the water grows the hole. This is
    for when that is not enough - a shoal running off an island, say - so the
    margin defaults to nothing and is there to be raised.
    """
    from shapely.geometry import Polygon

    out = []
    for ring in poly.interiors:
        island = Polygon(ring)
        if not island.is_valid:
            island = island.buffer(0)
        if island.is_empty:
            continue
        out.append(island.buffer(margin_ft) if margin_ft > 0 else island)
    return out


def from_context(context: dict, frame, margin_ft: float = DOCK_MARGIN_FT,
                 poly=None) -> list:
    """
    No-go polygons around every dock, pier and marina OSM knows about.

    `context` is what `basemap.fetch_context` returned. Anything outside the
    water is dropped when `poly` is given: a boatyard set back from the bank is
    not in the boat's way, and cluttering the map with it makes the ones that
    are matter less.
    """
    from shapely.geometry import LineString, Point, Polygon

    out = []
    for key in ("docks", "slipways"):
        for item in context.get(key) or []:
            pts = [frame.to_ft(lon, lat) for lon, lat in item["coords"]]
            if not pts:
                continue
            if len(pts) == 1:
                shape = Point(pts[0])
            elif len(pts) > 3 and pts[0] == pts[-1]:
                shape = Polygon(pts)
                if not shape.is_valid:
                    shape = shape.buffer(0)
            else:
                shape = LineString(pts)
            zone = shape.buffer(margin_ft)
            if zone.is_empty:
                continue
            if poly is not None:
                zone = zone.intersection(poly)
                if zone.is_empty or zone.area < 400.0:
                    continue
            out.append({"name": item.get("name") or key[:-1],
                        "kind": key[:-1], "geom": zone})
    return out


def suggest(poly, context=None, frame=None, dock_margin_ft: float = DOCK_MARGIN_FT,
            island_margin_ft: float = ISLAND_MARGIN_FT) -> list:
    """
    Everything worth keeping the boat out of, as {name, kind, geom}.

    Deliberately not applied on its own: the caller shows these and the person
    who has actually been to the lake decides. Nothing in OSM knows which dock
    has a cable across it.
    """
    out = [{"name": "island " + str(i + 1), "kind": "island", "geom": geom}
           for i, geom in enumerate(from_islands(poly, island_margin_ft))]
    if context and frame is not None:
        out += from_context(context, frame, dock_margin_ft, poly)
    return out
