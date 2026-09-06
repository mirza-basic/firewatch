"""Sentinel-2 at 10 m, rendered here and published as a picture.

Every other layer on the map is fetched by the reader's browser straight from a
keyless service. This one cannot be, and the reason is worth stating because it
looks like an over-complication until you hit it:

Sentinel Hub's OGC endpoints take their credential as an *instance id in the URL
path*. Any browser-side Sentinel-2 layer therefore writes that credential into
fire-map.html - which this project publishes to GitHub Pages and through ngrok.
The credential is quota-bearing, so that is not a theoretical leak: a reader of
the map could spend the month's processing units. There is also no keyless form;
a made-up instance id answers 400, not 200.

So the poller holds the token, renders one PNG per new scene, and the map places
it with `L.imageOverlay`. Nothing about the credential reaches the page. This is
also the only shape that works on GitHub Pages, where there is no server to
proxy through.

The image is requested in EPSG:3857 rather than 4326, which is the part that is
easy to get wrong and hard to see: `L.imageOverlay` stretches its image linearly
between two *projected* corners, so an equirectangular image drifts vertically
against the basemap - a few hundred metres over this municipality, enough to put
a fire on the wrong side of a ridge. Ask for Mercator and the stretch is the
identity.

Failure here is never fatal. Everything returns None and logs a warning, exactly
as `enrich` does: the 10 m layer is the one imagery tier the map does not need.
"""
from __future__ import annotations

import json
import logging
import math
import time

from . import geo, sources
from .config import CFG, SUPPORT_DIR, cdse_credentials

log = logging.getLogger("firewatch.imagery")

ODATA = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
TOKEN_URL = ("https://identity.dataspace.copernicus.eu/auth/realms/CDSE"
             "/protocol/openid-connect/token")
PROCESS = "https://sh.dataspace.copernicus.eu/api/v1/process"

# Sentinel Hub refuses a request over 2500 px on either side. The municipality is
# roughly 30 km across, so a full 10 m render would be ~3000 px and be rejected;
# 2500 gives ~13 m/px, which still resolves a burn scar of a few hectares.
MAX_PX = 2500

# Written beside fire-map.html in SUPPORT_DIR, *not* straight into PUBLIC_DIR.
# PUBLIC_DIR existing is what `sync_public` reads as "the user asked for this to
# be published", so creating it here would quietly start publishing the map to
# anyone holding the ngrok URL - a side effect of enabling a picture.
IMAGE_NAME = "fire-s2.png"

# B12/B11/B8A. The SWIR pair is what sees *through* smoke and what a burn scar is
# unambiguous in - a fresh scar goes deep red while healthy canopy stays green,
# and an actively burning front saturates. True colour shows the plume instead,
# which is prettier and much less diagnostic.
EVALSCRIPTS = {
    "swir": """//VERSION=3
function setup(){return {input:["B12","B11","B8A"],output:{bands:3}};}
function evaluatePixel(s){return [2.5*s.B12, 2.5*s.B11, 2.5*s.B8A];}""",
    "true": """//VERSION=3
function setup(){return {input:["B04","B03","B02"],output:{bands:3}};}
function evaluatePixel(s){return [2.5*s.B04, 2.5*s.B03, 2.5*s.B02];}""",
}

_token: tuple[float, str] | None = None      # (expires_at, access_token)


def _merc(lon: float, lat: float) -> tuple[float, float]:
    R = 6378137.0
    return (math.radians(lon) * R,
            math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * R)


def latest_scene(max_cloud: float | None = None) -> dict | None:
    """Newest Sentinel-2 L2A over the municipality under the cloud limit.

    Keyless - the CDSE catalogue needs no credential, only the *pixels* do. That
    matters: the "is there anything new?" question is asked every cycle and the
    expensive one only when the answer changes.
    """
    if max_cloud is None:
        max_cloud = float(CFG["imagery_s2_max_cloud"])
    w, s, e, n = geo.bbox_padded(CFG["nearby_buffer_km"])
    lon, lat = (w + e) / 2, (s + n) / 2
    since = time.strftime("%Y-%m-%dT00:00:00.000Z",
                          time.gmtime(time.time() - 30 * 86400))
    params = {
        "$filter": (
            "Collection/Name eq 'SENTINEL-2'"
            " and contains(Name,'MSIL2A')"
            f" and ContentDate/Start gt {since}"
            f" and OData.CSC.Intersects(area=geography'SRID=4326;POINT({lon:.4f} {lat:.4f})')"
        ),
        "$expand": "Attributes",
        "$orderby": "ContentDate/Start desc",
        "$top": "20",
    }
    try:
        r = sources._session().get(ODATA, params=params, timeout=sources._timeout())
        if r.status_code != 200:
            log.warning("sentinel-2 catalogue: HTTP %s", r.status_code)
            return None
        for v in r.json().get("value", []):
            at = {a.get("Name"): a.get("Value") for a in v.get("Attributes", [])}
            cloud = at.get("cloudCover")
            if cloud is None or float(cloud) > max_cloud:
                continue
            if v.get("Online") is False:
                continue          # archived to tape; a render would time out
            start = str(v.get("ContentDate", {}).get("Start", ""))
            if not start:
                continue
            return {"day": start[:10], "start": start,
                    "cloud": round(float(cloud), 1), "name": v.get("Name", "")}
    except Exception as exc:
        log.warning("sentinel-2 catalogue lookup failed: %s", exc)
    return None


