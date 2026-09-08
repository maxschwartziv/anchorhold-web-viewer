"""
Local coordinates, in feet.

Every calculation in this program is a distance or an angle over a few thousand
feet of one lake, so a proper projection would be more machinery than the
problem deserves. A tangent plane at the water's own latitude is accurate to
well under a foot over that span, and it keeps the arithmetic legible: feet in,
feet out, no library between you and the numbers.
"""

from __future__ import annotations

import math

FEET_PER_METRE = 3.280839895
METRES_PER_DEGREE_LAT = 111320.0


class LocalFrame:
    """Feet east and north of a reference point, and back to lon/lat."""

    def __init__(self, lon0: float, lat0: float):
        self.lon0 = lon0
        self.lat0 = lat0
        self.ft_per_deg_lat = METRES_PER_DEGREE_LAT * FEET_PER_METRE
        self.ft_per_deg_lon = self.ft_per_deg_lat * math.cos(math.radians(lat0))

    @classmethod
    def centred_on(cls, coords) -> "LocalFrame":
        """A frame centred on the mean of a lon/lat sequence."""
        pts = list(coords)
        lon = sum(p[0] for p in pts) / len(pts)
        lat = sum(p[1] for p in pts) / len(pts)
        return cls(lon, lat)

    def to_ft(self, lon: float, lat: float):
        return ((lon - self.lon0) * self.ft_per_deg_lon,
                (lat - self.lat0) * self.ft_per_deg_lat)

    def to_lonlat(self, x: float, y: float):
        return (self.lon0 + x / self.ft_per_deg_lon,
                self.lat0 + y / self.ft_per_deg_lat)

    def ring_to_ft(self, ring):
        return [self.to_ft(p[0], p[1]) for p in ring]

    def ring_to_lonlat(self, ring):
        return [list(self.to_lonlat(x, y)) for x, y in ring]


def bearing_deg(a, b) -> float:
    """Compass bearing from a to b, in degrees, 0 = north."""
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360.0


def turn_angles_deg(points):
    """
    Heading change at each interior vertex of a polyline, in degrees.

    This is what the survey turn limit is measured against: resample a track at
    the rule's own interval, then every value here has to stay under the limit,
    or the sonar processing throws those pings away as a turn.
    """
    if len(points) < 3:
        return []
    headings = [bearing_deg(points[i], points[i + 1]) for i in range(len(points) - 1)]
    return [abs((headings[i + 1] - headings[i] + 180.0) % 360.0 - 180.0)
            for i in range(len(headings) - 1)]


def polyline_length_ft(points) -> float:
    return sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def principal_axis(points):
    """
    The long axis of a set of points, as a unit vector.

    Survey lines run along it: on a lake that is far longer than it is wide,
    that is the direction with the fewest turns and the longest runs.
    """
    import numpy as np

    arr = np.asarray(points, dtype=float)
    arr = arr - arr.mean(axis=0)
    _vals, vecs = np.linalg.eigh(np.cov(arr.T))
    axis = vecs[:, -1]
    return float(axis[0]), float(axis[1])


def axis_bearing_deg(points) -> float:
    ux, uy = principal_axis(points)
    return math.degrees(math.atan2(ux, uy)) % 180.0
