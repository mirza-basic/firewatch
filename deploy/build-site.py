#!/usr/bin/env python3
"""Assemble the published site: the live map at /, the documentation at /docs/.

The pages in docs/ are written in artifact-body form - they open with <title> and
carry no <!doctype>, <html>, <head> or <body>, because the Artifact host wraps them.
A browser will render them anyway, but it will not invent the one tag that matters
here: without <meta name="viewport"> every one of them renders at desktop width on a
phone, which is where a fire map is actually read. So each page is wrapped properly
on the way out rather than published as-is.

Run from the repository root:

    python3 deploy/build-site.py [public_dir]
"""
from __future__ import annotations

import html
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
MUNICIPALITIES = ROOT / "data" / "bih" / "municipalities.json"

# Held back from the public site. Drop a name from this set to publish it.
#
# The Sentinel-3 plan describes work that has not been built and is not scheduled.
# On the public site it would read as a roadmap; in the repository it is what it
# is, a measured case for a change that was deliberately deferred.
SKIP = {"firewatch-sentinel3-plan.html"}

SKELETON = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{head}
</head>
<body>
{body}
</body>
</html>
"""


def split(src: str) -> tuple[str, str]:
    """(head, body). Everything up to the last </style> belongs in the head."""
    i = src.rfind("</style>")
    if i == -1:                      # no stylesheet: treat the <title> line as head
        j = src.find("\n")
        return src[:j], src[j:]
    i += len("</style>")
    return src[:i], src[i:]


def title_of(src: str) -> str:
    m = re.search(r"<title>(.*?)</title>", src, re.S)
    return html.unescape(m.group(1)).strip() if m else "FireWatch"


def blurb_of(src: str) -> str:
    """The standfirst, which every page already has as its one-line summary."""
    m = re.search(r'<p class="(?:standfirst|stand)">(.*?)</p>', src, re.S)
    if not m:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).split())


def build(public: pathlib.Path) -> list[tuple[str, str, str]]:
    out = public / "docs"
    out.mkdir(parents=True, exist_ok=True)
    pages = []
    for f in sorted(DOCS.glob("*.html")):
        if f.name in SKIP:
            continue
        src = f.read_text()
        head, body = split(src)
        (out / f.name).write_text(SKELETON.format(head=head.strip(), body=body.strip()))
        pages.append((f.name, title_of(src), blurb_of(src)))
    return pages


def index(public: pathlib.Path, pages) -> None:
    """A plain contents page, styled from one of the docs so it matches them."""
    style = split((DOCS / "firewatch-field-manual.html").read_text())[0]
    style = re.sub(r"<title>.*?</title>", "<title>FireWatch Documentation</title>",
                   style, flags=re.S)
    rows = "\n".join(
        f'      <li><a href="{html.escape(n)}"><strong>{html.escape(t)}</strong>'
        f'<span>{html.escape(b)}</span></a></li>'
        for n, t, b in pages)
    body = f"""<div class="wrap" style="grid-template-columns:minmax(0,1fr)">
  <main style="padding:44px 22px 80px;max-width:760px;margin:0 auto">
    <header class="mast">
      <div class="eyebrow">Documentation</div>
      <h1>FireWatch</h1>
      <p class="standfirst">Near-live wildfire monitoring for Grad Zavidovi&#263;i, from
      Meteosat, NASA FIRMS and Sentinel-3. <a href="../">The live map is here.</a></p>
    </header>
    <ul style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:2px">
{rows}
    </ul>
    <footer>
      <p>Boundary and settlement data &copy; OpenStreetMap contributors, ODbL 1.0.
      Fire detections courtesy of NASA FIRMS and EUMETSAT.</p>
    </footer>
  </main>
</div>
<style>
  ul a{{display:flex;flex-direction:column;gap:3px;padding:13px 15px;border-radius:9px;
    text-decoration:none;border:1px solid var(--rule);background:var(--surface)}}
  ul a:hover{{border-color:var(--accent)}}
  ul a strong{{font-family:Archivo,sans-serif;font-size:15.5px;color:var(--accent)}}
  ul a span{{color:var(--ink-2);font-size:14px;line-height:1.5}}
