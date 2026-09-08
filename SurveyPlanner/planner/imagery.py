"""
Re-deriving a shoreline from satellite imagery.

The NHD outline is a hydrographic boundary digitised at map scale. It knows
where the lake is; it does not know that a marina was built in the north arm,
that a line of private docks runs along the east bank, or that the island it
drew as a hole has since been joined to the shore. It is also not accurate to
the metre - it routinely sits ten or twenty feet up the bank.

Imagery knows all of that, and the separation is easy in the one way that
matters: **open water is dark and almost everything else is not**. Docks, piers,
moored boats, sand, grass and trees all reflect far more light than the water
beside them. So the method is deliberately plain - threshold brightness, take
the connected dark region that is the lake, and read its outline off the pixels.

Doing it that way round matters. The obvious approach is to look for bright
blobs *inside* the NHD polygon, but the polygon's own error then has to be
absorbed by pulling in from its edge, and pulling in far enough to stop the
sunlit bank reading as a structure also clips the near half off every dock.
The first version of this did exactly that and reported a lake whose docks all
mysteriously began 27 feet offshore. Tracing the water itself has no such
boundary to trust: the shoreline comes out where the water ends, and a dock is
whatever the lake outline now has bitten out of it.

The NHD polygon is still used, for two things it is good at: choosing which
dark region is the lake, and bounding how far the answer may move. A refined
shoreline is clipped to the original plus `tolerance_ft`, so a shadow on the
bank can cost you a few feet but cannot annex a field.

What this gets wrong, and will keep getting wrong:

  **Sun glint and whitecaps** are bright, so they read as structures. A breezy
  day can litter a lake with them. The area floor removes specks; a large
  glint patch will survive it.
  **Shadow on land** is dark, so it reads as water. A wooded bank in low sun
  can swallow a dock underneath it entirely.
  **Dark structures** read as water for the same reason. On Indian Lake the
  detector finds the pale-roofed boathouses and misses several dark green ones
  a few feet away.
  **The imagery has no date.** Esri publishes no capture time in the tile, so
  nothing here can tell you whether you are looking at last spring or 2015.

None of that is fixable from three colour channels, which is why nothing is
applied without being shown first. This finds candidates faster than reading
the imagery by eye. It is not a survey of what is in the water.
"""

from __future__ import annotations

import math

MIN_AREA_FT2 = 250.0        # smaller than a modest dock: below this is noise
# How far the refined shoreline may move from the one NHD drew. Generous
# enough for a polygon digitised at map scale, tight enough that a dark field
# or a shadowed wood cannot be absorbed into the lake.
TOLERANCE_FT = 80.0
# How far out to reach for the land half of the threshold sample.
SAMPLE_BAND_FT = 250.0
MAX_TILES = 400             # an explicit action, so a bigger budget than a basemap
WATER_FLOOR = 0.25          # below this share of the outline, the threshold failed


class ImageryError(RuntimeError):
    pass


# -- imagery ----------------------------------------------------------------

def fetch_mosaic(bounds, max_tiles: int = MAX_TILES, progress=None):
    """
    Imagery over `bounds`, with enough metadata to invert the projection.

    `basemap.fetch_satellite` returns a lon/lat box, which is fine for drawing
    and not fine for this: Web Mercator is not linear in latitude, and a pixel
    read back through a linear box lands in the wrong place. So the tile origin
    and zoom come back too, and `pixel_to_lonlat` inverts exactly.
    """
    from PIL import Image

    from .basemap import TILE_PX, _lonlat_to_tile, _tile

    west, south, east, north = bounds
    zoom = _zoom_for(bounds, max_tiles)
    x0, y0 = _lonlat_to_tile(west, north, zoom)
    x1, y1 = _lonlat_to_tile(east, south, zoom)
    tx0, ty0 = math.floor(x0), math.floor(y0)
    tx1, ty1 = math.floor(x1), math.floor(y1)
    across, down = tx1 - tx0 + 1, ty1 - ty0 + 1

    canvas = Image.new("RGB", (across * TILE_PX, down * TILE_PX), (0, 0, 0))
    got, total = 0, across * down
    for i, tx in enumerate(range(tx0, tx1 + 1)):
        for j, ty in enumerate(range(ty0, ty1 + 1)):
            tile = _tile(tx, ty, zoom)
            if tile is not None:
                canvas.paste(tile, (i * TILE_PX, j * TILE_PX))
                got += 1
            if progress:
                progress(got, total)
    if got == 0:
        raise ImageryError("No imagery could be fetched for that area.")
    if got < total * 0.6:
        raise ImageryError("Only %d of %d imagery tiles came back. Try again."
                           % (got, total))
    return canvas, {"zoom": zoom, "tx0": tx0, "ty0": ty0, "tile_px": TILE_PX}


