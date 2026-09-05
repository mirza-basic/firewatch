"""One command that re-points this repository at another municipality.

Everything here talks to Nominatim and Overpass, and nothing else in the project
ever does: the boundary and the settlement list are build-time artifacts, fetched
once and committed, so a running instance - and every GitHub Actions run - needs
no geocoding service at all. That is the whole reason this is a separate module
and a separate step rather than something the poller does lazily.

    python3 -m firewatch setup "Opcina Kakanj"

writes data/<id>.geojson, data/<id>-settlements.json and data/place.json, and
then tells you the three things it cannot do for you: rebuild the map band, write
the place's name forms in its own language, and clear the previous place's
history out of the database.

Two failure modes are guarded because both answer HTTP 200 and look like data:

* Searching for the town returns a Point. "Kakanj" is a place node; "Opcina
  Kakanj" is the boundary relation with a polygon. Only relations carrying a
  Polygon or MultiPolygon are accepted, and the alternatives are printed rather
  than silently ranked.
* A regional Overpass mirror answers for anywhere on earth with
  {"elements": []}. An empty settlement list is treated as a failed request and
  retried elsewhere, never written - accepting it would label every fire with
  bare coordinates and look like a place with no villages in it.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import requests

from .place import DATA_DIR, PLACE_FILE

NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Global mirrors only. overpass.osm.ch and friends are regional extracts that
# answer 200 with an empty element list for anywhere outside their box.
OVERPASS = ("https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter")
UA = "firewatch-setup/1.0 (+https://github.com/) one-off boundary fetch"


# Leading administrative words, stripped to get the name people actually use.
# "Opcina Kakanj" is how you find the relation; "Kakanj" is how a fire alert
# should read. Matched case-insensitively after folding diacritics.
ADMIN_PREFIXES = ("opcina", "opstina", "grad", "gemeinde", "stadt", "comune di",
                  "commune de", "municipio de", "municipality of", "city of",
                  "town of", "borough of", "county of", "kommune", "kommun",
                  "gmina", "obec", "obcina", "kozseg", "comuna")


def short_name(admin: str) -> str:
    """The bare place name: "Opcina Kakanj" -> "Kakanj"."""
    folded = unicodedata.normalize("NFKD", admin).encode("ascii", "ignore").decode()
    low = folded.lower()
    for prefix in ADMIN_PREFIXES:
        if low.startswith(prefix + " "):
            return admin[len(prefix) + 1:].strip() or admin
    return admin


def slug(name: str) -> str:
    """A filesystem- and id-safe form of the place name, diacritics folded."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    # "opcina-kakanj" -> "kakanj": the administrative prefix is how you find the
    # relation, not what the place is called.
    for prefix in ("opcina-", "opstina-", "grad-", "city-of-", "municipality-of-",
                   "comune-di-", "commune-de-", "gemeinde-", "municipio-de-"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s or "place"


def fetch_boundary(query: str, index: int = 0) -> dict:
    """The administrative relation for `query`, as a Nominatim hit."""
    r = requests.get(NOMINATIM, params={"q": query, "format": "json",
                                        "polygon_geojson": 1, "limit": 10},
                     headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    hits = [h for h in r.json()
            if h.get("osm_type") == "relation"
            and h.get("geojson", {}).get("type") in ("Polygon", "MultiPolygon")]
    if not hits:
        raise SystemExit(
            f"  no boundary relation for {query!r}.\n"
            "  Search the administrative name, not the town: 'Opcina Kakanj',\n"
            "  'Grad Zavidovici', 'City of Boulder' - a bare town name returns a\n"
            "  point, which has no area to clip against.")
    if len(hits) > 1:
        print(f"  {len(hits)} boundary relations matched:")
        for i, h in enumerate(hits):
            print(f"    [{i}]{' <-' if i == index else '  '} {h['display_name']}")
        print("  Pass --index N to pick a different one.\n")
    return hits[index]


def fetch_settlements(lat: float, lon: float, radius_km: float) -> list[dict]:
    """Named places within `radius_km`, which is what turns a coordinate into
    "7.0 km ESE of Kamenica".

    The radius wants to be comfortably wider than the municipality: a fire just
    over the border is named by the nearest village, and that village is often
    outside.
    """
    q = ('[out:json][timeout:90];'
         '(node["place"~"^(city|town|village|hamlet)$"]'
         f'(around:{int(radius_km * 1000)},{lat},{lon}););out body;')
    last = ""
    for url in OVERPASS:
        try:
            r = requests.post(url, data={"data": q},
                              headers={"User-Agent": UA}, timeout=120)
            elements = r.json().get("elements", []) if r.ok else []
        except Exception as exc:                       # any mirror can be down
            last = f"{url}: {exc}"
            print(f"  {url} failed: {exc}")
            continue
        if elements:
            print(f"  {url}: {len(elements)} places")
            return [{"n": e["tags"]["name"], "k": e["tags"]["place"],
                     "lat": e["lat"], "lon": e["lon"]}
                    for e in elements if e.get("tags", {}).get("name")]
        last = f"{url}: empty result"
        print(f"  {url} returned nothing - treating as a failure, not an answer")
    raise SystemExit(f"  no settlements from any mirror ({last}).\n"
                     "  Do not accept this: an empty list would label every fire\n"
                     "  with bare coordinates. Run it again.")


def name_forms(short: str, admin: str, lang: str) -> dict:
    """Scaffolded name forms - English correct, any other language a placeholder.

    The forms cannot be generated: Bosnian wants a genitive after "od"
    (Zavidovici -> od Zavidovica) and a locative after "u" (u opcini Zavidovici),
    and guessing produces text that is wrong in a way only a native reader sees.
    So the non-English block is seeded with the bare name and setup says, loudly,
    that four lines need a human. Wrong-but-obvious beats wrong-but-plausible.
    """
    en = {"title": f"FireWatch {short}",
          "area": admin,
          "of": f"of {short}",
          "in": f"in {admin}",
          "near": f"near {short}",
          "outside": "outside municipality"}
    if lang == "en":
        return {"en": en}
    return {"en": en, lang: dict(en, title=f"FireWatch {short}")}


def run(query: str, *, place_id: str | None = None, lang: str = "en",
        radius_km: float = 25.0, index: int = 0, force: bool = False) -> int:
    print(f"\n  Looking up {query!r} ...")
    hit = fetch_boundary(query, index)
    admin = hit["display_name"].split(",")[0].strip()
    short = short_name(admin)
    pid = place_id or slug(short)
    lat, lon = float(hit["lat"]), float(hit["lon"])
    print(f"  {hit['display_name']}")
    print(f"  relation {hit['osm_id']} · centre {lat:.6f}, {lon:.6f} · id {pid}")

    boundary = DATA_DIR / f"{pid}.geojson"
    settlements = DATA_DIR / f"{pid}-settlements.json"
    existing = [p for p in (boundary, settlements, PLACE_FILE) if p.exists()]
    if existing and not force:
        for p in existing:
            print(f"  exists: {p}")
        print("\n  Nothing written. Re-run with --force to overwrite.\n")
        return 1

    # Everything is fetched before anything is written. Overpass fails often enough
    # that writing the boundary first is not theoretical: it leaves a repository
    # holding one place's outline, another place's settlements and no profile
    # tying them together - and the next run reads the stale profile and reports
    # success. All three files or none.
    print(f"\n  Fetching settlements within {radius_km:g} km ...")
    places = fetch_settlements(lat, lon, radius_km)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    boundary.write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {"name": hit["display_name"],
                       "source": f"OpenStreetMap relation {hit['osm_id']}",
                       "licence": "ODbL 1.0"},
        "geometry": hit["geojson"]}]}, ensure_ascii=False), encoding="utf-8")
    ring = hit["geojson"]["coordinates"][0]
    n_vertices = len(ring[0] if hit["geojson"]["type"] == "MultiPolygon" else ring)
    print(f"  wrote {boundary.name} ({n_vertices} vertices)")

    settlements.write_text(json.dumps(places, ensure_ascii=False), encoding="utf-8")
    print(f"  wrote {settlements.name} ({len(places)} places)")

    profile = {
        "id": pid,
        "boundary": boundary.name,
        "settlements": settlements.name,
        "buffer": f"{pid}-buffer.geojson",
        "town": {"lat": lat, "lon": lon},
        "timezone": "UTC",
        "language": lang,
        "names": name_forms(short, admin, lang),
    }
    PLACE_FILE.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    print(f"  wrote {PLACE_FILE.name}")

    print(f"""
  Three things this cannot do for you:

  1. The map band. Only this needs shapely and pyproj, which is why they are
     imported inside one function and are not in requirements.txt:

         python3 -m pip install shapely pyproj
         python3 -m firewatch buffer

     Skip it and the map draws no band rather than a wrong one.

  2. The timezone. data/place.json says "UTC"; the map stamps every time in it.
     Set it to a real zone - "Europe/Sarajevo", "America/Denver".
""" + ("" if lang == "en" else f"""
  3. The {lang} name forms in data/place.json. They are seeded with the English
     wording, which will read wrong in {lang}. Four strings, once: "area" as a
     heading says it, "of" after a distance and bearing, "in" as a locative,
     "outside" for a detection over the border.
""") + f"""
  Then check it:

      python3 -m firewatch place
      python3 -m firewatch poll 24h

  and clear the previous place's detections, which are now outside the clip:

      python3 -m firewatch reclip           # dry run, shows what would go
      python3 -m firewatch reclip --apply   # copies the database first
""")
    return 0
