#!/usr/bin/env python3
"""Lift the documentation's diagrams out into standalone SVG files for the README.

The diagrams in docs/firewatch-documentation.html are inline <svg>, styled by the
page's own stylesheet and its :root palette. GitHub's Markdown sanitizer strips
inline <svg> from a README, so the figures have to be files referenced with <img> -
and an <img>-embedded SVG is an isolated document: it gets no page stylesheet, and
it cannot fetch a webfont. So each diagram is copied out with the .s-* rules it
needs inlined, every var() resolved from the page's own :root, and the Google-hosted
families swapped for system stacks.

Extracting rather than hand-copying is the point: the figure in the README is then
the same drawing as the figure in the docs, and a fix to one cannot silently leave
the other behind. Output is committed, like data/*.geojson - regenerate with:

    python3 deploy/extract-diagrams.py

Run from anywhere; paths are resolved against the repository root.
"""
from __future__ import annotations

import html.entities
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = DOCS / "img"

# The styling comes from one page for every diagram, wherever the drawing lives, so
# a figure extracted from the fork guide cannot end up a different colour from one
# extracted from the system documentation.
STYLE_SRC = DOCS / "firewatch-documentation.html"

# Identified by viewBox because it is the one attribute that is both unique per
# diagram and unlikely to be edited - a caption or heading above it can be reworded,
# and an id does not exist on these elements.
#
# alt is what a screen reader and a broken-image reader get. Two of the four already
# carry an aria-label in the docs; where they do, it wins, because it was written
# against the drawing. The other two never had one.
DIAGRAMS = [
    ("firewatch-documentation.html", "0 0 960 362", "timing-gaps.svg", None),
    ("firewatch-documentation.html", "0 0 900 268", "latency-budget.svg", None),
    ("firewatch-fork.html", "0 0 960 500", "github-actions.svg", None),
    ("firewatch-documentation.html", "0 0 720 250", "clustering.svg", None),
    ("firewatch-documentation.html", "0 0 760 668", "pipeline.svg",
     "The polling cycle. Three feeds - Meteosat MTG every 4 minutes, NASA FIRMS and "
     "Sentinel-3 every 20 - produce detections, which pass through six stages: a "
     "spatial clip to the municipality boundary plus a 2 km buffer; deduplication "
     "into SQLite, where INSERT OR IGNORE decides what is new; clustering into fire "
     "events by single linkage at 3.5 km and 8 hours; enrichment with the nearest "
     "settlement, wind and a spread-risk label; a diff against the previous cycle "
     "producing new, intensified, grew, corroborated, reignited and extinguished "
     "alerts; and delivery, rate-limited per event."),
    ("firewatch-documentation.html", "0 32 780 214", "alert-states.svg",
     "The state machine for one fire event. A first detection while the fire is still "
     "burning enters ACTIVE and raises a new alert; a first detection already older "
     "than the quiet window is recorded silently. ACTIVE means seen within 5 hours; "
     "5 hours with nothing moves the event to QUIET and raises extinguished. A fresh "
     "detection returns it to ACTIVE as reignited. While active, three self-transitions "
     "raise grew when the footprint widens by 0.5 km, intensified when peak power goes "
     "up 1.5 times and at least 3 MW, and corroborated when a second satellite sees it."),
]

# Archivo and IBM Plex Mono are loaded from Google Fonts by the docs page. An
# <img>-embedded SVG cannot fetch them, so asking for them would silently fall back
# to whatever the renderer picks - different on every reader's machine. Name the
# stacks instead, so the fallback is the design.
FONTS = {
    "Archivo,sans-serif": 'system-ui,-apple-system,"Segoe UI",Roboto,sans-serif',
    '"IBM Plex Mono",monospace': 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace',
}


# The five XML predefines an SVG file may keep. Everything else in the docs page is
# an HTML named entity - &times;, &ndash;, &middot; - which is undefined in a
# standalone SVG and makes the whole file a parse error rather than a wrong glyph.
XML_SAFE = {"amp", "lt", "gt", "quot", "apos"}


