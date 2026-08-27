"""Active-fire data sources.

Three independent feeds, all keyless:

  mtg      EUMETSAT Meteosat Third Generation FRP, via the public EUMETView WFS.
           Geostationary: a new 10-minute slice for the whole disk, published with
           roughly 25 minutes of latency. This is what makes near-live alerting
           possible - the polar-orbiting feeds simply cannot.
  firms    NASA FIRMS VIIRS + MODIS near-real-time. Far more sensitive (375 m
           pixels vs ~4 km) but only 4-7 overpasses a day and ~3 h latency.
           Best at catching small/new fires; useless for continuity.
  s3       Sentinel-3 A/B SLSTR FRP, also via EUMETView WFS. A couple of extra
           overpasses a day at 1 km resolution.

Together: MTG gives continuity, FIRMS/S3 give sensitivity and corroboration.

Note on the WFS: BBOX(geom,...) silently returns zero features on these layers,
so spatial filtering is done on the Lat/Lon *attributes* instead, which works.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from . import geo
from .config import CFG, FIRMS_SIGNUP_URL, firms_key
from .store import iso, parse_iso, utcnow

log = logging.getLogger("firewatch.sources")

WFS_URL = "https://view.eumetsat.int/geoserver/wfs"
FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov"

MTG_LAYER = "mtg_fd:frp"
S3_LAYERS = ["copernicus:sentinel3a_slstr_level2_frp",
             "copernicus:sentinel3b_slstr_level2_frp"]


class SourceError(RuntimeError):
    pass


class NoCredentials(SourceError):
    """A source needs a key that has not been supplied.

    Distinct from SourceError so the poller can report "not configured" rather than
    "failed" - a missing key is a setup step, not an outage, and the two want very
    different words in the status line.
    """


def _force_ipv4() -> None:
    """Make urllib3 resolve A records only, when FIREWATCH_FORCE_IPV4 is set.

    Needed wherever a host publishes AAAA records but the network has no IPv6
    route: the connection then fails with `[Errno 101] Network is unreachable`
    rather than falling back. GitHub Actions runners are exactly this - and of the
    four hosts FireWatch talks to, `firms.modaps.eosdis.nasa.gov` is the only one
    with an AAAA record, so FIRMS was the only feed that broke while Meteosat,
    Sentinel-3 and the weather lookup all worked.

    Opt-in, not automatic: a working dual-stack network resolves this correctly on
    its own, and hard-coding IPv4 would break an IPv6-only host.
    """
    if not (os.environ.get("FIREWATCH_FORCE_IPV4") or "").strip():
        return
    try:
        import socket

        import urllib3.util.connection as u3
        u3.allowed_gai_family = lambda: socket.AF_INET
    except Exception as exc:          # never let a tuning knob break a cycle
        log.warning("could not force IPv4: %s", exc)


def _session() -> requests.Session:
    _force_ipv4()
    s = requests.Session()
    s.headers.update({"User-Agent": CFG["user_agent"]})
    return s


def _classify(lat: float, lon: float) -> tuple[bool, bool]:
    """(inside_municipality, within_nearby_buffer)."""
    if geo.point_in_boundary(lat, lon):
        return True, True
    near = geo.distance_to_boundary_km(lat, lon) <= CFG["nearby_buffer_km"]
    return False, near


# --------------------------------------------------------------------------- WFS

# Query cost on these layers grows sharply with the length of the time filter:
# ~5 s for 24 h, ~21 s for 72 h, and a 30-day window does not return at all. So
# long windows are split into chunks and unioned client-side.
WFS_CHUNK_HOURS = 48.0

# How far back each EUMETView layer actually holds data, measured over a 600 km
# box (where any 48 h summer window has fires, so an empty answer means an empty
# archive and not a quiet sky): MTG returns nothing past ~40 days, while the
# Sentinel-3 layers keep thinning out but still answer beyond a year. `backfill`
# clamps to these instead of crawling hundreds of chunks that can only be empty.
ARCHIVE_DAYS = {"mtg": 40, "s3": 400}


def _wfs_features(session: requests.Session, layer: str, since: datetime,
                  pad_km: float, until: datetime | None = None) -> list[dict]:
    w, s, e, n = geo.bbox_padded(pad_km)
    cql = (f"time AFTER {iso(since)}"
           f" AND Lat BETWEEN {s:.4f} AND {n:.4f}"
           f" AND Lon BETWEEN {w:.4f} AND {e:.4f}")
    if until is not None:
        cql += f" AND time BEFORE {iso(until)}"
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": layer, "outputFormat": "application/json",
        "count": "3000", "CQL_FILTER": cql,
    }
    # The EUMETView gateway throws intermittent 502/503s; a couple of quick
    # retries turn a lost cycle into a small delay.
    last = ""
    for attempt in range(3):
        try:
            r = session.get(WFS_URL, params=params, timeout=CFG["http_timeout"])
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}"
        else:
            if r.status_code == 200:
                try:
                    return r.json().get("features", [])
                except ValueError:
                    last = f"non-JSON ({len(r.content)} bytes)"
            else:
                last = f"HTTP {r.status_code}"
        if attempt < 2:
            time.sleep(2 + 3 * attempt)
    raise SourceError(f"{layer}: {last}")


def _wfs_features_chunked(session: requests.Session, layer: str,
                          since_hours: float, pad_km: float) -> list[dict]:
    """Fetch a possibly long window as a series of bounded queries."""
    now = utcnow()
    if since_hours <= WFS_CHUNK_HOURS:
        return _wfs_features(session, layer, now - timedelta(hours=since_hours), pad_km)

    feats: list[dict] = []
    remaining = since_hours
    while remaining > 0:
        chunk = min(WFS_CHUNK_HOURS, remaining)
        start = now - timedelta(hours=remaining)
        end = start + timedelta(hours=chunk)
        try:
            feats.extend(_wfs_features(session, layer, start, pad_km, until=end))
        except SourceError as exc:
            # One bad chunk should not lose the whole backfill.
            log.warning("%s chunk %s..%s: %s", layer, iso(start), iso(end), exc)
        remaining -= chunk
    return feats


def fetch_mtg(since_hours: float = 6.0) -> list[dict]:
    """Meteosat Third Generation FRP points (10-minute cadence)."""
    session = _session()
    feats = _wfs_features_chunked(session, MTG_LAYER, since_hours,
                                  CFG["nearby_buffer_km"])
    out = []
    minconf = CFG["mtg_min_confidence"]
    for f in feats:
        p = f.get("properties", {})
        try:
            lat = float(p["Lat"]); lon = float(p["Lon"])
        except (KeyError, TypeError, ValueError):
            continue
        conf = p.get("Confidence")
        if conf is not None and float(conf) < minconf:
            continue
        inside, near = _classify(lat, lon)
        if not near:
            continue
        ts = p.get("time") or p.get("Datetime")
        out.append({
            "uid": f"mtg:{lat:.4f}:{lon:.4f}:{ts}",
            "source": "mtg", "sensor": "MTG-FCI",
            "lat": lat, "lon": lon, "ts": iso(parse_iso(ts)),
            "frp": _f(p.get("FRP")), "confidence": _f(conf),
            "daynight": None, "inside": inside,
            "raw": {k: p.get(k) for k in ("FRP", "FRPerr", "Confidence",
                                          "BT_mir_k", "BT_tir_k", "Datetime")},
        })
    return out


def fetch_sentinel3(since_hours: float = 24.0) -> list[dict]:
    """Sentinel-3 SLSTR FRP points."""
    session = _session()
    out = []
    for layer in S3_LAYERS:
        try:
            feats = _wfs_features_chunked(session, layer, since_hours,
                                          CFG["nearby_buffer_km"])
        except SourceError as exc:
            log.warning("sentinel3 %s: %s", layer, exc)
            continue
        sat = "S3A" if "3a" in layer else "S3B"
        for f in feats:
            p = f.get("properties", {})
            try:
                lat = float(p["Lat"]); lon = float(p["Lon"])
            except (KeyError, TypeError, ValueError):
                continue
            inside, near = _classify(lat, lon)
            if not near:
                continue
            ts = p.get("time") or p.get("Datetime")
            out.append({
                "uid": f"s3:{sat}:{lat:.4f}:{lon:.4f}:{ts}",
                "source": "s3", "sensor": f"SLSTR-{sat}",
                "lat": lat, "lon": lon, "ts": iso(parse_iso(ts)),
                "frp": _f(p.get("FRP")), "confidence": _f(p.get("Confidence")),
                "daynight": None, "inside": inside,
                "raw": {k: p.get(k) for k in ("FRP", "FRPerr", "Confidence",
                                              "BT", "UsedChannel", "Satellite")},
            })
    return out


# ------------------------------------------------------------------------- FIRMS

def _firms_sensor(ds: str) -> str:
    """Dataset name reduced to the satellite that flew it.

    VIIRS_SNPP_NRT and VIIRS_SNPP_SP are the same instrument on the same overpass,
    processed twice. The dataset name is part of the uid, so keeping the suffix
    would store one pixel as two detections - inflating counts and footprints for
    every period the live feed and the archive both cover.
    """
    for suffix in ("_NRT", "_SP"):
        if ds.endswith(suffix):
            return ds[: -len(suffix)]
    return ds


def fetch_firms(days: int = 1, start_date: str | None = None,
                datasets: list[str] | None = None) -> list[dict]:
    """NASA FIRMS VIIRS/MODIS hotspots.

    `start_date` is the first day of the window and `days` counts forward from it
    (the API caps days at 5). Costs 2 transactions per dataset against the
    5000-per-10-minute key limit, so a full sweep of 4 datasets is 8 - negligible.
    `datasets` overrides the live list, which is how `fetch_firms_range` reaches
    the SP archive for windows the NRT feed no longer serves.
    """
    session = _session()
    key, key_source = firms_key()
    if not key:
        # Not an error: Meteosat and Sentinel-3 need no credentials and carry the
        # cycle on their own. Raising here would mark the source failed and bury the
        # one thing the operator needs to read.
        raise NoCredentials(
            "no FIRMS key - VIIRS/MODIS skipped. Get one free at "
            f"{FIRMS_SIGNUP_URL} then run `set-firms-key`")
    w, s, e, n = geo.bbox_padded(CFG["nearby_buffer_km"])
    area = f"{w:.4f},{s:.4f},{e:.4f},{n:.4f}"
    start = start_date or utcnow().strftime("%Y-%m-%d")
    days = max(1, min(int(days), 5))          # API accepts [1..5] only
    out = []
    for ds in datasets or CFG["firms_datasets"]:
        url = f"{FIRMS_BASE}/api/area/csv/{key}/{ds}/{area}/{days}/{start}"
        try:
            r = session.get(url, timeout=CFG["http_timeout"])
        except requests.RequestException as exc:
            log.warning("firms %s: %s", ds, exc)
            continue
        body = r.text.strip()
        if r.status_code != 200 or not body or "," not in body.split("\n")[0]:
            # FIRMS signals errors with HTTP 400 and a bare English sentence
            # ("Invalid day range. Expects [1..5].") rather than JSON, so the
            # header-shape check is what distinguishes prose from real CSV.
            # A valid query with no fires is HTTP 200 with the header only.
            log.warning("firms %s: unexpected response %r", ds, body[:120])
            continue
        for row in csv.DictReader(io.StringIO(body)):
            try:
                lat = float(row["latitude"]); lon = float(row["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            inside, near = _classify(lat, lon)
            if not near:
                continue
            ts = _firms_ts(row.get("acq_date"), row.get("acq_time"))
            if ts is None:
                continue
            sensor = _firms_sensor(ds)
            out.append({
                "uid": f"firms:{sensor}:{lat:.5f}:{lon:.5f}:{iso(ts)}",
                "source": "firms", "sensor": sensor,
                "lat": lat, "lon": lon, "ts": iso(ts),
                "frp": _f(row.get("frp")),
                "confidence": _firms_conf(row.get("confidence")),
                "daynight": row.get("daynight"), "inside": inside,
                "raw": {"dataset": ds,
                        **{k: row.get(k) for k in
                           ("bright_ti4", "bright_ti5", "brightness", "bright_t31",
                            "frp", "confidence", "satellite", "instrument", "version")
                           if row.get(k) is not None}},
            })
    return out


def _firms_ts(date_s: str | None, time_s: str | None):
    if not date_s:
        return None
    try:
        hhmm = (time_s or "0").strip().zfill(4)
        return datetime.strptime(f"{date_s} {hhmm}", "%Y-%m-%d %H%M").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def _firms_conf(v):
    """VIIRS reports l/n/h; MODIS reports 0-100. Normalise to a number."""
    if v is None:
        return None
    v = str(v).strip()
    return {"l": 25.0, "n": 60.0, "h": 90.0}.get(v.lower(), _f(v))


def _f(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def firms_quota() -> dict | None:
    """Current transaction usage for the configured map key."""
    try:
        r = _session().get(
            f"{FIRMS_BASE}/mapserver/mapkey_status/",
            params={"MAP_KEY": firms_key()[0] or ""}, timeout=30)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def fetch_firms_range(days_back: int) -> list[dict]:
    """FIRMS over more than 5 days, by stitching consecutive 5-day windows.

    Windows older than `firms_nrt_days` are asked of the SP archive instead of the
    live feed: NRT answers them with a header-only CSV, which looks exactly like a
    quiet fortnight. Both lists are tried across the boundary window so nothing
    falls between them.
    """
    out = []
    remaining = max(1, int(days_back))
    start = utcnow() - timedelta(days=remaining - 1)
    nrt_from = utcnow() - timedelta(days=CFG["firms_nrt_days"])
    while remaining > 0:
        chunk = min(5, remaining)
        end = start + timedelta(days=chunk)
        sets = []
        if end >= nrt_from:
            sets.append(CFG["firms_datasets"])
        if start < nrt_from:
            sets.append(CFG["firms_archive_datasets"])
        for ds in sets:
            out.extend(fetch_firms(days=chunk, start_date=start.strftime("%Y-%m-%d"),
                                   datasets=ds))
        start = end
        remaining -= chunk
    return out
