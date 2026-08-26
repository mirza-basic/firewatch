"""Command line entry points.

    python3 -m firewatch menubar     run the menu bar app (default)
    python3 -m firewatch poll        one cycle, print a report, exit
    python3 -m firewatch watch       headless loop, notifications only
    python3 -m firewatch status [range]  show last known state
    python3 -m firewatch backfill [days] deep-fetch history (default 30)

  [range] is 24h | 3d | 7d | 30d | 1y  (default: 3d, or default_range from config)
    python3 -m firewatch map         rebuild and open the fire map
    python3 -m firewatch quota       FIRMS transaction usage
    python3 -m firewatch expose      publish the map via ngrok, print the URL
    python3 -m firewatch unexpose    stop publishing (other tunnels untouched)
    python3 -m firewatch expose-status
    python3 -m firewatch history [n] recent detections from the database
    python3 -m firewatch test-notify
    python3 -m firewatch sms-status    SMS backend + settings
    python3 -m firewatch test-sms      send a sample alert SMS
    python3 -m firewatch set-sms-key   store the httpSMS API key
    python3 -m firewatch sms-add <number>     add an SMS recipient
    python3 -m firewatch sms-remove <number>  remove one send a test notification
"""
from __future__ import annotations

import json
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import events as ev_mod
from . import expose as expose_mod
from . import sms as sms_mod
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


def cmd_expose() -> int:
    try:
        t = expose_mod.expose()
    except expose_mod.ExposeError as exc:
        print(f"\n  could not publish: {exc}\n")
        return 1
    url = t.get("public_url", "?")
    print(f"\n  Fire map published at\n    {url}\n")
    if t.get("_reused"):
        print("  (tunnel already existed — reused it)")
    if t.get("_started_agent"):
        print("  (started a new ngrok agent, since none was running)")
    elif t.get("_siblings"):
        print(f"  (joined the running agent alongside {t['_siblings']} existing tunnel(s))")
    print(f"  serving: {expose_mod.PUBLIC_DIR}")
    print("  the poller keeps this directory in sync every cycle")
    print("\n  free-tier URLs are random and change when the agent restarts.")
    print("  stop with: python3 -m firewatch unexpose\n")
    return 0


def cmd_unexpose() -> int:
    try:
        removed = expose_mod.unexpose()
    except expose_mod.ExposeError as exc:
        print(f"\n  {exc}\n")
        return 1
    print("  tunnel removed" if removed else "  no firewatch tunnel was running")
    return 0


def cmd_expose_status() -> int:
    st = expose_mod.status()
    print(f"\n  ngrok binary : {st['ngrok'] or 'not found'}")
    print(f"  agent running: {st['agent_up']}")
    print(f"  keep published: {st['auto_expose']}"
          "  (tunnel is rebuilt each cycle if it goes away)")
    print(f"  public dir   : {st['public_dir']}")
    print(f"  staged files : {', '.join(st['staged']) or '(none — not exposed)'}")
    print(f"  fire map URL : {st['firewatch_tunnel'] or '(not published)'}")
    if st["all_tunnels"]:
        print("  all tunnels on this agent:")
        for t in st["all_tunnels"]:
            print(f"    {t['name']}: {t['url']} -> {t['addr']}")
    print()
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


def cmd_sms_status() -> int:
    from .config import CFG
    ok, why = sms_mod.ready()
    print(f"\n  usable       : {ok}  ({why})")
    print(f"  enabled      : {CFG['sms_enabled']}")
    print(f"  from         : {CFG['sms_from'] or '(unset)'}")
    rec = sms_mod.recipients()
    print(f"  to           : {', '.join(rec) if rec else '(none)'}  [{len(rec)}]")
    print(f"  api key      : {'found' if sms_mod.api_key() else 'not found'}")
    print(f"  alert kinds  : {', '.join(CFG['sms_kinds'])}")
    print(f"  max chars    : {CFG['sms_max_chars']}")
    print(f"  map url      : {sms_mod.map_url() or '(map not published)'}\n")
    return 0


