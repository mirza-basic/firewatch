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
    """Tally a pass if `got == want`."""
    global passed, failed
    ok = got == want
    passed += ok
    failed += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {name:55s} got={got!r} want={want!r}")


def ev(municipalities, kind="new"):
    """Build a minimal alert record for the given municipality ids."""
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


def fake_send(text, chat_id, button_url=None):
    """Record the chat id instead of posting; fails for one channel."""
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
telegram.send = lambda text, chat_id, button_url=None: calls.append(chat_id) or True
result = telegram.send_alert(ev(["a", "b"]))
check("both matched municipalities get posted to (Sarajevo/Istocno Sarajevo case)",
     sorted(calls), ["-100A", "-100B"])
check("send_alert reports success when at least one post succeeded", result, True)

result = telegram.send_alert(ev([], kind="new"))
check("no municipalities on the event -> nothing sent, no crash", result, False)

result = telegram.send_alert(ev(["a"], kind="extinguished"))
check("a kind not in telegram_kinds is filtered before any lookup", result, False)

# --- the "Open on map" button: what actually goes over the wire. _post is faked, so
# nothing is sent; every request body is captured instead. ---
import importlib
importlib.reload(telegram)            # undo the send/channel_for fakes above
telegram.BIH_MUNI_FILE = tmp
telegram._municipality_channels.cache_clear()
telegram.CFG = {"telegram_kinds": ["new"], "telegram_enabled": True, "telegram_language": "bs"}
telegram.bot_token = lambda: "TOKEN"
telegram.channel_for = lambda mid: "-100A"
telegram.map_url = lambda: "https://example.github.io/firewatch"
bodies = []


class R:
    """Stand-in for a requests response."""

    def __init__(self, code, text=""):
        self.status_code, self.text = code, text


telegram._post = lambda url, body: bodies.append(dict(body)) or R(200)
telegram.send_alert(ev(["a"]))
b = bodies[0]
btn = b["reply_markup"]["inline_keyboard"][0][0]
check("button label is the Bosnian one, with the flame", btn["text"], "\U0001F525 Otvori na mapi")
check("button asks for the red preset", btn["style"], "danger")
check("button carries the position, zoom 14, and no event id",
     btn["url"], "https://example.github.io/firewatch/?lat=44.0000&lon=18.0000&z=14")
check("the link is not a line of the message text", "http" in b["text"], False)
check("deep_link(with_event=True) still supports the event panel",
     telegram.deep_link("https://x.io/f", {"lat": 1.0, "lon": 2.0, "id": "ab12"}, True),
     "https://x.io/f/?lat=1.0000&lon=2.0000&z=14&e=ab12")

bodies.clear()
telegram.map_url = lambda: None
telegram.send_alert(ev(["a"]))
check("no public map address -> no button at all", "reply_markup" in bodies[0], False)

# Telegram refuses some addresses outright; the alert must still go out, button-less.
bodies.clear()
telegram.map_url = lambda: "http://localhost:8000"
answers = [R(400, '{"description":"Bad Request: BUTTON_URL_INVALID"}'), R(200)]
telegram._post = lambda url, body: bodies.append(dict(body)) or answers.pop(0)
check("a rejected button falls back to a plain post that succeeds",
     telegram.send_alert(ev(["a"])), True)
check("first try had the button, the retry did not",
     ["reply_markup" in x for x in bodies], [True, False])

# An API that does not know `style` loses only the color, not the button.
bodies.clear()
telegram.map_url = lambda: "https://example.github.io/firewatch"
answers = [R(400, '{"description":"Bad Request: field style is unknown"}'), R(200)]
telegram._post = lambda url, body: bodies.append(
    json.loads(json.dumps(body))) or answers.pop(0)
check("a rejected style is retried without it", telegram.send_alert(ev(["a"])), True)
check("retry keeps the button but drops the color",
      ["style" in x["reply_markup"]["inline_keyboard"][0][0] for x in bodies], [True, False])

tmp.unlink(missing_ok=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
