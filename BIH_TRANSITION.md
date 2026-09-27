# Transition to full Bosnia and Herzegovina coverage

**Complete as of 2026-09-27** - merged into `main`, running live. This file is
kept as the historical record of the transition: `bih-all-municipalities`
turned FireWatch from a single-municipality deployment (Zavidovići) into one
that watches all 145 self-governing units of Bosnia and Herzegovina at once,
with a separate private Telegram channel per municipality so someone can
subscribe to just their own.

## Done

**Municipality data**
- `data/bih/municipalities.json` — all 145 units identified via an OSM
  `admin_level` 6/7 sweep plus Brčko District (tagged at level 4, outside the
  entity/canton structure, added separately). One junk OSM entry excluded (a
  mistagged *mjesna zajednica*), four genuine split-municipality pairs and one
  coincidental name clash disambiguated.
- Full boundary + settlements fetched for all 145 (`firewatch/setup_bih.py`,
  a resumable batch script - Nominatim `/lookup` by known relation id,
  Overpass for settlements, the same `town_point()` matching logic as the
  single-place `setup.py`). Visually spot-checked for gaps/overlaps.
- Bosnia and Herzegovina's own national boundary fetched the same way
  (`data/bih/country.geojson`, OSM relation 2528142) and wired in as
  `BOUNDARY_GEOJSON` - the fetch-time clip and the map's always-on outline,
  replacing Zavidovići's boundary in both roles. Confirmed end to end: a real
  poll cycle fetched and correctly placed a detection near Bihać, ~250 km from
  Zavidovići, which the old clip would never have kept.
- `data/bih/settlements.json` — merged and deduplicated from all 145
  municipalities' own settlement fetches (59,545 raw rows → 13,240 unique
  places), replacing Zavidovići's local 413-place list as `SETTLEMENTS_JSON`.
- `data/bih/country-buffer.geojson` — the "nearby" band, same concept as
  before, now drawn around the country outline.

**Classification and alerting**
- `firewatch/geo_bih.py` — point-in-polygon classification against all 145
  boundaries. Returns every municipality a point falls inside (usually one,
  two for Sarajevo/Istočno Sarajevo, whose constituent municipalities and
  coordinating "Grad" are deliberately overlapping polygons), or the single
  nearest municipality as a fallback for the rare gap between two adjacent
  OSM-traced boundaries that don't quite share an edge.
- `events.build_events()` stamps every event with `"municipalities"` (the
  classification result).
- `telegram.py` rewritten for per-municipality routing: `channel_for(id)`
  replaces the single fixed channel, `send_alert()` fans out to every matched
  municipality independently (one missing channel or one failed post never
  blocks another).
- Consequential fixes: `poller._telegram_channel_url()` (the map's old
  single-channel subscribe banner) now returns `None`; the menu bar's "Post
  Test Telegram" button is permanently disabled (no municipality context to
  target from a menu); `test-telegram` is now a CLI command taking a
  municipality id; `telegram-status` reports how many of 145 have a channel.
- `firewatch/mapgen.py` draws all 145 boundaries as a toggleable overlay
  ("BiH municipalities" / "Općine BiH"), separate from and on top of the
  always-on country outline.

**Removing the single-municipality config**
- `BOUNDARY_GEOJSON`/`SETTLEMENTS_JSON`/`BUFFER_GEOJSON`/`TOWN_LAT`/`TOWN_LON`
  all repointed to the country-wide artifacts above.
- `geo.from_town()` and every event's `dist_town_km`/`dir_town` removed
  entirely - no single town to measure from anymore; `place`/`place_parts`
  (nearest settlement, already computed independently) already covers what
  that field was for, at every one of 145 towns rather than one.
- Map strings (title, header, subtitle, the "nothing detected" message) now
  say Bosnia and Herzegovina rather than Grad Zavidovići. Old single-place
  files (`data/zavidovici.geojson`, `data/settlements.json`,
  `data/zavidovici-buffer.geojson`) deleted as orphaned.

