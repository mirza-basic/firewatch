"""Fire danger forecast: the Canadian Forest Fire Weather Index (FWI) System,
computed once (twice, around local noon) a day, independently for each of BiH's
145 municipalities.

This exists because EFFIS/GWIS - the free, official European fire-danger product -
only reaches ~8-10 km resolution and has a publication gap around "today"
(querying its WMS for the current date returned an empty 200 response on two
separate days, while yesterday and the forecast days on either side had real
data). Computing the same public, standard algorithm ourselves avoids both,
anchored at each municipality's own forecast point from a weather feed with no
such gap - one point per municipality, so one number never averages over an area
too large for it to mean anything.

The six formulas below (fine_fuel_moisture_code, duff_moisture_code, drought_code,
initial_spread_index, buildup_index, fire_weather_index) are a line-for-line port
of the official Natural Resources Canada reference implementation
(github.com/cffdrs/cffdrs_r and its Python sibling cffdrs/cffdrs_py), not a
reconstruction from memory - the constants are exactly theirs. `tests_fwi.py`
replays their own published 48-day validation dataset (Van Wagner & Pickett 1985)
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
Canada-specific - the ">=30N" band all of Bosnia and Herzegovina falls in is also
what EFFIS itself applies across Mediterranean and Central Europe. Every band is
kept so the module works unmodified at any latitude.
"""
from __future__ import annotations

import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from . import geo_bih, store
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

# Open-Meteo's advertised cap on `past_days` is 92; asking for 93 and dropping the
# first date gives every usable date a full 24h of hours behind it for the rain sum.
#
# Measured 2026-09-10: the hourly series spans the full 93 days, but the earliest
# ~28 come back with temp/rh/wind all `null`, so real data reached back only 65
# days. `_fetch_daily` drops any date with a null reading, so a shorter real window
# degrades gracefully - but SPINUP_DAYS is not a guarantee of how far back the
# replay reaches. 65 days is still within the few-months window the literature
# treats as enough for the Drought Code to wash out a wrong starting value; there
# is no guard against a much shorter run of real data.
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
    """Fine Fuel Moisture Code (FFMC): today's value from yesterday's and noon weather."""
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
    """Duff Moisture Code (DMC), advanced one day."""
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
    """Drought Code (DC), advanced one day."""
    temp = max(temp, -2.8)
    pe = max(_pe_base(lat, temp, mon), 0.0)
    rw = 0.83 * prec - 1.27
    smi = 800 * math.exp(-dc_yda / 400)
    dr0 = max(dc_yda - 400 * math.log(1 + 3.937 * rw / smi), 0.0)
    dr = dc_yda if prec <= 2.8 else dr0
    return max(dr + pe, 0.0)


def initial_spread_index(ffmc: float, ws: float) -> float:
    """Initial Spread Index (ISI) from FFMC and wind speed."""
    fm = FFMC_COEFFICIENT * (101 - ffmc) / (59.5 + ffmc)
    f_w = math.exp(0.05039 * ws)
    f_f = 91.9 * math.exp(-0.1386 * fm) * (1 + (fm ** 5.31) / 49_300_000)
    return 0.208 * f_w * f_f


def buildup_index(dmc: float, dc: float) -> float:
    """Buildup Index (BUI) from DMC and DC."""
    if dmc == 0 and dc == 0:
        return 0.0
    bui1 = 0.8 * dc * dmc / (dmc + 0.4 * dc)
    p = 0.0 if dmc == 0 else (dmc - bui1) / dmc
    cc = 0.92 + (0.0114 * dmc) ** 1.7
    bui0 = max(dmc - cc * p, 0.0)
    return bui0 if bui1 < dmc else bui1


def fire_weather_index(isi: float, bui: float) -> float:
    """Fire Weather Index (FWI) from ISI and BUI."""
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
# (mf010.fwi, GetLegendGraphic), so a class computed here lines up with what the
# same day would show on the official map.
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

    One call to one keyless host covers both the deep replay and the routine daily
    update: `past_days` asks to reach back to Open-Meteo's 92-day cap and
    `forecast_days` reaches forward. What comes back can reach less far than asked
    (see SPINUP_DAYS), so a date is dropped, not zero-filled, when its noon
    reading is null.
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
            continue          # hour not filled in (yet, or outside the real data window)
        rain24 = sum(v for v in prec[i - 23:i + 1] if v is not None)
        out[date] = {"temp": temp[i], "rh": rh[i], "ws": ws[i], "prec": rain24}
    return out


