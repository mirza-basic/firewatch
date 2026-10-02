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

from .config import CFG, force_ipv4, keychain_secret

log = logging.getLogger("firewatch.sms")

API_URL = "https://api.httpsms.com/v1/messages/send"
BULK_URL = "https://api.httpsms.com/v1/messages/bulk-send"
KEYCHAIN_SERVICE = "firewatch-httpsms"
# Recipients from the environment, for deployments with no writable config file
# (containers, CI runners) and for public repositories, where a phone number in
# a committed file would be published.
SMS_TO_ENV = "FIREWATCH_SMS_TO"

# Municipality filter from the environment. Same reasoning as SMS_TO_ENV: which
# municipality's fires reach one person's phone says roughly where they live, so a
# public repository's committed config.json is the wrong place for it.
SMS_MUNICIPALITIES_ENV = "FIREWATCH_SMS_MUNICIPALITIES"

# Characters that would force UCS-2 encoding, and their GSM-7 equivalents.
TRANSLIT = {
    "ć": "c", "Ć": "C", "č": "c", "Č": "C", "ž": "z", "Ž": "Z",
    "š": "s", "Š": "S", "đ": "dj", "Đ": "Dj", "ǆ": "dz",
    "→": "->", "·": "-", "—": "-", "–": "-", "°": "deg", "∝": "~",
    # Typographic quotes arrive via OSM place names (e.g. 'Ekološko izletište
    # „Ontario”'); unmapped they become "?", which reads like corruption.
    "„": '"', "”": '"', "“": '"', "‚": "'", "’": "'", "‘": "'", "…": "...",
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
    this way: they are tiny, and only consulted when an alert is going out.
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

    Accepts a list, a single string, or a comma- or semicolon-separated string.
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


def sms_municipalities_source() -> str:
    """Where the municipality filter is coming from, so status output can say."""
    return "environment" if (os.environ.get(SMS_MUNICIPALITIES_ENV) or "").strip() else "config.json"


def sms_municipalities() -> list[str]:
    """Which municipality ids' fires reach the SMS recipient list; empty means
    unrestricted. Same env-wins-over-config.json precedence as recipients()."""
    env = (os.environ.get(SMS_MUNICIPALITIES_ENV) or "").strip()
    if env:
        raw = env
    else:
        raw = _live("sms_municipalities")
        if raw is None:
            raw = CFG.get("sms_municipalities") or []
    if isinstance(raw, str):
        raw = raw.replace(";", ",").split(",")
    return [m.strip() for m in raw if m and m.strip()]


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
        "outside": "IZVAN BIH", "wind": "Vjetar", "rh": "vlaga ",
        "risk_word": "rizik", "map": "Karta", "sample": "Primjer",
        "test": "FIREWATCH TEST - nema požara, provjera dostave SMS-a",
    },
    "en": {
        # Written out rather than falling back to raw keys, which would read
        # "FIRE INTENSIFIED". Short and upper-case like the Bosnian set: the first
        # line is what gets read on a lock screen, and every character competes
        # with the coordinates.
        "kind": {"new": "NEW FIRE", "reignited": "BURNING AGAIN",
                 "intensified": "INTENSIFYING", "grew": "SPREADING",
                 "extinguished": "OUT", "corroborated": "CONFIRMED"},
        "sev": {"low": "LOW", "moderate": "MODERATE", "high": "HIGH",
                "severe": "SEVERE", "unknown": "UNKNOWN"},
        "risk": {"elevated": "elevated", "high": "high", "extreme": "extreme",
                 "moderate": "moderate", "unknown": "unknown"},
        "peak": "peak", "now": "now", "det": "det", "of_town": "of town",
        "outside": "OUTSIDE Bosnia and Herzegovina", "wind": "Wind", "rh": "RH ",
        "risk_word": "risk", "map": "Map", "sample": "Sample",
        "test": "FIREWATCH TEST - no fire, checking SMS delivery",
    },
}

