"""macOS notification delivery.

Two paths, preferred first:
  terminal-notifier  supports click-to-open, so the notification itself becomes a
                     link straight to Google Maps. Install with `brew install
                     terminal-notifier`.
  osascript          always present. Displays reliably but a click does nothing,
                     so the menu bar and map stay the way to reach the links.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys

from .config import CFG

log = logging.getLogger("firewatch.notify")

_TN = shutil.which("terminal-notifier")
# Linux desktops ship notify-send (libnotify). On a headless server none of the
# three exist, and backend() says so rather than naming one that is not installed -
# a poll cycle then simply reports the alert to the log and carries on.
_NS = None if sys.platform == "darwin" else shutil.which("notify-send")
_OSA = shutil.which("osascript")

ICONS = {
    "new": "🔥", "reignited": "🔥", "grew": "📈", "intensified": "📈",
    "corroborated": "🛰️", "extinguished": "✅",
}
TITLES = {
    "new": "New fire detected", "reignited": "Fire reignited",
    "grew": "Fire spreading", "intensified": "Fire intensifying",
    "corroborated": "Fire confirmed", "extinguished": "Fire appears out",
}


def _applescript_str(s: str) -> str:
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def send(title: str, message: str, subtitle: str = "", sound: str | None = None,
         url: str | None = None) -> bool:
    """Deliver one notification. Returns True if the command exited cleanly."""
    if not CFG["notifications_enabled"]:
        return False
    if _TN:
        cmd = [_TN, "-title", title, "-message", message, "-group", "firewatch"]
        if subtitle:
            cmd += ["-subtitle", subtitle]
        if sound:
            cmd += ["-sound", sound]
        if url:
            cmd += ["-open", url]
        return _run(cmd)

    if _NS:
        # notify-send has no subtitle and no sound; fold the subtitle into the body
        # so the place name is not simply lost.
        body = f"{subtitle} - {message}" if subtitle else message
        return _run([_NS, "-a", "FireWatch", "-u", "critical", title, body])

    if not _OSA:
        log.info("no notification backend; would have sent: %s - %s", title, message)
        return False

    script = f"display notification {_applescript_str(message)} with title {_applescript_str(title)}"
    if subtitle:
        script += f" subtitle {_applescript_str(subtitle)}"
    if sound:
        script += f" sound name {_applescript_str(sound)}"
    return _run([_OSA, "-e", script])


def _run(cmd: list[str]) -> bool:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=20)
        if r.returncode != 0:
            log.warning("notify failed: %s", r.stderr.decode()[:200])
        return r.returncode == 0
    except Exception as exc:
        log.warning("notify error: %s", exc)
        return False


def maps_url(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps?q={lat},{lon}"


def alert_text(alert: dict) -> tuple[str, str, str]:
    """(title, subtitle, message) for an alert record."""
    ev = alert["event"]
    kind = alert["kind"]
    icon = ICONS.get(kind, "🔥")
    where = "in Zavidovići" if ev["inside"] else "near Zavidovići"
    title = f"{icon} {TITLES.get(kind, kind)} {where}"
    subtitle = ev["place"]
    bits = [alert.get("detail", "")]
    if ev.get("latest_frp") is not None:
        bits.append(f"{ev['latest_frp']:.1f} MW")
    bits.append(f"{ev['dist_town_km']} km {ev['dir_town']} of town")
    if ev.get("wind"):
        w = ev["wind"]
        bits.append(f"wind {w['speed']:.0f} km/h {w['from']}")
    message = " · ".join(b for b in bits if b)
    return title, subtitle, message


def notify_alert(alert: dict) -> bool:
    ev = alert["event"]
    title, subtitle, message = alert_text(alert)
    sound = CFG["sound_new"] if alert["kind"] in ("new", "reignited") \
        else CFG["sound_update"]
    if alert["kind"] == "extinguished":
        sound = None
    return send(title, message, subtitle=subtitle, sound=sound,
                url=maps_url(ev["lat"], ev["lon"]))


def backend() -> str:
    if _TN:
        return "terminal-notifier"
    if _NS:
        return "notify-send"
    return "osascript" if _OSA else "none"
