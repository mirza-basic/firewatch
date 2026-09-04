"""Does Meteosat MTG report FRP at night? Ask the feed, not the intuition.

Kept because the answer is counter-intuitive and the README now cites it. The
timing figure plots detections, and Meteosat's are all afternoon ones - which
invites the conclusion that it stops watching after dark. It does not: mid-IR
fire detection needs no daylight. Measured 2 September 2026, one five-hour window
of local solar night per box:

    Persian Gulf flares   1951 detections   6.6-186.1 MW
    Angola / Zambia       1306 detections   2.4-296.6 MW
    Iberia (wildfires)      43 detections   3.2-13.7 MW
    Niger Delta / Greece / Central Africa    0 - nothing burning hot enough

Run with the repository root on the path:

    PYTHONPATH=. python3 deploy/probe-mtg-night.py

Queries the same WFS layer the app uses, but over boxes of our choosing and only
during local solar night, so a hit is unambiguous. Landmine-compliant: attribute
predicates on Lat/Lon (never BBOX(), which returns 0 features with HTTP 200), and
windows well under the 48 h cost cliff.
"""
from datetime import datetime, timedelta, timezone
from firewatch import sources
from firewatch.store import iso

# Boxes inside the Meteosat disc (roughly 60W-60E), chosen for things that burn
# after dark: persistent gas flares, and wildfire regions in season.
BOXES = [
    ("Persian Gulf flares", 28.0, 33.0, 46.0, 50.0),
    ("Niger Delta flares",   4.0,  6.5,  5.0,  8.0),
    ("Iberia",              37.0, 43.5, -9.0, -2.0),
    ("Greece / W Turkey",   36.0, 41.5, 21.0, 30.0),
    ("Angola / Zambia",    -16.0,-10.0, 20.0, 28.0),
    ("Central Africa",       2.0,  9.0, 17.0, 27.0),
]

def night_window(lon_c, days_ago=1):
    """UTC window covering local solar 22:30-03:30 for this longitude."""
    solar_offset = timedelta(hours=lon_c / 15.0)
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    local_midnight = (base - timedelta(days=days_ago)).replace(hour=0) - solar_offset
    return local_midnight - timedelta(hours=1, minutes=30), local_midnight + timedelta(hours=3, minutes=30)

session = sources._session()
print(f"{'box':22} {'UTC window':34} {'feats':>5}  night detections (local solar h)")
print("-" * 104)
for name, s, n, w, e in BOXES:
    lon_c = (w + e) / 2
    start, end = night_window(lon_c)
    cql = (f"time AFTER {iso(start)} AND time BEFORE {iso(end)}"
           f" AND Lat BETWEEN {s:.4f} AND {n:.4f}"
           f" AND Lon BETWEEN {w:.4f} AND {e:.4f}")
    params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
              "typeNames": sources.MTG_LAYER, "outputFormat": "application/json",
              "count": "3000", "CQL_FILTER": cql}
    try:
        r = session.get(sources.WFS_URL, params=params, timeout=(10, 90))
        feats = r.json().get("features", []) if r.status_code == 200 else None
    except Exception as exc:
        print(f"{name:22} {'':34} {type(exc).__name__}")
        continue
    if feats is None:
        print(f"{name:22} {'':34} HTTP {r.status_code}")
        continue
    hours, frps, samples = {}, [], []
    for f in feats:
        p = f.get("properties", {})
        ts = p.get("time") or p.get("Datetime")
        if not ts:
            continue
        t = sources.parse_iso(ts)
        solar_h = (t.hour + t.minute / 60 + lon_c / 15.0) % 24
        hours[int(solar_h)] = hours.get(int(solar_h), 0) + 1
        frp = p.get("FRP")
        if frp:
            frps.append(float(frp))
        if len(samples) < 2:
            samples.append((ts, float(p["Lat"]), float(p["Lon"]), frp, p.get("Confidence")))
    win = f"{iso(start)[5:16]} .. {iso(end)[5:16]}"
    dist = " ".join(f"{h:02d}h:{c}" for h, c in sorted(hours.items()))
    print(f"{name:22} {win:34} {len(feats):5d}  {dist}")
    if frps:
        print(f"{'':22} FRP min {min(frps):.1f} max {max(frps):.1f} MW · e.g. {samples[0]}")
