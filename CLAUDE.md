# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

FireWatch Zavidovići — near-live wildfire monitoring for the municipality of Grad
Zavidovići, Bosnia. A macOS menu bar app (`rumps`) that polls three satellite fire
feeds, clusters detections into tracked fire events, sends notifications on change,
and renders a live HTML map.

No build step, no package manager, no lockfile. Dependencies (`rumps`, `pyobjc`,
`requests`, `certifi`) are already installed system-wide against
`/Library/Frameworks/Python.framework/Versions/3.14/bin/python3`.

## Commands

```bash
# service control (installs a launchd login agent)
./firewatch-ctl install | uninstall | start | stop | restart | status | logs | poll | map

# direct CLI
python3 -m firewatch menubar          # the app (default)
python3 -m firewatch poll [range]     # one cycle + printed report
python3 -m firewatch watch            # headless loop, notifications only
python3 -m firewatch status [range]   # last published state
python3 -m firewatch map              # rebuild + open the HTML map
python3 -m firewatch backfill [days]  # deep history fetch (default 30, ~5 min)
python3 -m firewatch history [n]      # raw detections from SQLite
python3 -m firewatch quota            # FIRMS transaction usage (free call)
python3 -m firewatch test-notify

# tests
python3 tests_events.py               # 22 assertions, exits non-zero on failure
```

`[range]` is `24h | 3d | 7d | 30d`.

### Tests

`tests_events.py` is a plain linear script, not pytest — there is no test runner and
no way to select a single test by name. It builds synthetic detections via a local
`det()` helper, calls the real `events` functions, and tallies pass/fail. To isolate
one case, comment out the others or copy the case into a scratch file. It writes to
the real SQLite database only for the notification-cooldown case, which it cleans up
after itself.

## Architecture

One process does everything. `launchd` starts `menubar.py`, which runs `rumps` on the
main thread and owns a `Poller` background thread. There is no separate daemon, and
the menu bar, notifications and map all read the same published snapshot, so they
cannot disagree.

Each cycle (`poller.Poller.cycle`) runs a fixed chain — understanding this is most of
understanding the codebase:

```
sources.py   fetch each feed on its own schedule, failures isolated per source
geo.py       clip to the real municipality polygon (+6 km buffer → "nearby")
store.py     INSERT OR IGNORE by uid → returns ONLY never-seen detections
events.py    cluster detections into fire events (single linkage, 3.5 km / 8 h)
enrich.py    nearest settlement, wind, spread risk (active events only)
events.diff  compare with stored events → alert records
notify.py    deliver, rate-limited per (event, kind)
             then write snapshot.json + regenerate fire-map.html
```

Two design decisions drive everything else:

**Novelty is a property of the database, not a payload diff.** Every detection gets a
deterministic `uid` (source + sensor + rounded position + timestamp). "What is new"
is whatever `INSERT OR IGNORE` actually inserted. This is why a detection re-reported
across polls, or seen by two satellites, is only new once.

**Alerting operates on clustered events, never on raw detections.** One fire produces
one detection per sensor per overpass per hot pixel (45 for a single real fire), so
clustering is what makes an alert mean "something changed" rather than "a satellite
looked again". Events carry a stable id derived from their earliest detection.

**Fetch window ≠ view window ≠ retention.** Each poll fetches only ~24 h of overlap;
`window_hours` (720) is how far back events are *built*; `retention_days` (60) is how
long detections are *kept*. Retention must exceed the largest view range or the oldest
data is pruned exactly as it comes into view. Time-range filters are pure reads over
stored history and cost no API traffic.

### Data sources

| Module fn | Feed | Cadence | Notes |
|---|---|---|---|
| `fetch_mtg` | Meteosat MTG FRP (EUMETView WFS) | 10 min, ~25 min latency | the fast path; makes near-live alerting possible |
| `fetch_firms` | NASA FIRMS VIIRS ×3 + MODIS (CSV) | 4–7/day, ~3 h latency | sensitive (375 m); the only keyed API |
| `fetch_sentinel3` | Sentinel-3 A/B SLSTR FRP (EUMETView WFS) | ~2/day | extra passes; deepest archive (>1 year) |

`data/zavidovici.geojson` (boundary, OSM relation 2528292) and
`data/settlements.json` (413 places) are **build-time artifacts, committed to the
repo**. Nominatim and Overpass are never called at runtime. Regenerating them means
re-querying those services; Overpass is flaky and needs mirror fallback and retries.

## Landmines

These all cost real debugging time. Most are silent failures.

1. **Restart the service after every code change** — `./firewatch-ctl restart`. The
   running agent holds its modules in memory and keeps overwriting `snapshot.json`,
   so your edit looks like a no-op and the stale output looks like a logic bug.
2. **Use `requests`, never `urllib`/`urlopen`.** This Python build ships no CA bundle;
   stdlib HTTPS fails with `CERTIFICATE_VERIFY_FAILED` while `requests` works via
   `certifi`. The launchd plist also pins `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`.
