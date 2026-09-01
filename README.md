# FireWatch Zavidovići

### 🔥 [Live map](https://mirza-basic.github.io/firewatch) · 📖 [Documentation](https://mirza-basic.github.io/firewatch/docs/)

Both are published by the `poll` workflow: the map is rebuilt from the database
every run, and the documentation is generated from `docs/*.html` beside it.

> **The pages**, readable on the site above or straight from this repository:
> - [`docs/firewatch-documentation.html`](docs/firewatch-documentation.html) — how the
>   system works: data sources, field-level data reference, the processing pipeline,
>   change-detection state machine, rate limits and field notes.
> - [`docs/firewatch-api-reference.html`](docs/firewatch-api-reference.html) — how we
>   call the APIs: every endpoint, exact parameters, real captured request/response
>   traffic, error modes, chunking and quota costs.
> - [`docs/firewatch-macos.html`](docs/firewatch-macos.html) — install and run it on a Mac.
> - [`docs/firewatch-linux.html`](docs/firewatch-linux.html) — the same headless, on Linux.
> - [`docs/firewatch-hosting.html`](docs/firewatch-hosting.html) — serving the map from a
>   host: TLS, health checks, Docker, where to run it.
> - [`docs/firewatch-field-manual.html`](docs/firewatch-field-manual.html) — day-to-day
>   operation, configuration, and moving it to another municipality.
> - [`docs/firewatch-sms-preview.html`](docs/firewatch-sms-preview.html) — every alert
>   SMS, rendered, with its character budget.
> - [`docs/firewatch-fork.html`](docs/firewatch-fork.html) — **run this for your own
>   town**: take a copy, point it at another municipality, and let GitHub Actions poll
>   and GitHub Pages host it, for free.

Near-live wildfire monitoring for **Grad (opština) Zavidovići**, as a macOS menu bar
app with notifications and a live map.

    🌲              nothing burning
    🔥 1! · 11MW    one active fire, moderate, 11 MW peak radiative power

## Why three sources

NASA FIRMS is sensitive but slow: polar-orbiting satellites see Zavidovići **4–7
times a day** and publish about **3 hours** after observation, leaving 7–10 hour
blind gaps overnight and mid-morning.

A geostationary source closes those gaps. **Meteosat Third Generation** stares at
the same hemisphere continuously and publishes a new Fire Radiative Power field
every **10 minutes**, with roughly **25 minutes** of latency:

| Source | Resolution | Cadence | Latency | Role |
|---|---|---|---|---|
| Meteosat MTG FRP | ~4 km at this latitude | **10 min** | ~25 min | continuity, intensity trend |
| VIIRS + MODIS (FIRMS NRT) | 375 m / 1 km | 4–7 / day | ~3 h | sensitivity, small/new fires |
| Sentinel-3 SLSTR FRP | 1 km | ~2 / day | ~1–3 h | extra overpasses, corroboration |

Meteosat and Sentinel-3 need no credentials; FIRMS needs a free key (below). On a
real fire — 20 August 2026, in forest 18 km south-east of town — MTG produced 18
detections between 10:50Z and 16:30Z with a visible FRP decline (10.5 → 6.9 MW),
where FIRMS gave 7 scattered samples and Sentinel-3 caught the earliest sign of it
at 19:47Z the previous evening.

Honest limit: nothing here is truly real-time. The floor is roughly
**25–35 minutes** behind the flame front, and MTG's coarse pixels only see fires of
roughly 5 MW and up, so FIRMS still matters for catching small ones.

## Install

    ./firewatch-ctl install

Installs a `launchd` login agent, starts the menu bar app, and keeps it running.
Look for 🌲 in the menu bar.

**NASA FIRMS key.** No credential is committed to this repository. Get a free one at
<https://firms.modaps.eosdis.nasa.gov/api/map_key/> — it arrives in seconds — then:

    ./firewatch-ctl set-firms-key      # stores it in the Keychain

or set `FIRMS_MAP_KEY` in the environment, which is what a server or container wants.
Without a key FireWatch still runs: VIIRS/MODIS is skipped and reported as `[----]`
rather than a failure, while Meteosat and Sentinel-3 need no credentials and carry the
cycle. `python3 -m firewatch quota` shows which key is in use, by its last four
characters only.

**Notifications:** by default these go through `osascript`, so macOS attributes them
to *Script Editor* — allow it once under System Settings → Notifications. For
notifications you can click to open Google Maps directly:

    brew install terminal-notifier

FireWatch picks it up automatically on next start.

## SMS alerts

