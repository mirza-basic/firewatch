"""Fire danger forecast: the Canadian Forest Fire Weather Index (FWI) System,
computed once (twice, around local noon) a day for a single point.

This exists because EFFIS/GWIS - the free, official European fire-danger product -
only reaches ~8-10 km resolution and, measured against this municipality, has a
publication gap around "today" (querying its WMS for the current date returned an
empty 200 response on two separate days, while yesterday and the forecast days on
either side had real data). Both problems disappear by computing the same public,
standard algorithm ourselves, anchored exactly at this municipality's centroid,
from a weather feed with no such gap.

The six formulas below (fine_fuel_moisture_code, duff_moisture_code, drought_code,
initial_spread_index, buildup_index, fire_weather_index) are a line-for-line port
of the official Natural Resources Canada reference implementation
(github.com/cffdrs/cffdrs_r and its Python sibling cffdrs/cffdrs_py), not a
reconstruction from memory - the constants are exactly theirs. `tests_fwi.py`
replays their own published 47-day validation dataset (Van Wagner & Pickett 1985)
through this module and checks every day's FFMC/DMC/DC/ISI/BUI/FWI against their
published output, so a transcription mistake anywhere fails a test rather than
silently mis-rating a real fire day.

References:
  Van Wagner, C.E.; Pickett, T.L. 1985. Equations and FORTRAN program for the
  Canadian Forest Fire Weather Index System. Can. For. Serv. Tech. Rep. 33.
  Van Wagner, C.E. 1987. Development and structure of the Canadian Forest Fire
  Weather Index System. Can. For. Serv. Tech. Rep. 35.
  Lawson, B.D.; Armitage, O.B. 2008. Weather guide for the CFFDRS.

Day length adjustment tables (Le for DMC, Lf for DC) are latitude-banded, not
Canada-specific - the ">=30N" band Zavidovići falls in is also what EFFIS itself
applies across Mediterranean and Central Europe, since that latitude range is
close enough to southern Canada's for the same tables to hold. Keeping every band
(not just the one this municipality needs) is what lets this module work unmodified
if `geo.forecast_point()` ever names a point at a different latitude - a fork onto
another region, in particular.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from . import geo, store
from .config import CFG
from .store import iso, utcnow

log = logging.getLogger("firewatch.firedanger")

# Matches enrich.py's weather lookup - both read "noon" for the same municipality.
TIMEZONE = "Europe/Sarajevo"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Startup values. Van Wagner & Pickett's own documentation calls these "reasonable
# for most springtime conditions" and not necessarily right for other seasons or
# places - which is exactly why every computation here replays a full window of
# real weather through them rather than trusting them directly. See _fetch_daily.
FFMC0, DMC0, DC0 = 85.0, 6.0, 15.0

# Open-Meteo's own *advertised* cap on `past_days` is 92; asking for 93 and
# dropping the first date gives every *usable* date a full 24h of hours behind
# it for the rain sum, without losing any of the 92 - in principle.
#
# Measured 2026-09-10: the response's hourly series does span the full 93 days,
# but the earliest ~28 of them come back with temp/rh/wind all `null` - real
# data only reached back 65 days, not 92. `_fetch_daily` already drops any date
# with a null reading (originally written for a same-day hour Open-Meteo hasn't
# filled in yet, which turned out to catch this too), so a shorter real window
# degrades gracefully rather than computing on missing data - but do not read
# SPINUP_DAYS as a guarantee of how far back the replay actually reaches on any
# given day. 65 days is still within the few-months window the fire-science
# literature treats as enough for the Drought Code's memory of a wrong starting
# value to be washed out by real rain events; there is no guard against a much
# shorter run of real data, because none has been observed yet.
SPINUP_DAYS = 93

FFMC_COEFFICIENT = 250.0 * 59.5 / 101.0

# Day length adjustment for DMC (Eq. 16), by month, Jan..Dec.
ELL_46N = (6.5, 7.5, 9.0, 12.8, 13.9, 13.9, 12.4, 10.9, 9.4, 8.0, 7.0, 6.0)   # lat >= 30N (or <= -30S is handled by ELL_40S below)
ELL_20N = (7.9, 8.4, 8.9, 9.5, 9.9, 10.2, 10.1, 9.7, 9.1, 8.6, 8.1, 7.8)     # 10N <= lat < 30N
ELL_20S = (10.1, 9.6, 9.1, 8.5, 8.1, 7.8, 7.9, 8.3, 8.9, 9.4, 9.9, 10.2)     # -30S < lat <= -10S
ELL_40S = (11.5, 10.5, 9.2, 7.9, 6.8, 6.2, 6.5, 7.4, 8.7, 10.0, 11.2, 11.8)  # lat <= -30S
ELL_EQUATORIAL = 9.0                                                         # -10S < lat <= 10N

# Day length factor for DC (Eq. 22), by month, Jan..Dec.
FL_20N = (-1.6, -1.6, -1.6, 0.9, 3.8, 5.8, 6.4, 5.0, 2.4, 0.4, -1.6, -1.6)   # lat > 20N
FL_20S = (6.4, 5.0, 2.4, 0.4, -1.6, -1.6, -1.6, -1.6, -1.6, 0.9, 3.8, 5.8)   # lat <= -20S
FL_EQUATORIAL = 1.4                                                          # -20S < lat <= 20N


# --------------------------------------------------------------------- FWI System
# Six functions, one per Van Wagner & Pickett (1985) equation set. Signatures,
# constant names and structure follow the official R/Python source deliberately,
# so the two can still be diffed against each other by eye.

def fine_fuel_moisture_code(ffmc_yda: float, temp: float, rh: float,
                            ws: float, prec: float) -> float:
    wmo = FFMC_COEFFICIENT * (101 - ffmc_yda) / (59.5 + ffmc_yda)
    ra = (prec - 0.5) if prec > 0.5 else prec
    if prec > 0.5:
        if wmo > 150:
            wmo = (wmo + 0.0015 * (wmo - 150) * (wmo - 150) * math.sqrt(ra)
                   + 42.5 * ra * math.exp(-100 / (251 - wmo)) * (1 - math.exp(-6.93 / ra)))
        else:
            wmo = wmo + 42.5 * ra * math.exp(-100 / (251 - wmo)) * (1 - math.exp(-6.93 / ra))
    wmo = min(wmo, 250)
    ed = (0.942 * (rh ** 0.679) + 11 * math.exp((rh - 100) / 10)
          + 0.18 * (21.1 - temp) * (1 - math.exp(-rh * 0.115)))
    ew = (0.618 * (rh ** 0.753) + 10 * math.exp((rh - 100) / 10)
          + 0.18 * (21.1 - temp) * (1 - math.exp(-rh * 0.115)))
    if wmo < ed and wmo < ew:
        z = (0.424 * (1 - ((100 - rh) / 100) ** 1.7)
             + 0.0694 * math.sqrt(ws) * (1 - ((100 - rh) / 100) ** 8))
        x = z * 0.581 * math.exp(0.0365 * temp)
        wm = ew - (ew - wmo) / (10 ** x)
    elif wmo > ed:
        z = 0.424 * (1 - (rh / 100) ** 1.7) + 0.0694 * math.sqrt(ws) * (1 - (rh / 100) ** 8)
        x = z * 0.581 * math.exp(0.0365 * temp)
        wm = ed + (wmo - ed) / (10 ** x)
    else:
        wm = wmo
    ffmc1 = (59.5 * (250 - wm)) / (FFMC_COEFFICIENT + wm)
    return min(max(ffmc1, 0.0), 101.0)


def _ell(lat: float, mon: int) -> float:
    """Day length adjustment factor for DMC, by latitude band and month."""
    i = mon - 1
    if lat > 30 or lat <= -30:
        return ELL_46N[i] if lat > 30 else ELL_40S[i]
    if 10 < lat <= 30:
        return ELL_20N[i]
    if -30 < lat <= -10:
        return ELL_20S[i]
    return ELL_EQUATORIAL


def duff_moisture_code(dmc_yda: float, temp: float, rh: float, prec: float,
                       lat: float, mon: int) -> float:
    temp = max(temp, -1.1)
    rk = 1.894 * (temp + 1.1) * (100 - rh) * _ell(lat, mon) * 1e-04
    if prec <= 1.5:
        pr = dmc_yda
    else:
        rw = 0.92 * prec - 1.27
        wmi = 20 + 280 / math.exp(0.023 * dmc_yda)
        if dmc_yda <= 33:
            b = 100 / (0.5 + 0.3 * dmc_yda)
        elif dmc_yda <= 65:
            b = 14 - 1.3 * math.log(dmc_yda)
        else:
            b = 6.2 * math.log(dmc_yda) - 17.2
        wmr = wmi + 1000 * rw / (48.77 + b * rw)
        pr = 43.43 * (5.6348 - math.log(wmr - 20))
    pr = max(pr, 0.0)
    return max(pr + rk, 0.0)


def _pe_base(lat: float, temp: float, mon: int) -> float:
    """Potential evapotranspiration term for DC, by latitude band and month."""
    i = mon - 1
    if lat <= -20:
        fl = FL_20S[i]
    elif lat > 20:
        fl = FL_20N[i]
    else:
        fl = FL_EQUATORIAL
    return (0.36 * (temp + 2.8) + fl) / 2


def drought_code(dc_yda: float, temp: float, rh: float, prec: float,
                 lat: float, mon: int) -> float:
    temp = max(temp, -2.8)
    pe = max(_pe_base(lat, temp, mon), 0.0)
    rw = 0.83 * prec - 1.27
    smi = 800 * math.exp(-dc_yda / 400)
    dr0 = max(dc_yda - 400 * math.log(1 + 3.937 * rw / smi), 0.0)
    dr = dc_yda if prec <= 2.8 else dr0
    return max(dr + pe, 0.0)


def initial_spread_index(ffmc: float, ws: float) -> float:
    fm = FFMC_COEFFICIENT * (101 - ffmc) / (59.5 + ffmc)
    f_w = math.exp(0.05039 * ws)
    f_f = 91.9 * math.exp(-0.1386 * fm) * (1 + (fm ** 5.31) / 49_300_000)
    return 0.208 * f_w * f_f


def buildup_index(dmc: float, dc: float) -> float:
    if dmc == 0 and dc == 0:
        return 0.0
    bui1 = 0.8 * dc * dmc / (dmc + 0.4 * dc)
    p = 0.0 if dmc == 0 else (dmc - bui1) / dmc
    cc = 0.92 + (0.0114 * dmc) ** 1.7
    bui0 = max(dmc - cc * p, 0.0)
    return bui0 if bui1 < dmc else bui1


def fire_weather_index(isi: float, bui: float) -> float:
    if bui > 80:
        bb = 0.1 * isi * (1000 / (25 + 108.64 / math.exp(0.023 * bui)))
    else:
        bb = 0.1 * isi * (0.626 * (bui ** 0.809) + 2)
    return bb if bb <= 1.0 else math.exp(2.72 * ((0.434 * math.log(bb)) ** 0.647))


def step(prev: dict, temp: float, rh: float, ws: float, prec: float,
        lat: float, mon: int) -> dict:
    """Advance one day. `prev` carries yesterday's ffmc/dmc/dc."""
    rh = min(rh, 99.9999)          # the official fwi() wrapper's own guard
    ffmc = fine_fuel_moisture_code(prev["ffmc"], temp, rh, ws, prec)
    dmc = duff_moisture_code(prev["dmc"], temp, rh, prec, lat, mon)
    dc = drought_code(prev["dc"], temp, rh, prec, lat, mon)
    isi = initial_spread_index(ffmc, ws)
    bui = buildup_index(dmc, dc)
    fwi = fire_weather_index(isi, bui)
    return {"ffmc": ffmc, "dmc": dmc, "dc": dc, "isi": isi, "bui": bui, "fwi": fwi}