def cmd_set_sms_key() -> int:
    """Store the httpSMS API key in the macOS Keychain.

    Prompted rather than passed on the command line so it never lands in shell
    history or a process listing.
    """
    import getpass
    import shutil
    import subprocess
    if not shutil.which("security"):
        print("  macOS `security` tool not found")
        return 1
    key = getpass.getpass("  httpSMS API key (not echoed): ").strip()
    if not key:
        print("  nothing entered")
        return 1
    r = subprocess.run(["security", "add-generic-password", "-U",
                        "-a", "firewatch", "-s", sms_mod.KEYCHAIN_SERVICE,
                        "-w", key], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  keychain write failed: {r.stderr.strip()[:200]}")
        return 1
    ok, why = sms_mod.ready()
    print(f"  stored. usable now: {ok} ({why})")
    return 0


def _save_recipients(nums: list[str]) -> None:
    from .config import CFG
    CFG["sms_to"] = nums
    CFG.save()


def cmd_sms_add(number: str | None) -> int:
    if not number or not number.startswith("+"):
        print("  give a number in E.164 form, e.g. sms-add +38761234567")
        return 1
    rec = sms_mod.recipients()
    if number in rec:
        print(f"  {number} is already a recipient")
        return 0
    rec.append(number)
    _save_recipients(rec)
    print(f"  added. recipients now: {', '.join(rec)}")
    print("  run ./firewatch-ctl restart to apply to the running service")
    return 0


def cmd_sms_remove(number: str | None) -> int:
    rec = sms_mod.recipients()
    if not number or number not in rec:
        print(f"  not a recipient. current: {', '.join(rec) or '(none)'}")
        return 1
    rec.remove(number)
    _save_recipients(rec)
    print(f"  removed. recipients now: {', '.join(rec) or '(none)'}")
    print("  run ./firewatch-ctl restart to apply to the running service")
    return 0


def cmd_test_sms() -> int:
    """Send the test message, and print what a real alert would look like.

    Only the first is sent. A test that arrives reading "FIRE NEW: ..." is
    indistinguishable from the real thing on a recipient's phone, so the sample
    alert is shown here for its formatting and segment count and goes no further.
    """
    poller.setup_logging()
    text = sms_mod.test_text()
    ok, why = sms_mod.ready()
    print(f"\n  usable: {ok} ({why})")
    print(f"  {len(text)} chars, {sms_mod.segments(text)} segment(s)")
    print("  ---- message to send ----")
    print("\n".join("  | " + l for l in text.splitlines()))

    evs = poller.load_snapshot().get("events") or []
    if evs:
        alert = {"kind": "new", "event": evs[0], "detail": "sample"}
        sample = sms_mod.alert_text(alert)
        print(f"  ---- a real alert, for comparison (not sent) ----")
        print("\n".join("  | " + l for l in sample.splitlines()))
        print(f"  {len(sample)} chars, {sms_mod.segments(sample)} segment(s)")

    if not ok:
        print("\n  not sent\n")
        return 1
    sent = sms_mod.send(text)
    print(f"\n  delivered: {sent}\n")
    return 0 if sent else 1


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
    if cmd == "expose":
        return cmd_expose()
    if cmd == "unexpose":
        return cmd_unexpose()
    if cmd in ("expose-status", "exposestatus"):
        return cmd_expose_status()
    if cmd == "quota":
        return cmd_quota()
    if cmd == "history":
        return cmd_history(int(argv[1]) if len(argv) > 1 else 40)
    if cmd in ("sms-status", "smsstatus"):
        return cmd_sms_status()
    if cmd in ("test-sms", "testsms"):
        return cmd_test_sms()
    if cmd in ("sms-add", "smsadd"):
        return cmd_sms_add(argv[1] if len(argv) > 1 else None)
    if cmd in ("sms-remove", "smsremove"):
        return cmd_sms_remove(argv[1] if len(argv) > 1 else None)
    if cmd in ("set-sms-key", "setsmskey"):
        return cmd_set_sms_key()
    if cmd in ("test-notify", "testnotify"):
        return cmd_test_notify()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