def _zoom_for(bounds, max_tiles: int) -> int:
    from .basemap import _lonlat_to_tile

    west, south, east, north = bounds
    for zoom in range(19, 8, -1):
        x0, y0 = _lonlat_to_tile(west, north, zoom)
        x1, y1 = _lonlat_to_tile(east, south, zoom)
        tiles = ((math.floor(x1) - math.floor(x0) + 1)
                 * (math.floor(y1) - math.floor(y0) + 1))
        if tiles <= max_tiles:
            return zoom
    return 12


def pixel_to_lonlat(meta, col: float, row: float):
    """Exact inverse of the tile projection, for a pixel in the mosaic."""
    from .basemap import _tile_to_lonlat

    return _tile_to_lonlat(meta["tx0"] + col / meta["tile_px"],
                           meta["ty0"] + row / meta["tile_px"], meta["zoom"])


def _lonlat_to_pixel(meta, lon: float, lat: float):
    from .basemap import _lonlat_to_tile

    tx, ty = _lonlat_to_tile(lon, lat, meta["zoom"])
    return ((tx - meta["tx0"]) * meta["tile_px"],
            (ty - meta["ty0"]) * meta["tile_px"])


def ground_ft_per_pixel(meta, lat: float) -> float:
    """How much ground one pixel covers, for turning areas into square feet."""
    span = 24901.0 * 5280.0 * math.cos(math.radians(lat))
    return span / (2.0 ** meta["zoom"] * meta["tile_px"])


# -- the analysis -----------------------------------------------------------

