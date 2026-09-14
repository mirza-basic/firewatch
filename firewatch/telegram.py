"""Telegram alerts via the Bot API (https://core.telegram.org/bots/api).

A Telegram bot is created once (via @BotFather) and given a token; anyone who
wants alerts starts a chat with that bot, which is how their `chat_id` is
found (`getUpdates`, or forwarding an update from the bot to `getUpdates`).
Sending is then one call per recipient:

    POST https://api.telegram.org/bot<token>/sendMessage
    {"chat_id": <id>, "text": "…"}

No bulk endpoint exists, unlike httpSMS's `/bulk-send` - a fan-out to N chat
ids is N requests, which is why `send_alert` loops rather than posting once.

Telegram is UTF-8 end to end and the limit is 4096 characters per message, so
none of `sms.py`'s reasons to exist apply here:

* No GSM-7/UCS-2 split - Bosnian diacritics cost nothing, so alerts keep
  "Zavidovići" rather than folding to "Zavidovici".
* No one-segment budget to fit inside - a real alert here is under 200
  characters, nowhere near 4096, so there is no `worst_case()` degradation
  ladder to build or measure.

`TELEGRAM_TEXT` is `sms.SMS_TEXT` copied rather than imported, on purpose:
this project does not abstract two things that happen to look alike today
into one that would have to be prised back apart the day they diverge (see
`sms.py`'s own per-channel treatment of "extinguished" vs. `sms_kinds`, or
the day this module wants to add a line SMS could never afford). Wording
drifting apart later is a feature request, not a bug.
"""
from __future__ import annotations

import json
import logging
import os

import requests

from .config import CFG, keychain_secret

log = logging.getLogger("firewatch.telegram")

API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
KEYCHAIN_SERVICE = "firewatch-telegram"
# Chat ids from the environment, for deployments with no writable config file
# (containers, CI runners) and for public repositories - same reasoning as
# sms.SMS_TO_ENV, a chat id in a committed file is as public as a phone number.
TELEGRAM_TO_ENV = "FIREWATCH_TELEGRAM_TO"


# ------------------------------------------------------------------ credentials

def bot_token() -> str | None:
    """From the environment, else the macOS Keychain. Never from config.json."""
    env = os.environ.get("TELEGRAM_BOT_TOKEN")
    if env:
        return env.strip()
    return keychain_secret(KEYCHAIN_SERVICE)


def _live(key):
    """Read one setting from config.json *now* - see sms._live for why."""
    from .config import CONFIG_FILE
    try:
        return json.loads(CONFIG_FILE.read_text()).get(key)
    except (OSError, ValueError, AttributeError):
        return None


def recipients_source() -> str:
    return "environment" if (os.environ.get(TELEGRAM_TO_ENV) or "").strip() else "config.json"


def recipients() -> list[str]:
    """Chat ids alerts go to. Same override shape as `sms.recipients()`.

    Accepts a list, a single id, or a comma- or semicolon-separated string of
    ids - chat ids are just integers (negative for groups), so no E.164-style
    validation applies.
    """
    env = (os.environ.get(TELEGRAM_TO_ENV) or "").strip()
    if env:
        raw = env
    else:
        raw = _live("telegram_to")
        if raw is None:
            raw = CFG.get("telegram_to") or []
    if isinstance(raw, str):
        raw = raw.replace(";", ",").split(",")
    return [str(c).strip() for c in raw if c and str(c).strip()]


def ready() -> tuple[bool, str]:
    """(usable, reason) - mirrors sms.ready()."""
    if not CFG.get("telegram_enabled"):
        return False, "telegram_enabled is false"
    if not bot_token():
        return False, "no bot token (TELEGRAM_BOT_TOKEN or Keychain)"
    if not recipients():
        return False, f"no recipients ({TELEGRAM_TO_ENV} or telegram_to in config.json)"
    return True, "ready"


# ---------------------------------------------------------------------- content

