"""Which place this instance watches.

Everything that says "Zavidovići" out loud - the map title, the notification
wording, the reference point every distance is measured from, the names of the
two committed geometry files - resolves through here, so adapting this repository
to another municipality is an edit to one JSON file rather than a hunt through
seven modules for a string.

`data/place.json` is committed, like the geometry it names: the running app never
asks Nominatim or Overpass anything. `python3 -m firewatch setup` writes all three
files in one go; `FIREWATCH_PLACE_FILE` points at a different profile, which is
what a container mounting its own configuration needs.

The fallback below is not a template to fill in. It exists so that a missing or
half-written place.json degrades to something that still renders - `active()`
reports which of the two is live, and `python3 -m firewatch place` prints it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Names are per language because the map is bilingual and the phrases are not
# assemblable from a bare name: Bosnian wants the genitive after "od"
# (Zavidovići -> od Zavidovića) and a locative for "in" (u općini Zavidovići).
# A fork writes the three or four forms its own language needs, once.
#
#   title    page title and the header line
#   area     the subject form, as a heading would say it
#   of       after a distance and bearing: "12 km NE of Zavidovići"
#   in       locative: "Nothing detected {in} or within 2 km of it"
#   near     used by the desktop notification for a fire over the border
#   outside  the marker on a detection outside the boundary
FALLBACK = {
    "id": "zavidovici",
    "boundary": "zavidovici.geojson",
    "settlements": "settlements.json",
    "buffer": "zavidovici-buffer.geojson",
    "town": {"lat": 44.4388706, "lon": 18.1458239},
    "timezone": "Europe/Sarajevo",
    "language": "bs",
    "names": {
        "en": {
            "title": "FireWatch Zavidovići",
            "area": "Grad Zavidovići",
            "of": "of Zavidovići",
            "in": "in Grad Zavidovići",
            "near": "near Zavidovići",
            "outside": "outside municipality",
        },
        "bs": {
            "title": "FireWatch Zavidovići",
            "area": "Grad Zavidovići",
            "of": "od Zavidovića",
            "in": "u općini Zavidovići",
            "near": "blizu Zavidovića",
            "outside": "izvan općine",
        },
    },
}

PLACE_FILE = Path(os.environ.get("FIREWATCH_PLACE_FILE") or (DATA_DIR / "place.json"))

_SOURCE = "built-in fallback"


def _load() -> dict:
    """Merge the profile over the fallback, one level deep per language.

    Shallow-merging `names` per language rather than wholesale means a profile
    that gives only `en` still has a `bs` to fall back to instead of raising a
    KeyError from inside a template render.
    """
    global _SOURCE
    try:
        raw = json.loads(PLACE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return json.loads(json.dumps(FALLBACK))
    p = json.loads(json.dumps(FALLBACK))
    names = p["names"]
    for k, v in raw.items():
        if k == "names" and isinstance(v, dict):
            for lang, forms in v.items():
                names[lang] = {**names.get(lang, names["en"]), **forms}
        else:
            p[k] = v
    p["names"] = names
    _SOURCE = str(PLACE_FILE)
    return p


PLACE = _load()

TOWN_LAT = float(PLACE["town"]["lat"])
TOWN_LON = float(PLACE["town"]["lon"])
BOUNDARY_FILE = DATA_DIR / PLACE["boundary"]
SETTLEMENTS_FILE = DATA_DIR / PLACE["settlements"]
BUFFER_FILE = DATA_DIR / PLACE["buffer"]


def names(lang: str = "en") -> dict:
    """The name forms for a language, falling back to English."""
    return PLACE["names"].get(lang) or PLACE["names"]["en"]


def name(key: str, lang: str = "en") -> str:
    return names(lang).get(key, names("en").get(key, ""))


def source() -> str:
    """Where the live profile came from - the file, or the built-in fallback."""
    return _SOURCE


def is_default() -> bool:
    """True when nothing has re-pointed this instance.

    A fork that never ran `setup` is still watching Zavidovići, and every symptom
    of that (an empty map, alerts about nothing) looks like a broken feed rather
    than a missed step. `place` and the poll workflow say so out loud instead.
    """
    return PLACE["id"] == FALLBACK["id"]
