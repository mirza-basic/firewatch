# FireWatch Zavidovići

### 🔥 [Live map](https://mirza-basic.github.io/firewatch) · 📖 [Documentation](https://mirza-basic.github.io/firewatch/docs/)

Near-live wildfire monitoring for **Grad (opština) Zavidovići**, running entirely on
**GitHub Actions and GitHub Pages** — no server, no card, nothing to keep alive at
home. This repository *is* the running instance: `.github/workflows/poll.yml` performs
one full cycle per trigger (fetch → cluster → diff → alert → render), commits the
database back into `state/`, and publishes the map and these docs to Pages.

> **The pages**, readable on the site above or straight from this repository:
> - [`docs/firewatch-fork.html`](docs/firewatch-fork.html) — **run this for your own
>   town**: take a copy, point it at another municipality, and let GitHub Actions poll
>   and GitHub Pages host it, for free. Start here.
> - [`docs/firewatch-documentation.html`](docs/firewatch-documentation.html) — how the
>   system works: data sources, field-level data reference, the processing pipeline,
>   change-detection state machine, rate limits and field notes.
> - [`docs/firewatch-api-reference.html`](docs/firewatch-api-reference.html) — how we
>   call the APIs: every endpoint, exact parameters, real captured request/response
>   traffic, error modes, chunking and quota costs.
> - [`docs/firewatch-field-manual.html`](docs/firewatch-field-manual.html) — day-to-day
>   operation, configuration, and moving it to another municipality.
> - [`docs/firewatch-sms-preview.html`](docs/firewatch-sms-preview.html) — every alert
>   SMS, rendered, with its character budget.
> - [`docs/firewatch-hosting.html`](docs/firewatch-hosting.html) — serving the map from
>   your own host instead: TLS, health checks, Docker.
> - [`docs/firewatch-linux.html`](docs/firewatch-linux.html) — the same headless, on Linux.
> - [`docs/firewatch-macos.html`](docs/firewatch-macos.html) — the optional macOS menu
>   bar app.

## The deployment

Four facts explain the whole thing.

**The workflow has no `schedule:`.** Its only trigger is `workflow_dispatch`, so
something outside GitHub has to ask it to run — a free cron service, a home server,
anything that can make an HTTP request on a timer:

    POST https://api.github.com/repos/<you>/<repo>/actions/workflows/poll.yml/dispatches
    Authorization: Bearer <token>
    Accept: application/vnd.github+json
    Content-Type: application/json

    {"ref":"main"}

GitHub's own cron is the obvious alternative and the reason it is not used: it has a
five-minute floor and drifts 5–30 minutes under load, which is a lot to add to a feed
already 25 minutes behind the fire. An external caller fires when it says it will.

Three things about that call:

- **`ref` is required.** An empty body gets `422 Unprocessable Entity` with no
  explanation of what is missing.
- **Success is `204 No Content` with an empty body.** Do not read "no response" as a
  failure and retry into a queue of duplicate runs.
- **The token needs `Actions: read and write`** — a fine-grained PAT scoped to this one
  repository is enough. Set a reminder before it expires: nothing on GitHub knows the
  workflow was supposed to be called, so an expired token fails silently.

Cadence is yours. Under ten minutes buys nothing — MTG publishes a new slice every ten
minutes with ~25 minutes of latency — so **10–15 minutes** is the sweet spot, putting an
alert 30–45 minutes behind the flame front. Runs are queued, not overlapped
(`concurrency: poll`), and the cycle is idempotent, so a skipped run loses nothing.

The cost of moving the trigger outside is that **GitHub can no longer tell you it
stopped**: if the caller dies there is no failed run and no red cross, just a map that
quietly stops updating. Watch the timestamp on the map, not the Actions tab.

**The repository is the disk.** A runner keeps nothing, so `state/firewatch.db` is
committed back at the end of each cycle. That database carries the `notified` rows, so a
failed push is not cosmetic: the next run rediscovers the same fire and texts about it
again. Hence the rebase-and-retry loop and the `::error::` when all three attempts fail.
The commit is gated on the **detection count** rather than the file's bytes —
`meta.last_cycle` changes every cycle, so `git add -A state` would commit a fresh 188 KB
blob every run forever.