def analyse(poly, frame, bounds, min_area_ft2: float = MIN_AREA_FT2,
            tolerance_ft: float = TOLERANCE_FT, max_tiles: int = MAX_TILES,
            progress=None) -> dict:
    """
    Trace the water, and report both the new outline and what it cut out.

    Returns {"refined": polygon, "obstructions": [...], "meta": ...}. The
    obstructions are everything the old outline claimed that the imagery says
    is not water: docks and piers as notches from the bank, islands and moored
    rafts as separate pieces.
    """
    import numpy as np
    from skimage.filters import threshold_otsu
    from skimage.morphology import (binary_closing, binary_opening, disk,
                                    remove_small_holes, remove_small_objects)

    # Weights are roughly the measured share of the work, so the bar moves at
    # something like a constant rate instead of sitting at 20% for two minutes.
    say = _Reporter(progress, [("fetching imagery", 40),
                               ("finding the land and water", 12),
                               ("cleaning up speckle", 12),
                               ("picking out the lake", 12),
                               ("tracing the outline", 18),
                               ("measuring what changed", 6)])

    say.begin("fetching imagery", "asking for satellite tiles")
    image, meta = fetch_mosaic(
        bounds, max_tiles,
        lambda g, t: say.within(g, t, "tile " + str(g) + " of " + str(t)))
    pixels = np.asarray(image, dtype="float32")
    height, width = pixels.shape[:2]
    megapixels = width * height / 1e6

    say.begin("finding the land and water",
              "%d x %d px, %.1f megapixels" % (width, height, megapixels))
    luminance = (0.299 * pixels[:, :, 0] + 0.587 * pixels[:, :, 1]
                 + 0.114 * pixels[:, :, 2])

    centre_lat = frame.to_lonlat(*poly.representative_point().coords[0])[1]
    ft_per_px = ground_ft_per_pixel(meta, centre_lat)
    say.detail("%.2f ft per pixel" % ft_per_px)

    # Otsu needs both classes in the sample it is given. Handing it the water
    # alone - which is what this did first - has it split the water into darker
    # and lighter halves and call the lighter half land, and the lake comes back
    # at 45% of its real size. So the sample is the lake plus a band of shore
    # around it: genuinely bimodal, and centred on the boundary being looked for.
    say.detail("marking the lake and %.0f ft of shore around it"
               % SAMPLE_BAND_FT)
    band = _polygon_mask(poly.buffer(SAMPLE_BAND_FT), frame, meta,
                         width, height, erode_px=1)
    if band.sum() < 500:
        raise ImageryError("The outline covers too little imagery to work with.")
    say.detail("choosing a brightness threshold from %.1f M sample pixels"
               % (band.sum() / 1e6))
    cut = float(threshold_otsu(luminance[band]))
    say.detail("threshold %.1f of 255" % cut)

    say.begin("cleaning up speckle", "separating dark water from everything else")
    water = luminance <= cut
    speck = max(4, int(min_area_ft2 / (ft_per_px ** 2)))
    say.detail("dropping anything under %.0f sq ft (%d px)"
               % (min_area_ft2, speck))
    water = binary_opening(water, disk(1))
    water = remove_small_objects(water, min_size=speck)
    water = binary_closing(water, disk(1))
    # A boat wake or a bright raft leaves a pinhole in the lake. Holes smaller
    # than the size we care about are noise, not islands.
    water = remove_small_holes(water, area_threshold=speck)

    say.begin("picking out the lake", "labelling connected water")
    lake, share = _lake_component(water, poly, frame, meta, width, height)
    say.detail("water covers %.0f%% of the drawn outline" % (share * 100))
    if share < WATER_FLOOR:
        raise ImageryError(
            "The imagery did not separate cleanly - only %.0f%% of the drawn"
            " lake came back dark enough to be water. That usually means"
            " glint, ice or heavy sediment rather than a lake that has"
            " shrunk." % (share * 100))

    say.begin("tracing the outline", "following the water's edge")
    refined = _vectorise(lake, frame, meta)
    if refined is None or refined.is_empty:
        raise ImageryError("The traced water could not be turned into a shape.")
    say.detail("%d vertices, %d hole(s)"
               % (len(refined.exterior.coords), len(refined.interiors)))
    # Bound how far the answer may move from the outline we started with, so a
    # shadowed wood cannot be annexed into the lake.
    refined = refined.intersection(poly.buffer(tolerance_ft))
    if refined.geom_type == "MultiPolygon":
        refined = max(refined.geoms, key=lambda g: g.area)

    say.begin("measuring what changed", "comparing against the drawn outline")
    obstructions = _pieces(poly.difference(refined), min_area_ft2, poly)
    say.done("%d piece(s) found, %.1f acres of water"
             % (len(obstructions), refined.area / 43560.0))
    return {"refined": refined, "obstructions": obstructions, "meta": meta,
            "threshold": cut, "ft_per_pixel": ft_per_px}


class _Reporter:
    """
    Turns named stages into one overall fraction and a line of commentary.

    Each stage carries a weight, so the bar advances at roughly a constant
    rate rather than jumping. Without that the honest thing to show would be
    an indeterminate spinner, and a spinner on a job that takes minutes is
    indistinguishable from a hang - which is what this looked like.
    """

    def __init__(self, sink, stages):
        self.sink = sink
        self.stages = stages
        self.total = sum(w for _n, w in stages) or 1
        self.done_weight = 0.0
        self.stage_weight = 0.0
        self.stage = ""

    def _emit(self, detail, extra=0.0):
        if not self.sink:
            return
        fraction = (self.done_weight + extra) / self.total
        self.sink(min(max(fraction, 0.0), 1.0), self.stage, detail)

    def begin(self, name, detail=""):
        self.done_weight += self.stage_weight
        self.stage = name
        self.stage_weight = dict(self.stages).get(name, 0)
        self._emit(detail)

    def within(self, got, total, detail=""):
        share = (got / total) if total else 0.0
        self._emit(detail, self.stage_weight * share)

    def detail(self, detail):
        self._emit(detail, self.stage_weight * 0.5)

    def done(self, detail=""):
        self.done_weight = self.total
        self.stage_weight = 0.0
        self._emit(detail)


