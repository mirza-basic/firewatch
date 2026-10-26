"""Push alerts to the Locator app through Firebase Cloud Messaging (HTTP v1).

Same shape as telegram.py, on purpose: an event's matched municipalities decide who
is told, the same kinds are sent (`push_kinds` defaults to `telegram_kinds`), every
kind that fired for one event in one cycle is merged into one notification, and the
wording is Telegram's (`telegram.merged_alert_text()`) split into a title and a body.

Recipients are FCM *topics*, not devices - the app subscribes itself, so there is no
user table here and nothing identifies a reader:

    fire.<municipality id>   one per municipality, e.g. fire.zavidovici
    fire.all                 the whole country

A fire in two municipalities (Sarajevo / Istočno Sarajevo overlap) publishes to both
municipality topics but to `fire.all` only once, so a country subscriber is not told
twice. One topic failing never blocks another.

Credentials: a Google service account with the "Firebase Cloud Messaging API Admin"
role, as JSON, in FIREWATCH_FCM_SERVICE_ACCOUNT. The project id is read from it unless
FIREWATCH_FCM_PROJECT_ID says otherwise. Neither ever goes in config.json.

The notification carries `data` so a tap opens the map on the fire (all values must be
strings): type, kind, event_id, municipality, lat, lon, z. The app ignores any push
whose `type` is not "fire" or whose coordinates fall outside Bosnia and Herzegovina.
"""
from __future__ import annotations

import json
import logging
import os
import time

import requests

from . import telegram
from .config import CFG, force_ipv4

log = logging.getLogger("firewatch.push")

SERVICE_ACCOUNT_ENV = "FIREWATCH_FCM_SERVICE_ACCOUNT"
PROJECT_ID_ENV = "FIREWATCH_FCM_PROJECT_ID"
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
API = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"

COUNTRY_TOPIC = "fire.all"
# The Android notification channel the app creates (MainActivity.kt).
ANDROID_CHANNEL = "crisis_alerts"
# A wait longer than this drops the push rather than stalling the alert job.
MAX_RETRY_WAIT = 5.0
ZOOM = 14

_token: dict = {"value": None, "expiry": 0.0}


def topic_for(municipality_id: str) -> str:
    return f"fire.{municipality_id}"


# ------------------------------------------------------------------ credentials

def _service_account() -> dict | None:
    raw = (os.environ.get(SERVICE_ACCOUNT_ENV) or "").strip()
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        log.warning("%s is not valid JSON - push off", SERVICE_ACCOUNT_ENV)
        return None
    return info if isinstance(info, dict) and info.get("private_key") else None


def project_id() -> str | None:
    env = (os.environ.get(PROJECT_ID_ENV) or "").strip()
    if env:
        return env
    return (_service_account() or {}).get("project_id")


def ready() -> tuple[bool, str]:
    """(usable, reason)."""
    if not CFG.get("push_enabled"):
        return False, "push_enabled is false"
    if not _service_account():
        return False, f"no service account ({SERVICE_ACCOUNT_ENV})"
    if not project_id():
        return False, f"no project id ({PROJECT_ID_ENV})"
    return True, "ready"


def _access_token(force: bool = False) -> str | None:
    """An OAuth2 bearer token for the service account, cached until shortly before it
    expires. None on any failure - a push that cannot authenticate is just not sent."""
    if not force and _token["value"] and time.time() < _token["expiry"] - 60:
        return _token["value"]
    info = _service_account()
    if not info:
        return None
    force_ipv4()
    try:
        # Imported here so everything else (and the tests) runs without google-auth.
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
        creds.refresh(Request())
    except Exception as exc:
        log.warning("fcm token request failed: %s", exc)
        return None
    _token["value"] = creds.token
    _token["expiry"] = creds.expiry.timestamp() if creds.expiry else time.time() + 1800
    return _token["value"]


# ---------------------------------------------------------------------- content

def _title_body(alerts: list[dict]) -> tuple[str, str]:
    """Telegram's merged message, first line as the title, the rest as the body."""
    lines = telegram.merged_alert_text(alerts).split("\n")
    return lines[0], "\n".join(lines[1:])