# The map translates these client-side; SMS has to do it here.
COMPASS_BS = {"N": "S", "NNE": "SSI", "NE": "SI", "ENE": "ISI", "E": "I",
              "ESE": "IJI", "SE": "JI", "SSE": "JJI", "S": "J", "SSW": "JJZ",
              "SW": "JZ", "WSW": "ZJZ", "W": "Z", "WNW": "ZSZ", "NW": "SZ",
              "NNW": "SSZ"}


def _lang() -> tuple[str, dict]:
    """(language code, wording table) for sms_language, falling back to English."""
    code = str(CFG.get("sms_language") or "bs").lower()
    return code, SMS_TEXT.get(code, SMS_TEXT["en"])


def _dir(d: str, code: str) -> str:
    """Compass bearing in the alert language."""
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


# One GSM-7 segment. Past this a message costs two, and every alert carries weather
# because an active fire is always enriched.
ONE_SEGMENT = 160


def _compose(ev, code, T, kind, sev, peak, latest, place, url) -> str:
    """The message body, for a given rendering of the place phrase."""
    lines = [
        f"{kind}: {place}",
        # "10.8/1.2MW" rather than "10.8MW maks/1.2 sada": peak first, current
        # second. Ten characters of labelling is the difference between one segment
        # and two once the map link is present.
        f"{sev} {peak}/{latest}MW",
        # Three decimals is ~110 m, finer than any of these sensors resolves.
        f"{ev['lat']:.3f},{ev['lon']:.3f}",
    ]
    w = ev.get("weather")
    if w:
        risk = T["risk"].get(ev.get("risk"), ev.get("risk"))
        lines.append(f"{T['wind']} {w.get('speed', 0):.0f}km/h "
                     f"{_dir(w.get('from', '?'), code)} {T['rh']}{w.get('humidity','?')}%"
                     + (f" {T['risk_word']} {risk}" if ev.get("risk") else ""))
    if url:
        lines.append(url)                 # bare: a label costs 7 characters
    return ascii_only("\n".join(lines))


def merged_alert_text(alerts: list[dict]) -> str:
    """What changed, where, how hot, the conditions, and the map - in one segment.

    One message covers every kind that fired for the same event in the same cycle
    (reignited, intensified and grew can all be true at once, see events.diff()),
    rather than one near-identical text per kind.

    Fitting is done by degrading the *place phrase*, the only part with slack:
    first the full "99.9 km SSZ od <name>", then the settlement name alone, then a
    word-boundary trim of the name. The longest measured real alert is 155
    characters; a silent second segment is the kind of thing nobody notices until
    the bill. A merged kind label spends part of the same slack, so it uses the
    same ladder.
    """
    ev = alerts[0]["event"]
    code, T = _lang()
    peak = f"{ev['max_frp']:.1f}" if ev.get("max_frp") is not None else "?"
    latest = f"{ev['latest_frp']:.1f}" if ev.get("latest_frp") is not None else "?"
    kind = " + ".join(T["kind"].get(a["kind"], f"FIRE {a['kind'].upper()}") for a in alerts)
    sev = T["sev"].get(ev["severity"], ev["severity"].upper())
    url = map_url() or ""

    text = _compose(ev, code, T, kind, sev, peak, latest, _place(ev, code), url)
    if len(text) > ONE_SEGMENT:
        # Drop the distance and bearing, keep the settlement.
        name = (ev.get("place_parts") or {}).get("name") or _place(ev, code)
        text = _compose(ev, code, T, kind, sev, peak, latest, ascii_only(name), url)
    if len(text) > ONE_SEGMENT:
        # Last resort: trim the name itself rather than spill into a second segment.
        # Cut at a word boundary - a hard slice leaves things like 'izletiste "Ontari',
        # which reads as corruption rather than as an abbreviation.
        over = len(text) - ONE_SEGMENT
        name = ascii_only((ev.get("place_parts") or {}).get("name") or "")
        cut = name[:max(3, len(name) - over)]
        if " " in cut and not name[len(cut):len(cut) + 1].isspace():
            cut = cut.rsplit(" ", 1)[0]
        text = _compose(ev, code, T, kind, sev, peak, latest,
                        cut.rstrip(' ,-"\'') or name[:3], url)
    return text


