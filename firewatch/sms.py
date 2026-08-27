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
    try:
        from . import expose
        t = expose.find_tunnel()
        return (t or {}).get("public_url")
    except Exception:
        return None


def alert_text(alert: dict) -> str:
    """Compact but complete: what, where, how bad, conditions, link."""
    ev = alert["event"]
    peak = f"{ev['max_frp']:.1f}" if ev.get("max_frp") is not None else "?"
    latest = f"{ev['latest_frp']:.1f}" if ev.get("latest_frp") is not None else "?"
    lines = [
        f"FIRE {alert['kind'].upper()}: {ev.get('place','')}",
        f"{ev['severity'].upper()} {peak}MW peak/{latest} now",
        f"{ev['n_det']} det ({','.join(ev.get('sources', []))}) "
        f"{ev['dist_town_km']}km {ev['dir_town']} of town",
        f"{ev['lat']:.4f},{ev['lon']:.4f}",
    ]
    if not ev.get("inside"):
        lines.append("OUTSIDE municipality")
    w = ev.get("weather")
    if w:
        lines.append(f"Wind {w.get('speed', 0):.0f}km/h {w.get('from','?')}"
                     f" RH{w.get('humidity','?')}%"
                     + (f" {ev['risk']} risk" if ev.get("risk") else ""))
    url = map_url()
    if url:
        lines.append(f"Map: {url}")
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
    lines = ["FIREWATCH TEST - no fire, checking SMS delivery"]
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
