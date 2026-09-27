"""Batch fetch of boundary + settlements for every municipality in
data/bih/municipalities.json.

Talks to the same two services `fork-template`'s setup.py does (Nominatim,
Overpass), for the same reason - build-time artifacts, fetched once and
committed, so the running app never geocodes at runtime - but this branch is
built on `main`, which has none of that module's scaffolding (no place.py, no
per-fork setup command: main is one hardcoded instance). So the small pieces
this needs (matching a fetched settlement to the resolved place name, folding
diacritics for that comparison) are reimplemented here rather than imported
from a module this branch doesn't have.

The difference from a single `setup` run, beyond scale (145 municipalities
instead of one), is how each one is looked up: a name search has to
disambiguate by hand when several relations match a query. Here every OSM
relation id is already known (from the Overpass admin_level sweep that built
municipalities.json), so this uses Nominatim's /lookup endpoint by relation id
instead - no name ambiguity possible, one request per municipality rather than
a search-and-pick.

Resumable by design: a municipality already on disk (both its boundary and
settlements files) is skipped unless --force. Overpass has been observed to
504/timeout under load even for a single-place fetch, one query at a time; at
145 municipalities that is not a corner case, it is expected, so a failure
here is logged and the run moves on rather than aborting - re-running the
script later picks up exactly what is missing.

Nominatim's usage policy caps requests at 1/second; NOMINATIM_DELAY enforces
that regardless of how fast Overpass answers, since the two aren't otherwise
rate-limited against each other in the same loop.
"""
from __future__ import annotations

import json
import sys
import time
import unicodedata
from pathlib import Path

import requests

NOMINATIM_LOOKUP = "https://nominatim.openstreetmap.org/lookup"
NOMINATIM_DELAY = 1.1  # seconds; their usage policy caps at 1 req/s

# Global mirrors only - see setup.py on fork-template for why: regional extracts
# answer 200 with an empty element list for anywhere outside their own box.
OVERPASS = ("https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter")
UA = "firewatch-setup-bih/1.0 (+https://github.com/) batch boundary fetch"

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "bih"
MUNI_FILE = DATA_DIR / "municipalities.json"


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def fetch_settlements(lat: float, lon: float, radius_km: float) -> list[dict]:
    """Named places within `radius_km` - identical query to fork-template's
    setup.py, duplicated here for the reason in the module docstring."""
    q = ('[out:json][timeout:90];'
         '(node["place"~"^(city|town|village|hamlet)$"]'
         f'(around:{int(radius_km * 1000)},{lat},{lon}););out body;')
    last = ""
    for url in OVERPASS:
        try:
            r = requests.post(url, data={"data": q},
                              headers={"User-Agent": UA}, timeout=120)
            elements = r.json().get("elements", []) if r.ok else []
        except Exception as exc:
            last = f"{url}: {exc}"
            continue
        if elements:
            return [{"n": e["tags"]["name"], "k": e["tags"]["place"],
                     "lat": e["lat"], "lon": e["lon"]}
                    for e in elements if e.get("tags", {}).get("name")]
        last = f"{url}: empty result"
    raise RuntimeError(f"no settlements from any mirror ({last})")


def town_point(places: list[dict], short: str,
                rel_lat: float, rel_lon: float) -> tuple[float, float, bool]:
    """Where the town actually is, if Overpass found one under that name -
    same logic and reasoning as fork-template's setup.py.town_point()."""
    target = _fold(short)
    for p in places:
        if p["k"] in ("town", "city") and _fold(p["n"]) == target:
            return float(p["lat"]), float(p["lon"]), True
    return rel_lat, rel_lon, False


def fetch_relation(osm_relation: int) -> dict:
    """The boundary + representative point for one already-known relation id."""
    r = requests.get(NOMINATIM_LOOKUP,
                      params={"osm_ids": f"R{osm_relation}", "format": "json",
                              "polygon_geojson": 1},
                      headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    hits = r.json()
    if not hits:
        raise RuntimeError(f"relation {osm_relation}: no lookup result")
    hit = hits[0]
    if hit.get("geojson", {}).get("type") not in ("Polygon", "MultiPolygon"):
        raise RuntimeError(f"relation {osm_relation}: not a polygon "
                           f"({hit.get('geojson', {}).get('type')})")
    return hit


def process_one(row: dict, radius_km: float = 25.0) -> dict:
    pid = row["id"]
    boundary_path = DATA_DIR / f"{pid}.geojson"
    settlements_path = DATA_DIR / f"{pid}-settlements.json"

    hit = fetch_relation(row["osm_relation"])
    rel_lat, rel_lon = float(hit["lat"]), float(hit["lon"])

    places = fetch_settlements(rel_lat, rel_lon, radius_km)
    lat, lon, matched = town_point(places, row["short_name"], rel_lat, rel_lon)

    boundary_path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": hit["display_name"],
                           "source": f"OpenStreetMap relation {row['osm_relation']}",
                           "licence": "ODbL 1.0"},
            "geometry": hit["geojson"]}]},
        ensure_ascii=False), encoding="utf-8")
    settlements_path.write_text(json.dumps(places, ensure_ascii=False),
                                encoding="utf-8")

    ring = hit["geojson"]["coordinates"][0]
    n_vertices = len(ring[0] if hit["geojson"]["type"] == "MultiPolygon" else ring)

    row["boundary"] = boundary_path.name
    row["settlements"] = settlements_path.name
    row["town"] = {"lat": lat, "lon": lon}
    row["town_matched"] = matched
    row["n_vertices"] = n_vertices
    row["n_settlements"] = len(places)
    row.pop("fetch_error", None)
    return row


def run(force: bool = False) -> None:
    rows = json.loads(MUNI_FILE.read_text())
    by_id = {r["id"]: r for r in rows}
    already_done = 0
    fetched = 0
    failed: list[str] = []

    for i, row in enumerate(rows, 1):
        pid = row["id"]
        boundary_path = DATA_DIR / f"{pid}.geojson"
        settlements_path = DATA_DIR / f"{pid}-settlements.json"
        if not force and boundary_path.exists() and settlements_path.exists():
            already_done += 1
            continue

        print(f"[{i}/{len(rows)}] {row['short_name']} ({pid}) ...", flush=True)
        try:
            process_one(row)
            fetched += 1
            print(f"  ok: {row['n_vertices']} vertices, {row['n_settlements']} "
                 f"settlements, town {'matched' if row['town_matched'] else 'not matched (relation centre used)'}",
                 flush=True)
        except (Exception, SystemExit) as exc:
            # fetch_settlements() raises SystemExit on a total Overpass failure
            # (both mirrors empty or unreachable) - reasonable for the
            # interactive single-place tool, fatal here otherwise: at 145
            # municipalities a double-mirror miss is expected, not exceptional.
            print(f"  FAILED: {exc}", flush=True)
            row["fetch_error"] = str(exc)[:200]
            failed.append(pid)
        finally:
            # Rewritten every iteration so a crash mid-run loses nothing already
            # done - the file on disk always reflects the furthest progress made.
            MUNI_FILE.write_text(json.dumps(list(by_id.values()), indent=2,
                                            ensure_ascii=False), encoding="utf-8")
            time.sleep(NOMINATIM_DELAY)

    print(f"\n{fetched} fetched this run, "
         f"{already_done} already had data, {len(failed)} failed.")
    if failed:
        print("Failed (re-run this script to retry just these):")
        for pid in failed:
            print(f"  {pid}")


if __name__ == "__main__":
    run(force="--force" in sys.argv[1:])
