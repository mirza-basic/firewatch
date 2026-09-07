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
python3 -m firewatch imagery [--force]   # render the newest Sentinel-2 scene
python3 -m firewatch set-cdse-key       # store a Copernicus client in the Keychain
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
map`) and looking, and treat anything in the generated page as unguarded — a
mistake of the `L.Polygon.getCenter()` kind (landmine 14) reaches the reader with
nothing in between.

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
imagery.py   newest Sentinel-2 scene; renders one PNG per *new* scene only
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

**`active` is one line, and it is a claim about the data.** `events.py:173` is the
whole of it — `"active" if (now - last_ts) <= quiet_after else "quiet"`, where
`last_ts` is the newest detection *in the cluster* and `quiet_after` is `quiet_hours`
(5). It is recomputed every cycle from `now`, so no status is stored and none can get
stuck; an event goes quiet by not being re-detected, and there is no expiry job. What
it asserts is that *a satellite still reports heat*, not that the fire is burning:
cloud blocks detection completely on all three sensors, and MTG cannot see below
roughly 5 MW, so a fire can go `quiet` on schedule while burning unchanged. That is
why `extinguished` is informational and silent — it is news about the feed, not about
the forest.

**The eye button detaches the three marker groups, it does not clear them.**
`drawEvents` and `drawDets` rebuild their contents on every refresh, zoom and
range change, so clearing would last until the next tick; a `LayerGroup` removed
from the map keeps accepting children that are simply not drawn, which is why the
hidden state survives without any draw path knowing about it. The one thing that
does need to know is the popup restore in `applyData` - `openPopup()` on a
detached marker is a no-op, so it is gated on the toggle.

**Hidden is not a preference and is deliberately not persisted.** It resets on
load and whenever `applyData` accepts fresh data, because a cycle that brings a
new fire must not deliver it invisibly. The reset sits *after* the
`generated_at` guard, so a quiet cycle - which is most of them, once a minute -
leaves the reader's view alone. Persisting it in `localStorage` next to
`fw_lang` is the obvious move and the wrong one: it would let someone hide the
markers, close the tab, and reopen a map that never shows a fire again.

The map opens in **Bosnian**, unconditionally — there is no `navigator.language`
sniff. A reader's own choice wins: the EN/BS toggle writes `fw_lang` to
`localStorage` and that is checked first, so switching to English is remembered per
device.

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
   a `uid` carrying the dataset name stores one pixel as two detections — inflating
   `n_det`, footprints and "grew" alerts wherever the live feed and the archive
   overlap. `_firms_sensor()` strips the suffix for both `uid` and `sensor`; the full
   name stays in `raw.dataset`, and `store.UID_SCHEME` rewrites rows whose uid was
   built under a different scheme. Note `VIIRS_NOAA21` has no SP twin — HTTP 400
   "Invalid source" — so the archive list is one satellite shorter.
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
17. **Lowering `quiet_hours` *increases* SMS volume.** The intuition is that a
    shorter window means less noise; the opposite happens. A short window pushes
    events into `quiet` during the ordinary gap between polar overpasses, and the
    next detection then arrives as `reignited` — which is in `sms_kinds`, unlike the
    silent `extinguished`. One long fire under a 1 h window becomes a day-long
    stutter of extinguished/reignited pairs, one text per pair.

    **It also has a hard floor: the slowest feed's delivery latency.** Sentinel-3
    arrives 3-4 h after acquisition (S3A measured 4.13 h), so under the old 4 h
    setting a fire only S3 had seen landed already `quiet` and the `new` gate — which
    requires `status == "active"` — dropped the alert entirely. The fire was then
    announced hours later by FIRMS, as `reignited`, which told the reader it was
    burning *again* when it had never been reported once. Replaying all stored
    history at 4 vs 5 moves exactly three alerts, every one of them a fire Sentinel-3
    reported first, 2.4-3.7 h earlier and from `reignited` to `new`, for one fewer
    SMS. Below a feed's latency, that feed can only ever alert when it lands on the
    fast half of its distribution.

## Satellite imagery on the map

The three basemaps are all archival - Esri World Imagery is months to years old -
so none of them can show a fire that is burning. Three tiers of real imagery sit
in the layer control, and the ordering is counter-intuitive: **the fast layer is
the coarse one.**

| Tier | Source | Key? | Resolution here | Cadence |
|---|---|---|---|---|
| Meteosat MTG | `view.eumetsat.int/geoserver/wms` | none | ~1.7 × 1.3 km | 10 min, ~25 min behind |
| VIIRS / HLS | `gibs.earthdata.nasa.gov` WMTS | none | 250 m / 30 m | daily, 4-5 h behind |
| Sentinel-2 | CDSE Sentinel Hub | **yes** | ~11 m | ~2-3 days here |

The first two are fetched by the reader's browser from keyless services and add
no dependency. Note the WMS host is the *same GeoServer* `sources.py` already
polls for FRP features - the imagery was one query away the whole time.

**What you can actually see is smoke, not flame.** Measured against the real
2026-09-05 event at 44.354/18.225 (FIRMS 3.7 + 5.3 MW, Sentinel-3 15.4 + 16.5 MW):
the VIIRS 250 m true-colour frame showed a clear grey plume drifting SE, while
the VIIRS SWIR fire-band combo - the one documented to saturate red on sub-pixel
flame - showed *nothing*. At this municipality's fire sizes the plume is the
signal. `mtg_fd:rgb_firetemperature` is the exception: it rendered two
unmistakable red hotspots at 20:40Z that evening.

Zavidovići sits at a ~54° viewing zenith from 0°E, so `1/cos θ ≈ 1.7` inflates
every MTG figure: FDHSI solar 1 km → ~1.7 × 1.3 km, thermal 2 km → ~3.4 × 2.6 km.
That last number is an independent justification for `cluster_radius_km = 3.5`.

**Sentinel-2 is rendered here and published as a picture, never as a tile layer**
(`imagery.py`). Sentinel Hub takes its credential as an *instance id in the URL
path*, so a browser-side layer would write a quota-bearing credential into
`fire-map.html` - a file this repo publishes to GitHub Pages and through ngrok.
There is no keyless form; a made-up instance id answers 400. Server-side
rendering is also the only shape that works on Pages, where there is nothing to
proxy through. The catalogue half *is* keyless (CDSE OData), which is why the
"is there a new scene?" question is asked every cycle and the pixels are bought
only when the answer changes.

**There is no usable Sentinel-3 imagery layer**, which is worth writing down
because the capabilities document strongly implies otherwise. Checked
2026-09-06: GIBS carries **zero** Sentinel-3 imagery (only orbit tracks).
`copernicus:sentinel3a/b_olci_l1_rgb_fullres` return a blank frame - mean 255,
standard deviation 0 - at every time tried, and their advertised abstract is the
*Meteosat FCI* one ("cannot be made directly from FCI because it lacks a Green
band") carrying a 10-minute cadence that no polar orbiter has. Only
`copernicus:daily_sentinel3ab_olci_l1_rgb_fulres` has real pixels, and measuring
its repeated scanlines puts it at **~650 m per source pixel**, so it is strictly
worse than the VIIRS 250 m layer already on the map and, being a daily composite,
adds no time diversity either. `copernicus:sentinel_slstr_frp` answers
**`LayerNotDefined`** on every endpoint despite being advertised, and
`copernicus:sentinel3a_slstr_level2_frp` returns near-empty tiles with a time
dimension stuck at 2026-06-11. Even a working FRP layer would only redraw, as a
picture, the Sentinel-3 detections the map already plots as real points with FRP
values from the WFS feed. **Treat this server's capabilities as advertising, not
as fact - measure the pixels.**

A keyless 10 m route does exist if the credential ever becomes unwelcome:
Element84 Earth Search STAC plus the unsigned, non-requester-pays
`sentinel-cogs` bucket. It costs a real imaging dependency - parsing TIFF tile
offsets and inflating DEFLATE - which is why it was not taken.

## Landmines: imagery

18. **EUMETView answers a bad `time` with HTTP 200 and `text/xml`.** An
    out-of-archive timestamp returns `ServiceException code="InvalidDimensionValue"`
    in ~470 bytes; Leaflet draws nothing and the reader sees a blank map with no
    error anywhere. Third instance of this project's 200-means-nothing-wrong
    trap, after landmines 3 and 5.
19. **`rgb_truecolour` and `vis06_hrfi` need *two* separate guards, and
    conflating them gets both wrong.** They have a daily publication hole from
    00:00Z to 01:50Z, first frame at 02:00Z, reproduced on two separate days -
    while capabilities advertise an unbroken `PT10M` series. Ask inside it and
    you get the landmine-18 exception. **That hole is not darkness**: the same
    layers answer 200 at 20:00Z and 22:00Z with the sun 27-38° below the
    horizon. So `dayFrom` (minutes into the UTC day) handles availability.

    But those night frames are *useless*, and in two different disguises:
    `rgb_truecolour` returns a **fully transparent** tile, so the reader sees
    the basemap and assumes the layer never loaded, while `vis06_hrfi` returns
    an **opaque black rectangle**. Neither is an error. Hence `needsSun` and a
    solar-elevation test, measured against pixel brightness: at +7.5° the frame
    reads 31/255 mean, at +2.2° it is 1.0, below the horizon it is blank - so
    the cutoff is `SUN_MIN_DEG = 3`, above the horizon rather than on it.

    **Only the geostationary visible layers carry `needsSun`.** The GIBS layers
    are daily composites built from a daytime overpass, so they are perfectly
    readable at midnight; flagging them would be wrong. And the sun test must
    run on the *live* branch too - the default view is the live edge, so a
    layer useless right now is the first one the reader meets.
20. **Never send `exceptions=`.** Any value - `se_xml`, `se_inimage`,
    `se_blank` - makes their proxy answer **502 with an empty body**, so the
    usual OGC trick of requesting a transparent error tile makes it worse.
21. **Omitting `time` returns the newest frame** - byte-identical to
    `time=current` - and an off-grid time snaps to nearest (`12:03` returned the
    `12:00` bytes). So "latest" needs no GetCapabilities, which matters: that
    document is 282 KB. Do **not** derive "latest" from the capabilities
    `default` either - it read 14:00Z at 14:30 wall clock while 15:30Z answered
    at 15:37.
22. **A GIBS tile past its own `Level` is HTTP 400 XML, not a blank tile.**
    `maxNativeZoom` is load-bearing, not an optimisation: without it the layer
    vanishes the moment the reader zooms past z9.
23. **Sub-daily `TIME` is silently ignored** by GIBS and Worldview daily
    composites - `2026-09-05`, `…T11:49Z` and `…T20:49Z` return identical bytes.
    Only the Meteosat tier is genuinely sub-daily.
24. **The Sentinel-2 render is requested in EPSG:3857, not 4326.**
    `L.imageOverlay` stretches its image linearly between two *projected*
    corners, so an equirectangular image drifts vertically against the basemap -
    a few hundred metres here, enough to put a fire on the wrong side of a ridge.
25. **`imagery.py` writes to `SUPPORT_DIR`, never straight to `PUBLIC_DIR`.**
    `PUBLIC_DIR` existing is exactly what `sync_public` reads as "the user asked
    for this to be published", so creating it to hold a picture would quietly
    start publishing the map. `sync_public` copies a **fixed file list**, so the
    PNG had to be added to it - miss that and the local `file://` map looks
    perfect while the published one, the URL in every SMS, shows an empty layer.
