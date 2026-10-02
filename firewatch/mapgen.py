"""Render the live fire map.

Two files are written side by side:

  fire-map.html       the page, with the current snapshot inlined for first paint
  fire-map-data.js    the same snapshot as `window.__fwData = {...}`

A file:// page cannot XHR a sibling JSON file under Chrome/Safari CORS rules, but it
*can* pull in a sibling script via a <script src> tag. So instead of reloading, the
page periodically appends a cache-busted script tag and re-renders from the assigned
object. That keeps the map view, the selected range, the selected fire and the
timeline position exactly where the reader left them, which a full reload would
discard. If script injection fails, it falls back to reloading.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import geo, imagery
from .config import (BOUNDARY_GEOJSON, MAP_PATH, PUBLIC_DIR, TOWN_LAT, TOWN_LON,
                     firebase_config)

# All 145 BiH municipality boundaries, drawn as one toggleable reference layer
# (see bih_municipalities_geojson()). Display only: it does not affect what a
# poll fetches or how detections are clipped.
BIH_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "bih"
BIH_MUNI_FILE = BIH_DATA_DIR / "municipalities.json"

# The loading screen's satellite/planet/HUD scene lives in its own file, so
# there is one copy to edit; the file's header comment explains its design.
SPLASH_SVG_FILE = Path(__file__).resolve().parent.parent / "docs" / "img" / "splash-screen.svg"


def _splash_svg() -> str:
    """The scene's own <svg>...</svg>, without the XML prolog and the
    standalone-file header comment, for splicing into the page.

    The opening tag is found by its xmlns attribute, not a bare "<svg": the
    header comment is free text that can itself contain "<svg>...</svg>" and
    comes earlier in the file."""
    text = SPLASH_SVG_FILE.read_text(encoding="utf-8")
    start = text.index('<svg xmlns=')
    return text[start:].strip()


TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FireWatch Bosna i Hercegovina</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%20512%20512%22%20width%3D%22512%22%20height%3D%22512%22%3E%0A%20%20%3C%21--%20Just%20the%20background%20and%20the%20burning%20tree%20-%20the%20detection-reticle%0A%20%20%20%20%20%20%20overlay%20and%20the%20wordmark%20%28see%20app-icon.svg%29%20both%20dropped%20out%20for%20this%0A%20%20%20%20%20%20%20variant%2C%20used%20wherever%20the%20icon%20renders%20small%20%28favicon%2C%20apple-touch-%0A%20%20%20%20%20%20%20icon%2C%20macOS%20menu%20bar%29.%20--%3E%0A%20%20%3Cdefs%3E%0A%20%20%20%20%3CradialGradient%20id%3D%22appBg%22%20cx%3D%2250%25%22%20cy%3D%2240%25%22%20r%3D%2260%25%22%3E%0A%20%20%20%20%20%20%3Cstop%20offset%3D%220%25%22%20stop-color%3D%22%231E293B%22%20/%3E%0A%20%20%20%20%20%20%3Cstop%20offset%3D%22100%25%22%20stop-color%3D%22%230F172A%22%20/%3E%0A%20%20%20%20%3C/radialGradient%3E%0A%20%20%20%20%3ClinearGradient%20id%3D%22fireGrad%22%20x1%3D%220%25%22%20y1%3D%22100%25%22%20x2%3D%220%25%22%20y2%3D%220%25%22%3E%0A%20%20%20%20%20%20%3Cstop%20offset%3D%220%25%22%20stop-color%3D%22%23EA580C%22%20/%3E%0A%20%20%20%20%20%20%3Cstop%20offset%3D%2250%25%22%20stop-color%3D%22%23EF4444%22%20/%3E%0A%20%20%20%20%20%20%3Cstop%20offset%3D%22100%25%22%20stop-color%3D%22%23FBBF24%22%20/%3E%0A%20%20%20%20%3C/linearGradient%3E%0A%20%20%20%20%3Cfilter%20id%3D%22coreGlow%22%20x%3D%22-20%25%22%20y%3D%22-20%25%22%20width%3D%22140%25%22%20height%3D%22140%25%22%3E%0A%20%20%20%20%20%20%3CfeGaussianBlur%20stdDeviation%3D%228%22%20result%3D%22blur%22%20/%3E%0A%20%20%20%20%20%20%3CfeComposite%20in%3D%22SourceGraphic%22%20in2%3D%22blur%22%20operator%3D%22over%22%20/%3E%0A%20%20%20%20%3C/filter%3E%0A%20%20%3C/defs%3E%0A%0A%20%20%3Crect%20width%3D%22512%22%20height%3D%22512%22%20rx%3D%22112%22%20fill%3D%22url%28%23appBg%29%22%20/%3E%0A%0A%20%20%3Cg%20transform%3D%22translate%28256%2C%20256%29%20scale%281.7%29%22%20filter%3D%22url%28%23coreGlow%29%22%3E%0A%20%20%20%20%3Cpath%20d%3D%22M%200%2C-75%20L%2022%2C-45%20L%2012%2C-45%20L%2032%2C-10%20L%2018%2C-10%20L%2045%2C35%20L%20-45%2C35%20L%20-18%2C-10%20L%20-32%2C-10%20L%20-12%2C-45%20L%20-22%2C-45%20Z%22%20fill%3D%22url%28%23fireGrad%29%22%20/%3E%0A%20%20%20%20%3Cpath%20d%3D%22M%200%2C-45%20L%2011%2C-25%20L%206%2C-25%20L%2016%2C5%20L%208%2C5%20L%2022%2C35%20L%20-22%2C35%20L%20-8%2C5%20L%20-16%2C5%20L%20-6%2C-25%20L%20-11%2C-25%20Z%22%20fill%3D%22%23FDE047%22%20opacity%3D%220.9%22%20/%3E%0A%20%20%20%20%3Cline%20x1%3D%220%22%20y1%3D%2235%22%20x2%3D%220%22%20y2%3D%22-15%22%20stroke%3D%22%230F172A%22%20stroke-width%3D%224%22%20stroke-linecap%3D%22round%22%20opacity%3D%220.9%22%20/%3E%0A%20%20%3C/g%3E%0A%3C/svg%3E%0A"/>
<link rel="apple-touch-icon" href="data:image/png;base64,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"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root{
    --bg:#0e1116; --panel:#151a21; --panel2:#1c232c; --line:#28313d;
    --fg:#e6edf3; --dim:#8b98a5; --accent:#ff6b35;
    --low:#ffd166; --moderate:#ff9f1c; --high:#ff6b35; --severe:#e63946; --quiet:#6b7785;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
    font:14px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif}
  /* Three heights on purpose: plain vh for ancient browsers, --appvh (set from
     window.innerHeight by JS) for Chrome/Safari without dvh, then dvh where it
     exists. position:relative makes this the anchor for #timebar. */
  #wrap{display:flex;position:relative;overflow:hidden;
    height:100vh;height:var(--appvh,100vh);height:100dvh}
  #side{width:370px;flex:0 0 370px;background:var(--panel);border-right:1px solid var(--line);
    display:flex;flex-direction:column}
  #map{flex:1;background:#0b0e12}
  header{padding:16px 18px 14px;border-bottom:1px solid var(--line)}
  h1{margin:0;font-size:15px;letter-spacing:.02em;display:flex;align-items:center;gap:8px}
  h1 .dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
  .sub{color:var(--dim);font-size:12px;margin-top:5px;font-variant-numeric:tabular-nums;
    transition:color .3s}
  .sub.flash{color:var(--accent)}
  .status{margin-top:12px;display:flex;gap:8px;flex-wrap:wrap}
  .seg{margin-top:12px;display:flex;background:var(--panel2);border:1px solid var(--line);
    border-radius:8px;padding:2px;gap:2px}
  .seg button{flex:1;background:none;border:0;color:var(--dim);font:inherit;font-size:11.5px;
    padding:6px 3px;border-radius:6px;cursor:pointer;transition:.13s;white-space:nowrap}
  .seg button:hover{color:var(--fg)}
  .seg button.on{background:var(--accent);color:#fff;font-weight:600}
  .seg button b{font-weight:700;font-variant-numeric:tabular-nums}
  .seg button.on b{color:#fff}
  /* The count sits under the range label: five buttons share one row, so
     side by side it would wrap or clip the number. */
  #hrange button b{display:block;font-size:10.5px;line-height:1.2;margin-top:1px}
  .chip{background:var(--panel2);border:1px solid var(--line);border-radius:999px;
    padding:4px 10px;font-size:11.5px;color:var(--dim)}
  .chip b{color:var(--fg);font-weight:600}
  #list{overflow-y:auto;flex:1;padding:10px}
  .ev{background:var(--panel2);border:1px solid var(--line);border-left-width:3px;
    border-radius:9px;padding:12px 13px;margin-bottom:9px;cursor:pointer;transition:.14s}
  .ev:hover{border-color:#3a4655;transform:translateX(2px)}
  .ev.sel{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
  .ev h2{margin:0 0 3px;font-size:13.5px;font-weight:600;display:flex;
    justify-content:space-between;gap:8px;align-items:baseline}
  .sev{font-size:10px;text-transform:uppercase;letter-spacing:.07em;padding:2px 7px;
    border-radius:4px;background:#2a323d;color:var(--dim);white-space:nowrap}
  .meta{color:var(--dim);font-size:12px;margin-top:6px;display:grid;
    grid-template-columns:auto 1fr;gap:2px 10px}
  .meta span:nth-child(odd){color:#6f7d8c}
  .spark{margin-top:9px;height:30px;width:100%;display:block}
  .acts{margin-top:10px;display:flex;gap:6px;flex-wrap:wrap}
  .acts a,.acts button{font-size:11.5px;text-decoration:none;color:var(--fg);
    background:#26303b;border:1px solid var(--line);border-radius:6px;padding:4px 9px;
    cursor:pointer;font-family:inherit}
  .acts a:hover,.acts button:hover{background:#31404f;border-color:#4a5866}
  footer{padding:10px 14px;border-top:1px solid var(--line);color:var(--dim);font-size:11px}
  .foot-link{color:var(--accent);text-decoration:none;border-bottom:1px solid transparent}
  .foot-link:hover,.foot-link:focus-visible{border-bottom-color:currentColor}
  .empty{text-align:center;color:var(--dim);padding:40px 20px}
  .empty .big{font-size:34px;margin-bottom:10px}
  /* Anchored to both edges of the map area rather than centred, so the ruler gets
     the full width available - 370px is the docked side panel. */
  #timebar{position:absolute;left:384px;right:14px;bottom:18px;z-index:1050;
    background:rgba(21,26,33,.94);border:1px solid var(--line);border-radius:11px;
    padding:10px 14px;display:flex;align-items:center;gap:11px;
    backdrop-filter:blur(9px)}
  #tlwrap{position:relative;flex:1 1 auto;min-width:0;height:40px}
  #tlscroll{position:absolute;inset:0;overflow-x:auto;overflow-y:hidden;
    -webkit-overflow-scrolling:touch;scrollbar-width:none;cursor:grab;
    border-radius:7px;background:#101720;border:1px solid var(--line)}
  #tlscroll::-webkit-scrollbar{display:none}
  #tlscroll:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
  #tltrack{position:relative;height:100%}
  #tlcontent{position:absolute;top:0;bottom:0}
  #tlmarks,#tlticks{position:absolute;inset:0}
  /* the moment being shown sits under this line, dead centre */
  #playhead{position:absolute;left:50%;top:-3px;bottom:-3px;width:2px;
    background:var(--accent);pointer-events:none;border-radius:2px;
    box-shadow:0 0 7px rgba(255,107,53,.7)}
  .tk{position:absolute;top:0;bottom:0;width:1px;background:#31404f}
  .tk.maj{background:#4a5866;width:1px}
  .tlab{position:absolute;top:3px;font-size:9.5px;color:var(--dim);
    white-space:nowrap;transform:translateX(-50%);pointer-events:none}
  .tmk{position:absolute;bottom:3px;width:3px;height:11px;border-radius:1px;
    opacity:.95}
  #tlabel{font-size:11.5px;color:var(--dim);min-width:132px;white-space:nowrap;
    font-variant-numeric:tabular-nums}
  #play{background:#26303b;border:1px solid var(--line);color:var(--fg);border-radius:6px;
    width:30px;height:26px;cursor:pointer;font-size:12px}
  #speed{background:#26303b;border:1px solid var(--line);color:var(--fg);border-radius:6px;
    height:26px;min-width:38px;padding:0 6px;cursor:pointer;font:inherit;font-size:11.5px;
    font-variant-numeric:tabular-nums}
  #unit{background:#26303b;border:1px solid var(--line);color:var(--fg);border-radius:6px;
    height:26px;min-width:44px;padding:0 7px;cursor:pointer;font:inherit;font-size:11.5px}
  #speed:hover,#play:hover,#unit:hover{background:#31404f}
  /* Collapsed by default at every width, not just on a phone: the outer .legend
     div is just a layout wrapper, and the toggle button / expanded body each
     carry their own card styling so only one of the two is ever on screen. */
  .legend{line-height:1.6}
  .legend-toggle{display:block;width:100%;max-width:min(74vw,300px);text-align:left;
    cursor:pointer;background:rgba(21,26,33,.94);border:1px solid var(--line);
    color:var(--fg);border-radius:9px;padding:8px 13px;font:inherit;font-size:12.5px;
    line-height:1.4;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
  .legend-body{display:none;background:rgba(21,26,33,.96);border:1px solid var(--line);
    border-radius:9px;padding:10px 12px;margin-bottom:6px;width:min(74vw,300px);
    max-height:46dvh;overflow-y:auto;color:var(--dim);font-size:11.5px;line-height:1.7;
    backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
  .legend.open .legend-body{display:block}
  .legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
  /* Fire-danger detail inside a municipality's popup - see fwiDetail() in mapgen.py. */
  .fwi-row{display:flex;justify-content:space-between;gap:14px}
  .fwi-row+.fwi-row{margin-top:1px}
  .fwi-codes,.fwi-note{opacity:.65;margin-top:6px;font-size:10.5px;line-height:1.5}
  /* This municipality's own subscribe link - see muniPopupHtml(). There is no
     single global banner: each municipality has its own channel (see
     poller._telegram_channel_url()). Accent-filled so it reads as the one
     clickable action in the popup besides close. */
  .muni-tg{display:flex;align-items:center;justify-content:center;gap:6px;
    background:var(--accent);color:#fff;text-decoration:none;font-weight:600;
    font-size:11.5px;border-radius:7px;padding:7px 8px;margin-top:8px;
    transition:filter .13s}
  .muni-tg:hover,.muni-tg:focus-visible{filter:brightness(1.08)}
  .leaflet-bar a.eye{display:flex;align-items:center;justify-content:center}
  .imgnote{background:rgba(21,26,33,.94);padding:6px 10px;border-radius:9px;
    border:1px solid var(--line);color:var(--dim);font-size:11.5px;line-height:1.5;
    max-width:230px}
  .imgnote b{color:#e6edf3;font-weight:600}
  .imgnote s{color:#f0a35e;text-decoration:none}
  .leaflet-bottom.leaflet-right,
  .leaflet-bottom.leaflet-left{margin-bottom:94px}
  /* Leaflet's popup pane defaults to z-index 700, below this page's fixed UI
     (langsw at 1250, the mobile drawer at 1300). Raised above all of it so a
     popup the reader just opened is never covered. Municipality info does not
     use this pane - see #muniinfo below. */
  .leaflet-popup-pane{z-index:1400}
  /* One shared fixed, centred panel for municipality info and fire events,
     instead of a Leaflet popup anchored to the click point (which can overlap
     the layer control, measure tool or eye toggle). Centred with padding on
     every side, so nothing else has to move or hide. Positioned like #langsw:
     centred over the map area, offset for the sidebar on desktop. */
  #muniinfo{position:fixed;top:50%;left:calc(50vw + 185px);transform:translate(-50%,-50%);
    z-index:1260;display:none;max-width:280px;width:calc(100% - 48px);
    max-height:70vh;overflow-y:auto;background:rgba(21,26,33,.97);
    border:1px solid var(--line);border-radius:10px;padding:14px 34px 14px 16px;
    box-shadow:0 10px 32px rgba(0,0,0,.45);backdrop-filter:blur(10px);
    -webkit-backdrop-filter:blur(10px);color:var(--fg);font-size:12.5px;line-height:1.5}
  #muniinfo.show{display:block}
  #muniinfo .miclose{position:absolute;top:8px;right:10px;width:24px;height:24px;
    border:0;background:none;color:var(--dim);font-size:19px;line-height:1;cursor:pointer;
    border-radius:5px}
  #muniinfo .miclose:hover,#muniinfo .miclose:focus-visible{background:var(--panel2);color:var(--fg)}
  /* First-paint cover. Above everything (including the popup pane at 1400)
     because it hides the map, sidebar and controls assembling at once.
     Removed after the inlined DATA is drawn (see the applyStaticLabels()/
     recompute() call near the end of the script); it never waits on the
     network, since the page ships with a snapshot inlined. */
  #loading{position:fixed;inset:0;z-index:2000;
    background:radial-gradient(ellipse at 50% 42%,#0d1626 0%,#080b14 55%,var(--bg) 80%);
    transition:opacity .6s ease,visibility 0s linear .6s}
  #loading.hide{opacity:0;visibility:hidden;pointer-events:none}
  .ldwrap{position:absolute;inset:0;color:var(--fg)}
  /* The scene is one self-contained SVG (planet horizon, treeline, satellite,
     scanning HUD) with its own keyframes, filling the screen with no letterbox
     bars (preserveAspectRatio="slice"). Bars are not free: an animated layer
     compositing at the seam between the SVG and the page background measured
     more expensive. The 1200x800 viewBox is close to a real window's aspect
     ratio, so slice crops little. */
  .ldscene{position:absolute;inset:0;overflow:hidden}
  .ldscene svg{display:block;width:100%;height:100%}
  .ldsub{position:absolute;right:16px;bottom:38px;color:var(--dim);
    font-size:12px;letter-spacing:.02em;text-align:right}
  .ldsig{position:absolute;right:16px;bottom:16px;color:var(--dim);
    font-size:11px;letter-spacing:.03em;opacity:.7;text-align:right}
  .leaflet-popup-content-wrapper{background:var(--panel);color:var(--fg);border-radius:9px}
  .leaflet-popup-tip{background:var(--panel)}
  .leaflet-popup-content{margin:11px 13px;font-size:12.5px}
  .leaflet-popup-content b{color:var(--accent)}
  .leaflet-popup-content a{color:#7cc4ff}
  .leaflet-bar a{background:var(--panel2);color:var(--fg);border-color:var(--line)}
  .leaflet-bar a:hover{background:#31404f}
  /* Layers control dressed as the legend: a labelled button that opens a card on
     click (not hover), sharing .legend-toggle / .legend-body so the two panels
     cannot drift apart. Leaflet's own toggle icon is hidden and its list is shown
     only while .open - the extra :not(.open) keeps the selector above Leaflet's
     own "expanded" rule. Opens downward, since the control sits top-right. */
  .leaflet-control-layers.layersctl{background:transparent;border:0;box-shadow:none;
    padding:0;color:var(--fg)}
  .layersctl .leaflet-control-layers-toggle{display:none}
  .layersctl .legend-toggle{margin:0 0 6px auto;width:auto;min-width:0}
  .layersctl:not(.open) .leaflet-control-layers-list{display:none}
  .layersctl.open .leaflet-control-layers-list{display:block;margin:0;
    background:rgba(21,26,33,.96);border:1px solid var(--line);border-radius:9px;
    padding:8px 10px;width:min(74vw,300px);max-height:60dvh;overflow-y:auto;
    color:var(--dim);font-size:11.5px;box-shadow:none;
    backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
  .layersctl label{display:block;margin:0}
  .layersctl label>span{display:flex;align-items:center;gap:8px;padding:4px 4px;
    border-radius:5px;cursor:pointer;font-size:12px;line-height:1.5;color:var(--fg)}
  .layersctl label>span:hover{background:rgba(255,255,255,.07)}
  .layersctl .leaflet-control-layers-selector{margin:0;width:14px;height:14px;
    flex:none;accent-color:var(--accent);cursor:pointer;position:static;top:auto}
  .layersctl .leaflet-control-layers-separator{height:1px;border:0;margin:6px 0;
    background:var(--line)}
  /* --- measure tool -------------------------------------------------------
     While measuring, the fire markers and the boundary must not swallow a click
     that is meant to drop a vertex, so hit testing is turned off for the panes
     that hold them. The measure pane is a sibling of the overlay pane, hence its
     own rule - finished measurements are clickable (for their remove popup) only
     when the tool is off. muniLayer needs its own selector: it renders on
     L.canvas rather than SVG paths (145 boundaries - see its own comment), so it
     is a single <canvas> element the `path` rule below never matches, and
     without this line a click meant for a vertex opens a municipality popup
     instead. */
  .leaflet-container.measuring{cursor:crosshair}
  /* !important is load-bearing: Leaflet's own
     `.leaflet-pane>svg path.leaflet-interactive` rule is more specific than this
     one, so without it a click meant for a vertex opens a fire popup instead. */
  .measuring .leaflet-overlay-pane path,
  .measuring .leaflet-overlay-pane canvas,
  .measuring .leaflet-marker-pane,
  .measuring .leaflet-measure-pane path{pointer-events:none!important}
  .mbar a.on{background:var(--accent);color:#fff;border-color:var(--accent)}
  .mbar a.on:hover{background:var(--accent)}
  .mlabel.leaflet-tooltip{background:rgba(21,26,33,.92);border:1px solid var(--line);
    color:var(--fg);border-radius:6px;padding:2px 7px;font-size:11px;font-weight:600;
    white-space:nowrap;font-variant-numeric:tabular-nums;box-shadow:none;text-align:center}
  .mlabel.leaflet-tooltip:before{display:none}
  .mlabel.seg{font-weight:400;font-size:10px;color:var(--dim);padding:1px 5px}
  .mlabel.live{border-color:var(--accent)}
  .mpanel{background:rgba(21,26,33,.94);border:1px solid var(--line);border-radius:9px;
    padding:9px 11px;font-size:11px;color:var(--dim);width:206px;line-height:1.4;
    backdrop-filter:blur(9px);-webkit-backdrop-filter:blur(9px)}
  .mpanel .mrow{color:var(--fg);font-size:14px;font-weight:600;margin-bottom:5px;
    font-variant-numeric:tabular-nums}
  .mpanel .mrow:empty{display:none}
  .macts{display:flex;gap:5px;margin-top:8px}
  .macts button{flex:1;background:#26303b;border:1px solid var(--line);color:var(--fg);
    border-radius:6px;padding:4px 6px;font:inherit;font-size:11.5px;cursor:pointer}
  .macts button:hover:enabled{background:#31404f}
  .macts button:disabled{opacity:.42;cursor:default}
  .macts button.pri:enabled{background:var(--accent);border-color:var(--accent);
    color:#fff;font-weight:600}
  .mpop b{color:var(--accent)}
  .mpop button{margin-top:8px;background:#26303b;border:1px solid var(--line);
    color:var(--fg);border-radius:6px;padding:4px 9px;font:inherit;font-size:11.5px;
    cursor:pointer}
  .mpop button:hover{background:#31404f}
  /* Centred over #map, not the viewport: #side docks at a fixed 370px on desktop,
     so viewport-centre (50vw) sits 185px left of the map's own visual centre. On
     a phone #side is an off-canvas overlay that takes no layout width, so #map
     already spans the full viewport and plain 50vw is correct there - restored
     in the mobile media query below. */
  #langsw{position:fixed;top:10px;left:calc(50vw + 185px);transform:translateX(-50%);
    z-index:1250;display:flex;gap:2px;padding:2px;border-radius:9px;
    border:1px solid var(--line);background:rgba(21,26,33,.94);
    backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
    transition:opacity .15s}
  /* Leaflet's popup pane lives inside .leaflet-map-pane, whose pan transform
     makes it the containing block, so no z-index on .leaflet-popup-pane can
     paint above a sibling of .leaflet-map-pane with its own z-index (Leaflet's
     corner containers, #timebar, #langsw...). Only a single raw detection and
     the measure tool still use an anchored popup; both are small and out of
     the way, so the rare overlap is accepted. Municipality info and fire
     events use #muniinfo instead. */
  #langsw button{background:none;border:0;color:var(--dim);font:inherit;font-size:11.5px;
    font-weight:600;letter-spacing:.03em;padding:5px 11px;border-radius:7px;cursor:pointer}
  #langsw button:hover{color:var(--fg)}
  #langsw button.on{background:var(--accent);color:#fff}
  /* Scales from the ring's own centre, so it starts at the marker's edge and
     expands past it whatever the marker's size. */
  .pulse{transform-box:fill-box;transform-origin:center;
         animation:pulse 1.8s ease-out infinite}
  @keyframes pulse{0%{transform:scale(1);opacity:1}100%{transform:scale(2.1);opacity:0}}
  /* Desktop keeps the panel docked; these two are only used on small screens. */
  #drawer-btn{display:none}
  #drawer-close{display:none}
  #backdrop{display:none}

  /* Phone/tablet: the panel becomes an off-canvas drawer so the map gets the
     whole screen. min-height:0 stops a flex item being floored at its content
     height. */
  @media (max-width:880px){
    #side{position:fixed;top:0;left:0;height:100%;height:100dvh;width:min(86vw,340px);
      flex:0 0 auto;min-height:0;z-index:1200;transform:translateX(-102%);
      transition:transform .26s ease;box-shadow:0 0 42px rgba(0,0,0,.55)}
    #side.open{transform:none}
    #map{flex:1 1 auto;min-height:0}
    /* #side is an off-canvas overlay here, not docked, so #map already spans the
       full viewport width - the desktop offset above would be wrong here. */
    #langsw{left:50%}
    #muniinfo{left:50%}
    #drawer-btn{display:flex;align-items:center;gap:7px;position:fixed;top:10px;left:10px;
      z-index:1300;background:rgba(21,26,33,.95);color:var(--fg);border:1px solid var(--line);
      border-radius:9px;padding:9px 12px;font:inherit;font-size:14px;cursor:pointer;
      backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
    #drawer-btn:active{background:#26303b}
    /* The toggle is fixed above the drawer, so it would sit on top of the panel
       header and cover the title. The drawer has its own close button. */
    body.drawer-open #drawer-btn,
    body.drawer-open #langsw{display:none}
    #drawer-badge:not(:empty){font-size:12px;font-weight:700;color:var(--accent)}
    #drawer-close{display:block;position:absolute;top:10px;right:10px;background:#26303b;
      border:1px solid var(--line);color:var(--fg);border-radius:7px;width:30px;height:30px;
      font-size:16px;line-height:1;cursor:pointer;padding:0}
    header{padding:12px 46px 11px 14px}
    #backdrop{display:block;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:1100;
      opacity:0;pointer-events:none;transition:opacity .26s}
    #backdrop.on{opacity:1;pointer-events:auto}
    /* keep Leaflet's own controls clear of the drawer button */
    .leaflet-top.leaflet-left{margin-top:54px}
    /* the drawer is off-canvas, so the map is full width here */
    #timebar{left:11px;right:11px;padding:8px 11px;gap:8px;flex-wrap:wrap;
      bottom:calc(11px + env(safe-area-inset-bottom, 0px))}
    #tlabel{min-width:0;font-size:11px;flex:1 1 auto}
    /* the ruler gets its own full-width row; beside the buttons it is too
       narrow to scrub */
    #tlwrap{order:9;flex:1 1 100%;margin-top:3px}
    #list{padding:8px}
    .mpanel{width:min(52vw,190px);padding:8px 9px}
    /* Lift the bottom control stack clear of the timeline bar; the legend
       collapses to a single "Key" button so it never permanently covers a
       phone-sized map. */
    .leaflet-bottom.leaflet-right,
    .leaflet-bottom.leaflet-left{
      margin-bottom:calc(124px + env(safe-area-inset-bottom, 0px))}
    /* attribution must stay visible, but it can be smaller on a phone */
    .leaflet-control-attribution{font-size:9.5px;padding:1px 5px}
  }
  @media (prefers-reduced-motion:reduce){#side,#backdrop{transition:none}}
</style></head><body>
<div id="loading" role="status" aria-live="polite">
  <div class="ldwrap">
    <div class="ldscene" aria-hidden="true">
      <!-- The scene (planet horizon, satellite, HUD) lives in
           docs/img/splash-screen.svg and is spliced in here by render();
           that file's header comment documents its design. -->
      __SPLASH_SVG__
    </div>
    <div class="ldsub">Loading map&hellip;</div>
    <div class="ldsig">Created by Mirza Basic</div>
  </div>
</div>
<button id="drawer-btn" aria-controls="side" aria-expanded="false" aria-label="Show fire list">
  <span aria-hidden="true">&#9776;</span><span id="drawer-label">Fires</span><span id="drawer-badge"></span>
</button>
<div id="backdrop"></div>
<div id="langsw" role="group" aria-label="Language / Jezik">
  <button data-l="bs" type="button">BS</button><button data-l="en" type="button">EN</button>
</div>
<div id="muniinfo" role="dialog"><button class="miclose" aria-label="Close" type="button">&times;</button><div id="muniinfo-body"></div></div>
<div id="wrap">
  <div id="side">
    <header>
      <button id="drawer-close" aria-label="Close fire list">&times;</button>
      <h1><span class="dot" id="hdot"></span><span id="htitle">FireWatch Bosna i Hercegovina</span></h1>
      <div class="sub" id="hsub"></div>
      <div class="seg" id="hrange"></div>
      <div class="status" id="hchips"></div>
    </header>
    <div id="list"></div>
    <footer id="foot"></footer>
  </div>
  <div id="map"></div>
  <div id="timebar">
    <button id="play" title="Animate">&#9654;</button>
    <button id="speed" title="Playback speed">1&times;</button>
    <button id="unit" title="Timeline step">hour</button>
    <!-- Scrollable time ruler. The moment under the centre playhead is "now
         showing"; the track is rebuilt whenever the range or step unit changes. -->
    <div id="tlwrap">
      <div id="tlscroll" tabindex="0" role="slider" aria-label="Timeline">
        <div id="tltrack"><div id="tlcontent"><div id="tlmarks"></div>
          <div id="tlticks"></div></div></div>
      </div>
      <div id="playhead" aria-hidden="true"></div>
    </div>
    <span id="tlabel"></span>
  </div>
</div>
<script>
// Usage analytics (Firebase / GA4). fwTrack exists either way so call sites need no
// guard; it queues until the SDK has loaded and is a no-op when there is no config.
// file:// is skipped because Firebase Analytics needs http(s) + IndexedDB and would
// only log a console error. Nothing here touches applyData's 60 s refresh: page_view
// fires once per visit and every other event is a deliberate reader action.
const FW_FIREBASE = __FIREBASE__;
const fwQueue = [];
let fwLog = null;
function fwTrack(name, params){
  if(!FW_FIREBASE) return;
  if(fwLog) fwLog(name, params); else fwQueue.push([name, params]);
}
if(FW_FIREBASE && /^https?:$/.test(location.protocol)){
  const V = "11.0.2", B = "https://www.gstatic.com/firebasejs/" + V + "/";
  Promise.all([import(B + "firebase-app.js"), import(B + "firebase-analytics.js")])
    .then(([app, an]) => an.isSupported().then(ok => {
      if(!ok) return;
      const a = an.getAnalytics(app.initializeApp(FW_FIREBASE));
      fwLog = (n, p) => an.logEvent(a, n, p);
      fwQueue.splice(0).forEach(q => fwLog(q[0], q[1]));
    }))
    .catch(() => { /* blocked by an ad blocker or offline: analytics is optional */ });
}
</script>
<script>
// Captured before anything else runs, so the loading screen's minimum display
// time (LOAD_MIN_MS) is measured from first paint; otherwise a fast machine
// would flash the animation for a few milliseconds.
const LOAD_START = Date.now();

let DATA = __DATA__;
const DATA_URL = "__DATA_JS__";
const BOUNDARY = __BOUNDARY__;
const BUFFER = __BUFFER__;
const BIH_MUNICIPALITIES = __BIH_MUNICIPALITIES__;
// Substituted from config so the sun test below cannot disagree with it about
// where the town is.
const TOWN_LAT = __TOWN_LAT__, TOWN_LON = __TOWN_LON__;
const SRC = {mtg:{c:"#4cc9f0",n:"Meteosat MTG (10 min)"},
             firms:{c:"#ffd166",n:"VIIRS/MODIS (NRT)"},
             s3:{c:"#b5179e",n:"Sentinel-3 SLSTR"}};
const SEVC = {low:"#ffa726",moderate:"#fb8c00",high:"#f4511e",severe:"#d81b3c",unknown:"#8b98a5"};

// ---- localisation ----------------------------------------------------------
// Bosnian plurals need three forms, so counted strings are arrays [one,few,many]
// and plural() picks by the Slavic rule. English uses [one, other, other].
const I18N = {
  en: {
    sub:"Bosnia and Herzegovina · updated {t}", noActive:"No active fires",
    activeFires:["{n} active fire","{n} active fires","{n} active fires"],
    firesNoneActive:["{n} fire, none active","{n} fires, none active","{n} fires, none active"],
    detections:"detections", ok:"ok", fail:"fail",
    drawerFires:"Fires", showList:"Show fire list", closeList:"Close fire list",
    key:"Key", layers:"Layers", legDet:"Detections", legSev:"Fire severity", legSize:"(size ∝ FRP)",
    legState:"State", legBurning:"solid & filled — burning now",
    legQuiet:"dashed — quiet / out",
    sev_low:"low", sev_moderate:"moderate", sev_high:"high", sev_severe:"severe",
    sev_unknown:"unknown", st_active:"active", st_quiet:"quiet",
    noFires:"No fires · {range}",
    nothing:"Nothing detected in Bosnia and Herzegovina or within {km} km of its border in this period.",
    peak:"peak", now:"now", latest:"latest", lastSeen:"Last seen", started:"Started",
    extent:"Extent", weather:"Weather", note:"Note", outside:"outside municipality",
    ofN:"of {n}", across:"across",
    placeOf:"{km} km {dir} of {name}",
    spread:"{risk} spread risk", risk_elevated:"elevated", risk_high:"high",
    risk_extreme:"extreme", risk_moderate:"moderate", risk_unknown:"unknown",
    satellite:"Satellite", copyCoords:"Copy coords", copied:"copied",
    discoveredBy:"Reported by", savedAt:"saved",
    wind:"wind", from:"from", gusts:"gusts", rh:"RH",
    noDetRange:"no detections in range", boundary:"boundary",
    lMuni:"BiH municipalities",
    docs:"Documentation", telegramSub:"Alerts on Telegram",
    r_24h:"Last 24h", r_3d:"Last 3 days", r_7d:"Last 7 days", r_30d:"Last month",
    r_1y:"Last year",
    rs_24h:"24h", rs_3d:"3 days", rs_7d:"7 days", rs_30d:"Month", rs_1y:"Year",
    justNow:"just now", agoMin:"{n} min ago", agoH:"{n} h ago", agoD:"{n} d ago",
    animate:"Animate", pause:"Pause", speed:"Playback speed",
    step:"Timeline step", u_min:"min", u_hour:"hour", u_day:"day", u_week:"week",
    zoomFires:"Zoom to fires", lMap:"Map", lSat:"Satellite", lTopo:"Terrain",
    hideFires:"Hide fire markers", showFires:"Show fire markers",
    lNone:"No basemap",
    imFire:"Meteosat fire temperature", imGeo:"Meteosat GeoColour",
    imTrue:"Meteosat true colour", imHrfi:"Meteosat visible 0.6 km",
    imIr:"Meteosat thermal IR", imViirs:"VIIRS true colour 250 m",
    imSwir:"VIIRS fire bands 375 m", imHls:"Sentinel-2 / Landsat 30 m",
    imLatest:"latest frame", imToday:"today, may still be filling in",
    imS2:"Sentinel-2 10 m (latest scene)", imCloud:"cloud",
    imDark:"dark here now — try fire temperature",
    imNoScene:"no scene imaged on this date",
    imNoArchive:"nothing in the archive this far back",
    imNoFrame:"no frame published for this hour",
    legImagery:"Imagery", legImNote:"fire temperature stacks on any one other layer",
    m_dist:"Measure distance", m_area:"Measure area", m_clear:"Clear measurements",
    m_hintDist:"Click the map to add points. Double-click, or Done, to finish.",
    m_hintArea:"Click round the area you want. Double-click, or Done, to close it.",
    m_needDist:"one more point", m_needArea:"at least three points",
    m_done:"Done", m_undo:"Undo", m_close:"Close", m_remove:"Remove",
    m_perimeter:"perimeter",
    lBuffer:"{km} km buffer", legZone:"Watched area",
    zoneNote:"kept, flagged nearby",
    fwTitle:"Fire danger", fwBadge:"Fire danger today: {cls}", fwToday:"Today",
    fwLow:"Low", fwModerate:"Moderate", fwHigh:"High", fwVeryHigh:"Very High",
    fwExtreme:"Extreme", fwVeryExtreme:"Very Extreme",
    fwNote:"Canadian Fire Weather Index, from Open-Meteo weather at the "+
      "municipality centre — not a satellite reading. Forecast beyond a "+
      "few days grows less reliable.",
    fwUpdated:"updated {t}"
  },
  bs: {
    sub:"Bosna i Hercegovina · ažurirano {t}", noActive:"Nema aktivnih požara",
    activeFires:["{n} aktivan požar","{n} aktivna požara","{n} aktivnih požara"],
    firesNoneActive:["{n} požar, nijedan aktivan","{n} požara, nijedan aktivan",
                     "{n} požara, nijedan aktivan"],
    detections:"detekcije", ok:"ok", fail:"greška",
    drawerFires:"Požari", showList:"Prikaži listu požara", closeList:"Zatvori listu",
    key:"Legenda", layers:"Slojevi", legDet:"Detekcije", legSev:"Jačina požara", legSize:"(veličina ∝ FRP)",
    legState:"Stanje", legBurning:"puna linija — trenutno gori",
    legQuiet:"crtkano — mirno / ugašeno",
    sev_low:"nizak", sev_moderate:"umjeren", sev_high:"visok", sev_severe:"ekstreman",
    sev_unknown:"nepoznato", st_active:"aktivan", st_quiet:"mirno",
    noFires:"Nema požara · {range}",
    nothing:"Ništa nije detektovano u Bosni i Hercegovini niti u krugu od {km} km od granice u ovom periodu.",
    peak:"maks.", now:"sada", latest:"zadnje", lastSeen:"Zadnje viđeno", started:"Počelo",
    extent:"Raspon", weather:"Vrijeme", note:"Napomena", outside:"izvan općine",
    ofN:"od {n}", across:"u širini",
    placeOf:"{km} km {dir} od {name}",
    spread:"rizik širenja: {risk}", risk_elevated:"povišen", risk_high:"visok",
    risk_extreme:"ekstreman", risk_moderate:"umjeren", risk_unknown:"nepoznat",
    satellite:"Satelit", copyCoords:"Kopiraj koordinate", copied:"kopirano",
    discoveredBy:"Prvi prijavio", savedAt:"sačuvano",
    wind:"vjetar", from:"iz", gusts:"udari", rh:"vlaga",
    noDetRange:"nema detekcija u periodu", boundary:"granica",
    lMuni:"Općine BiH",
    docs:"Dokumentacija", telegramSub:"Obavijesti na Telegramu",
    r_24h:"Zadnja 24h", r_3d:"Zadnja 3 dana", r_7d:"Zadnjih 7 dana", r_30d:"Zadnji mjesec",
    r_1y:"Zadnja godina",
    rs_24h:"24h", rs_3d:"3 dana", rs_7d:"7 dana", rs_30d:"Mjesec", rs_1y:"Godina",
    justNow:"upravo sad", agoMin:"prije {n} min", agoH:"prije {n} h", agoD:"prije {n} d",
    animate:"Animiraj", pause:"Pauza", speed:"Brzina reprodukcije",
    step:"Korak vremenske ose", u_min:"min", u_hour:"sat", u_day:"dan", u_week:"sedm.",
    zoomFires:"Približi na požare", lMap:"Karta", lSat:"Satelit", lTopo:"Teren",
    hideFires:"Sakrij oznake požara", showFires:"Prikaži oznake požara",
    lNone:"Bez karte",
    imFire:"Meteosat temperatura vatre", imGeo:"Meteosat GeoColour",
    imTrue:"Meteosat prave boje", imHrfi:"Meteosat vidljivi 0,6 km",
    imIr:"Meteosat termalni IR", imViirs:"VIIRS prave boje 250 m",
    imSwir:"VIIRS kanali vatre 375 m", imHls:"Sentinel-2 / Landsat 30 m",
    imLatest:"najnoviji snimak", imToday:"danas, možda još nije potpun",
    imS2:"Sentinel-2 10 m (zadnji snimak)", imCloud:"oblačnost",
    imDark:"ovdje je mrak — probaj temperaturu vatre",
    imNoScene:"za ovaj datum nema snimka",
    imNoArchive:"arhiva ne ide tako daleko",
    imNoFrame:"za ovaj sat nema snimka",
    legImagery:"Snimci", legImNote:"temperatura vatre ide preko bilo kojeg drugog sloja",
    m_dist:"Izmjeri udaljenost", m_area:"Izmjeri površinu", m_clear:"Obriši mjerenja",
    m_hintDist:"Klikni po karti da dodaš točke. Dvoklik ili Gotovo za završetak.",
    m_hintArea:"Klikni oko površine koju mjeriš. Dvoklik ili Gotovo da se zatvori.",
    m_needDist:"još jedna točka", m_needArea:"najmanje tri točke",
    m_done:"Gotovo", m_undo:"Vrati", m_close:"Zatvori", m_remove:"Ukloni",
    m_perimeter:"obim",
    lBuffer:"pojas {km} km", legZone:"Praćeno područje",
    zoneNote:"prati se, označeno kao blizu",
    fwTitle:"Opasnost od požara", fwBadge:"Opasnost od požara danas: {cls}",
    fwToday:"Danas",
    fwLow:"Nizak", fwModerate:"Umjeren", fwHigh:"Visok", fwVeryHigh:"Vrlo visok",
    fwExtreme:"Ekstreman", fwVeryExtreme:"Vrlo ekstreman",
    fwNote:"Kanadski indeks požarne opasnosti (FWI), iz Open-Meteo podataka za "+
      "centar općine — nije satelitsko očitanje. Prognoza nakon nekoliko dana "+
      "postaje manje pouzdana.",
    fwUpdated:"ažurirano {t}"
  }
};
// Compass points are computed server-side in English; translate the letters.
const COMPASS_BS = {N:"S",NNE:"SSI",NE:"SI",ENE:"ISI",E:"I",ESE:"IJI",SE:"JI",SSE:"JJI",
  S:"J",SSW:"JJZ",SW:"JZ",WSW:"ZJZ",W:"Z",WNW:"ZSZ",NW:"SZ",NNW:"SSZ"};

// Bosnian by default: this map covers Bosnia and Herzegovina, and the people who
// need it in an emergency read Bosnian. English is one click away in the header.
// A reader's own choice still wins - the toggle writes fw_lang and that is checked
// first - so switching to English is remembered on that device.
let LANG = "bs";
try {
  const saved = localStorage.getItem("fw_lang");
  if(saved && I18N[saved]) LANG = saved;
} catch(e) { /* file:// and private windows can block storage; stay with the default */ }

// Index into a counted string's [one, few, many] array for n in the current language.
function plural(n){
  if(LANG !== "bs") return n === 1 ? 0 : 1;
  const a = Math.abs(n) % 100, b = a % 10;
  if(b === 1 && a !== 11) return 0;
  if(b >= 2 && b <= 4 && !(a >= 12 && a <= 14)) return 1;
  return 2;
}
// Translate a key, falling back to English, picking the plural form by vars.n and
// substituting {name} placeholders from vars.
function t(key, vars){
  let v = (I18N[LANG] || I18N.en)[key];
  if(v === undefined) v = I18N.en[key];
  if(Array.isArray(v)) v = v[plural(vars && vars.n != null ? vars.n : 1)] || v[0];
  if(vars) for(const k in vars) v = v.split("{"+k+"}").join(vars[k]);
  return v;
}
const dir = d => LANG === "bs" ? (COMPASS_BS[d] || d) : d;

// Bosnian wants the genitive after "od". Names ending in -a take -e, which covers
// most settlements around here (Kamenica -> Kamenice, Vozuća -> Vozuće). Anything
// else is left as-is rather than guessed at.
function genitive(name){
  if(LANG !== "bs" || !name) return name;
  return /a$/.test(name) ? name.slice(0, -1) + "e" : name;
}
// Human-readable location of an event: "N km <dir> of <settlement>".
function placeOf(e){
  const p = e.place_parts;
  if(!p || !p.name) return e.place;                 // older snapshot: use the server text
  if(p.km == null) return p.name;                   // sitting on the settlement itself
  return t("placeOf", {km:p.km, dir:dir(p.dir), name:genitive(p.name)});
}

// Municipality names for an event's tagged ids (two for the deliberately
// overlapping Sarajevo/Istočno Sarajevo polygons), from the layer data already
// on the page. Empty when the event is outside every municipality.
const MUNI_NAME = {};
((BIH_MUNICIPALITIES && BIH_MUNICIPALITIES.features) || []).forEach(
  f=>{ MUNI_NAME[f.properties.id] = f.properties.name; });
function muniOf(e){
  return (e.municipalities||[]).map(id=>MUNI_NAME[id]).filter(Boolean).join(" / ");
}

// ---- range selection -------------------------------------------------------
// Cutoffs are computed server-side so every client agrees on the window edges
// regardless of the viewer's own clock or timezone.
let RANGE = DATA.default_range || "3d";
if(!DATA.range_cutoffs || !DATA.range_cutoffs[RANGE]) RANGE = "3d";
const cutoffOf = r => Date.parse(DATA.range_cutoffs[r]);

let EVENTS = [], dets = [], tMin = 0, tMax = 0;

// Rebuild EVENTS, dets and the time span from DATA for the selected range.
function recompute(){
  const c = cutoffOf(RANGE);
  EVENTS = (DATA.events||[])
    .filter(e => Date.parse(e.last_ts) >= c)
    .map(e => ({...e, series:(e.series||[]).filter(s => Date.parse(s.ts) >= c)}));
  dets = [];
  EVENTS.forEach(e => e.series.forEach(s => dets.push({...s, ev:e.id, t:Date.parse(s.ts)})));
  dets.sort((a,b)=>a.t-b.t);
  // Span the whole selected range, not just the period that happens to contain
  // detections. A quiet week should read as a quiet week, not collapse the
  // timeline to the one afternoon something burned.
  tMin = c;
  tMax = Math.max(Date.now(), dets.length ? dets[dets.length - 1].t : 0);
  if(autoUnit) unitIx = pickUnit(tMax - tMin);
  clampUnit();
  rebuildSlider();
  if(selected && !EVENTS.some(e=>e.id===selected)) selected = null;
}

const map = L.map("map",{zoomControl:true,attributionControl:true}).setView([44.386,18.276],10);
map.attributionControl.setPrefix("");   // keep provider credits, drop Leaflet branding
const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  {maxZoom:19,attribution:"&copy; OpenStreetMap"});
const sat = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  {maxZoom:19,attribution:"Esri, Maxar, Earthstar Geographics"});
const topo = L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
  {maxZoom:17,attribution:"&copy; OpenTopoMap (CC-BY-SA)"});
sat.addTo(map);          // satellite is the default: terrain and fuel are visible

// "No basemap": an empty group that Leaflet treats as a base layer and that draws
// nothing, leaving #map's near-black background so the fire layers stand alone.
// It reads especially well under rgb_firetemperature, whose screen blend over
// near-black leaves the hotspots at full strength.
//
// A base layer rather than a checkbox because Leaflet's base layers are a radio
// group: one is always chosen, so there is no way to untick your way to nothing.
const blank = L.layerGroup();

// ------------------------------------------------------------------ imagery
// All three basemaps above are archival (Esri World Imagery is months to years
// old) and cannot show a fire burning now. These can; the split between the two
// providers is about what a small fire looks like from orbit.
//
// Meteosat is ~1.7 x 1.3 km per pixel here: Bosnia and Herzegovina sits at a
// ~54 degree viewing zenith from 0E, which inflates FCI's 1 km nadir figure by
// 1/cos, about the same across the whole country. That cannot resolve a
// few-hectare fire, but it lands every 10 minutes, the only cadence that shows a
// plume while the fire still burns. GIBS polar imagery is 250 m and can show a
// plume, but arrives 4-5 h late and once per satellite per day. So the fast
// layer is the coarse one.
//
// Every field in IMAGERY was measured against the live services, because the
// advertised metadata is wrong in both directions:
//
//   - Capabilities advertise an unbroken PT10M series from `from`, but
//     rgb_truecolour and vis06_hrfi have a reproducible daily hole running
//     00:00Z to 01:50Z, first frame at 02:00Z. That is `dayFrom`, in minutes
//     into the UTC day. Asking inside the hole returns a ServiceException with
//     HTTP 200, which Leaflet renders as nothing - a blank map, no error.
//     This is a publication boundary and *not* darkness: those layers answer
//     at 20:00Z and 22:00Z, with the sun 27-38 degrees below the horizon.
//   - The capabilities `default` value lags (it read 14:00Z at 14:30 wall clock,
//     yet 15:30Z answered at 15:37), so "latest" is never derived from it.
//     Omitting `time` returns the newest frame, byte-identical to time=current.
const EUM_WMS = "https://view.eumetsat.int/geoserver/wms";
const GIBS_WMTS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best";
const IMG_OPACITY = .85;

// `from` is only enforced for Meteosat, where rgb_firetemperature genuinely
// starts inside the map's one-year range. Every GIBS layer here predates that
// window, so there is no lower bound to police.
// `grp` is what makes layers combinable. Within a group the layers are
// alternatives and only one can be on; across groups they stack, because the
// pairing that matters is a *thermal* layer over a *visible* one - Meteosat
// hotspots on VIIRS terrain, with the plume visible in the same frame. That one
// picture is worth more than either layer alone.
//
// `blend:"screen"` is what makes the stack readable, and it is not decoration.
// Fire Temperature RGB is near-black everywhere except the fire (mean 3.4 of
// 255 over this municipality), so screen compositing discards its background
// completely and keeps only the hot pixels. Screen can only ever *lighten*, so
// a blended layer physically cannot obscure the imagery underneath - which is
// why this is safe in a way that stacking two rasters at 85% opacity is not.
const IMAGERY = [
  {k:"imFire", src:"eum", layer:"mtg_fd:rgb_firetemperature", from:"2025-08-05",
   grp:"hot", blend:"screen"},
  {k:"imIr",   src:"eum", layer:"mtg_fd:ir105_hrfi",          from:"2024-09-23", grp:"vis"},
  {k:"imGeo",  src:"eum", layer:"mtg_fd:rgb_geocolour",       from:"2024-09-23", grp:"vis"},
  {k:"imTrue", src:"eum", layer:"mtg_fd:rgb_truecolour",      from:"2024-09-23", dayFrom:120, needsSun:true, grp:"vis"},
  {k:"imHrfi", src:"eum", layer:"mtg_fd:vis06_hrfi",          from:"2024-09-16", dayFrom:120, needsSun:true, grp:"vis"},
  {k:"imViirs", src:"gibs", lvl:9, grp:"vis",
   layer:"VIIRS_NOAA20_CorrectedReflectance_TrueColor"},
  {k:"imSwir",  src:"gibs", lvl:9, grp:"vis",
   layer:"VIIRS_NOAA20_CorrectedReflectance_BandsM11-I2-I1"},
  {k:"imHls",   src:"gibs", lvl:12, grp:"vis",
   layer:"HLS_S30_Nadir_BRDF_Adjusted_Reflectance"},
];

// Above tilePane (200) so imagery covers the archival basemap, below
// overlayPane (400) so the boundary, the nearby band and the fire markers all
// stay on top of it. Same trick as the measure pane further down.
map.createPane("imagery").style.zIndex = 250;
// The thermal pane sits above the visible one and blends instead of covering.
// If a browser ever ignores mix-blend-mode the layer still renders, just
// opaquely - so the fallback is ugly rather than wrong.
const hotPane = map.createPane("imageryHot");
hotPane.style.zIndex = 260;
hotPane.style.mixBlendMode = "screen";

// A GIBS tile requested past its own TileMatrixSet is HTTP 400 XML, not a
// blank tile, so maxNativeZoom is load-bearing rather than an optimisation:
// without it the layer vanishes the moment the reader zooms in past Level9.
IMAGERY.forEach(im => {
  // A screen-blended layer runs at full opacity: dimming it would only dim the
  // hotspots, since the background it contributes is already black.
  const pane = im.grp === "hot" ? "imageryHot" : "imagery";
  const op = im.blend ? 1 : IMG_OPACITY;
  im.op = op;
  im.lyr = im.src === "eum"
    ? L.tileLayer.wms(EUM_WMS, {layers:im.layer, format:"image/png",
        transparent:true, pane:pane, opacity:op,
        attribution:"EUMETSAT"})
    : L.tileLayer(GIBS_WMTS + "/" + im.layer + "/default/{day}" +
        "/GoogleMapsCompatible_Level" + im.lvl + "/{z}/{y}/{x}.jpg",
        // Seeded with today rather than a placeholder: Leaflet starts fetching
        // the moment the layer is added, before our overlayadd handler sets the
        // real date, and a placeholder's 400s would land after that and raise a
        // false "no scene" note.
        {day:new Date().toISOString().slice(0,10), pane:pane, opacity:op,
         maxNativeZoom:im.lvl, maxZoom:19, attribution:"NASA EOSDIS GIBS"});
  // GIBS has no frame for a date it never imaged - HLS revisits every ~5 days
  // and lands days later, so "today" is normally empty - and it says so with an
  // HTTP error per tile rather than a blank one. Without this the reader gets an
  // unexplained empty layer, which is the exact failure this whole readout
  // exists to prevent. One flag per layer, cleared whenever a tile does load.
  if(im.src === "gibs"){
    im.lyr.on("tileerror", () => {
      if(imgOn[im.grp] === im && !im.empty){ im.empty = true; syncImagery(sliderTime()); }
    });
    im.lyr.on("tileload", () => { im.empty = false; });
  }
});

// Sentinel-2 at 10 m, rendered server-side because its credential cannot go in
// a published page (see imagery.py). It is a still, not a tile layer: one scene
// every 2-3 days here, so there is no clock to follow - the note carries the
// scene date instead, and the reader is told rather than left guessing why
// scrubbing does not move it.
//
// Rendered in EPSG:3857 for this exact reason: L.imageOverlay stretches its
// image linearly between two *projected* corners, so a 4326 image would drift
// vertically against the basemap.
//
// It goes through overlays() and a control rebuild rather than addOverlay,
// because the control is thrown away and rebuilt on every language switch -
// an entry added directly would survive exactly until the reader pressed BS.
// A new scene lands every 2-3 days, so rebuilding then costs nothing.
let s2Layer = null, s2Rec = null;
// Add, replace or drop the Sentinel-2 overlay when the snapshot's scene changes.
function s2Sync(){
  const d = DATA.imagery || null;
  if(!d && !s2Rec) return;
  if(d && s2Rec && d.file === s2Rec.file && d.day === s2Rec.day) return;
  const wasOn = s2Layer && map.hasLayer(s2Layer);
  if(s2Layer){ map.removeLayer(s2Layer); s2Layer = null; }
  s2Rec = d;
  if(d){
    // Cache-bust on the scene date: the file name never changes, so a reader
    // who has seen one render would otherwise keep the old picture for ever.
    s2Layer = L.imageOverlay(d.file + "?v=" + encodeURIComponent(d.day),
      L.latLngBounds(d.bounds[0], d.bounds[1]),
      {pane:"imagery", opacity:IMG_OPACITY, attribution:"Copernicus Sentinel-2"});
  }
  rebuildControls();
  if(wasOn && s2Layer) s2Layer.addTo(map);
}

// Solar elevation at the town. This is a *usefulness* test, not an availability
// one, and the difference is the whole point: after sunset these two layers
// still answer 200 with a perfectly valid frame, it is just a frame of night.
// rgb_truecolour returns a fully transparent tile - so the reader sees the
// basemap and thinks the layer failed to load - while vis06_hrfi returns an
// opaque black rectangle. Two different flavours of the same nothing.
//
// Measured against the imagery: at +7.5 deg the frame reads 31/255
// mean, at +2.2 deg it is 1.0, and below the horizon it is blank. So the cutoff
// sits at +3, a little above the horizon rather than on it.
//
// Only the geostationary visible layers carry `needsSun`. The GIBS layers are
// *daily composites* built from a daytime overpass, so they are perfectly
// readable at midnight - flagging them would be wrong.
const SUN_MIN_DEG = 3;
// Solar elevation in degrees at the town for a UTC millisecond timestamp.
function sunElev(ms){
  const rad = Math.PI/180, n = ms/86400000 + 2440587.5 - 2451545.0;
  const L = (280.460 + 0.9856474*n) % 360;
  const g = ((357.528 + 0.9856003*n) % 360) * rad;
  const lam = (L + 1.915*Math.sin(g) + 0.020*Math.sin(2*g)) * rad;
  const eps = (23.439 - 0.0000004*n) * rad;
  const dec = Math.asin(Math.sin(eps)*Math.sin(lam));
  const ra = Math.atan2(Math.cos(eps)*Math.sin(lam), Math.cos(lam));
  const gmst = (18.697374558 + 24.06570982441908*n) % 24;
  const ha = (gmst*15 + TOWN_LON)*rad - ra, phi = TOWN_LAT*rad;
  return Math.asin(Math.sin(phi)*Math.sin(dec) +
                   Math.cos(phi)*Math.cos(dec)*Math.cos(ha)) / rad;
}

// UTC helpers: minutes into the day, YYYY-MM-DD, and rounding down to a 10-minute slot.
const utcMin = ms => {const d = new Date(ms); return d.getUTCHours()*60 + d.getUTCMinutes();};
const utcDay = ms => new Date(ms).toISOString().slice(0,10);
const tenMin = ms => Math.floor(ms/600000)*600000;

// Any moment within this of now counts as "latest": we drop `time` and let the
// server hand back its freshest frame. That single rule disposes of the whole
// upper-bound problem, because the slider's own tMax is Date.now() and a
// timestamp even one slot into the future is a ServiceException.
const LIVE_EDGE_MS = 30*60000;

// Whether imagery layer `im` has a usable frame at `ms`: {ok, live} or {ok:false, why}.
function imgAvail(im, ms){
  // "Live" only decides *which* frame we ask for; it must not short-circuit the
  // checks, because the default view is the live edge and a layer that is useless
  // right now is the one the reader meets first.
  const live = Date.now() - ms < LIVE_EDGE_MS;
  const at = live ? Date.now() : ms;
  if(!live){
    if(im.from && at < Date.parse(im.from + "T00:00:00Z"))
      return {ok:false, why:"imNoArchive"};
    if(im.dayFrom && utcMin(at) < im.dayFrom)
      return {ok:false, why:"imNoFrame"};
  }
  if(im.needsSun && sunElev(at) < SUN_MIN_DEG) return {ok:false, why:"imDark"};
  return {ok:true, live:live};
}

// Note showing what imagery frame the reader is actually looking at: the slider
// can read 12:03 while the frame is 12:00, "live" is whatever the server has, and
// an archive gap otherwise looks exactly like a clear sky. It is also the only
// place a silent hole gets explained.
let imgEl = null;
const imgCtl = L.control({position:"bottomleft"});
imgCtl.onAdd = () => {
  imgEl = L.DomUtil.create("div","imgnote");
  imgEl.style.display = "none";
  return imgEl;
};
imgCtl.addTo(map);

// One row per active layer: with two of them stacked, "which frame am I looking
// at" has two answers and they are usually different - a live 10-minute thermal
// frame over a daily visible composite.
function imgNote(rows){
  if(!imgEl) return;
  if(!rows || !rows.length){ imgEl.style.display = "none"; return; }
  imgEl.style.display = "";
  imgEl.innerHTML = rows.map(r => `<b>${r.head}</b>` +
    (r.detail ? (r.warn ? ` <s>${r.detail}</s>` : ` ${r.detail}`) : "")).join("<br>");
}

// One slot per group, so the two can be on at once.
const imgOn = {vis:null, hot:null};
const imgActive = () => [imgOn.vis, imgOn.hot].filter(Boolean);

// Unavailable moments hide the raster by dropping its opacity to zero rather
// than removing the layer. Removing it would fire overlayremove, which unticks
// the reader's own checkbox and loses the selection, so scrubbing back into a
// valid window would not bring the layer back.
function syncOne(im, ms){
  const a = imgAvail(im, ms);
  if(!a.ok){
    im.lyr.setOpacity(0);
    return {head:t(im.k), detail:t(a.why), warn:true};
  }
  im.lyr.setOpacity(im.op);
  if(im.src === "eum"){
    const stamp = a.live ? null : new Date(tenMin(ms)).toISOString().slice(0,19) + "Z";
    if(im.at !== stamp){
      im.at = stamp;
      if(stamp) im.lyr.wmsParams.time = stamp; else delete im.lyr.wmsParams.time;
      im.lyr.redraw();
    }
    return {head:t(im.k), detail:a.live ? t("imLatest") : fmtLocal(tenMin(ms))};
  }
  const day = utcDay(a.live ? Date.now() : ms);
  if(im.at !== day){
    im.at = day;
    im.empty = false;                 // a new date deserves a fresh verdict
    im.lyr.options.day = day;
    im.lyr.redraw();
  }
  if(im.empty) return {head:t(im.k), detail:t("imNoScene"), warn:true};
  return {head:t(im.k), detail:day + (a.live ? " \u00b7 " + t("imToday") : "")};
}

// Point every active imagery layer at the frame for `ms` and refresh the note.
function syncImagery(ms){
  const rows = imgActive().map(im => syncOne(im, ms));
  if(s2Layer && map.hasLayer(s2Layer))
    rows.push({head:t("imS2"),
               detail:s2Rec.day + " \u00b7 " + s2Rec.cloud + "% " + t("imCloud")});
  imgNote(rows);
}

// Radio behaviour without a radio group: Leaflet gives base layers exclusivity
// and overlays none, but two imagery rasters stacked at 85% are unreadable.
// Exclusivity within a group only. Sentinel-2 is a visible layer like any
// other, so it competes for the "vis" slot rather than clearing the map.
map.on("overlayadd", e => {
  if(e.name) fwTrack("layer_add", {layer: String(e.name).replace(/<[^>]*>/g, "").slice(0, 60)});
  if(s2Layer && e.layer === s2Layer){
    if(imgOn.vis){ map.removeLayer(imgOn.vis.lyr); imgOn.vis = null; }
    syncImagery(sliderTime());
    return;
  }
  const im = IMAGERY.find(x => x.lyr === e.layer);
  if(!im) return;
  if(im.grp === "vis" && s2Layer && map.hasLayer(s2Layer)) map.removeLayer(s2Layer);
  IMAGERY.forEach(o => {
    if(o !== im && o.grp === im.grp && map.hasLayer(o.lyr)) map.removeLayer(o.lyr);
  });
  imgOn[im.grp] = im;
  im.at = undefined;                 // force a param write on the next sync
  syncImagery(sliderTime());
});
map.on("overlayremove", e => {
  const im = IMAGERY.find(x => x.lyr === e.layer);
  if(im && imgOn[im.grp] === im) imgOn[im.grp] = null;
  syncImagery(sliderTime());
});

// The "nearby" band - everything within nearby_buffer_km of the outline, which is
// exactly what the spatial clip keeps and flags `inside=0`. Pre-built into
// BUFFER_GEOJSON (config.py) rather than offset in the browser, since the answer
// only changes when the config does. Drawn before the boundary so the outline
// stays the stronger line.
// The artifact wins over DATA.buffer_km: the band on screen *is* the artifact, so
// its label must follow the geometry, not a config value baked into older data.
const bufKm = () => (BUFFER && BUFFER.features[0].properties.buffer_km)
  || DATA.buffer_km || 6;
// interactive:false for the same reason as bLayer below: it has no popup of its
// own and must not intercept a click meant for a municipality shape.
const bandLayer = BUFFER ? L.geoJSON(BUFFER,{interactive:false,style:{color:"#7cc4ff",
  weight:1.1,opacity:.55,dashArray:"3,5",fillColor:"#7cc4ff",fillOpacity:.05}}).addTo(map) : null;
// Info-panel HTML for a municipality. A function, not a fixed string, so
// re-opening it after applyData() swaps in a fresh DATA shows the current
// fire-danger reading: the boundaries never change but fwiDetail()'s source does.
function muniPopupHtml(f){
  const tg = f.properties.telegram_invite
    ? `<a class="muni-tg" href="${f.properties.telegram_invite}" target="_blank" rel="noopener">\u{1F514} ${t("telegramSub")}</a>`
    : "";
  return `<b>${f.properties.name}</b>${fwiDetail(f.properties.id)}${tg}`;
}
// One shared, fixed, centred panel (see its CSS comment for why it is not a
// Leaflet popup); a municipality or a fire event renders its HTML into the same
// element, since only one is shown at a time.
const muniInfoEl = document.getElementById("muniinfo");
const muniInfoBody = document.getElementById("muniinfo-body");
L.DomEvent.disableClickPropagation(muniInfoEl);
L.DomEvent.disableScrollPropagation(muniInfoEl);
muniInfoEl.querySelector(".miclose").addEventListener("click", () => closeInfoPanel());
// Show `html` in the shared info panel.
function openInfoPanel(html){
  muniInfoBody.innerHTML = html;
  muniInfoEl.classList.add("show");
}
// Hide the info panel, recording the dismissal so the same click is not also used
// as a measure-tool vertex (see lastDismissAt).
function closeInfoPanel(){
  if(muniInfoEl.classList.contains("show")) lastDismissAt = Date.now();
  muniInfoEl.classList.remove("show");
}
// Open the info panel for municipality feature `f` and report the open.
function openMuniInfo(f){
  fwTrack("municipality_open", {municipality_id: f.properties.id, municipality: f.properties.name});
  openInfoPanel(muniPopupHtml(f));
}
// Delegated, because the panel's HTML is rebuilt on every open and refresh. This
// counts clicks on the subscribe link, not subscriptions - Telegram never tells us
// whether the reader then pressed Join.
muniInfoEl.addEventListener("click", ev => {
  const a = ev.target.closest && ev.target.closest("a.muni-tg");
  if(a) fwTrack("telegram_subscribe_click", {link: a.href});
});
// A click on a municipality or event is debounced rather than opened immediately,
// so the clicks that make up a double-click (Leaflet's zoom-in gesture) do not
// flash the panel open. The map's "dblclick" fires after both clicks, in time to
// cancel the pending open; one shared timer suffices because only one panel can
// be open, so only one open can be pending.
let infoClickTimer = null;
map.on("dblclick", () => {
  if(infoClickTimer){ clearTimeout(infoClickTimer); infoClickTimer = null; }
});
// A click that dismisses something open should only do that, not also act as
// that click's normal map action (e.g. dropping a measurement point while the
// reader is closing a popup; see the measure tool's click handler below).
//
// This is a timestamp, not an "is something open" boolean: Leaflet's built-in
// closePopupOnClick runs before any handler this page registers, so a popup the
// click is closing is already closed by the time our handlers run and looks like
// "nothing was open". Every handler for one click fires in the same tick, so a
// timestamp is independent of handler order. It is recorded only on an actual
// close transition, so it cannot get stuck.
let lastDismissAt = 0;
map.on("popupclose", () => { lastDismissAt = Date.now(); });
// Any other map click (a fire marker, open water, the boundary itself) closes
// the panel - the same "click elsewhere to dismiss" convention as
// closeExpandablePanels(). A layer click bubbles to the map, so clicking a
// different feature closes the current panel before that feature's debounced
// open runs, which makes switching panels instant rather than stacking them.
map.on("click", closeInfoPanel);
// Municipality reference layer. Canvas, not the default SVG renderer: 145
// boundaries are ~330k vertices, and SVG means one DOM path per feature. Lighter
// weight and fill than BOUNDARY's outline so 145 of them are not visual noise,
// and so Sarajevo/Istocno Sarajevo's deliberately overlapping shapes do not read
// as a rendering bug.
const muniLayer = (BIH_MUNICIPALITIES && BIH_MUNICIPALITIES.features
    && BIH_MUNICIPALITIES.features.length)
  ? L.geoJSON(BIH_MUNICIPALITIES,{renderer:L.canvas({padding:0.5}),
      style:{color:"#9fb3c8",weight:1,opacity:.6,fillColor:"#9fb3c8",fillOpacity:.02},
      onEachFeature:(f,lyr)=>{
        lyr.bindTooltip(f.properties.name,{sticky:true});
        lyr.on("click", () => {
          if(infoClickTimer) clearTimeout(infoClickTimer);
          infoClickTimer = setTimeout(() => { infoClickTimer = null; openMuniInfo(f); }, 300);
        });
      }})
    .addTo(map)
  : null;
// A separate object each time: L.control.layers keeps a reference, so reusing one
// across rebuilds would carry the old language's key with it. Both halves of the
// control are built by a function so every rebuild (e.g. a language switch) gets
// the same full base-layer list.
const bases = () => ({[t("lSat")]:sat, [t("lMap")]:osm,
                      [t("lTopo")]:topo, [t("lNone")]:blank});
// Imagery first, then the band: the control lists them in insertion order and
// the imagery group is what a reader reaches for during a fire.
const overlays = () => {
  const o = {};
  IMAGERY.forEach(im => { o[t(im.k)] = im.lyr; });
  if(s2Layer) o[t("imS2")] = s2Layer;
  if(bandLayer) o[t("lBuffer",{km:bufKm()})] = bandLayer;
  if(muniLayer) o[t("lMuni")] = muniLayer;
  return o;
};

// Built as an always-expanded Leaflet control (collapsed:false) whose list and
// button are then driven by the same open/close machinery as the legend, so the
// two share one-open-at-a-time and click-the-map-to-dismiss.
function makeLayersCtl(){
  const c = L.control.layers(bases(), overlays(), {position:"topright", collapsed:false});
  c.addTo(map);
  const d = c.getContainer();
  d.classList.add("layersctl");
  const btn = L.DomUtil.create("button","legend-toggle");
  btn.setAttribute("aria-expanded","false");
  btn.innerHTML = `\u2630\u00a0 ${t("layers")}`;
  d.insertBefore(btn, d.firstChild);
  c._el = d;
  btn.addEventListener("click", () => togglePanel(c));
  expandablePanels.push(c);
  return c;
}

// Brighter and heavier than strictly needed on the pale street map: a mid-blue
// hairline disappears against dark forest imagery.
// interactive:false is load-bearing: this shape covers the whole country and is
// stacked above muniLayer's 145 smaller ones, so without it every click hits this
// popup-less layer first and never reaches the municipality underneath.
const bLayer = L.geoJSON(BOUNDARY,{interactive:false,style:{color:"#7cc4ff",weight:2.4,
  opacity:.95,fillColor:"#7cc4ff",fillOpacity:.05,dashArray:"6,5"}}).addTo(map);
// animate:false is load-bearing for deep links: an animated fit leaves a zoom
// animation in flight that, when it finishes, overwrites whatever deepLink() set
// in the meantime (the centre snaps back to the country view).
map.fitBounds(bLayer.getBounds(),{padding:[24,24],animate:false});

// Jump straight to what is burning - at municipality zoom a single fire is a
// few pixels, which is exactly when you most want to see it.
const zoomBtn = L.control({position:"topleft"});
zoomBtn.onAdd = () => {
  const d = L.DomUtil.create("div","leaflet-bar");
  const a = L.DomUtil.create("a","",d);
  a.href="#"; a.title=t("zoomFires"); a.innerHTML="&#128293;";
  a.style.fontSize="15px"; a.style.textAlign="center";
  L.DomEvent.on(a,"click",e=>{
    L.DomEvent.preventDefault(e);
    if(!EVENTS.length){ map.fitBounds(bLayer.getBounds(),{padding:[24,24]}); return; }
    map.fitBounds(L.latLngBounds(EVENTS.map(e=>[e.lat,e.lon])).pad(0.55),{maxZoom:14});
  });
  return d;
};
zoomBtn.addTo(map);

// ---- expandable corner panels -----------------------------------------------
// The legend and the fire-danger panel share this: only one open at a time (an
// expanded FFMC/DMC/DC readout behind an already-open legend would be a second
// scroll-y panel stacked on the first), and clicking the map collapses whichever
// is open, the same "click elsewhere to dismiss" convention as a popup.
const expandablePanels = [];
// Open or close one panel and keep its toggle's aria-expanded in step.
function setPanelOpen(ctl, open){
  const d = ctl._el;
  if(!d) return;
  d.classList.toggle("open", open);
  const btn = d.querySelector(".legend-toggle");
  if(btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
}
// Collapse every corner panel.
function closeExpandablePanels(){
  expandablePanels.forEach(ctl => setPanelOpen(ctl, false));
}
// Toggle one panel, closing any other first.
function togglePanel(ctl){
  const wasOpen = ctl._el && ctl._el.classList.contains("open");
  expandablePanels.forEach(other => { if(other !== ctl) setPanelOpen(other, false); });
  setPanelOpen(ctl, !wasOpen);
}

const legend = L.control({position:"bottomright"});
legend.onAdd = () => {
  const d = L.DomUtil.create("div","legend");
  const rows = `<b style="color:#e6edf3">${t("legDet")}</b><br>` +
    Object.entries(SRC).map(([k,v])=>`<i style="background:${v.c}"></i>${v.n}`).join("<br>") +
    `<br><b style="color:#e6edf3">${t("legSev")}</b> <span style="opacity:.7">${t("legSize")}</span><br>` +
    ["low","moderate","high","severe"]
      .map(k=>`<i style="background:${SEVC[k]}"></i>${t("sev_"+k)}`).join("<br>") +
    `<br><b style="color:#e6edf3">${t("legState")}</b><br>` +
    `<i style="background:#f4511e"></i>${t("legBurning")}<br>` +
    `<i style="background:none;border:1px dashed #f4511e"></i>${t("legQuiet")}` +
    `<br><b style="color:#e6edf3">${t("legImagery")}</b> ` +
    `<span style="opacity:.7">${t("legImNote")}</span>` +
    (bandLayer ? `<br><b style="color:#e6edf3">${t("legZone")}</b><br>` +
      `<i style="background:#7cc4ff;opacity:.9"></i>${t("boundary")}<br>` +
      `<i style="background:rgba(124,196,255,.14);border:1px dashed #7cc4ff"></i>` +
      `${t("lBuffer",{km:bufKm()})} <span style="opacity:.7">— ${t("zoneNote")}</span>`
      : "");
  // Body first, button after: the control sits bottom-right, so it opens upward.
  d.innerHTML = `<div class="legend-body">${rows}</div>` +
    `<button class="legend-toggle" aria-expanded="false">\u25eb\u00a0 ${t("key")}</button>`;
  // Without this, tapping the legend pans the map underneath it.
  L.DomEvent.disableClickPropagation(d);
  L.DomEvent.disableScrollPropagation(d);
  legend._el = d;
  d.querySelector(".legend-toggle").addEventListener("click", () => togglePanel(legend));
  return d;
};
legend.addTo(map);
expandablePanels.push(legend);
let layersCtl = makeLayersCtl();

// ---- fire danger -------------------------------------------------------------
// One weather-derived index per municipality (see firedanger.py's module
// docstring), so there is no single reading a corner badge could show. Each
// municipality's own class/FWI appears in its info panel via muniPopupHtml(),
// formatted by fwiDetail() below.
const FWI_CLASS_I18N = {low:"fwLow", moderate:"fwModerate", high:"fwHigh",
  very_high:"fwVeryHigh", extreme:"fwExtreme", very_extreme:"fwVeryExtreme"};
// Clicking the map collapses whichever corner panel is open.
map.on("click", closeExpandablePanels);

// Fire-danger HTML (today plus forecast days) for municipality id `mid`, or "".
function fwiDetail(mid){
  const fd = (DATA.fire_danger || {})[mid];
  // Nothing computed yet (feature disabled or first cycle not run): omit the
  // section rather than show a placeholder or a fabricated reading.
  if(!fd || !fd.today) return "";
  const today = fd.today;
  const clsLabel = e => t(FWI_CLASS_I18N[e.class] || "fwLow");
  const swatch = c => `<i style="background:${c}"></i>`;
  const rows = [today, ...(fd.forecast || [])].map(e => {
    const [, mo, da] = e.date.split("-");
    const label = e.date === today.date ? t("fwToday")
      : `${parseInt(da, 10)} ${monName(mo)}`;
    return `<div class="fwi-row"><span>${label}</span><span>` +
      `${swatch(e.color)}${clsLabel(e)} <span style="opacity:.6">` +
      `(${e.fwi.toFixed(1)})</span></span></div>`;
  }).join("");
  const mins = (Date.now() - new Date(fd.updated_at).getTime()) / 60000;
  return `<hr><b style="color:#e6edf3">${t("fwTitle")}</b><br>` +
    `${swatch(today.color)}${t("fwBadge", {cls: clsLabel(today)})}` +
    `<div style="margin-top:6px">${rows}</div>` +
    `<div class="fwi-codes">FFMC ${today.ffmc} · DMC ${today.dmc} · DC ${today.dc}</div>` +
    `<div class="fwi-note">${t("fwNote")}</div>` +
    `<div class="fwi-note">${t("fwUpdated", {t: ago(mins)})}</div>`;
}

const detLayer = L.layerGroup().addTo(map);
const evLayer  = L.layerGroup().addTo(map);
const trail    = L.layerGroup().addTo(map);
let selected = null;

// Hide every fire graphic, so the imagery underneath can be read. The markers
// (pulsing ring, footprint circle, halo) are built to be impossible to miss,
// which gets in the way when looking at smoke in a satellite frame.
//
// It detaches the three *groups* from the map rather than clearing them or
// hiding a pane. drawEvents() and drawDets() rebuild their contents on every
// refresh and zoom; clearing would last until the next tick, whereas a detached
// group keeps accepting children that are simply not shown, so no draw path
// needs to know about it. The sidebar list stays, so "what is burning" is still
// answered.
//
// Deliberately *not* remembered, unlike the language choice: it is a momentary
// "let me look under them", and a reload or a cycle that brings a new fire must
// not deliver it invisibly. It resets to visible on load and whenever fresh data
// lands - see applyData.
let markersOn = true;

// Attach or detach the fire marker groups according to markersOn.
function applyMarkers(){
  [evLayer, detLayer, trail].forEach(g => {
    if(markersOn) { if(!map.hasLayer(g)) g.addTo(map); }
    else if(map.hasLayer(g)) map.removeLayer(g);
  });
  if(eyeEl){
    eyeEl.innerHTML = markersOn ? EYE_ON : EYE_OFF;
    eyeEl.title = t(markersOn ? "hideFires" : "showFires");
    eyeEl.setAttribute("aria-pressed", markersOn ? "false" : "true");
  }
}

// Inline SVG rather than an emoji: there is no eye-with-a-slash emoji that
// renders the same way across platforms, and the pair has to read as one
// control in two states.
const EYE_ON = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
  '<path d="M1.6 12S5.3 5 12 5s10.4 7 10.4 7-3.7 7-10.4 7S1.6 12 1.6 12z"/>' +
  '<circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
  '<path d="M9.9 5.2A9.8 9.8 0 0 1 12 5c6.7 0 10.4 7 10.4 7a18 18 0 0 1-3.2 4.1"/>' +
  '<path d="M6.4 6.5A17.6 17.6 0 0 0 1.6 12S5.3 19 12 19a9.9 9.9 0 0 0 4.2-.9"/>' +
  '<path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="M2 2l20 20"/></svg>';

// The eye button that toggles markersOn; eyeEl is set when the control is added.
let eyeEl = null;
const eyeBtn = L.control({position:"topleft"});
eyeBtn.onAdd = () => {
  const d = L.DomUtil.create("div","leaflet-bar");
  eyeEl = L.DomUtil.create("a","eye",d);
  eyeEl.href = "#";
  eyeEl.setAttribute("role","button");
  L.DomEvent.on(eyeEl,"click",e => {
    L.DomEvent.preventDefault(e);
    L.DomEvent.stopPropagation(e);
    markersOn = !markersOn;
    applyMarkers();
  });
  applyMarkers();
  return d;
};
eyeBtn.addTo(map);

const MONTHS = {
  en:["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],
  bs:["jan","feb","mar","apr","maj","jun","jul","aug","sep","okt","nov","dec"]
};
const _sarajevo = new Intl.DateTimeFormat("en-GB",{timeZone:"Europe/Sarajevo",
  year:"numeric",day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit",
  hour12:false});
// Date/time parts of `ts` in Sarajevo local time, keyed by Intl part type.
function tlParts(ts){
  const p = {};
  for(const part of _sarajevo.formatToParts(new Date(ts))) p[part.type] = part.value;
  return p;
}
const monName = mm => (MONTHS[LANG] || MONTHS.en)[parseInt(mm, 10) - 1];
// With a one-year range a bare "14 Mar, 09:12" is ambiguous, so the year is spelled
// out wherever it is not the current one; present-day stamps stay short.
const CUR_YEAR = tlParts(Date.now()).year;
// `sep` carries the full stop Bosnian puts after a year in a date, which would
// read as a typo on a bare ruler tick.
const yearBit = (p, sep = "") => p.year === CUR_YEAR ? "" : ` ${p.year}${sep}`;
// Short local timestamp; carries the year only when it is not the current one.
function fmtLocal(ts){
  const p = tlParts(ts);
  const mon = monName(p.month);
  return LANG === "bs" ? `${p.day}. ${mon}${yearBit(p, ".")} ${p.hour}:${p.minute}`
                       : `${p.day} ${mon}${yearBit(p)}, ${p.hour}:${p.minute}`;
}
// Full local timestamp for the timeline readout; always carries the year, since it
// is the one label that answers "where am I".
function fmtStamp(ts){
  const p = tlParts(ts);
  const mon = monName(p.month);
  return LANG === "bs" ? `${p.day}. ${mon} ${p.year}. ${p.hour}:${p.minute}`
                       : `${p.day} ${mon} ${p.year}, ${p.hour}:${p.minute}`;
}
// Format an age in minutes as "just now" / "N min ago" / "N h ago" / "N d ago".
const ago = m => m<1 ? t("justNow")
  : m<60 ? t("agoMin",{n:Math.round(m)})
  : m<1440 ? t("agoH",{n:(m/60).toFixed(1)})
  : t("agoD",{n:(m/1440).toFixed(1)});
// Detection dots grow as you zoom in. Fixed-size dots either vanish at
// municipality zoom or merge into one mass when a fire has 45 of them in 2 km.
const detR = () => {
  const z = map.getZoom();
  return Math.max(6.5, Math.min(11, 5 + (z - 8) * 0.95));
};

const R_EARTH_M = 6371008.8;
// Great-circle distance in metres between two lat/lon points.
function haversineM(la1,lo1,la2,lo2){
  const p1=la1*Math.PI/180, p2=la2*Math.PI/180;
  const dp=p2-p1, dl=(lo2-lo1)*Math.PI/180;
  const a=Math.sin(dp/2)**2 + Math.cos(p1)*Math.cos(p2)*Math.sin(dl/2)**2;
  return 2*R_EARTH_M*Math.asin(Math.min(1,Math.sqrt(a)));
}
// Radius that actually encloses the drawn detections. Deriving it from extent_km/2
// under-covers, because extent is the bounding-box diagonal while the marker sits
// at the centroid - the farthest detection can be further than half that diagonal.
function footprintM(e){
  let m = 0;
  (e.series||[]).forEach(s=>{ m = Math.max(m, haversineM(e.lat,e.lon,s.lat,s.lon)); });
  return Math.max(400, m + 100);          // +100 m so edge dots sit inside the ring
}

// Radius of the event circle in metres. It is the real footprint, but never
// smaller on screen than a detection dot plus 3 px: zoomed out, a 400 m circle
// is sub-pixel and could neither be seen nor clicked.
function evRadiusM(e){
  const mpp = 156543.03392*Math.cos(e.lat*Math.PI/180)/Math.pow(2,map.getZoom());
  return Math.max(footprintM(e), (detR()+3)*mpp);
}

// Markers are rebuilt on every refresh, which would leave a stale event panel open
// with no marker behind it. The open one's id is tracked so applyData() can
// refresh the panel with the event's new data, or close it if the event is gone.
let infoOpenId = null;

// Redraw the per-event footprint circles (and the pulse ring on active ones).
function drawEvents(){
  evLayer.clearLayers();
  const openOf = e => () => {
    select(e.id,false);
    if(infoClickTimer) clearTimeout(infoClickTimer);
    infoClickTimer = setTimeout(() => {
      infoClickTimer = null; infoOpenId = e.id;
      fwTrack("fire_open", {event_id: e.id, status: e.status});
      openInfoPanel(popupHtml(e));
    }, 300);
  };

  // One circle per event, in METRES so it scales with zoom and keeps enclosing the
  // detections it summarises (see evRadiusM for the floor). It is also the click
  // target for the info panel. Largest first, so a small cluster inside a big one
  // is drawn on top of it and stays clickable.
  EVENTS.map(e=>({e, m:evRadiusM(e)})).sort((x,y)=>y.m-x.m).forEach(({e,m})=>{
    const col = SEVC[e.severity]||SEVC.unknown;
    const quiet = e.status!=="active";
    L.circle([e.lat,e.lon],{
      radius:m, color:col, weight:quiet?2.5:3.5,
      // Stroke keeps full hue even when quiet: a faded fill over green terrain
      // desaturates toward olive, and a burnt-out severe fire should still read
      // as severe. Dash + thin fill carry "not burning", not the colour.
      opacity:1,
      fillColor:col, fillOpacity:quiet?.10:.45,
      dashArray:quiet?"6,4":null}).on("click", openOf(e)).addTo(evLayer);
    if(!quiet){
      L.circle([e.lat,e.lon],{radius:m,color:col,weight:3,opacity:.9,fill:false,
        interactive:false,className:"pulse"}).addTo(evLayer);
    }
  });
}

// Which feed got this fire into the database first, which satellite, and when it
// landed. Arrival, not acquisition: the feed that saw it earliest is often not the
// one that reported it earliest. Falls back to the series head when the credit_*
// fields are absent (an older cached fire-map-data.js), without a saved time.
function credit(e){
  const s = e.credit_source || (e.series && e.series[0] && e.series[0].source);
  if(!s) return "";
  const grey = "color:#8b98a5";
  const sensor = e.credit_sensor
    ? ` <span style="${grey}">${e.credit_sensor}</span>` : "";
  const saved = e.credit_saved_at
    ? ` <span style="${grey}">&middot; ${t("savedAt")} ${
        fmtLocal(Date.parse(e.credit_saved_at))}</span>` : "";
  return `${t("discoveredBy")} <b style="color:${SRC[s]?.c||"#fff"}">${
    SRC[s]?.n||s}</b>${sensor}${saved}<br>`;
}

// Info-panel HTML for one fire event.
function popupHtml(e){
  const w = e.weather;
  return `<b>${t("sev_"+e.severity).toUpperCase()}</b> &middot; ${t("st_"+e.status)}<br>
    ${placeOf(e)}<br>
    FRP <b>${e.max_frp==null?"n/a":e.max_frp.toFixed(1)+" MW"}</b> ${t("peak")},
    ${e.latest_frp==null?"n/a":e.latest_frp.toFixed(1)+" MW"} ${t("latest")}<br>
    ${e.n_det} ${t("detections")} &middot; ${e.sources.join(", ")}<br>
    ${credit(e)}
    ${t("lastSeen").toLowerCase()} ${ago(e.age_min)} &middot; ${e.extent_km} km ${t("across")}
    ${w?`<br>${t("wind")} ${Math.round(w.speed)} km/h ${t("from")} ${dir(w.from)} (${t("gusts")} ${Math.round(w.gusts)}), ${t("rh")} ${w.humidity}%`:""}
    <br><br><a href="https://www.google.com/maps?q=${e.lat},${e.lon}" target="_blank">Google Maps</a>
     &middot; <a href="https://www.openstreetmap.org/?mlat=${e.lat}&mlon=${e.lon}#map=14/${e.lat}/${e.lon}" target="_blank">OSM</a>`;
}

// Draw detections up to time `upto`, plus the selected event's trail and the
// timeline label.
function drawDets(upto){
  // Every path that changes the moment on screen funnels through here (timeline
  // scroller, keyboard, playback, range change, the 60 s refresh), so this is
  // the one place the imagery clock has to be advanced.
  syncImagery(upto);
  detLayer.clearLayers(); trail.clearLayers();
  const shown = dets.filter(d=>d.t<=upto);
  shown.forEach(d=>{
    const age = (upto-d.t)/3600000;
    // Age fades a detection, but with a floor so an old one never disappears
    // (a near-invisible fill with an outline reads as an empty ring).
    const op = Math.max(.55,1-age/40);
    // Each detection gets a white border with a dark outer edge; the two thin
    // rings keep the cyan/amber/magenta source colours readable on street and
    // satellite basemaps.
    const dc = SRC[d.source]?.c || "#fff";
    const dr = detR();
    L.circleMarker([d.lat,d.lon],{radius:dr,color:"#0b0f14",
      weight:3.4,opacity:op*.8,fill:false,interactive:false}).addTo(detLayer);
    // A single detection is a point-in-time reading, not worth a centred panel,
    // so it keeps an ordinary popup anchored where it was clicked.
    L.circleMarker([d.lat,d.lon],{radius:dr,color:"#ffffff",
      weight:2,opacity:op*.95,fillOpacity:op*.95,fillColor:dc})
      .bindPopup(`${SRC[d.source]?.n||d.source}<br>${fmtLocal(d.t)}<br>FRP ${d.frp==null?"n/a":d.frp+" MW"}`)
      .addTo(detLayer);
  });
  if(selected){
    const pts = shown.filter(d=>d.ev===selected).map(d=>[d.lat,d.lon]);
    if(pts.length>1) L.polyline(pts,{color:"#ff6b35",weight:1.4,opacity:.5,dashArray:"3,4"}).addTo(trail);
  }
  // The clock alone says where you are on the timeline. It still shows with no
  // detections, since the timeline spans the whole range.
  document.getElementById("tlabel").textContent =
    fmtStamp(upto) + (dets.length ? ` · ${shown.length}/${dets.length}` : "");
}

// Inline SVG sparkline of an event's FRP series, or "" with fewer than two points.
function sparkline(e){
  const pts = e.series.filter(s=>s.frp!=null);
  if(pts.length<2) return "";
  const W=316,H=30,mx=Math.max(...pts.map(p=>p.frp)),t0=Date.parse(pts[0].ts),
        t1=Date.parse(pts[pts.length-1].ts)||t0+1;
  const xy = pts.map(p=>[6+(Date.parse(p.ts)-t0)/Math.max(1,t1-t0)*(W-12),
                         H-3-(p.frp/(mx||1))*(H-9)]);
  const col = SEVC[e.severity]||"#888";
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <polyline fill="none" stroke="${col}" stroke-width="1.6" stroke-linejoin="round"
      points="${xy.map(p=>p[0].toFixed(1)+","+p[1].toFixed(1)).join(" ")}"/>
    <polyline fill="${col}" opacity=".13" stroke="none"
      points="6,${H-3} ${xy.map(p=>p[0].toFixed(1)+","+p[1].toFixed(1)).join(" ")} ${(W-6)},${H-3}"/>
    <text x="${W-4}" y="10" fill="#6f7d8c" font-size="9" text-anchor="end">${mx.toFixed(0)} MW</text>
  </svg>`;
}

// Render the sidebar list of event cards for the current range.
function renderList(){
  const el = document.getElementById("list");
  if(!EVENTS.length){
    el.innerHTML = `<div class="empty"><div class="big">🌲</div>
      <b>${t("noFires",{range:t("r_"+RANGE)})}</b><br><span style="font-size:12px">
      ${t("nothing",{km:DATA.buffer_km})}</span></div>`;
    return;
  }
  el.innerHTML = EVENTS.map(e=>{
    const col = e.status==="active"?(SEVC[e.severity]||"#888"):"#6b7785";
    const w = e.weather;
    return `<div class="ev" id="ev-${e.id}" data-id="${e.id}" style="border-left-color:${col}">
      <h2><span>${e.status==="active"?"🔥":"💤"} ${muniOf(e)?`${muniOf(e)} <span style="color:#8b98a5;font-weight:400">· ${placeOf(e)}</span>`:placeOf(e)}</span>
          <span class="sev" style="color:${col}">${t("sev_"+e.severity)}</span></h2>
      <div class="meta">
        <span>FRP</span><span>${e.max_frp==null?"n/a":e.max_frp.toFixed(1)+" MW "+t("peak")+" / "+
          (e.latest_frp==null?"n/a":e.latest_frp.toFixed(1)+" MW "+t("now"))}</span>
        <span>${t("lastSeen")}</span><span>${ago(e.age_min)} · ${fmtLocal(Date.parse(e.last_ts))}</span>
        <span>${t("started")}</span><span>${fmtLocal(Date.parse(e.first_ts))}</span>
        <span>${t("legDet")}</span><span>${e.series.length}${e.series.length!==e.n_det?" "+t("ofN",{n:e.n_det}):""} · ${e.sources.map(s=>
          `<span style="color:${SRC[s]?.c||"#fff"}">${s}</span>`).join(" ")}</span>
        <span>${t("extent")}</span><span>${e.extent_km} km</span>
        ${w?`<span>${t("weather")}</span><span>${w.temp}°C, ${t("rh")} ${w.humidity}%, ${t("wind")} ${Math.round(w.speed)} km/h ${t("from")} ${dir(w.from)}
             ${e.risk?`· <b style="color:${e.risk==="extreme"||e.risk==="high"?"#e63946":"#8b98a5"}">${t("spread",{risk:t("risk_"+e.risk)})}</b>`:""}</span>`:""}
        ${e.inside?"":`<span>${t("note")}</span><span style="color:#ffd166">${t("outside")}</span>`}
      </div>
      ${sparkline(e)}
      <div class="acts">
        <a href="https://www.google.com/maps?q=${e.lat},${e.lon}" target="_blank">Google Maps</a>
        <a href="https://www.google.com/maps/@?api=1&map_action=map&center=${e.lat},${e.lon}&zoom=15&basemap=satellite" target="_blank">${t("satellite")}</a>
        <button onclick="navigator.clipboard.writeText('${e.lat}, ${e.lon}');this.textContent='${t("copied")}'">${t("copyCoords")}</button>
      </div></div>`;
  }).join("");
  el.querySelectorAll(".ev").forEach(d=>d.onclick=ev=>{
    if(ev.target.tagName==="A"||ev.target.tagName==="BUTTON") return;
    select(d.dataset.id,true);
    if(isMobile()) setDrawer(false);   // otherwise the drawer hides the fire
  });
}

// Select (or deselect, if already selected) an event; optionally fly to it.
function select(id,fly){
  selected = selected===id?null:id;
  document.querySelectorAll(".ev").forEach(d=>d.classList.toggle("sel",d.dataset.id===selected));
  const e = EVENTS.find(x=>x.id===selected);
  if(e&&fly) map.flyTo([e.lat,e.lon],13,{duration:.6});
  drawDets(sliderTime());
}

// Render the range buttons and wire them to switch range and redraw everything.
function renderRange(){
  const el = document.getElementById("hrange");
  el.innerHTML = Object.keys(DATA.ranges||{}).map(k=>{
    const c = (DATA.range_counts||{})[k]||{};
    const label = t("r_"+k), short = t("rs_"+k);
    return `<button data-r="${k}" class="${k===RANGE?"on":""}" title="${label}">${short}${
      c.events!=null?`<b>(${c.events})</b>`:""}</button>`;
  }).join("");
  el.querySelectorAll("button").forEach(b=>b.onclick=()=>{
    RANGE = b.dataset.r;
    fwTrack("range_change", {range: RANGE});
    recompute(); renderRange(); renderHeader(); drawEvents(); renderList();
    setSliderTime(tMax); drawDets(sliderTime());
// clientWidth is only trustworthy after the first layout pass, and the track's
// end padding is half of it.
requestAnimationFrame(() => { rebuildSlider(); drawDets(sliderTime()); });
  });
}

// Update the sidebar header: status dot and title, update time, chips, footer.
function renderHeader(){
  const act = EVENTS.filter(e=>e.status==="active").length;
  const worstFrp = EVENTS.filter(e=>e.status==="active")
    .reduce((m,e)=>Math.max(m,e.max_frp||0),0);
  const sev = worstFrp>=200?"severe":worstFrp>=50?"high":worstFrp>=10?"moderate":worstFrp>0?"low":null;
  const col = act? (SEVC[sev]||"#ff6b35") : "#3fb950";
  const dot = document.getElementById("hdot");
  dot.style.background = col; dot.style.boxShadow = `0 0 9px ${col}`;
  document.getElementById("htitle").textContent = act
    ? t("activeFires",{n:act})
    : (EVENTS.length ? t("firesNoneActive",{n:EVENTS.length}) : t("noActive"));
  document.getElementById("hsub").textContent =
    t("sub",{t:fmtLocal(Date.parse(DATA.generated_at))});
  const badge = document.getElementById("drawer-badge");
  if(badge) badge.textContent = act ? String(act) : "";
  document.getElementById("hchips").innerHTML = [
    `<span class="chip">${t("detections")} <b>${dets.length}</b></span>`,
    ...Object.entries(DATA.source_status||{}).map(([k,v])=>
      `<span class="chip" title="${(v.detail||"").replace(/"/g,"")}">${k} <b style="color:${v.ok?"#3fb950":"#e63946"}">${v.ok?t("ok"):t("fail")}</b></span>`)
  ].join("");
  // The documentation is published at <site>/docs/ by the same Pages deploy, so it
  // is linked only when this instance has a public address; a local file:// map
  // has no sibling docs directory.
  const docsLink = DATA.public_url
    ? ` &nbsp;|&nbsp; <a href="docs/" class="foot-link">${t("docs")}</a>` : "";
  document.getElementById("foot").innerHTML =
    `Meteosat MTG · VIIRS/MODIS FIRMS · Sentinel-3 &nbsp;|&nbsp; ${t("boundary")}: OSM rel. 2528292${docsLink}`;
}

// Chrome on Android reports the visible height in innerHeight, so this keeps a
// usable viewport height for browsers without dvh. visualViewport fires as the
// URL bar slides away, which plain resize does not always cover.
function syncAppVh(){
  document.documentElement.style.setProperty("--appvh", window.innerHeight + "px");
}
syncAppVh();
addEventListener("resize", syncAppVh);
addEventListener("orientationchange", syncAppVh);
if(window.visualViewport) visualViewport.addEventListener("resize", syncAppVh);

// ---- mobile drawer ---------------------------------------------------------
const sideEl = document.getElementById("side");
const backdropEl = document.getElementById("backdrop");
const drawerBtn = document.getElementById("drawer-btn");
const isMobile = () => matchMedia("(max-width:880px)").matches;

// Open or close the mobile off-canvas drawer.
function setDrawer(open){
  sideEl.classList.toggle("open", open);
  backdropEl.classList.toggle("on", open);
  document.body.classList.toggle("drawer-open", open);
  drawerBtn.setAttribute("aria-expanded", open ? "true" : "false");
}
drawerBtn.onclick = () => setDrawer(!sideEl.classList.contains("open"));
backdropEl.onclick = () => setDrawer(false);
document.getElementById("drawer-close").onclick = () => setDrawer(false);
document.addEventListener("keydown", e => { if(e.key === "Escape") setDrawer(false); });
// Leaving mobile width must not leave the drawer state stuck on the docked panel.
matchMedia("(max-width:880px)").addEventListener("change", () => setDrawer(false));

const scrollEl  = document.getElementById("tlscroll");
const trackEl   = document.getElementById("tltrack");
const contentEl = document.getElementById("tlcontent");
const marksEl   = document.getElementById("tlmarks");
const ticksEl   = document.getElementById("tlticks");

// ---- timeline step -------------------------------------------------------
// The slider counts whole units so dragging lands on round times. Switching unit
// keeps the moment you were looking at and just changes the resolution.
const UNITS = [
  {key:"min",  ms:60000},
  {key:"hour", ms:3600000},
  {key:"day",  ms:86400000},
  {key:"week", ms:604800000},
];
let unitIx = 1;          // hour
let autoUnit = true;     // until the reader picks one explicitly
const unitMs = () => UNITS[unitIx].ms;
const unitBtn = document.getElementById("unit");

// Number of slider steps across the current range at the current unit.
function stepCount(){
  return Math.max(1, Math.ceil((tMax - tMin) / unitMs()));
}
// Pick the finest unit that keeps the slider usable: enough steps to scrub
// meaningfully, few enough that one nudge is not a month.
function pickUnit(span){
  for(let i = 0; i < UNITS.length; i++){
    const n = Math.ceil(span / UNITS[i].ms);
    if(n <= 400) return i;
  }
  return UNITS.length - 1;
}
// Every unit stays selectable on every range: a month in minutes is 43200 steps,
// coarse to drag but exact, and the reader asked for it. Only a pathological
// count is corrected.
const MAX_STEPS = 200000;
// Coarsen the unit until the step count is at most MAX_STEPS.
function clampUnit(){
  while(unitIx < UNITS.length - 1 && stepCount() > MAX_STEPS) unitIx++;
}
// Pixels per unit. A scroller can be arbitrarily long, so unlike a slider it
// loses no precision at fine units: a month of minutes is ~86000px of track,
// which scrolls perfectly well and is exact to the minute.
const PX_PER_UNIT = {min:2, hour:14, day:46, week:96};
const pxUnit = () => PX_PER_UNIT[UNITS[unitIx].key];
const trackPx = () => stepCount() * pxUnit();
const wrapW = () => scrollEl.clientWidth || 1;

// x within the content layer for a moment, and the inverse
const spansYears = () => tlParts(tMin).year !== tlParts(tMax).year;
const xOf = ms => ((ms - tMin) / unitMs()) * pxUnit();
const msOf = x => tMin + (x / pxUnit()) * unitMs();

// The moment under the centre playhead, clamped to the range.
function sliderTime(){
  return Math.min(tMax, Math.max(tMin, msOf(scrollEl.scrollLeft)));
}
// Scroll events arrive asynchronously, so a flag set and cleared around the
// assignment is already false when the event fires, and playback's own scrolling
// would look like a reader interruption. Remember the position we set instead and
// compare.
let progAt = -1;
function setScroll(x){
  progAt = x;
  scrollEl.scrollLeft = x;
}
const isOurScroll = () => Math.abs(scrollEl.scrollLeft - progAt) < 2;

// Scroll the timeline so `ms` sits under the playhead.
function setSliderTime(ms){
  const x = Math.min(trackPx(), Math.max(0, xOf(Math.min(Math.max(ms, tMin), tMax))));
  setScroll(x);
  paintTicks();
  syncAria();
}
// Re-lay-out the timeline track for the current range/unit, keeping the moment shown.
function rebuildSlider(){
  const prev = sliderTime();
  const half = wrapW() / 2;
  // Half a viewport of padding at each end so the first and last instants can
  // reach the centre playhead.
  trackEl.style.width = (trackPx() + wrapW()) + "px";
  contentEl.style.left = half + "px";
  contentEl.style.width = trackPx() + "px";
  let minor = pxUnit();
  while(minor < 9) minor *= 2;          // keep hairlines from merging into grey
  contentEl.style.backgroundImage =
    "repeating-linear-gradient(to right, #263442 0 1px, transparent 1px " + minor + "px)";
  paintMarks();
  setSliderTime(isFinite(prev) ? prev : tMax);
  if(unitBtn) unitBtn.textContent = t("u_" + UNITS[unitIx].key);
}

// Detection marks live on the track, so you can see when things happened and
// scroll straight to them instead of hunting blind.
function paintMarks(){
  marksEl.innerHTML = dets.map(d =>
    `<div class="tmk" style="left:${xOf(d.t).toFixed(1)}px;background:${
      SRC[d.source]?.c || "#fff"}"></div>`).join("");
}

// Labels are drawn only for the visible window - a month of minutes would be
// thousands of them otherwise.
function paintTicks(){
  // Wider spacing once labels can carry a year, or "14 Mar 2025" collides with
  // its neighbour at the spacing bare dates were measured for.
  const step = Math.max(1, Math.ceil((spansYears() ? 96 : 74) / pxUnit()));
  const from = Math.max(0, Math.floor((scrollEl.scrollLeft - wrapW() / 2) / pxUnit()) - step);
  const to = Math.min(stepCount(), Math.ceil((scrollEl.scrollLeft + wrapW()) / pxUnit()) + step);
  const out = [];
  for(let i = Math.floor(from / step) * step; i <= to; i += step){
    const ms = tMin + i * unitMs();
    out.push(`<div class="tk maj" style="left:${xOf(ms).toFixed(1)}px"></div>`);
    out.push(`<div class="tlab" style="left:${xOf(ms).toFixed(1)}px">${tickLabel(ms)}</div>`);
  }
  ticksEl.innerHTML = out.join("");
}
// Ruler label for a tick: the clock time, or the date at midnight / on day+ units.
function tickLabel(ms){
  const u = UNITS[unitIx].key;
  const p = tlParts(ms);
  if(u === "min" || u === "hour")
    return (p.hour === "00" && p.minute === "00")
      ? `${p.day} ${monName(p.month)}${yearBit(p)}` : `${p.hour}:${p.minute}`;
  return `${p.day} ${monName(p.month)}${yearBit(p)}`;
}

// Keep the scroller's ARIA slider values in step with its position.
function syncAria(){
  scrollEl.setAttribute("aria-valuemin", "0");
  scrollEl.setAttribute("aria-valuemax", String(stepCount()));
  scrollEl.setAttribute("aria-valuenow",
    String(Math.round((sliderTime() - tMin) / unitMs())));
  scrollEl.setAttribute("aria-valuetext", fmtStamp(sliderTime()));
}

let rafPending = false;
scrollEl.addEventListener("scroll", () => {
  // A scroll the reader started should take over from playback.
  if(timer && !isOurScroll()) stopPlay();
  if(rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => {
    rafPending = false;
    paintTicks();
    syncAria();
    drawDets(sliderTime());
  });
}, {passive:true});

// A scroller is not keyboard-navigable on its own.
scrollEl.addEventListener("keydown", e => {
  const one = pxUnit(), big = one * 10;
  const map = {ArrowLeft:-one, ArrowRight:one, PageUp:-big, PageDown:big,
               Home:-Infinity, End:Infinity};
  if(!(e.key in map)) return;
  e.preventDefault();
  stopPlay();
  const d = map[e.key];
  setScroll(d === -Infinity ? 0
    : d === Infinity ? trackPx() : scrollEl.scrollLeft + d);
  paintTicks(); syncAria(); drawDets(sliderTime());
});

map.on("zoomend", () => { drawEvents(); drawDets(sliderTime()); });
addEventListener("resize", () => rebuildSlider());
let timer=null;
// Playback speed is a rate over timeline *steps*, not a fixed duration per pass,
// so the unit button also chooses pace: minutes crawl, days sweep. The tick
// interval stays fixed so motion stays smooth regardless.
const SPEEDS = [{label:"0.5\u00d7", mult:0.5},
                {label:"1\u00d7",   mult:1},
                {label:"2\u00d7",   mult:2},
                {label:"4\u00d7",   mult:4}];
const STEPS_PER_SEC = 4;                           // at 1x
// Both ends need a guard: a 3-day range in day steps is 3 steps and would be a
// blink, and 30 days in minute steps is 43200 and would run for three hours.
// The ceiling is generous because minute steps are a deliberate choice to watch a
// fire develop; 4x is the escape hatch, not a lower ceiling.
const MIN_PASS_MS = 10000, MAX_PASS_MS = 600000;
const TICK_MS = 60;
let speedIx = 1;                                   // 1x by default

// How long one pass over the whole range should take at the current unit+speed.
function passMs(){
  const raw = 1000 * stepCount() / (STEPS_PER_SEC * SPEEDS[speedIx].mult);
  return Math.min(MAX_PASS_MS, Math.max(MIN_PASS_MS, raw));
}
const playBtn = document.getElementById("play");
const speedBtn = document.getElementById("speed");
speedBtn.textContent = SPEEDS[speedIx].label;

// Stop playback and reset the play button.
function stopPlay(){
  if(timer){ clearInterval(timer); timer = null; }
  playBtn.innerHTML = "&#9654;";
  playBtn.title = t("animate");
}

// Start playback from the start of the range, or from the current position.
function startPlay(fromHere){
  if(timer){ clearInterval(timer); timer = null; }
  playBtn.innerHTML = "&#10074;&#10074;";
  playBtn.title = t("pause");
  const total = trackPx();
  if(!fromHere) setSliderTime(tMin);
  // Scroll position is animated in pixels, so a fine unit stays smooth rather
  // than stepping - the track is long, not coarse.
  let pos = scrollEl.scrollLeft;
  const per = total / (passMs() / TICK_MS);
  timer = setInterval(() => {
    pos = Math.min(total, pos + per);
    setScroll(pos);
    paintTicks(); syncAria();
    drawDets(sliderTime());
    if(pos >= total) stopPlay();
  }, TICK_MS);
}

playBtn.onclick = () => timer ? stopPlay() : startPlay(false);
unitBtn.textContent = t("u_" + UNITS[unitIx].key);
unitBtn.onclick = () => {
  const at = sliderTime();            // keep the moment being viewed
  unitIx = (unitIx + 1) % UNITS.length;
  autoUnit = false;
  clampUnit();
  rebuildSlider();
  setSliderTime(at);
  drawDets(sliderTime());
  // The running timer holds a pixel position and a rate from the old track;
  // both are meaningless now that the unit - and so the pace - changed.
  if(timer) startPlay(true);
};

speedBtn.onclick = () => {
  speedIx = (speedIx + 1) % SPEEDS.length;
  speedBtn.textContent = SPEEDS[speedIx].label;
  // Already running: adopt the new speed without losing the current position.
  if(timer) startPlay(true);
};

// ---- measure tool ----------------------------------------------------------
// Distance along a line, and the area of a polygon, drawn by hand rather than by
// pulling in Leaflet.draw + Leaflet.measure: that is two more CDN files and a
// light theme to override, when all this actually needs is a polyline, a polygon
// and two bits of spherical maths. Measurements live in their own pane and layer
// group, so the 60 s refresh - which rebuilds every fire layer from scratch -
// never touches them, and neither does a range change.
map.createPane("measure").style.zIndex = 650;
const M_PANE = "measure", M_COL = "#7cffb2";
const mLayer = L.layerGroup().addTo(map);

const COMPASS16 = Object.keys(COMPASS_BS);      // already in compass order
// Initial great-circle bearing in degrees from a to b.
function bearingDeg(a, b){
  const r = Math.PI/180, p1 = a.lat*r, p2 = b.lat*r, dl = (b.lng - a.lng)*r;
  const y = Math.sin(dl)*Math.cos(p2);
  const x = Math.cos(p1)*Math.sin(p2) - Math.sin(p1)*Math.cos(p2)*Math.cos(dl);
  return (Math.atan2(y, x)*180/Math.PI + 360) % 360;
}

// Spherical excess, never the projected pixel area: Web Mercator inflates area by
// 1/cos(lat)^2, which is ~1.94x at 44 N - a 100 ha burn scar would read as 194 ha.
function geoAreaM2(pts){
  if(pts.length < 3) return 0;
  const r = Math.PI/180;
  let sum = 0;
  for(let i=0; i<pts.length; i++){
    const p1 = pts[i], p2 = pts[(i+1) % pts.length];
    sum += (p2.lng - p1.lng)*r * (2 + Math.sin(p1.lat*r) + Math.sin(p2.lat*r));
  }
  return Math.abs(sum * R_EARTH_M * R_EARTH_M / 2);
}
const segM = (a,b) => haversineM(a.lat, a.lng, b.lat, b.lng);
// Length in metres of a path, closing back to the start for polygons.
function pathM(pts, closed){
  let m = 0;
  for(let i=1; i<pts.length; i++) m += segM(pts[i-1], pts[i]);
  if(closed && pts.length > 2) m += segM(pts[pts.length-1], pts[0]);
  return m;
}
// Locale-formatted number with d decimals.
function num(n, d){
  try {
    return n.toLocaleString(LANG === "bs" ? "bs-BA" : "en-GB",
      {minimumFractionDigits:d, maximumFractionDigits:d});
  } catch(e){ return n.toFixed(d); }
}
const fmtLen = m => m < 1000 ? num(m,0) + " m"
                             : num(m/1000, m < 10000 ? 2 : 1) + " km";
// Hectares alongside km2: forest and burn-scar sizes are quoted in hectares here,
// and 1 km2 = 100 ha is not a conversion anyone does mid-sentence.
function fmtArea(a){
  if(a < 1e4) return num(a, 0) + " m²";
  if(a < 1e6) return num(a/1e4, 2) + " ha";
  return num(a/1e6, 2) + " km² · " + num(a/1e4, 0) + " ha";
}

const mTip = (latlng, html, cls, opts) => L.tooltip(Object.assign(
  {permanent:true, direction:"center", className:"mlabel " + (cls || ""),
   interactive:false, opacity:1}, opts || {})).setLatLng(latlng).setContent(html);

let mMode = null;         // null | "dist" | "area"
let draft = null;         // the measurement being drawn
const mShapes = [];       // finished ones, in the order they were drawn
const needPts = () => mMode === "area" ? 3 : 2;

// Perimeter/length and area for a measurement.
function shapeStats(mode, pts){
  const closed = mode === "area";
  return {len: pathM(pts, closed), area: closed ? geoAreaM2(pts) : 0};
}
// HTML summary (size, plus bearing for a two-point line) shown on a measurement.
function shapeSummary(mode, pts){
  const s = shapeStats(mode, pts);
  if(mode === "area")
    return `<b>${fmtArea(s.area)}</b><br><span style="opacity:.72">` +
           `${t("m_perimeter")} ${fmtLen(s.len)}</span>`;
  // A two-point line is a bearing as much as a distance - which way a fire would
  // have to run to cover it is the question being asked.
  if(pts.length === 2){
    const b = bearingDeg(pts[0], pts[1]);
    return `<b>${fmtLen(s.len)}</b><br><span style="opacity:.72">` +
           `${dir(COMPASS16[Math.round(b/22.5) % 16])} ${num(b,0)}°</span>`;
  }
  return `<b>${fmtLen(s.len)}</b>`;
}

const mVert = (p, r) => L.circleMarker(p, {pane:M_PANE, radius:r, color:"#0b0f14",
  weight:1.4, fillColor:M_COL, fillOpacity:1, interactive:false});

// Draw a finished measurement (shape, vertices, segment labels, summary, remove popup).
function renderShape(sh){
  const closed = sh.mode === "area";
  const shape = closed
    ? L.polygon(sh.pts, {pane:M_PANE, color:M_COL, weight:2.2, opacity:.95,
        fillColor:M_COL, fillOpacity:.13})
    : L.polyline(sh.pts, {pane:M_PANE, color:M_COL, weight:2.8, opacity:.95});
  sh.layers = [shape];
  // Added before the label is built: L.Polygon.getCenter() throws outright
  // ("Must add layer to map before using getCenter") until the layer is on a map.
  mLayer.addLayer(shape);
  sh.pts.forEach(p => sh.layers.push(mVert(p, 3.4)));
  // Per-segment lengths, but only while they stay readable - a twenty-vertex
  // trace turns into a wall of overlapping labels otherwise.
  const segs = sh.pts.length - (closed ? 0 : 1);
  if(sh.pts.length > 2 && segs <= 12){
    for(let i=0; i<segs; i++){
      const a = sh.pts[i], b = sh.pts[(i+1) % sh.pts.length];
      sh.layers.push(mTip(L.latLng((a.lat+b.lat)/2, (a.lng+b.lng)/2),
        fmtLen(segM(a,b)), "seg"));
    }
  }
  const sum = shapeSummary(sh.mode, sh.pts);
  sh.layers.push(mTip(closed ? shape.getCenter() : sh.pts[sh.pts.length-1], sum));
  const pop = L.DomUtil.create("div", "mpop");
  pop.innerHTML = sum + `<div><button type="button">${t("m_remove")}</button></div>`;
  pop.querySelector("button").onclick = () => removeShape(sh);
  shape.bindPopup(pop);
  sh.layers.forEach(l => { if(l !== shape) mLayer.addLayer(l); });
}

// Delete one finished measurement and its layers.
function removeShape(sh){
  map.closePopup();
  (sh.layers || []).forEach(l => mLayer.removeLayer(l));
  const i = mShapes.indexOf(sh);
  if(i >= 0) mShapes.splice(i, 1);
  mRefresh();
}
const clearShapes = () => mShapes.slice().forEach(removeShape);

// Labels carry translated words and locale-formatted numbers, so a language
// switch has to rebuild them. The geometry is kept; only the layers are redrawn.
function relabelShapes(){
  mShapes.forEach(sh => {
    (sh.layers || []).forEach(l => mLayer.removeLayer(l));
    renderShape(sh);
  });
}

// Start a fresh in-progress measurement for the current mode.
function newDraft(){
  const closed = mMode === "area";
  draft = {pts:[], verts:[], live:null};
  draft.line = closed
    ? L.polygon([], {pane:M_PANE, color:M_COL, weight:2.2, opacity:.95,
        dashArray:"6,5", fillColor:M_COL, fillOpacity:.10, interactive:false})
    : L.polyline([], {pane:M_PANE, color:M_COL, weight:2.6, opacity:.95,
        interactive:false});
  draft.rubber = L.polyline([], {pane:M_PANE, color:M_COL, weight:1.6, opacity:.8,
    dashArray:"4,5", interactive:false});
  mLayer.addLayer(draft.line);
  mLayer.addLayer(draft.rubber);
}
// Discard the in-progress measurement's layers.
function clearDraft(){
  if(!draft) return;
  [draft.line, draft.rubber, draft.live].concat(draft.verts)
    .forEach(l => { if(l) mLayer.removeLayer(l); });
  draft = null;
}
// Redraw the in-progress measurement from its points.
function drawDraft(){
  if(!draft) return;
  draft.line.setLatLngs(draft.pts);
  draft.verts.forEach(v => mLayer.removeLayer(v));
  draft.verts = draft.pts.map(p => mVert(p, 3.6));
  draft.verts.forEach(v => mLayer.addLayer(v));
  if(!draft.pts.length) draft.rubber.setLatLngs([]);
  mPanelUpdate();
}

// Add a vertex to the draft, ignoring a near-duplicate click.
function addPoint(ll){
  if(!draft) return;
  const n = draft.pts.length;
  if(n){
    // The second click of a finishing double-click arrives as a click first;
    // dropping a near-duplicate keeps it from adding a zero-length segment.
    const a = map.latLngToContainerPoint(draft.pts[n-1]);
    if(a.distanceTo(map.latLngToContainerPoint(ll)) < 12) return;
  }
  draft.pts.push(ll);
  drawDraft();
}
// Drop the draft's last vertex.
function undoPoint(){
  if(!draft || !draft.pts.length) return;
  draft.pts.pop();
  drawDraft();
}
// Commit the draft as a finished measurement and arm a new one.
function finishDraft(){
  if(!draft || draft.pts.length < needPts()) return;
  const sh = {mode:mMode, pts:draft.pts.slice(), layers:[]};
  clearDraft();
  mShapes.push(sh);
  renderShape(sh);
  newDraft();                 // the tool stays armed for the next measurement
  mPanelUpdate(); mRefresh();
}

// Update the rubber band (and live label) as the pointer moves while measuring.
function onMeasureMove(e){
  if(!draft || !draft.pts.length) return;
  const closed = mMode === "area", last = draft.pts[draft.pts.length-1];
  // Closing leg included once the polygon has real area, so the rubber band shows
  // the shape that would actually be measured, not an open chain.
  draft.rubber.setLatLngs(closed && draft.pts.length > 1
    ? [last, e.latlng, draft.pts[0]] : [last, e.latlng]);
  const pts = draft.pts.concat([e.latlng]);
  let html = closed && pts.length > 2 ? fmtArea(geoAreaM2(pts))
                                      : fmtLen(pathM(pts, false));
  html += `<br><span style="opacity:.7;font-weight:400">+${fmtLen(segM(last, e.latlng))}</span>`;
  if(!draft.live){
    // Beside the cursor, not under it - the crosshair has to stay visible.
    draft.live = mTip(e.latlng, html, "live", {direction:"right", offset:[14,0]});
    mLayer.addLayer(draft.live);
  } else draft.live.setLatLng(e.latlng).setContent(html);
}

const mPanelCtl = L.control({position:"topright"});
mPanelCtl.onAdd = () => {
  const d = L.DomUtil.create("div", "mpanel");
  L.DomEvent.disableClickPropagation(d);
  L.DomEvent.disableScrollPropagation(d);
  d.innerHTML = `<div class="mrow"></div><div class="mhint"></div>` +
    `<div class="macts"><button type="button" class="pri mdone"></button>` +
    `<button type="button" class="mundo"></button>` +
    `<button type="button" class="mexit"></button></div>`;
  d.querySelector(".mdone").onclick = () => finishDraft();
  d.querySelector(".mundo").onclick = () => undoPoint();
  d.querySelector(".mexit").onclick = () => exitMeasure();
  mPanelCtl._el = d;
  return d;
};
// Refresh the measure panel's readout, hint and button states.
function mPanelUpdate(){
  const el = mPanelCtl._el;
  if(!el || !mMode) return;
  const n = draft ? draft.pts.length : 0, closed = mMode === "area";
  const row = el.querySelector(".mrow");
  if(n >= needPts()){
    const s = shapeStats(mMode, draft.pts);
    row.innerHTML = closed
      ? `${fmtArea(s.area)}<br><span style="opacity:.7;font-size:11px;font-weight:400">` +
        `${t("m_perimeter")} ${fmtLen(s.len)}</span>`
      : fmtLen(s.len);
  } else row.textContent = n ? t(closed ? "m_needArea" : "m_needDist") : "";
  el.querySelector(".mhint").textContent = t(closed ? "m_hintArea" : "m_hintDist");
  const done = el.querySelector(".mdone"), undo = el.querySelector(".mundo");
  done.textContent = t("m_done"); undo.textContent = t("m_undo");
  el.querySelector(".mexit").textContent = t("m_close");
  done.disabled = n < needPts();
  undo.disabled = !n;
}

const mCtl = L.control({position:"topleft"});
mCtl.onAdd = () => {
  const d = L.DomUtil.create("div", "leaflet-bar mbar");
  const mk = (glyph, cls, fn) => {
    const a = L.DomUtil.create("a", cls, d);
    a.href = "#"; a.innerHTML = glyph;
    a.style.fontSize = "14px"; a.style.textAlign = "center";
    L.DomEvent.on(a, "click", e => { L.DomEvent.stop(e); fn(); });
    return a;
  };
  mk("📏", "m-dist", () => enterMeasure("dist"));
  mk("⬠",       "m-area", () => enterMeasure("area"));
  mk("✕",       "m-clear", clearShapes);
  mCtl._d = d;
  mLabels(); mRefresh();
  return d;
};
// Set the measure buttons' tooltips in the current language.
function mLabels(){
  const d = mCtl._d;
  if(!d) return;
  d.querySelector(".m-dist").title = t("m_dist");
  d.querySelector(".m-area").title = t("m_area");
  d.querySelector(".m-clear").title = t("m_clear");
}
// Reflect the active mode and presence of finished shapes on the measure buttons.
function mRefresh(){
  const d = mCtl._d;
  if(!d) return;
  d.querySelector(".m-dist").classList.toggle("on", mMode === "dist");
  d.querySelector(".m-area").classList.toggle("on", mMode === "area");
  d.querySelector(".m-clear").style.display = mShapes.length ? "" : "none";
}

// Arm the measure tool in `mode`, or disarm it if that mode is already active.
function enterMeasure(mode){
  if(mMode === mode){ exitMeasure(); return; }
  clearDraft();
  mMode = mode;
  L.DomUtil.addClass(map.getContainer(), "measuring");
  map.doubleClickZoom.disable();      // double-click ends a measurement instead
  newDraft();
  mPanelCtl.remove(); mPanelCtl.addTo(map);
  setDrawer(false);                  // on a phone the drawer covers the map
  mPanelUpdate(); mRefresh();
}
// Disarm the measure tool and discard any draft.
function exitMeasure(){
  clearDraft();
  mMode = null;
  L.DomUtil.removeClass(map.getContainer(), "measuring");
  map.doubleClickZoom.enable();
  mPanelCtl.remove();
  mRefresh();
}
mCtl.addTo(map);

map.on("click", e => {
  // See lastDismissAt above: a click that just closed a popup reads as "now",
  // inside this 50 ms window, so it drops no point. A later click is a
  // deliberate new point.
  if(mMode && Date.now() - lastDismissAt > 50) addPoint(e.latlng);
});
map.on("dblclick", () => { if(mMode) finishDraft(); });
map.on("mousemove", e => { if(mMode) onMeasureMove(e); });
document.addEventListener("keydown", e => {
  if(!mMode) return;
  if(e.key === "Escape") exitMeasure();
  else if(e.key === "Enter"){ e.preventDefault(); finishDraft(); }
  else if(e.key === "Backspace"){ e.preventDefault(); undoPoint(); }
});

// ---- language switching ----------------------------------------------------
// The legend and the layer control build their labels in onAdd, so they are
// removed and re-added rather than patched in place.
// Apply the current language to the static labels and tooltips.
function applyStaticLabels(){
  document.documentElement.lang = LANG;
  document.getElementById("drawer-label").textContent = t("drawerFires");
  const db = document.getElementById("drawer-btn");
  db.setAttribute("aria-label", t("showList"));
  document.getElementById("drawer-close").setAttribute("aria-label", t("closeList"));
  playBtn.title = timer ? t("pause") : t("animate");
  speedBtn.title = t("speed");
  unitBtn.title = t("step");
  unitBtn.textContent = t("u_" + UNITS[unitIx].key);
  document.querySelectorAll("#langsw button").forEach(b =>
    b.classList.toggle("on", b.dataset.l === LANG));
  mLabels(); mPanelUpdate();
}

// Remove and re-add the controls so their labels pick up the current language.
function rebuildControls(){
  legend.remove(); legend.addTo(map);
  const wasOpen = layersCtl._el.classList.contains("open");
  expandablePanels.splice(expandablePanels.indexOf(layersCtl), 1);
  layersCtl.remove();
  layersCtl = makeLayersCtl();
  if(wasOpen) setPanelOpen(layersCtl, true);
  zoomBtn.remove(); zoomBtn.addTo(map);
  eyeBtn.remove(); eyeBtn.addTo(map);
  imgCtl.remove(); imgCtl.addTo(map);
  syncImagery(sliderTime());
  mCtl.remove(); mCtl.addTo(map);
  if(mMode){ mPanelCtl.remove(); mPanelCtl.addTo(map); mPanelUpdate(); }
  relabelShapes();
}

// Full re-render from DATA (used after a language switch).
function renderAll(){
  applyStaticLabels();
  s2Sync();
  recompute(); renderRange(); renderHeader(); drawEvents(); renderList();
  drawDets(sliderTime());
}

// Switch language, remember the choice, and rebuild everything that shows text.
function setLang(l){
  if(!I18N[l] || l === LANG) return;
  LANG = l;
  fwTrack("language_change", {language: l});
  try { localStorage.setItem("fw_lang", l); } catch(e) { /* storage may be blocked */ }
  rebuildControls();
  renderAll();
}
document.querySelectorAll("#langsw button").forEach(b =>
  b.addEventListener("click", () => setLang(b.dataset.l)));

applyStaticLabels();
s2Sync();
recompute(); renderRange(); renderHeader(); drawEvents(); renderList();
setSliderTime(tMax); drawDets(sliderTime());
// Deep link from a Telegram alert: ?lat=..&lon=..&z=..&e=<event id>. Runs once, at load -
// never from applyData, so the 60 s refresh cannot yank the view back (landmine 11).
// Coordinates are checked against Bosnia and Herzegovina's box so a mangled or hostile
// link falls through to the normal country view instead of flying the map to the ocean.
(function deepLink(){
  let q;
  try { q = new URLSearchParams(location.search); } catch(e) { return; }
  const lat = parseFloat(q.get("lat")), lon = parseFloat(q.get("lon"));
  if(!(lat >= 42.4 && lat <= 45.4 && lon >= 15.6 && lon <= 19.7)) return;
  const z = Math.min(16, Math.max(6, parseInt(q.get("z"), 10) || 14));
  map.setView([lat, lon], z, {animate:false});
  const ev = EVENTS.find(x => x.id === q.get("e"));
  if(ev){
    select(ev.id, false);
    infoOpenId = ev.id;
    openInfoPanel(popupHtml(ev));
  }
  fwTrack("deeplink_open", {event_found: !!ev});
})();
// The initial DATA is inlined for first paint (see the module docstring), so the
// loading cover never waits on the network. It hides once the first synchronous
// render above has run and the minimum display time has passed.
const LOAD_MIN_MS = 3000;
setTimeout(() => {
  const ld = document.getElementById("loading");
  if(!ld) return;
  ld.classList.add("hide");
  // visibility:hidden alone does not stop CSS animations: the scene's infinite
  // loops (fire flicker, smoke, the HUD ring) would keep computing every frame
  // on an invisible element. display:none stops them; it waits for the .6s fade
  // so the transition is still visible.
  setTimeout(() => { ld.style.display = "none"; }, 600);
}, Math.max(0, LOAD_MIN_MS - (Date.now() - LOAD_START)));
// ---- live refresh without losing the reader's place ------------------------
// A file:// page cannot fetch() a sibling JSON file, but it can load one as a
// script. The poller rewrites fire-map-data.js each cycle; we pull it in with a
// cache-busted <script> tag and re-render from it. Map centre, zoom, selected
// range, selected fire and slider position are all left untouched, which a
// location.reload() would discard.
//
// This is how often the page re-checks, not how often the data changes: the hosted
// copy is polled by an external scheduler every ~15 minutes. Checking every minute
// is nearly free (one script tag against a static file) and shows a fresh publish
// within a minute of landing. The header's "updated {t}" line is the honest answer
// to "how fresh is this".
const REFRESH_MS = window.__fwRefreshMs || 60000;
let refreshFails = 0;

// Adopt a freshly loaded snapshot and redraw without moving the reader's view.
// Returns false when it carries nothing new.
function applyData(d){
  if(!d || !d.generated_at) return false;
  if(d.generated_at === DATA.generated_at) return false;   // nothing new
  const at = sliderTime();                                 // remember the moment shown
  DATA = d;
  // Past the generated_at guard, so this is genuinely new data, never a quiet
  // cycle. Markers come back on: a fire that has just changed outranks whatever
  // the reader was looking at underneath.
  if(!markersOn){ markersOn = true; applyMarkers(); }
  // A new scene can arrive with any refresh. s2Sync rebuilds the layer control
  // when it does, which is safe here only because it never touches the view -
  // no fitBounds, no setView, no flyTo.
  s2Sync();
  if(!DATA.range_cutoffs || !DATA.range_cutoffs[RANGE]) RANGE = DATA.default_range || "3d";
  const wasOpen = infoOpenId;
  recompute();
  renderRange(); renderHeader(); drawEvents(); renderList();
  setSliderTime(at);
  drawDets(sliderTime());
  if(wasOpen && markersOn){
    // Refresh the panel with the event's new data (FRP, status, age...); a fire
    // that is changing is the one a reader is watching. If the event no longer
    // exists in this range, close the panel rather than leave stale info.
    const ev = EVENTS.find(x=>x.id===wasOpen);
    if(ev) openInfoPanel(popupHtml(ev)); else { infoOpenId = null; closeInfoPanel(); }
  }
  const sub = document.getElementById("hsub");
  sub.classList.add("flash");
  setTimeout(()=>sub.classList.remove("flash"), 900);
  return true;
}

// Load the sibling data script via a cache-busted <script> tag, apply it, and
// reschedule; falls back to a reload after repeated failures.
function refresh(){
  // Never redraw mid-animation; just try again on the next tick.
  if(timer){ setTimeout(refresh, 2000); return; }
  const s = document.createElement("script");
  s.src = DATA_URL + "?t=" + Date.now();
  s.onload = () => {
    refreshFails = 0;
    try { applyData(window.__fwData); } catch(err) { console.error(err); }
    s.remove();
    setTimeout(refresh, REFRESH_MS);
  };
  s.onerror = () => {
    s.remove();
    // If the sibling script cannot be loaded at all, reload rather than
    // silently going stale.
    if(++refreshFails >= 3){ location.reload(); return; }
    setTimeout(refresh, REFRESH_MS);
  };
  document.body.appendChild(s);
}
setTimeout(refresh, REFRESH_MS);
</script></body></html>
"""


