"""Configuration and paths.

User-overridable settings live in the config directory below; anything absent falls
back to the defaults in this file.

Locations are platform-aware so the same tree runs on a Mac and on a Linux host.
macOS keeps exactly the paths it always had - moving them would strand an existing
install's database - while Linux follows the XDG layout. Any of the three can be
pointed somewhere else with an environment variable, which is what containers and
system services need: FIREWATCH_DATA_DIR, FIREWATCH_PUBLIC_DIR, FIREWATCH_CONFIG_DIR.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PKG_DIR.parent
DATA_DIR = PROJECT_DIR / "data"

MACOS = sys.platform == "darwin"


def _env_path(name: str) -> Path | None:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else None


def _data_dir() -> Path:
    override = _env_path("FIREWATCH_DATA_DIR")
    if override:
        return override
    if MACOS:
        return Path.home() / "Library" / "Application Support" / "FireWatch"
    return (_env_path("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "firewatch"


def _config_dir() -> Path:
    override = _env_path("FIREWATCH_CONFIG_DIR")
    if override:
        return override
    return (_env_path("XDG_CONFIG_HOME") or Path.home() / ".config") / "firewatch"


def _public_dir() -> Path:
    override = _env_path("FIREWATCH_PUBLIC_DIR")
    if override:
        return override
    if MACOS:
        # Not under Application Support - see the note further down; the space in
        # that path breaks ngrok's file server.
        return Path.home() / "Library" / "Caches" / "FireWatch" / "public"
    return (_env_path("XDG_CACHE_HOME") or Path.home() / ".cache") / "firewatch" / "public"


SUPPORT_DIR = _data_dir()
CONFIG_DIR = _config_dir()
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
PUBLIC_DIR = _public_dir()

BOUNDARY_GEOJSON = DATA_DIR / "zavidovici.geojson"
SETTLEMENTS_JSON = DATA_DIR / "settlements.json"
# The "nearby" band drawn on the map: everything within nearby_buffer_km of the
# outline. Like the two files above it is a build-time artifact - see
# geo.build_buffer() - so the running app needs neither shapely nor pyproj.
BUFFER_GEOJSON = DATA_DIR / "zavidovici-buffer.geojson"

# No FIRMS key is committed to this repository, deliberately. One used to be, which
# made a fresh clone work immediately at the cost of every clone sharing one key and
# one 5000-per-10-minutes limit - and putting the key in git history for good. Supply
# your own: `set-firms-key`, or FIRMS_MAP_KEY in the environment.
FIRMS_KEYCHAIN_SERVICE = "firewatch-firms"
FIRMS_SIGNUP_URL = "https://firms.modaps.eosdis.nasa.gov/api/map_key/"

DEFAULTS = {
    # NASA FIRMS map key. Limit is 5000 transactions / 10 min; an area call costs 2.
    # Empty on purpose - read through firms_key(), which prefers the environment and
    # the Keychain over this. Setting it here puts a credential in a plain file.
    "firms_map_key": "",
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
    # Built-in map server (`python3 -m firewatch serve`). Loopback by default:
    # putting the map on the public internet should be a decision someone typed.
    # On a real host, front it with nginx or Caddy for TLS rather than binding
    # 0.0.0.0 directly.
    "serve_host": "127.0.0.1",
    "serve_port": 8080,
    # /health reports "stale" past this age. The snapshot is rewritten every cycle,
    # and the fast loop runs every 4 minutes, so 15 covers a couple of missed
    # cycles without crying wolf over one slow WFS response.
    "health_max_age_s": 900,
    # Read timeout. WFS chunks legitimately take ~5 s for 24 h and ~21 s for 48 h,
    # so this has to be generous.
    "http_timeout": 90,
    # Connect timeout, which is a different question: a reachable host answers a
    # TCP handshake in well under a second. Applying 90 s to both meant a runner
    # whose packets were being dropped stalled 90 s per dataset - six minutes to
    # discover FIRMS was unreachable.
    "connect_timeout": 10,
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
        # Atomic: the recipient list is now re-read at send time, so a poller can be
        # reading this file at the moment `sms-add` rewrites it. A partial read would
        # be a JSON error, and an alert would go nowhere.
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.overrides(), indent=2, ensure_ascii=False))
        os.replace(tmp, CONFIG_FILE)


PUBLIC_URL_ENV = "FIREWATCH_PUBLIC_URL"


def public_url() -> str | None:
    """A fixed public address for the map, or None.

    `expose.find_tunnel()` answers the same question by asking the local ngrok agent,
    which is right on a laptop and useless anywhere else: a host serves the map at an
    address of its own that ngrok knows nothing about. Without this, an SMS from a
    hosted instance simply dropped its map link - the one line you most want when a
    fire alert arrives.

    Set it and it wins, which also means the poller stops asking ngrok at all.
    """
    v = (os.environ.get(PUBLIC_URL_ENV) or "").strip().rstrip("/")
    if not v:
        return None
    if not v.startswith(("http://", "https://")):
        logging.getLogger("firewatch.config").warning(
            "%s is not a URL (%r) - ignoring it", PUBLIC_URL_ENV, v[:40])
        return None
    return v


def keychain_secret(service: str, account: str = "firewatch") -> str | None:
    """One secret out of the macOS Keychain, or None.

    None on any other platform, if the `security` tool is missing, or on any error -
    callers all have a fallback, and a credential lookup that raises would take a
    poll cycle down with it.
    """
    if not shutil.which("security"):
        return None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


_warned_no_key = False


def firms_key() -> tuple[str | None, str]:
    """(key, where it came from), or (None, "not set").

    Environment first so a systemd unit or container can inject one without touching
    any file, then the Keychain, then config.json. There is no fallback: a missing
    key means FIRMS is skipped, which is a real and survivable state - Meteosat and
    Sentinel-3 need no credentials and keep working.
    """
    global _warned_no_key
    env = os.environ.get("FIRMS_MAP_KEY")
    if env and env.strip():
        return env.strip(), "environment"
    kc = keychain_secret(FIRMS_KEYCHAIN_SERVICE)
    if kc:
        return kc, "keychain"
    cfg = str(CFG.get("firms_map_key") or "").strip()
    if cfg:
        return cfg, "config.json"
    if not _warned_no_key:
        _warned_no_key = True
        logging.getLogger("firewatch.config").warning(
            "no FIRMS key, so VIIRS/MODIS is skipped - Meteosat and Sentinel-3 still "
            "run. Get one free at %s, then `set-firms-key` or set FIRMS_MAP_KEY.",
            FIRMS_SIGNUP_URL)
    return None, "not set"


def secrets() -> list[str]:
    """Every credential this process knows, for redaction.

    Resolved once and cached: the Keychain lookup shells out, and this is consulted
    on every log record.
    """
    global _secrets_cache
    if _secrets_cache is None:
        vals = {v for v in (firms_key()[0],) if v}
        for env in ("FIRMS_MAP_KEY", "HTTPSMS_API_KEY"):
            v = os.environ.get(env)
            if v and v.strip():
                vals.add(v.strip())
        for svc in (FIRMS_KEYCHAIN_SERVICE, "firewatch-httpsms"):
            v = keychain_secret(svc)
            if v:
                vals.add(v)
        # Short strings would redact half the log; a real key is never this small.
        _secrets_cache = sorted((v for v in vals if len(v) >= 12), key=len, reverse=True)
    return _secrets_cache


_secrets_cache: list[str] | None = None


def redact(text: str) -> str:
    """Replace any known credential with a marker."""
    for v in secrets():
        if v in text:
            text = text.replace(v, f"<redacted:{v[-4:]}>")
    return text


class RedactingFormatter(logging.Formatter):
    """Scrub credentials out of every log line, message and traceback alike.

    The FIRMS key travels *in the URL path*, so a connection error from requests
    puts the whole query - key included - into its exception message, and
    `log.warning("firms %s: %s", ds, exc)` writes it to disk. Redacting centrally
    rather than at each call site covers the paths nobody anticipated, which is the
    point: this leaked quietly for months.

    A formatter and not a Filter, which is the obvious choice and the wrong one:
    filters run *before* formatting, so on the first handler `record.exc_text` is
    still empty and the traceback escapes unredacted - then the formatter caches the
    raw text on the record, and only the *second* handler's filter sees it. With a
    file handler first and a stream handler second, that redacts the console and
    writes the key to disk: exactly backwards.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def ensure_dirs():
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


CFG = Config()
