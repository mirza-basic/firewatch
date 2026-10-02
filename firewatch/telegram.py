"""Telegram alerts, one channel per municipality (https://core.telegram.org/bots/api).

145 private channels, one per municipality (private because of Telegram's cap
on public channels per account; readers join by invite link). Whichever
municipality (or two, for a fire inside Sarajevo or Istocno Sarajevo) an
event's location matched decides who gets posted to - see
events.build_events()'s "municipalities" field and channel_for(). A bot posts
to a channel the same way it posts to a private chat, as long as it has been
added as an administrator of it:

    POST https://api.telegram.org/bot<token>/sendMessage
    {"chat_id": "-1004423431890", "text": "…"}

send_alert() fans out to every matched municipality independently - unlike
sms.py's single recipient list, this is a real per-target loop, and one
channel failing or being unconfigured never blocks another.

Telegram is UTF-8 end to end with a 4096-character limit per message, so
sms.py's constraints do not apply here:

* No GSM-7/UCS-2 split - Bosnian diacritics cost nothing, so alerts keep
  "Zavidovići" rather than folding to "Zavidovici".
* No one-segment budget - the detection count and source list and the IZVAN BIH
  marker that SMS drops all fit, so nothing is trimmed and there is no
  `worst_case()` degradation ladder.

`TELEGRAM_TEXT` is `sms.SMS_TEXT` copied rather than imported, on purpose: two
things that look alike today are not abstracted into one that would have to be
prised apart when the wording diverges. `_place`/`_dir`/`COMPASS_BS` are copied
for the same reason.

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
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import requests

from .config import CFG, force_ipv4, keychain_secret

log = logging.getLogger("firewatch.telegram")

API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
KEYCHAIN_SERVICE = "firewatch-telegram"
# A wait longer than this drops the alert instead of blocking the poll cycle on
# it - the fast MTG path this architecture leans on cannot afford to stall for
# however long Telegram asks.
MAX_RETRY_WAIT = 5.0

# data/bih/municipalities.json maps municipality id -> Bot-API-postable chat id
# (e.g. "-1004423431890"), populated as private channels are provisioned. A
# private channel has no @handle; the Bot API needs the numeric chat id, which is
# the channel's own id with a "-100" prefix. Telethon (used to create the
# channels, since the Bot API cannot) reports the bare id, so the prefix is added
# when provisioning results are merged into this file, not here.
BIH_MUNI_FILE = Path(__file__).resolve().parent.parent / "data" / "bih" / "municipalities.json"


@lru_cache(maxsize=1)
def _municipality_channels() -> dict[str, str]:
    """municipality id -> chat id for every municipality that has a channel (cached)."""
    try:
        rows = json.loads(BIH_MUNI_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {r["id"]: str(r["telegram_channel"]) for r in rows if r.get("telegram_channel")}


def channel_for(municipality_id: str) -> str | None:
    """The chat id to post to for one municipality, or None if it has no channel
    yet, so a partially provisioned deployment alerts on what it has."""
    return _municipality_channels().get(municipality_id)


# ------------------------------------------------------------------ credentials

def bot_token() -> str | None:
    """From the environment, else the macOS Keychain. Never from config.json."""
    env = os.environ.get("TELEGRAM_BOT_TOKEN")
    if env:
        return env.strip()
    return keychain_secret(KEYCHAIN_SERVICE)


def ready() -> tuple[bool, str]:
    """(usable, reason). Readiness does not depend on any particular municipality
    having a channel; channel_for() answers that per alert."""
    if not CFG.get("telegram_enabled"):
        return False, "telegram_enabled is false"
    if not bot_token():
        return False, "no bot token (TELEGRAM_BOT_TOKEN or Keychain)"
    if not _municipality_channels():
        return False, f"no channels configured in {BIH_MUNI_FILE}"
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
        "outside": "IZVAN BIH", "wind": "Vjetar", "rh": "vlaga ",
        "risk_word": "rizik", "sample": "Primjer",
        "test": "FIREWATCH TEST - nema požara, provjera Telegram kanala",
        "open_map": "\U0001F525 Otvori na mapi",
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
        "outside": "OUTSIDE Bosnia and Herzegovina", "wind": "Wind", "rh": "RH ",
        "risk_word": "risk", "sample": "Sample",
        "test": "FIREWATCH TEST - no fire, checking the Telegram channel",
        "open_map": "\U0001F525 Open on map",
    },
}

# The map translates these client-side; the channel post has to do it here too.
COMPASS_BS = {"N": "S", "NNE": "SSI", "NE": "SI", "ENE": "ISI", "E": "I",
              "ESE": "IJI", "SE": "JI", "SSE": "JJI", "S": "J", "SSW": "JJZ",
              "SW": "JZ", "WSW": "ZJZ", "W": "Z", "WNW": "ZSZ", "NW": "SZ",
              "NNW": "SSZ"}

# Bot API 9.4 buttons take only preset colors ("primary" blue, "success" green, "danger"
# red) - there is no orange. Red is the nearest to fire; see send() for the fallback.
BUTTON_STYLE = "danger"

# Kinds that read as good news get a check rather than a flame.
KIND_EMOJI = {"extinguished": "✅"}
DEFAULT_EMOJI = "\U0001F525"


def _lang() -> tuple[str, dict]:
    """(language code, wording table) for telegram_language, falling back to English."""
    code = str(CFG.get("telegram_language") or "bs").lower()
    return code, TELEGRAM_TEXT.get(code, TELEGRAM_TEXT["en"])


def _dir(d: str, code: str) -> str:
    """Compass bearing in the alert language."""
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


def _compose(ev, code, T, kind, sev, peak, latest, place, emoji) -> str:
    """The message body. Nothing here degrades - see the module docstring. The map is
    not a line of it: it is an inline button under the post (see `send()`)."""
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
    return "\n".join(lines)


def merged_alert_text(alerts: list[dict]) -> str:
    """One message for every kind that fired for the same event in the same cycle.

    Reignited, corroborated, intensified and grew can all be true of one event at
    once (see events.diff()), and separate posts about the same fire read as spam.
    The body is one event snapshot whichever kinds triggered it; merging only
    changes the leading label line.
    """
    ev = alerts[0]["event"]
    code, T = _lang()
    peak = f"{ev['max_frp']:.1f}" if ev.get("max_frp") is not None else "?"
    latest = f"{ev['latest_frp']:.1f}" if ev.get("latest_frp") is not None else "?"
    kind_keys = [a["kind"] for a in alerts]
    kind = " + ".join(T["kind"].get(k, f"FIRE {k.upper()}") for k in kind_keys)
    sev = T["sev"].get(ev["severity"], ev["severity"].upper())
    # The check mark only when the whole message is that kind alone; any other
    # kind alongside it keeps the fire emoji.
    emoji = KIND_EMOJI.get(kind_keys[0], DEFAULT_EMOJI) if len(kind_keys) == 1 else DEFAULT_EMOJI
    return _compose(ev, code, T, kind, sev, peak, latest, _place(ev, code),
                    emoji)


def deep_link(url: str | None, ev: dict, with_event: bool = False) -> str:
    """The map address with this fire's position in the query string, so opening it
    from Telegram lands already zoomed on the fire instead of at country scale.

    GitHub Pages ignores query strings, so this costs nothing server-side - the page's
    own script reads them (see `deepLink()` in mapgen). `with_event` adds `e=<id>`,
    which makes the map also open that event's panel; alerts leave it off.
    The slash before `?` is explicit: `/firewatch?x` makes Pages redirect to
    `/firewatch/` and is one more hop for nothing. SMS deliberately does not use any of
    this - its 160-character budget has two characters of headroom.
    """
    if not url:
        return ""
    q = f"lat={ev['lat']:.4f}&lon={ev['lon']:.4f}&z=14"
    if with_event and ev.get("id"):
        q += f"&e={quote(str(ev['id']), safe='')}"
    return f"{url.rstrip('/')}/?{q}"


def button_url(alerts: list[dict]) -> str | None:
    """Where the post's "Open on map" button points: the deep link for the alert's
    event, or None when there is no public map address to link to."""
    return deep_link(map_url(), alerts[0]["event"]) or None


def alert_text(alert: dict) -> str:
    """A single alert as one message; see merged_alert_text()."""
    return merged_alert_text([alert])


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
    return "\n".join(lines)


def test_button_url() -> str | None:
    """The button the test post carries - same shape as a real alert's, aimed at the
    newest event in the snapshot (or the bare map when there is none)."""
    evs = _latest_events()
    return (deep_link(map_url(), evs[0]) if evs else map_url()) or None


def _latest_events() -> list[dict]:
    """Events from the published snapshot (else the database), newest first. Empty on any
    problem."""
    try:
        from .config import SNAPSHOT_PATH
        evs = json.loads(SNAPSHOT_PATH.read_text()).get("events") or []
        if evs:
            return evs
    except Exception:
        pass
    # No snapshot (a GitHub runner only has the committed database): read the
    # newest events straight from it.
    try:
        from . import store
        con = store.connect()
        try:
            evs = list(store.load_events(con).values())
        finally:
            con.close()
        return sorted(evs, key=lambda e: e.get("last_ts") or "", reverse=True)
    except Exception:
        return []


# ------------------------------------------------------------------------- send

def _post(url: str, body: dict) -> requests.Response | None:
    """POST a JSON body; None on a transport error."""
    # api.telegram.org publishes an AAAA record, and GitHub Actions runners have
    # no IPv6 route - see config.force_ipv4(). A no-op unless FIREWATCH_FORCE_IPV4
    # is set, and cheap enough to call on every post rather than once at startup.
    force_ipv4()
    try:
        return requests.post(url, json=body,
                             timeout=(CFG["connect_timeout"], CFG["http_timeout"]),
                             headers={"User-Agent": CFG["user_agent"]})
    except requests.RequestException as exc:
        log.warning("telegram request failed: %s", exc)
        return None


def _retry_after(r: requests.Response) -> float | None:
    """Seconds Telegram asks us to wait in a 429 body, or None."""
    try:
        v = r.json().get("parameters", {}).get("retry_after")
        return float(v) if v is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def send(text: str, chat_id: str, button_url: str | None = None) -> bool:
    """Post to one chat id (one municipality's channel).

    One request per recipient; there is no bulk endpoint. A 429 is retried once,
    after the wait Telegram asks for, capped at MAX_RETRY_WAIT: retrying past that
    would block the poll cycle, and ignoring `retry_after` risks a longer mute.

    `button_url` becomes an inline "Open on map" button under the post. Telegram
    refuses some addresses (localhost, anything it cannot parse) with
    BUTTON_URL_INVALID; the post is then re-sent without the button, because an alert
    that arrives without its map link beats one that does not arrive.
    """
    if not CFG.get("telegram_enabled") or not bot_token():
        log.warning("telegram not sent to %s: bot not configured", chat_id)
        return False
    url = API_BASE.format(token=bot_token())
    body = {"chat_id": chat_id, "text": text}
    if button_url:
        body["reply_markup"] = {"inline_keyboard": [[
            {"text": _lang()[1]["open_map"], "url": button_url, "style": BUTTON_STYLE}]]}
    r = _post(url, body)
    if r is None:
        return False
    # Two ways a button can be refused, each with its own smaller step down: a bad
    # address loses the button, a rejected `style` (older Bot API) loses only the color.
    for _ in range(2):
        if r.status_code != 400 or "reply_markup" not in body:
            break
        if "BUTTON_URL_INVALID" in r.text:
            log.warning("telegram refused the map button (%s) - sending without it",
                        button_url)
            body.pop("reply_markup")
        elif "style" in body["reply_markup"]["inline_keyboard"][0][0]:
            log.warning("telegram refused the button style (%s) - sending uncolored",
                        r.text[:120])
            body["reply_markup"]["inline_keyboard"][0][0].pop("style")
        else:
            break
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
        log.warning("telegram rejected (%s): HTTP %s %s",
                    chat_id, r.status_code, r.text[:200])
        return False
    log.debug("telegram posted to %s", chat_id)
    return True


def send_alert_group(alerts: list[dict]) -> bool:
    """Post one merged message per matched municipality's channel.

    Kinds are filtered by telegram_kinds before merging (see merged_alert_text()).
    Each channel is independent: a missing channel or failed post never blocks
    another. Returns True if any post succeeded.
    """
    kinds = CFG.get("telegram_kinds") or []
    included = [a for a in alerts if not kinds or a["kind"] in kinds]
    if not included:
        return False
    municipality_ids = included[0]["event"].get("municipalities") or []
    if not municipality_ids:
        return False
    text = merged_alert_text(included)
    link = button_url(included)
    kinds_label = "+".join(a["kind"] for a in included)
    any_sent = False
    for mid in municipality_ids:
        chat_id = channel_for(mid)
        if not chat_id:
            log.info("telegram: no channel configured for municipality %s", mid)
            continue
        if send(text, chat_id, button_url=link):
            log.info("telegram posted to %s (%s) for %s", mid, chat_id, kinds_label)
            any_sent = True
    return any_sent


def send_alert(alert: dict) -> bool:
    """Post a single alert to every municipality the event matched (two for a fire
    inside Sarajevo or Istocno Sarajevo); see send_alert_group()."""
    return send_alert_group([alert])