def data_path_for(html_path: Path) -> Path:
    """Sibling data file for a given page path."""
    return html_path.with_name(html_path.stem + "-data.js")


def write_data(snapshot: dict, html_path: Path | None = None) -> Path:
    """Write the refreshable data file, atomically.

    os.replace is atomic on the same filesystem, so a page refreshing at the same
    moment can never read a half-written file.
    """
    out = data_path_for(Path(html_path or MAP_PATH))
    payload = json.dumps(snapshot, ensure_ascii=False)
    tmp = out.with_suffix(out.suffix + ".tmp")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(f"window.__fwData = {payload};\n", encoding="utf-8")
    os.replace(tmp, out)
    return out


def sync_public(html_path: Path | None = None) -> bool:
    """Mirror the map into PUBLIC_DIR, but only if that directory already exists.

    `firewatch-ctl expose` creates it; until then this is a no-op, so nothing is
    ever staged for publication unless the user asked for it. Only the two map
    files are copied - the log, database and snapshot stay private.
    """
    if not PUBLIC_DIR.is_dir():
        return False
    src = Path(html_path or MAP_PATH)
    # The Sentinel-2 render rides along: only this list is copied, so leaving it
    # out would leave the published map (the URL in every SMS) with an empty
    # layer while the local file:// map looks perfect.
    for f in (src, data_path_for(src), src.parent / imagery.IMAGE_NAME):
        if f.exists():
            shutil.copy2(f, PUBLIC_DIR / f.name)
    # Also publish the page as index.html so the bare URL opens the map instead
    # of a directory listing - that is the link people actually share.
    if src.exists():
        shutil.copy2(src, PUBLIC_DIR / "index.html")
    return True


