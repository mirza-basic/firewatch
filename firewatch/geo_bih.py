"""Country-wide municipality classification for the multi-channel BiH deployment.

Where geo.py's boundary_rings()/point_in_boundary() answer "is this point inside
*the one* configured municipality", this answers "which of BiH's 145 does this
point belong to" - and the answer is a list, not a single id, for two structural
reasons rather than one:

Sarajevo and Istocno Sarajevo are each a coordinating "Grad" whose territory is
the union of several constituent municipalities (Centar, Novo Sarajevo, Novi
Grad, Stari Grad; Istocna Ilidza, Istocno Novo Sarajevo, etc.) - both the
umbrella and its constituents are separate, deliberately overlapping polygons
here (see data/bih/municipalities.json), so a fire in Centar correctly matches
both "centar" and "sarajevo" and posts to both channels. Every other
municipality is a flat, non-overlapping peer, so it only ever matches itself.

A point can also match *nothing*: adjacent municipalities were traced as
separate OSM relations, rarely sharing an exact digitized edge, so a fire can
land in the sliver of a gap between two polygons that both claim to border it.
Rather than drop that detection unclassified, it falls back to whichever
municipality's boundary is nearest - a fire must always be attributed to
something, even if the map is the one that is slightly wrong, not the fire.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "bih"
MUNI_FILE = DATA_DIR / "municipalities.json"

EARTH_R_KM = 6371.0088


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(min(1.0, math.sqrt(a)))


class Municipality:
    __slots__ = ("id", "short_name", "rings", "bbox")

    def __init__(self, id_: str, short_name: str,
                rings: tuple[tuple[tuple[float, float], ...], ...]):
        self.id = id_
        self.short_name = short_name
        self.rings = rings
        lons = [x for ring in rings for x, _ in ring]
        lats = [y for ring in rings for _, y in ring]
        self.bbox = (min(lons), min(lats), max(lons), max(lats))

    def bbox_contains(self, lat: float, lon: float) -> bool:
        min_lon, min_lat, max_lon, max_lat = self.bbox
        return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat

    def contains(self, lat: float, lon: float) -> bool:
        """Ray-casting (even-odd rule), gated behind the cheap bbox check -
        identical algorithm to geo.point_in_boundary(), just per-municipality."""
        if not self.bbox_contains(lat, lon):
            return False
        inside = False
        for ring in self.rings:
            for i in range(len(ring) - 1):
                x1, y1 = ring[i]
                x2, y2 = ring[i + 1]
                if (y1 > lat) != (y2 > lat):
                    x_int = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
                    if lon < x_int:
                        inside = not inside
        return inside

    def bbox_lower_bound_km(self, lat: float, lon: float) -> float:
        """Cheap, provably-not-an-overestimate distance to this municipality's
        bounding box - never farther than the true distance to its boundary,
        so ranking by this and only exact-checking the closest few candidates
        cannot skip over the actual nearest municipality."""
        min_lon, min_lat, max_lon, max_lat = self.bbox
        clamped_lat = min(max(lat, min_lat), max_lat)
        clamped_lon = min(max(lon, min_lon), max_lon)
        return _haversine_km(lat, lon, clamped_lat, clamped_lon)

    def distance_to_boundary_km(self, lat: float, lon: float) -> float:
        return min(_haversine_km(lat, lon, y, x) for ring in self.rings for x, y in ring)


@lru_cache(maxsize=1)
def municipalities() -> tuple[Municipality, ...]:
    rows = json.loads(MUNI_FILE.read_text())
    out = []
    for r in rows:
        fc = json.loads((DATA_DIR / r["boundary"]).read_text())
        geom = fc["features"][0]["geometry"]
        polys = (geom["coordinates"] if geom["type"] == "MultiPolygon"
                 else [geom["coordinates"]])
        rings = tuple(tuple((float(x), float(y)) for x, y in ring)
                      for poly in polys for ring in poly)
        out.append(Municipality(r["id"], r["short_name"], rings))
    return tuple(out)


def classify_point(lat: float, lon: float) -> list[str]:
    """Every municipality id whose polygon contains this point.

    Usually one. Two for a point inside Sarajevo or Istocno Sarajevo (the
    constituent municipality plus the coordinating "Grad" - both are real,
    correctly overlapping polygons, not a bug to resolve to a single winner).
    Exactly one - the nearest by distance to boundary, never a tie broken
    arbitrarily among zero real candidates - when nothing contains the point at
    all, which only happens in the sliver of a gap between two adjacent
    municipalities' independently-traced OSM boundaries.
    """
    matches = [m.id for m in municipalities() if m.contains(lat, lon)]
    if matches:
        return matches

    # Fallback path only: bbox_lower_bound_km is a true lower bound (see its
    # docstring), so ranking by it and exact-checking just the closest 10
    # cannot miss the real nearest municipality - but it does turn "check all
    # 145 boundaries' every vertex" into "check 10 of them", which matters
    # here specifically because this path runs once per unclassified
    # detection, not once per poll cycle.
    ranked = sorted(municipalities(), key=lambda m: m.bbox_lower_bound_km(lat, lon))
    nearest = min(ranked[:10], key=lambda m: m.distance_to_boundary_km(lat, lon))
    return [nearest.id]