**A Pages deploy replaces the whole site.** `public/` holds the map;
`deploy/build-site.py` adds `docs/` to it just before upload. This is why documentation
publishing cannot be its own workflow: a deploy carrying only the docs would take the map
— the URL in every SMS — off the air, and the next poll would delete the docs. The build
script wraps each `docs/*.html` in a real `<!doctype>`/`<head>` skeleton, because those
files are written in artifact-body form so they can also be published as Claude
artifacts; served unwrapped, every page renders at desktop width on a phone.

**Secrets, not config.** Set these under *Settings → Secrets and variables → Actions*:

| Secret | |
|---|---|
| `FIRMS_MAP_KEY` | free NASA key; without it VIIRS/MODIS is skipped and the other two feeds carry the cycle |
| `HTTPSMS_API_KEY` | optional, for SMS alerts |
| `HTTPSMS_FROM` | optional, the gateway phone number |
| `FIREWATCH_SMS_TO` | optional, comma-separated recipients |

`FIREWATCH_SMS_TO` exists precisely because this repo is public and `sms_to` in
`config.json` would publish real phone numbers. The workflow logs the recipient *count*,
never the numbers — GitHub masks an exact secret value, not the individual numbers inside
a comma-separated one.

### Three repository settings that fail silently when wrong

- **Workflow permissions** must be *Read and write* (*Settings → Actions → General*), or
  the state push fails.
- **Pages source** must be *GitHub Actions*, not a branch.
- **`FIREWATCH_PUBLIC_URL` is hardcoded in both workflow files** — `poll.yml` and
  `test-sms.yml`. Left pointing at someone else's site it is still a valid, working URL,
  so nothing errors and every SMS quietly links to the wrong map.

### Two runner quirks worth knowing

- **GitHub's runners have no IPv6 egress**, and `firms.modaps.eosdis.nasa.gov` is the one
  host here that publishes an AAAA record. Without `FIREWATCH_FORCE_IPV4: "1"` every
  FIRMS fetch dies with `[Errno 101] Network is unreachable` after ~90 s of retries while
  Meteosat and Sentinel-3 look perfectly healthy. Both workflows set it. The same applies
  to `api.httpsms.com`, which resolves through a host with AAAA records.
- **FIRMS sometimes drops the handshake from datacenter IPs.** Intermittent
  `ConnectTimeoutError` on Actions while the same request answers in half a second from a
  laptop — most likely throttling on NASA's side, nothing to fix locally. Neither failure
  breaks a cycle: source failures are isolated, and the run still publishes a map.

### First run

Push, then drive one cycle from the Actions tab before wiring up the external trigger —
one moving part at a time. *Actions → poll → Run workflow*. Then check the Pages URL, and
only then point a cron service at the dispatch endpoint.

Setting all of this up for a different municipality, end to end, is
[`docs/firewatch-fork.html`](docs/firewatch-fork.html).

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

Meteosat and Sentinel-3 need no credentials; FIRMS needs a free key from
<https://firms.modaps.eosdis.nasa.gov/api/map_key/> — it arrives in seconds. Without a
key FireWatch still runs: VIIRS/MODIS is skipped and reported as `[----]` rather than a
failure. On a real fire — 20 August 2026, in forest 18 km south-east of town — MTG
produced 18 detections between 10:50Z and 16:30Z with a visible FRP decline (10.5 → 6.9
MW), where FIRMS gave 7 scattered samples and Sentinel-3 caught the earliest sign of it
at 19:47Z the previous evening.

Honest limit: nothing here is truly real-time. The floor is roughly
**25–35 minutes** behind the flame front, and MTG's coarse pixels only see fires of
roughly 5 MW and up, so FIRMS still matters for catching small ones.

## What it does each cycle

1. **Fetches** all three sources — on a runner every dispatch is a fresh process, so
   every feed is polled each time; the `interval_*` settings only pace the long-running
   local loop. A failing source never blocks the others, and WFS calls retry transient
   502/503s.
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
   | `extinguished` | nothing for `quiet_hours` (5 h) — informational, silent |

   Each event+kind is rate-limited to one notification per 25 minutes.
