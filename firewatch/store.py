"""SQLite persistence for detections, clustered events and notification state."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import place
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


PLACE_KEY = "place_id"


def _stamp_place(con: sqlite3.Connection) -> None:
    """Record which municipality this database holds detections for.

    Re-pointing an instance at another place - which is the whole of forking this
    repository - leaves every stored detection outside the new clip, and nothing
    about that is visible: the map is empty because the old fires are filtered
    out, the new area has none yet, and it reads as a broken feed. The stamp is
    what lets `place`, `poll` and `reclip` say so instead.

    Deliberately not self-healing. Deleting rows because a config file changed is
    the wrong default when the file might have changed by mistake - and retention
    is 400 days, so what goes is not coming back from the feeds. `reclip --apply`
    does it, on purpose, after a backup.
    """
    row = con.execute("SELECT v FROM meta WHERE k=?", (PLACE_KEY,)).fetchone()
    if row is not None:
        return
    # Only an empty database. Stamping one that already holds detections would
    # assert something unknown - and would assert it wrongly in exactly the case
    # this exists for, since a fork clones a repository whose committed database
    # is full of the original municipality's fires and only then re-points the
    # place. Those are answered geometrically in place_mismatch() instead.
    if con.execute("SELECT 1 FROM detections LIMIT 1").fetchone() is None:
        con.execute("INSERT INTO meta(k, v) VALUES(?, ?)", (PLACE_KEY, place.PLACE["id"]))
        con.commit()


def place_mismatch(con: sqlite3.Connection) -> str | None:
    """The place this database was stamped for, when it is not the configured one.

    Only ever answers from the stamp. Guessing from geometry was tried and is
    wrong: a fork usually starts from a *neighbouring* municipality, whose
    boundaries touch, so a third of one place's history legitimately falls inside
    the other's clip - 35 of 198 for the two this was measured against. "Mostly
    outside" would be a threshold with nothing behind it. Databases predating the
    stamp answer None here, and `place` reports the out-of-scope count instead,
    which is a fact rather than an inference.
    """
    row = con.execute("SELECT v FROM meta WHERE k=?", (PLACE_KEY,)).fetchone()
    if row is None:
        return None
    return row["v"] if row["v"] != place.PLACE["id"] else None


def adopt_place(con: sqlite3.Connection) -> None:
    """Re-stamp after the old place's detections have been cleared out."""
    con.execute("INSERT INTO meta(k, v) VALUES(?, ?)"
                " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (PLACE_KEY, place.PLACE["id"]))
    con.commit()


def connect() -> sqlite3.Connection:
    ensure_dirs()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    _migrate(con)
    _stamp_place(con)
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


def out_of_scope(con: sqlite3.Connection) -> list[dict]:
    """Stored detections the current spatial clip would no longer accept.

    Re-derived from the geometry rather than trusting the stored `inside` flag, so
    this stays correct if the boundary file is ever regenerated too. Lowering
    nearby_buffer_km does not retroactively touch history - the clip only runs at
    fetch time - so this is what finds what the old, wider setting let in.
    """
    from . import geo
    from .config import CFG
    km = float(CFG["nearby_buffer_km"])
    gone = []
    for r in con.execute("SELECT * FROM detections"):
        d = dict(r)
        if geo.point_in_boundary(d["lat"], d["lon"]):
            continue
        if geo.distance_to_boundary_km(d["lat"], d["lon"]) <= km:
            continue
        gone.append(d)
    return gone


def delete_detections(con: sqlite3.Connection, uids: list[str]) -> int:
    """Remove detections by uid, in batches SQLite will accept.

    Events are left alone on purpose: they are rebuilt from the surviving
    detections on the next cycle, and save_events() deletes any event id that no
    longer comes out of the clustering.
    """
    n = 0
    for i in range(0, len(uids), 400):
        chunk = uids[i:i + 400]
        n += con.execute(
            "DELETE FROM detections WHERE uid IN (%s)" % ",".join("?" * len(chunk)),
            tuple(chunk)).rowcount
    con.commit()
    return n


def stats(con: sqlite3.Connection) -> dict:
    row = con.execute(
        "SELECT COUNT(*) n, MIN(ts) mn, MAX(ts) mx FROM detections"
    ).fetchone()
    return {"detections": row["n"], "oldest": row["mn"], "newest": row["mx"]}
