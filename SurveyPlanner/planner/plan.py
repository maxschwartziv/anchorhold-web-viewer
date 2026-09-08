"""
Turning a lake outline into lines a boat can actually run.

Three things shape every plan here, and each of them came from a survey that
went wrong without it:

  Straight lines.   Side scan assumes the boat travelled in a straight line
                    while the swath was built. A curved track smears the image,
                    and the processing throws those pings away as a turn, so a
                    "clever" curved path surveys nothing.
  A setback.        The boat has to stay clear of the bank, and the margin has
                    to survive GPS error, not just arithmetic. Planning to
                    exactly the limit puts you inside it on a bad fix.
  Water transits.   The straight hop between the end of one line and the start
                    of the next will cross a peninsula if nothing stops it.
"""

from __future__ import annotations

import math

from .geometry import axis_bearing_deg, polyline_length_ft, turn_angles_deg

MILES_TO_FEET = 5280.0
# A stop so a setting that cannot work - two minutes a day on a large lake -
# ends with a message rather than running until the window stops responding.
MAX_DAYS = 400
# The routing raster is 12 ft cells over the whole navigable area, and
# least-cost pathing wants a float64 cost array the same shape - eight
# bytes a cell, allocated fresh for every route. A 60-mile river came to
# 330 million cells: a third of a gigabyte for the mask and 2.6 GB per
# route. That does not fail, it thrashes, which is what a hang is.
MAX_RASTER_CELLS = 30_000_000


class PlanSettings:
    """Everything a person might reasonably want to change."""

    def __init__(self, **kw):
        self.spacing_ft = kw.get("spacing_ft", 40.0)
        self.setback_ft = kw.get("setback_ft", 50.0)
        self.setback_margin_ft = kw.get("setback_margin_ft", 6.0)
        self.speed_mph = kw.get("speed_mph", 3.0)
        self.day_hours = kw.get("day_hours", 2.0)
        self.day_min_hours = kw.get("day_min_hours", 1.5)
        self.bearing_deg = kw.get("bearing_deg", None)      # None = lake's long axis
        self.orthogonal = kw.get("orthogonal", False)
        self.orthogonal_spacing_ft = kw.get("orthogonal_spacing_ft", 80.0)
        self.min_line_ft = kw.get("min_line_ft", 80.0)
        # Line of sight and square blocks travel together: blocks are what get
        # paired with a station, and a station is what makes sight meaningful.
        self.require_line_of_sight = kw.get("require_line_of_sight", True)
        self.square_blocks = kw.get("square_blocks", True)
        self.sight_range_ft = kw.get("sight_range_ft", 0.0)   # 0 = land only
        # Survey less than all of it. The water dropped is the water
        # furthest from a launch, because that is where the trip out
        # costs more than the ground is worth.
        self.coverage_pct = kw.get("coverage_pct", 100.0)
        self.block_fraction = kw.get("block_fraction", 0.33)
        # Places the boat must not go: docks, swim areas, the ground
        # around an island, anything the person who knows the lake draws.
        # Shapely polygons in local feet.
        self.no_go = list(kw.get("no_go") or [])
        # How far to stay off a no-go area. Separate from the shore
        # setback because the reasons differ: the bank is about depth,
        # a dock is about the things around it you cannot see - mooring
        # lines, cables, a swim ladder, a boat on the far side of it.
        self.no_go_margin_ft = kw.get("no_go_margin_ft", 25.0)
        # A first transect that follows the shore, as close in as the
        # setback and the turn filter allow, before the grid starts.
        self.shore_pass = kw.get("shore_pass", False)
        # The sonar filter discards pings through a turn. 50 degrees
        # over 10 m is the threshold it uses, so it is the threshold a
        # shore-following track has to stay inside to be worth running.
        self.turn_limit_deg = kw.get("turn_limit_deg", 50.0)
        self.turn_sample_ft = kw.get("turn_sample_ft", 32.808)   # 10 m
        # How much of a detour near-shore work is worth. Priced, not
        # forced, so it gives way when the detour is genuinely expensive.
        # Measured on Indian Lake, distance from the bank of the first
        # line of a block against the last: 155/121 ft at zero, 138/147
        # at one, 87/203 at five. Five is what actually puts the bank
        # first, and costs 1.3% more driving to do it.
        self.shore_first_weight = kw.get("shore_first_weight", 5.0)

    def line_density(self) -> float:
        """
        Feet of survey line per square foot of water.

        One pass at `spacing` lays 1/spacing. The orthogonal pass lays its
        own set on top at its own spacing - a second complete sweep of the
        same ground, not a discount on the first. Pricing a block without
        it is what let a day with the 90-degree pass ticked run half again
        over the hours allowed: the blocks were sized for one sweep and
        then given two.
        """
        density = 1.0 / max(self.spacing_ft, 1.0)
        if self.orthogonal:
            density += 1.0 / max(self.orthogonal_spacing_ft, 1.0)
        return density

    @property
    def build_setback_ft(self) -> float:
        """Where lines are actually drawn: the limit plus a margin to hold it."""
        return self.setback_ft + self.setback_margin_ft

    def as_dict(self) -> dict:
        return {k: v for k, v in vars(self).items()}


def navigable_area(poly, settings: PlanSettings, roi=None):
    """
    The water shrunk to where the boat is allowed to be, and to what was asked
    for.

    The setback is a hard rule and applies whatever the region of interest is:
    an ROI drawn across the bank does not license running the boat aground, so
    it is intersected after the shrink, never instead of it. No-go areas come
    out last and unconditionally, for the same reason, each grown by its own
    margin first - what has to be avoided is not the dock but the water
    around it.
    """
    from shapely.geometry import MultiPolygon

    area = poly.buffer(-settings.build_setback_ft)
    if roi is not None and not area.is_empty:
        area = area.intersection(roi)
    for zone in settings.no_go:
        if area.is_empty:
            break
        margin = settings.no_go_margin_ft
        area = area.difference(zone.buffer(margin) if margin > 0 else zone)
    if area.is_empty:
        return area
    if isinstance(area, MultiPolygon):
        keep = [g for g in area.geoms if g.area > 4000]
        if not keep:
            return max(area.geoms, key=lambda p: p.area)
        return MultiPolygon(keep) if len(keep) > 1 else keep[0]
    return area


def shore_track(region, settings: PlanSettings) -> list:
    """
    A drivable track following the shore, as close in as the rules allow.

    The navigable boundary is the shoreline already offset inward by the
    setback, and it is far too crinkly to drive: at Indian Lake it turns
    160 degrees inside 10 m, against a filter that discards everything
    over 50. Simplifying it does not help - chords get shorter but the
    vertices stay sharp, and it measured 163 degrees at every tolerance
    tried.

    What works is a morphological opening: erode the water by a radius
    and dilate it back. That rounds every corner to at least that radius
    and drops arms narrower than twice it, and the radius follows from
    the rule rather than being tuned - a heading change of `limit` over
    `sample` is an arc of radius sample/limit, or 37.6 ft for 50 degrees
    over 10 m. Measured at 40 ft: 49 degrees, 99.2% of the water kept.

    An opening can only shrink the region, so the setback survives it by
    construction. Coves narrower than the boat can turn in are left for
    the grid.
    """
    from shapely.geometry import MultiPolygon

    limit = max(5.0, settings.turn_limit_deg)
    sample = max(1.0, settings.turn_sample_ft)
    radius = sample / math.radians(limit)

    for attempt in range(6):
        opened = region.buffer(-radius).buffer(radius)
        if opened.is_empty:
            return []
        parts = (opened.geoms if isinstance(opened, MultiPolygon)
                 else [opened])
        rings = []
        for part in parts:
            if part.area < 4000:
                continue
            rings.append(list(part.exterior.coords))
            rings.extend(list(hole.coords) for hole in part.interiors)
        if not rings:
            return []
        if max(worst_turn_deg(r, sample) for r in rings) <= limit:
            return rings
        # The arc radius is the right answer for a smooth curve and a
        # little optimistic for a rasterised one. Widen and try again.
        radius *= 1.25
    return rings


def worst_turn_deg(coords, sample_ft: float) -> float:
    """Biggest heading change between consecutive steps of `sample_ft`."""
    from shapely.geometry import LineString

    if len(coords) < 3:
        return 0.0
    line = LineString(coords)
    if line.length < sample_ft * 3:
        return 0.0
    # Round the step count up, not down: int() makes the last step longer
    # than the interval being tested, and a heading change measured over
    # 42 ft is not the one the 10 m rule is about. Short legs read 57
    # degrees that way against a 50 degree limit they were inside.
    n = max(3, math.ceil(line.length / sample_ft))
    pts = [line.interpolate(i / n, normalized=True).coords[0]
           for i in range(n + 1)]
    return max(turn_angles_deg(pts) or [0.0])