def alert_text(alert: dict) -> str:
    """A single alert as one message; see merged_alert_text()."""
    return merged_alert_text([alert])


def worst_case(code: str | None = None) -> dict:
    """The longest alert this deployment can produce, and what it costs.

    "An alert is four lines and one segment" is a measured claim, not a structural
    one, and two things move it: a longer settlement name in the data, and the
    length of the published URL (every character of it is spent before a word of
    the fire is). Headroom is currently two characters. This builds the worst alert
    the data allows and reports it; `sms-status` prints it.

    The event is synthetic on purpose: absurd values (1234.5 MW peak, 99.9 km,
    100% humidity, extreme risk) with the longest real place name, so the answer
    holds for fires that have not happened yet.
    """
    from . import geo
    names = [p["n"] for p in geo.settlements()] or [""]
    longest = max(names, key=lambda n: len(ascii_only(n)))
    code = (code or _lang()[0])
    ev = {
        "lat": -44.4444, "lon": -18.8888,
        "max_frp": 1234.5, "latest_frp": 999.9, "severity": "severe",
        "place": f"99.9 km NNW of {longest}",
        "place_parts": {"name": longest, "km": 99.9, "dir": "NNW"},
        "weather": {"speed": 123.4, "from": "WNW", "humidity": 100},
        "risk": "extreme",
    }
    saved = CFG.get("sms_language")
    CFG["sms_language"] = code
    try:
        texts = {k: alert_text({"event": ev, "kind": k, "detail": ""})
                 for k in CFG["sms_kinds"] or ["new"]}
    finally:
        CFG["sms_language"] = saved
    kind, text = max(texts.items(), key=lambda kv: len(kv[1]))
    return {"language": code, "kind": kind, "text": text, "chars": len(text),
            "segments": segments(text), "headroom": ONE_SEGMENT - len(text),
            "place": longest, "url": map_url() or ""}


def test_text() -> str:
    """A test message that cannot be mistaken for a real alert.

    The body is the newest event's alert, line for line and in the same language,
    so it exercises the wording, the genitive, the bearing translation and the true
    segment cost. Only the leading TEST line differs, and it has to: a test that
    reads like "NOVI POZAR: ..." on someone's phone at 3am is worse than none.

    Headroom is thin: with the longest settlement name in the data and absurd
    numbers the Bosnian version is 156 characters, four short of a second segment.
    """
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
    text = ascii_only("\n".join(lines))
    if len(text) > ONE_SEGMENT and evs:
        # Same degradation as an alert, and it bites sooner here: the TEST line is
        # 52 characters that a real alert does not carry.
        name = (evs[0].get("place_parts") or {}).get("name") or ""
        lines[1] = f"{T['sample']}: {ascii_only(name)}"
        text = ascii_only("\n".join(lines))
    return text


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
    # api.httpsms.com CNAMEs to ghs.googlehosted.com, which publishes an AAAA
    # record - GitHub Actions runners have no IPv6 route. A no-op unless
    # FIREWATCH_FORCE_IPV4 is set; see config.force_ipv4().
    force_ipv4()
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


def send_alert_group(alerts: list[dict]) -> bool:
    """Send one text for all kinds that fired for one event in a cycle.

    Applies the sms_kinds filter and the sms_municipalities filter first; see
    merged_alert_text().
    """
    kinds = CFG.get("sms_kinds") or []
    included = [a for a in alerts if not kinds or a["kind"] in kinds]
    if not included:
        return False
    ev = included[0]["event"]
    # Empty means unrestricted; non-empty means only these municipalities' fires
    # reach the one SMS recipient list. Telegram routes per-municipality on its
    # own and needs no equivalent filter.
    only = sms_municipalities()
    if only and not set(ev.get("municipalities") or []) & set(only):
        return False
    text = merged_alert_text(included)
    if send(text):
        log.info("sms sent to %d recipient(s) (%d chars, %d segment(s)) for %s",
                 len(recipients()), len(text), segments(text),
                 "+".join(a["kind"] for a in included))
        return True
    return False


def send_alert(alert: dict) -> bool:
    """Send a single alert as its own text."""
    return send_alert_group([alert])