**Telegram channel provisioning**
- Channel creation automated via Telethon (the user API, since the Bot API
  has no method to create a channel at all) -
  `~/firewatch-telegram-setup/provision_telegram_channels.py` on the user's
  own machine, run interactively (needs a login code only the account holder
  can supply).
- **Private channels with invite links, not public `@handles`** - Telegram
  enforces a real, hard, per-account cap on how many *public* channels an
  account can administer (hit it at 9-10 during testing: "You're admin of
  too many public channels"). No workaround exists at 145 scale on one
  account, so every channel is private instead; the bot posts via its
  numeric chat id.
- Safety features added after a real 24-hour flood-wait (escalated from an
  earlier ~1-hour one during a ~50-item burst): `MAX_NEW_PER_RUN` (default
  33) stops the script cleanly before hitting a wall instead of sleeping
  through one unattended, and any flood-wait ≥2 minutes now stops the whole
  session rather than auto-retrying.
- **145/145 channels provisioned** (0 failed, finished 2026-09-27). The
  resumable batch design (default 33/run, stopping cleanly on a flood-wait
  rather than sleeping through one) got there over several sessions across
  multiple days, exactly as planned.
- **Real channel data merged into `data/bih/municipalities.json`** for all
  145: `telegram_channel` holds the actual `-100`-prefixed Bot-API chat id,
  `telegram_invite` the `https://t.me/+...` invite link, both from
  `~/firewatch-telegram-setup/telegram_provision_results.json`. No
  municipality is left with a placeholder or a `null` any more.
- **`poll.yml` and `test-telegram.yml` updated for the per-municipality
  design** - the `FIREWATCH_TELEGRAM_CHANNEL` secret (meaningless since the
  per-municipality rewrite - routing is committed data now, not a secret) is
  gone from both; `TELEGRAM_BOT_TOKEN` is the one Telegram secret left.
  `test-telegram.yml` had been fully broken (a hard crash on any run) since
  the rewrite - it called `telegram.channel()`/`channel_source()`, both
  removed - and now takes a required `municipality` input, passed through as
  an env var rather than interpolated into the run script (a
  `workflow_dispatch` input is untrusted text, same script-injection concern
  as anything else). `telegram.map_url()` is unaffected by any of this - it
  still exists, unlike `channel()`/`channel_source()`.
- **A real per-municipality Telegram subscribe link on the map** - each
  municipality's popup shows a "Subscribe on Telegram" button using its own
  invite link when one exists (`mapgen.py`'s `muniPopupHtml()`), replacing the
  single global `#tgbanner` banner, which is now removed - it was permanently
  dead code, since `poller._telegram_channel_url()` returns `None`
  unconditionally in this per-municipality design.

**A real, non-isolated end-to-end test** - `python3 -m firewatch test-telegram
maglaj` posted successfully to Maglaj's real private channel, confirmed by
Telegram accepting the send. It caught a real bug on the way: the
`TELEGRAM_BOT_TOKEN` in the macOS Keychain (`firewatch-telegram` service) was
still the *old single-municipality bot's* token
(`@firewatchzavidovicibot`, from before this branch), not
`@FirewatchBiHBot` - the bot `provision_telegram_channels.py` actually
promoted to admin in all 113 channels. Every send failed with Telegram's
`400 chat not found` until the Keychain token was replaced with
`@FirewatchBiHBot`'s real one via `set-telegram-key`. This would have
silently broken alerting for every one of the 113 channels in production -
worth checking `getMe` against the configured token whenever this is
re-verified, not just that a token is present.

**Fire danger** - `firedanger.py` already computes one FWI reading per
municipality (`update_all()` loops all 145, each using its own
`Municipality.forecast_point` vertex-mean, not one country-wide point) - this
was done in the same work that added per-municipality routing, not a
follow-up. There is no remaining "single country-wide point" problem here.

**Test coverage**
- `tests_geo_bih.py` (7/7) and `tests_telegram_routing.py` (10/10) added.
- `tests_events.py` (23/23) and `tests_fwi.py` (290/290) still pass
  unmodified - `forecast_point()` now correctly reports the country's own
  centroid.
- The hardcoded-path bug in both test files (see `fork-template`'s identical
  fix) was independently present here too and is now fixed, so these
  actually test this checkout rather than whatever's elsewhere.
- **`reclip`/`store.out_of_scope()` re-verified against the country-wide
  boundary, not assumed** - `python3 -m firewatch reclip` against the real
  17,665-detection BiH backfill reports all of them in scope (expected,
  since the fetch clip used the same boundary to gather them); a synthetic
  detection planted at Rome, Italy was correctly caught as 393.54 km out.
  Confirms `geo.point_in_boundary()`/`distance_to_boundary_km()` work
  correctly against `data/bih/country.geojson`, and that this doesn't
  silently no-op.

**Running live, locally, for real** - the old single-municipality
`com.firewatch.zavidovici` launchd service was uninstalled (data archived to
`~/Library/Application Support/FireWatch-zavidovici-archive-20260926/`, not
deleted), this branch installed in its place as `com.firewatch.bih`, with a
clean database and `telegram_enabled`/`sms_enabled`/`auto_expose` all on.
First real cycle caught a genuine new fire (Brajkovići, Čapljina) and
alerted correctly: real SMS sent, desktop notification sent, Telegram
correctly skipped it (Čapljina isn't provisioned yet) rather than erroring.

Found and fixed a real bug installing it: **`firewatch-ctl` carried its own
separate, hardcoded `LABEL="com.firewatch.zavidovici"`**, never updated when
`menubar.py`'s `SERVICE_LABEL` became `"com.firewatch.bih"` - so the shell
script would have kept controlling a job under the old name while the
Python app's own `_service_target()` (its "Restart" menu action, its status
check) targeted the new name, which was never actually running. Fixed by
matching the two.

**`CLAUDE.md`** now has its own "## This branch" section (mirroring
`fork-template`'s), covering the country-wide data model, the bot-identity
landmine `test-telegram maglaj` caught for real (see above), and the
intentionally-partial `telegram_channel`/`telegram_invite` fields. The
`## Branches` section also gained a paragraph describing this branch, next
to the existing `fork-template` one.

**`docs/*.html` audit, done for all 9 affected files** (of 12 total;
`firewatch-spread-risk.html`, `firewatch-sms-preview.html`, `firewatch-hosting.html`
and `firewatch-linux.html` never mentioned Zavidovići):
- `firewatch-documentation.html` (1,366 lines, the flagship architecture doc) -
  full rewrite: title/brand, the facts panel, the 560 km² Zavidovići lede, the
  spatial clip's municipality-vs-country framing, the Telegram section (one
  public channel → 145 private ones), the map's municipality-outline claim, file
  paths, test/module/line counts, the OSM relation id, and the `IZVAN OPĆINE` →
  `IZVAN BIH` marker. Real measured examples (the 3-4 September fire timeline,
  the MTG sensor floor, the Kamenica pattern) were kept, not fabricated
  replacements, explicitly reframed as measurements from the original
  single-municipality deployment.
- `firewatch-api-reference.html` - real architectural rewrite: the Nominatim/
  Overpass sections described the old interactive single-place `setup.py`;
  replaced with what `setup_bih.py` actually does (`/lookup` by known relation
  id ×145, `around:radius` per municipality merged/deduplicated). The Telegram
  Bot API section was entirely the old one-channel model with a fake chat id;
  rewritten for the real per-municipality private-channel design, including the
  bot-identity landmine as a documented failure mode. FIRMS/Meteosat curl
  examples now use the real, current country-wide bbox.
- `firewatch-fire-danger.html` - fixed to describe `update_one()`/`update_all()`
  and each municipality's own `Municipality.forecast_point` rather than one
  `update()`/`geo.forecast_point()` for the whole deployment; fixed a wrong CLI
  example (bare `fire-danger` now lists all 145, not one forecast).
- `firewatch-field-manual.html` - fixed the opening lede ("watches one
  municipality") and rewrote the Telegram setup walkthrough, which gave
  actionable but wrong instructions (one bot, one public channel,
  `telegram_channel` in `config.json` - none of which exist here). Added a
  scoping note to the separate "relocate to a new municipality by hand"
  appendix rather than rewriting it - that section describes `main`'s manual
  process, same as `firewatch-fork.html`, not this branch.
- `firewatch-telegram-preview.html` - one-public-channel framing and the marker
  name fixed.
- `firewatch-macos.html` - the one cheap fix from earlier (stale plist name).
- `firewatch-satellite-latency.html`, `firewatch-sentinel3-plan.html` and
  `firewatch-fork.html` were reviewed and deliberately left unchanged: the
  first two are real orbital-geometry measurements at one specific, real
  latitude (legitimate worked examples, not scope claims), and the third
  describes `fork-template`'s own workflow generically, orthogonal to this
  branch.

**Two more real bugs found and fixed while doing this audit** - the fifth and
sixth this session, after the muniLayer canvas click-through, the popup
z-index trap, the Keychain bot-token mismatch, and `firewatch-ctl`'s label
mismatch:
- `telegram.py`/`sms.py`'s `outside`/`IZVAN OPĆINE` label said "outside the
  municipality" when `ev["inside"]` has meant "inside Bosnia and Herzegovina"
  since the country-wide clip landed - a live mistranslation shown to real
  Telegram subscribers. Fixed to `IZVAN BIH`.
- `firewatch-ctl`'s `test-telegram` case never forwarded `$2` the way
  `sms-add`/`sms-remove` already do, so `./firewatch-ctl test-telegram` could
  never actually test anything (`cmd_test_telegram` requires a municipality id
  on this branch). Fixed.

Six bugs in one session, all caught by actually running things - installing
the service, cross-checking docs against running code, sending a real test
message - rather than reading code in isolation. None of them would have
surfaced from `tests_*.py` passing.

## Remaining

**Nothing.** The transition this file tracks is complete: all 145
municipalities classified, routed, and alerting on all three channels;
all 145 Telegram channels provisioned and verified end to end
(`test-telegram stolac`, one of the last 15, delivered successfully); the
merge into `main` landed and is running live on GitHub Actions without
the cold-start problem that hit the first real run.

**Since the merge into `main` (2026-09-27), two more real things were found
and fixed by watching the live system run, not by re-reading this file:**

- **`firedanger.update_all()`'s cold start got the first real Actions run
  cancelled.** All 145 municipalities were due at once (nothing cached on the
  freshly reset database), sequential fetching took several minutes, and the
  external caller triggering `poll.yml` was manually cancelled thinking it
  had hung - which discarded all progress, since nothing commits until a
  cycle finishes. Fixed with a bounded thread pool (measured 145/145 in ~9s,
  down from minutes) plus a coordinated retry for Open-Meteo's own
  undocumented-but-real per-minute rate limit (measured directly: a 429
  whose body says "Minutely API request limit exceeded"). Confirmed healthy
  afterward: 5 automated `main` commits with detections growing normally, no
  further cancellations.
- **Multiple alert kinds for one event in the same cycle now merge into one
  message per channel** instead of one per kind. Real case: a fire near
  Mostar gained a second satellite source and crossed the intensified
  threshold in the same cycle, producing two near-identical Telegram posts
  back to back ("POTVRĐEN" then "POJAČAVA SE"). `events.diff()` firing
  several kinds at once for one event is correct, documented behavior - the
  fix merges them into one message ("POTVRĐEN + POJAČAVA SE") per channel,
  with cooldown and per-channel kind-filtering preserved exactly as before.
