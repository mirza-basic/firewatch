"""macOS menu bar front end.

The title shows live state at a glance:
    🌲              nothing burning
    🔥 2!!  · 48MW  two active fires, worst one high severity at 48 MW

Each fire becomes a submenu with its details and one-click links out to maps.
The whole menu is rebuilt each cycle - simpler and far less error-prone than
patching individual rows, and cheap at this scale.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import webbrowser
from functools import partial
from pathlib import Path

import rumps

from . import events as ev_mod
from . import mapgen, notify, poller, sources
from .config import CFG, LOG_PATH, MAP_PATH, SUPPORT_DIR

log = logging.getLogger("firewatch.menubar")

IDLE_TITLE = "🌲"
SEV_MARK = {"low": "", "moderate": "!", "high": "!!", "severe": "!!!"}


def _ago(minutes: float) -> str:
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes:.0f}m ago"
    if minutes < 1440:
        return f"{minutes / 60:.1f}h ago"
    return f"{minutes / 1440:.1f}d ago"


def _info(text: str) -> rumps.MenuItem:
    """A non-clickable informational row."""
    return rumps.MenuItem(text, callback=None)


class FireWatchApp(rumps.App):
    def __init__(self):
        super().__init__("FireWatch", title=IDLE_TITLE, quit_button=None)
        self.snapshot = poller.load_snapshot()
        self.range_key = ev_mod.resolve_range(CFG.get("default_range"))
        self.poller = poller.Poller(on_update=self._on_update)
        self._refreshing = False
        self._rebuild()
        self.poller.start()

    # ------------------------------------------------------------------- updates
    def _on_update(self, snap: dict) -> None:
        self.snapshot = snap
        self._refreshing = False
        try:
            self._rebuild()
        except Exception:
            log.exception("menu rebuild failed")

    def _rebuild(self) -> None:
        snap = self.snapshot
        summary = snap.get("summary", {})
        n = summary.get("n_active", 0)

        if n:
            worst = summary.get("worst") or 0
            mark = SEV_MARK.get(summary.get("severity", ""), "")
            self.title = f"🔥 {n}{mark}" + (f" · {worst:.0f}MW" if worst else "")
        else:
            self.title = IDLE_TITLE

        gen = snap.get("generated_at", "")
        status = ("Refreshing…" if self._refreshing else
                  f"{n} active fire{'s' if n != 1 else ''} · "
                  f"{snap.get('n_detections', 0)} detections / "
                  f"{snap.get('window_hours')}h window")

        ss = snap.get("source_status", {})
        ok = [k for k, v in ss.items() if v.get("ok")]
        bad = [k for k, v in ss.items() if not v.get("ok")]
        src_line = f"Updated {gen[11:16]}Z · {'+'.join(ok) if ok else 'no sources'}"
        if bad:
            src_line += f" · failed: {'+'.join(bad)}"

        rows: list = [_info(status), _info(src_line), rumps.separator,
                      self._range_item(snap), rumps.separator]

        evs = ev_mod.filter_events(snap.get("events", []), self.range_key)
        label = (snap.get("ranges", {}) or ev_mod.RANGES).get(self.range_key)
        label = label if isinstance(label, str) else ev_mod.RANGES[self.range_key]["label"]
        LIMIT = 15
        if not evs:
            rows.append(_info(f"No fires · {label.lower()}"))
        for e in evs[:LIMIT]:
            rows.append(self._fire_item(e))
        if len(evs) > LIMIT:
            rows.append(_info(f"…and {len(evs) - LIMIT} more — see the map"))

        notif = rumps.MenuItem("Notifications", callback=self.toggle_notify)
        notif.state = 1 if CFG["notifications_enabled"] else 0

        rows += [
            rumps.separator,
            rumps.MenuItem("Open Fire Map", callback=self.open_map, key="m"),
            rumps.MenuItem("Refresh Now", callback=self.refresh, key="r"),
            rumps.separator,
            notif,
            rumps.MenuItem("Send Test Notification", callback=self.test_notify),
            rumps.MenuItem(f"Alerts via: {notify.backend()}", callback=None),
            rumps.separator,
            rumps.MenuItem("FIRMS Quota…", callback=self.show_quota),
            rumps.MenuItem("Open Data Folder", callback=self.open_folder),
            rumps.MenuItem("View Log", callback=self.open_log),
            rumps.separator,
            rumps.MenuItem("Quit FireWatch", callback=self.quit_app, key="q"),
        ]
        self.menu.clear()
        self.menu = rows

    def _range_item(self, snap: dict) -> rumps.MenuItem:
        """Last 24h / 3 days / 7 days / month, with the event count for each."""
        counts = snap.get("range_counts", {})
        cur = ev_mod.RANGES[self.range_key]["label"]
        parent = rumps.MenuItem(f"Show: {cur}", callback=None)
        for key, spec in ev_mod.RANGES.items():
            c = counts.get(key, {})
            n = c.get("events")
            suffix = f"  ({n})" if n is not None else ""
            item = rumps.MenuItem(f"{spec['label']}{suffix}",
                                  callback=partial(self._set_range, key))
            item.state = 1 if key == self.range_key else 0
            parent.add(item)
        return parent

    def _set_range(self, key, _sender=None):
        self.range_key = key
        CFG["default_range"] = key
        CFG.save()
        self._rebuild()
        # Rewrite the map so it opens on the same range the menu is showing.
        # The snapshot on disk carries the range chosen at poll time, so it has
        # to be updated here or the map would reopen on the previous selection.
        try:
            self.snapshot["default_range"] = key
            mapgen.render(self.snapshot)
        except Exception:
            log.exception("map re-render failed")

    def _fire_item(self, e: dict) -> rumps.MenuItem:
        icon = "🔥" if e["status"] == "active" else "💤"
        frp = f"{e['latest_frp']:.0f} MW" if e.get("latest_frp") is not None else "—"
        parent = rumps.MenuItem(
            f"{icon} {frp} · {e['place']} · {_ago(e['age_min'])}", callback=None)

        peak = (f"{e['max_frp']:.1f} MW" if e.get("max_frp") is not None else "n/a")
        parent.add(_info(f"Severity: {e['severity']} · {e['status']}"))
        parent.add(_info(f"Peak FRP: {peak}"))
        shown = len(e.get("series", []))
        det_txt = (f"{shown} of {e['n_det']} detections" if shown != e["n_det"]
                   else f"{e['n_det']} detections")
        parent.add(_info(f"{det_txt} · {', '.join(e['sources'])}"))
        parent.add(_info(f"{e['dist_town_km']} km {e['dir_town']} of Zavidovići"))
        parent.add(_info(f"Footprint: {e['extent_km']} km across"))
        parent.add(_info(f"First seen: {e['first_ts'][5:16].replace('T', ' ')}Z"))
        if not e.get("inside"):
            parent.add(_info("⚠︎ outside municipality boundary"))
        w = e.get("weather")
        if w:
            parent.add(_info(f"{w['temp']}°C · RH {w['humidity']}%"))
            parent.add(_info(f"Wind {w['speed']:.0f} km/h from {w['from']}"
                             f" → spreads {w['towards']}"))
            if e.get("risk"):
                parent.add(_info(f"Spread risk: {e['risk']}"))
        parent.add(rumps.separator)
        parent.add(rumps.MenuItem("Open in Google Maps",
                                  callback=partial(self._open_maps, e)))
        parent.add(rumps.MenuItem("Open Satellite View",
                                  callback=partial(self._open_sat, e)))
        parent.add(rumps.MenuItem("Copy Coordinates",
                                  callback=partial(self._copy_coords, e)))
        parent.add(rumps.MenuItem("Show on Fire Map", callback=self.open_map))
        return parent

    # ----------------------------------------------------------------- callbacks
    def _open_maps(self, e, _sender=None):
        webbrowser.open(notify.maps_url(e["lat"], e["lon"]))

    def _open_sat(self, e, _sender=None):
        webbrowser.open("https://www.google.com/maps/@?api=1&map_action=map"
                        f"&center={e['lat']},{e['lon']}&zoom=15&basemap=satellite")

    def _copy_coords(self, e, _sender=None):
        subprocess.run("pbcopy", input=f"{e['lat']}, {e['lon']}".encode())
        notify.send("FireWatch", f"{e['lat']}, {e['lon']}", subtitle="Copied")

    def open_map(self, _=None):
        if not Path(MAP_PATH).exists():
            mapgen.render(self.snapshot)
        webbrowser.open(Path(MAP_PATH).as_uri())

    def refresh(self, _=None):
        if self._refreshing:
            return
        self._refreshing = True
        self._rebuild()
        threading.Thread(target=self.poller.poll_now, daemon=True).start()

    def toggle_notify(self, sender):
        sender.state = 0 if sender.state else 1
        CFG["notifications_enabled"] = bool(sender.state)
        CFG.save()

    def test_notify(self, _=None):
        notify.send("🔥 FireWatch test", "Notifications are working",
                    subtitle="Grad Zavidovići", sound=CFG["sound_update"])

    def show_quota(self, _=None):
        q = sources.firms_quota()
        rumps.alert("FIRMS transaction quota",
                    f"{q['current_transactions']} / {q['transaction_limit']} used "
                    f"per {q['transaction_interval']}" if q
                    else "Could not read quota")

    def open_folder(self, _=None):
        subprocess.run(["open", str(SUPPORT_DIR)])

    def open_log(self, _=None):
        subprocess.run(["open", "-t", str(LOG_PATH)])

    def quit_app(self, _=None):
        self.poller.stop()
        rumps.quit_application()


def main():
    poller.setup_logging()
    FireWatchApp().run()
