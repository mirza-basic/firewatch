"""Verify push alerts: topic routing (one per municipality, the country topic once),
the notification content, kind filtering, and that nothing is sent unconfigured. FCM is
faked, so no network call and no google-auth are needed."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from firewatch import push

passed = failed = 0


def check(name, got, want):
    """Tally a pass if `got == want`."""
    global passed, failed
    ok = got == want
    passed += ok
    failed += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {name:55s} got={got!r} want={want!r}")


def alert(municipalities, kind="new", event_id="evt-1"):
    """Build a minimal alert record for the given municipality ids."""
    return {"kind": kind, "event": {
        "id": event_id, "municipalities": municipalities, "max_frp": 12.0,
        "latest_frp": 12.0, "severity": "moderate", "n_det": 3, "sources": ["mtg"],
        "extent_km": 1.0, "inside": True, "lat": 44.2017, "lon": 17.9077,
        "place": "test", "place_parts": {"name": "Lovnica", "km": 2.0, "dir": "SW"},
    }}


# --- not ready: nothing is published ---------------------------------------------
push.CFG = {"push_enabled": False, "telegram_kinds": ["new", "grew"], "push_kinds": [],
            "connect_timeout": 1, "http_timeout": 1, "user_agent": "t"}
published = []
push.send = lambda payload: published.append(payload["message"]) or True

check("not ready when push_enabled is false", push.ready()[0], False)
check("send_alert_group does nothing when not ready",
      push.send_alert_group([alert(["zenica"])]), False)
check("...and published nothing", published, [])

# --- ready (the credential check is faked, no real key needed) --------------------
push.CFG["push_enabled"] = True
push.ready = lambda: (True, "ready")

published.clear()
check("single municipality reports success", push.send_alert_group([alert(["zenica"])]), True)
check("topics: its municipality then the country, once each",
      [m["topic"] for m in published], ["fire.zenica", "fire.all"])

published.clear()
push.send_alert_group([alert(["sarajevo-centar", "istocno-novo-sarajevo"])])
check("two municipalities: both topics, country topic only once",
      [m["topic"] for m in published],
      ["fire.sarajevo-centar", "fire.istocno-novo-sarajevo", "fire.all"])

published.clear()
check("no municipality means no push", push.send_alert_group([alert([])]), False)
check("...and nothing published", published, [])

# --- kinds: falls back to telegram_kinds, filters, merges -------------------------
published.clear()
check("a kind outside the list is not sent",
      push.send_alert_group([alert(["zenica"], kind="extinguished")]), False)
push.send_alert_group([alert(["zenica"], kind="new"), alert(["zenica"], kind="grew"),
                       alert(["zenica"], kind="extinguished")])
check("merged into one notification per topic", len(published), 2)
check("merged kinds in data, filtered ones dropped", published[0]["data"]["kind"], "new+grew")

push.CFG["push_kinds"] = ["extinguished"]
published.clear()
push.send_alert_group([alert(["zenica"], kind="new")])
check("push_kinds overrides telegram_kinds", published, [])
push.CFG["push_kinds"] = []

# --- message content --------------------------------------------------------------
published.clear()
push.send_alert_group([alert(["zenica"])])
m = published[0]
check("data values are all strings", all(isinstance(v, str) for v in m["data"].values()), True)
check("data carries what the app needs", {k: m["data"][k] for k in ("type", "lat", "lon", "z", "event_id")},
      {"type": "fire", "lat": "44.2017", "lon": "17.9077", "z": "14", "event_id": "evt-1"})
check("municipality in data", m["data"]["municipality"], "zenica")
check("notification has a title and a body", bool(m["notification"]["title"]) and bool(m["notification"]["body"]), True)
check("android channel and tag", m["android"]["notification"],
      {"channel_id": "crisis_alerts", "tag": "evt-1"})
check("country message names the first municipality", published[1]["data"]["municipality"], "zenica")

# --- one failure never blocks the rest --------------------------------------------
calls = []
push.send = lambda payload: calls.append(payload["message"]["topic"]) or payload["message"]["topic"] != "fire.zenica"
check("a failed topic does not block the others, and success is still reported",
      push.send_alert_group([alert(["zenica", "tuzla"])]), True)
check("all three topics were attempted", calls, ["fire.zenica", "fire.tuzla", "fire.all"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