def build_message(topic: str, alerts: list[dict], municipality: str | None) -> dict:
    """The FCM v1 request body for one topic."""
    ev = alerts[0]["event"]
    title, body = _title_body(alerts)
    event_id = str(ev.get("id") or "")
    data = {
        "type": "fire",
        "kind": "+".join(a["kind"] for a in alerts),
        "event_id": event_id,
        "lat": f"{ev['lat']:.4f}",
        "lon": f"{ev['lon']:.4f}",
        "z": str(ZOOM),
    }
    if municipality:
        data["municipality"] = municipality
    message = {
        "topic": topic,
        "notification": {"title": title, "body": body},
        "data": data,
        "android": {
            "priority": "HIGH",
            "notification": {"channel_id": ANDROID_CHANNEL, "tag": event_id},
        },
        # iOS stays inert until its Firebase app and APNs key exist (see
        # Locator's crisis_alerts_plan.md); the block is harmless until then.
        "apns": {
            "headers": {"apns-priority": "10", "apns-collapse-id": event_id[:64]},
            "payload": {"aps": {"sound": "default"}},
        },
    }
    if not event_id:                       # tag / collapse id must not be empty
        message["android"]["notification"].pop("tag")
        message["apns"]["headers"].pop("apns-collapse-id")
    return {"message": message}


# ------------------------------------------------------------------------- send

def send(payload: dict) -> bool:
    """Publish one message. One retry for an expired token, one for a rate limit or a
    transient 5xx; everything else is logged and dropped."""
    ok, why = ready()
    if not ok:
        log.warning("push not sent: %s", why)
        return False
    topic = payload["message"]["topic"]
    url = API.format(project=project_id())
    force_ipv4()
    refreshed = retried = False
    while True:
        token = _access_token(force=refreshed)
        if not token:
            return False
        try:
            r = requests.post(url, json=payload, timeout=(CFG["connect_timeout"], CFG["http_timeout"]),
                              headers={"Authorization": f"Bearer {token}",
                                       "User-Agent": CFG["user_agent"]})
        except requests.RequestException as exc:
            log.warning("fcm request failed (%s): %s", topic, exc)
            return False
        if r.status_code == 200:
            log.debug("push published to %s", topic)
            return True
        if r.status_code == 401 and not refreshed:
            refreshed = True               # cached token rejected: fetch a fresh one
            continue
        if r.status_code in (429, 500, 503) and not retried:
            retried = True
            wait = _retry_after(r)
            if wait is not None and wait <= MAX_RETRY_WAIT:
                time.sleep(wait)
                continue
        log.warning("fcm rejected (%s): HTTP %s %s", topic, r.status_code, r.text[:200])
        return False


def _retry_after(r: requests.Response) -> float | None:
    try:
        return float(r.headers.get("Retry-After", 1))
    except ValueError:
        return None


def test_topic(target: str) -> str:
    """`all` -> the country topic, otherwise that municipality's."""
    return COUNTRY_TOPIC if target == "all" else topic_for(target)


def test_message(topic: str) -> dict:
    """A notification that cannot be taken for a real alert: titled TEST, aimed at the
    newest known fire (or the country centre) so a tap exercises the map deep link."""
    evs = telegram._latest_events()
    lat, lon = (evs[0]["lat"], evs[0]["lon"]) if evs else (44.1195, 18.1877)
    return {"message": {
        "topic": topic,
        "notification": {"title": "TEST - Firewatch push",
                         "body": "Test notification, not a real fire."},
        "data": {"type": "fire", "kind": "test", "event_id": "",
                 "lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "z": str(ZOOM)},
        "android": {"priority": "HIGH",
                    "notification": {"channel_id": ANDROID_CHANNEL}},
    }}


def send_alert_group(alerts: list[dict]) -> bool:
    """Publish one merged notification per matched municipality, and one to the
    country topic. Kinds are filtered by push_kinds before merging. Returns True if
    any publish succeeded."""
    kinds = CFG.get("push_kinds") or CFG.get("telegram_kinds") or []
    included = [a for a in alerts if not kinds or a["kind"] in kinds]
    if not included:
        return False
    municipality_ids = included[0]["event"].get("municipalities") or []
    if not municipality_ids:
        return False
    ok, why = ready()
    if not ok:
        log.info("push skipped: %s", why)
        return False
    kinds_label = "+".join(a["kind"] for a in included)
    any_sent = False
    for mid in municipality_ids:
        if send(build_message(topic_for(mid), included, mid)):
            log.info("push published to %s for %s", topic_for(mid), kinds_label)
            any_sent = True
    if send(build_message(COUNTRY_TOPIC, included, municipality_ids[0])):
        log.info("push published to %s for %s", COUNTRY_TOPIC, kinds_label)
        any_sent = True
    return any_sent


def send_alert(alert: dict) -> bool:
    return send_alert_group([alert])
