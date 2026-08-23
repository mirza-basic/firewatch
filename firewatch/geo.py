"""Geometry helpers: boundary containment, distance, bearing, nearest settlement."""
from __future__ import annotations

import json
import math
from functools import lru_cache

from .config import BOUNDARY_GEOJSON, SETTLEMENTS_JSON, TOWN_LAT, TOWN_LON

EARTH_R_KM = 6371.0088
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


@lru_cache(maxsize=1)
def boundary_ring() -> tuple[tuple[float, float], ...]:
    """Outer ring of the municipality as ((lon, lat), ...), closed."""
    fc = json.loads(BOUNDARY_GEOJSON.read_text())
    ring = fc["features"][0]["geometry"]["coordinates"][0]
    return tuple((float(x), float(y)) for x, y in ring)


@lru_cache(maxsize=1)
def bbox() -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat) of the municipality."""
    r = boundary_ring()
    xs = [p[0] for p in r]
    ys = [p[1] for p in r]
    return min(xs), min(ys), max(xs), max(ys)


def bbox_padded(km: float) -> tuple[float, float, float, float]:
    """Municipality bbox grown by `km` in every direction."""
    w, s, e, n = bbox()
    dlat = km / 111.32
    mid = (s + n) / 2
    dlon = km / (111.32 * max(0.1, math.cos(math.radians(mid))))
    return w - dlon, s - dlat, e + dlon, n + dlat


def point_in_boundary(lat: float, lon: float) -> bool:
    """Ray-casting test against the municipality outline (even-odd rule)."""
    ring = boundary_ring()
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > lat) != (y2 > lat):
            x_int = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_int:
                inside = not inside
    return inside


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(deg: float) -> str:
    return COMPASS[int((deg + 11.25) % 360 // 22.5)]


def distance_to_boundary_km(lat: float, lon: float) -> float:
    """Approximate great-circle distance to the nearest boundary vertex."""
    return min(haversine_km(lat, lon, y, x) for x, y in boundary_ring())


@lru_cache(maxsize=1)
def settlements() -> tuple[dict, ...]:
    return tuple(json.loads(SETTLEMENTS_JSON.read_text()))


def nearest_settlement(lat: float, lon: float) -> tuple[dict | None, float, str]:
    """Closest named place, its distance in km, and the compass direction of the
    fire as seen from that place (i.e. "4 km SSE of Vozuća")."""
    best, best_km = None, float("inf")
    for p in settlements():
        d = haversine_km(lat, lon, p["lat"], p["lon"])
        if d < best_km:
            best, best_km = p, d
    if best is None:
        return None, float("inf"), ""
    return best, best_km, compass(bearing_deg(best["lat"], best["lon"], lat, lon))


def location_parts(lat: float, lon: float) -> dict:
    """Components of the location phrase, so the UI can compose it in any language.

    describe_location() bakes an English sentence; the map needs the pieces to
    build "6.7 km IJI od Kamenice" as well as "6.7 km ESE of Kamenica".
    """
    place, km, direction = nearest_settlement(lat, lon)
    if place is None:
        return {"name": None, "km": None, "dir": None}
    return {"name": place["n"], "km": round(km, 1) if km >= 0.8 else None,
            "dir": direction if km >= 0.8 else None}


def describe_location(lat: float, lon: float) -> str:
    """Human-readable placement, e.g. '3.4 km SSE of Vozuća'."""
    place, km, direction = nearest_settlement(lat, lon)
    if place is None:
        return f"{lat:.4f}, {lon:.4f}"
    if km < 0.8:
        return place["n"]
    return f"{km:.1f} km {direction} of {place['n']}"


def from_town(lat: float, lon: float) -> tuple[float, str]:
    """Distance and direction from Zavidovići town centre."""
    d = haversine_km(TOWN_LAT, TOWN_LON, lat, lon)
    return d, compass(bearing_deg(TOWN_LAT, TOWN_LON, lat, lon))


def centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Mean position of (lat, lon) pairs - fine at municipality scale."""
    if not points:
        return 0.0, 0.0
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))
