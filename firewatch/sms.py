"""SMS alerts via httpSMS (https://httpsms.com).

httpSMS turns an Android phone into an SMS gateway: the app on the phone sends the
message, and this posts to their API.

    POST https://api.httpsms.com/v1/messages/send
    x-api-key: <key>
    {"from": "+387…", "to": "+387…", "content": "…"}

Several recipients go through /v1/messages/bulk-send instead, where `to` is an
array - one API call for the whole fan-out.

Two things shape the message body:

* A single GSM-7 segment is 160 characters, but one non-GSM character switches the
  whole message to UCS-2 at 70 characters per segment. Bosnian diacritics would do
  exactly that, so text is transliterated to ASCII - "Zavidovici", not
  "Zavidovići". Same information, a third of the segments.
* The map link is resolved when the message is sent, never stored: the free ngrok
  URL changes every time the agent restarts.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess

import requests

from .config import CFG, keychain_secret

log = logging.getLogger("firewatch.sms")

API_URL = "https://api.httpsms.com/v1/messages/send"
BULK_URL = "https://api.httpsms.com/v1/messages/bulk-send"
KEYCHAIN_SERVICE = "firewatch-httpsms"
# Recipients from the environment, for deployments with no writable config file
# (containers, CI runners) and for public repositories, where a phone number in
# a committed file would be published.
SMS_TO_ENV = "FIREWATCH_SMS_TO"

# Characters that would force UCS-2 encoding, and their GSM-7 equivalents.
TRANSLIT = {
    "ć": "c", "Ć": "C", "č": "c", "Č": "C", "ž": "z", "Ž": "Z",
    "š": "s", "Š": "S", "đ": "dj", "Đ": "Dj", "ǆ": "dz",
    "→": "->", "·": "-", "—": "-", "–": "-", "°": "deg", "∝": "~",
    " ": " ", "🔥": "", "✅": "",
}


def ascii_only(text: str) -> str:
    """Fold to GSM-7-safe ASCII so one segment stays 160 chars, not 70."""
    # Đ is the one letter whose folding depends on its neighbours: "POTVRĐEN" wants
    # DJ and "Đurđevik" wants Dj. A flat mapping produces "POTVRDjEN", and the same
    # applies to any place name written in caps.
    text = re.sub(r"Đ(?=[A-ZČĆŽŠĐ])", "DJ", text)
    for k, v in TRANSLIT.items():
        text = text.replace(k, v)
    return text.encode("ascii", "replace").decode("ascii")


def segments(text: str) -> int:
    """How many SMS segments this message will cost."""
    try:
        text.encode("ascii")
        per = 160 if len(text) <= 160 else 153      # concatenated GSM-7
    except UnicodeEncodeError:
        per = 70 if len(text) <= 70 else 67         # concatenated UCS-2
    return max(1, -(-len(text) // per))


# ------------------------------------------------------------------ credentials

def api_key() -> str | None:
    """From the environment, else the macOS Keychain. Never from config.json."""
    env = os.environ.get("HTTPSMS_API_KEY")
    if env:
        return env.strip()
    return keychain_secret(KEYCHAIN_SERVICE)


def sender() -> str:
    """The gateway number alerts are sent *from*.

    Environment first, like the API key: it belongs to the same httpSMS account and
    is fixed for the life of a deployment, so a systemd unit or `docker run -e` is
    the right place for it. Falls back to `sms_from` in config.json.
    """
    env = os.environ.get("HTTPSMS_FROM")
    if env and env.strip():
        return env.strip()
    return str(_live("sms_from") or CFG.get("sms_from") or "").strip()


def _live(key):
    """Read one setting from config.json *now*, not from the import-time snapshot.

    Recipients change while the service is running - that is the whole point of
    `sms-add` - and CFG is loaded once at import, so a long-lived poller would keep
    texting the old list until someone restarted it. Only the SMS settings are read
    this way: they are tiny, and only consulted when an alert is actually going out.
    """
    from .config import CONFIG_FILE
    try:
        return json.loads(CONFIG_FILE.read_text()).get(key)
    except (OSError, ValueError, AttributeError):
        return None                      # missing or half-written: caller falls back


def recipients_source() -> str:
    """Where the recipient list is coming from, so status output can say."""
    return "environment" if (os.environ.get(SMS_TO_ENV) or "").strip() else "config.json"


def recipients() -> list[str]:
    """Where alerts go.

    `FIREWATCH_SMS_TO` wins when set - a container or a CI runner has no config file
    to edit, and phone numbers must not sit in a repository. Otherwise the list is
    read fresh from config.json on every call, so `sms-add` applies to a running
    service without a restart.

    Note the two are different in kind, and the trade is deliberate: the environment
    is fixed for the life of the process, so where it is used the list stops being
    editable at runtime. `sms-add` says so rather than appearing to succeed.

    Accepts a list, a single string, or a comma- or semicolon-separated string, so
    older single-recipient configs keep working.
    """
    env = (os.environ.get(SMS_TO_ENV) or "").strip()
    if env:
        raw = env
    else:
        raw = _live("sms_to")
        if raw is None:
            raw = CFG.get("sms_to") or []
    if isinstance(raw, str):
        raw = raw.replace(";", ",").split(",")
    return [n.strip() for n in raw if n and n.strip()]


def ready() -> tuple[bool, str]:
    """(usable, reason) - so status output can say exactly what is missing."""
    if not CFG.get("sms_enabled"):
        return False, "sms_enabled is false"
    if not api_key():
        return False, "no API key (HTTPSMS_API_KEY or Keychain)"
    if not sender():
        return False, "no sender number (HTTPSMS_FROM or sms_from)"
    if not recipients():
        return False, f"no recipients ({SMS_TO_ENV} or sms_to in config.json)"
    bad = [n for n in recipients() if not n.startswith("+") or not n[1:].isdigit()]
    if bad:
        return False, f"not E.164: {', '.join(bad)}"
    return True, "ready"


# ---------------------------------------------------------------------- content

def map_url() -> str | None:
    """The link put in an alert.

    A configured address wins over the ngrok lookup: on a host the tunnel does not
    exist, and the free ngrok URL changes on every agent restart anyway.
    """
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


# Alert wording, per language. Written with diacritics and folded to ASCII on the
# way out by ascii_only(), so the source stays readable while the message stays on
# GSM-7 - one non-GSM character would cut a segment from 160 characters to 70.
SMS_TEXT = {
    "bs": {
        "kind": {"new": "NOVI POŽAR", "reignited": "PONOVO GORI",
                 "intensified": "POJAČAVA SE", "grew": "ŠIRI SE",
                 "extinguished": "UGAŠEN", "corroborated": "POTVRĐEN"},
        "sev": {"low": "NIZAK", "moderate": "UMJEREN", "high": "VISOK",
                "severe": "EKSTREMAN", "unknown": "NEPOZNAT"},
        "risk": {"elevated": "povišen", "high": "visok", "extreme": "ekstreman",
                 "moderate": "umjeren", "unknown": "nepoznat"},
        "peak": "maks", "now": "sada", "det": "det", "of_town": "od grada",
        "outside": "IZVAN OPĆINE", "wind": "Vjetar", "rh": "vlaga",
        "risk_word": "rizik", "map": "Karta",
        "test": "FIREWATCH TEST - nema požara, provjera dostave SMS-a",
    },
    "en": {
        "kind": {}, "sev": {}, "risk": {},
        "peak": "peak", "now": "now", "det": "det", "of_town": "of town",
        "outside": "OUTSIDE municipality", "wind": "Wind", "rh": "RH",
        "risk_word": "risk", "map": "Map",
        "test": "FIREWATCH TEST - no fire, checking SMS delivery",
    },
}

# The map translates these client-side; SMS has to do it here.
COMPASS_BS = {"N": "S", "NNE": "SSI", "NE": "SI", "ENE": "ISI", "E": "I",
              "ESE": "IJI", "SE": "JI", "SSE": "JJI", "S": "J", "SSW": "JJZ",
              "SW": "JZ", "WSW": "ZJZ", "W": "Z", "WNW": "ZSZ", "NW": "SZ",
              "NNW": "SSZ"}


def _lang() -> tuple[str, dict]:
    code = str(CFG.get("sms_language") or "bs").lower()
    return code, SMS_TEXT.get(code, SMS_TEXT["en"])


def _dir(d: str, code: str) -> str:
    return COMPASS_BS.get(d, d) if code == "bs" else d


def _place(ev: dict, code: str) -> str:
    """"7.5 km IJI od Kamenice" rather than the stored English phrase.

    `place` is built server-side in English, so Bosnian has to be composed from
    `place_parts`. Names ending in -a take the genitive -e after "od", which covers
    most settlements here (Kamenica -> Kamenice); anything else is left alone rather
    than guessed at, exactly as the map does it.
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