def token() -> str | None:
    """A CDSE access token, cached until shortly before it expires."""
    global _token
    if _token and _token[0] > time.time():
        return _token[1]
    cid, sec, _src = cdse_credentials()
    if not cid or not sec:
        return None
    try:
        r = sources._session().post(
            TOKEN_URL,
            data={"grant_type": "client_credentials",
                  "client_id": cid, "client_secret": sec},
            timeout=sources._timeout())
        if r.status_code != 200:
            log.warning("cdse token: HTTP %s", r.status_code)
            return None
        j = r.json()
        tok = j.get("access_token")
        if not tok:
            return None
        # Tokens last ten minutes; retire ours a minute early so a render never
        # starts on one that expires mid-flight.
        _token = (time.time() + max(60, int(j.get("expires_in", 600)) - 60), tok)
        return tok
    except Exception as exc:
        log.warning("cdse token request failed: %s", exc)
        return None


def render(day: str, kind: str = "swir") -> dict | None:
    """Render one scene to PNG in PUBLIC_DIR. Returns the map's overlay record."""
    tok = token()
    if not tok:
        return None
    w, s, e, n = geo.bbox_padded(CFG["nearby_buffer_km"])
    x0, y0 = _merc(w, s)
    x1, y1 = _merc(e, n)
    span_x, span_y = x1 - x0, y1 - y0
    scale = MAX_PX / max(span_x, span_y)
    width = max(1, min(MAX_PX, round(span_x * scale)))
    height = max(1, min(MAX_PX, round(span_y * scale)))
    body = {
        "input": {
            "bounds": {
                "bbox": [x0, y0, x1, y1],
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/3857"},
            },
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": f"{day}T00:00:00Z", "to": f"{day}T23:59:59Z"},
                    "mosaickingOrder": "leastCC",
                },
            }],
        },
        "output": {"width": width, "height": height,
                   "responses": [{"identifier": "default",
                                  "format": {"type": "image/png"}}]},
        "evalscript": EVALSCRIPTS.get(kind, EVALSCRIPTS["swir"]),
    }
    try:
        r = sources._session().post(
            PROCESS, json=body, timeout=sources._timeout(),
            headers={"Authorization": f"Bearer {tok}"})
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
            log.warning("sentinel-2 render: HTTP %s %s", r.status_code,
                        r.text[:200] if not r.content[:4] == b"\x89PNG" else "")
            return None
        SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = SUPPORT_DIR / IMAGE_NAME
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(r.content)
        tmp.replace(out)
    except Exception as exc:
        log.warning("sentinel-2 render failed: %s", exc)
        return None
    return {"file": IMAGE_NAME, "day": day, "kind": kind,
            "bounds": [[s, w], [n, e]], "px": [width, height]}


def refresh(con, force: bool = False) -> dict | None:
    """Once per new scene: look, and render only if the answer changed.

    The catalogue call is keyless and cheap; the render costs processing units
    against a 10,000-a-month allowance. So the gate is the scene date, held in
    `meta`, not the cycle.
    """
    from . import store
    if not CFG.get("imagery_s2_enabled"):
        return None
    prev_raw = store.get_meta(con, "imagery_s2")
    prev = json.loads(prev_raw) if prev_raw else None
    scene = latest_scene()
    if not scene:
        return prev
    out = SUPPORT_DIR / IMAGE_NAME
    if (not force and prev and prev.get("day") == scene["day"]
            and out.exists()):
        return prev
    rec = render(scene["day"])
    if not rec:
        return prev
    rec["cloud"] = scene["cloud"]
    store.set_meta(con, "imagery_s2", json.dumps(rec))
    log.info("sentinel-2 %s rendered (%.1f%% cloud, %dx%d px)",
             rec["day"], rec["cloud"], rec["px"][0], rec["px"][1])
    return rec