def shore_bearing_deg(tracks, min_agreement: float = 0.0):
    """
    The direction the shore mostly runs in, weighted by length.

    So the first straight transect can be laid roughly parallel to the
    curved one beside it. Bearings are doubled before averaging and halved
    after, because a line has no front: due north and due south are the
    same direction, and averaged raw they cancel to nothing.

    `min_agreement` guards against averaging a shape that has no direction.
    The resultant's length over the total is how much the stretches agree -
    one for a straight bank, zero for a bank that doubles back on itself -
    and below the threshold this returns None rather than a number. A day
    holding two short stretches at right angles produced a mean across both
    and laid its grid 77 degrees off its own bank.
    """
    x = y = total = 0.0
    for coords in tracks:
        for a, b in zip(coords, coords[1:]):
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            if length < 1e-6:
                continue
            angle = 2.0 * math.atan2(b[0] - a[0], b[1] - a[1])
            x += length * math.cos(angle)
            y += length * math.sin(angle)
            total += length
    if total < 1e-9:
        return None if min_agreement > 0 else 0.0
    if math.hypot(x, y) / total < min_agreement:
        return None
    return math.degrees(math.atan2(y, x)) / 2.0 % 180.0


def chop_track(coords, piece_ft: float = 1200.0) -> list:
    """
    Cut a long track into consecutive pieces, keeping every bend.

    The day machinery works in legs, so the perimeter has to arrive as
    legs. Cutting at vertices rather than resampling keeps the curve the
    smoothing produced instead of quietly straightening it, and
    consecutive pieces share an endpoint so the hop between them is zero.
    """
    if len(coords) < 2:
        return []
    pieces, current, run = [], [coords[0]], 0.0
    for a, b in zip(coords, coords[1:]):
        current.append(b)
        run += math.hypot(b[0] - a[0], b[1] - a[1])
        if run >= piece_ft:
            pieces.append(current)
            current, run = [b], 0.0
    if len(current) > 1:
        if pieces and run < piece_ft * 0.25:
            pieces[-1].extend(current[1:])   # a stub is not a leg
        else:
            pieces.append(current)
    return pieces


# What building a plan costs, fitted to seven runs across a spread of
# settings rather than guessed: 3 + 1.3 x days seconds, plus a little
# for the raster. The first guesses here were half the real figures.
SECONDS_BASE = 3.0
SECONDS_PER_MILLION_CELLS = 2.0
SECONDS_PER_DAY = 1.3
# Survey line is not all a day does. Hops between lines and blocks, and
# the shuffling that keeps days above the minimum, added about a third
# again across those runs - estimates without it came out 12 to 32%
# short of the day count every time.
WORK_OVERHEAD = 1.3


def estimate(poly, settings: PlanSettings, roi=None, access_points=None,
             frame=None) -> dict:
    """
    Roughly how big a plan this will be, and how long it will take.

    Worked out from areas and lengths alone - no raster, no routing - so
    it costs a buffer operation rather than the minutes the real thing
    takes. That is the whole point: the number is only useful if it
    arrives before the wait it is describing.

    It is an estimate and reads like one. The transit is the part that
    cannot be known without routing, and it is the part that varies most
    - a launch at the wrong end of a reservoir can double a day.
    """
    out = {"error": None, "line_mi": 0.0, "days": 0, "seconds": 0.0}
    region = navigable_area(poly, settings, roi)
    if region.is_empty:
        out["error"] = "nothing inside the setback"
        return out
    too_big = oversized(region)
    if too_big:
        out["error"] = too_big
        return out

    work_ft = region.area * settings.line_density()
    if settings.shore_pass:
        # The transect follows the navigable boundary once, less what
        # smoothing drops. Measured at Indian Lake: 5.2 of 5.9 miles.
        work_ft += _boundary_length(region) * 0.88
    out["line_mi"] = work_ft / MILES_TO_FEET
    work_ft *= WORK_OVERHEAD

    budget_ft = settings.day_hours * settings.speed_mph * MILES_TO_FEET
    trip_ft = _typical_round_trip(region, access_points, frame)
    usable = budget_ft - trip_ft
    if usable <= budget_ft * 0.1:
        out["error"] = (
            "The run out and home would use most of a "
            + format(settings.day_hours, "g") + " hour day. Mark an "
            "access point nearer the water, or allow longer days."
        )
        return out
    out["days"] = max(1, int(math.ceil(work_ft / usable)))
    out["round_trip_mi"] = trip_ft / MILES_TO_FEET

    minx, miny, maxx, maxy = _raster_area(poly, region).bounds
    cells = ((maxx - minx) / 12.0 + 2) * ((maxy - miny) / 12.0 + 2)
    out["cells"] = cells
    out["seconds"] = (SECONDS_BASE
                      + cells / 1e6 * SECONDS_PER_MILLION_CELLS
                      + out["days"] * SECONDS_PER_DAY)
    if settings.require_line_of_sight:
        # Sight discards whatever no marked position can see, and how
        # much that is cannot be known without computing the viewsheds
        # - the very thing this is trying to avoid doing. Say so rather
        # than quietly being wrong: it was out by a factor of two here.
        out["caveat"] = ("line of sight will drop whatever cannot be"
                         " watched, so expect fewer days than this")
    return out


def _boundary_length(region) -> float:
    """How far it is round the navigable water, islands included."""
    from shapely.geometry import MultiPolygon

    parts = region.geoms if isinstance(region, MultiPolygon) else [region]
    return sum(p.exterior.length + sum(h.length for h in p.interiors)
               for p in parts)


def _typical_round_trip(region, access_points, frame) -> float:
    """
    A day's run out and home, guessed from the launches.

    Straight line to the middle of the water and back, with a quarter
    added for going round things. The real figure is routed and varies
    by day; this is only trying to be the right size.
    """
    if not access_points or frame is None:
        return 0.0
    middle = region.representative_point()
    here = (middle.x, middle.y)
    nearest = min(math.dist(frame.to_ft(*p["lonlat"]), here)
                  for p in access_points)
    return nearest * 2.0 * 1.25


def _raster_area(poly, region, margin_ft: float = 2640.0):
    """
    The water the routing grid has to cover.

    The region, plus half a mile of the waterbody around it so that
    line of sight and a route round a headland still see the land just
    outside. Not the whole waterbody: the grid is 12 ft cells over
    whatever it is given, and building it over a sixty-mile river asked
    for 4.9 GB - which meant drawing a region of interest, the one
    remedy on offer, did not actually make the plan affordable.
    """
    from shapely.geometry import box

    minx, miny, maxx, maxy = region.bounds
    near = box(minx - margin_ft, miny - margin_ft,
               maxx + margin_ft, maxy + margin_ft)
    clipped = poly.intersection(near)
    return clipped if not clipped.is_empty else poly


def oversized(region, cell_ft: float = 12.0):
    """
    Whether this water is too big to plan in one go, and why.

    Answered before anything is allocated. The alternative is finding
    out during the first route, by which point several gigabytes have
    been asked for and the machine is swapping rather than working -
    and from the outside that is indistinguishable from a crash.
    """
    minx, miny, maxx, maxy = region.bounds
    cells = ((maxx - minx) / cell_ft + 2) * ((maxy - miny) / cell_ft + 2)
    if cells <= MAX_RASTER_CELLS:
        return None
    across = (maxx - minx) / MILES_TO_FEET
    down = (maxy - miny) / MILES_TO_FEET
    return (
        "This water spans " + format(across, ".0f") + " by "
        + format(down, ".0f") + " miles, which needs a "
        + format(cells / 1e6, ",.0f") + " million cell routing grid - about "
        + format(cells * 8 / 1e9, ".1f") + " GB for every route it works out. "
        "Draw a region of interest over the part you want to survey and "
        "compute that instead. Zoom in first: the region is drawn on the "
        "map, so it is easier to place accurately when the map is not "
        "showing " + format(max(across, down), ".0f") + " miles at once."
    )


