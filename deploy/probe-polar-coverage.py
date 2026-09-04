"""When do the polar satellites actually observe Bosnia's latitude?

Written to settle a claim that turned out to be wrong. Our own database has no polar
detection between 02:00 and 07:00 UTC in eight months, and it was tempting to read
that as a nightly coverage hole. It is not: 560 km of municipality with a handful of
fires a month means an empty hour there says "nothing was detected", not "nothing
was overhead".

Measured 4 September 2026 - 2,998 detections, 41.5-47.5N / 5-30E, three days, the
band busy with real fires so an empty hour is about coverage:

    UTC   00  01  02  03  04  05  06  07  08  09  10  11
         273 731  51   4   -   -   -  18  21  14 262 761
    UTC   12  13  14  15  16  17  18  19  20  21  22  23
         443  78  34   -   -   -  49   9   -   -   - 250

    empty: 04:00-07:00, 15:00-18:00, 20:00-23:00 UTC - three ~3 h holes a day,
    not one long night-time one. 02:00-03:00 is thinly covered but real, at wide
    scan angles from an adjacent swath.

So a fire unseen between 00:41 and 08:01 was not unseen for want of a satellite
overhead. It was too weak to register: 0.7-2.1 MW that night.

Run with the repository root on the path (needs a FIRMS key):

    PYTHONPATH=. python3 deploy/probe-polar-coverage.py

Our own database cannot answer this: 560 km of municipality with a handful of fires
a month, so an empty hour there means "no fire was detected", which is not the same
as "nothing was overhead". This asks FIRMS for a wide box at the same latitude band,
where September has enough fires burning that an empty hour is about coverage.
"""
from collections import defaultdict
from datetime import timedelta

from firewatch import sources
from firewatch.config import firms_key
from firewatch.store import utcnow

KEY, _ = firms_key()
BAND = (5.0, 41.5, 30.0, 47.5)          # w, s, e, n - Mediterranean Europe, 41.5-47.5N
DAYS = 3
DATASETS = ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "MODIS_NRT"]

session = sources._session()
start = (utcnow() - timedelta(days=DAYS)).strftime("%Y-%m-%d")
area = "%.4f,%.4f,%.4f,%.4f" % BAND

per_ds = {}
for ds in DATASETS:
    url = f"{sources.FIRMS_BASE}/api/area/csv/{KEY}/{ds}/{area}/{DAYS}/{start}"
    r = session.get(url, timeout=(10, 120))
    lines = r.text.strip().split("\n")
    if r.status_code != 200 or len(lines) < 2:
        print(f"  {ds}: no rows (HTTP {r.status_code})")
        continue
    head = lines[0].split(",")
    i_time, i_lat, i_lon = head.index("acq_time"), head.index("latitude"), head.index("longitude")
    hours = defaultdict(int)
    for row in lines[1:]:
        f = row.split(",")
        hhmm = f[i_time].zfill(4)                  # unpadded HHMM integer: 22 means 00:22
        hours[int(hhmm[:2])] += 1
    per_ds[ds] = (hours, len(lines) - 1)
    print(f"  {ds}: {len(lines)-1} detections")

print(f"\nDetections by UTC hour, {BAND[1]}-{BAND[3]}N / {BAND[0]}-{BAND[2]}E, {DAYS} days")
print(f"{'hUTC':>5} {'VIIRS/SNPP':>11} {'NOAA20':>8} {'NOAA21':>8} {'MODIS':>8}   total")
tot_by_hour = defaultdict(int)
for h in range(24):
    cells = []
    for ds in DATASETS:
        n = per_ds.get(ds, ({}, 0))[0].get(h, 0)
        cells.append(n)
        tot_by_hour[h] += n
    t = sum(cells)
    mark = "" if t else "   <- nothing overhead"
    print(f"{h:5d} {cells[0]:11d} {cells[1]:8d} {cells[2]:8d} {cells[3]:8d} {t:7d}{mark}")

empty = [h for h in range(24) if tot_by_hour[h] == 0]
print(f"\nUTC hours with zero polar detections anywhere in the band: {empty}")
if empty:
    runs, cur = [], [empty[0]]
    for h in empty[1:]:
        (cur.append(h) if h == cur[-1] + 1 else (runs.append(cur), cur := [h]))
    runs.append(cur)
    for r in runs:
        print(f"  contiguous: {r[0]:02d}:00-{r[-1]+1:02d}:00 UTC  ({len(r)} h)")
