"""Command line entry points.

    python3 -m firewatch menubar     run the menu bar app (default)
    python3 -m firewatch poll        one cycle, print a report, exit
    python3 -m firewatch watch       headless loop, notifications only
    python3 -m firewatch status [range]  show last known state
    python3 -m firewatch backfill [days] deep-fetch history (default 30)

  [range] is 24h | 3d | 7d | 30d  (default: 3d, or default_range from config)
    python3 -m firewatch map         rebuild and open the fire map
    python3 -m firewatch quota       FIRMS transaction usage
    python3 -m firewatch history [n] recent detections from the database
    python3 -m firewatch test-notify send a test notification
"""
from __future__ import annotations

import json
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import events as ev_mod
from . import mapgen, notify, poller, sources, store
from .config import CFG, MAP_PATH, SNAPSHOT_PATH


def _print_snapshot(snap: dict, rng: str | None = None) -> None:
    requested = rng or snap.get("default_range")
    rng = ev_mod.resolve_range(requested)
    if requested and requested not in ev_mod.RANGES and requested not in ev_mod.RANGE_ALIASES:
        print(f"unknown range {requested!r}; use one of {', '.join(ev_mod.RANGES)}")
        return
    all_evs = snap.get("events", [])
    evs = ev_mod.filter_events(all_evs, rng)
    label = ev_mod.RANGES[rng]["label"]
    n_act = sum(1 for e in evs if e["status"] == "active")
    n_det = sum(len(e.get("series", [])) for e in evs)

    print(f"\nFireWatch Zavidovići · {snap.get('generated_at')}")
    print(f"  Range: {label}  (since {snap.get('range_cutoffs', {}).get(rng, '?')})")
    print(f"  {n_act} active · {len(evs)} in range · {n_det} detections")
    counts = snap.get("range_counts", {})
    if counts:
        bits = [f"{ev_mod.RANGES[k]['label']}: {v.get('events', 0)}"
                for k, v in counts.items() if k in ev_mod.RANGES]
        print(f"  All ranges → {'  |  '.join(bits)}")
    for name, st in snap.get("source_status", {}).items():
        flag = "ok " if st.get("ok") else "FAIL"
        print(f"  [{flag}] {name:6s} {st.get('detail', '')}")
    if not evs:
        print(f"\n  No fires · {label.lower()}. 🌲\n")
        return
    for e in evs:
        icon = "🔥" if e["status"] == "active" else "💤"
        print(f"\n  {icon} {e['severity'].upper():8s} {e['place']}")
        print(f"     {e['lat']:.5f}, {e['lon']:.5f}  "
              f"({e['dist_town_km']} km {e['dir_town']} of town)"
              f"{'' if e['inside'] else '  [outside municipality]'}")
        peak = f"{e['max_frp']:.1f}" if e.get("max_frp") is not None else "n/a"
        last = f"{e['latest_frp']:.1f}" if e.get("latest_frp") is not None else "n/a"
        shown = len(e.get("series", []))
        det_txt = (f"{shown} of {e['n_det']} detections" if shown != e["n_det"]
                   else f"{e['n_det']} detections")
        print(f"     FRP peak {peak} MW / latest {last} MW · {det_txt}"
              f" from {', '.join(e['sources'])}")
        print(f"     first {e['first_ts']} → last {e['last_ts']}"
              f" ({e['age_min']:.0f} min ago) · {e['extent_km']} km across")
        w = e.get("weather")
        if w:
            print(f"     {w['temp']}°C, RH {w['humidity']}%, wind "
                  f"{w['speed']:.0f} km/h from {w['from']} → spreads {w['towards']}"
                  f" · {e.get('risk', '?')} spread risk")
        print(f"     https://www.google.com/maps?q={e['lat']},{e['lon']}")
    print()


def cmd_poll(rng: str | None = None) -> int:
    poller.setup_logging()
    snap = poller.Poller().poll_now()
    _print_snapshot(snap, rng)
    if snap.get("alerts_sent"):
        print("  alerts sent:", ", ".join(
            f"{a['kind']}({a['detail']})" for a in snap["alerts_sent"]), "\n")
    print(f"  map: {MAP_PATH}\n  snapshot: {SNAPSHOT_PATH}\n")
    return 0


def cmd_watch() -> int:
    poller.setup_logging()
    p = poller.Poller()
    p.start()
    print("FireWatch running headless. Ctrl-C to stop.")
    try:
        while True:
            p._stop.wait(3600)
    except KeyboardInterrupt:
        p.stop()
        print("\nstopped")
    return 0


def cmd_status(rng: str | None = None) -> int:
    _print_snapshot(poller.load_snapshot(), rng)
    return 0


def cmd_backfill(days: int = 30) -> int:
    poller.setup_logging()
    r = poller.backfill(days)
    print(f"backfill: fetched {r['fetched']}, {r['new']} new")
    snap = poller.Poller().poll_now()
    _print_snapshot(snap, "30d")
    return 0


def cmd_map() -> int:
    snap = poller.load_snapshot()
    path = mapgen.render(snap)
    print(f"wrote {path}")
    webbrowser.open(Path(path).as_uri())
    return 0


def cmd_quota() -> int:
    q = sources.firms_quota()
    if not q:
        print("could not read quota")
        return 1
    print(f"FIRMS key: {q['current_transactions']} / {q['transaction_limit']}"
          f" transactions per {q['transaction_interval']}")
    print("  (an area query costs 2; a full 4-dataset sweep costs 8)")
    return 0


def cmd_history(n: int = 40) -> int:
    con = store.connect()
    rows = con.execute(
        "SELECT ts, source, sensor, lat, lon, frp, confidence, inside"
        " FROM detections ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    print(f"\n{'time (UTC)':21s} {'source':7s} {'sensor':18s} "
          f"{'lat':>9s} {'lon':>9s} {'FRP':>7s} {'conf':>5s} in")
    print("-" * 86)
    for r in rows:
        frp = f"{r['frp']:.1f}" if r["frp"] is not None else "-"
        cf = f"{r['confidence']:.0f}" if r["confidence"] is not None else "-"
        print(f"{r['ts']:21s} {r['source']:7s} {r['sensor'] or '':18s} "
              f"{r['lat']:9.4f} {r['lon']:9.4f} {frp:>7s} {cf:>5s} "
              f"{'Y' if r['inside'] else 'n'}")
    print(f"\n{store.stats(con)}\n")
    return 0


def cmd_test_notify() -> int:
    ok = notify.send("🔥 FireWatch test", "Notifications are working",
                     subtitle="Grad Zavidovići", sound=CFG["sound_update"])
    print(f"backend={notify.backend()} delivered={ok}")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    cmd = (argv[0] if argv else "menubar").lower()
    if cmd in ("menubar", "app", "ui"):
        from .menubar import main as ui_main
        ui_main()
        return 0
    if cmd == "poll":
        return cmd_poll(argv[1] if len(argv) > 1 else None)
    if cmd == "watch":
        return cmd_watch()
    if cmd == "status":
        return cmd_status(argv[1] if len(argv) > 1 else None)
    if cmd == "map":
        return cmd_map()
    if cmd == "backfill":
        return cmd_backfill(int(argv[1]) if len(argv) > 1 else 30)
    if cmd == "quota":
        return cmd_quota()
    if cmd == "history":
        return cmd_history(int(argv[1]) if len(argv) > 1 else 40)
    if cmd in ("test-notify", "testnotify"):
        return cmd_test_notify()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