def grid_lines(region, bearing_deg_: float, spacing_ft: float,
               min_line_ft: float = 80.0):
    """
    Parallel straight lines across a region, at a bearing, evenly spaced.

    Each line is clipped to the water, so one pass across a lake with arms
    comes back as several separate segments - which is correct: they are
    genuinely separate runs with a headland in between.
    """
    import numpy as np
    from shapely.geometry import LineString, MultiPolygon

    theta = math.radians(bearing_deg_)
    along = (math.sin(theta), math.cos(theta))
    across = (-along[1], along[0])
    out = []
    parts = region.geoms if isinstance(region, MultiPolygon) else [region]
    for part in parts:
        if part.is_empty or part.area < 4000:
            continue
        pts = np.asarray(part.exterior.coords)
        a = pts[:, 0] * along[0] + pts[:, 1] * along[1]
        b = pts[:, 0] * across[0] + pts[:, 1] * across[1]
        reach = a.max() - a.min()
        pos = b.min() + spacing_ft / 2.0
        while pos <= b.max():
            mid = (across[0] * pos, across[1] * pos)
            sweep = LineString([
                (mid[0] + along[0] * (a.min() - reach), mid[1] + along[1] * (a.min() - reach)),
                (mid[0] + along[0] * (a.max() + reach), mid[1] + along[1] * (a.max() + reach)),
            ])
            clipped = sweep.intersection(part)
            pieces = (list(clipped.geoms) if clipped.geom_type == "MultiLineString"
                      else ([clipped] if not clipped.is_empty else []))
            out.extend(p for p in pieces if p.length >= min_line_ft)
            pos += spacing_ft
    return out


def build_lines(poly, settings: PlanSettings, roi=None):
    """
    The full set of survey lines: primary, plus the orthogonal pass if asked.

    `roi` limits the work to part of the water. The bearing still comes from
    the whole lake unless it is set explicitly - a small region's own long axis
    is an accident of where the box was drawn, and lines that do not line up
    with the rest of the survey are a nuisance to merge later.
    """
    import numpy as np

    region = navigable_area(poly, settings, roi)
    if region.is_empty:
        return [], 0.0
    bearing = settings.bearing_deg
    if bearing is None:
        bearing = axis_bearing_deg(np.asarray(poly.exterior.coords))
    lines = [{"kind": "primary", "bearing": bearing, "geom": g}
             for g in grid_lines(region, bearing, settings.spacing_ft,
                                 settings.min_line_ft)]
    if settings.orthogonal:
        cross = (bearing + 90.0) % 180.0
        lines += [{"kind": "orthogonal", "bearing": cross, "geom": g}
                  for g in grid_lines(region, cross, settings.orthogonal_spacing_ft,
                                      settings.min_line_ft)]
    return lines, bearing


def order_nearest_first(lines, start_xy=None):
    """
    Greedy nearest-neighbour over the lines, either end first.

    A sweep in index order looks tidy on paper and sails badly: on a lake with
    arms, consecutive lines of one sweep sit in different arms, and the transit
    between them can exceed the line itself. Measured on a real 306-acre lake,
    ordering this way cut transit from 39 miles to 6.
    """
    remaining = list(range(len(lines)))
    if not remaining:
        return []
    ends = [(list(l["geom"].coords)[0], list(l["geom"].coords)[-1]) for l in lines]
    if start_xy is None:
        first = min(remaining, key=lambda k: ends[k][0][1])
        cursor = ends[first][0]
    else:
        cursor = start_xy
    ordered = []
    while remaining:
        best, flip, best_d = None, False, float("inf")
        for k in remaining:
            d0 = math.dist(cursor, ends[k][0])
            d1 = math.dist(cursor, ends[k][1])
            if d0 < best_d:
                best, flip, best_d = k, False, d0
            if d1 < best_d:
                best, flip, best_d = k, True, d1
        remaining.remove(best)
        coords = list(lines[best]["geom"].coords)
        if flip:
            coords = coords[::-1]
        ordered.append({**lines[best], "coords": coords, "transit_ft": best_d})
        cursor = coords[-1]
    return ordered


def split_into_days(ordered, settings: PlanSettings):
    """
    Cut the ordered lines into outings of a workable length.

    Lines are the unit, not whole blocks: making a block atomic leaves stubs of
    a few minutes at the end of each one, and a day that is three minutes long
    is not a day.
    """
    budget = settings.day_hours * settings.speed_mph * MILES_TO_FEET
    floor = settings.day_min_hours * settings.speed_mph * MILES_TO_FEET
    days, current, travelled = [], [], 0.0
    for line in ordered:
        length = polyline_length_ft(line["coords"])
        hop = line.get("transit_ft", 0.0)
        if current and travelled >= floor and travelled + length + hop > budget:
            days.append(current)
            current, travelled, hop = [], 0.0, 0.0
        current.append({**line,
                        "transit": line.get("transit") if current else None,
                        "transit_ft": hop if current else 0.0})
        travelled += length + hop
    if current:
        days.append(current)

    # A remainder of a few minutes is not much of an outing, so fold a tiny
    # tail into the day before it - but only if that day can take it. This
    # used to fold unconditionally and let the receiving day run over, which
    # is no longer allowed: the cap is the cap. A tail that will not fit
    # stays its own short last day, which the rules permit.
    if len(days) > 1:
        def spent(day):
            return sum(polyline_length_ft(l["coords"])
                       + l.get("transit_ft", 0.0) for l in day)

        tail = spent(days[-1])
        if tail < floor * 0.5 and spent(days[-2]) + tail <= budget:
            days[-2].extend(days.pop())
    return days


def summarise(days, settings: PlanSettings) -> list:
    out = []
    for i, day in enumerate(days, start=1):
        # The run out and the run home are travel, not survey. Counting
        # them as line would flatter the coverage and hide the real cost
        # of a launch point at the wrong end of the lake.
        work = [l for l in day if not l.get("is_return")]
        line_ft = sum(polyline_length_ft(l["coords"]) for l in work)
        transit_ft = sum(l.get("transit_ft", 0.0) for l in day)
        transit_ft += sum(polyline_length_ft(l["coords"])
                          for l in day if l.get("is_return"))
        total = line_ft + transit_ft
        out.append({
            "day": i,
            "lines": len(day),
            "line_mi": line_ft / MILES_TO_FEET,
            "transit_mi": transit_ft / MILES_TO_FEET,
            "hours": total / MILES_TO_FEET / settings.speed_mph,
        })
    return out


def check_clearance(days, poly, settings: PlanSettings,
                    access_xy=None) -> dict:
    """
    How close the plan actually comes to the bank.

    Reported rather than assumed, and measured on the whole mission track -
    the run out, the survey lines, the hops between them and the run home.
    Checking survey lines alone is what let a plan cross a dock and still
    report a clean 100 ft: the offending segment was the join between two
    legs, which belongs to neither and so was never looked at.

    Two numbers come back, because they answer different questions. The
    survey lines must hold the setback. The transit cannot - a launch is on
    the bank, and a boat that stays 100 ft off the bank never reaches it -
    so transit is measured with the approach to each access point excluded,
    and what it must do is stay on water.
    """
    from shapely.geometry import LineString, MultiPoint

    bank = poly.boundary
    survey = float("inf")
    track = float("inf")
    over_land = 0.0
    approach = None
    if access_xy:
        approach = MultiPoint(list(access_xy)).buffer(
            max(settings.setback_ft * 2.0, 100.0))

    for day in days:
        for line in day:
            if line.get("is_return") or line.get("kind") == "return":
                continue
            survey = min(survey, LineString(line["coords"]).distance(bank))
        points = mission_track(day)
        if len(points) < 2:
            continue
        geom = LineString(points)
        if approach is not None:
            geom = geom.difference(approach)
        if not geom.is_empty:
            track = min(track, geom.distance(bank))
            over_land += geom.difference(poly).length

    return {"min_clearance_ft": survey,
            "min_track_clearance_ft": track,
            "over_land_ft": over_land,
            "required_ft": settings.setback_ft,
            "ok": survey >= settings.setback_ft,
            "track_on_water": over_land < 1.0}


def check_turns(days, sample_ft: float = 32.8) -> dict:
    """
    Worst heading change, at the interval the sonar filter uses.

    Two numbers, because two different things are being asked. A grid
    line has to be straight - any turn in it is a fault. A
    shore-following transect is a curve on purpose and only has to stay
    inside the filter's threshold. Reporting one number for both would
    either fail every shore pass or stop noticing a bent grid line.
    """
    from shapely.geometry import LineString

    worst = {"grid": 0.0, "shore": 0.0}
    for day in days:
        for line in day:
            if line.get("is_return"):
                continue
            geom = LineString(line["coords"])
            if geom.length < sample_ft * 2:
                continue
            n = max(2, math.ceil(geom.length / sample_ft))
            pts = [geom.interpolate(i / n, normalized=True).coords[0]
                   for i in range(n + 1)]
            angles = turn_angles_deg(pts)
            if not angles:
                continue
            key = "shore" if line.get("kind") == "shore" else "grid"
            worst[key] = max(worst[key], max(angles))
    return {"max_turn_deg": worst["grid"],
            "max_shore_turn_deg": worst["shore"],
            "max_turn_anywhere_deg": max(worst.values())}