26. **An empty GIBS day is an HTTP error per tile, not a blank tile.** HLS
    revisits every ~5 days and lands days later, so "today" is normally empty
    and every tile 400s. There is no way to know in advance, so the layer
    listens for Leaflet's `tileerror` and reports it; `tileload` clears the
    flag. The layers are also seeded with today's date rather than a
    placeholder, because Leaflet starts fetching the moment a layer is added -
    before the handler sets the real date - and a placeholder's errors would
    land afterwards and raise a false "no scene" note.
27. **Imagery exclusivity is per *group*, in code, not by Leaflet.** Base
    layers get exclusivity and overlays get none, so `overlayadd` does it by
    hand - but only within a group. `grp:"vis"` layers are alternatives;
    `grp:"hot"` (Fire Temperature) stacks on top of any one of them, and
    Sentinel-2 competes for the `vis` slot like any other visible layer rather
    than clearing the map. Unavailable moments drop opacity to 0 rather than
    removing the layer, because removing it fires `overlayremove`, unticks the
    reader's own checkbox and loses the selection.
28. **The thermal layer composites with `mix-blend-mode:screen`, and that is
    load-bearing rather than decorative.** Fire Temperature RGB is near-black
    everywhere except the fire - mean 3.4 of 255 over this municipality - so
    screen discards its background entirely and leaves only the hot pixels on
    top of the visible layer. Two useful consequences: **screen can only
    lighten**, so the blended layer physically cannot obscure the imagery
    underneath, which is exactly what stacking two rasters at 85% opacity does;
    and a blended layer must run at **opacity 1**, because dimming it only dims
    the hotspots - the background it contributes is already black. It lives in
    its own pane (`imageryHot`, z-index 260) above `imagery` (250). A browser
    that ignored `mix-blend-mode` would render it opaquely: ugly, not wrong.

