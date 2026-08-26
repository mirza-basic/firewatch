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


# --------------------------------------------------------------------------- buffer

@lru_cache(maxsize=1)
def load_buffer() -> dict | None:
    """The pre-built "nearby" band, or None if it is missing or out of date.

    Returned only when the artifact was built for the buffer distance currently
    configured - otherwise the map would draw a confident line in the wrong place,
    which is worse than drawing none at all.
    """
    from .config import BUFFER_GEOJSON, CFG
    try:
        fc = json.loads(BUFFER_GEOJSON.read_text())
    except (OSError, ValueError):
        return None
    built_km = (fc.get("features") or [{}])[0].get("properties", {}).get("buffer_km")
    if built_km is None or abs(float(built_km) - float(CFG["nearby_buffer_km"])) > 1e-6:
        return None
    return fc


def build_buffer(km: float | None = None):
    """Regenerate data/zavidovici-buffer.geojson.

    Build-time only. shapely and pyproj are imported here and nowhere else, so the
    running app keeps its four dependencies; regenerating needs them installed.

    Two details worth keeping straight:

    * The band is buffer *minus* municipality, so it renders as a ring with the
      municipality as a hole - the map can then fill it faintly without washing
      out the area you actually care about.
    * It is offset in UTM 33N metres, not degrees. Buffering in lon/lat would be
      stretched by 1/cos(lat) - about 40% at this latitude.

    It draws distance to the *outline*, while _classify() measures distance to the
    nearest boundary *vertex*, which is never smaller, so the band is drawn a touch
    generous rather than exactly. The worst case is sqrt(km^2 + (L/2)^2) - km for
    the longest ring segment L (1.54 km here): ~49 m at a 6 km buffer, ~143 m at
    2 km. It grows as the buffer shrinks - worth re-checking if this ever drops
    below a kilometre or so.
    """
    from shapely.geometry import mapping, shape
    from shapely.ops import transform as sh_transform
    from pyproj import Transformer

    from .config import BUFFER_GEOJSON, CFG
    km = float(CFG["nearby_buffer_km"] if km is None else km)

    fc = json.loads(BOUNDARY_GEOJSON.read_text())
    poly = shape(fc["features"][0]["geometry"])
    to_m = Transformer.from_crs("EPSG:4326", "EPSG:32633", always_xy=True).transform
    to_deg = Transformer.from_crs("EPSG:32633", "EPSG:4326", always_xy=True).transform

    poly_m = sh_transform(to_m, poly)
    # quad_segs=12 approximates each round join with chords that cut ~13 m inside
    # the true arc (6000*(1-cos(3.75deg))); that alone put a real detection outside
    # the drawn band. 64 brings it to well under a metre and simplify() collapses
    # the redundant points again, so the extra resolution is nearly free.
    band_m = poly_m.buffer(km * 1000, quad_segs=64).difference(poly_m)
    # Simplification pulls the outer edge *inward*, which can drop a detection that
    # the classifier kept - at 25 m one of the 183 stored detections fell outside
    # the drawn band. 8 m is invisible at any usable zoom and keeps the picture on
    # the generous side of the filter, which is the direction that cannot mislead.
    band = sh_transform(to_deg, band_m.simplify(8))

    out = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {"buffer_km": km,
                       "note": "nearby band: within buffer_km of the municipality"},
        "geometry": _round_geom(mapping(band), 5)}]}
    BUFFER_GEOJSON.write_text(json.dumps(out, separators=(",", ":")))
    load_buffer.cache_clear()
    return BUFFER_GEOJSON


def _round_geom(geom, nd: int):
    """Trim coordinate precision. 5 decimals is ~1 m here - far finer than the
    geometry warrants, and it keeps the inlined copy small."""
    def walk(c):
        if isinstance(c, (list, tuple)):
            if c and isinstance(c[0], (int, float)):
                return [round(float(v), nd) for v in c]
            return [walk(x) for x in c]
        return c
    return {**geom, "coordinates": walk(geom["coordinates"])}