# -- square blocks, and only water someone can see ---------------------------

def block_side_ft(settings: PlanSettings) -> float:
    """
    How big a square has to be to hold one outing.

    A day covers `hours x speed` of line, and lines at `spacing` apart sweep
    `spacing` of width each, so the area worked is length x spacing. The square
    root of that is the side. A little is held back for the turns at each end.
    """
    travel_ft = settings.day_hours * settings.speed_mph * MILES_TO_FEET
    # A fraction of a day, not a whole one. A block sized to fill the day
    # leaves nothing for the trip out, so the very first block overran and
    # there was no smaller unit to fall back on. At about a third, blocks
    # combine to fill whatever the transit leaves.
    return math.sqrt(travel_ft * settings.block_fraction / settings.line_density())


def square_blocks(region, settings: PlanSettings, bearing_deg_: float):
    """
    Cut the water into squares, aligned to the survey bearing.

    Working a compact square at a time keeps the boat near the operator and
    finishes ground before moving on, which a long transect across the whole
    lake does not. An arm that pinches in the middle splits a square into two
    pieces; both are kept, because they are genuinely separate water.
    """
    import numpy as np
    from shapely.geometry import MultiPolygon, Polygon

    theta = math.radians(bearing_deg_)
    along = (math.sin(theta), math.cos(theta))
    across = (-along[1], along[0])
    side = block_side_ft(settings)

    parts = region.geoms if isinstance(region, MultiPolygon) else [region]
    pts = np.vstack([np.asarray(p.exterior.coords) for p in parts])
    a = pts[:, 0] * along[0] + pts[:, 1] * along[1]
    b = pts[:, 0] * across[0] + pts[:, 1] * across[1]

    blocks = []
    ai = math.floor(a.min() / side) * side
    while ai < a.max():
        bi = math.floor(b.min() / side) * side
        while bi < b.max():
            corners = [(ai, bi), (ai + side, bi), (ai + side, bi + side), (ai, bi + side)]
            square = Polygon([(along[0] * p + across[0] * q,
                               along[1] * p + across[1] * q) for p, q in corners])
            piece = square.intersection(region)
            for sub in (piece.geoms if isinstance(piece, MultiPolygon) else [piece]):
                if not sub.is_empty and sub.area > 25000:
                    blocks.append(sub)
            bi += side
        ai += side
    return blocks


def visible_blocks(blocks, raster, stations):
    """
    Pair each block with the nearest station that can actually see it.

    Line of sight is geometry - land either blocks the view or it does not -
    and it is checked against the block's own middle, then its edge if the
    middle is hidden. A block no station can see is returned separately rather
    than dropped silently: it is a real gap in the plan and someone has to
    decide what to do about it.
    """
    paired, unseen = [], []
    for block in blocks:
        centre = block.representative_point()
        r, c = raster.rc(centre.x, centre.y)
        seen = [i for i, st in enumerate(stations) if st["view"][r, c]]
        if not seen:
            edge = [block.exterior.interpolate(k / 12, normalized=True)
                    for k in range(12)]
            seen = [i for i, st in enumerate(stations)
                    if any(st["view"][raster.rc(p.x, p.y)] for p in edge)]
        if not seen:
            unseen.append(block)
            continue
        best = min(seen, key=lambda i: math.dist(
            (centre.x, centre.y), stations[i]["xy"]))
        paired.append({
            "block": block,
            "station": best,
            "distance_ft": math.dist((centre.x, centre.y), stations[best]["xy"]),
        })
    paired.sort(key=lambda p: (p["station"], p["distance_ft"]))
    return paired, unseen


def lines_for_blocks(paired, settings: PlanSettings, bearing_deg_: float,
                     stations, start_xy=None, shore_tracks=None, shore=None):
    """
    Survey lines through a day's blocks, ordered and oriented together.

    Three things are wanted here and they are one question, not three: begin at
    the block nearest the launch, spend as little as possible getting between
    blocks, and leave each block at the corner where the next one begins.

    Answering the first two greedily and the third not at all - which is what
    this did - gets all three wrong, because a block entered at its cheapest
    corner finishes at the far one, and the drive to the next block is then the
    width of a block. Choosing the entry without looking at the exit is a local
    decision to a problem that is not local.

    So the order is settled first (nearest the launch, then un-crossed by
    2-opt), and the orientations are settled afterwards for the whole chain at
    once. A stack of parallel lines can be run four ways, and picking one fixes
    both where the boat enters and where it leaves; the cheapest set of choices
    across the day is a shortest path with four states per run, which is exact
    and instant for the handful of runs a day holds.
    """
    from shapely.geometry import LineString

    # Half a block of detour is worth it to reach the bank first.
    order = _block_order(paired, start_xy, stations, shore,
                         block_side_ft(settings) * 0.5)

    # The shore transect is part of the day it belongs to, cut to the
    # blocks it passes through. Run as its own outing - which is what
    # this did - it costs a separate trip out to water the day was going
    # to visit anyway, and leaves the bank unsurveyed on the day the
    # boat is actually there.
    # Clipped to the whole day's water, then filed under the block it runs
    # past. Clipping block by block instead - which is what this did -
    # chops the transect at every block edge, and the stubs are then thrown
    # away as too short: 0.37 miles survived of 5.70. It also leaves the
    # bearing to be guessed from fragments, which put one day's grid 74
    # degrees across its own stretch of bank.
    shore_by_block = {}
    if settings.shore_pass and shore_tracks:
        from shapely.ops import unary_union

        # Clipped with a few feet of tolerance. The track runs along the
        # edge of the blocks because both come from the same boundary, so an
        # exact clip has it weaving in and out numerically and shattering
        # into stubs: 63 fragments of which the 80 ft filter kept 23% of the
        # transect. At one foot of slack it is 12 pieces and 91%.
        water = unary_union([e["block"] for e in order]).buffer(5.0)
        for track in shore_tracks:
            for part in _lines_in(LineString(track).intersection(water)):
                if part.length < settings.min_line_ft:
                    continue
                middle = part.interpolate(0.5, normalized=True)
                home = min(order, key=lambda e: e["block"].distance(middle))
                shore_by_block.setdefault(id(home), []).append(part)

    # ...and the day's grid is laid parallel to the day's own stretch of
    # bank, not to the whole lake. A cove running east and a reach
    # running north are one bearing only on a lake shaped like a stick.
    along_shore = [list(p.coords) for pieces in shore_by_block.values()
                   for p in pieces]
    day_bearing = bearing_deg_
    if along_shore:
        agreed = shore_bearing_deg(along_shore, min_agreement=0.6)
        if agreed is not None:
            day_bearing = agreed

    runs = []
    for entry in order:
        for piece in shore_by_block.get(id(entry), []):
            runs.append({"lines": [piece], "kind": "shore",
                         "bearing": None, "entry": entry})
        passes = [(day_bearing, settings.spacing_ft, "primary")]
        if settings.orthogonal:
            # The second sweep is run block by block alongside the first, not
            # as a separate lap of the whole group - a lap costs the transit
            # out to every block twice over.
            passes.append(((day_bearing + 90.0) % 180.0,
                           settings.orthogonal_spacing_ft, "orthogonal"))
        for pass_bearing, pass_spacing, kind in passes:
            lines = grid_lines(entry["block"], pass_bearing, pass_spacing,
                               settings.min_line_ft)
            if not lines:
                continue
            theta = math.radians(pass_bearing)
            across = (-math.cos(theta), math.sin(theta))
            lines.sort(key=lambda g: (g.centroid.x * across[0]
                                      + g.centroid.y * across[1]))
            runs.append({"lines": lines, "kind": kind,
                         "bearing": pass_bearing, "entry": entry})

    out = []
    chosen = _pick_orientations(runs, start_xy, shore,
                                settings.shore_first_weight)
    for run, traversal in zip(runs, chosen):
        for geom, coords in traversal:
            out.append({"kind": run["kind"], "bearing": run["bearing"],
                        "geom": geom, "coords": coords,
                        "station": run["entry"]["station"],
                        "block_distance_ft": run["entry"]["distance_ft"]})
    return out


