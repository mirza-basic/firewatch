"""Batch fetch of boundary + settlements for every municipality in
data/bih/municipalities.json.

Uses Nominatim and Overpass to produce build-time artifacts, fetched once and
committed, so the running app never geocodes at runtime.

Every OSM relation id is already known (from the Overpass admin_level sweep that
built municipalities.json), so each municipality is resolved with Nominatim's
/lookup endpoint by relation id: no name ambiguity, one request per municipality.

Resumable: a municipality already on disk (both its boundary and settlements
files) is skipped unless --force. Overpass times out or answers 504 under load,
which across 145 municipalities is expected, so a failure is logged and the run
moves on; re-running picks up exactly what is missing.

Nominatim's usage policy caps requests at 1/second; NOMINATIM_DELAY enforces
that regardless of how fast Overpass answers.
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

# Global mirrors only: regional extracts answer 200 with an empty element list
# for anywhere outside their own box.
OVERPASS = ("https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter")
UA = "firewatch-setup-bih/1.0 (+https://github.com/) batch boundary fetch"

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "bih"
MUNI_FILE = DATA_DIR / "municipalities.json"


def _fold(s: str) -> str:
    """Lowercase ASCII fold, for comparing place names across diacritics."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def fetch_settlements(lat: float, lon: float, radius_km: float) -> list[dict]:
    """Named cities, towns, villages and hamlets within `radius_km`, trying each Overpass mirror."""
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
    """Coordinates of the town named `short` if Overpass found one.

    Returns (lat, lon, matched); falls back to the relation's centre point."""
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
    """Fetch and write one municipality's boundary and settlements; returns the updated row."""
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
    """Process every municipality not yet on disk (all of them with `force`), saving progress after each."""
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
            # A total Overpass failure (every mirror empty or unreachable) must not
            # abort the run: across 145 municipalities it is expected.
            print(f"  FAILED: {exc}", flush=True)
            row["fetch_error"] = str(exc)[:200]
            failed.append(pid)
        finally:
            # Rewritten every iteration so a crash loses nothing already done.
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
