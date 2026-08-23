"""Publish the fire map through ngrok.

Design constraints that shaped this:

* The ngrok free tier allows one *agent session*, not one tunnel. So this never
  runs `ngrok http ...` when an agent is already up - that would start a second
  session and be rejected. Instead it POSTs to the agent's local API on :4040,
  which is exactly how pyngrok adds tunnels, and the new tunnel joins the
  existing session alongside whatever else is already published.
* Only PUBLIC_DIR is served, holding nothing but the map and its data file. The
  database, log and snapshot stay in the parent directory and never get a URL.
* ngrok 3.x can serve a directory directly (`file:///path`), so no local web
  server is needed.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import mapgen
from .config import MAP_PATH, PUBLIC_DIR

log = logging.getLogger("firewatch.expose")

AGENT_API = "http://127.0.0.1:4040/api"
TUNNEL_NAME = "firewatch"

# pyngrok keeps its own copy here; a Homebrew/manual install lands on PATH.
NGROK_CANDIDATES = [
    Path.home() / "Library" / "Application Support" / "ngrok" / "ngrok",
]


class ExposeError(RuntimeError):
    pass


# --------------------------------------------------------------------- helpers

def ngrok_binary() -> Path | None:
    found = shutil.which("ngrok")
    if found:
        return Path(found)
    return next((p for p in NGROK_CANDIDATES if p.exists()), None)


def _api(path: str, method: str = "GET", payload: dict | None = None,
         timeout: float = 10.0):
    url = f"{AGENT_API}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body else {}


def agent_up() -> bool:
    try:
        _api("/tunnels", timeout=3)
        return True
    except Exception:
        return False


def tunnels() -> list[dict]:
    try:
        return _api("/tunnels").get("tunnels", [])
    except Exception:
        return []


def find_tunnel(name: str = TUNNEL_NAME) -> dict | None:
    for t in tunnels():
        # A tunnel created as "firewatch" is reported as "firewatch" and, for the
        # https variant, sometimes "firewatch (http)".
        if str(t.get("name", "")).split(" ")[0] == name:
            return t
    return None


def start_agent(timeout: float = 20.0) -> None:
    """Start a tunnel-less agent session if none is running."""
    exe = ngrok_binary()
    if exe is None:
        raise ExposeError(
            "ngrok not found. Install it, or run your existing ngrok agent first.")
    subprocess.Popen([str(exe), "start", "--none", "--log", "stdout"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if agent_up():
            return
        time.sleep(0.5)
    raise ExposeError("started ngrok but its local API never came up on :4040")


# ------------------------------------------------------------------ public dir

def stage() -> Path:
    """Create PUBLIC_DIR and put the current map in it."""
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    if not Path(MAP_PATH).exists():
        raise ExposeError(
            f"no map to publish yet at {MAP_PATH} - run `python3 -m firewatch poll` first")
    mapgen.sync_public()
    return PUBLIC_DIR


# --------------------------------------------------------------------- actions

def expose() -> dict:
    """Publish the map. Returns the tunnel record.

    Idempotent: if a firewatch tunnel already exists, its URL is returned rather
    than creating a duplicate.
    """
    stage()
    started_agent = False
    if not agent_up():
        start_agent()
        started_agent = True

    existing = find_tunnel()
    if existing:
        existing["_reused"] = True
        existing["_started_agent"] = started_agent
        return existing

    others = len(tunnels())
    try:
        t = _api("/tunnels", "POST", {
            "name": TUNNEL_NAME,
            "proto": "http",
            # as_uri() percent-encodes the space in "Application Support"
            "addr": PUBLIC_DIR.as_uri(),
            "schemes": ["https"],
        }, timeout=30)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise ExposeError(
            f"ngrok refused the tunnel (HTTP {exc.code}). {detail}") from exc
    t["_reused"] = False
    t["_started_agent"] = started_agent
    t["_siblings"] = others
    return t


def unexpose() -> bool:
    """Remove the firewatch tunnel, leaving any other tunnels alone."""
    t = find_tunnel()
    if not t:
        return False
    name = urllib.request.quote(str(t["name"]), safe="")
    try:
        _api(f"/tunnels/{name}", "DELETE")
    except urllib.error.HTTPError as exc:
        if exc.code not in (204, 404):
            raise ExposeError(f"could not remove tunnel: HTTP {exc.code}") from exc
    return True


def status() -> dict:
    return {
        "agent_up": agent_up(),
        "ngrok": str(ngrok_binary() or ""),
        "public_dir": str(PUBLIC_DIR),
        "staged": sorted(p.name for p in PUBLIC_DIR.glob("*")) if PUBLIC_DIR.is_dir() else [],
        "firewatch_tunnel": (find_tunnel() or {}).get("public_url"),
        "all_tunnels": [
            {"name": t.get("name"), "url": t.get("public_url"),
             "addr": t.get("config", {}).get("addr")}
            for t in tunnels()
        ],
    }
