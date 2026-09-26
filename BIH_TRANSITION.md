# Transition to full Bosnia and Herzegovina coverage

This branch (`bih-all-municipalities`, forked from `main`) is turning FireWatch
from a single-municipality deployment (Zavidovići) into one that watches all
145 self-governing units of Bosnia and Herzegovina, with a separate Telegram
channel per municipality so someone can subscribe to just their own. This
file tracks what's actually done versus what's still needed - status as of
2026-09-26.

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
- **130/145 channels provisioned as of this writing** (0 failed). Resumable -
  re-running the same script skips everything done and continues.
- **Real channel data merged into `data/bih/municipalities.json`** for all 130
  provisioned so far: `telegram_channel` holds the actual `-100`-prefixed
  Bot-API chat id, `telegram_invite` the `https://t.me/+...` invite link -
  both from `~/firewatch-telegram-setup/telegram_provision_results.json`. The
  15 not-yet-provisioned municipalities get `null` for both fields rather
  than a stale placeholder, so `telegram.channel_for()` correctly skips them.
  Re-run the same merge once the remaining batch is provisioned.
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

**`CLAUDE.md`** now has its own "## This branch" section (mirroring
`fork-template`'s), covering the country-wide data model, the bot-identity
landmine `test-telegram maglaj` caught for real (see above), and the
intentionally-partial `telegram_channel`/`telegram_invite` fields. The
`## Branches` section also gained a paragraph describing this branch, next
to the existing `fork-template` one.

## Remaining

**Blocking:**

1. **Finish channel provisioning** - 15 remaining as of this writing. Resume
   with the same script on the user's machine
   (`~/firewatch-telegram-setup/provision_telegram_channels.py`), in capped
   batches (default 33/run) across multiple days - do not attempt to rush
   this given the flood-wait history. Re-run the `municipalities.json` merge
   (see Done above) once this finishes.

**Lower priority / worth doing before this is a real deployment:**

2. **`docs/*.html`** - 9 of 12 files reference Zavidovići. Most are a few
   incidental mentions (an example plist name, a sample coordinate), but
   `firewatch-documentation.html` (1,366 lines) is deeply branded throughout
   - title, header, and real narrative built around one actual Zavidovići
   fire event - and needs its own dedicated editorial pass, not a quick
   find-and-replace. One cheap, unambiguous fix landed
   (`firewatch-macos.html`'s stale `com.firewatch.zavidovici.plist` example
   → `com.firewatch.bih.plist`); the rest is still open.
