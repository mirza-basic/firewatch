"""Telegram alerts, broadcast to one public channel (https://core.telegram.org/bots/api).

A single public channel (@firewatchzavidovici) rather than a managed list of
individual chat ids: anyone subscribes from the channel's public link, with no
per-person onboarding and no `getUpdates` lookup to find their chat id - the
cost a chat-id-per-recipient design would carry. A bot posts to a channel the
same way it posts to a private chat, as long as it has been added as an
administrator of it:

    POST https://api.telegram.org/bot<token>/sendMessage
    {"chat_id": "@firewatchzavidovici", "text": "…"}

There is exactly one recipient (the channel itself), so unlike sms.py there is
no fan-out and no bulk endpoint to reach for.

Telegram is UTF-8 end to end and the limit is 4096 characters per message, so
none of sms.py's reasons to exist apply here:

* No GSM-7/UCS-2 split - Bosnian diacritics cost nothing, so alerts keep
  "Zavidovići" rather than folding to "Zavidovici".
* No one-segment budget to fit inside - everything SMS had to drop for cost
  (the detection count and source list, distance from town, the IZVAN OPĆINE
  marker) fits here with room left over, so nothing is trimmed and there is no
  `worst_case()` degradation ladder to build or measure.

`TELEGRAM_TEXT` is `sms.SMS_TEXT` copied rather than imported, on purpose: this
project does not abstract two things that happen to look alike today into one
that would have to be prised back apart the day they diverge - see sms.py's own
note on this, which applies here for the same reason. `_place`/`_dir`/
`COMPASS_BS` are copied for the same reason rather than imported as sms.py
internals.

A channel enforces its own rate limit - roughly one message per second - and
ignoring a 429's `retry_after` risks the bot being muted from the channel for
longer than the delay it originally asked for. `send()` honours it once, up to
a small cap, rather than retrying forever or blocking the poll cycle on it.
"""
from __future__ import annotations

import json
import logging
import os
import time

import requests

from .config import CFG, keychain_secret

log = logging.getLogger("firewatch.telegram")

API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
KEYCHAIN_SERVICE = "firewatch-telegram"
# Channel handle from the environment, for deployments with no writable config file
# (containers, CI runners - including this repo's own GitHub Actions deployment,
# which writes a fresh config.json on every run) - same reasoning as
# sms.SMS_TO_ENV, even though a channel handle is not itself sensitive: it keeps
# every Telegram setting a fork needs in one place (repo secrets), rather than
# splitting the credential into secrets and the handle into a workflow file edit.
TELEGRAM_CHANNEL_ENV = "FIREWATCH_TELEGRAM_CHANNEL"
# A wait longer than this drops the alert instead of blocking the poll cycle on
# it - the fast MTG path this architecture leans on cannot afford to stall for
# however long Telegram asks.
MAX_RETRY_WAIT = 5.0


# ------------------------------------------------------------------ credentials

def bot_token() -> str | None:
    """From the environment, else the macOS Keychain. Never from config.json."""
    env = os.environ.get("TELEGRAM_BOT_TOKEN")
    if env:
        return env.strip()
    return keychain_secret(KEYCHAIN_SERVICE)


def channel() -> str:
    """The @channel or numeric chat id alerts are posted to, or "" unset.

    `FIREWATCH_TELEGRAM_CHANNEL` wins when set, then `telegram_channel` in
    config.json - same order as `sms.recipients()` and `SMS_TO_ENV`, and for
    the same reason: a container or CI runner has no writable config file to
    edit. There is deliberately no built-in default. A channel handle is not
    sensitive the way a phone number is, so it could ship as a plain constant,
    but a real one would mean a fork that sets its own TELEGRAM_BOT_TOKEN and
    forgets this posts, with its own bot, at *this* deployment's channel -
    `ready()` would report True and Telegram would answer 403 rather than the
    cleaner "not configured" every other missing setting gets.
    """
    env = (os.environ.get(TELEGRAM_CHANNEL_ENV) or "").strip()
    if env:
        return env
    return str(CFG.get("telegram_channel") or "").strip()