## Landmine: FIRMS from a CI runner

Two distinct failures, both seen on GitHub Actions, both looking like "FIRMS is
down" and neither being that:

1. `[Errno 101] Network is unreachable`, instantly. IPv6 with no route - see below.
2. `ConnectTimeoutError ... connect timeout=90`, after 90 s per dataset. The
   handshake is dropped. Intermittent from GitHub's IPs: one run succeeds, the next
   times out, while the same request answers in 0.5 s from a laptop. Most likely
   datacenter-IP throttling on NASA's side; nothing to fix locally.

`connect_timeout` (10) is separate from `http_timeout` (90) because they answer
different questions: a reachable host completes a TCP handshake in well under a
second, while a 48 h WFS chunk legitimately takes ~21 s to read. Applying 90 s to
both meant six minutes to discover FIRMS was unreachable - and `fetch_firms` now
`break`s out of the dataset loop on a connect failure, because the other three
cannot succeed if the first could not connect.

None of this breaks a cycle: source failures are isolated, Meteosat and Sentinel-3
carry it, and the run still publishes a map. The cost of getting it wrong is a slow
cycle, not a dead one.

## Landmine: IPv6 without a route

`FIREWATCH_FORCE_IPV4=1` makes urllib3 resolve A records only. Of the four hosts
this talks to, **only `firms.modaps.eosdis.nasa.gov` publishes an AAAA record** -
`view.eumetsat.int` (MTG and Sentinel-3) and `api.open-meteo.com` are IPv4-only. So
on a network with no IPv6 egress, FIRMS is the *only* feed that breaks, with
`[Errno 101] Network is unreachable` after ~90 s of retries per dataset, while
everything else looks healthy. GitHub Actions runners are exactly that network.

