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
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# Read rather than imported: this script runs on a runner that installs only
# requests and certifi, and it has no business importing the package to learn one
# name. A missing or unreadable profile just leaves the index page unnamed.
def _area() -> str:
    import json
    try:
        pl = json.loads((ROOT / "data" / "place.json").read_text(encoding="utf-8"))
        return pl["names"][pl.get("language", "en")]["area"]
    except Exception:
        try:
            return pl["names"]["en"]["area"]
        except Exception:
            return "your municipality"

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
      <p class="standfirst">Near-live wildfire monitoring for {html.escape(_area())}, from
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


if __name__ == "__main__":
    pub = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "public")
    pages = build(pub)
    index(pub, pages)
    print(f"  {len(pages)} pages -> {pub / 'docs'}")
    for n, t, _ in pages:
        print(f"    {n:38} {t}")