Alerts can also arrive by SMS through [httpSMS](https://httpsms.com), which uses
your own Android phone as the gateway. Each message carries the fire details and a
link to the live map, resolved at send time because the ngrok URL is ephemeral.

    ./firewatch-ctl sms           show settings and what is missing
    ./firewatch-ctl set-sms-key   store the httpSMS API key in the Keychain
    ./firewatch-ctl test-sms      preview + send a sample alert

Three things to set, once:

    # 1. the API key from https://httpsms.com/settings  (prompted, not echoed)
    ./firewatch-ctl set-sms-key

    # 2. the gateway phone, E.164 format
    python3 - <<'EOF'
    from firewatch.config import CFG
    CFG["sms_from"] = "+387XXXXXXXXX"   # the Android phone running httpSMS
    CFG.save()
    EOF

    # 3. one or more recipients
    ./firewatch-ctl sms-add +387XXXXXXXXX
    ./firewatch-ctl sms-add +387YYYYYYYYY      # a friend, etc.
    ./firewatch-ctl sms-remove +387YYYYYYYYY

    ./firewatch-ctl test-sms
    ./firewatch-ctl restart

`sms_to` holds a list. One recipient goes through `/v1/messages/send`; several go
through `/v1/messages/bulk-send`, which takes `to` as an array — so a fan-out is a
single API call, not one per number. A plain or comma-separated string is accepted
too. `FIREWATCH_SMS_TO` overrides the list entirely, for deployments with no
writable config file; with it set, `sms-add` and `sms-remove` refuse rather than
write a file nothing reads.

Each recipient consumes one message from the phone's own throughput budget
(`messages_per_minute`, default 10, max 29 in the httpSMS app), so a handful of
friends is fine; a large list during a busy fire day is not.

The key lives in the macOS Keychain (service `firewatch-httpsms`), never in
`config.json`. `HTTPSMS_API_KEY` overrides it.

**Message text is transliterated to ASCII** — "Zavidovici", not "Zavidovići". One
non-GSM character switches the whole SMS to UCS-2 encoding, which cuts a segment
from 160 characters to 70; folding the diacritics keeps every alert to a single
segment. An alert is four lines — what changed and where, intensity, coordinates,
weather — plus the map link, measured at **at most 153 characters** across every
stored event, every alert kind and the longest settlement name.

Which alerts are texted is `sms_kinds` — by default `new`, `reignited`,
`intensified`, `grew`. `extinguished` is excluded so a fire merely cooling off does
not cost a message. SMS respects the same 25-minute per-event cooldown as the
desktop notifications, and is sent as an independent channel: it still goes out
when the Mac is asleep and nobody sees the notification.

## Commands

    ./firewatch-ctl install | uninstall | start | stop | restart
    ./firewatch-ctl status        service state + current fires
    ./firewatch-ctl logs          follow the log
    ./firewatch-ctl poll          one cycle in the foreground
    ./firewatch-ctl map           rebuild and open the map

Or drive the package directly:

    python3 -m firewatch menubar | poll | watch | status | map | quota | history | test-notify
    python3 -m firewatch status 24h|3d|7d|30d|1y state for one time range
    python3 -m firewatch backfill [days]         deep-fetch history (default 30)

`watch` is the headless loop if you ever want it without the menu bar.

**After editing any code, run `./firewatch-ctl restart`.** The daemon holds the
old modules in memory and keeps overwriting `snapshot.json`, so changes appear to
do nothing until it is restarted.

## Time ranges

**Last 24h · Last 3 days · Last 7 days · Last month · Last year**, available in all
three places:

- **Map** — a segmented control at the top of the panel. Each button carries its own
  event count, so a quiet "Last 24h" still shows the week had four fires. Switching
  refilters the fires, the detection dots and the timeline animation together.
- **Menu bar** — a *Show:* submenu with counts and a checkmark. The choice persists to
  config and the map reopens on the same range.
- **CLI** — `python3 -m firewatch status 30d`, which also prints every range's count.

All five are **rolling windows** measured back from now — 24 h / 72 h / 168 h /
720 h / 8760 h.
Nothing drops out of the shortest range just because the clock passed midnight, and
"Last month" means a rolling 30 days rather than the previous calendar month.
An event belongs to a range if it was *last active* inside it, so a fire that started
six days ago but was burning an hour ago still appears under "Last 24h"; its detection
series is trimmed to the range so the sparkline and timeline stay consistent.

The ranges cost no extra API traffic. The database keeps 400 days (`retention_days`,
deliberately more than the longest, one-year range) and each poll only fetches ~24 h
of overlap, so filtering is a pure read over history already on disk.

**A fresh install has no history**, which makes a 7-day filter misleading rather than
useful. Fix it once with:

    python3 -m firewatch backfill

That is 30 days by default and takes about 5 minutes. Two limits force the work to be
chunked rather than fetched in one shot:

- **FIRMS** caps a single query at 5 days, so the month is stitched from 6 windows
  (48 transactions, still under 1% of the 10-minute quota).
- **The EUMETView WFS** cost grows sharply with the length of the time filter — ~5 s
  for 24 h, ~21 s for 72 h, and a 30-day query simply never returns. Long windows are
  split into 48 h chunks and unioned client-side; a failed chunk is logged and skipped
  rather than losing the whole run.

Backfilled fires that are already out do **not** raise notifications — a `new` alert
means newly burning, not newly discovered.

**Archive depth differs by source**, measured over a 600 km box so that an empty
answer means an empty archive rather than a quiet sky:

| Feed | Depth |
|---|---|
| Sentinel-3 SLSTR | over a year, sparse but real throughout |
| MTG FRP | ~40 days, thinning from about day 30 |
| FIRMS `*_NRT` | ~40 days, then a header-only CSV with HTTP 200 |
| FIRMS `*_SP` | lags ~3 months, then goes back years |

So the far edge of "Last month" is covered by VIIRS/MODIS and Sentinel-3 without the
10-minute geostationary detail, and roughly **40–90 days back FIRMS has nothing to
give** — too old for NRT, too recent for SP. `backfill` clamps to these depths rather
than crawling chunks that can only be empty. The gap fills in as SP catches up, but
only when `backfill` is run again; nothing re-fetches history on its own.

## What it does each cycle

1. **Fetches** each source on its own schedule — MTG every 4 min (2 min while a fire
   is active), FIRMS and Sentinel-3 every 20 min. A failing source never blocks the
   others, and WFS calls retry transient 502/503s.
2. **Clips** every detection to the real municipality outline (OSM relation 2528292,
   731-point polygon) plus a 2 km buffer, so fires just over the border are flagged
   rather than silently dropped. The map draws that buffer as a dashed band around
   the municipality, so you can see where the cutoff falls.
3. **Stores** detections in SQLite keyed by source + position + timestamp. Novelty is
   decided by the database, so a detection re-reported across polls or sensors is only
   ever new once — that is the "compare with the last response" part.
4. **Clusters** detections into *fire events* by single linkage in space and time
   (3.5 km, 8 h). One fire seen by three satellites over twelve overpasses is one row,
   not 35 alerts.
5. **Diffs** against the previous cycle and notifies on:

   | Alert | Trigger |
   |---|---|
   | `new` | a fire never reported before |
   | `intensified` | peak FRP up ≥1.5× and ≥3 MW |
   | `grew` | more detections **and** footprint wider by ≥0.5 km |
   | `corroborated` | a second independent satellite now sees it |
   | `reignited` | a quiet event is producing detections again |
   | `extinguished` | nothing for 4 h (informational, silent) |

   Each event+kind is rate-limited to one notification per 25 minutes.
6. **Enriches** active fires with wind, temperature and humidity from Open-Meteo, and
   names the location from a bundled list of 413 nearby settlements — so you get
   "7.0 km ESE of Kamenica, wind 15 km/h from S → spreads N, high spread risk"
   instead of bare coordinates.
7. **Publishes** `snapshot.json` and rewrites the HTML map.

## The map

`./firewatch-ctl map` — dark Leaflet map with:

- municipality boundary, and OSM / satellite / terrain base layers
- fire circles sized by FRP, coloured by severity, pulsing while active
- individual detections coloured by satellite, fading with age
- a **scrollable timeline ruler** instead of a slider: labelled time ticks, a fixed
  centre playhead, and every detection drawn on the track as a coloured mark, so you
  can see when things happened and scroll straight to them
- it spans the **whole selected range**, not just the period containing detections —
  a quiet week reads as a quiet week
- a **step unit** button cycling minute / hour / day / week. Because a scroller can
  be arbitrarily long it loses no precision at fine units: a month of minutes is
  ~86,000 px of track, exact to the minute. All four units work on every range; the
  *default* is the finest under 400 steps
- **play button** with three speeds, animating by scrolling the track; touching the
  ruler takes over and pauses playback
- keyboard support on the ruler: arrows step one unit, PageUp/Down ten, Home/End the
  ends, with `aria-valuetext` announcing the moment under the playhead
- per-fire panel with an FRP sparkline, weather, spread risk and outbound links
- 🔥 button to zoom straight to what is burning

Every 60 s it pulls fresh data in place — the poller rewrites a sibling
`fire-map-data.js` and the page re-renders from it — so your zoom, pan, selected
range, timeline position and any open popup are preserved. Leaving it open on a
second display gives a live wall view that never jumps.

## How the hosted copy runs

There is no server. `.github/workflows/poll.yml` runs one full cycle per trigger —
fetch, cluster, diff, alert, render — and GitHub Pages serves the result. The SQLite
database is committed back into `state/` at the end of each run, so the repository
itself is the disk that carries state between runs.

The workflow has **no `schedule:`**: its only trigger is `workflow_dispatch`, called
from outside on whatever cadence you choose. GitHub's own cron has a five-minute floor
and drifts 5–30 minutes under load, which is a lot to add to a feed already 25 minutes
behind the fire. The trade is that GitHub cannot tell you the trigger died — watch the
timestamp on the map, not the Actions tab.

The same run publishes this documentation to `/docs/` beside the map, generated from
`docs/*.html` by `deploy/build-site.py`.

Setting this up for a different municipality is
[`docs/firewatch-fork.html`](docs/firewatch-fork.html), end to end.

## Layout

    firewatch/
      config.py    settings (~/.config/firewatch/config.json overrides)
      geo.py       boundary containment, distance/bearing, nearest settlement
      sources.py   MTG + FIRMS + Sentinel-3 adapters
      store.py     SQLite: detections, events, notification state
      events.py    space-time clustering and change detection
      enrich.py    weather / spread risk
      notify.py    terminal-notifier, notify-send or osascript
      sms.py       httpSMS alert text and delivery
      mapgen.py    HTML map renderer
      expose.py    publish the map through a local ngrok agent
      serve.py     poll + serve the map over HTTP, with /health
      poller.py    the loop
      menubar.py   rumps menu bar UI (macOS only, imported lazily)
    data/
      zavidovici.geojson          boundary (OSM rel. 2528292)
      zavidovici-buffer.geojson   the drawn "nearby" band
      settlements.json            413 named places for location descriptions
    deploy/                       Dockerfile, systemd unit, nginx server block
    tests_events.py               clustering + alert-logic tests (python3 tests_events.py)

State lives in `~/Library/Application Support/FireWatch/` — `firewatch.db`,
`snapshot.json`, `fire-map.html`, `firewatch.log` — and Linux follows XDG instead.
Detections are kept for `retention_days` (400), so even the one-year view stays
populated.

## Tuning

Edit `~/.config/firewatch/config.json` (any subset of the defaults in
`firewatch/config.py`), then `./firewatch-ctl restart`. Useful knobs:

- `interval_mtg`, `interval_mtg_active` — poll cadence
- `mtg_min_confidence` (default 30) — raise to cut false positives
- `serve_host` / `serve_port` (default `127.0.0.1:8080`) — the built-in map server
- `nearby_buffer_km` (default 2) — how far outside the border to watch. Changing it
  needs `python3 -m firewatch buffer` afterwards, or the band vanishes from the map.
  Narrowing it only affects new fetches; `python3 -m firewatch reclip --apply` drops
  the history a wider setting had already let in
- `quiet_hours` (default 5) — silence after which a fire is reported quiet rather
  than active. Lowering it sends *more* SMS, not fewer: events drop to quiet between
  satellite overpasses and the next detection returns as `reignited`
- `window_hours` (default 8760) — how far back events are built; the view horizon
- `default_range` (default `3d`) — which range the menu bar and map open on
- `retention_days` (default 400) — database retention; must exceed the largest range
- `notify_cooldown_min` (default 25) — re-alert rate limit

Only settings you actually change are written to `config.json`. Saving the full
resolved config would freeze every default and stop later updates from reaching an
existing install.

## FIRMS quota

The key allows **5000 transactions per 10 minutes** (not per day). An area query
costs 2, so a 4-dataset sweep is 8 — at the default cadence roughly 24/hour, under
0.5% of budget. `python3 -m firewatch quota` shows live usage.

## Notes

- Detections are satellite *thermal anomalies*, not confirmed wildfires. Industrial
  heat, flares and agricultural burning all register. Treat it as a screening signal
  and verify before acting.
- Cloud cover blocks detection entirely on every one of these sensors.
- `fire-detection-zavidovici.sh`, `fire-detection-bih.sh` and
  `zavidovici-granica.gpx` are standalone leftovers kept in the repo; nothing in
  `firewatch/` reads them.

Data: NASA FIRMS (LANCE/ESDIS) · EUMETSAT MTG & Sentinel-3 via EUMETView ·
boundary and settlements © OpenStreetMap contributors (ODbL) · weather Open-Meteo.