It is opt-in on purpose: a working dual-stack network handles the fallback itself,
and forcing IPv4 would break an IPv6-only host. The failure is easy to misread as a
FIRMS outage or a bad key, so check `dig AAAA` before believing either.

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

Two things it must keep doing:

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
  `firewatch-httpsms`), never `config.json`. It must be the **account** key from
  <https://httpsms.com/settings>, not the *phone* API key from
  <https://httpsms.com/phone-api-keys> that the Android app signs in with (QR code) -
  that one is scoped to the app's own traffic and cannot call `/v1/messages/send`. The
  wrong key fails late and quietly: `ready()` only checks that a key is *present*, so
  `sms-status` says `found` and the first real fire is what tells you. The gateway app
  is an APK (<https://apk.httpsms.com/HttpSms.apk>), not a Play Store install, and a 200
  from httpSMS means *accepted*, not sent - the phone still has to be awake, online and
  not battery-optimised into the background.
- **Alerts are written in Bosnian** (`sms_language`, "bs" or "en"). `SMS_TEXT`
  holds both wordings, `COMPASS_BS` translates the bearing, and `_place()` rebuilds
  the location from `place_parts` rather than reusing the English `place` string —
  the genitive after "od" is why ("Kamenica" → "od Kamenice"), the same rule the map
  applies client-side.
- **An alert is four lines and one segment.** The detection count with its source
  list, the distance from town, the `IZVAN OPĆINE` marker and the map link were all
  dropped: an active fire always carries weather, and with those lines every such
  alert cost two segments. What survives is what you act on — what changed, where,
  how hot, and coordinates that work with no signal. Verified across every stored
  event × every sending kind × the longest settlement name × absurd values
  (1234.5 MW, 99.9 km, 100% RH): max 158 characters, always one segment - two
  characters of headroom. Adding a line back will break that, so measure before you
  do, and `sms.worst_case()` is how rather than by hand: it builds that same worst
  alert against the live settlement list and the live map URL, and `sms-status` and
  the `test-sms` workflow both print it with the headroom left. The URL is a line of
  the message, so a longer settlement name or a longer `FIREWATCH_PUBLIC_URL` spends
  that headroom without anyone deciding to. English wording is written out in
  `SMS_TEXT` rather than falling back to raw keys, and fits with a character less to
  spare than Bosnian.
- **Message text must stay ASCII.** `ascii_only()` folds Bosnian diacritics
  because one non-GSM character switches the whole message to UCS-2, dropping a
  segment from 160 characters to 70. `segments()` reports the real cost. `Đ` is the
  awkward one: it needs `DJ` inside an uppercase word and `Dj` otherwise, so a flat
  mapping produced `POTVRDjEN` and would mangle any place name written in caps.
- The map link is resolved at send time. `FIREWATCH_PUBLIC_URL` wins if set;
  otherwise `expose.find_tunnel()` asks the local ngrok agent — the free URL changes
  on every agent restart, so it must not be cached. On a host there is no agent, so
  without the variable a hosted alert silently loses its map link, which is the line
  that matters most in a fire SMS. Setting it also stops the poller calling
  `expose.ensure()` at all.
- **The sender is deploy config; the recipients are live state.** `sms.sender()`
  reads `HTTPSMS_FROM` then `sms_from` - it belongs to the same httpSMS account as
  the key, is fixed for the life of a deployment, and so belongs in the environment.
  `sms.recipients()` deliberately does **not** use the import-time `CFG` snapshot: it
  re-reads `config.json` on every call, because numbers get added while the service
  is running and a long-lived poller would otherwise keep texting the old list until
  somebody restarted it. `FIREWATCH_SMS_TO` (comma- or semicolon-separated) overrides
  that entirely, for deployments with no writable config file and for public repos,
  where a committed phone number would be published. The trade is one-way: with it set
  the list is fixed for the life of the process, so `sms-add`/`sms-remove` **refuse**
  rather than write a file nothing reads - `_env_overrides_recipients()` in `__main__`
  is that guard, and `sms-status` prints which source is live.
  `Config.save()` is atomic (`os.replace`) for the same reason - two processes touch
  that file, and a partial read would drop an alert.
- `sms_to` is a **list**; `recipients()` also accepts a plain or comma-separated
  string. One recipient uses `/messages/send`; two or more use
  `/messages/bulk-send` with `to` as an array — one call for the fan-out.
- Inert until a sender, a recipient list and the key are all present; `sms.ready()`
  returns the specific reason it is not usable, including non-E.164 numbers.

## The GitHub deployment

This repository *is* a running instance, not just the source of one. `.github/workflows/`
holds the whole production deployment, and it is the one described in
`docs/firewatch-fork.html`.

```
poll.yml     the cycle: fetch → cluster → alert → render → publish
test-sms.yml manual SMS delivery check; previews by default, sending is a checkbox
```

Four facts drive everything about it:

**There is no `schedule:`.** `poll.yml` is `workflow_dispatch` only, called from outside
by `POST /repos/{owner}/{repo}/actions/workflows/poll.yml/dispatches` with
`{"ref":"main"}`. GitHub's cron has a five-minute floor and drifts 5-30 minutes under
load, which is a lot to add to a feed already 25 minutes behind the fire. The cost of
moving the trigger outside is that **GitHub can no longer tell you it stopped**: if the
caller dies, there is no failed run and no red cross, just a map that quietly stops
updating. There is also nothing left to keep alive - the 60-day disable rule applies only
to scheduled workflows - which is why the old `state/.heartbeat` commit is gone.

**The repository is the disk.** A runner keeps nothing, so `state/firewatch.db` is
committed back at the end of each cycle. That database carries the `notified` rows, so a
failed push is not a cosmetic failure: the next run re-discovers the same fire and texts
about it again. Hence the rebase-and-retry loop, and the `::error::` if all three attempts
fail. The commit is gated on the **detection count**, not on the file's bytes -
`meta.last_cycle` changes every cycle, so `git add -A state` would commit a fresh 188 KB
blob every run forever.

**A Pages deploy replaces the whole site.** `public/` holds the map, and
`deploy/build-site.py` adds `docs/` to it just before upload. This is why documentation
publishing cannot be its own workflow: a deploy carrying only the docs would take the map
- the URL in every SMS - off the air, and the next poll would delete the docs. The build
script wraps each `docs/*.html` in a real `<!doctype>`/`<head>` skeleton, because those
files are written in artifact-body form (no doctype, no viewport) so they can also be
published as Claude artifacts. Serving them unwrapped renders every page at desktop width
on a phone.

**Secrets, not config.** `FIRMS_MAP_KEY`, `HTTPSMS_API_KEY`, `HTTPSMS_FROM` and
`FIREWATCH_SMS_TO` come from repository secrets. The last one exists precisely because
this repo is public and `sms_to` in `config.json` would publish real phone numbers; the
workflow logs the recipient *count*, never the numbers, since GitHub masks an exact secret
value and not the individual numbers inside a comma-separated one.

Three settings in the repo itself, all of which fail silently when wrong: **Workflow
permissions** must be read/write or the state push fails; **Pages source** must be
*GitHub Actions* and not a branch; and `FIREWATCH_PUBLIC_URL` is hardcoded in **both**
workflow files - left pointing at someone else's site it is still a valid, working URL,
so every SMS quietly links to the wrong map.

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
  `events`, `notified`, `meta`), `snapshot.json`, `fire-map.html`, `fire-s2.png`,
  `firewatch.log`, `stdout/stderr.log`. The last rendered Sentinel-2 scene date
  lives in `meta` under `imagery_s2`, which is what gates a re-render.