def bih_municipalities_geojson() -> dict:
    """All 145 BiH municipality boundaries as one FeatureCollection, each
    feature tagged with the id/name the map needs for its tooltip.

    Returns {} when the data is missing (a checkout without data/bih/), so the
    layer is omitted rather than the page failing to render, like a missing
    buffer band.

    `telegram_invite` rides along: an invite link is meant to be public, unlike
    the Sentinel Hub instance id, so embedding it in the published page is fine.
    It is None for a municipality not yet provisioned; muniPopupHtml() then omits
    the subscribe link rather than show one that 404s.
    """
    try:
        rows = json.loads(BIH_MUNI_FILE.read_text())
    except (OSError, ValueError):
        return {}
    features = []
    for r in rows:
        try:
            fc = json.loads((BIH_DATA_DIR / r["boundary"]).read_text())
        except (OSError, ValueError, KeyError):
            continue
        features.append({
            "type": "Feature",
            "properties": {"id": r["id"], "name": r["short_name"],
                           "telegram_invite": r.get("telegram_invite")},
            "geometry": fc["features"][0]["geometry"],
        })
    return {"type": "FeatureCollection", "features": features}


def render(snapshot: dict, path: Path | None = None) -> Path:
    """Render the snapshot into the map page, write its data file, and mirror to
    PUBLIC_DIR when that exists. Returns the page path."""
    out = Path(path or MAP_PATH)
    boundary = json.loads(BOUNDARY_GEOJSON.read_text())
    # None when the artifact is missing or was built for a different buffer
    # distance; the page then simply omits the band rather than drawing a
    # confident line in the wrong place. `python3 -m firewatch buffer` rebuilds it.
    band = geo.load_buffer()
    bih_munis = bih_municipalities_geojson()
    html = (TEMPLATE
            .replace("__FIREBASE__", json.dumps(firebase_config(), separators=(",", ":")).replace("</", "<\\/"))
            .replace("__DATA__", json.dumps(snapshot, ensure_ascii=False))
            .replace("__BOUNDARY__", json.dumps(boundary, separators=(",", ":")))
            .replace("__BUFFER__", json.dumps(band, separators=(",", ":")))
            .replace("__BIH_MUNICIPALITIES__", json.dumps(bih_munis, separators=(",", ":")))
            .replace("__DATA_JS__", data_path_for(out).name)
            .replace("__TOWN_LAT__", repr(TOWN_LAT))
            .replace("__TOWN_LON__", repr(TOWN_LON))
            .replace("__SPLASH_SVG__", _splash_svg()))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    write_data(snapshot, out)
    sync_public(out)
    return out
