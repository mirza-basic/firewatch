"""Contextual enrichment: current weather at the fire, for spread assessment."""
from __future__ import annotations

import logging
import time

import requests

from . import geo
from . import place
from .config import CFG

log = logging.getLogger("firewatch.enrich")

_cache: dict[tuple, tuple[float, dict]] = {}
_TTL = 900.0          # 15 minutes


def weather(lat: float, lon: float) -> dict | None:
    """Temperature, humidity and wind at the fire location (Open-Meteo, keyless).

    Wind matters twice over: it drives spread direction, and low humidity plus
    gusts is what turns a smouldering 8 MW hotspot into a problem.
    """
    key = (round(lat, 2), round(lon, 2))
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
                    "current": "temperature_2m,relative_humidity_2m,"
                               "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
                    # Only labels the timestamps in the response; the values
                    # themselves are for the coordinates, not the zone. Still worth
                    # following place.json rather than naming a city here.
                    "timezone": place.PLACE["timezone"]},
            headers={"User-Agent": CFG["user_agent"]}, timeout=30)
        cur = r.json().get("current", {})
        if not cur:
            return None
        deg = cur.get("wind_direction_10m")
        out = {
            "temp": cur.get("temperature_2m"),
            "humidity": cur.get("relative_humidity_2m"),
            "speed": cur.get("wind_speed_10m"),
            "gusts": cur.get("wind_gusts_10m"),
            "dir_deg": deg,
            # Meteorological convention: wind_direction is where it blows FROM.
            "from": geo.compass(deg) if deg is not None else "",
            "towards": geo.compass((deg + 180) % 360) if deg is not None else "",
            "time": cur.get("time"),
        }
        _cache[key] = (time.time(), out)
        return out
    except Exception as exc:
        log.warning("weather lookup failed: %s", exc)
        return None


def fire_risk(w: dict | None) -> str:
    """Coarse spread-risk label from current conditions."""
    if not w:
        return "unknown"
    gust = w.get("gusts") or w.get("speed") or 0
    rh = w.get("humidity")
    rh = 100 if rh is None else rh
    if gust >= 40 and rh <= 35:
        return "extreme"
    if gust >= 30 or rh <= 25:
        return "high"
    if gust >= 18 or rh <= 40:
        return "elevated"
    return "moderate"