# --------------------------------------------------------------------- danger class
# Thresholds and colours read directly off EFFIS's own published legend
# (mf010.fwi, GetLegendGraphic) - so a class computed here lines up with what the
# same day would show on the official map, which is the whole point of using the
# same standard algorithm.
CLASSES = (
    (11.2, "low", "#9cffc0"),
    (21.3, "moderate", "#cde24e"),
    (38.0, "high", "#e6ac00"),
    (50.0, "very_high", "#d97010"),
    (70.0, "extreme", "#ad060e"),
)
VERY_EXTREME = ("very_extreme", "#3a0015")


def classify(fwi: float) -> tuple[str, str]:
    """(class key, legend colour) for a FWI value."""
    for limit, key, color in CLASSES:
        if fwi < limit:
            return key, color
    return VERY_EXTREME


# --------------------------------------------------------------------------- fetch

def _fetch_daily(lat: float, lon: float, forecast_days: int) -> dict[str, dict]:
    """Noon temp/RH/wind and 24h-to-noon rain, per local date.

    One call, one keyless host, covers both the deep replay and the routine daily
    update: `past_days` *asks* to reach back to Open-Meteo's own 92-day cap and
    `forecast_days` reaches forward, so a cold start and a normal day use the same
    request shape. What actually comes back can reach less far than asked - see
    SPINUP_DAYS - which is why every date here is dropped, not zero-filled, when
    its noon reading is null.
    """
    r = requests.get(
        OPEN_METEO_URL,
        params={
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
            "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation",
            "past_days": SPINUP_DAYS, "forecast_days": max(1, int(forecast_days)),
            "timezone": TIMEZONE, "wind_speed_unit": "kmh", "precipitation_unit": "mm",
        },
        headers={"User-Agent": CFG["user_agent"]}, timeout=30)
    r.raise_for_status()
    h = r.json()["hourly"]
    times = h["time"]
    temp, rh, ws, prec = (h["temperature_2m"], h["relative_humidity_2m"],
                          h["wind_speed_10m"], h["precipitation"])
    noon_idx = {t[:10]: i for i, t in enumerate(times) if t.endswith("T12:00")}
    dates = sorted(noon_idx)
    out: dict[str, dict] = {}
    # The earliest date is a lead-in only, kept so the next one has a full 24h of
    # hours behind it for the rain sum - it is never itself returned or stepped.
    for date in dates[1:]:
        i = noon_idx[date]
        if i < 23:
            continue
        if temp[i] is None or rh[i] is None or ws[i] is None:
            continue          # an hour Open-Meteo hasn't filled in yet
        rain24 = sum(v for v in prec[i - 23:i + 1] if v is not None)
        out[date] = {"temp": temp[i], "rh": rh[i], "ws": ws[i], "prec": rain24}
    return out