def _lines_in(geom) -> list:
    """Every line inside a geometry of any kind, flattened."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type in ("MultiLineString", "GeometryCollection"):
        out = []
        for part in geom.geoms:
            out.extend(_lines_in(part))
        return out
    return []                      # a point covers no ground


def _block_order(entries, start_xy, stations, shore=None,
                 shore_lead_ft: float = 0.0):
    """
    The order to work a day's blocks in, beginning nearest the launch.

    `shore_lead_ft` is how much of a detour a block touching the bank is
    worth. Near-shore water is where the structure is - docks, drop-offs,
    the things a side scan survey is usually run to find - so it goes first
    where the cost of getting there is comparable, and an outing cut short
    has covered the bank rather than the middle. Set to zero it is pure
    nearest-first, as before.
    """
    from shapely.geometry import Point

    remaining = list(entries)
    if not remaining:
        return []
    cursor = start_xy
    if cursor is None:
        cursor = stations[remaining[0]["station"]]["xy"]

    inland = {}
    if shore is not None and shore_lead_ft > 0:
        for entry in remaining:
            inland[id(entry)] = (0.0 if entry["block"].distance(shore) < 1.0
                                 else shore_lead_ft)

    order = []
    while remaining:
        here = Point(cursor)
        pick = min(remaining,
                   key=lambda e: (e["block"].distance(here)
                                  + inland.get(id(e), 0.0),
                                  here.distance(e["block"].representative_point())))
        remaining.remove(pick)
        order.append(pick)
        middle = pick["block"].representative_point()
        cursor = (middle.x, middle.y)
    return _two_opt(order, start_xy)


def _two_opt(order, start_xy, rounds: int = 4):
    """
    Un-cross a greedy route.

    Nearest-neighbour takes the cheap hops first and then has to come back for
    whatever it stepped over, which shows up as a path that crosses itself.
    Reversing a stretch of the sequence removes the crossing whenever it
    shortens the total, and a day holds few enough blocks that trying every
    reversal costs nothing.
    """
    if len(order) < 3:
        return order
    pts = []
    for entry in order:
        middle = entry["block"].representative_point()
        pts.append((middle.x, middle.y))

    def total(seq):
        cost = 0.0 if start_xy is None else math.dist(start_xy, pts[seq[0]])
        for a, b in zip(seq, seq[1:]):
            cost += math.dist(pts[a], pts[b])
        return cost

    seq = list(range(len(order)))
    best = total(seq)
    for _ in range(rounds):
        improved = False
        for i in range(len(seq) - 1):
            for j in range(i + 2, len(seq)):
                trial = seq[:i + 1] + seq[i + 1:j + 1][::-1] + seq[j + 1:]
                cost = total(trial)
                if cost < best - 1e-6:
                    seq, best, improved = trial, cost, True
        if not improved:
            break
    return [order[i] for i in seq]


def _traversals(lines):
    """
    The four ways to run a stack of parallel lines.

    Either end of the stack, and either end of the first line; everything after
    that follows, because each line is flipped to begin where the last one
    ended. Each comes back as a list of (geom, coords), so its entry corner is
    the first coordinate and its exit corner the last.
    """
    out = []
    for backwards in (False, True):
        stack = lines[::-1] if backwards else lines
        for flip_first in (False, True):
            run, flip = [], flip_first
            for geom in stack:
                coords = list(geom.coords)
                if flip:
                    coords = coords[::-1]
                flip = not flip
                run.append((geom, coords))
            out.append(run)
    return out


def _pick_orientations(runs, start_xy, shore=None, shore_weight: float = 0.0):
    """
    Choose how to run each stack so the whole day's driving is least.

    Four states per run, one transition cost between consecutive runs, and the
    cheapest path through them is a two-line dynamic program. Greedy would be
    simpler and is what made this necessary: it commits to an entry corner
    without knowing the exit that choice implies, so the saving on one hop is
    paid back with interest on the next.
    """
    from shapely.geometry import Point

    options = [_traversals(run["lines"]) for run in runs]
    if not options:
        return []

    def bank(run):
        """What starting here costs in near-shore work left until later.

        A stack of lines has two ends, and in a block against the bank one
        of them is the shore and the other is open water. Starting at the
        shore end works the near-shore lines first, which is where the
        structure is and what an outing cut short should already have
        covered. Priced rather than forced, so it gives way when the detour
        would cost more than it is worth.

        Measured on the first line, not on the point the boat enters at.
        Every line is clipped to the water and so ends at the setback, which
        makes every entry point the same distance from the bank and the
        whole term inert - which is exactly what it was.
        """
        if shore is None or shore_weight <= 0:
            return 0.0
        return shore_weight * shore.distance(run[0][0])

    cost = [(0.0 if start_xy is None else math.dist(start_xy, run[0][1][0]))
            + bank(run) for run in options[0]]
    back = [[0] * len(options[0])]
    for i in range(1, len(options)):
        fresh, choice = [], []
        for run in options[i]:
            entry = run[0][1][0]
            best_j, best_c = 0, None
            for j, prev in enumerate(options[i - 1]):
                total = (cost[j] + math.dist(prev[-1][1][-1], entry)
                         + bank(run))
                if best_c is None or total < best_c:
                    best_j, best_c = j, total
            fresh.append(best_c)
            choice.append(best_j)
        cost, _ = fresh, None
        back.append(choice)

    picked = [min(range(len(cost)), key=lambda k: cost[k])]
    for i in range(len(options) - 1, 0, -1):
        picked.append(back[i][picked[-1]])
    picked.reverse()
    return [options[i][picked[i]] for i in range(len(options))]


def build_plan(poly, settings: PlanSettings, roi=None, access_points=None,
               frame=None, log=print):
    """
    The whole job: water in, days out.

    Two shapes of plan come out of here, and which one you get depends on
    whether anybody has to watch the boat.

      Blocks + sight   The water is cut into squares, each square paired with
                       the nearest bank position that can see it, and worked
                       nearest-first. Days stay compact and in view.
      Plain grid       Lines straight across the whole area, ordered by
                       nearest neighbour. Shorter overall, but a day can range
                       the length of the lake.

    Returns (days, info) - info carries the bearing, what got covered and, when
    sight is required, any water nobody can see.
    """
    import numpy as np
    from shapely.geometry import LineString

    from .access import WaterRaster, route_between, suggest_stations

    region = navigable_area(poly, settings, roi)
    if region.is_empty:
        return [], {"error": "nothing inside the setback"}
    too_big = oversized(region)
    if too_big:
        return [], {"error": too_big}
    bearing = settings.bearing_deg
    if bearing is None:
        bearing = axis_bearing_deg(np.asarray(poly.exterior.coords))
    info = {"bearing_deg": bearing, "unseen_acres": 0.0, "stations": []}

    # Square the grid to the shore before anything is cut, because the
    # bearing decides where the block edges fall. "The next closest
    # transect is roughly parallel to the first" is the whole point of
    # taking the bearing from the shore rather than the lake's bounding
    # axis: the straight line beside the curved one should run alongside
    # it, not across it. On an elongated lake the two agree closely -
    # 161 against 165 degrees at Indian Lake - and on a lake shaped like
    # anything else they do not.
    shore_tracks = shore_track(region, settings) if settings.shore_pass else []
    if shore_tracks and settings.bearing_deg is None:
        bearing = shore_bearing_deg(shore_tracks)
        info["bearing_deg"] = bearing


    if not settings.square_blocks and not settings.require_line_of_sight:
        lines, _ = build_lines(poly, settings, roi)
        start = None
        if access_points and frame is not None:
            start = frame.to_ft(*access_points[0]["lonlat"])
        # Hops here need routing exactly as much as they do in the block
        # path: a plain grid ordered nearest-first jumps across the lake
        # too, and a straight jump crosses whatever is in the way.
        grid_raster = WaterRaster(_raster_area(poly, region), cell_ft=12.0)
        grid_mask, dropped = reachable_water(
            _nav_mask_of(region, grid_raster), grid_raster,
            [start] if start else None)
        if dropped > 0.05:
            info["unreachable_acres"] = dropped
            log(format(dropped, ".1f") + " acres have no water route from"
                " the launch and were left out.")
            lines = [ln for ln in lines
                     if _on_mask(LineString(ln["geom"]).interpolate(
                         0.5, normalized=True).coords[0],
                         grid_raster, grid_mask)]
        # The shore pass leads here too. Building it only on the block
        # path - which is what this did - left a plain grid opening on a
        # grid line with the shore transect nowhere in the plan.
        shore_legs = []
        if settings.shore_pass:
            water = _reachable_region(region, grid_raster, grid_mask)
            for track in shore_track(water, settings):
                for piece in chop_track(track):
                    shore_legs.append(
                        {"kind": "shore", "bearing": None,
                         "geom": LineString(piece), "coords": piece,
                         "station": 0, "block_distance_ft": 0.0})
            if shore_legs:
                info["shore_track_ft"] = sum(
                    polyline_length_ft(l["coords"]) for l in shore_legs)
                info["shore_turn_deg"] = max(
                    worst_turn_deg(l["coords"], settings.turn_sample_ft)
                    for l in shore_legs)
        # Ordered together with the grid, not in front of it. Put first,
        # six miles of perimeter is three hours of nothing but shoreline -
        # a day dedicated to the transect rather than the transect worked
        # into the day the boat is already down that end of the lake.
        ordered = _order_with_hops(
            order_nearest_first(shore_legs + lines, start),
            grid_raster, grid_mask)
        return split_into_days(ordered, settings), info

    log("mapping what can be seen from the bank…")
    raster = WaterRaster(_raster_area(poly, region), cell_ft=12.0)
    navmask = _nav_mask_of(region, raster)
    launch_xy = ([frame.to_ft(*p["lonlat"]) for p in access_points]
                 if access_points and frame is not None else None)
    navmask, stranded = reachable_water(navmask, raster, launch_xy)
    if stranded > 0.05:
        info["unreachable_acres"] = stranded
        log(format(stranded, ".1f") + " acres have no water route from a"
            " launch and were left out.")
    if access_points and frame is not None:
        stations = [{"xy": frame.to_ft(*p["lonlat"]),
                     "view": raster.viewshed(frame.to_ft(*p["lonlat"]),
                                             settings.sight_range_ft)}
                    for p in access_points]
        coverage = float((navmask & _any_view(stations)).sum()) / max(int(navmask.sum()), 1)
    else:
        stations, coverage = suggest_stations(poly, raster, navmask,
                                              max_range_ft=settings.sight_range_ft)
    info["stations"] = [st["xy"] for st in stations]
    info["coverage"] = coverage
    if not stations:
        log("no station sees any water - falling back to a plain grid")
        lines, _ = build_lines(poly, settings, roi)
        return split_into_days(order_nearest_first(lines), settings), info

    log("cutting the water into blocks…")
    # The shore track, on the water the boat can actually reach. Built
    # from the whole region - which is what this did - it follows water
    # that reachable_water then throws away, and the routing has to cross
    # land to get to it: 140 ft of dry ground and a track touching the
    # bank at 0.0 ft.
    #
    # Regenerating on the trimmed water is the right way round. Filtering
    # pieces afterwards against the navigation mask is not, because that
    # mask is deliberately a cell inside the region and the track
    # deliberately runs along its edge - the test threw away all but a
    # quarter mile of a six mile transect.
    shore_ready = []
    if settings.shore_pass:
        shore_ready = shore_track(
            _reachable_region(region, raster, navmask), settings)

    blocks = square_blocks(region, settings, bearing)
    # A block sitting in stranded water is water nobody can get to.
    blocks = [b for b in blocks
              if _on_mask((b.representative_point().x,
                           b.representative_point().y), raster, navmask)]
    info["blocks"] = len(blocks)
    if settings.require_line_of_sight:
        paired, unseen = visible_blocks(blocks, raster, stations)
        info["unseen_acres"] = sum(b.area for b in unseen) / 43560.0
        if unseen:
            log(f"{len(unseen)} block(s), {info['unseen_acres']:.1f} acres, "
                f"are not visible from any marked position")
    else:
        paired = [{"block": b, "station": 0,
                   "distance_ft": math.dist(
                       (b.representative_point().x, b.representative_point().y),
                       stations[0]["xy"])} for b in blocks]
        paired.sort(key=lambda p: p["distance_ft"])

    # Near-shore water first, across the whole plan and not just within a
    # day. The bank is where the structure is - docks, drop-offs, the
    # things a side scan survey is usually run to find - so the days are
    # seeded from blocks touching it, and a plan cut short for weather or
    # hours has covered the bank rather than the middle. Ordering blocks
    # only inside a day, which is what this did first, left the early days
    # in open water: 205 ft from the bank over the first half of the plan
    # against 122 ft over the second.
    lead = block_side_ft(settings) * 0.5
    for entry in paired:
        entry["from_bank_ft"] = entry["block"].distance(poly.boundary)
    paired.sort(key=lambda p: (p["station"],
                               p["distance_ft"]
                               + (0.0 if p["from_bank_ft"] < 1.0 else lead)))

    # One connected patch of water per outing, so each day is a survey in
    # its own right rather than a share of one.
    budget_ft = settings.day_hours * settings.speed_mph * MILES_TO_FEET
    floor_ft = settings.day_min_hours * settings.speed_mph * MILES_TO_FEET
    launches = None
    if access_points and frame is not None:
        launches = [frame.to_ft(*p["lonlat"]) for p in access_points]
    elif stations:
        launches = [st["xy"] for st in stations]
    # Drop the least accessible water first when less than everything is
    # wanted. Blocks are already ordered station-then-distance, so the tail
    # is the far end of the lake.
    if settings.coverage_pct < 99.9 and paired:
        total = sum(p["block"].area for p in paired)
        want = total * settings.coverage_pct / 100.0
        by_reach = sorted(paired, key=lambda p: p["distance_ft"])
        kept, running = [], 0.0
        for entry in by_reach:
            if running >= want:
                break
            kept.append(entry)
            running += entry["block"].area
        info["skipped_acres"] = (total - running) / 43560.0
        info["covered_pct"] = running / total * 100.0 if total else 0.0
        order = {id(p): i for i, p in enumerate(paired)}
        paired = sorted(kept, key=lambda p: order[id(p)])
        log(format(info["skipped_acres"], '.1f') + " acres left out to meet "
            + format(settings.coverage_pct, 'g') + "% coverage.")

    log("grouping blocks into connected outings…")
    groups = contiguous_days(paired, settings, bearing, stations, launches,
                             raster, navmask)
    info["groups"] = len(groups)
    days, unreachable = [], []

    # Sizing a group beforehand gets close but cannot be exact: the run out
    # and the run home are routed over water, and their length is not known
    # until the first and last lines of the day are fixed. So each day is
    # built, measured, and cut back until it fits, and whatever was cut
    # becomes the next outing. Lawnmower order is spatially adjacent, so
    # both the part kept and the part carried over are still one connected
    # patch of water.
    def _group_lines(group):
        # The chain starts at the launch, so the first block worked is
        # the one the boat reaches first rather than the one the seed
        # happened to be.
        start = None
        if launches:
            centre = group[0]["block"].representative_point()
            start = min(launches,
                        key=lambda xy: math.dist(xy, (centre.x, centre.y)))
        return lines_for_blocks(group, settings, bearing, stations, start,
                                shore_ready, poly.boundary)

    pending = [_group_lines(g) for g in groups]
    pending = [p for p in pending if p]

    if settings.shore_pass:
        shore_legs = [l for run in pending for l in run
                      if l.get("kind") == "shore"]
        if shore_legs:
            info["shore_track_ft"] = sum(polyline_length_ft(l["coords"])
                                         for l in shore_legs)
            info["shore_turn_deg"] = max(
                worst_turn_deg(l["coords"], settings.turn_sample_ft)
                for l in shore_legs)
            log("shore transect: "
                + format(info["shore_track_ft"] / MILES_TO_FEET, ".1f")
                + " mi worked into the days, worst turn "
                + format(info["shore_turn_deg"], ".0f") + " deg of "
                + format(settings.turn_limit_deg, ".0f") + " allowed.")
        else:
            log("no shore transect fitted - after the setback the water"
                " is too narrow to hold a curve inside "
                + format(settings.turn_limit_deg, ".0f")
                + " deg per 10 m.")
    while pending:
        lines = pending.pop(0)
        log("fitting day " + str(len(days) + 1) + " to the hours…")
        day, used = _day_within_budget(lines, launches, budget_ft,
                                       raster, navmask)
        spent = _spent_ft(day)
        if spent > budget_ft * 1.02:
            # Only reachable when a single line plus its trip out overruns.
            # There is nothing smaller to fall back to, so it is kept and
            # reported rather than silently dropped.
            unreachable.append({"over_ft": spent - budget_ft,
                                "hours": spent / MILES_TO_FEET
                                / settings.speed_mph})
        # A remainder too small to be an outing is not worth creating.
        # Taking a little less now leaves a tail that can stand on its
        # own, which is cheaper than a separate trip to the lake for
        # twenty minutes of line.
        if used < len(lines):
            day, used = _leave_a_workable_tail(
                lines, used, day, launches, budget_ft, floor_ft,
                raster, navmask)
        days.append(day)
        if used < len(lines):
            pending.insert(0, lines[used:])
        if len(days) >= MAX_DAYS:
            log("stopping at " + str(MAX_DAYS) + " days - this much water"
                " will not fit in outings that short")
            break

    days = _merge_short_days(days, launches, budget_ft, floor_ft,
                             raster, navmask)
    short = [d for d in days[:-1] if _spent_ft(d) < floor_ft * 0.99]
    if short:
        info["short_days"] = len(short)
        log(str(len(short)) + " day(s) below the minimum could not be"
            " merged - the water they cover does not touch another"
            " day's, or the pair would not fit the hours.")

    if unreachable:
        info["over_budget"] = len(unreachable)
        worst = max(u["hours"] for u in unreachable)
        log(str(len(unreachable)) + " day(s) cannot be cut to fit, worst "
            + format(worst, '.1f') + " h - one line plus the trip out is "
            + "already over. Mark an access point nearer, or allow a "
            + "longer day.")
    info["days"] = len(days)
    return days, info


def _nav_mask_of(region, raster):
    """
    A grid for routing on, where a whole cell fits inside the region.

    `_mask_of` marks a cell from its centre, so a path through that centre can
    still be half a cell diagonal - about 8 ft at 12 ft cells - outside the
    region it is supposed to stay in. On a 100 ft margin that showed up as
    transits coming within 99.0 ft of a no-go area. Pulling the region in by
    the half-diagonal before rasterising makes the cell test conservative, so
    a rule set in feet holds in feet rather than to the nearest cell.
    """
    shrunk = region.buffer(-raster.cell * 0.71)
    if shrunk.is_empty:
        shrunk = region                      # cells coarser than the water
    return _mask_of(shrunk, raster)


def _mask_of(region, raster):
    """
    A boolean grid of a shapely region, on the raster's own cells.

    Holes are taken out, which they were not before. Indian Lake has one
    island; with only the outer ring rasterised the routing was told the
    island was water, so the straight hop across it looked clear and 185 ft
    of day one ran over dry ground.
    """
    import numpy as np
    from matplotlib.path import Path
    from shapely.geometry import MultiPolygon

    rows, cols = np.mgrid[0:raster.height, 0:raster.width]
    pts = np.column_stack(((raster.minx + (cols + 0.5) * raster.cell).ravel(),
                           (raster.miny + (rows + 0.5) * raster.cell).ravel()))
    mask = np.zeros((raster.height, raster.width), dtype=bool)
    for part in (region.geoms if isinstance(region, MultiPolygon) else [region]):
        inside = Path(np.asarray(part.exterior.coords)).contains_points(pts)
        mask |= inside.reshape(raster.height, raster.width)
        for hole in part.interiors:
            cut = Path(np.asarray(hole.coords)).contains_points(pts)
            mask &= ~cut.reshape(raster.height, raster.width)
    return mask


def reachable_water(navmask, raster, launches=None):
    """
    The part of the navigable water a boat can get to from a launch.

    Shrinking a lake by the setback, and cutting no-go areas out of it,
    routinely leaves pieces that no longer touch: a narrow neck closes, a
    causeway separates two basins, an island is left with a ring of water
    around it that connects to nothing.

    Least-cost routing does not fail when asked for a path to such a piece.
    Land is expensive in the cost grid, not forbidden, so it buys its way
    across - which is exactly how 185 ft of a day at Indian Lake came to run
    straight over an island. Water with no route to the launch has to be
    dropped before anything is planned in it.

    Returns (mask, acres_dropped).
    """
    import numpy as np
    from skimage.measure import label

    from .access import _nearest_true

    tagged = label(navmask, connectivity=2)
    keep = set()
    for xy in (launches or []):
        # A launch is on the bank by definition, so take the navigable
        # cell nearest it rather than the cell it lands in.
        rc = _nearest_true(navmask, raster.rc(*xy))
        if rc is not None and tagged[rc]:
            keep.add(int(tagged[rc]))
    if not keep:
        counts = np.bincount(tagged.ravel())
        if len(counts) < 2:
            return navmask, 0.0
        keep.add(int(counts[1:].argmax()) + 1)

    good = np.isin(tagged, list(keep))
    dropped = int(navmask.sum()) - int(good.sum())
    return good, dropped * raster.cell * raster.cell / 43560.0


def _reachable_region(region, raster, navmask):
    """
    The parts of the navigable region a boat can get to.

    Whole parts, tested at their middle rather than cell by cell. The
    stranded water this is here to exclude is a separate piece of the
    region - a pocket the setback closed off - so parts are the right
    unit, and testing a middle keeps the answer clear of the edge cases
    that a boundary test creates.
    """
    from shapely.geometry import MultiPolygon
    from shapely.ops import unary_union

    parts = region.geoms if isinstance(region, MultiPolygon) else [region]
    keep = []
    for part in parts:
        middle = part.representative_point()
        if _on_mask((middle.x, middle.y), raster, navmask):
            keep.append(part)
    if not keep:
        return region
    return keep[0] if len(keep) == 1 else unary_union(keep)


def _on_mask(point, raster, mask) -> bool:
    """Is this position on a cell the mask allows?"""
    return bool(mask[raster.rc(*point)])


def _any_view(stations):
    import numpy as np
    out = None
    for st in stations:
        out = st["view"] if out is None else (out | st["view"])
    return out if out is not None else np.zeros((1, 1), dtype=bool)


# -- days that stand on their own -------------------------------------------

def contiguous_days(paired, settings: PlanSettings, bearing_deg_: float,
                    stations, launches=None, raster=None, navmask=None):
    """Grow each outing outward from a seed block, never letting go.

    A day has to be one connected patch of water, or it is not a survey -
    it is two half surveys sharing a date, and neither can be processed or
    looked at on its own.

    So the group is sized here, to a whole day including the trip out and
    home, and nothing downstream is allowed to cut it again. Splitting a
    connected group afterwards on a time budget is what broke this before:
    the halves came out disconnected.

    The round trip is routed rather than estimated. A straight line up a
    winding lake is far shorter than the water path, and the difference
    was going straight into overrun.
    """
    from .access import route_between

    budget = settings.day_hours * settings.speed_mph * MILES_TO_FEET
    remaining = list(paired)
    days = []
    while remaining:
        seed = remaining.pop(0)
        centre = seed["block"].representative_point()
        here = (centre.x, centre.y)
        reserve = 0.0
        if launches:
            launch = min(launches, key=lambda xy: math.dist(xy, here))
            if raster is not None:
                path = route_between(raster, launch, here, navmask)
                reserve = polyline_length_ft(path) * 2.0
            else:
                reserve = math.dist(launch, here) * 2.0 * 1.25
        available = budget - reserve
        if available <= 0:
            # Cannot even get there and back. Keep it as its own day so the
            # caller can see the problem instead of it vanishing.
            days.append([seed])
            continue
        group = [seed]
        travelled = _block_work(seed, settings, bearing_deg_)
        grew = True
        while grew and travelled < available:
            grew = False
            reach = [o for o in remaining if _touches(o["block"], group)]
            reach.sort(key=lambda o: o["distance_ft"])
            for other in reach:
                cost = _block_work(other, settings, bearing_deg_)
                if travelled + cost > available:
                    continue
                remaining.remove(other)
                group.append(other)
                travelled += cost
                grew = True
                break
        days.append(group)
    return days


def _work_legs(day) -> list:
    """A day stripped back to its survey work.

    The run out, the run home and the hops are all rebuilt from the
    legs, so carrying them into a merge would double-count the travel
    and route from the wrong end.
    """
    out = []
    for leg in day:
        if leg.get("is_return") or leg.get("kind") == "return":
            continue
        out.append({k: v for k, v in leg.items()
                    if k not in ("transit", "transit_ft")})
    return out


def _leave_a_workable_tail(lines, used, day, launches, budget_ft,
                           floor_ft, raster, navmask):
    """
    Cut a day so that what is left over is still worth a trip.

    Filling each day to the brim leaves whatever will not fit as the
    next day, and the last of those is routinely twenty minutes long.
    Backing off until the remainder clears the minimum costs the first
    day a little and saves the lake a wasted outing.
    """
    rest = sum(polyline_length_ft(l["coords"]) for l in lines[used:])
    if rest >= floor_ft or used <= 1:
        return day, used
    for take in range(used - 1, 0, -1):
        if sum(polyline_length_ft(l["coords"])
               for l in lines[take:]) < floor_ft:
            continue
        trimmed, _ = _day_within_budget(lines[:take], launches,
                                        budget_ft, raster, navmask)
        if _spent_ft(trimmed) >= floor_ft:
            return trimmed, take
        break        # any less and this day drops below the floor too
    return day, used


def _merge_short_days(days, launches, budget_ft, floor_ft, raster,
                      navmask):
    """
    Fold a day below the minimum into a neighbour that can take it.

    Only into a neighbour: days are built in the order the boat would
    work them, so the day before and the day after are the water next to
    it. Merging across the lake would meet the hours and produce an
    outing in two halves, which is two half surveys sharing a date.

    The last day is left alone however short it is - that is an early
    finish, not a wasted trip.

    Merging alone is not enough. A run holding 2.3 hours of work cannot
    be two days of at least 1.5, so it comes out 2.0 and 0.3, and the
    0.3 cannot then be folded into a neighbour already at 2.0. The pair
    has to be pooled and re-cut instead: 2.3 hours across two days is
    1.5 and 0.8, or one day of 2.3 if it fits.
    """
    changed = True
    while changed and len(days) > 1:
        changed = False
        for i in range(len(days) - 1):
            if _spent_ft(days[i]) >= floor_ft:
                continue
            # Pairs first, then a wider window. A pair often cannot help:
            # a full day of 2.0 h beside a short one of 0.4 is 2.4 h,
            # which is neither one day inside the cap nor two days above
            # the floor. Three days pooled has the slack that two do not.
            for lo, hi in ((i, i + 1), (i - 1, i), (i - 1, i + 1),
                           (i, i + 2)):
                lo, hi = max(0, lo), min(len(days) - 1, hi)
                if hi <= lo:
                    continue
                window = days[lo:hi + 1]
                legs = [leg for day in window for leg in _work_legs(day)]
                recut = _recut(legs, launches, budget_ft, floor_ft,
                               raster, navmask, len(window))
                if recut is None:
                    continue
                last_of_plan = hi == len(days) - 1
                if (_short_count(recut, floor_ft, last_of_plan)
                        >= _short_count(window, floor_ft, last_of_plan)):
                    continue
                days[lo:hi + 1] = recut
                changed = True
                break
            if changed:
                break
    return days


def _short_count(window, floor_ft: float, includes_last: bool) -> int:
    """How many of these days fall below the minimum.

    The final day of the whole plan is allowed to be short - that is an
    early finish - so it is not counted when the window reaches the end.
    """
    days = window[:-1] if includes_last else window
    return sum(1 for day in days if _spent_ft(day) < floor_ft * 0.99)


def _recut(legs, launches, budget_ft, floor_ft, raster, navmask,
           at_most: int):
    """
    A pool of work cut into days again, or None if it needs more days
    than it came from.

    Each day is filled to the hours and then backed off far enough to
    leave a workable tail, which is what turns 2.0 + 0.4 into 1.5 + 0.9.
    The caller decides whether the result is an improvement; this only
    refuses to make the plan longer.
    """
    if not legs:
        return None
    out, rest = [], legs
    while rest:
        day, used = _day_within_budget(rest, launches, budget_ft, raster,
                                       navmask)
        if used < len(rest):
            day, used = _leave_a_workable_tail(rest, used, day, launches,
                                               budget_ft, floor_ft,
                                               raster, navmask)
        out.append(day)
        rest = rest[used:]
        if len(out) > at_most:
            return None
    return out


def _order_with_hops(lines, raster=None, navmask=None):
    """
    Lines in order, each carrying the way in from the end of the last.

    A hop is a straight line only when a straight line stays on water.
    The moment the next line is round a headland or across a cove - which
    is every time a day moves from one block to the next - the segment the
    exporter draws between those two waypoints runs over land. It went
    unnoticed because it belongs to no leg: the clearance check looked at
    survey lines, and this segment is the join between them.
    """
    from .access import route_between

    ordered = []
    for line in lines:
        coords = line.get("coords") or list(line["geom"].coords)
        if not ordered:
            ordered.append({**line, "coords": coords, "transit": None,
                            "transit_ft": 0.0})
            continue
        a, b = ordered[-1]["coords"][-1], coords[0]
        if raster is None or _straight_is_clear(a, b, raster, navmask):
            path, length = None, math.dist(a, b)
        else:
            path = route_between(raster, a, b, navmask)
            length = polyline_length_ft(path)
        ordered.append({**line, "coords": coords, "transit": path,
                        "transit_ft": length})
    return ordered


def _straight_is_clear(a, b, raster, navmask) -> bool:
    """Does the straight hop from a to b stay on navigable water?

    Sampled at half a cell, so nothing narrower than six feet of land can
    hide between two samples. Routing every hop unconditionally would be
    correct too, and far slower - most hops are the forty feet to the next
    line and cross nothing.
    """
    grid = navmask if navmask is not None else raster.water
    span = math.dist(a, b)
    steps = max(2, int(span / (raster.cell * 0.5)) + 1)
    for i in range(steps + 1):
        t = i / steps
        r, c = raster.rc(a[0] + (b[0] - a[0]) * t,
                         a[1] + (b[1] - a[1]) * t)
        if not grid[r, c]:
            return False
    return True


def mission_track(day):
    """
    Every point the boat visits, in order - exactly what gets exported.

    The exporters write each leg's way in and then its coordinates, so
    consecutive legs are joined by a straight segment that exists in the
    mission but in no leg. Anything checking the plan has to look at this,
    not at the legs, or it checks something the boat never does.
    """
    pts = []
    for leg in day:
        for segment in (leg.get("transit") or [], leg["coords"]):
            for point in segment:
                if not pts or point != pts[-1]:
                    pts.append(point)
    return pts


def _spent_ft(day) -> float:
    """Everything the boat travels in a day: line, hops, and the trip."""
    return (sum(polyline_length_ft(l["coords"]) for l in day)
            + sum(l.get("transit_ft", 0.0) for l in day))


def _day_within_budget(lines, launches, budget_ft, raster, navmask):
    """
    As many lines as the hours allow, and not one more.

    Returns (day, lines_used). Binary search rather than stepping down a
    line at a time, because every measurement routes the trip out and home
    over water and a full block holds hundreds of lines.

    A single line that overruns on its own is still returned - there is
    nothing smaller to fall back to - and the caller reports it.
    """
    def build(count):
        ordered = _order_with_hops(lines[:count], raster, navmask)
        launch = None
        if launches:
            first = ordered[0]["coords"][0]
            launch = min(launches, key=lambda xy: math.dist(xy, first))
        return add_access_transit(ordered, launch, raster, navmask)

    whole = build(len(lines))
    if _spent_ft(whole) <= budget_ft:
        return whole, len(lines)

    low, high, best, best_day = 1, len(lines) - 1, 0, None
    while low <= high:
        mid = (low + high) // 2
        candidate = build(mid)
        if _spent_ft(candidate) <= budget_ft:
            best, best_day, low = mid, candidate, mid + 1
        else:
            high = mid - 1
    if best_day is None:
        return build(1), 1
    return best_day, best


def _round_trip_ft(entry, launches) -> float:
    """Straight-line there and back from the nearest launch, with slack.

    A rough figure on purpose: the routed path is not known until the day
    is built, and routing every candidate block to price it would cost more
    than it saves. The quarter added on covers the difference between the
    straight line and a path that has to go round a headland.
    """
    if not launches:
        return 0.0
    centre = entry["block"].representative_point()
    here = (centre.x, centre.y)
    nearest = min(math.dist(here, xy) for xy in launches)
    return nearest * 2.0 * 1.25

def _touches(block, group, tolerance_ft: float = 2.0) -> bool:
    return any(block.distance(entry["block"]) <= tolerance_ft for entry in group)


def _block_work(entry, settings: PlanSettings, bearing_deg_: float) -> float:
    """
    Roughly how much travel a block is worth.

    Area times line density is the line length - both passes, when the
    orthogonal one is on. The sixth added covers the turns at the end of
    each line, which are short individually and add up to real time across
    a block full of them.
    """
    return entry["block"].area * settings.line_density() * 1.17


def add_access_transit(day, access_xy, raster=None, navmask=None):
    """
    Put the run out from the launch, and the run home, into the day.

    Left out, every estimate is short by the trip to the far end of the lake
    and back - which on a long reservoir is the better part of an hour. Routed
    over water when a raster is given, because the straight line from a car
    park to the first survey line will cross a headland.
    """
    if not day or access_xy is None:
        return day
    from .access import route_between

    def leg(a, b):
        if raster is not None:
            return route_between(raster, a, b, navmask)
        return [a, b]

    first = day[0]["coords"][0]
    last = day[-1]["coords"][-1]
    out = leg(access_xy, first)
    home = leg(last, access_xy)
    day = list(day)
    day[0] = {**day[0], "transit": out,
              "transit_ft": polyline_length_ft(out)}
    day.append({"kind": "return", "coords": home,
                "transit_ft": 0.0, "is_return": True})
    return day