6. **Enriches** active fires with wind, temperature and humidity from Open-Meteo, and
   names the location from a bundled list of 413 nearby settlements — so you get
   "7.0 km ESE of Kamenica, wind 15 km/h from S → spreads N, high spread risk"
   instead of bare coordinates.
7. **Publishes** `snapshot.json`, rewrites the HTML map, and — on Actions — commits the
   database and uploads the site.

`active` is one claim about the data, recomputed every cycle: the newest detection in the
cluster is younger than `quiet_hours`. Nothing is stored, so no status can get stuck, and
there is no expiry job. It asserts that *a satellite still reports heat*, not that the
fire is out — cloud blocks detection on all three sensors and MTG cannot see below ~5 MW,
so a fire can go quiet on schedule while burning unchanged. That is why `extinguished` is
silent: it is news about the feed, not about the forest.

## SMS alerts

Alerts arrive by SMS through [httpSMS](https://httpsms.com), which uses your own Android
phone as the gateway. On the hosted deployment all three settings are repository secrets
— `HTTPSMS_API_KEY`, `HTTPSMS_FROM` and `FIREWATCH_SMS_TO` — and the map link comes from
`FIREWATCH_PUBLIC_URL`. Nothing about SMS is committed.

    Actions → test-sms → Run workflow

previews the real message and its segment cost and sends nothing; sending is a checkbox,
because a test SMS goes to real phones and costs a real segment.

**Alerts are written in Bosnian** (`sms_language`), and **the text is transliterated to
ASCII** — "Zavidovici", not "Zavidovići". One non-GSM character switches the whole SMS
to UCS-2 encoding, which cuts a segment from 160 characters to 70; folding the diacritics
keeps every alert to a single segment. An alert is four lines — what changed and where,
intensity, coordinates, weather — plus the map link, measured at **at most 153
characters** across every stored event, every alert kind and the longest settlement name.
Adding a line back breaks that, so measure before you do.

Which alerts are texted is `sms_kinds` — by default `new`, `reignited`, `intensified`,
`grew`. `extinguished` is excluded so a fire merely cooling off does not cost a message.
SMS respects the same 25-minute per-event cooldown as the desktop notifications and is an
independent channel: a failed notification never suppresses the text.

One recipient goes through `/v1/messages/send`; several go through
`/v1/messages/bulk-send`, which takes `to` as an array — so a fan-out is a single API
call, not one per number. Each recipient still consumes one message from the phone's own
throughput budget (`messages_per_minute`, default 10, max 29 in the httpSMS app), so a
handful of friends is fine; a large list during a busy fire day is not.

With `FIREWATCH_SMS_TO` set the list is fixed for the life of the process, so the local
`sms-add` / `sms-remove` commands refuse rather than write a file nothing reads. To change
who gets texted on the hosted copy, edit the secret. Locally the list lives in
`config.json` as `sms_to`, re-read on every send so numbers can be added without a
restart, and the key comes from the macOS Keychain (service `firewatch-httpsms`) instead
of the environment.

## The map

Dark Leaflet map, published to Pages every run, with:

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
- an EN/BS toggle; the page opens in **Bosnian**, and a reader's choice is remembered
  per device

Every 60 s the page pulls fresh data in place — the cycle rewrites a sibling
`fire-map-data.js` and the page re-renders from it — so zoom, pan, selected range,
timeline position and any open popup are preserved. Leaving it open on a second display
gives a live wall view that never jumps.

`python3 -m firewatch map` rebuilds and opens the same page locally.

## Time ranges

**Last 24h · Last 3 days · Last 7 days · Last month · Last year**, as a segmented
control at the top of the map panel. Each button carries its own event count, so a quiet
"Last 24h" still shows the week had four fires; switching refilters the fires, the
detection dots and the timeline animation together. On the CLI it is
`python3 -m firewatch status 30d`, which also prints every range's count.

All five are **rolling windows** measured back from now — 24 h / 72 h / 168 h / 720 h /
8760 h. Nothing drops out of the shortest range just because the clock passed midnight,
and "Last month" means a rolling 30 days rather than the previous calendar month. An
event belongs to a range if it was *last active* inside it, so a fire that started six
days ago but was burning an hour ago still appears under "Last 24h"; its detection series
is trimmed to the range so the sparkline and timeline stay consistent.

The ranges cost no extra API traffic. The database keeps 400 days (`retention_days`,
deliberately more than the longest, one-year range) and each poll only fetches ~24 h
of overlap, so filtering is a pure read over history already on disk.

**A fresh fork has no history**, which makes a 7-day filter misleading rather than
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

## Commands

Everything runs from the package, headless, on Linux or macOS with `requests` and
`certifi` alone:

    python3 -m firewatch poll [range]     one cycle + a printed report
    python3 -m firewatch status [range]   last published state
    python3 -m firewatch map              rebuild and open the HTML map
    python3 -m firewatch watch            headless loop, notifications only
    python3 -m firewatch serve [host:port]  poll + serve the map over HTTP, with /health
    python3 -m firewatch backfill [days]  deep history fetch (default 30, ~5 min)
    python3 -m firewatch buffer [km]      rebuild the drawn "nearby" band
    python3 -m firewatch reclip [--apply] drop stored history the clip now rejects
    python3 -m firewatch history [n]      raw detections from SQLite
    python3 -m firewatch quota            FIRMS transaction usage (a free call)
    python3 -m firewatch test-sms | test-notify

`[range]` is `24h | 3d | 7d | 30d | 1y`.

    python3 tests_events.py               clustering + alert-logic tests

## Running it somewhere else

The hosted deployment above is the reference one, but the package is not tied to it.

**Your own host.** `deploy/` holds a Dockerfile, a systemd unit and an nginx server
block. `python3 -m firewatch serve` polls *and* serves in one process on purpose — a host
serving a stale map because the poller was a second unit that died is worse than either.
`/health` (alias `/healthz`) answers 200 only while `snapshot.json` is younger than
`health_max_age_s` (900), and reports `starting`, `stale` and `sources_down` as 503; the
static files stay perfectly serveable long after polling has died, so a monitor that only
fetches `/` would never notice. Details in
[`docs/firewatch-hosting.html`](docs/firewatch-hosting.html) and
[`docs/firewatch-linux.html`](docs/firewatch-linux.html).

**macOS menu bar app.** `./firewatch-ctl install` installs a `launchd` login agent and
starts a `rumps` menu bar app that owns the same poller — look for 🌲 in the menu bar:

    🌲              nothing burning
    🔥 1! · 11MW    one active fire, moderate, 11 MW peak radiative power

    ./firewatch-ctl install | uninstall | start | stop | restart
    ./firewatch-ctl status | logs | poll | map
    ./firewatch-ctl set-firms-key | set-sms-key | sms | sms-add | test-sms

On macOS credentials belong in the **Keychain, not the environment**: `launchd` does not
read a shell profile, so an `export FIRMS_MAP_KEY` in `~/.zshrc` is visible to a terminal
`poll` and invisible to the running agent. Notifications go through `osascript` (macOS
attributes them to *Script Editor* — allow it once under System Settings →
Notifications); `brew install terminal-notifier` makes them clickable links to Google
Maps. **After editing any code, `./firewatch-ctl restart`** — the agent holds the old
modules in memory and keeps overwriting `snapshot.json`, so changes appear to do nothing.
Full instructions in [`docs/firewatch-macos.html`](docs/firewatch-macos.html).

`menubar.py` is the only macOS-specific module and `__main__` imports it lazily, so every
other command runs anywhere.

## Tuning

On Actions, `state/config/config.json` is written by the workflow (only
`auto_expose: false`, because there is no ngrok agent on a runner) — commit further
overrides there. Locally the file is `~/.config/firewatch/config.json`, and a restart
picks it up. Any subset of the defaults in `firewatch/config.py`:

- `mtg_min_confidence` (default 30) — raise to cut false positives
- `interval_mtg`, `interval_mtg_active` — poll cadence for the long-running loop only
- `nearby_buffer_km` (default 2) — how far outside the border to watch. Changing it
  needs `python3 -m firewatch buffer` afterwards, or the band vanishes from the map.
  Narrowing it only affects new fetches; `python3 -m firewatch reclip --apply` drops
  the history a wider setting had already let in
- `quiet_hours` (default 5) — silence after which a fire is reported quiet rather
  than active. Lowering it sends *more* SMS, not fewer: events drop to quiet between
  satellite overpasses and the next detection returns as `reignited`. It also has a hard
  floor at the slowest feed's delivery latency — Sentinel-3 lands 3–4 h after
  acquisition, so under 4 h a fire only S3 had seen arrived already quiet and the `new`
  alert was dropped entirely
- `window_hours` (default 8760) — how far back events are built; the view horizon
- `retention_days` (default 400) — database retention; must exceed the largest range
- `default_range` (default `3d`) — which range the map opens on
- `notify_cooldown_min` (default 25) — re-alert rate limit
- `serve_host` / `serve_port` (default `127.0.0.1:8080`) — the built-in map server

Only settings you actually change are written. Saving the full resolved config would
freeze every default and stop later updates from reaching an existing install.

## FIRMS quota

The key allows **5000 transactions per 10 minutes** (not per day). An area query
costs 2, so a 4-dataset sweep is 8 — at a 15-minute cadence roughly 32/hour, well under
1% of budget. `python3 -m firewatch quota` shows live usage and reports the key by its
last four characters only.

## Layout

    .github/workflows/
      poll.yml     the cycle: fetch → cluster → alert → render → publish
      test-sms.yml manual SMS delivery check; previews by default
    firewatch/
      config.py    settings, credential resolution, log redaction
      geo.py       boundary containment, distance/bearing, nearest settlement
      sources.py   MTG + FIRMS + Sentinel-3 adapters
      store.py     SQLite: detections, events, notification state
      events.py    space-time clustering and change detection
      enrich.py    weather / spread risk
      notify.py    terminal-notifier, notify-send or osascript
      sms.py       httpSMS alert text and delivery
      mapgen.py    HTML map renderer
      serve.py     poll + serve the map over HTTP, with /health
      expose.py    publish a local map through an ngrok agent
      poller.py    the loop
      menubar.py   rumps menu bar UI (macOS only, imported lazily)
    data/
      zavidovici.geojson          boundary (OSM rel. 2528292)
      zavidovici-buffer.geojson   the drawn "nearby" band
      settlements.json            413 named places for location descriptions
    deploy/
      build-site.py               wraps docs/*.html and adds them to the Pages upload
      Dockerfile, systemd unit, nginx server block
    docs/                         the published documentation pages
    state/                        the committed database — this is the disk
    public/                       the built site (map + data file), rewritten each run
    tests_events.py               clustering + alert-logic tests

`data/*` are build-time artifacts committed to the repo: Nominatim and Overpass are never
called at runtime. Locally, state lives in `~/Library/Application Support/FireWatch/` on
macOS and follows XDG on Linux; `FIREWATCH_DATA_DIR`, `FIREWATCH_PUBLIC_DIR` and
`FIREWATCH_CONFIG_DIR` override all of it, which is what the workflow and containers use.

## Notes

- Detections are satellite *thermal anomalies*, not confirmed wildfires. Industrial
  heat, flares and agricultural burning all register. Treat it as a screening signal
  and verify before acting.
- Cloud cover blocks detection entirely on every one of these sensors.
- **No credential is committed to this repository.** The FIRMS key resolves through
  `config.firms_key()`: `FIRMS_MAP_KEY` in the environment, then the macOS Keychain, then
  `firms_map_key` in `config.json`. There is no fallback and no bundled key — one shared
  key would mean one shared quota for every clone, and a credential in git history for
  good. The key travels in the URL *path*, so `config.RedactingFormatter` scrubs it out
  of every log line.
- `fire-detection-zavidovici.sh`, `fire-detection-bih.sh` and
  `zavidovici-granica.gpx` are standalone leftovers kept in the repo; nothing in
  `firewatch/` reads them.

Data: NASA FIRMS (LANCE/ESDIS) · EUMETSAT MTG & Sentinel-3 via EUMETView ·
boundary and settlements © OpenStreetMap contributors (ODbL) · weather Open-Meteo.