- `~/Library/Caches/FireWatch/public/` — the only directory ever exposed publicly.
  Rewritten from the snapshot every cycle, so losing it costs nothing. It is *not*
  under Application Support, for the reason in landmine 12.
- `~/.config/firewatch/config.json` — user overrides only; anything absent falls back
  to `DEFAULTS` in `config.py`. Restart after editing.
- **No credential is committed to this repo.** The FIRMS map key resolves through
  `config.firms_key()`, which returns `(key, source)` or `(None, "not set")` and
  tries, in order: `FIRMS_MAP_KEY` in the environment, the macOS Keychain (service
  `firewatch-firms`, set by `set-firms-key`), then `firms_map_key` in `config.json`.
  There is no fallback, and no key ships with the repo: one shared key would mean
  one shared 5000-per-10-minutes limit for every clone, and a credential in git
  history for good.
- **No key is a supported state, not a failure.** `fetch_firms` raises
  `sources.NoCredentials`, the poller records `configured: False` and logs at *info*,
  the CLI prints `[----]` rather than `[FAIL]`, and `/health` excludes the source
  instead of counting it down - otherwise an install deliberately running without
  FIRMS would report 503 for ever. Meteosat and Sentinel-3 need no credentials, so
  two of three feeds keep working.
- **The Copernicus client resolves the same way**, through `cdse_credentials()`:
  `CDSE_CLIENT` (`"<id>:<secret>"`) or `CDSE_CLIENT_ID`/`CDSE_CLIENT_SECRET` in the
  environment, then the Keychain (service `firewatch-cdse`, set by `set-cdse-key`).
  Never `config.json`. Both halves live in **one** Keychain item because `security`
  holds one password per service, and splitting them would let an install pair an id
  with the wrong secret - which fails as an opaque 401 at render time, long after the
  mistake. `secrets()` adds the raw item *and* each half separately: the combined
  string redacts the log line that carries it, but a traceback from the token POST
  carries the **secret alone**, and only the split covers that.
