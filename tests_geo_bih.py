"""Verify country-wide municipality classification: correct single matches,
correct Sarajevo/Istocno Sarajevo overlap, correct gap fallback, and that it
runs fast enough to matter for a real poll cycle."""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from firewatch import geo_bih

passed = failed = 0


def check(name, got, want):
    global passed, failed
    ok = got == want
    passed += ok
    failed += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {name:40s} got={got!r} want={want!r}")


# Each municipality's own town point must classify into (at least) itself.
check("Zavidovici town point -> zavidovici",
      geo_bih.classify_point(44.4388706, 18.1458239), ["zavidovici"])
check("Jablanica town point -> jablanica",
      geo_bih.classify_point(43.6617485, 17.7618471), ["jablanica"])

# Centar (a Sarajevo constituent) must match both itself and the coordinating
# "Grad Sarajevo" umbrella - the deliberate overlap, not a bug to resolve.
centar_matches = set(geo_bih.classify_point(43.8992312, 18.4151611))
check("Centar town point matches {centar, sarajevo}",
      centar_matches >= {"centar", "sarajevo"}, True)

# A point clearly outside Bosnia entirely (mid-Adriatic) must still resolve to
# exactly one municipality via the nearest-boundary fallback, never an empty
# list and never more than one (no polygon actually contains it).
far = geo_bih.classify_point(42.0, 15.0)
check("far-outside point returns exactly one fallback id", len(far), 1)

# A point on the open sea south of Neum (BiH's only coastal municipality) is
# outside every polygon but should fall back to the geographically nearest
# one, not an arbitrary distant match.
near_neum = geo_bih.classify_point(42.85, 17.65)
check("point just off Neum's coast falls back to neum", near_neum, ["neum"])

# Never zero, for any of the 145 municipalities' own town points.
import json
rows = json.loads((geo_bih.DATA_DIR / "municipalities.json").read_text())
all_ok = True
for r in rows:
    m = geo_bih.classify_point(r["town"]["lat"], r["town"]["lon"])
    if r["id"] not in m:
        print(f"  FAIL  {r['id']} town point did not self-match: got {m}")
        all_ok = False
check("every municipality's own town point self-matches", all_ok, True)

# Timing: a real poll cycle classifies at most a few dozen detections, not
# thousands - this just guards against an accidental O(n^2) blowup.
t0 = time.time()
for r in rows:
    geo_bih.classify_point(r["town"]["lat"], r["town"]["lon"])
elapsed = time.time() - t0
check(f"145 classifications complete in well under 5s ({elapsed:.2f}s)",
      elapsed < 5.0, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
