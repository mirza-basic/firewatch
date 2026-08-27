"""Serve the map over HTTP.

`expose.py` publishes through an ngrok tunnel, which suits a laptop: no ports, no
DNS, no TLS to arrange. On a host with an address of its own that indirection is
in the way, and the free tier's random URL - which changes every time a shared
agent restarts - is exactly the wrong property for something people bookmark.

So this is the other half: a plain static server over PUBLIC_DIR, which holds the
map and its data file and nothing else. The database, log and snapshot live in a
different directory entirely and cannot be reached from here.

Two deliberate choices:

* It binds 127.0.0.1 unless told otherwise. Putting the map on the public internet
  should be a decision someone typed, and on a real host it belongs behind nginx or
  Caddy anyway - they terminate TLS, which this does not do.
* It re-renders from the stored snapshot on startup, so a fresh container serves a
  real map immediately rather than 404ing until the first poll finishes.
"""
from __future__ import annotations

import json
import logging
import threading
from functools import partial
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import CFG, PUBLIC_DIR, SNAPSHOT_PATH

log = logging.getLogger("firewatch.serve")


def health() -> tuple[int, dict]:
    """(HTTP status, payload) describing whether this instance is actually working.

    "Serving" and "healthy" are different questions: the static files stay readable
    long after polling has stopped, so a monitor that only fetches / would never
    notice the data going stale. What matters is the age of the snapshot, which is
    rewritten every cycle.

    Deliberately says nothing about keys, paths or the public URL - this endpoint is
    reachable by anyone who can reach the map.
    """
    max_age = float(CFG["health_max_age_s"])
    try:
        snap = json.loads(Path(SNAPSHOT_PATH).read_text())
    except (OSError, ValueError):
        # No snapshot yet is a real state, not an error: a host that has just come
        # up has nothing to serve until the first cycle lands.
        return 503, {"status": "starting", "age_seconds": None}

    gen = snap.get("generated_at")
    try:
        age = (datetime.now(timezone.utc)
               - datetime.strptime(gen, "%Y-%m-%dT%H:%M:%SZ").replace(
                   tzinfo=timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return 503, {"status": "unknown", "age_seconds": None}

    raw = snap.get("source_status") or {}
    # A source with no key is excluded, not counted as down: otherwise an install
    # running deliberately without FIRMS reports 503 for ever.
    sources = {k: bool(v.get("ok")) for k, v in raw.items() if v.get("configured", True)}
    unconfigured = sorted(k for k, v in raw.items() if not v.get("configured", True))
    body = {
        "status": "ok" if age <= max_age else "stale",
        "generated_at": gen,
        "age_seconds": round(age),
        "max_age_seconds": round(max_age),
        "active_fires": (snap.get("summary") or {}).get("n_active", 0),
        "events": len(snap.get("events") or []),
        "detections": snap.get("n_detections", 0),
        "sources": sources,
        "buffer_km": snap.get("buffer_km"),
    }
    if unconfigured:
        body["unconfigured"] = unconfigured
    # One dead feed is normal and self-healing; all of them is not, and the map is
    # only as live as its fastest working source.
    if sources and not any(sources.values()):
        body["status"] = "sources_down"
    return (200 if body["status"] == "ok" else 503), body


class _Handler(SimpleHTTPRequestHandler):
    """Static files only, quiet logs, and no caching of the data file.

    SimpleHTTPRequestHandler resolves every request against `directory` and
    rejects traversal, so PUBLIC_DIR is the whole surface. GET and HEAD are the
    only methods it implements; anything else is already a 501.
    """

    def log_message(self, fmt, *args):        # noqa: A003 - base class name
        log.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/") in ("/health", "/healthz"):
            return self._health()
        return super().do_GET()

    def do_HEAD(self):
        if self.path.split("?")[0].rstrip("/") in ("/health", "/healthz"):
            return self._health(body=False)
        return super().do_HEAD()

    def _health(self, body: bool = True):
        status, payload = health()
        blob = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(blob)

    def list_directory(self, path):
        """Never index a directory.

        refresh_public() guarantees an index.html, so a listing can only appear if
        something went wrong - and then it would enumerate the directory instead of
        saying so. 404 is both safer and more honest.
        """
        self.send_error(404, "Not found")
        return None

    def end_headers(self):
        # The page re-reads fire-map-data.js every 60 s with a cache-busting query
        # string, but a proxy that ignores the query would still pin it. Say it
        # plainly instead.
        if self.path.startswith("/fire-map-data.js"):
            self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def refresh_public() -> bool:
    """Rebuild PUBLIC_DIR from the stored snapshot.

    Returns True if a real snapshot was used. With none - a fresh host, before the
    first poll finishes - it renders an *empty* snapshot rather than leaving the
    directory bare, because "no fires" is the correct thing to show and a bare
    directory is not a page at all.
    """
    from . import mapgen, poller
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    try:
        snapshot = json.loads(Path(SNAPSHOT_PATH).read_text())
        real = True
    except (OSError, ValueError):
        snapshot = poller._empty_snapshot()
        real = False
    mapgen.render(snapshot)
    return real


def make_server(host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    host = CFG["serve_host"] if host is None else host
    port = int(CFG["serve_port"] if port is None else port)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    handler = partial(_Handler, directory=str(PUBLIC_DIR))
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def serve_forever(host: str | None = None, port: int | None = None,
                  background: bool = False) -> ThreadingHTTPServer:
    """Start the server. In the background it runs on a daemon thread, so the
    caller's own loop - the poller - stays on the main thread."""
    httpd = make_server(host, port)
    shown = httpd.server_address[0]
    if shown in ("0.0.0.0", "::"):
        shown = "<this host>"
    log.info("serving %s on http://%s:%d/", PUBLIC_DIR, shown, httpd.server_address[1])
    if background:
        threading.Thread(target=httpd.serve_forever, name="firewatch-http",
                         daemon=True).start()
    else:
        httpd.serve_forever()
    return httpd
