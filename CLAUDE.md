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
python3 -m firewatch buffer [km]      # rebuild the drawn "nearby" band
python3 -m firewatch reclip [--apply] # drop stored history the clip now rejects
python3 -m firewatch serve [host:port] [--no-poll]   # poll + serve the map over HTTP
python3 -m firewatch backfill [days]  # deep history fetch (default 30, ~5 min)
python3 -m firewatch history [n]      # raw detections from SQLite
python3 -m firewatch quota            # FIRMS transaction usage (free call)
python3 -m firewatch test-notify

# tests
python3 tests_events.py               # 23 assertions, exits non-zero on failure
```

`[range]` is `24h | 3d | 7d | 30d | 1y`.

### Tests

`tests_events.py` is a plain linear script, not pytest — there is no test runner and
no way to select a single test by name. It builds synthetic detections via a local
`det()` helper, calls the real `events` functions, and tallies pass/fail. To isolate
one case, comment out the others or copy the case into a scratch file. It writes to
the real SQLite database only for the notification-cooldown case, which it cleans up
after itself.

**The map's JavaScript has no automated tests, by decision.** `mapgen.py` emits ~1,900
lines of JS that Python cannot reach. Testing it means jsdom + Leaflet under npm, and
this project deliberately has no package manager — that trade was considered and
declined, so do not add one. Verify map changes by rendering (`python3 -m firewatch
map`) and looking, and treat anything in the generated page as unguarded: a jsdom
harness built during development caught `L.Polygon.getCenter()` throwing before its
layer is on a map, which would have broken every area measurement, and that class of
bug will not be caught here again.

## Architecture

One process does everything. `launchd` starts `menubar.py`, which runs `rumps` on the
main thread and owns a `Poller` background thread. There is no separate daemon, and
the menu bar, notifications and map all read the same published snapshot, so they
cannot disagree.

Each cycle (`poller.Poller.cycle`) runs a fixed chain — understanding this is most of
understanding the codebase:

```
sources.py   fetch each feed on its own schedule, failures isolated per source
geo.py       clip to the real municipality polygon (+2 km buffer → "nearby")
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
`window_hours` (8760) is how far back events are *built*; `retention_days` (400) is how
long detections are *kept*. Retention must exceed the largest view range or the oldest
data is pruned exactly as it comes into view. Time-range filters are pure reads over
stored history and cost no API traffic — but they can only show what is stored, so the
year view is shallow until `backfill` has run, and no deeper than the feeds' own
archives (FIRMS is NRT-only; Sentinel-3 goes back furthest).

### Data sources

| Module fn | Feed | Cadence | Notes |
|---|---|---|---|
| `fetch_mtg` | Meteosat MTG FRP (EUMETView WFS) | 10 min, ~25 min latency | the fast path; makes near-live alerting possible |
| `fetch_firms` | NASA FIRMS VIIRS ×3 + MODIS (CSV) | 4–7/day, ~3 h latency | sensitive (375 m); the only keyed API |
| `fetch_sentinel3` | Sentinel-3 A/B SLSTR FRP (EUMETView WFS) | ~2/day | extra passes; deepest archive (>1 year) |

**How far back each feed actually reaches**, measured (August 2026) over a 600 km
box so that an empty answer means an empty archive rather than a quiet sky —
`sources.ARCHIVE_DAYS` and `firms_nrt_days` encode these, and `backfill` clamps to
them rather than crawling chunks that can only be empty:

| Feed | Depth | |
|---|---|---|
| MTG WFS | ~40 days | thins from ~day 30; nothing at 60 |
| Sentinel-3 WFS | > 1 year | sparse but real throughout |
| FIRMS `*_NRT` | ~40 days | header-only CSV beyond that, HTTP 200 |
| FIRMS `*_SP` | ~90 days → years | standard processing lags ~3 months |

So roughly **40–90 days back, FIRMS has nothing to give**: too old for NRT, too
recent for SP. That gap fills itself in as SP catches up, but only if `backfill` is
run again — nothing re-fetches history on its own.

`data/zavidovici.geojson` (boundary, OSM relation 2528292),
`data/settlements.json` (413 places) and `data/zavidovici-buffer.geojson` (the
drawn "nearby" band) are **build-time artifacts, committed to the repo**. Nominatim
and Overpass are never called at runtime. Regenerating the first two means
re-querying those services; Overpass is flaky and needs mirror fallback and retries.

The buffer band is regenerated with `python3 -m firewatch buffer [km]`, which is the
only thing in the project that imports `shapely` and `pyproj` — both are imported
inside `geo.build_buffer()` so the running app still needs only its four
dependencies. `geo.load_buffer()` returns `None` unless the artifact's `buffer_km`
matches the configured `nearby_buffer_km`, and `mapgen` then omits the band rather
than drawing a confident line in the wrong place. **Change `nearby_buffer_km` and
the band silently disappears from the map until you rebuild it.**