- `quota` prints the last four characters and the source, never the key.
  `config.keychain_secret()` is the one Keychain reader; `sms.api_key()` uses it too.
- **On macOS the key belongs in the Keychain, not the environment.** `launchd` does not
  read a shell profile, and the generated plist injects only `SSL_CERT_FILE`,
  `REQUESTS_CA_BUNDLE` and `PYTHONUNBUFFERED` - so an `export FIRMS_MAP_KEY` in
  `~/.zshrc` is visible to a terminal `poll` and invisible to the running agent. Both
  keys resolve the same way, so the agent silently loses FIRMS *and* SMS while a
  hand-run poll looks perfect. Environment variables are for Linux and hosts, where a
  service manager passes them in on purpose.
- **The FIRMS key travels in the URL *path***, so any `requests` exception carries the
  whole query - key included - and a bare `log.warning("firms %s: %s", ds, exc)`
  writes it straight to `firewatch.log`. `config.RedactingFormatter` scrubs every
  known credential out of every log line. It is a **formatter, not a
  `logging.Filter`** - filters run before formatting, so the first handler emits the
  traceback while `record.exc_text` is still empty, then caches the raw text for the
  second handler to redact. With the file handler first, that redacts the console and
  writes the key to disk: exactly backwards.

Notifications go through `osascript` (attributed to *Script Editor*, click does
nothing) unless `terminal-notifier` is installed, in which case notifications become
clickable links to Google Maps. `notify.backend()` reports which is active.