def alert_text(alert: dict) -> str:
    """Compact but complete: what, where, how bad, conditions, link."""
    ev = alert["event"]
    code, T = _lang()
    peak = f"{ev['max_frp']:.1f}" if ev.get("max_frp") is not None else "?"
    latest = f"{ev['latest_frp']:.1f}" if ev.get("latest_frp") is not None else "?"
    kind = T["kind"].get(alert["kind"], f"FIRE {alert['kind'].upper()}")
    sev = T["sev"].get(ev["severity"], ev["severity"].upper())
    lines = [
        f"{kind}: {_place(ev, code)}",
        f"{sev} {peak}MW {T['peak']}/{latest} {T['now']}",
        f"{ev['n_det']} {T['det']} ({','.join(ev.get('sources', []))}) "
        f"{ev['dist_town_km']}km {_dir(ev['dir_town'], code)} {T['of_town']}",
        f"{ev['lat']:.4f},{ev['lon']:.4f}",
    ]
    if not ev.get("inside"):
        lines.append(T["outside"])
    w = ev.get("weather")
    if w:
        risk = T["risk"].get(ev.get("risk"), ev.get("risk"))
        # "RH34%" in English, "vlaga 34%" in Bosnian - the label needs a space in
        # one and not the other.
        gap = " " if code == "bs" else ""
        lines.append(f"{T['wind']} {w.get('speed', 0):.0f}km/h "
                     f"{_dir(w.get('from', '?'), code)}"
                     f" {T['rh']}{gap}{w.get('humidity','?')}%"
                     + (f" {T['risk_word']} {risk}" if ev.get("risk") else ""))
    url = map_url()
    if url:
        lines.append(f"{T['map']}: {url}")
    text = ascii_only("\n".join(lines))
    cap = int(CFG.get("sms_max_chars", 320))
    if len(text) > cap:
        text = text[:cap - 1].rstrip() + "…".encode("ascii", "replace").decode()
    return text