Narrowing `nearby_buffer_km` does **not** retroactively drop history. The clip runs
at fetch time only, so detections the old, wider setting let in stay in the database
and on the map until `python3 -m firewatch reclip --apply` removes them — a dry run
by default, and it copies the database first, because retention is 400 days and the
feeds will not serve that history again. Events need no separate cleanup:
`save_events` deletes any event id that no longer falls out of the clustering, so
they correct themselves on the next cycle.

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

   **A FIRMS dataset name is not a sensor identity either.** `VIIRS_SNPP_NRT` and
   `VIIRS_SNPP_SP` are the same instrument on the same overpass, processed twice, so
   putting the dataset name in the `uid` stored one pixel as two detections — which
   inflates `n_det`, footprints and "grew" alerts wherever the live feed and the
   archive overlap. `_firms_sensor()` strips the suffix for both `uid` and `sensor`,
   the original stays in `raw.dataset`, and `store.UID_SCHEME` migrates rows written
   under the old scheme. Note `VIIRS_NOAA21` has no SP twin — HTTP 400 "Invalid
   source" — so the archive list is one satellite shorter.
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
12. **Never point an ngrok `file://` tunnel at a path containing a space.**
    `as_uri()` percent-encodes it and the agent's file server does not decode the
    escape, so it serves nothing: the tunnel is listed as up, the URL answers 503
    `ERR_NGROK_3004`, and `http.count` in the tunnel metrics stays at zero. This is
    why `PUBLIC_DIR` lives under `~/Library/Caches` and not next to everything else
    in *Application Support*. A literal space fails too — the agent API rejects the
    URL outright.
13. **A freshly started ngrok agent answers its local API before it can create
    tunnels.** For a second or two every POST is refused with 503 / `error_code
    104` ("not yet ready"), so `agent_up()` returning True is not the same as
    ready. Worse, a POST that fails this way *keeps the name*: afterwards it 404s
    on GET, 404s on DELETE and 400s on POST for the life of that agent process.
    `_create_tunnel` waits the window out and, if it still meets a burned name,
    moves to `firewatch-2` — which is why `find_tunnel` matches suffixed names.