# ------------------------------------------------------------------------ update
# One gate and one payload per municipality: each of the 145 recomputes (or skips)
# independently, so a fetch failure for one does not touch the others.

# Courtesy pause after each real request in update_one(); only charged when a
# request actually went out, not per municipality checked.
_FETCH_DELAY_S = 0.3

# Cold start (nothing cached, all 145 due at once) takes several minutes fully
# sequential - long enough that a watching caller can mistake it for hung. The
# fetches therefore run in a bounded thread pool; database writes stay sequential
# in the calling thread, since SQLite's WAL mode wants a single writer.
#
# Open-Meteo enforces a rolling per-minute cap despite advertising none for this
# endpoint (measured: a 429 with body `{"reason":"Minutely API request limit
# exceeded. Please try again in one minute.","error":true}`). Back-to-back full
# runs hit it hard (145/145 from a quiet state, then 68/145 moments later), so a
# lower worker count only slows how fast the budget is spent; the 429 retry in
# update_all() is what recovers the municipalities that lose the race.
_MAX_WORKERS = 4


def _gate_key(municipality_id: str) -> str:
    """meta key holding the date/noon gate for one municipality."""
    return f"fwi_gate:{municipality_id}"


def _payload_key(municipality_id: str) -> str:
    """meta key holding the last computed payload for one municipality."""
    return f"fwi_payload:{municipality_id}"