def find_obstructions(poly, frame, bounds, **kw) -> list:
    """Just the things the imagery says are not water. See `analyse`."""
    return analyse(poly, frame, bounds, **kw)["obstructions"]


def refine_shoreline(poly, frame, bounds, **kw):
    """Just the new outline. See `analyse`."""
    return analyse(poly, frame, bounds, **kw)["refined"]


# -- turning pixels into shapes ---------------------------------------------

def _lake_component(water, poly, frame, meta, width, height):
    """
    The one connected dark region that is the lake we asked about.

    Returns (component, share) where share is how much of the drawn outline
    that component actually covers. Measuring the component against the outline
    the other way round - its whole area over the outline's - is meaningless as
    a check, because the component runs off into whatever else is dark in the
    mosaic and reported 160% for a lake it had traced correctly.
    """
    import numpy as np
    from skimage.measure import label

    inside = _polygon_mask(poly, frame, meta, width, height, erode_px=1)
    tagged = label(water, connectivity=2)
    overlap = np.bincount(tagged[inside].ravel())
    if len(overlap) < 2:
        raise ImageryError("No water was found inside the outline.")
    component = tagged == int(overlap[1:].argmax()) + 1
    covered = float((component & inside).sum()) / max(float(inside.sum()), 1.0)
    return component, covered


def _vectorise(mask, frame, meta):
    """A pixel region as a shapely polygon, holes and all."""
    from shapely.geometry import Polygon
    from skimage.measure import find_contours

    rings = []
    for contour in find_contours(mask.astype("float32"), 0.5):
        if len(contour) < 8:
            continue
        shape = Polygon([frame.to_ft(*pixel_to_lonlat(meta, c, r))
                         for r, c in contour])
        if not shape.is_valid:
            # A traced outline that touches itself - a spit a pixel wide, a
            # dock pinching a bay closed - repairs into several pieces. The
            # ring we want is the largest of them, not the collection.
            shape = shape.buffer(0)
            if shape.geom_type == "MultiPolygon":
                shape = max(shape.geoms, key=lambda g: g.area)
        if shape.geom_type == "Polygon" and not shape.is_empty:
            rings.append(shape)
    if not rings:
        return None
    rings.sort(key=lambda p: -p.area)
    outer = rings[0]
    holes = [p.exterior for p in rings[1:]
             if outer.contains(p.representative_point())]
    built = Polygon(outer.exterior, holes)
    if not built.is_valid:
        built = built.buffer(0)
    if built.geom_type == "MultiPolygon":
        built = max(built.geoms, key=lambda g: g.area)
    return built


def _pieces(geom, min_area_ft2: float, poly) -> list:
    """
    Whatever got cut out, as named zones, biggest first.

    Every zone is a plain Polygon. `difference` returns a
    GeometryCollection whenever the two shapes touch along an edge -
    a sliver of area with a stray line or point beside it - and passing
    one of those on as a zone hands everything downstream a shape with
    no `exterior`, which the map and the exporters both assume.
    """
    out = []
    for part in _polygons_in(geom):
        if part.area < min_area_ft2:
            continue
        out.append({"geom": part, "area_ft2": part.area})
    out.sort(key=lambda z: -z["area_ft2"])
    for i, zone in enumerate(out, start=1):
        zone["kind"] = _guess_kind(zone, poly)
        zone["name"] = zone["kind"] + " " + str(i)
    return out