def map_url() -> str | None:
    """The link put in an alert. See sms.map_url() - same resolution order."""
    from .config import public_url
    fixed = public_url()
    if fixed:
        return fixed
    try:
        from . import expose
        t = expose.find_tunnel()
        return (t or {}).get("public_url")
    except Exception:
        return None


# Alert wording, per language. A copy of sms.SMS_TEXT, not a shared import - see
# the module docstring. Kept with diacritics: nothing here folds them to ASCII.
TELEGRAM_TEXT = {
    "bs": {
        "kind": {"new": "NOVI POŽAR", "reignited": "PONOVO GORI",
                 "intensified": "POJAČAVA SE", "grew": "ŠIRI SE",
                 "extinguished": "UGAŠEN", "corroborated": "POTVRĐEN"},
        "sev": {"low": "NIZAK", "moderate": "UMJEREN", "high": "VISOK",
                "severe": "EKSTREMAN", "unknown": "NEPOZNAT"},
        "risk": {"elevated": "povišen", "high": "visok", "extreme": "ekstreman",
                 "moderate": "umjeren", "unknown": "nepoznat"},
        "peak": "maks", "now": "sada", "det": "det", "of_town": "od grada",
        "outside": "IZVAN OPĆINE", "wind": "Vjetar", "rh": "vlaga ",
        "risk_word": "rizik", "map": "Karta", "sample": "Primjer",
        "test": "FIREWATCH TEST - nema požara, provjera dostave Telegrama",
    },
    "en": {
        "kind": {"new": "NEW FIRE", "reignited": "BURNING AGAIN",
                 "intensified": "INTENSIFYING", "grew": "SPREADING",
                 "extinguished": "OUT", "corroborated": "CONFIRMED"},
        "sev": {"low": "LOW", "moderate": "MODERATE", "high": "HIGH",
                "severe": "SEVERE", "unknown": "UNKNOWN"},
        "risk": {"elevated": "elevated", "high": "high", "extreme": "extreme",
                 "moderate": "moderate", "unknown": "unknown"},
        "peak": "peak", "now": "now", "det": "det", "of_town": "of town",
        "outside": "OUTSIDE municipality", "wind": "Wind", "rh": "RH ",
        "risk_word": "risk", "map": "Map", "sample": "Sample",
        "test": "FIREWATCH TEST - no fire, checking Telegram delivery",
    },
}

# The map translates these client-side; this alert has to do it here too -
# a copy of sms.COMPASS_BS for the same "no shared import" reason above.
COMPASS_BS = {"N": "S", "NNE": "SSI", "NE": "SI", "ENE": "ISI", "E": "I",
              "ESE": "IJI", "SE": "JI", "SSE": "JJI", "S": "J", "SSW": "JJZ",
              "SW": "JZ", "WSW": "ZJZ", "W": "Z", "WNW": "ZSZ", "NW": "SZ",
              "NNW": "SSZ"}


def _lang() -> tuple[str, dict]:
    # Deliberately its own setting rather than reusing sms_language: a fork could
    # run Telegram in English for a wider audience while SMS stays Bosnian for a
    # local phone list, or the other way round.
    code = str(CFG.get("telegram_language") or "bs").lower()
    return code, TELEGRAM_TEXT.get(code, TELEGRAM_TEXT["en"])


def _dir(d: str, code: str) -> str:
    return COMPASS_BS.get(d, d) if code == "bs" else d


def _place(ev: dict, code: str) -> str:
    """Same composition as sms._place() - see there for the genitive rule."""
    if code != "bs":
        return ev.get("place", "")
    p = ev.get("place_parts") or {}
    name = p.get("name")
    if not name:
        return ev.get("place", "")
    if p.get("km") is None:
        return name
    gen = name[:-1] + "e" if name.endswith("a") else name
    return f"{p['km']} km {_dir(p.get('dir', ''), code)} od {gen}"


