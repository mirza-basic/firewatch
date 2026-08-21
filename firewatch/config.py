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

BOUNDARY_GEOJSON = DATA_DIR / "zavidovici.geojson"
SETTLEMENTS_JSON = DATA_DIR / "settlements.json"

DEFAULTS = {
    # NASA FIRMS map key. Limit is 5000 transactions / 10 min; an area call costs 2.
    "firms_map_key": "REDACTED-ROTATE-THIS-KEY",
    # Only the near-real-time sensors are useful for alerting. The _SP (standard
    # processing) sets lag by weeks and BA_* are burned-area products, not hotspots.
    "firms_datasets": [
        "VIIRS_SNPP_NRT",
        "VIIRS_NOAA20_NRT",
        "VIIRS_NOAA21_NRT",
        "MODIS_NRT",
    ],
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
    # lives in SQLite, so a 7-day window costs nothing extra per cycle.
    "window_hours": 720.0,
    # Which range the menu bar and map open on: 24h | 3d | 7d | 30d
    "default_range": "3d",
    # Keep detections this long. Must exceed the largest view range (30 days) or
    # the oldest data would be pruned just as it comes into view.
    "retention_days": 60,
    # Confidence floor for MTG FRP points (0-100). Low values are noisy.
    "mtg_min_confidence": 30,
    # Notify only when max FRP grows by at least this factor AND this many MW.
    "grow_frp_factor": 1.5,
    "grow_frp_min_mw": 3.0,
    # Do not re-notify the same event for the same reason within this window.
    "notify_cooldown_min": 25,
    "notifications_enabled": True,
    "sound_new": "Basso",
    "sound_update": "Tink",
    # Include detections outside the municipality but within this buffer, flagged
    # as "nearby" - a fire 2 km over the border still matters to you.
    "nearby_buffer_km": 6.0,
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
