"""Render the live fire map.

Two files are written side by side:

  fire-map.html       the page, with the current snapshot inlined for first paint
  fire-map-data.js    the same snapshot as `window.__fwData = {...}`

A file:// page cannot XHR a sibling JSON file under Chrome/Safari CORS rules, but it
*can* pull in a sibling script via a <script src> tag. So instead of reloading, the
page periodically appends a cache-busted script tag and re-renders from the assigned
object. That keeps the map view, the selected range, the selected fire and the
timeline position exactly where the reader left them - a full reload threw all of
that away every minute. If script injection ever fails, it falls back to reloading.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import geo
from .config import BOUNDARY_GEOJSON, MAP_PATH, PUBLIC_DIR

TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FireWatch Zavidovići</title>
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
  .legend{background:rgba(21,26,33,.94);padding:9px 11px;border-radius:9px;
    border:1px solid var(--line);color:var(--dim);font-size:11.5px;line-height:1.7}
  .legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
  .legend-toggle{display:none}
  .leaflet-bottom.leaflet-right,
  .leaflet-bottom.leaflet-left{margin-bottom:94px}
  .leaflet-popup-content-wrapper{background:var(--panel);color:var(--fg);border-radius:9px}
  .leaflet-popup-tip{background:var(--panel)}
  .leaflet-popup-content{margin:11px 13px;font-size:12.5px}
  .leaflet-popup-content b{color:var(--accent)}
  .leaflet-popup-content a{color:#7cc4ff}
  .leaflet-bar a{background:var(--panel2);color:var(--fg);border-color:var(--line)}
  .leaflet-bar a:hover{background:#31404f}
  /* --- measure tool -------------------------------------------------------
     While measuring, the fire markers and the boundary must not swallow a click
     that is meant to drop a vertex, so hit testing is turned off for the panes
     that hold them. The measure pane is a sibling of the overlay pane, hence its
     own rule - finished measurements are clickable (for their remove popup) only
     when the tool is off. */
  .leaflet-container.measuring{cursor:crosshair}
  /* !important is load-bearing: Leaflet's own
     `.leaflet-pane>svg path.leaflet-interactive` rule is more specific than this
     one, so without it a click meant for a vertex opens a fire popup instead. */
  .measuring .leaflet-overlay-pane path,
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
  #langsw{position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:1250;
    display:flex;gap:2px;padding:2px;border-radius:9px;border:1px solid var(--line);
    background:rgba(21,26,33,.94);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
  #langsw button{background:none;border:0;color:var(--dim);font:inherit;font-size:11.5px;
    font-weight:600;letter-spacing:.03em;padding:5px 11px;border-radius:7px;cursor:pointer}
  #langsw button:hover{color:var(--fg)}
  #langsw button.on{background:var(--accent);color:#fff}
  .pulse{animation:pulse 2.1s ease-out infinite}
  @keyframes pulse{0%{r:8;opacity:.85}70%{opacity:0}100%{r:26;opacity:0}}
  /* Desktop keeps the panel docked; these two are only used on small screens. */
  #drawer-btn{display:none}
  #drawer-close{display:none}
  #backdrop{display:none}

  /* Phone/tablet: the panel becomes an off-canvas drawer so the map gets the whole
     screen. Stacking it wasted half the display, and min-height:auto on a flex item
     floored the panel at its content height - which is what left the map 61px tall. */
  @media (max-width:880px){
    #side{position:fixed;top:0;left:0;height:100%;height:100dvh;width:min(86vw,340px);
      flex:0 0 auto;min-height:0;z-index:1200;transform:translateX(-102%);
      transition:transform .26s ease;box-shadow:0 0 42px rgba(0,0,0,.55)}
    #side.open{transform:none}
    #map{flex:1 1 auto;min-height:0}
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
    /* the ruler onto its own row, full width - beside the buttons it was too
       narrow to scrub, and the bar overflowed */
    #tlwrap{order:9;flex:1 1 100%;margin-top:3px}
    #list{padding:8px}
    .mpanel{width:min(52vw,190px);padding:8px 9px}
    /* The legend used to be hidden here because it sat on top of the timeline bar.
       Instead, lift the whole bottom-right control stack clear of the bar and make
       the legend collapse to a single "Key" button, so it is reachable without
       permanently covering a phone-sized map. */
    .leaflet-bottom.leaflet-right,
    .leaflet-bottom.leaflet-left{
      margin-bottom:calc(124px + env(safe-area-inset-bottom, 0px))}
    /* attribution must stay visible, but it can be smaller on a phone */
    .leaflet-control-attribution{font-size:9.5px;padding:1px 5px}
    .legend{background:none;border:0;padding:0;line-height:1.6}
    .legend-toggle{display:block;width:100%;text-align:left;cursor:pointer;
      background:rgba(21,26,33,.94);border:1px solid var(--line);color:var(--fg);
      border-radius:9px;padding:8px 13px;font:inherit;font-size:12.5px;
      backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
    .legend-body{display:none;background:rgba(21,26,33,.96);border:1px solid var(--line);
      border-radius:9px;padding:10px 12px;margin-bottom:6px;max-width:74vw;
      max-height:46dvh;overflow-y:auto;
      backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
    .legend.open .legend-body{display:block}
  }
  @media (prefers-reduced-motion:reduce){#side,#backdrop{transition:none}}
</style></head><body>
<button id="drawer-btn" aria-controls="side" aria-expanded="false" aria-label="Show fire list">
  <span aria-hidden="true">&#9776;</span><span id="drawer-label">Fires</span><span id="drawer-badge"></span>
</button>
<div id="backdrop"></div>
<div id="langsw" role="group" aria-label="Language / Jezik">
  <button data-l="bs" type="button">BS</button><button data-l="en" type="button">EN</button>
</div>
<div id="wrap">
  <div id="side">
    <header>
      <button id="drawer-close" aria-label="Close fire list">&times;</button>
      <h1><span class="dot" id="hdot"></span><span id="htitle">FireWatch Zavidovići</span></h1>
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
let DATA = __DATA__;
const DATA_URL = "__DATA_JS__";
const BOUNDARY = __BOUNDARY__;
const BUFFER = __BUFFER__;
const SRC = {mtg:{c:"#4cc9f0",n:"Meteosat MTG (10 min)"},
             firms:{c:"#ffd166",n:"VIIRS/MODIS (NRT)"},
             s3:{c:"#b5179e",n:"Sentinel-3 SLSTR"}};
const SEVC = {low:"#ffa726",moderate:"#fb8c00",high:"#f4511e",severe:"#d81b3c",unknown:"#8b98a5"};

// ---- localisation ----------------------------------------------------------
// Bosnian plurals need three forms, so counted strings are arrays [one,few,many]
// and plural() picks by the Slavic rule. English uses [one, other, other].
const I18N = {
  en: {
    sub:"Grad Zavidovići · updated {t}", noActive:"No active fires",
    activeFires:["{n} active fire","{n} active fires","{n} active fires"],
    firesNoneActive:["{n} fire, none active","{n} fires, none active","{n} fires, none active"],
    detections:"detections", ok:"ok", fail:"fail",
    drawerFires:"Fires", showList:"Show fire list", closeList:"Close fire list",
    key:"Key", legDet:"Detections", legSev:"Fire severity", legSize:"(size ∝ FRP)",
    legState:"State", legBurning:"solid & filled — burning now",
    legQuiet:"dashed — quiet / out",
    sev_low:"low", sev_moderate:"moderate", sev_high:"high", sev_severe:"severe",
    sev_unknown:"unknown", st_active:"active", st_quiet:"quiet",
    noFires:"No fires · {range}",
    nothing:"Nothing detected in or within {km} km of Grad Zavidovići in this period.",
    peak:"peak", now:"now", latest:"latest", lastSeen:"Last seen", started:"Started",
    extent:"Extent", weather:"Weather", note:"Note", outside:"outside municipality",
    ofTown:"of town", ofZav:"of Zavidovići", ofN:"of {n}", across:"across",
    placeOf:"{km} km {dir} of {name}",
    spread:"{risk} spread risk", risk_elevated:"elevated", risk_high:"high",
    risk_extreme:"extreme", risk_moderate:"moderate", risk_unknown:"unknown",
    satellite:"Satellite", copyCoords:"Copy coords", copied:"copied",
    wind:"wind", from:"from", gusts:"gusts", rh:"RH",
    noDetRange:"no detections in range", boundary:"boundary", refreshNote:
      "data refreshes in place every 60 s · your view is kept",
    docs:"Documentation",
    r_24h:"Last 24h", r_3d:"Last 3 days", r_7d:"Last 7 days", r_30d:"Last month",
    r_1y:"Last year",
    rs_24h:"24h", rs_3d:"3 days", rs_7d:"7 days", rs_30d:"Month", rs_1y:"Year",
    justNow:"just now", agoMin:"{n} min ago", agoH:"{n} h ago", agoD:"{n} d ago",
    animate:"Animate", pause:"Pause", speed:"Playback speed",
    step:"Timeline step", u_min:"min", u_hour:"hour", u_day:"day", u_week:"week",
    zoomFires:"Zoom to fires", lMap:"Map", lSat:"Satellite", lTopo:"Terrain",
    m_dist:"Measure distance", m_area:"Measure area", m_clear:"Clear measurements",
    m_hintDist:"Click the map to add points. Double-click, or Done, to finish.",
    m_hintArea:"Click round the area you want. Double-click, or Done, to close it.",
    m_needDist:"one more point", m_needArea:"at least three points",
    m_done:"Done", m_undo:"Undo", m_close:"Close", m_remove:"Remove",
    m_perimeter:"perimeter",
    lBuffer:"{km} km buffer", legZone:"Watched area",
    zoneNote:"kept, flagged nearby"
  },
  bs: {
    sub:"Grad Zavidovići · ažurirano {t}", noActive:"Nema aktivnih požara",
    activeFires:["{n} aktivan požar","{n} aktivna požara","{n} aktivnih požara"],
    firesNoneActive:["{n} požar, nijedan aktivan","{n} požara, nijedan aktivan",
                     "{n} požara, nijedan aktivan"],
    detections:"detekcije", ok:"ok", fail:"greška",
    drawerFires:"Požari", showList:"Prikaži listu požara", closeList:"Zatvori listu",
    key:"Legenda", legDet:"Detekcije", legSev:"Jačina požara", legSize:"(veličina ∝ FRP)",
    legState:"Stanje", legBurning:"puna linija — trenutno gori",
    legQuiet:"crtkano — mirno / ugašeno",
    sev_low:"nizak", sev_moderate:"umjeren", sev_high:"visok", sev_severe:"ekstreman",
    sev_unknown:"nepoznato", st_active:"aktivan", st_quiet:"mirno",
    noFires:"Nema požara · {range}",
    nothing:"Ništa nije detektovano u općini Zavidovići niti u krugu od {km} km u ovom periodu.",
    peak:"maks.", now:"sada", latest:"zadnje", lastSeen:"Zadnje viđeno", started:"Počelo",
    extent:"Raspon", weather:"Vrijeme", note:"Napomena", outside:"izvan općine",
    ofTown:"od grada", ofZav:"od Zavidovića", ofN:"od {n}", across:"u širini",
    placeOf:"{km} km {dir} od {name}",
    spread:"rizik širenja: {risk}", risk_elevated:"povišen", risk_high:"visok",
    risk_extreme:"ekstreman", risk_moderate:"umjeren", risk_unknown:"nepoznat",
    satellite:"Satelit", copyCoords:"Kopiraj koordinate", copied:"kopirano",
    wind:"vjetar", from:"iz", gusts:"udari", rh:"vlaga",
    noDetRange:"nema detekcija u periodu", boundary:"granica", refreshNote:
      "podaci se osvježavaju svakih 60 s · prikaz se čuva",
    docs:"Dokumentacija",
    r_24h:"Zadnja 24h", r_3d:"Zadnja 3 dana", r_7d:"Zadnjih 7 dana", r_30d:"Zadnji mjesec",
    r_1y:"Zadnja godina",
    rs_24h:"24h", rs_3d:"3 dana", rs_7d:"7 dana", rs_30d:"Mjesec", rs_1y:"Godina",
    justNow:"upravo sad", agoMin:"prije {n} min", agoH:"prije {n} h", agoD:"prije {n} d",
    animate:"Animiraj", pause:"Pauza", speed:"Brzina reprodukcije",
    step:"Korak vremenske ose", u_min:"min", u_hour:"sat", u_day:"dan", u_week:"sedm.",
    zoomFires:"Približi na požare", lMap:"Karta", lSat:"Satelit", lTopo:"Teren",
    m_dist:"Izmjeri udaljenost", m_area:"Izmjeri površinu", m_clear:"Obriši mjerenja",
    m_hintDist:"Klikni po karti da dodaš točke. Dvoklik ili Gotovo za završetak.",
    m_hintArea:"Klikni oko površine koju mjeriš. Dvoklik ili Gotovo da se zatvori.",
    m_needDist:"još jedna točka", m_needArea:"najmanje tri točke",
    m_done:"Gotovo", m_undo:"Vrati", m_close:"Zatvori", m_remove:"Ukloni",
    m_perimeter:"obim",
    lBuffer:"pojas {km} km", legZone:"Praćeno područje",
    zoneNote:"prati se, označeno kao blizu"
  }
};
// Compass points are computed server-side in English; translate the letters.
const COMPASS_BS = {N:"S",NNE:"SSI",NE:"SI",ENE:"ISI",E:"I",ESE:"IJI",SE:"JI",SSE:"JJI",
  S:"J",SSW:"JJZ",SW:"JZ",WSW:"ZJZ",W:"Z",WNW:"ZSZ",NW:"SZ",NNW:"SSZ"};

// Bosnian by default: this map is for Grad Zavidovići, and the people who need it
// in an emergency read Bosnian. English is one click away in the header.
// A reader's own choice still wins - the toggle writes fw_lang and that is checked
// first - so switching to English is remembered on that device.
let LANG = "bs";
try {
  const saved = localStorage.getItem("fw_lang");
  if(saved && I18N[saved]) LANG = saved;
} catch(e) { /* file:// and private windows can block storage; stay with the default */ }

function plural(n){
  if(LANG !== "bs") return n === 1 ? 0 : 1;
  const a = Math.abs(n) % 100, b = a % 10;
  if(b === 1 && a !== 11) return 0;
  if(b >= 2 && b <= 4 && !(a >= 12 && a <= 14)) return 1;
  return 2;
}
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
function placeOf(e){
  const p = e.place_parts;
  if(!p || !p.name) return e.place;                 // older snapshot: use the server text
  if(p.km == null) return p.name;                   // sitting on the settlement itself
  return t("placeOf", {km:p.km, dir:dir(p.dir), name:genitive(p.name)});
}

// ---- range selection -------------------------------------------------------
// Cutoffs are computed server-side so every client agrees on the window edges
// regardless of the viewer's own clock or timezone.
let RANGE = DATA.default_range || "3d";
if(!DATA.range_cutoffs || !DATA.range_cutoffs[RANGE]) RANGE = "3d";
const cutoffOf = r => Date.parse(DATA.range_cutoffs[r]);

let EVENTS = [], dets = [], tMin = 0, tMax = 0;

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

// The "nearby" band - everything within nearby_buffer_km of the outline, which is
// exactly what the spatial clip keeps and flags `inside=0`. Pre-built into
// data/zavidovici-buffer.geojson rather than offset in the browser: offsetting a
// 731-point ring correctly is real work, and the answer only changes when the
// config does. Drawn before the boundary so the outline stays the stronger line.
// The artifact wins over DATA.buffer_km: the band on screen *is* the artifact, so
// if the snapshot was written before a buffer change the label must follow the
// geometry, not the stale config value that came with the data.
const bufKm = () => (BUFFER && BUFFER.features[0].properties.buffer_km)
  || DATA.buffer_km || 6;
const bandLayer = BUFFER ? L.geoJSON(BUFFER,{style:{color:"#7cc4ff",weight:1.1,
  opacity:.55,dashArray:"3,5",fillColor:"#7cc4ff",fillOpacity:.05}}).addTo(map) : null;
// A separate object each time: L.control.layers keeps a reference, so reusing one
// across rebuilds would carry the old language's key with it.
const overlays = () => bandLayer ? {[t("lBuffer",{km:bufKm()})]:bandLayer} : {};

let layersCtl = L.control.layers(
  {[t("lSat")]:sat,[t("lMap")]:osm,[t("lTopo")]:topo},overlays(),
  {position:"topright"}).addTo(map);

// Brighter and slightly heavier than it needed to be on the pale street map -
// a mid-blue hairline disappears against dark forest imagery.
const bLayer = L.geoJSON(BOUNDARY,{style:{color:"#7cc4ff",weight:2.4,opacity:.95,
  fillColor:"#7cc4ff",fillOpacity:.05,dashArray:"6,5"}}).addTo(map);
map.fitBounds(bLayer.getBounds(),{padding:[24,24]});

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
  const btn = d.querySelector(".legend-toggle");
  btn.addEventListener("click", () => {
    const open = d.classList.toggle("open");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  return d;
};
legend.addTo(map);

const detLayer = L.layerGroup().addTo(map);
const evLayer  = L.layerGroup().addTo(map);
const trail    = L.layerGroup().addTo(map);
let selected = null;

const MONTHS = {
  en:["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],
  bs:["jan","feb","mar","apr","maj","jun","jul","aug","sep","okt","nov","dec"]
};
const _sarajevo = new Intl.DateTimeFormat("en-GB",{timeZone:"Europe/Sarajevo",
  year:"numeric",day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit",
  hour12:false});
function tlParts(ts){
  const p = {};
  for(const part of _sarajevo.formatToParts(new Date(ts))) p[part.type] = part.value;
  return p;
}
const monName = mm => (MONTHS[LANG] || MONTHS.en)[parseInt(mm, 10) - 1];
// Every stamp used to be "14 Mar, 09:12", which was fine while nothing on the page
// was older than a month. With a year range it is genuinely ambiguous, so the year
// is spelled out wherever it is not the current one - present-day stamps, which are
// most of them, stay short.
const CUR_YEAR = tlParts(Date.now()).year;
// `sep` carries the full stop Bosnian puts after a year in a date, which would
// read as a typo on a bare ruler tick.
const yearBit = (p, sep = "") => p.year === CUR_YEAR ? "" : ` ${p.year}${sep}`;
function fmtLocal(ts){
  const p = tlParts(ts);
  const mon = monName(p.month);
  return LANG === "bs" ? `${p.day}. ${mon}${yearBit(p, ".")} ${p.hour}:${p.minute}`
                       : `${p.day} ${mon}${yearBit(p)}, ${p.hour}:${p.minute}`;
}
// The timeline readout always carries the year, whatever it is: it is the one label
// that answers "where am I", and it should not need a convention to read.
function fmtStamp(ts){
  const p = tlParts(ts);
  const mon = monName(p.month);
  return LANG === "bs" ? `${p.day}. ${mon} ${p.year}. ${p.hour}:${p.minute}`
                       : `${p.day} ${mon} ${p.year}, ${p.hour}:${p.minute}`;
}
const ago = m => m<1 ? t("justNow")
  : m<60 ? t("agoMin",{n:Math.round(m)})
  : m<1440 ? t("agoH",{n:(m/60).toFixed(1)})
  : t("agoD",{n:(m/1440).toFixed(1)});
const frpR = f => f==null?13:Math.max(13,Math.min(54,10+Math.sqrt(f)*5));
// Detection dots grow as you zoom in. Fixed-size dots either vanish at
// municipality zoom or merge into one mass when a fire has 45 of them in 2 km.
const detR = () => {
  const z = map.getZoom();
  return Math.max(6.5, Math.min(11, 5 + (z - 8) * 0.95));
};

const R_EARTH_M = 6371008.8;
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

// Markers are rebuilt on every refresh, which closes any open popup. Track them
// by event id so an open popup can be restored afterwards.
const markerById = {};
let popupOpenId = null;

function drawEvents(){
  evLayer.clearLayers();
  for(const k in markerById) delete markerById[k];
  EVENTS.forEach(e=>{
    const col = SEVC[e.severity]||SEVC.unknown;
    const quiet = e.status!=="active";

    // Footprint: L.circle takes a radius in METRES, so it scales with zoom and
    // keeps enclosing the detections it summarises. The intensity marker below is
    // L.circleMarker, whose radius is in screen pixels and deliberately fixed -
    // mixing the two in one symbol is what made the old marker look broken past
    // zoom ~11, where geographic spread outgrows any fixed pixel radius.
    // Floor of 400 m keeps a single-detection event visible at all.
    L.circle([e.lat,e.lon],{
      radius:footprintM(e),
      color:col, weight:1.4, opacity:quiet?.35:.6,
      fillColor:col, fillOpacity:quiet?.04:.08,
      dashArray:"4,5", interactive:false}).addTo(evLayer);

    // Soft dark halo so a warm ring keeps an edge on light terrain.
    L.circleMarker([e.lat,e.lon],{
      radius:frpR(e.max_frp)+1, color:"#0b0f14", weight:4, opacity:.34,
      fill:false, interactive:false}).addTo(evLayer);

    const c = L.circleMarker([e.lat,e.lon],{
      radius:frpR(e.max_frp), color:col, weight:quiet?2.5:3.5,
      // Stroke keeps full hue even when quiet: a faded fill over green terrain
      // desaturates toward olive, and a burnt-out severe fire should still read
      // as severe. Dash + thin fill carry "not burning", not the colour.
      opacity:1,
      fillColor:col, fillOpacity:quiet?.10:.45,
      dashArray:quiet?"6,4":null});
    c.bindPopup(popupHtml(e),{maxWidth:290});
    c.on("click",()=>select(e.id,false));
    c.on("popupopen",()=>{popupOpenId = e.id});
    c.on("popupclose",()=>{ if(popupOpenId===e.id) popupOpenId = null; });
    c.addTo(evLayer);
    markerById[e.id] = c;
    if(!quiet){
      L.circleMarker([e.lat,e.lon],{radius:e.max_frp?frpR(e.max_frp)+7:13,color:col,
        weight:1,opacity:.45,fill:false,className:"pulse"}).addTo(evLayer);
    }
  });
}

function popupHtml(e){
  const w = e.weather;
  return `<b>${t("sev_"+e.severity).toUpperCase()}</b> &middot; ${t("st_"+e.status)}<br>
    ${placeOf(e)}<br>
    <span style="color:#8b98a5">${e.dist_town_km} km ${dir(e.dir_town)} ${t("ofZav")}</span><br>
    FRP <b>${e.max_frp==null?"n/a":e.max_frp.toFixed(1)+" MW"}</b> ${t("peak")},
    ${e.latest_frp==null?"n/a":e.latest_frp.toFixed(1)+" MW"} ${t("latest")}<br>
    ${e.n_det} ${t("detections")} &middot; ${e.sources.join(", ")}<br>
    ${t("lastSeen").toLowerCase()} ${ago(e.age_min)} &middot; ${e.extent_km} km ${t("across")}
    ${w?`<br>${t("wind")} ${Math.round(w.speed)} km/h ${t("from")} ${dir(w.from)} (${t("gusts")} ${Math.round(w.gusts)}), ${t("rh")} ${w.humidity}%`:""}
    <br><br><a href="https://www.google.com/maps?q=${e.lat},${e.lon}" target="_blank">Google Maps</a>
     &middot; <a href="https://www.openstreetmap.org/?mlat=${e.lat}&mlon=${e.lon}#map=14/${e.lat}/${e.lon}" target="_blank">OSM</a>`;
}

function drawDets(upto){
  detLayer.clearLayers(); trail.clearLayers();
  const shown = dets.filter(d=>d.t<=upto);
  shown.forEach(d=>{
    const age = (upto-d.t)/3600000;
    // Age still fades a detection, but the old floor of .16 made anything over a
    // day old effectively invisible - with an outline it just read as an empty
    // ring. Keep the recency gradient, raise the floor so nothing disappears.
    const op = Math.max(.55,1-age/40);
    // Each detection gets a white border with a dark outer edge. The stroke used
    // to be the same colour as the fill, so a dot had no outline at all and the
    // cyan/amber/magenta washed into green forest and tan farmland. Two thin
    // rings keep all three source colours readable on street and satellite.
    const dc = SRC[d.source]?.c || "#fff";
    const dr = detR();
    L.circleMarker([d.lat,d.lon],{radius:dr,color:"#0b0f14",
      weight:3.4,opacity:op*.8,fill:false,interactive:false}).addTo(detLayer);
    L.circleMarker([d.lat,d.lon],{radius:dr,color:"#ffffff",
      weight:2,opacity:op*.95,fillOpacity:op*.95,fillColor:dc})
      .bindPopup(`${SRC[d.source]?.n||d.source}<br>${fmtLocal(d.t)}<br>FRP ${d.frp==null?"n/a":d.frp+" MW"}`)
      .addTo(detLayer);
  });
  if(selected){
    const pts = shown.filter(d=>d.ev===selected).map(d=>[d.lat,d.lon]);
    if(pts.length>1) L.polyline(pts,{color:"#ff6b35",weight:1.4,opacity:.5,dashArray:"3,4"}).addTo(trail);
  }
  // The clock alone says where you are on the timeline; a step index on top of it
  // was noise. It still shows with no detections, which is the point of spanning
  // the whole range.
  document.getElementById("tlabel").textContent =
    fmtStamp(upto) + (dets.length ? ` · ${shown.length}/${dets.length}` : "");
}

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
      <h2><span>${e.status==="active"?"🔥":"💤"} ${placeOf(e)}</span>
          <span class="sev" style="color:${col}">${t("sev_"+e.severity)}</span></h2>
      <div class="meta">
        <span>FRP</span><span>${e.max_frp==null?"n/a":e.max_frp.toFixed(1)+" MW "+t("peak")+" / "+
          (e.latest_frp==null?"n/a":e.latest_frp.toFixed(1)+" MW "+t("now"))}</span>
        <span>${t("lastSeen")}</span><span>${ago(e.age_min)} · ${fmtLocal(Date.parse(e.last_ts))}</span>
        <span>${t("started")}</span><span>${fmtLocal(Date.parse(e.first_ts))}</span>
        <span>${t("legDet")}</span><span>${e.series.length}${e.series.length!==e.n_det?" "+t("ofN",{n:e.n_det}):""} · ${e.sources.map(s=>
          `<span style="color:${SRC[s]?.c||"#fff"}">${s}</span>`).join(" ")}</span>
        <span>${t("extent")}</span><span>${e.extent_km} km · ${e.dist_town_km} km ${dir(e.dir_town)} ${t("ofTown")}</span>
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

function select(id,fly){
  selected = selected===id?null:id;
  document.querySelectorAll(".ev").forEach(d=>d.classList.toggle("sel",d.dataset.id===selected));
  const e = EVENTS.find(x=>x.id===selected);
  if(e&&fly) map.flyTo([e.lat,e.lon],13,{duration:.6});
  drawDets(sliderTime());
}

function renderRange(){
  const el = document.getElementById("hrange");
  el.innerHTML = Object.keys(DATA.ranges||{}).map(k=>{
    const c = (DATA.range_counts||{})[k]||{};
    const label = t("r_"+k), short = t("rs_"+k);
    return `<button data-r="${k}" class="${k===RANGE?"on":""}" title="${label}">${short}${
      c.events!=null?` <b>(${c.events})</b>`:""}</button>`;
  }).join("");
  el.querySelectorAll("button").forEach(b=>b.onclick=()=>{
    RANGE = b.dataset.r;
    recompute(); renderRange(); renderHeader(); drawEvents(); renderList();
    setSliderTime(tMax); drawDets(sliderTime());
// clientWidth is only trustworthy after the first layout pass, and the track's
// end padding is half of it.
requestAnimationFrame(() => { rebuildSlider(); drawDets(sliderTime()); });
  });
}

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
  // The documentation is published beside the map, at <site>/docs/, by the same
  // Pages deploy that publishes this page - so it is only linked when this instance
  // has a public address. The local file:// map has no sibling docs directory and
  // would only offer a dead link.
  const docsLink = DATA.public_url
    ? ` &nbsp;|&nbsp; <a href="docs/" class="foot-link">${t("docs")}</a>` : "";
  document.getElementById("foot").innerHTML =
    `Meteosat MTG · VIIRS/MODIS FIRMS · Sentinel-3 &nbsp;|&nbsp; ${t("boundary")}: OSM rel. 2528292${docsLink}
     <br>${t("refreshNote")}`;
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
// Every unit stays selectable on every range. An earlier version bumped the unit
// whenever it produced "too many" steps, which quietly removed minutes from all
// but the 24h range - the default range is 3 days, so cycling never reached them.
// A month in minutes is 43200 steps: coarse to drag, but exact, and the reader
// asked for it. Only a genuinely pathological count is corrected.
const MAX_STEPS = 200000;
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

// Names kept from the slider version so every call site stays valid.
function sliderTime(){
  return Math.min(tMax, Math.max(tMin, msOf(scrollEl.scrollLeft)));
}
// Scroll events arrive asynchronously, so a flag set-then-cleared around the
// assignment is already false by the time the event fires - which made the
// handler treat playback's own scrolling as a reader interruption and stop it
// after the first tick. Remember the position we set instead and compare.
let progAt = -1;
function setScroll(x){
  progAt = x;
  scrollEl.scrollLeft = x;
}
const isOurScroll = () => Math.abs(scrollEl.scrollLeft - progAt) < 2;

function setSliderTime(ms){
  const x = Math.min(trackPx(), Math.max(0, xOf(Math.min(Math.max(ms, tMin), tMax))));
  setScroll(x);
  paintTicks();
  syncAria();
}
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
function tickLabel(ms){
  const u = UNITS[unitIx].key;
  const p = tlParts(ms);
  if(u === "min" || u === "hour")
    return (p.hour === "00" && p.minute === "00")
      ? `${p.day} ${monName(p.month)}${yearBit(p)}` : `${p.hour}:${p.minute}`;
  return `${p.day} ${monName(p.month)}${yearBit(p)}`;
}

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

map.on("zoomend", () => drawDets(sliderTime()));
addEventListener("resize", () => rebuildSlider());
let timer=null;
// Playback speed is a rate over timeline *steps*, not a fixed duration per pass.
// A fixed duration made every unit take the same five seconds, so switching to
// minutes bought detail and lost nothing else - the animation just skipped
// faster. Rate-based, the unit button also chooses pace: minutes crawl, days
// sweep. The tick interval stays fixed so motion stays smooth regardless.
const SPEEDS = [{label:"0.5\u00d7", mult:0.5},
                {label:"1\u00d7",   mult:1},
                {label:"2\u00d7",   mult:2},
                {label:"4\u00d7",   mult:4}];
const STEPS_PER_SEC = 4;                           // at 1x
// Both ends need a guard: a 3-day range in day steps is 3 steps and would be a
// blink, and 30 days in minute steps is 43200 and would run for three hours.
// The ceiling is deliberately generous - minute steps are the deliberate choice
// to watch a fire develop, and a tight cap turned every range into the same
// two-and-a-half-minute skim. 4x is the escape hatch, not a lower ceiling.
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

function stopPlay(){
  if(timer){ clearInterval(timer); timer = null; }
  playBtn.innerHTML = "&#9654;";
  playBtn.title = t("animate");
}

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
function pathM(pts, closed){
  let m = 0;
  for(let i=1; i<pts.length; i++) m += segM(pts[i-1], pts[i]);
  if(closed && pts.length > 2) m += segM(pts[pts.length-1], pts[0]);
  return m;
}
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

function shapeStats(mode, pts){
  const closed = mode === "area";
  return {len: pathM(pts, closed), area: closed ? geoAreaM2(pts) : 0};
}
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
function clearDraft(){
  if(!draft) return;
  [draft.line, draft.rubber, draft.live].concat(draft.verts)
    .forEach(l => { if(l) mLayer.removeLayer(l); });
  draft = null;
}
function drawDraft(){
  if(!draft) return;
  draft.line.setLatLngs(draft.pts);
  draft.verts.forEach(v => mLayer.removeLayer(v));
  draft.verts = draft.pts.map(p => mVert(p, 3.6));
  draft.verts.forEach(v => mLayer.addLayer(v));
  if(!draft.pts.length) draft.rubber.setLatLngs([]);
  mPanelUpdate();
}

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
function undoPoint(){
  if(!draft || !draft.pts.length) return;
  draft.pts.pop();
  drawDraft();
}
function finishDraft(){
  if(!draft || draft.pts.length < needPts()) return;
  const sh = {mode:mMode, pts:draft.pts.slice(), layers:[]};
  clearDraft();
  mShapes.push(sh);
  renderShape(sh);
  newDraft();                 // the tool stays armed for the next measurement
  mPanelUpdate(); mRefresh();
}

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
function mLabels(){
  const d = mCtl._d;
  if(!d) return;
  d.querySelector(".m-dist").title = t("m_dist");
  d.querySelector(".m-area").title = t("m_area");
  d.querySelector(".m-clear").title = t("m_clear");
}
function mRefresh(){
  const d = mCtl._d;
  if(!d) return;
  d.querySelector(".m-dist").classList.toggle("on", mMode === "dist");
  d.querySelector(".m-area").classList.toggle("on", mMode === "area");
  d.querySelector(".m-clear").style.display = mShapes.length ? "" : "none";
}

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
function exitMeasure(){
  clearDraft();
  mMode = null;
  L.DomUtil.removeClass(map.getContainer(), "measuring");
  map.doubleClickZoom.enable();
  mPanelCtl.remove();
  mRefresh();
}
mCtl.addTo(map);

map.on("click", e => { if(mMode) addPoint(e.latlng); });
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

function rebuildControls(){
  legend.remove(); legend.addTo(map);
  layersCtl.remove();
  layersCtl = L.control.layers(
    {[t("lSat")]:sat,[t("lMap")]:osm,[t("lTopo")]:topo},overlays(),
    {position:"topright"}).addTo(map);
  zoomBtn.remove(); zoomBtn.addTo(map);
  mCtl.remove(); mCtl.addTo(map);
  if(mMode){ mPanelCtl.remove(); mPanelCtl.addTo(map); mPanelUpdate(); }
  relabelShapes();
}

function renderAll(){
  applyStaticLabels();
  recompute(); renderRange(); renderHeader(); drawEvents(); renderList();
  drawDets(sliderTime());
}

function setLang(l){
  if(!I18N[l] || l === LANG) return;
  LANG = l;
  try { localStorage.setItem("fw_lang", l); } catch(e) { /* storage may be blocked */ }
  rebuildControls();
  renderAll();
}
document.querySelectorAll("#langsw button").forEach(b =>
  b.addEventListener("click", () => setLang(b.dataset.l)));

applyStaticLabels();
recompute(); renderRange(); renderHeader(); drawEvents(); renderList();
setSliderTime(tMax); drawDets(sliderTime());
// ---- live refresh without losing the reader's place ------------------------
// A file:// page cannot fetch() a sibling JSON file, but it can load one as a
// script. The poller rewrites fire-map-data.js each cycle; we pull it in with a
// cache-busted <script> tag and re-render from it. Map centre, zoom, selected
// range, selected fire and slider position are all left untouched - which is the
// whole point, since location.reload() discarded every one of them.
const REFRESH_MS = window.__fwRefreshMs || 60000;
let refreshFails = 0;

function applyData(d){
  if(!d || !d.generated_at) return false;
  if(d.generated_at === DATA.generated_at) return false;   // nothing new
  const at = sliderTime();                                 // remember the moment shown
  DATA = d;
  if(!DATA.range_cutoffs || !DATA.range_cutoffs[RANGE]) RANGE = DATA.default_range || "3d";
  const wasOpen = popupOpenId;
  recompute();
  renderRange(); renderHeader(); drawEvents(); renderList();
  setSliderTime(at);
  drawDets(sliderTime());
  if(wasOpen && markerById[wasOpen]){
    // openPopup() auto-pans to fit the popup, which would nudge the view the
    // reader chose. Suppress it for this programmatic restore only - a popup the
    // reader opens by clicking still pans normally.
    const mk = markerById[wasOpen], pu = mk.getPopup();
    const prevAutoPan = pu.options.autoPan;
    pu.options.autoPan = false;
    mk.openPopup();
    pu.options.autoPan = prevAutoPan;
  }
  const sub = document.getElementById("hsub");
  sub.classList.add("flash");
  setTimeout(()=>sub.classList.remove("flash"), 900);
  return true;
}

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
    // If the sibling script cannot be loaded at all, fall back to the old
    // behaviour rather than silently going stale.
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
    for f in (src, data_path_for(src)):
        if f.exists():
            shutil.copy2(f, PUBLIC_DIR / f.name)
    # Also publish the page as index.html so the bare URL opens the map instead
    # of a directory listing - that is the link people actually share.
    if src.exists():
        shutil.copy2(src, PUBLIC_DIR / "index.html")
    return True


def render(snapshot: dict, path: Path | None = None) -> Path:
    out = Path(path or MAP_PATH)
    boundary = json.loads(BOUNDARY_GEOJSON.read_text())
    # None when the artifact is missing or was built for a different buffer
    # distance; the page then simply omits the band rather than drawing a
    # confident line in the wrong place. `python3 -m firewatch buffer` rebuilds it.
    band = geo.load_buffer()
    html = (TEMPLATE
            .replace("__DATA__", json.dumps(snapshot, ensure_ascii=False))
            .replace("__BOUNDARY__", json.dumps(boundary, separators=(",", ":")))
            .replace("__BUFFER__", json.dumps(band, separators=(",", ":")))
            .replace("__DATA_JS__", data_path_for(out).name))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    write_data(snapshot, out)
    sync_public(out)
    return out