## Branches

`main` is this deployment and names Zavidovići in code: `config.py` hardcodes the three
`data/` filenames and `TOWN_LAT`/`TOWN_LON`, and both workflow files carry a literal
`FIREWATCH_PUBLIC_URL`.

**`fork-template` is the branch anyone adapting this should start from**, and it is where
work on adaptability belongs. It adds `place.py` and `setup.py`, moves every place-specific
string into `data/place.json`, derives the Pages URL from `github.repository`, and carries
`FORK.md`. Porting a fork-template change back to `main` is usually wrong — main is
deliberately the concrete instance, not the template.

**A boundary can arrive as several rings, and `geo.py` is the only thing that knows it.**
Nominatim returns a MultiPolygon for anything with an enclave, an exclave or an island - a
country, some counties - and `geo.boundary_rings()` flattens every ring of every part,
holes included, because the even-odd containment test toggles per crossing. Read one ring
and the rest of the place silently stops existing. The older `boundary_ring()` did exactly
that (`coordinates[0]`, which for a MultiPolygon is a whole polygon) and the ValueError it
raised came from inside `bbox_padded()` - so all three feeds reported `[FAIL]`, per-source
isolation swallowed it, the cycle exited 0, and the map rendered correctly and stayed empty
for ever. Fixed on both branches 2026-09-05, found against `Bosna i Hercegovina` (OSM
relation 2528142, 2 parts). A municipality is one polygon, so nothing at that scale ever
exercised it.

## Repo conventions

- `firewatch/` is the system. `fire-detection-zavidovici.sh` and
  `fire-detection-bih.sh` are standalone shell leftovers and
  `fire-detection-requraments` the hand-written spec — read them for context, leave
  them alone rather than extending them.
- **Both halves of the layer control are built by a function** - `bases()` and
  `overlays()`. The control is destroyed and rebuilt on every language switch, and
  the base list used to be written out at each of the two call sites, which is one
  edit away from a base layer that vanishes the moment the reader presses BS.
  `lNone` is an empty `L.layerGroup()`: a base layer and not a checkbox, because
  Leaflet's base layers are a radio group, so there is no way to untick your way
  to no basemap.
- **A new map label needs both `I18N.en` and `I18N.bs`** or `t()` silently falls
  back to the raw key and the reader sees `imFire` in the layer control. The layer
  control itself is now built from one `overlays()` function, so it survives the
  language-switch rebuild without a second edit - but the dictionaries are still two.
- `docs/*.html` are self-contained documentation pages, also published as Claude
  artifacts. Update them when behaviour changes — particularly the "field notes" and
  "landmines" content, which is the part that goes stale invisibly.
- Code style follows the existing modules: `from __future__ import annotations`,
  module-level docstring explaining *why*, comments reserved for non-obvious
  decisions and measured facts rather than restating the code.
- `.gitignore` covers `__pycache__/`, the generated plist, and everything under
  `state/` that a cycle rewrites, so `git status` stays quiet after a run. Keep
  `__pycache__` out in particular: a byte-compiled module bakes in whatever default
  was in the source, credentials included.
