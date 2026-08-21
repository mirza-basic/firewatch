"""Render the live fire map as a single self-contained HTML file.

The snapshot data is embedded directly in the page rather than fetched, because a
file:// page cannot XHR a sibling JSON file under Chrome/Safari CORS rules. The
poller rewrites this file every cycle and the page reloads itself, which gives a
live view with no local web server involved.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import BOUNDARY_GEOJSON, MAP_PATH

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
  #wrap{display:flex;height:100vh;overflow:hidden}
  #side{width:370px;flex:0 0 370px;background:var(--panel);border-right:1px solid var(--line);
    display:flex;flex-direction:column}
  #map{flex:1;background:#0b0e12}
  header{padding:16px 18px 14px;border-bottom:1px solid var(--line)}
  h1{margin:0;font-size:15px;letter-spacing:.02em;display:flex;align-items:center;gap:8px}
  h1 .dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
  .sub{color:var(--dim);font-size:12px;margin-top:5px;font-variant-numeric:tabular-nums}
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
  .empty{text-align:center;color:var(--dim);padding:40px 20px}
  .empty .big{font-size:34px;margin-bottom:10px}
  #timebar{position:absolute;left:50%;transform:translateX(-50%);bottom:18px;z-index:500;
    background:rgba(21,26,33,.94);border:1px solid var(--line);border-radius:11px;
    padding:10px 14px;display:flex;align-items:center;gap:11px;width:min(620px,72vw);
    backdrop-filter:blur(9px)}
  #timebar input[type=range]{flex:1;accent-color:var(--accent)}
  #tlabel{font-size:11.5px;color:var(--dim);min-width:132px;font-variant-numeric:tabular-nums}
  #play{background:#26303b;border:1px solid var(--line);color:var(--fg);border-radius:6px;
    width:30px;height:26px;cursor:pointer;font-size:12px}
  .legend{background:rgba(21,26,33,.94);padding:9px 11px;border-radius:9px;
    border:1px solid var(--line);color:var(--dim);font-size:11.5px;line-height:1.7}
  .legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
  .leaflet-popup-content-wrapper{background:var(--panel);color:var(--fg);border-radius:9px}
  .leaflet-popup-tip{background:var(--panel)}
  .leaflet-popup-content{margin:11px 13px;font-size:12.5px}
  .leaflet-popup-content b{color:var(--accent)}
  .leaflet-popup-content a{color:#7cc4ff}
  .leaflet-bar a{background:var(--panel2);color:var(--fg);border-color:var(--line)}
  .leaflet-bar a:hover{background:#31404f}
  .pulse{animation:pulse 2.1s ease-out infinite}
  @keyframes pulse{0%{r:8;opacity:.85}70%{opacity:0}100%{r:26;opacity:0}}
  @media (max-width:880px){#wrap{flex-direction:column}#side{width:100%;flex:0 0 46%}}
</style></head><body>
<div id="wrap">
  <div id="side">
    <header>
      <h1><span class="dot" id="hdot"></span><span id="htitle">FireWatch Zavidovići</span></h1>
      <div class="sub" id="hsub"></div>
      <div class="seg" id="hrange"></div>
      <div class="status" id="hchips"></div>
    </header>
    <div id="list"></div>
    <footer id="foot"></footer>
  </div>
  <div id="map"></div>
</div>
<div id="timebar">
  <button id="play" title="Animate">&#9654;</button>
  <input type="range" id="slider" min="0" max="100" value="100">
  <span id="tlabel"></span>
</div>
<script>
const DATA = __DATA__;
const BOUNDARY = __BOUNDARY__;
const SRC = {mtg:{c:"#4cc9f0",n:"Meteosat MTG (10 min)"},
             firms:{c:"#ffd166",n:"VIIRS/MODIS (NRT)"},
             s3:{c:"#b5179e",n:"Sentinel-3 SLSTR"}};
const SEVC = {low:"#ffd166",moderate:"#ff9f1c",high:"#ff6b35",severe:"#e63946",unknown:"#8b98a5"};

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
  tMin = dets.length ? dets[0].t : c;
  tMax = dets.length ? dets[dets.length-1].t : Date.now();
  if(selected && !EVENTS.some(e=>e.id===selected)) selected = null;
}

const map = L.map("map",{zoomControl:true,attributionControl:true}).setView([44.386,18.276],10);
const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  {maxZoom:19,attribution:"&copy; OpenStreetMap"}).addTo(map);
const sat = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  {maxZoom:19,attribution:"Esri, Maxar, Earthstar Geographics"});
const topo = L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
  {maxZoom:17,attribution:"&copy; OpenTopoMap (CC-BY-SA)"});
L.control.layers({"Map":osm,"Satellite":sat,"Terrain":topo},{},{position:"topright"}).addTo(map);

const bLayer = L.geoJSON(BOUNDARY,{style:{color:"#5b8def",weight:2,opacity:.9,
  fillColor:"#5b8def",fillOpacity:.055,dashArray:"5,4"}}).addTo(map);
map.fitBounds(bLayer.getBounds(),{padding:[24,24]});

// Jump straight to what is burning - at municipality zoom a single fire is a
// few pixels, which is exactly when you most want to see it.
const zoomBtn = L.control({position:"topleft"});
zoomBtn.onAdd = () => {
  const d = L.DomUtil.create("div","leaflet-bar");
  const a = L.DomUtil.create("a","",d);
  a.href="#"; a.title="Zoom to fires"; a.innerHTML="&#128293;";
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
  d.innerHTML = "<b style='color:#e6edf3'>Detections</b><br>" +
    Object.entries(SRC).map(([k,v])=>`<i style="background:${v.c}"></i>${v.n}`).join("<br>") +
    "<br><b style='color:#e6edf3'>Circle</b> = fire, size &prop; FRP";
  return d;
};
legend.addTo(map);

const detLayer = L.layerGroup().addTo(map);
const evLayer  = L.layerGroup().addTo(map);
const trail    = L.layerGroup().addTo(map);
let selected = null;

const fmtLocal = t => new Date(t).toLocaleString("en-GB",
  {day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",timeZone:"Europe/Sarajevo"});
const ago = m => m<60?`${Math.round(m)} min ago`:(m<1440?`${(m/60).toFixed(1)} h ago`:`${(m/1440).toFixed(1)} d ago`);
const frpR = f => f==null?7:Math.max(7,Math.min(42,6+Math.sqrt(f)*3.4));

function drawEvents(){
  evLayer.clearLayers();
  EVENTS.forEach(e=>{
    const col = SEVC[e.severity]||SEVC.unknown;
    const quiet = e.status!=="active";
    const c = L.circleMarker([e.lat,e.lon],{
      radius:frpR(e.max_frp), color:quiet?"#6b7785":col, weight:2.5,
      fillColor:quiet?"#6b7785":col, fillOpacity:quiet?.14:.34,
      dashArray:quiet?"3,3":null});
    c.bindPopup(popupHtml(e),{maxWidth:290});
    c.on("click",()=>select(e.id,false));
    c.addTo(evLayer);
    if(!quiet){
      L.circleMarker([e.lat,e.lon],{radius:e.max_frp?frpR(e.max_frp)+7:13,color:col,
        weight:1,opacity:.45,fill:false,className:"pulse"}).addTo(evLayer);
    }
  });
}

function popupHtml(e){
  const w = e.weather;
  return `<b>${e.severity.toUpperCase()}</b> &middot; ${e.status}<br>
    ${e.place}<br>
    <span style="color:#8b98a5">${e.dist_town_km} km ${e.dir_town} of Zavidovići</span><br>
    FRP <b>${e.max_frp==null?"n/a":e.max_frp.toFixed(1)+" MW"}</b> peak,
    ${e.latest_frp==null?"n/a":e.latest_frp.toFixed(1)+" MW"} latest<br>
    ${e.n_det} detections &middot; ${e.sources.join(", ")}<br>
    last seen ${ago(e.age_min)} &middot; ${e.extent_km} km across
    ${w?`<br>wind ${Math.round(w.speed)} km/h from ${w.from} (gusts ${Math.round(w.gusts)}), RH ${w.humidity}%`:""}
    <br><br><a href="https://www.google.com/maps?q=${e.lat},${e.lon}" target="_blank">Google Maps</a>
     &middot; <a href="https://www.openstreetmap.org/?mlat=${e.lat}&mlon=${e.lon}#map=14/${e.lat}/${e.lon}" target="_blank">OSM</a>`;
}

function drawDets(upto){
  detLayer.clearLayers(); trail.clearLayers();
  const shown = dets.filter(d=>d.t<=upto);
  shown.forEach(d=>{
    const age = (upto-d.t)/3600000;
    const op = Math.max(.16,1-age/26);
    L.circleMarker([d.lat,d.lon],{radius:3.6,color:SRC[d.source]?.c||"#fff",
      weight:1,opacity:op,fillOpacity:op*.85,fillColor:SRC[d.source]?.c||"#fff"})
      .bindPopup(`${SRC[d.source]?.n||d.source}<br>${fmtLocal(d.t)}<br>FRP ${d.frp==null?"n/a":d.frp+" MW"}`)
      .addTo(detLayer);
  });
  if(selected){
    const pts = shown.filter(d=>d.ev===selected).map(d=>[d.lat,d.lon]);
    if(pts.length>1) L.polyline(pts,{color:"#ff6b35",weight:1.4,opacity:.5,dashArray:"3,4"}).addTo(trail);
  }
  document.getElementById("tlabel").textContent = dets.length
    ? `${fmtLocal(upto)} · ${shown.length}/${dets.length}`
    : "no detections in range";
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
    const label = (DATA.ranges&&DATA.ranges[RANGE]||RANGE).toLowerCase();
    el.innerHTML = `<div class="empty"><div class="big">🌲</div>
      <b>No fires ${label}</b><br><span style="font-size:12px">
      Nothing detected in or within ${DATA.buffer_km} km of Grad Zavidovići
      in this period.</span></div>`;
    return;
  }
  el.innerHTML = EVENTS.map(e=>{
    const col = e.status==="active"?(SEVC[e.severity]||"#888"):"#6b7785";
    const w = e.weather;
    return `<div class="ev" id="ev-${e.id}" data-id="${e.id}" style="border-left-color:${col}">
      <h2><span>${e.status==="active"?"🔥":"💤"} ${e.place}</span>
          <span class="sev" style="color:${col}">${e.severity}</span></h2>
      <div class="meta">
        <span>FRP</span><span>${e.max_frp==null?"n/a":e.max_frp.toFixed(1)+" MW peak / "+
          (e.latest_frp==null?"n/a":e.latest_frp.toFixed(1)+" MW now")}</span>
        <span>Last seen</span><span>${ago(e.age_min)} · ${fmtLocal(Date.parse(e.last_ts))}</span>
        <span>Started</span><span>${fmtLocal(Date.parse(e.first_ts))}</span>
        <span>Detections</span><span>${e.series.length}${e.series.length!==e.n_det?` of ${e.n_det}`:""} · ${e.sources.map(s=>
          `<span style="color:${SRC[s]?.c||"#fff"}">${s}</span>`).join(" ")}</span>
        <span>Extent</span><span>${e.extent_km} km · ${e.dist_town_km} km ${e.dir_town} of town</span>
        ${w?`<span>Weather</span><span>${w.temp}°C, RH ${w.humidity}%, wind ${Math.round(w.speed)} km/h from ${w.from}
             ${e.risk?`· <b style="color:${e.risk==="extreme"||e.risk==="high"?"#e63946":"#8b98a5"}">${e.risk} spread risk</b>`:""}</span>`:""}
        ${e.inside?"":'<span>Note</span><span style="color:#ffd166">outside municipality</span>'}
      </div>
      ${sparkline(e)}
      <div class="acts">
        <a href="https://www.google.com/maps?q=${e.lat},${e.lon}" target="_blank">Google Maps</a>
        <a href="https://www.google.com/maps/@?api=1&map_action=map&center=${e.lat},${e.lon}&zoom=15&basemap=satellite" target="_blank">Satellite</a>
        <button onclick="navigator.clipboard.writeText('${e.lat}, ${e.lon}');this.textContent='copied'">Copy coords</button>
      </div></div>`;
  }).join("");
  el.querySelectorAll(".ev").forEach(d=>d.onclick=ev=>{
    if(ev.target.tagName==="A"||ev.target.tagName==="BUTTON") return;
    select(d.dataset.id,true);
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
  el.innerHTML = Object.entries(DATA.ranges||{}).map(([k,label])=>{
    const c = (DATA.range_counts||{})[k]||{};
    const short = (DATA.ranges_short||{})[k] || label;
    return `<button data-r="${k}" class="${k===RANGE?"on":""}" title="${label}">${short}${
      c.events!=null?` <b>${c.events}</b>`:""}</button>`;
  }).join("");
  el.querySelectorAll("button").forEach(b=>b.onclick=()=>{
    RANGE = b.dataset.r;
    recompute(); renderRange(); renderHeader(); drawEvents(); renderList();
    slider.value = 100; drawDets(tMax);
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
    ? `${act} active fire${act>1?"s":""}`
    : (EVENTS.length ? `${EVENTS.length} fire${EVENTS.length>1?"s":""}, none active` : "No active fires");
  document.getElementById("hsub").textContent =
    `Grad Zavidovići · updated ${fmtLocal(Date.parse(DATA.generated_at))}`;
  document.getElementById("hchips").innerHTML = [
    `<span class="chip">detections <b>${dets.length}</b></span>`,
    ...Object.entries(DATA.source_status||{}).map(([k,v])=>
      `<span class="chip" title="${(v.detail||"").replace(/"/g,"")}">${k} <b style="color:${v.ok?"#3fb950":"#e63946"}">${v.ok?"ok":"fail"}</b></span>`)
  ].join("");
  document.getElementById("foot").innerHTML =
    `Meteosat MTG · VIIRS/MODIS FIRMS · Sentinel-3 &nbsp;|&nbsp; boundary: OSM rel. 2528292
     <br>page reloads every 60 s`;
}

const slider = document.getElementById("slider");
const sliderTime = () => +slider.value===100 ? tMax : tMin+(tMax-tMin)*(+slider.value/100);
slider.oninput = () => drawDets(sliderTime());
let timer=null;
document.getElementById("play").onclick = function(){
  if(timer){clearInterval(timer);timer=null;this.innerHTML="&#9654;";return}
  if(!dets.length) return;
  this.innerHTML="&#10074;&#10074;"; slider.value=0;
  timer=setInterval(()=>{
    slider.value = Math.min(100,+slider.value+1.4);
    drawDets(sliderTime());
    if(+slider.value>=100){clearInterval(timer);timer=null;
      document.getElementById("play").innerHTML="&#9654;"}
  },70);
};

recompute(); renderRange(); renderHeader(); drawEvents(); renderList(); drawDets(tMax);
setTimeout(()=>location.reload(), 60000);
</script></body></html>
"""


def render(snapshot: dict, path: Path | None = None) -> Path:
    out = Path(path or MAP_PATH)
    boundary = json.loads(BOUNDARY_GEOJSON.read_text())
    html = (TEMPLATE
            .replace("__DATA__", json.dumps(snapshot, ensure_ascii=False))
            .replace("__BOUNDARY__", json.dumps(boundary, separators=(",", ":"))))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out