</style>"""
    (public / "docs" / "index.html").write_text(
        SKELETON.format(head=style.strip(), body=body.strip()))


CHANNELS_PAGE = """<!doctype html>
<html lang="bs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FireWatch - Telegram kanali</title>
<style>
  :root{{--bg:#faf8f5;--surface:#fff;--ink:#1c1917;--ink-2:#57534e;--rule:#e7e2da;--accent:#c2410c}}
  @media (prefers-color-scheme:dark){{:root{{--bg:#16130f;--surface:#211d18;--ink:#f5f1ea;--ink-2:#b4aca0;--rule:#3a342c;--accent:#fb923c}}}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif}}
  main{{max-width:760px;margin:0 auto;padding:36px 16px 80px}}
  h1{{margin:0 0 6px;font-size:28px}}
  p{{color:var(--ink-2);margin:0 0 18px}}
  a{{color:var(--accent)}}
  input{{width:100%;padding:11px 13px;font:inherit;color:var(--ink);background:var(--surface);
    border:1px solid var(--rule);border-radius:9px;margin-bottom:14px}}
  ul{{list-style:none;margin:0;padding:0;display:grid;gap:6px}}
  li a{{display:flex;justify-content:space-between;gap:12px;padding:11px 14px;border-radius:9px;
    text-decoration:none;color:var(--ink);background:var(--surface);border:1px solid var(--rule)}}
  li a:hover{{border-color:var(--accent)}}
  li a span{{color:var(--accent);white-space:nowrap}}
  #none{{display:none}}
</style>
</head>
<body>
<main>
  <h1>&#128293; Telegram kanali</h1>
  <p>Svaka op&#263;ina ima svoj Telegram kanal na kojem se objavljuju upozorenja o
  po&#382;arima. Odaberite svoju op&#263;inu i pretplatite se. <a href="../">Otvori mapu</a></p>
  <input id="q" type="search" placeholder="Tra&#382;i op&#263;inu..." autocomplete="off">
  <ul id="list">
{rows}
  </ul>
  <p id="none">Nema rezultata.</p>
</main>
<script>
  var q=document.getElementById("q"),items=document.querySelectorAll("#list li"),none=document.getElementById("none");
  function norm(s){{return s.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g,"").replace(/\u0111/g,"d");}}
  q.addEventListener("input",function(){{
    var v=norm(q.value.trim()),n=0;
    items.forEach(function(li){{var ok=norm(li.textContent).indexOf(v)!==-1;li.style.display=ok?"":"none";n+=ok;}});
    none.style.display=n?"none":"block";
  }});
</script>
</body>
</html>
"""


def channels(public: pathlib.Path) -> int:
    """Write /channels/: every municipality's public invite link, searchable.

    Built from data/bih/municipalities.json at deploy time, so a newly provisioned
    channel appears on the next publish. A municipality with no invite yet is left
    out rather than shown with a dead link.
    """
    munis = json.loads(MUNICIPALITIES.read_text())
    rows = sorted((m["short_name"], m["telegram_invite"])
                  for m in munis if m.get("telegram_invite"))
    items = "\n".join(
        f'    <li><a href="{html.escape(u)}" target="_blank" rel="noopener">'
        f'{html.escape(n)}<span>Pretplati se &rarr;</span></a></li>'
        for n, u in rows)
    out = public / "channels"
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(CHANNELS_PAGE.format(rows=items))
    return len(rows)


if __name__ == "__main__":
    pub = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "public")
    pages = build(pub)
    index(pub, pages)
    print(f"  {channels(pub)} channels -> {pub / 'channels'}")
    print(f"  {len(pages)} pages -> {pub / 'docs'}")
    for n, t, _ in pages:
        print(f"    {n:38} {t}")
