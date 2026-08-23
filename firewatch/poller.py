"""The polling engine: fetch, store, cluster, diff, notify, publish.

Each source is polled on its own schedule (MTG fast, FIRMS/Sentinel-3 slow) and a
failure in one never blocks the others. After every cycle a snapshot JSON and the
HTML map are rewritten so the menu bar and map always reflect current state.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import timedelta

from . import enrich, events, mapgen, notify, sms, sources, store
from .config import CFG, LOG_PATH, SNAPSHOT_PATH, ensure_dirs
from .store import iso, utcnow

log = logging.getLogger("firewatch.poller")

# Each poll only needs enough overlap to catch late-published data - the full
# history lives in SQLite. Querying the WFS over 72 h costs ~21 s versus ~4 s for
# 24 h and returns nothing extra, so the windows are kept deliberately short.
SOURCE_SPECS = {
    "mtg": dict(fn=lambda: sources.fetch_mtg(since_hours=24),
                interval_key="interval_mtg"),
    "firms": dict(fn=lambda: sources.fetch_firms(days=2),
                  interval_key="interval_firms"),
    "s3": dict(fn=lambda: sources.fetch_sentinel3(since_hours=30),
               interval_key="interval_sentinel3"),
}


def setup_logging(verbose: bool = False) -> None:
    ensure_dirs()
    handlers = [logging.FileHandler(LOG_PATH), logging.StreamHandler()]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-19s %(message)s",
        handlers=handlers, force=True)


class Poller:
    """Owns the polling loop and the current snapshot."""

    def __init__(self, on_update=None):
        self.on_update = on_update
        self.lock = threading.RLock()
        self.snapshot: dict = _empty_snapshot()
        self.source_status: dict[str, dict] = {}
        self._next_due: dict[str, float] = {k: 0.0 for k in SOURCE_SPECS}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="firewatch-poll",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def poll_now(self, sources_to_poll=None) -> dict:
        """Force an immediate cycle (used by the menu bar's Refresh)."""
        for k in (sources_to_poll or SOURCE_SPECS):
            self._next_due[k] = 0.0
        return self.cycle()

    # --------------------------------------------------------------------- loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception:
                log.exception("poll cycle failed")
                self.last_error = "cycle failed - see log"
            # Wake often enough to honour the shortest interval.
            self._stop.wait(20)

    def _interval(self, name: str) -> float:
        if name == "mtg":
            active = self.snapshot.get("summary", {}).get("n_active", 0)
            return CFG["interval_mtg_active"] if active else CFG["interval_mtg"]
        return CFG[SOURCE_SPECS[name]["interval_key"]]

    # -------------------------------------------------------------------- cycle
    def cycle(self) -> dict:
        now = time.monotonic()
        fetched: list[dict] = []
        polled: list[str] = []

        for name, spec in SOURCE_SPECS.items():
            if now < self._next_due[name]:
                continue
            polled.append(name)
            t0 = time.time()
            try:
                dets = spec["fn"]()
                fetched.extend(dets)
                self.source_status[name] = {
                    "ok": True, "n": len(dets), "at": iso(utcnow()),
                    "detail": f"{len(dets)} detections in {time.time()-t0:.1f}s"}
            except Exception as exc:
                log.warning("source %s failed: %s", name, exc)
                self.source_status[name] = {
                    "ok": False, "n": 0, "at": iso(utcnow()),
                    "detail": f"{type(exc).__name__}: {exc}"[:160]}
            self._next_due[name] = time.monotonic() + self._interval(name)

        if not polled:
            return self.snapshot

        con = store.connect()
        try:
            fresh = store.upsert_detections(con, fetched)
            if fresh:
                log.info("%d new detection(s) from %s", len(fresh),
                         ",".join(sorted({d['source'] for d in fresh})))

            window = store.recent_detections(con, CFG["window_hours"])
            previous = store.load_events(con)
            current = events.build_events(window)

            # Enrich only what a person will actually look at.
            for ev in current:
                if ev["status"] == "active":
                    w = enrich.weather(ev["lat"], ev["lon"])
                    ev["weather"] = w
                    ev["risk"] = enrich.fire_risk(w)

            alerts = events.diff(previous, current)
            store.save_events(con, current)

            sent = []
            for a in alerts:
                ev_id, kind = a["event"]["id"], a["kind"]
                if store.was_notified(con, ev_id, kind, CFG["notify_cooldown_min"]):
                    continue
                notified = notify.notify_alert(a)
                # SMS is a separate channel and must go out even if the desktop
                # notification failed - the Mac may be asleep or locked with
                # nobody looking at it. Hence OR, not a gate.
                try:
                    texted = sms.send_alert(a)
                except Exception:
                    log.exception("sms alert failed")
                    texted = False
                if notified or texted:
                    store.mark_notified(con, ev_id, kind)
                    sent.append(a)
                    log.info("alerted %s: %s (%s) [notify=%s sms=%s]", kind,
                             a["event"]["place"], a.get("detail", ""),
                             notified, texted)

            snap = {
                "generated_at": iso(utcnow()),
                "events": current,
                "summary": events.summarise(current),
                "ranges": {k: v["label"] for k, v in events.RANGES.items()},
            "ranges_short": {k: v.get("short", v["label"])
                             for k, v in events.RANGES.items()},
                "ranges_short": {k: v.get("short", v["label"])
                                 for k, v in events.RANGES.items()},
                "range_counts": events.range_counts(current),
                "range_cutoffs": {k: iso(events.range_cutoff(k))
                                  for k in events.RANGES},
                "default_range": CFG.get("default_range", events.DEFAULT_RANGE),
                "source_status": dict(self.source_status),
                "window_hours": CFG["window_hours"],
                "buffer_km": CFG["nearby_buffer_km"],
                "n_detections": len(window),
                "alerts_sent": [{"kind": a["kind"], "id": a["event"]["id"],
                                 "detail": a.get("detail", "")} for a in sent],
                "notify_backend": notify.backend(),
            }
            with self.lock:
                self.snapshot = snap
            SNAPSHOT_PATH.write_text(json.dumps(snap, ensure_ascii=False, indent=1))
            mapgen.render(snap)
            store.set_meta(con, "last_cycle", iso(utcnow()))
            self._maybe_prune(con)
            self.last_error = None

            if self.on_update:
                try:
                    self.on_update(snap)
                except Exception:
                    log.exception("on_update callback failed")
            return snap
        finally:
            con.close()

    @staticmethod
    def _maybe_prune(con) -> None:
        """Trim detections older than a month, at most once a day."""
        last = store.get_meta(con, "last_prune")
        if last:
            try:
                if (utcnow() - store.parse_iso(last)) < timedelta(days=1):
                    return
            except Exception:
                pass
        removed = store.prune(con, keep_days=int(CFG["retention_days"]))
        store.set_meta(con, "last_prune", iso(utcnow()))
        if removed:
            log.info("pruned %d detection(s) older than %s days",
                     removed, CFG["retention_days"])

    def get(self) -> dict:
        with self.lock:
            return self.snapshot


def backfill(days: int = 30) -> dict:
    """One-off deep fetch so the longer view ranges have history behind them.

    The steady-state loop only pulls ~24 h per cycle, which is all it needs once
    running - but a fresh install has an empty database, and a 7-day filter over
    two days of data is misleading rather than useful.
    """
    log.info("backfilling %d days from all sources", days)
    got: list[dict] = []
    for name, fn in (
        ("mtg", lambda: sources.fetch_mtg(since_hours=days * 24)),
        ("firms", lambda: sources.fetch_firms_range(days)),
        ("s3", lambda: sources.fetch_sentinel3(since_hours=days * 24)),
    ):
        try:
            d = fn()
            got.extend(d)
            log.info("  %s: %d detections", name, len(d))
        except Exception as exc:
            log.warning("  %s backfill failed: %s", name, exc)

    con = store.connect()
    try:
        fresh = store.upsert_detections(con, got)
        log.info("backfill stored %d new of %d fetched", len(fresh), len(got))
        return {"fetched": len(got), "new": len(fresh)}
    finally:
        con.close()


def _empty_snapshot() -> dict:
    return {"generated_at": iso(utcnow()), "events": [],
            "summary": {"n_active": 0, "n_active_inside": 0, "n_total": 0,
                        "worst": 0.0, "severity": "none"},
            "ranges": {k: v["label"] for k, v in events.RANGES.items()},
            "ranges_short": {k: v.get("short", v["label"])
                             for k, v in events.RANGES.items()},
            "range_counts": events.range_counts([]),
            "range_cutoffs": {k: iso(events.range_cutoff(k)) for k in events.RANGES},
            "default_range": CFG.get("default_range", events.DEFAULT_RANGE),
            "source_status": {}, "window_hours": CFG["window_hours"],
            "buffer_km": CFG["nearby_buffer_km"], "n_detections": 0,
            "alerts_sent": [], "notify_backend": notify.backend()}


def load_snapshot() -> dict:
    """Read the last published snapshot from disk."""
    try:
        return json.loads(SNAPSHOT_PATH.read_text())
    except Exception:
        return _empty_snapshot()