# ------------------------------------------------------------------------ update

_GATE_KEY = "fwi_gate"
_PAYLOAD_KEY = "fwi_payload"


def _load_json_meta(con, key: str):
    raw = store.get_meta(con, key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def update(con, force: bool = False) -> dict | None:
    """Recompute today's fire danger (and the short forecast) if due.

    `force` bypasses the once/twice-a-day gate - only the CLI's `fire-danger
    --force` uses it, for checking the module against a live fetch on demand.

    Runs at most twice a day - once for whichever cycle first runs after local
    midnight (today's entry is then built from forecast noon values), and once
    more for the first cycle after local noon (today's entry is rebuilt from the
    now-actual noon observation, per Open-Meteo's own `past_days` window). Every
    other cycle returns the cached payload with no network call at all, the same
    shape as imagery.refresh()'s scene-date gate.

    Deliberately stateless across days: rather than persisting yesterday's codes
    and bridging forward (which needs separate cold-start and gap-repair paths),
    every run replays the *entire* fetched window from the standard startup
    values. That window *asks* to reach SPINUP_DAYS back, and how far it actually
    reaches varies with what Open-Meteo has filled in (measured as low as 65 real
    days back on 2026-09-10) - either way it is real weather, not a persisted
    guess, so a day the poller was not running for needs no special case: there
    is no stored state for it to have gone stale.
    """
    if not CFG.get("fire_danger_enabled", True):
        return None
    lat, lon = geo.forecast_point()
    now = datetime.now(ZoneInfo(TIMEZONE))
    today = now.date().isoformat()
    noon_passed = now.hour >= 12

    gate = _load_json_meta(con, _GATE_KEY)
    cached = _load_json_meta(con, _PAYLOAD_KEY)
    if not force and gate and gate.get("date") == today \
            and (gate.get("noon_passed") or not noon_passed):
        return cached

    try:
        daily = _fetch_daily(lat, lon, int(CFG["fire_danger_forecast_days"]))
    except Exception as exc:
        log.warning("fire danger fetch failed: %s", exc)
        return cached

    dates = sorted(daily)
    if today not in dates:
        log.warning("fire danger: %s not in the fetched window (%s..%s)", today,
                    dates[0] if dates else "?", dates[-1] if dates else "?")
        return cached

    codes = {"ffmc": FFMC0, "dmc": DMC0, "dc": DC0}
    trend = []
    for d in dates:
        w = daily[d]
        codes = step(codes, w["temp"], w["rh"], w["ws"], w["prec"], lat, int(d[5:7]))
        cls_key, color = classify(codes["fwi"])
        trend.append({"date": d, **{k: round(v, 1) for k, v in codes.items()},
                      "class": cls_key, "color": color})

    today_entry = next(e for e in trend if e["date"] == today)
    future = [e for e in trend if e["date"] > today]
    payload = {
        "point": {"lat": round(lat, 4), "lon": round(lon, 4)},
        "today": today_entry,
        "forecast": future,
        "updated_at": iso(utcnow()),
    }
    store.set_meta(con, _GATE_KEY, json.dumps({"date": today, "noon_passed": noon_passed}))
    store.set_meta(con, _PAYLOAD_KEY, json.dumps(payload, ensure_ascii=False))
    return payload
