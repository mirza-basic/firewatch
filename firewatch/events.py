"""Group detections into fire events and work out what changed since last poll.

A single fire produces many detections: one per sensor, per overpass, per pixel.
Reporting them raw means dozens of duplicate alerts for one fire. So detections
are clustered in space *and* time (single linkage), and alerting happens at the
level of the cluster - one fire, one identity, tracked over its lifetime.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from . import geo
from .config import CFG
from .store import iso, parse_iso, utcnow

SEVERITY = [(200.0, "severe"), (50.0, "high"), (10.0, "moderate"), (0.0, "low")]

# Selectable view ranges - all rolling windows measured back from now, so a fire
# never drops out of the shortest range just because the clock passed midnight.
RANGES = {
    "24h": {"label": "Last 24h", "short": "24h", "hours": 24.0},
    "3d": {"label": "Last 3 days", "short": "3 days", "hours": 72.0},
    "7d": {"label": "Last 7 days", "short": "7 days", "hours": 168.0},
    "30d": {"label": "Last month", "short": "Month", "hours": 720.0},
    # The year view is a read over whatever history is stored; it costs no API
    # traffic, but it is only as deep as the database. A fresh install shows the
    # same thing here as in "Last month" until `backfill` has run.
    "1y": {"label": "Last year", "short": "Year", "hours": 8760.0},
}
DEFAULT_RANGE = "3d"

# Older builds offered a local-calendar-day "today" range; map it forward so a
# saved default_range does not become invalid.
RANGE_ALIASES = {"today": "24h"}


def resolve_range(name: str | None) -> str:
    """Normalise a range key, tolerating retired names."""
    if not name:
        return DEFAULT_RANGE
    name = RANGE_ALIASES.get(name, name)
    return name if name in RANGES else DEFAULT_RANGE


def range_cutoff(name: str, now: datetime | None = None) -> datetime:
    """UTC instant that a named range starts at."""
    now = now or utcnow()
    return now - timedelta(hours=RANGES[resolve_range(name)]["hours"])


def filter_events(evs: list[dict], name: str, now: datetime | None = None) -> list[dict]:
    """Events with any activity inside the range.

    Matching is on last_ts: a fire that started six days ago but was still burning
    an hour ago belongs in "Last 24h". Its detection series is trimmed to the range
    so the map's timeline and sparkline stay consistent with the selection.
    """
    cutoff = range_cutoff(name, now)
    out = []
    for e in evs:
        if parse_iso(e["last_ts"]) < cutoff:
            continue
        e = dict(e)
        e["series"] = [s for s in e["series"] if parse_iso(s["ts"]) >= cutoff]
        out.append(e)
    return out


def range_counts(evs: list[dict], now: datetime | None = None) -> dict:
    """How many events and detections each range would show."""
    out = {}
    for name in RANGES:
        sel = filter_events(evs, name, now)
        out[name] = {
            "events": len(sel),
            "active": sum(1 for e in sel if e["status"] == "active"),
            "detections": sum(len(e["series"]) for e in sel),
        }
    return out


def severity(max_frp: float | None) -> str:
    if max_frp is None:
        return "unknown"
    for threshold, label in SEVERITY:
        if max_frp >= threshold:
            return label
    return "low"


def _event_id(uid: str) -> str:
    return hashlib.sha1(uid.encode()).hexdigest()[:12]


def cluster(dets: list[dict]) -> list[list[dict]]:
    """Single-linkage clustering in space-time via union-find."""
    n = len(dets)
    if n == 0:
        return []
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    radius = CFG["cluster_radius_km"]
    gap = timedelta(hours=CFG["cluster_gap_hours"])
    times = [parse_iso(d["ts"]) for d in dets]
    order = sorted(range(n), key=lambda i: times[i])

    for ai in range(n):
        i = order[ai]
        for bi in range(ai + 1, n):
            j = order[bi]
            if times[j] - times[i] > gap:
                break               # sorted by time, so nothing further can match
            if geo.haversine_km(dets[i]["lat"], dets[i]["lon"],
                                dets[j]["lat"], dets[j]["lon"]) <= radius:
                union(i, j)

    groups: dict[int, list[dict]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(dets[i])
    return list(groups.values())


def build_events(dets: list[dict]) -> list[dict]:
    """Turn detections into event records, newest activity first."""
    now = utcnow()
    quiet_after = timedelta(hours=CFG["quiet_hours"])
    events = []

    for group in cluster(dets):
        group = sorted(group, key=lambda d: d["ts"])
        frps = [d["frp"] for d in group if d.get("frp") is not None]
        lats = [d["lat"] for d in group]
        lons = [d["lon"] for d in group]
        lat, lon = geo.centroid(list(zip(lats, lons)))
        last_ts = parse_iso(group[-1]["ts"])
        age_min = (now - last_ts).total_seconds() / 60.0

        # Spatial extent as the diagonal of the detection bounding box. The exact
        # max-pairwise distance is O(n^2), which a month-long event with thousands
        # of detections makes expensive; the diagonal is O(n) and never smaller.
        extent = geo.haversine_km(min(lats), min(lons), max(lats), max(lons))

        # FRP of the most recent detection that reported one.
        latest_frp = next((d["frp"] for d in reversed(group)
                           if d.get("frp") is not None), None)
        sources = sorted({d["source"] for d in group})
        dist_town, dir_town = geo.from_town(lat, lon)

        events.append({
            "id": _event_id(group[0]["uid"]),
            "first_ts": iso(parse_iso(group[0]["ts"])),
            "last_ts": iso(last_ts),
            "age_min": round(age_min, 1),
            "lat": round(lat, 5), "lon": round(lon, 5),
            "n_det": len(group),
            "max_frp": max(frps) if frps else None,
            "latest_frp": latest_frp,
            "sum_frp": round(sum(frps), 2) if frps else None,
            "sources": sources,
            "sensors": sorted({d["sensor"] for d in group if d.get("sensor")}),
            "status": "active" if (now - last_ts) <= quiet_after else "quiet",
            "inside": any(d["inside"] for d in group),
            "place": geo.describe_location(lat, lon),
            # components so the map can localise the phrase
            "place_parts": geo.location_parts(lat, lon),
            "dist_town_km": round(dist_town, 1),
            "dir_town": dir_town,
            "extent_km": round(extent, 2),
            "severity": severity(max(frps) if frps else None),
            "series": [{"ts": d["ts"], "frp": d.get("frp"),
                        "source": d["source"], "lat": d["lat"], "lon": d["lon"]}
                       for d in group],
        })

    events.sort(key=lambda e: e["last_ts"], reverse=True)
    return events


def diff(previous: dict[str, dict], current: list[dict]) -> list[dict]:
    """Compare against the previous snapshot and return alert records.

    Alert kinds:
      new         a fire we have never reported
      reignited   a previously quiet event is producing detections again
      grew        more detections and a measurably larger footprint
      intensified radiative power jumped
      corroborated a second independent satellite now sees it
      extinguished no detections for `quiet_hours` (informational)
    """
    alerts = []
    factor = CFG["grow_frp_factor"]
    min_mw = CFG["grow_frp_min_mw"]

    for ev in current:
        old = previous.get(ev["id"])

        if old is None:
            # Only alert on a fire that is actually burning now. Backfilling
            # history, or a first run against a populated database, would
            # otherwise fire a notification for every fire that ever happened.
            if ev["status"] == "active":
                alerts.append({"kind": "new", "event": ev,
                               "detail": f"{ev['n_det']} detection(s), "
                                         f"{_frp(ev['max_frp'])}"})
            continue

        if old.get("status") == "quiet" and ev["status"] == "active":
            alerts.append({"kind": "reignited", "event": ev,
                           "detail": f"active again, {_frp(ev['latest_frp'])}"})

        gained = set(ev["sources"]) - set(old.get("sources", []))
        if gained:
            alerts.append({"kind": "corroborated", "event": ev,
                           "detail": f"now also seen by {', '.join(sorted(gained))}"})

        o_frp, n_frp = old.get("max_frp"), ev.get("max_frp")
        if o_frp and n_frp and n_frp >= o_frp * factor and (n_frp - o_frp) >= min_mw:
            alerts.append({"kind": "intensified", "event": ev,
                           "detail": f"{o_frp:.0f} to {n_frp:.0f} MW"})

        if ev["n_det"] > old.get("n_det", 0):
            grew_km = ev["extent_km"] - old.get("extent_km", 0.0)
            if grew_km >= 0.5:
                alerts.append({"kind": "grew", "event": ev,
                               "detail": f"footprint +{grew_km:.1f} km "
                                         f"(now {ev['extent_km']:.1f} km across)"})

        if old.get("status") == "active" and ev["status"] == "quiet":
            alerts.append({"kind": "extinguished", "event": ev,
                           "detail": f"no detection for {ev['age_min']/60:.1f} h"})

    return alerts


def _frp(v) -> str:
    return f"{v:.1f} MW" if v is not None else "FRP n/a"


def summarise(events: list[dict]) -> dict:
    active = [e for e in events if e["status"] == "active"]
    inside = [e for e in active if e["inside"]]
    return {
        "n_active": len(active),
        "n_active_inside": len(inside),
        "n_total": len(events),
        "worst": max((e["max_frp"] or 0) for e in active) if active else 0.0,
        "severity": severity(max((e["max_frp"] or 0) for e in active)) if active else "none",
    }