3. **Never use `BBOX(geom, …)` in a WFS CQL filter.** It returns zero features with
   HTTP 200 on these layers — indistinguishable from "no fires". Filter on the `Lat`
   and `Lon` *attributes* instead.
4. **Keep WFS time filters ≤ 48 h per request.** Cost is superlinear (~5 s for 24 h,
   ~21 s for 72 h, a 30-day query never returns). Long windows must go through
   `_wfs_features_chunked`.
5. **FIRMS `days` is 1–5 only** and `start_date` counts *forward* from itself. Errors
   are HTTP 400 with an English sentence, not JSON; a valid query with no fires is
   HTTP 200 with only the CSV header. The guard checks status *and* body shape.
6. **FIRMS `acq_time` is an unpadded `HHMM` integer** — `22` means 00:22, not 22:00.
   Always `.zfill(4)` before parsing.
7. **`Config.save()` must write overrides only.** Writing the resolved dict freezes
   every default to disk, after which changing a default in code has no effect on an
   existing install.
8. **In the map, pixel and metre radii are different encodings.** `L.circleMarker`
   radius is screen pixels (fire *intensity*, fixed at all zooms); `L.circle` radius
   is metres (fire *footprint*, scales with zoom). Conflating them made markers look
   broken past zoom ~11. Footprint radius is the true max distance from centroid to a
   drawn detection — `extent_km/2` under-covers, because extent is a bounding-box
   diagonal while the marker sits at the centroid.
9. **A `new` alert requires `status == "active"`.** Without that check, backfilling
   history notifies for every fire that ever happened.
10. **Do not tighten `cluster_radius_km` below ~3.5.** It is set by MTG's ~4 km pixel
    size; smaller values split one geostationary fire into several events.
11. **`mapgen` writes two files**: `fire-map.html` (snapshot inlined for first
    paint) and `fire-map-data.js` (`window.__fwData = {...}`, written atomically via
    `os.replace`). A `file://` page cannot XHR a sibling JSON file, but it can load
    one as a `<script src>`, so the page refreshes in place by injecting a
    cache-busted script tag — it does **not** reload. Anything added to `applyData()`
    must not call `fitBounds`/`setView`/`flyTo`, or the reader's view gets yanked
    every minute. Programmatic `openPopup()` needs `autoPan` disabled for the same
    reason.

## SMS alerts

`sms.py` posts to httpSMS (`POST https://api.httpsms.com/v1/messages/send`, header
`x-api-key`, body `{from,to,content}`). The poller treats it as a channel
independent of the desktop notification — `mark_notified` fires if *either*
succeeded, so a failed notification never suppresses the SMS.

- The API key comes from `HTTPSMS_API_KEY` or the macOS Keychain (service
  `firewatch-httpsms`), never `config.json`.
- **Message text must stay ASCII.** `ascii_only()` folds Bosnian diacritics
  because one non-GSM character switches the whole message to UCS-2, dropping a
  segment from 160 characters to 70. `segments()` reports the real cost.
- The map link is resolved at send time via `expose.find_tunnel()` — the free
  ngrok URL changes on every agent restart, so it must not be cached.
- `sms_to` is a **list**. `recipients()` also accepts a plain or
  comma-separated string for backward compatibility. One recipient uses
  `/messages/send`; two or more use `/messages/bulk-send` with `to` as an
  array — one call for the fan-out.
- Inert until `sms_from`, `sms_to` and the key are all present; `sms.ready()`
  returns the specific reason it is not usable, including non-E.164 numbers.

## State and configuration

- `~/Library/Application Support/FireWatch/` — `firewatch.db` (SQLite: `detections`,
  `events`, `notified`, `meta`), `snapshot.json`, `fire-map.html`, `firewatch.log`,
  `stdout/stderr.log`.
- `~/.config/firewatch/config.json` — user overrides only; anything absent falls back
  to `DEFAULTS` in `config.py`. Restart after editing.
- The FIRMS map key is committed in `config.py` DEFAULTS (and in the legacy shell
  scripts). `FIRMS_MAP_KEY` in the environment overrides it.

Notifications go through `osascript` (attributed to *Script Editor*, click does
nothing) unless `terminal-notifier` is installed, in which case notifications become
clickable links to Google Maps. `notify.backend()` reports which is active.

## Repo conventions

- `firewatch/` is the current system. `fire-detection-zavidovici.sh`,
  `fire-detection-bih.sh` and `.bak` are the superseded originals — they still run
  standalone; leave them alone rather than extending them.
  `fire-detection-requraments` is the original hand-written spec.
- `docs/*.html` are self-contained documentation pages, also published as Claude
  artifacts. Update them when behaviour changes — particularly the "field notes" and
  "landmines" content, which is the part that goes stale invisibly.
- Code style follows the existing modules: `from __future__ import annotations`,
  module-level docstring explaining *why*, comments reserved for non-obvious
  decisions and measured facts rather than restating the code.
- There is no `.gitignore`, and `firewatch/__pycache__/*.pyc` is currently tracked,
  so `git status` is noisy after any run. Consider ignoring them before committing.