def _polygons_in(geom) -> list:
    """Every polygon inside a geometry of any kind, flattened."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        out = []
        for part in geom.geoms:
            out.extend(_polygons_in(part))
        return out
    return []                      # a line or a point encloses nothing


# Bigger than this, attached to the bank, and it is not a structure - it is the
# drawn outline being wrong. At Indian Lake the largest piece was 4.4 acres of
# housing in a cove the NHD polygon still calls water.
CORRECTION_FT2 = 20000.0


def _guess_kind(zone, poly) -> str:
    """
    A label, offered with very little confidence.

    The one reliable signal is whether the piece touches the bank, because that
    separates two genuinely different findings. Away from the bank it is
    something in the water - an island, a raft, a patch of glint. Against the
    bank it is either a structure reaching out, or the outline having claimed
    dry land in the first place, and size is what tells those apart: a dock is
    tens of feet across and a mistake is hundreds.

    Shape only refines the small cases, and only loosely. A moored boat, a swim
    platform and a whitecap are not distinguishable by outline, and naming them
    confidently would make this list look more authoritative than it is.
    """
    geom = zone["geom"]
    ashore = geom.distance(poly.exterior) < 10.0
    if not ashore:
        return "island" if zone["area_ft2"] > 8000.0 else "obstruction"
    if zone["area_ft2"] > CORRECTION_FT2:
        return "shoreline correction"
    try:
        box = geom.minimum_rotated_rectangle
        edges = list(zip(box.exterior.coords[:-1], box.exterior.coords[1:]))
        sides = sorted(math.dist(a, b) for a, b in edges)
        long_side, short_side = sides[-1], sides[0]
    except Exception:
        return "dock"
    if short_side < 1.0:
        return "dock"
    return "pier" if long_side / short_side > 3.0 else "dock"


def _polygon_mask(poly, frame, meta, width, height, erode_px: int = 1):
    """
    A pixel mask of a polygon, holes included, optionally pulled in.

    Drawn with PIL rather than tested with `matplotlib.path.contains_points`.
    The point-in-polygon test asks the question 25 million times - once per
    pixel of the mosaic - against a 656-vertex ring, and took 122 seconds here
    while every other stage of the analysis together took three. Filling a
    polygon into a bitmap is the same answer arrived at the way a rasteriser
    arrives at it, in well under a second.
    """
    import numpy as np
    from PIL import Image, ImageDraw
    from skimage.morphology import binary_erosion, disk

    def to_px(ring):
        return [_lonlat_to_pixel(meta, *frame.to_lonlat(x, y)) for x, y in ring]

    canvas = Image.new("L", (width, height), 0)
    pen = ImageDraw.Draw(canvas)
    parts = poly.geoms if poly.geom_type == "MultiPolygon" else [poly]
    for part in parts:
        if part.is_empty:
            continue
        pen.polygon(to_px(part.exterior.coords), fill=1)
        for hole in part.interiors:
            pen.polygon(to_px(hole.coords), fill=0)
    mask = np.asarray(canvas, dtype=bool)
    if erode_px > 1:
        mask = binary_erosion(mask, disk(erode_px))
    return mask


# -- putting it back --------------------------------------------------------

def refine_polygon(poly, zones):
    """
    The lake with a set of zones cut out of it.

    Kept for the case where the zones came from somewhere other than `analyse`
    - a hand-drawn no-go list, say. When they came from `analyse`, use the
    `refined` polygon it already built instead: it is the traced water rather
    than the old outline minus pieces of it, and the two differ wherever the
    NHD line was in the wrong place to begin with.
    """
    from shapely.ops import unary_union

    if not zones:
        return poly
    refined = poly.difference(unary_union([z["geom"] for z in zones]))
    if refined.is_empty:
        return poly
    if refined.geom_type == "MultiPolygon":
        refined = max(refined.geoms, key=lambda g: g.area)
    return refined


def polygon_to_rings(poly, frame) -> list:
    """A shapely polygon back to lon/lat rings, outer ring first."""
    rings = [[frame.to_lonlat(x, y) for x, y in poly.exterior.coords]]
    for hole in poly.interiors:
        rings.append([frame.to_lonlat(x, y) for x, y in hole.coords])
    return rings
