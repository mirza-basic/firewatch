"""SQLite persistence for detections, clustered events and notification state."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    uid        TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    sensor     TEXT,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    ts         TEXT NOT NULL,          -- ISO8601 UTC, observation time
    frp        REAL,                   -- megawatts, NULL if not reported
    confidence REAL,
    daynight   TEXT,
    inside     INTEGER NOT NULL,       -- 1 = inside municipality, 0 = nearby
    first_seen TEXT NOT NULL,          -- when we first pulled it
    raw        TEXT
);
CREATE INDEX IF NOT EXISTS idx_det_ts ON detections(ts);

CREATE TABLE IF NOT EXISTS events (
    id         TEXT PRIMARY KEY,
    first_ts   TEXT NOT NULL,
    last_ts    TEXT NOT NULL,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    n_det      INTEGER NOT NULL,
    max_frp    REAL,
    sum_frp    REAL,
    sources    TEXT,
    status     TEXT,                   -- active | quiet
    place      TEXT,
    inside     INTEGER,
    extent_km  REAL,
    created_at TEXT,
    updated_at TEXT,
    payload    TEXT                    -- full JSON of the event
);

CREATE TABLE IF NOT EXISTS notified (
    event_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    at         TEXT NOT NULL,
    PRIMARY KEY (event_id, kind)
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Detection uids used to carry the full FIRMS dataset name, suffix and all, so
# VIIRS_SNPP_NRT and VIIRS_SNPP_SP - the same instrument, the same overpass,
# processed twice - stored one pixel as two detections. That only became reachable
# when `backfill` started fetching the SP archive for history, and it would have
# double-counted every FIRMS detection in the overlap. Stored rows have to be
# collapsed too, or the old ones never match the new uids.
UID_SCHEME = 2


def _migrate(con: sqlite3.Connection) -> None:
    row = con.execute("SELECT v FROM meta WHERE k='uid_scheme'").fetchone()
    if row and int(row["v"]) >= UID_SCHEME:
        return
    for suffix in ("_NRT", "_SP"):
        con.execute(
            "UPDATE OR IGNORE detections"
            "   SET uid = replace(uid, ?, ''), sensor = replace(sensor, ?, '')"
            " WHERE source = 'firms' AND instr(uid, ?) > 0",
            (suffix, suffix, suffix))
    # Whatever OR IGNORE skipped is a row whose collapsed uid is already taken:
    # the duplicate this migration exists to remove.
    con.execute("DELETE FROM detections WHERE source = 'firms'"
                " AND (instr(uid, '_NRT') > 0 OR instr(uid, '_SP') > 0)")
    con.execute("INSERT INTO meta(k, v) VALUES('uid_scheme', ?)"
                " ON CONFLICT(k) DO UPDATE SET v = excluded.v", (str(UID_SCHEME),))
    con.commit()


def connect() -> sqlite3.Connection:
    ensure_dirs()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def upsert_detections(con: sqlite3.Connection, dets: list[dict]) -> list[dict]:
    """Insert detections, returning only those not seen before.

    This is the core of "compare with the last response": novelty is decided by
    the database, not by diffing whole API payloads, so a detection re-reported
    across polls or sensors is only ever new once.
    """
    fresh: list[dict] = []
    now = iso(utcnow())
    for d in dets:
        cur = con.execute(
            "INSERT OR IGNORE INTO detections"
            " (uid,source,sensor,lat,lon,ts,frp,confidence,daynight,inside,first_seen,raw)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (d["uid"], d["source"], d.get("sensor"), d["lat"], d["lon"], d["ts"],
             d.get("frp"), d.get("confidence"), d.get("daynight"),
             1 if d.get("inside") else 0, now, json.dumps(d.get("raw", {}))),
        )
        if cur.rowcount:
            fresh.append(d)
    con.commit()
    return fresh


def recent_detections(con: sqlite3.Connection, hours: float) -> list[dict]:
    cutoff = iso(utcnow() - timedelta(hours=hours))
    rows = con.execute(
        "SELECT * FROM detections WHERE ts >= ? ORDER BY ts", (cutoff,)
    ).fetchall()
    return [dict(r) for r in rows]


def load_events(con: sqlite3.Connection) -> dict[str, dict]:
    rows = con.execute("SELECT id, payload FROM events").fetchall()
    out = {}
    for r in rows:
        try:
            out[r["id"]] = json.loads(r["payload"])
        except Exception:
            continue
    return out


def save_events(con: sqlite3.Connection, events: list[dict]) -> None:
    now = iso(utcnow())
    keep = {e["id"] for e in events}
    for e in events:
        existing = con.execute(
            "SELECT created_at FROM events WHERE id=?", (e["id"],)
        ).fetchone()
        created = existing["created_at"] if existing else now
        con.execute(
            "INSERT INTO events"
            " (id,first_ts,last_ts,lat,lon,n_det,max_frp,sum_frp,sources,status,place,"
            "  inside,extent_km,created_at,updated_at,payload)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "  first_ts=excluded.first_ts, last_ts=excluded.last_ts, lat=excluded.lat,"
            "  lon=excluded.lon, n_det=excluded.n_det, max_frp=excluded.max_frp,"
            "  sum_frp=excluded.sum_frp, sources=excluded.sources, status=excluded.status,"
            "  place=excluded.place, inside=excluded.inside, extent_km=excluded.extent_km,"
            "  updated_at=excluded.updated_at, payload=excluded.payload",
            (e["id"], e["first_ts"], e["last_ts"], e["lat"], e["lon"], e["n_det"],
             e.get("max_frp"), e.get("sum_frp"), ",".join(e.get("sources", [])),
             e.get("status"), e.get("place"), 1 if e.get("inside") else 0,
             e.get("extent_km"), created, now, json.dumps(e, ensure_ascii=False)),
        )
    if keep:
        con.execute(
            "DELETE FROM events WHERE id NOT IN (%s)" % ",".join("?" * len(keep)),
            tuple(keep),
        )
    else:
        con.execute("DELETE FROM events")
    con.commit()


def was_notified(con: sqlite3.Connection, event_id: str, kind: str,
                 cooldown_min: float) -> bool:
    row = con.execute(
        "SELECT at FROM notified WHERE event_id=? AND kind=?", (event_id, kind)
    ).fetchone()
    if not row:
        return False
    age_min = (utcnow() - parse_iso(row["at"])).total_seconds() / 60.0
    return age_min < cooldown_min


def mark_notified(con: sqlite3.Connection, event_id: str, kind: str) -> None:
    con.execute(
        "INSERT INTO notified (event_id,kind,at) VALUES (?,?,?)"
        " ON CONFLICT(event_id,kind) DO UPDATE SET at=excluded.at",
        (event_id, kind, iso(utcnow())),
    )
    con.commit()


def get_meta(con: sqlite3.Connection, key: str, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def set_meta(con: sqlite3.Connection, key: str, value) -> None:
    con.execute(
        "INSERT INTO meta (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, str(value)),
    )
    con.commit()


def prune(con: sqlite3.Connection, keep_days: int = 30) -> int:
    cutoff = iso(utcnow() - timedelta(days=keep_days))
    n = con.execute("DELETE FROM detections WHERE ts < ?", (cutoff,)).rowcount
    con.execute("DELETE FROM notified WHERE at < ?", (cutoff,))
    con.commit()
    return n


def stats(con: sqlite3.Connection) -> dict:
    row = con.execute(
        "SELECT COUNT(*) n, MIN(ts) mn, MAX(ts) mx FROM detections"
    ).fetchone()
    return {"detections": row["n"], "oldest": row["mn"], "newest": row["mx"]}