def _compose(ev, code, T, kind, sev, peak, latest, place, url) -> str:
    """The message body. No character budget to fit inside - see module docstring
    for why sms._compose()'s degradation ladder has nothing to do here."""
    lines = [
        f"{kind}: {place}",
        f"{sev} {peak}/{latest}MW",
        f"{ev['lat']:.3f},{ev['lon']:.3f}",
    ]
    w = ev.get("weather")
    if w:
        risk = T["risk"].get(ev.get("risk"), ev.get("risk"))
        lines.append(f"{T['wind']} {w.get('speed', 0):.0f}km/h "
                     f"{_dir(w.get('from', '?'), code)} {T['rh']}{w.get('humidity','?')}%"
                     + (f" {T['risk_word']} {risk}" if ev.get("risk") else ""))
    if url:
        lines.append(url)
    return "\n".join(lines)


def alert_text(alert: dict) -> str:
    """What changed, where, how hot, the conditions, and the map."""
    ev = alert["event"]
    code, T = _lang()
    peak = f"{ev['max_frp']:.1f}" if ev.get("max_frp") is not None else "?"
    latest = f"{ev['latest_frp']:.1f}" if ev.get("latest_frp") is not None else "?"
    kind = T["kind"].get(alert["kind"], f"FIRE {alert['kind'].upper()}")
    sev = T["sev"].get(ev["severity"], ev["severity"].upper())
    url = map_url() or ""
    return _compose(ev, code, T, kind, sev, peak, latest, _place(ev, code), url)


def test_text() -> str:
    """A test message that cannot be mistaken for a real alert. See sms.test_text()."""
    code, T = _lang()
    lines = [T["test"]]
    evs = _latest_events()
    if evs:
        ev = evs[0]
        peak = f"{ev['max_frp']:.1f}" if ev.get("max_frp") is not None else "?"
        latest = f"{ev['latest_frp']:.1f}" if ev.get("latest_frp") is not None else "?"
        sev = T["sev"].get(ev.get("severity"), str(ev.get("severity", "?")).upper())
        lines += [
            f"{T['sample']}: {_place(ev, code)}",
            f"{sev} {peak}/{latest}MW",
            f"{ev['lat']:.3f},{ev['lon']:.3f}",
        ]
    url = map_url()
    if url:
        lines.append(url)
    return "\n".join(lines)


def _latest_events() -> list[dict]:
    """Events from the published snapshot, newest first. Empty on any problem."""
    try:
        from .config import SNAPSHOT_PATH
        return json.loads(SNAPSHOT_PATH.read_text()).get("events") or []
    except Exception:
        return []


# ------------------------------------------------------------------------- send

def send(text: str, to: list[str] | None = None) -> bool:
    """Send to every configured recipient.

    One request per chat id - the Bot API has no fan-out endpoint, unlike
    httpSMS's bulk-send. `ready()` already confirmed there is at least one
    recipient; a request failing for one does not stop the others, so a
    partial delivery still returns True if at least one got through.
    """
    ok, why = ready()
    if not ok:
        log.warning("telegram not sent: %s", why)
        return False
    chats = to if to is not None else recipients()
    if not chats:
        return False
    url = API_BASE.format(token=bot_token())
    delivered = 0
    for chat_id in chats:
        try:
            r = requests.post(url, json={"chat_id": chat_id, "text": text},
                              timeout=45, headers={"User-Agent": CFG["user_agent"]})
        except requests.RequestException as exc:
            log.warning("telegram request failed for %s: %s", chat_id, exc)
            continue
        if r.status_code != 200:
            log.warning("telegram rejected for %s: HTTP %s %s",
                        chat_id, r.status_code, r.text[:200])
            continue
        delivered += 1
    log.debug("telegram delivered to %d/%d recipient(s)", delivered, len(chats))
    return delivered > 0


def send_alert(alert: dict) -> bool:
    kinds = CFG.get("telegram_kinds") or []
    if kinds and alert["kind"] not in kinds:
        return False
    text = alert_text(alert)
    if send(text):
        log.info("telegram sent to %d recipient(s) for %s",
                 len(recipients()), alert["kind"])
        return True
    return False