def test_text() -> str:
    """A test message that cannot be mistaken for a real alert.

    It is built from the newest event when there is one, so the formatting, the
    transliteration and the segment count are exercised on real data - but it
    leads with TEST and says outright that nothing is burning. A test that reads
    like "FIRE NEW: ..." on a recipient's phone is worse than no test at all,
    and the menu bar puts this one click away from anyone.
    """
    _, T = _lang()
    lines = [T["test"]]
    evs = _latest_events()
    if evs:
        ev = evs[0]
        peak = f"{ev['max_frp']:.1f}" if ev.get("max_frp") is not None else "?"
        lines.append(f"Sample: {ev.get('place', '?')} {peak}MW "
                     f"{ev.get('severity', '?')}")
    url = map_url()
    if url:
        lines.append(f"Map: {url}")
    return ascii_only("\n".join(lines))


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

    One recipient uses /messages/send; several use /messages/bulk-send, which
    takes `to` as an array, so a fan-out is a single API call rather than one per
    number. Each recipient still costs a message against the phone's
    messages_per_minute budget (default 10, max 29).
    """
    ok, why = ready()
    if not ok:
        log.warning("sms not sent: %s", why)
        return False
    nums = to if to is not None else recipients()
    if not nums:
        return False
    bulk = len(nums) > 1
    body = {"from": sender(), "content": text,
            "to": nums if bulk else nums[0]}
    try:
        r = requests.post(BULK_URL if bulk else API_URL, json=body, timeout=45,
                          headers={"x-api-key": api_key(),
                                   "Content-Type": "application/json",
                                   "User-Agent": CFG["user_agent"]})
    except requests.RequestException as exc:
        log.warning("sms request failed: %s", exc)
        return False
    if r.status_code not in (200, 201, 202):
        log.warning("sms rejected (%s): HTTP %s %s",
                    "bulk" if bulk else "single", r.status_code, r.text[:200])
        return False
    log.debug("sms accepted for %d recipient(s)", len(nums))
    return True


def send_alert(alert: dict) -> bool:
    kinds = CFG.get("sms_kinds") or []
    if kinds and alert["kind"] not in kinds:
        return False
    text = alert_text(alert)
    if send(text):
        log.info("sms sent to %d recipient(s) (%d chars, %d segment(s)) for %s",
                 len(recipients()), len(text), segments(text), alert["kind"])
        return True
    return False
