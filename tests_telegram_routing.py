"""Verify per-municipality Telegram routing: fan-out to every matched
municipality, a missing channel skipped rather than blocking the others, and
one channel's post failure never suppresses another's."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from firewatch import telegram

passed = failed = 0


def check(name, got, want):
    global passed, failed
    ok = got == want
    passed += ok
    failed += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {name:55s} got={got!r} want={want!r}")


def ev(municipalities, kind="new"):
    return {"kind": kind, "event": {
        "municipalities": municipalities, "max_frp": 12.0, "latest_frp": 12.0,
        "severity": "moderate", "n_det": 3, "sources": ["mtg"], "extent_km": 1.0,
        "inside": True, "lat": 44.0, "lon": 18.0, "place": "test",
        "place_parts": {"name": None, "km": None, "dir": None},
    }}


# --- channel_for() against a fixture, not the real (partially-provisioned) data ---
fixture = [
    {"id": "has-channel", "telegram_channel": "-1001111111111"},
    {"id": "no-channel", "telegram_channel": ""},
    {"id": "missing-key"},
]
tmp = Path("/tmp/tests_telegram_routing_fixture.json")
tmp.write_text(json.dumps(fixture))
telegram.BIH_MUNI_FILE = tmp
telegram._municipality_channels.cache_clear()

check("configured municipality resolves", telegram.channel_for("has-channel"),
     "-1001111111111")
check("empty telegram_channel resolves to None", telegram.channel_for("no-channel"), None)
check("missing telegram_channel key resolves to None",
     telegram.channel_for("missing-key"), None)
check("unknown municipality id resolves to None", telegram.channel_for("nope"), None)

# --- send_alert() fan-out, independence, and skip-on-missing - send() faked so
# no network call happens and every call is recorded instead ---
calls = []


def fake_send(text, chat_id):
    calls.append(chat_id)
    return chat_id != "-1001111111111"  # one deliberately "fails" to prove independence


telegram.send = fake_send
telegram.CFG = {"telegram_kinds": ["new"]}

calls.clear()
result = telegram.send_alert(ev(["has-channel", "no-channel"]))
check("fan-out calls send() only for the configured one", calls, ["-1001111111111"])
check("send_alert reports overall success even though that one call 'failed'",
     result, False)  # fake_send returns False for this id on purpose

calls.clear()
telegram.channel_for = lambda mid: {"a": "-100A", "b": "-100B"}.get(mid)
telegram.send = lambda text, chat_id: calls.append(chat_id) or True
result = telegram.send_alert(ev(["a", "b"]))
check("both matched municipalities get posted to (Sarajevo/Istocno Sarajevo case)",
     sorted(calls), ["-100A", "-100B"])
check("send_alert reports success when at least one post succeeded", result, True)

result = telegram.send_alert(ev([], kind="new"))
check("no municipalities on the event -> nothing sent, no crash", result, False)

result = telegram.send_alert(ev(["a"], kind="extinguished"))
check("a kind not in telegram_kinds is filtered before any lookup", result, False)

tmp.unlink(missing_ok=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