def _load_json_meta(con, key: str):
    """A JSON value from meta, or None when absent or unparseable."""
    raw = store.get_meta(con, key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def update_one(con, municipality_id: str, lat: float, lon: float,
              force: bool = False) -> dict | None:
    """Recompute today's fire danger (and the short forecast) for one
    municipality, if due.

    `force` bypasses the once/twice-a-day gate (used by the CLI's `fire-danger
    --force`).

    Runs at most twice a day per municipality: for the first cycle after local
    midnight (today's entry built from forecast noon values) and the first cycle
    after local noon (rebuilt from the actual noon observation). Every other cycle
    returns the cached payload with no network call, like imagery.refresh()'s
    scene-date gate.

    Stateless across days: rather than persisting yesterday's codes and bridging
    forward (which needs cold-start and gap-repair paths), every run replays the
    entire fetched window from the standard startup values. How far back that
    window really reaches varies (see SPINUP_DAYS), but it is always real weather,
    so a day the poller missed needs no special case.
    """
    now = datetime.now(ZoneInfo(TIMEZONE))
    today = now.date().isoformat()
    noon_passed = now.hour >= 12

    gate_key, payload_key = _gate_key(municipality_id), _payload_key(municipality_id)
    gate = _load_json_meta(con, gate_key)
    cached = _load_json_meta(con, payload_key)
    if not force and gate and gate.get("date") == today \
            and (gate.get("noon_passed") or not noon_passed):
        return cached

    try:
        daily = _fetch_daily(lat, lon, int(CFG["fire_danger_forecast_days"]))
    except Exception as exc:
        log.warning("fire danger fetch failed for %s: %s", municipality_id, exc)
        return cached
    finally:
        # Courtesy towards a free, keyless API; only reached when a request
        # actually went out, so gated cycles cost nothing here.
        time.sleep(_FETCH_DELAY_S)

    payload = _compute_payload(daily, lat, lon, today, municipality_id)
    if payload is None:
        return cached
    store.set_meta(con, gate_key, json.dumps({"date": today, "noon_passed": noon_passed}))
    store.set_meta(con, payload_key, json.dumps(payload, ensure_ascii=False))
    return payload


def _compute_payload(daily: dict[str, dict], lat: float, lon: float, today: str,
                      municipality_id: str) -> dict | None:
    """The pure-CPU half of update_one(): replay the FWI System through an
    already-fetched window and shape the payload. No I/O or shared state, so
    update_all() runs it in the thread that owns the database connection.
    """
    dates = sorted(daily)
    if today not in dates:
        log.warning("fire danger: %s not in %s's fetched window (%s..%s)", today,
                    municipality_id, dates[0] if dates else "?",
                    dates[-1] if dates else "?")
        return None

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
    return {
        "point": {"lat": round(lat, 4), "lon": round(lon, 4)},
        "today": today_entry,
        "forecast": future,
        "updated_at": iso(utcnow()),
    }


def update_all(con, force: bool = False) -> dict[str, dict]:
    """update_one() for every municipality, fetching concurrently.

    Most cycles nothing is due (see update_one()'s gate) and this costs nothing.
    When some are due, the fetches run in a bounded thread pool (_fetch_batch())
    while compute-and-persist stays in this thread, one municipality at a time.
    Municipalities that hit Open-Meteo's per-minute limit get one coordinated retry
    after waiting it out (see _MAX_WORKERS); any other fetch failure is logged and
    skipped without affecting the rest. Returns payloads keyed by municipality id.
    """
    out: dict[str, dict] = {}
    if not CFG.get("fire_danger_enabled", True):
        return out

    now = datetime.now(ZoneInfo(TIMEZONE))
    today = now.date().isoformat()
    noon_passed = now.hour >= 12
    forecast_days = int(CFG["fire_danger_forecast_days"])

    # Split gated (skip) from due (fetch) with the same test update_one() applies.
    due: list[tuple[str, float, float]] = []
    for m in geo_bih.municipalities():
        lat, lon = m.forecast_point
        gate = _load_json_meta(con, _gate_key(m.id))
        cached = _load_json_meta(con, _payload_key(m.id))
        if not force and gate and gate.get("date") == today \
                and (gate.get("noon_passed") or not noon_passed):
            if cached:
                out[m.id] = cached
            continue
        due.append((m.id, lat, lon))

    if not due:
        return out

    results = _fetch_batch(due, forecast_days)

    # Rate-limited municipalities (see _MAX_WORKERS) get one retry after a single
    # shared wait, not each sleeping and retrying independently, which would
    # re-trigger the same limit as a second stampede.
    rate_limited = [mid for mid, (status, _) in results.items() if status == "429"]
    if rate_limited:
        log.warning("fire danger: %d/%d municipalities hit Open-Meteo's per-minute "
                    "limit, waiting 65s before one retry", len(rate_limited), len(due))
        time.sleep(65)
        lookup = {mid: (lat, lon) for mid, lat, lon in due}
        retry_items = [(mid, *lookup[mid]) for mid in rate_limited]
        results.update(_fetch_batch(retry_items, forecast_days))

    lookup = {mid: (lat, lon) for mid, lat, lon in due}
    for mid, (status, value) in results.items():
        lat, lon = lookup[mid]
        cached = _load_json_meta(con, _payload_key(mid))
        if status != "ok":
            reason = "rate-limited twice" if status == "429" else str(value)
            log.warning("fire danger fetch failed for %s: %s", mid, reason)
            if cached:
                out[mid] = cached
            continue
        payload = _compute_payload(value, lat, lon, today, mid)
        if payload is None:
            if cached:
                out[mid] = cached
            continue
        store.set_meta(con, _gate_key(mid),
                        json.dumps({"date": today, "noon_passed": noon_passed}))
        store.set_meta(con, _payload_key(mid), json.dumps(payload, ensure_ascii=False))
        out[mid] = payload

    return out


def _fetch_batch(items: list[tuple[str, float, float]],
                  forecast_days: int) -> dict[str, tuple[str, object]]:
    """Fetch a batch concurrently (bounded, see _MAX_WORKERS). Each result is
    ("ok", daily_dict), ("429", None) for Open-Meteo's rate limit, or
    ("error", exception) for anything else; 429 is kept distinct because it is the
    one outcome update_all() retries.
    """
    out: dict[str, tuple[str, object]] = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_daily, lat, lon, forecast_days): mid
                   for mid, lat, lon in items}
        for future in as_completed(futures):
            mid = futures[future]
            try:
                out[mid] = ("ok", future.result())
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 429:
                    out[mid] = ("429", None)
                else:
                    out[mid] = ("error", exc)
            except Exception as exc:
                out[mid] = ("error", exc)
    return out