def channel_source() -> str:
    """Where the channel handle is coming from, so status output can say."""
    return ("environment" if (os.environ.get(TELEGRAM_CHANNEL_ENV) or "").strip()
            else "config.json")


def ready() -> tuple[bool, str]:
    """(usable, reason) - mirrors sms.ready()."""
    if not CFG.get("telegram_enabled"):
        return False, "telegram_enabled is false"
    if not bot_token():
        return False, "no bot token (TELEGRAM_BOT_TOKEN or Keychain)"
    if not channel():
        return False, f"no channel ({TELEGRAM_CHANNEL_ENV} or telegram_channel in config.json)"
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


# Alert wording, per language. Kept with diacritics - Telegram is UTF-8, so
# nothing here needs the ASCII fold sms.py applies on the way out.
TELEGRAM_TEXT = {
    "bs": {
        "kind": {"new": "NOVI POŽAR", "reignited": "PONOVO GORI",
                 "intensified": "POJAČAVA SE", "grew": "ŠIRI SE",
                 "extinguished": "UGAŠEN", "corroborated": "POTVRĐEN"},
        "sev": {"low": "NIZAK", "moderate": "UMJEREN", "high": "VISOK",
                "severe": "EKSTREMAN", "unknown": "NEPOZNAT"},
        "risk": {"elevated": "povišen", "high": "visok", "extreme": "ekstreman",
                 "moderate": "umjeren", "unknown": "nepoznat"},
        "peak": "maks", "now": "sada", "det": "detekcija",
        "outside": "IZVAN OPĆINE", "wind": "Vjetar", "rh": "vlaga ",
        "risk_word": "rizik", "sample": "Primjer",
        "test": "FIREWATCH TEST - nema požara, provjera Telegram kanala",
    },
    "en": {
        "kind": {"new": "NEW FIRE", "reignited": "BURNING AGAIN",
                 "intensified": "INTENSIFYING", "grew": "SPREADING",
                 "extinguished": "OUT", "corroborated": "CONFIRMED"},
        "sev": {"low": "LOW", "moderate": "MODERATE", "high": "HIGH",
                "severe": "SEVERE", "unknown": "UNKNOWN"},
        "risk": {"elevated": "elevated", "high": "high", "extreme": "extreme",
                 "moderate": "moderate", "unknown": "unknown"},
        "peak": "peak", "now": "now", "det": "detections",
        "outside": "OUTSIDE municipality", "wind": "Wind", "rh": "RH ",
        "risk_word": "risk", "sample": "Sample",
        "test": "FIREWATCH TEST - no fire, checking the Telegram channel",
    },
}

# The map translates these client-side; the channel post has to do it here too.
COMPASS_BS = {"N": "S", "NNE": "SSI", "NE": "SI", "ENE": "ISI", "E": "I",
              "ESE": "IJI", "SE": "JI", "SSE": "JJI", "S": "J", "SSW": "JJZ",
              "SW": "JZ", "WSW": "ZJZ", "W": "Z", "WNW": "ZSZ", "NW": "SZ",
              "NNW": "SSZ"}

# Kinds that read as good news get a check rather than a flame.
KIND_EMOJI = {"extinguished": "✅"}
DEFAULT_EMOJI = "\U0001F525"


def _lang() -> tuple[str, dict]:
    code = str(CFG.get("telegram_language") or "bs").lower()
    return code, TELEGRAM_TEXT.get(code, TELEGRAM_TEXT["en"])


def _dir(d: str, code: str) -> str:
    return COMPASS_BS.get(d, d) if code == "bs" else d


def _place(ev: dict, code: str) -> str:
    """"7.5 km IJI od Kamenice" rather than the stored English phrase.

    `place` is built server-side in English, so Bosnian has to be composed from
    `place_parts`. Names ending in -a take the genitive -e after "od", which
    covers most settlements here (Kamenica -> Kamenice); anything else is left
    alone rather than guessed at, exactly as the map and sms.py do it.
    """
    if code != "bs":
        return ev.get("place", "")
    p = ev.get("place_parts") or {}
    name = p.get("name")
    if not name:
        return ev.get("place", "")
    if p.get("km") is None:
        return name                       # sitting on the settlement itself
    gen = name[:-1] + "e" if name.endswith("a") else name
    return f"{p['km']} km {_dir(p.get('dir', ''), code)} od {gen}"