def unentity(text: str) -> str:
    """HTML named entities -> the characters themselves; XML's own five left alone."""
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in XML_SAFE:
            return m.group(0)
        char = html.entities.html5.get(name + ";") or html.entities.html5.get(name)
        return char if char else m.group(0)
    return re.sub(r"&([A-Za-z][A-Za-z0-9]*);", sub, text)


def devar(text: str, colors: dict[str, str]) -> str:
    """Resolve var(--x). Needed in the markup too: 22 elements carry inline styles."""
    return re.sub(r"var\((--[a-z0-9-]+)\)", lambda m: colors.get(m.group(1), "#000"), text)


def palette(src: str) -> dict[str, str]:
    """The page's :root custom properties, so a recoloured docs page recolours these."""
    root = re.search(r":root\s*\{(.*?)\}", src, re.S)
    if not root:
        sys.exit("no :root block in the docs page - has the stylesheet moved?")
    return {k: v.strip() for k, v in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", root.group(1))}


def svg_rules(src: str, colors: dict[str, str]) -> str:
    """Every .s-* rule from the page stylesheet, with var() and webfonts resolved."""
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", src, re.S))
    rules = re.findall(r"(\.s-[a-z0-9-]+)\s*\{([^}]*)\}", css)
    if not rules:
        sys.exit("no .s-* rules found - the diagrams would come out unstyled")
    out = []
    for sel, body in rules:
        body = devar(body, colors)
        for want, have in FONTS.items():
            body = body.replace(want, have)
        out.append(f"  {sel}{{{' '.join(body.split())}}}")
    return "\n".join(out)


def extract(src: str, view_box: str) -> tuple[str, str]:
    """(open tag, inner markup) for the one <svg> carrying this viewBox."""
    i = src.find(f'viewBox="{view_box}"')
    if i == -1:
        sys.exit(f"viewBox {view_box!r} is no longer in the docs page")
    start = src.rfind("<svg", 0, i)
    end = src.find("</svg>", i)
    tag_end = src.find(">", i)
    return src[start:tag_end + 1], src[tag_end + 1:end]


def build(open_tag: str, inner: str, rules: str, colors: dict[str, str],
          alt: str | None) -> str:
    vb = re.search(r'viewBox="([^"]*)"', open_tag).group(1)
    x, y, w, h = (float(n) for n in vb.split())
    label = alt or (re.search(r'aria-label="([^"]*)"', open_tag) or [None, ""])[1]
    if not label:
        sys.exit(f"no alt text for the {vb!r} diagram, and none in the page")
    label = unentity(label).replace("&", "&amp;").replace('"', "&quot;")
    ground = colors.get("--bg", "#ffffff")
    inner = unentity(devar(inner, colors))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" width="{w:g}" '
        f'height="{h:g}" role="img" aria-label="{label}">\n'
        f"<title>{label}</title>\n"
        f"<style>\n{rules}\n</style>\n"
        # An <img> is painted on whatever the reader's page happens to be. Without an
        # opaque ground these light-palette diagrams would sit on GitHub's dark theme
        # as dark text on dark. With it, they read as figure cards in either theme.
        f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" fill="{ground}"/>'
        f"{inner}</svg>\n"
    )


def main() -> int:
    style_src = STYLE_SRC.read_text()
    colors = palette(style_src)
    rules = svg_rules(style_src, colors)
    OUT.mkdir(parents=True, exist_ok=True)
    pages: dict[str, str] = {}
    for page, view_box, name, alt in DIAGRAMS:
        if page not in pages:
            pages[page] = (DOCS / page).read_text()
        open_tag, inner = extract(pages[page], view_box)
        svg = build(open_tag, inner, rules, colors, alt)
        (OUT / name).write_text(svg)
        print(f"  {name:22} {len(svg):>6,} bytes   {page}")
    print(f"\n  {len(DIAGRAMS)} diagrams -> {OUT.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
