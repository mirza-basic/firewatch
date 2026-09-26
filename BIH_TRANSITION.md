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
- **113/145 channels provisioned as of this writing** (0 failed). Resumable -
  re-running the same script skips everything done and continues.
- **Real channel data merged into `data/bih/municipalities.json`** for all 113
  provisioned so far: `telegram_channel` holds the actual `-100`-prefixed
  Bot-API chat id, `telegram_invite` the `https://t.me/+...` invite link -
  both from `~/firewatch-telegram-setup/telegram_provision_results.json`. The
  32 not-yet-provisioned municipalities get `null` for both fields rather
  than a stale placeholder, so `telegram.channel_for()` correctly skips them.
  Re-run the same merge once the remaining batch is provisioned.
- **`poll.yml` updated for the per-municipality design** - the
  `FIREWATCH_TELEGRAM_CHANNEL` secret (meaningless since the per-municipality
  rewrite - routing is committed data now, not a secret) is gone;
  `TELEGRAM_BOT_TOKEN` is the one Telegram secret left.
- **A real per-municipality Telegram subscribe link on the map** - each
  municipality's popup shows a "Subscribe on Telegram" button using its own
  invite link when one exists (`mapgen.py`'s `muniPopupHtml()`), replacing the
  single global `#tgbanner` banner, which is now removed - it was permanently
  dead code, since `poller._telegram_channel_url()` returns `None`
  unconditionally in this per-municipality design.

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

## Remaining

**Blocking:**

1. **Finish channel provisioning** - 32 remaining as of this writing. Resume
   with the same script on the user's machine
   (`~/firewatch-telegram-setup/provision_telegram_channels.py`), in capped
   batches (default 33/run) across multiple days - do not attempt to rush
   this given the flood-wait history. Re-run the `municipalities.json` merge
   (see Done above) once this finishes.

**Newly found, not yet fixed:**

2. **`test-telegram.yml` is broken** - it calls `telegram.channel()`,
   `telegram.channel_source()`, `telegram.test_text()` and `telegram.map_url()`,
   none of which exist any more after the per-municipality rewrite (which
   replaced them with `channel_for(municipality_id)` and changed
   `cmd_test_telegram` to take a municipality id argument). This workflow has
   been broken since that rewrite, not by anything in this round of changes -
   `poll.yml` was the one explicitly fixed. Needs the same per-municipality
   treatment `poll.yml` and the CLI already got.

**Lower priority / worth doing before this is a real deployment:**

3. **`CLAUDE.md` itself** has no section for this branch - `fork-template`
   has its own "## This branch" block at the top explaining what it is and
   what rules apply; this branch should get the same treatment once its
   shape has settled (probably after item 1 above).
4. **`docs/*.html`** (the self-contained doc pages, also published as Claude
   artifacts) are unaudited - likely reference Zavidovići-specific details
   throughout, the same way `CLAUDE.md`'s own pre-this-branch content did.
5. **No real, non-isolated end-to-end test yet** - every verification so far
   has been either isolated (scratch `FIREWATCH_DATA_DIR`, no real
   credentials) or unit-level (mocked `telegram.send`). Now that 113 real
   private channels exist with real chat ids merged in, worth confirming one
   real alert actually posts through the whole real pipeline.
6. **`store.out_of_scope()`/`reclip`-style history cleanup** hasn't been
   re-verified against the new country-wide boundary specifically - it reads
   the same `geo.point_in_boundary()`/`distance_to_boundary_km()` the fetch
   clip uses, so it should "just work" the same way the clip itself did, but
   this hasn't been explicitly exercised since the swap.