def _compose(ev, code, T, kind, sev, peak, latest, place, url, emoji) -> str:
    """The message body. Nothing here degrades - see the module docstring."""
    lines = [
        f"{emoji} {kind}: {place}",
        f"{sev} · {peak} MW {T['peak']} / {latest} MW {T['now']}",
    ]
    src = f"{ev.get('n_det', 0)} {T['det']} ({'+'.join(sorted(ev.get('sources') or []))})"
    if ev.get("extent_km") is not None:
        src += f" · {ev['extent_km']:.1f} km"
    if not ev.get("inside", True):
        src += f" · {T['outside']}"
    lines.append(src)
    lines.append(f"{ev['lat']:.3f},{ev['lon']:.3f}")
    w = ev.get("weather")
    if w:
        risk = T["risk"].get(ev.get("risk"), ev.get("risk"))
        lines.append(f"{T['wind']} {w.get('speed', 0):.0f}km/h "
                     f"{_dir(w.get('from', '?'), code)} {T['rh']}{w.get('humidity','?')}%"
                     + (f" · {T['risk_word']} {risk}" if ev.get("risk") else ""))
    if url:
        lines.append(url)
    return "\n".join(lines)


def alert_text(alert: dict) -> str:
    """What changed, where, how hot, the conditions, and the map."""
    ev = alert["event"]
    code, T = _lang()
    peak = f"{ev['max_frp']:.1f}" if ev.get("max_frp") is not None else "?"
    latest = f"{ev['latest_frp']:.1f}" if ev.get("latest_frp") is not None else "?"
    kind_key = alert["kind"]
    kind = T["kind"].get(kind_key, f"FIRE {kind_key.upper()}")
    sev = T["sev"].get(ev["severity"], ev["severity"].upper())
    emoji = KIND_EMOJI.get(kind_key, DEFAULT_EMOJI)
    return _compose(ev, code, T, kind, sev, peak, latest, _place(ev, code),
                     map_url() or "", emoji)


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

def _post(url: str, body: dict) -> requests.Response | None:
    try:
        return requests.post(url, json=body,
                             timeout=(CFG["connect_timeout"], CFG["http_timeout"]),
                             headers={"User-Agent": CFG["user_agent"]})
    except requests.RequestException as exc:
        log.warning("telegram request failed: %s", exc)
        return None


def _retry_after(r: requests.Response) -> float | None:
    try:
        v = r.json().get("parameters", {}).get("retry_after")
        return float(v) if v is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def send(text: str) -> bool:
    """Post to the configured channel.

    One request, one recipient - see the module docstring for why there is no
    fan-out here. A 429 is retried once, after waiting the amount of time
    Telegram itself asks for, capped at MAX_RETRY_WAIT: retrying past that would
    block the poll cycle for longer than a channel post is worth, and ignoring
    `retry_after` risks a longer, unannounced mute instead of a graceful wait.
    """
    ok, why = ready()
    if not ok:
        log.warning("telegram not sent: %s", why)
        return False
    url = API_BASE.format(token=bot_token())
    body = {"chat_id": channel(), "text": text}
    r = _post(url, body)
    if r is None:
        return False
    if r.status_code == 429:
        wait = _retry_after(r)
        if wait is None or wait > MAX_RETRY_WAIT:
            log.warning("telegram rate-limited (retry_after=%s) - dropping this "
                        "post rather than blocking the cycle", wait)
            return False
        log.warning("telegram rate-limited, waiting %.1fs and retrying once", wait)
        time.sleep(wait)
        r = _post(url, body)
        if r is None:
            return False
    if r.status_code != 200:
        log.warning("telegram rejected: HTTP %s %s", r.status_code, r.text[:200])
        return False
    log.debug("telegram posted to %s", channel())
    return True


def send_alert(alert: dict) -> bool:
    kinds = CFG.get("telegram_kinds") or []
    if kinds and alert["kind"] not in kinds:
        return False
    text = alert_text(alert)
    if send(text):
        log.info("telegram posted to %s for %s", channel(), alert["kind"])
        return True
    return False