14. **`L.Polygon.getCenter()` throws until the layer is on a map** ("Must add
    layer to map before using getCenter"), so the measure tool adds the polygon to
    its layer group *before* it builds the area label, not after.
15. **Suppressing hit testing while measuring needs `!important`.** Leaflet's own
    `.leaflet-pane>svg path.leaflet-interactive` rule is more specific than any
    reasonable selector, so without it a click meant to drop a vertex opens a fire
    popup instead. Measure geometry lives in its own `measure` pane (z-index 650)
    so the 60 s refresh, which clears and rebuilds every fire layer, cannot take it
    with it.
16. **ngrok never closes a `file://` tunnel.** The DELETE hangs and the tunnel stays
    listed, so `unexpose` switches publishing off first and then says plainly that
    the URL stays up until the agent restarts. A free account also allows only three
    endpoints per agent session — a fourth is refused with `ERR_NGROK_324`, and the
    session is shared with anything else on the machine that uses ngrok.

## Running on a Linux host

`menubar.py` is the only macOS-specific module - `rumps`/`pyobjc`, `launchctl`,
`pbcopy`, `open` - and `__main__` imports it lazily, so every other command runs
headless on Linux with `requests` and `certifi` alone. Three things make that work:

- **Paths are platform-aware** (`config._data_dir` and friends). macOS keeps exactly
  the paths it always had, because moving them would strand an existing install's
  database; Linux follows XDG. `FIREWATCH_DATA_DIR`, `FIREWATCH_PUBLIC_DIR` and
  `FIREWATCH_CONFIG_DIR` override all of it, which is what containers need.
- **`notify.py` has three backends** - `terminal-notifier`, `notify-send`,
  `osascript` - and `backend()` returns `"none"` when none is present rather than
  naming one that is not installed. A missing backend logs the alert and returns
  False; it never breaks a cycle.
- **The Keychain lookup already degrades**: no `security` binary means the httpSMS
  key comes from `HTTPSMS_API_KEY`.

`serve.py` is the hosting counterpart to `expose.py`. ngrok suits a laptop; a host
with its own address wants a plain static server over `PUBLIC_DIR`, behind nginx or
Caddy for TLS. `serve` polls *and* serves in one process on purpose - a host serving
a stale map because the poller was a second unit that died is worse than either.

Two things it must keep doing, both learned the hard way:

1. **`refresh_public()` renders an empty snapshot when there is none.** Otherwise a
   fresh container has an empty `PUBLIC_DIR` and `SimpleHTTPRequestHandler` answers
   `/` with a *directory listing*.
2. **`list_directory` is overridden to 404.** Belt and braces for the same thing.

`/health` (alias `/healthz`) answers 200 only while `snapshot.json` is younger than
`health_max_age_s` (900). That distinction is the whole point: the static files stay
perfectly serveable long after polling has died, so a monitor that only fetches `/`
would never notice. It reports `starting` (no snapshot yet), `stale` (too old) and
`sources_down` (a fresh snapshot in which every feed failed), all as 503. It
deliberately exposes no keys, paths or the public URL - anyone who can reach the map
can reach it.

`deploy/` holds a Dockerfile, a systemd unit and an nginx server block. The
Dockerfile **has been built and run** (linux/arm64, 376 MB): all three feeds polled
from inside the container, `/health` went 503 `starting` then 200 `ok`, `docker stop`
exited 0 in 0.3 s, and a marker row written to the volume survived a restart. The
systemd unit and the nginx block are still unexercised.

`.dockerignore` deliberately does **not** exclude `menubar.py`. Dropping a module out
of a package would mean a future import of it breaks only inside the image - a bug
that cannot reproduce locally. It is 15 KB and `__main__` imports it lazily.

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
- **The sender is deploy config; the recipients are live state.** `sms.sender()`
  reads `HTTPSMS_FROM` then `sms_from` - it belongs to the same httpSMS account as
  the key, is fixed for the life of a deployment, and so belongs in the environment.
  `sms.recipients()` deliberately does **not** use the import-time `CFG` snapshot: it
  re-reads `config.json` on every call, because numbers get added while the service
  is running and a long-lived poller would otherwise keep texting the old list until
  somebody restarted it. `Config.save()` is atomic (`os.replace`) for the same
  reason - two processes now touch that file, and a partial read would drop an alert.
- `sms_to` is a **list**. `recipients()` also accepts a plain or
  comma-separated string for backward compatibility. One recipient uses
  `/messages/send`; two or more use `/messages/bulk-send` with `to` as an
  array — one call for the fan-out.
- Inert until `sms_from`, `sms_to` and the key are all present; `sms.ready()`
  returns the specific reason it is not usable, including non-E.164 numbers.

## Publishing the map

`expose.py` serves `PUBLIC_DIR` — the map and its data file, nothing else — through
the ngrok agent's local API on :4040, joining whatever session is already running
rather than starting a second one.

The failure that matters is invisible from inside the app: the agent is shared with
any other ngrok user on the machine, and when it restarts it drops every tunnel it
was carrying. The poller keeps writing fresh files and the local map keeps working,
so the only symptom is that the public URL — already sent out in SMS alerts, and
unrecoverable once gone, because free-tier URLs are random — quietly stops
resolving. So `expose.ensure()` runs every cycle and rebuilds the tunnel whenever it
finds it missing, gated on `auto_expose`: set by `expose`, cleared by `unexpose`, so
nothing is ever published that was not asked for. The resulting URL goes into
`snapshot.json` as `public_url`, which is where the menu bar reads it — querying
ngrok during a menu rebuild would put a blocking HTTP call on the main thread.

## State and configuration

- `~/Library/Application Support/FireWatch/` — `firewatch.db` (SQLite: `detections`,
  `events`, `notified`, `meta`), `snapshot.json`, `fire-map.html`, `firewatch.log`,
  `stdout/stderr.log`.
- `~/Library/Caches/FireWatch/public/` — the only directory ever exposed publicly.
  Rewritten from the snapshot every cycle, so losing it costs nothing. It is *not*
  under Application Support, for the reason in landmine 12.
- `~/.config/firewatch/config.json` — user overrides only; anything absent falls back
  to `DEFAULTS` in `config.py`. Restart after editing.
- **No credential is committed to this repo.** The FIRMS map key resolves through
  `config.firms_key()`, which returns `(key, source)` or `(None, "not set")` and
  tries, in order: `FIRMS_MAP_KEY` in the environment, the macOS Keychain (service
  `firewatch-firms`, set by `set-firms-key`), then `firms_map_key` in `config.json`.
  There is no fallback. A key used to be committed, which made a fresh clone poll
  immediately at the cost of one shared key, one shared rate limit, and a credential
  in git history for good.
- **No key is a supported state, not a failure.** `fetch_firms` raises
  `sources.NoCredentials`, the poller records `configured: False` and logs at *info*,
  the CLI prints `[----]` rather than `[FAIL]`, and `/health` excludes the source
  instead of counting it down - otherwise an install deliberately running without
  FIRMS would report 503 for ever. Meteosat and Sentinel-3 need no credentials, so
  two of three feeds keep working.
- `quota` prints the last four characters and the source, never the key.
  `config.keychain_secret()` is the one Keychain reader; `sms.api_key()` uses it too.
- **The FIRMS key travels in the URL *path***, so any `requests` exception carries the
  whole query - key included - and `log.warning("firms %s: %s", ds, exc)` wrote it
  straight to `firewatch.log`. `config.RedactingFormatter` now scrubs every known
  credential out of every log line. It is a **formatter, not a `logging.Filter`** -
  filters run before formatting, so the first handler emits the traceback while
  `record.exc_text` is still empty, then caches the raw text for the second handler
  to redact. With the file handler first, that redacts the console and writes the key
  to disk: exactly backwards.

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
