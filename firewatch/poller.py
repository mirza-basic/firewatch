"""The polling engine: fetch, store, cluster, diff, notify, publish.

Each source is polled on its own schedule (MTG fast, FIRMS/Sentinel-3 slow) and a
failure in one never blocks the others. After every cycle a snapshot JSON and the
HTML map are rewritten so the menu bar and map always reflect current state.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import threading
import time
from datetime import timedelta

from . import enrich, events, expose, firedanger, imagery, mapgen, notify, sms, sources, store, telegram
from .config import (CFG, LOG_PATH, SNAPSHOT_PATH, RedactingFormatter,
                     ensure_dirs, public_url as config_public_url)
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
    """File plus stdout.

    The file rotates because a host runs this for months, not an afternoon, and an
    unbounded log on a small VPS is a slow leak. stdout is what journald and
    `docker logs` capture, so in a container the file is the redundant one.
    """
    ensure_dirs()
    handlers = [
        logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ]
    fmt = RedactingFormatter("%(asctime)s %(levelname)-7s %(name)-19s %(message)s")
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
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
        """Start the background polling thread (no-op if already running)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="firewatch-poll",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the polling loop to exit after the current cycle."""
        self._stop.set()

    def poll_now(self, sources_to_poll=None) -> dict:
        """Force an immediate cycle (used by the menu bar's Refresh)."""
        for k in (sources_to_poll or SOURCE_SPECS):
            self._next_due[k] = 0.0
        return self.cycle()

    # --------------------------------------------------------------------- loop
    def _loop(self) -> None:
        """Run cycles until stopped; a failed cycle is logged, never fatal."""
        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception:
                log.exception("poll cycle failed")
                self.last_error = "cycle failed - see log"
            # Wake often enough to honour the shortest interval.
            self._stop.wait(20)

    def _interval(self, name: str) -> float:
        """Seconds until a source is next due (MTG polls faster while a fire is active)."""
        if name == "mtg":
            active = self.snapshot.get("summary", {}).get("n_active", 0)
            return CFG["interval_mtg_active"] if active else CFG["interval_mtg"]
        return CFG[SOURCE_SPECS[name]["interval_key"]]

    # -------------------------------------------------------------------- cycle
    def cycle(self) -> dict:
        """Run one fetch -> store -> cluster -> alert -> publish pass.

        Returns the new snapshot, or the previous one if no source was due.
        """
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
            except sources.NoCredentials as exc:
                # A setup step, not an outage. Logged at info and shown as
                # "not configured" so it never reads as a feed that broke, and
                # never triggers the retry-and-alarm path an outage would.
                log.info("source %s: %s", name, exc)
                self.source_status[name] = {
                    "ok": False, "n": 0, "at": iso(utcnow()),
                    "configured": False, "detail": str(exc)[:160]}
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

            # Keyless catalogue check every cycle, a render only when the scene
            # date changes - the look is free, the pixels are not.
            s2 = imagery.refresh(con)

            # Cheap to check, rare to do work: gated inside per municipality to at
            # most twice a day each. Returns a dict keyed by municipality - see
            # firedanger.update_all().
            try:
                fire_danger = firedanger.update_all(con)
            except Exception:
                log.exception("fire danger update failed")
                fire_danger = {}

            alerts = events.diff(previous, current)
            store.save_events(con, current)

            # Cooldown is per (event, kind). Several kinds can be true of one event
            # in the same cycle (see events.diff()), and one near-identical message
            # per kind reads as spam, so eligible alerts are grouped by event and
            # each channel sends at most one merged message per event per cycle.
            eligible = [a for a in alerts if not store.was_notified(
                con, a["event"]["id"], a["kind"], CFG["notify_cooldown_min"])]
            groups: dict[str, list[dict]] = {}
            for a in eligible:
                groups.setdefault(a["event"]["id"], []).append(a)

            sms_kinds = set(CFG.get("sms_kinds") or [])
            telegram_kinds = set(CFG.get("telegram_kinds") or [])
            sent = []
            for ev_id, group in groups.items():
                notified = notify.notify_alert_group(group)
                # SMS and Telegram are separate channels and must go out even if
                # the desktop notification failed - the Mac may be asleep or
                # locked with nobody looking at it. Hence OR, not a gate.
                try:
                    texted = sms.send_alert_group(group)
                except Exception:
                    log.exception("sms alert failed")
                    texted = False
                try:
                    posted = telegram.send_alert_group(group)
                except Exception:
                    log.exception("telegram alert failed")
                    posted = False
                kinds_label = "+".join(a["kind"] for a in group)
                log.info("alerted %s: %s [notify=%s sms=%s telegram=%s]",
                         kinds_label, group[0]["event"]["place"],
                         notified, texted, posted)
                # A kind counts as delivered only through a channel that would have
                # included it: sms_kinds/telegram_kinds filter per kind within a
                # merged message, and marking every kind notified because something
                # sent would suppress a kind neither channel carries if it later
                # shows up alone.
                for a in group:
                    delivered = notified or (texted and a["kind"] in sms_kinds) \
                        or (posted and a["kind"] in telegram_kinds)
                    if delivered:
                        store.mark_notified(con, ev_id, a["kind"])
                        sent.append(a)

            # Resolved before the snapshot is written: the menu bar and map read the
            # URL from it, so a dead tunnel must not be advertised for another cycle.
            # A configured address skips ngrok entirely (no agent to ask on a host).
            public_url = config_public_url()
            if not public_url:
                try:
                    public_url = expose.ensure()
                except Exception:
                    log.exception("public map check failed")
                    public_url = None

            snap = {
                "generated_at": iso(utcnow()),
                "public_url": public_url,
                "events": current,
                "summary": events.summarise(current),
                "ranges": {k: v["label"] for k, v in events.RANGES.items()},
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
                # None until a scene has been rendered; the map then omits the layer.
                "imagery": s2,
                # Keyed by municipality id; only municipalities that have computed
                # at least once appear, and a missing one gets no reading in its
                # popup rather than a stale or fabricated one.
                "fire_danger": fire_danger,
                # See _telegram_channel_url().
                "telegram_url": _telegram_channel_url(),
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
        """Return the current snapshot."""
        with self.lock:
            return self.snapshot


def _telegram_channel_url() -> str | None:
    """Global public join link for the alert channel - always None.

    There is one private channel per municipality (invite link, not a public
    handle), so no single link applies. Per-municipality subscribe links come
    from telegram.channel_for() on the map side.
    """
    return None


def backfill(days: int = 30) -> dict:
    """One-off deep fetch so the longer view ranges have history behind them.

    The steady-state loop pulls ~24 h per cycle; a fresh install has an empty
    database, and a 7-day filter over two days of data would mislead.
    """
    log.info("backfilling %d days from all sources", days)
    # Each WFS source is clamped to its own archive depth; asking MTG for a year
    # is 183 chunked requests that can only come back empty.
    mtg_days = min(days, sources.ARCHIVE_DAYS["mtg"])
    s3_days = min(days, sources.ARCHIVE_DAYS["s3"])
    got: list[dict] = []
    for name, fn in (
        ("mtg", lambda: sources.fetch_mtg(since_hours=mtg_days * 24)),
        ("firms", lambda: sources.fetch_firms_range(days)),
        ("s3", lambda: sources.fetch_sentinel3(since_hours=s3_days * 24)),
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
    """Snapshot with no events, used before the first cycle or when none is on disk."""
    return {"generated_at": iso(utcnow()), "events": [], "public_url": None,
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
            "alerts_sent": [], "notify_backend": notify.backend(),
            "imagery": None, "fire_danger": {}, "telegram_url": None}


def load_snapshot() -> dict:
    """Read the last published snapshot from disk."""
    try:
        return json.loads(SNAPSHOT_PATH.read_text())
    except Exception:
        return _empty_snapshot()
