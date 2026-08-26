"""Configuration and paths.

User-overridable settings live in ~/.config/firewatch/config.json; anything absent
falls back to the defaults below.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PKG_DIR.parent
DATA_DIR = PROJECT_DIR / "data"

SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "FireWatch"
CONFIG_DIR = Path.home() / ".config" / "firewatch"
CONFIG_FILE = CONFIG_DIR / "config.json"
DB_PATH = SUPPORT_DIR / "firewatch.db"
LOG_PATH = SUPPORT_DIR / "firewatch.log"
SNAPSHOT_PATH = SUPPORT_DIR / "snapshot.json"
MAP_PATH = SUPPORT_DIR / "fire-map.html"
# Directory served publicly when `firewatch-ctl expose` is used. It holds only the
# map and its data file - never the log, database or snapshot, which stay in
# SUPPORT_DIR and have no business being on a public URL. Created on demand, so
# nothing is published unless you ask for it.
#
# NOT under SUPPORT_DIR, despite everything else living there: ngrok serves a
# directory as `file://<path>`, and its file server (3.37.2) does not decode
# percent-escapes - so "Application%20Support" is looked up literally, finds
# nothing, and every request dies as ERR_NGROK_3004 with the tunnel still listed
# as up. A literal space cannot be sent either; the agent API rejects the URL.
# The only reliable fix is a path with no spaces in it. Caches is the right kind
# of place for it too: every file here is rewritten from the snapshot each cycle.
PUBLIC_DIR = Path.home() / "Library" / "Caches" / "FireWatch" / "public"

BOUNDARY_GEOJSON = DATA_DIR / "zavidovici.geojson"
SETTLEMENTS_JSON = DATA_DIR / "settlements.json"
# The "nearby" band drawn on the map: everything within nearby_buffer_km of the
# outline. Like the two files above it is a build-time artifact - see
# geo.build_buffer() - so the running app needs neither shapely nor pyproj.
BUFFER_GEOJSON = DATA_DIR / "zavidovici-buffer.geojson"

DEFAULTS = {
    # NASA FIRMS map key. Limit is 5000 transactions / 10 min; an area call costs 2.
    "firms_map_key": "REDACTED-ROTATE-THIS-KEY",
    # Only the near-real-time sensors are useful for alerting; the _SP (standard
    # processing) twins lag months and are used for history only, below. BA_* are
    # burned-area products, not hotspots, and are never fetched.
    "firms_datasets": [
        "VIIRS_SNPP_NRT",
        "VIIRS_NOAA20_NRT",
        "VIIRS_NOAA21_NRT",
        "MODIS_NRT",
    ],
    # Archive twins, used by `backfill` for windows older than the NRT horizon.
    # Measured against this key: NRT serves roughly the last 40 days and returns
    # a header-only CSV beyond that - HTTP 200, indistinguishable from "no
    # fires". SP (standard processing) lags about three months and then goes back
    # years, so 40-90 days ago has neither and stays empty until SP catches up.
    # NOAA21 has no SP dataset at all (HTTP 400 "Invalid source"), which is why
    # this list is one satellite shorter than the live one.
    "firms_archive_datasets": [
        "VIIRS_SNPP_SP",
        "VIIRS_NOAA20_SP",
        "MODIS_SP",
    ],
    # Deep-fetch windows starting further back than this use the archive list.
    "firms_nrt_days": 40,
    # Poll intervals in seconds. MTG publishes a new 10-minute slice with roughly
    # 25 minutes of latency, so checking every 4 minutes catches it promptly.
    "interval_mtg": 240,
    "interval_firms": 1200,
    "interval_sentinel3": 1200,
    # When a fire is active, tighten the fast loop.
    "interval_mtg_active": 120,
    # Detections are grouped into one event when within this distance and time gap.
    # MTG pixels are ~3-4 km at this latitude, hence the generous radius.
    "cluster_radius_km": 3.5,
    "cluster_gap_hours": 8.0,
    # An event with no new detection for this long is treated as no longer burning.
    "quiet_hours": 4.0,
    # Working window for clustering. This is the *view* horizon, not the fetch
    # horizon: each poll only fetches ~24 h of overlap because history already
    # lives in SQLite, so a year-long window is one wider read per cycle, not a
    # year of fetching.
    # At the observed rate (~130 detections a month) clustering a year is still a
    # few thousand rows, and the union-find pass breaks out on the 8 h gap.
    "window_hours": 8760.0,
    # Which range the menu bar and map open on: 24h | 3d | 7d | 30d | 1y
    "default_range": "3d",
    # Keep detections this long. Must exceed the largest view range (a year) or
    # the oldest data would be pruned just as it comes into view.
    "retention_days": 400,
    # Confidence floor for MTG FRP points (0-100). Low values are noisy.
    "mtg_min_confidence": 30,
    # Notify only when max FRP grows by at least this factor AND this many MW.
    "grow_frp_factor": 1.5,
    "grow_frp_min_mw": 3.0,
    # Do not re-notify the same event for the same reason within this window.
    "notify_cooldown_min": 25,
    "notifications_enabled": True,
    # Set by `expose`, cleared by `unexpose`. While true the poller re-creates the
    # ngrok tunnel whenever it finds it gone - the agent is shared with anything
    # else on this machine that uses ngrok, and restarting it silently drops every
    # tunnel it was carrying, including the URL already sent out in SMS alerts.
    "auto_expose": False,
    # SMS alerts via httpSMS (your Android phone is the gateway). Numbers must be
    # E.164, e.g. "+38761234567". The API key is never stored here - it comes from
    # HTTPSMS_API_KEY or the macOS Keychain (`firewatch-ctl set-sms-key`).
    "sms_enabled": True,
    "sms_from": "",                    # the Android phone running the httpSMS app
    "sms_to": [],                      # list of recipients, E.164
    # "extinguished" is left out on purpose: it is the informational one, and an
    # SMS costs a segment every time a fire merely cools off.
    "sms_kinds": ["new", "reignited", "intensified", "grew"],
    "sms_max_chars": 320,              # 2 GSM-7 segments
    "sound_new": "Basso",
    "sound_update": "Tink",
    # Include detections outside the municipality but within this buffer, flagged
    # as "nearby" - a fire just over the border still matters to you.
    # Changing this needs `python3 -m firewatch buffer` afterwards, or the band
    # drawn on the map goes stale and is silently omitted.
    "nearby_buffer_km": 2.0,
    "http_timeout": 90,
    "user_agent": "firewatch-zavidovici/1.0 (+https://github.com/) contact: local",
}

# Reference point for bearings/distances: Zavidovići town centre.
TOWN_LAT, TOWN_LON = 44.4388706, 18.1458239


class Config(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        if CONFIG_FILE.exists():
            try:
                self.update(json.loads(CONFIG_FILE.read_text()))
            except Exception:
                pass
        env_key = os.environ.get("FIRMS_MAP_KEY")
        if env_key:
            self["firms_map_key"] = env_key

    def overrides(self) -> dict:
        """Only the settings that actually differ from the defaults."""
        return {k: v for k, v in self.items()
                if k not in DEFAULTS or DEFAULTS[k] != v}

    def save(self):
        """Persist overrides only.

        Writing the whole dict would freeze every default at its current value,
        so later changes to DEFAULTS could never reach an existing install - which
        is exactly what happened when window_hours was raised for the month view.
        """
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(self.overrides(), indent=2, ensure_ascii=False))


def ensure_dirs():
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


CFG = Config()
