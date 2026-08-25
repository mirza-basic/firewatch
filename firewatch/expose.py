"""Publish the fire map through ngrok.

Design constraints that shaped this:

* The ngrok free tier allows one *agent session*, not one tunnel. So this never
  runs `ngrok http ...` when an agent is already up - that would start a second
  session and be rejected. Instead it POSTs to the agent's local API on :4040,
  which is exactly how pyngrok adds tunnels, and the new tunnel joins the
  existing session alongside whatever else is already published.
* Only PUBLIC_DIR is served, holding nothing but the map and its data file. The
  database, log and snapshot live elsewhere entirely and never get a URL.
* ngrok 3.x can serve a directory directly (`file:///path`), so no local web
  server is needed. Two of its limits bite here: a free account allows three
  endpoints per agent session (a fourth is refused with ERR_NGROK_324), and the
  agent will not tear a `file://` tunnel down - the DELETE simply never returns.
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
from .config import CFG, MAP_PATH, PUBLIC_DIR

log = logging.getLogger("firewatch.expose")

AGENT_API = "http://127.0.0.1:4040/api"
TUNNEL_NAME = "firewatch"

# When re-publishing fails it fails identically every cycle (no binary, agent
# refusing the session), so back off rather than warn every few minutes.
RETRY_AFTER_S = 600.0
_next_retry = 0.0

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
        # https variant, sometimes "firewatch (http)". "firewatch-2" and friends
        # are ours too - see _create_tunnel on why the name has to move.
        n = str(t.get("name", "")).split(" ")[0]
        if n == name or n.startswith(name + "-"):
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


def _create_tunnel(timeout: float = 45.0) -> dict:
    """POST the tunnel, waiting out the agent's start-up window.

    The local API starts answering as soon as the process is up, but tunnels
    cannot be created until the agent has established its session with ngrok's
    servers - a second or two later, longer on a cold link. Until then every POST
    comes back 503 / error_code 104, which makes a perfectly healthy agent that
    was started moments ago indistinguishable from a broken one. So retry rather
    than believe the first answer.
    """
    payload = {
        "name": TUNNEL_NAME,
        "proto": "http",
        # Safe only because PUBLIC_DIR has no spaces - see the note on it in
        # config.py. as_uri() would percent-encode one, and ngrok serves the
        # escaped path verbatim.
        "addr": PUBLIC_DIR.as_uri(),
        "schemes": ["https"],
    }
    deadline = time.time() + timeout
    attempt = 1
    while True:
        try:
            return _api("/tunnels", "POST", payload, timeout=30)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if (exc.code == 503 and '"error_code":104' in detail
                    and time.time() < deadline):
                time.sleep(1.0)
                continue
            # A POST that failed while the session was still coming up leaves the
            # name registered but the tunnel neither listed nor running, and that
            # is terminal: DELETE 404s and every later POST is refused. The name
            # is only reusable once the agent restarts, so take the next one.
            if "already exists" in detail and attempt < 5:
                attempt += 1
                payload["name"] = f"{TUNNEL_NAME}-{attempt}"
                log.info("tunnel name in use by a dead registration, trying %s",
                         payload["name"])
                continue
            raise ExposeError(
                f"ngrok refused the tunnel (HTTP {exc.code}). "
                f"{detail[:400]}") from exc


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

def expose(remember: bool = True) -> dict:
    """Publish the map. Returns the tunnel record.

    Idempotent: if a firewatch tunnel already exists, its URL is returned rather
    than creating a duplicate. `remember` records the intent to stay published so
    ensure() can rebuild the tunnel after an agent restart; the rebuild itself
    passes False, since it is acting on an intent already recorded.
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
        if remember:
            _remember(True)
        return existing

    others = len(tunnels())
    t = _create_tunnel()
    t["_reused"] = False
    t["_started_agent"] = started_agent
    t["_siblings"] = others
    if remember:
        _remember(True)
    return t


def unexpose() -> bool:
    """Remove the firewatch tunnel, leaving any other tunnels alone.

    Publishing is switched off first and unconditionally, so that even when the
    agent refuses to drop the tunnel the poller will not keep re-creating it.
    """
    _remember(False)
    t = find_tunnel()
    if not t:
        return False
    name = urllib.request.quote(str(t["name"]), safe="")
    try:
        _api(f"/tunnels/{name}", "DELETE", timeout=5)
    except urllib.error.HTTPError as exc:
        if exc.code not in (204, 404):
            raise ExposeError(f"could not remove tunnel: HTTP {exc.code}") from exc
    except TimeoutError as exc:
        # ngrok 3.37.2 hangs on DELETE of a file:// tunnel and leaves it listed.
        # Nothing here can force it, so say so rather than reporting success.
        raise ExposeError(
            "ngrok will not close a file:// tunnel - the URL stays up until its "
            "agent restarts. Publishing is now off, so it will not be re-created."
        ) from exc
    return True


def _remember(on: bool) -> None:
    """Persist whether the map is meant to be published."""
    if bool(CFG.get("auto_expose")) == on:
        return
    CFG["auto_expose"] = on
    try:
        CFG.save()
    except Exception:
        log.warning("could not persist auto_expose=%s", on)


def ensure() -> str | None:
    """Re-establish the tunnel if it is supposed to be up but is not.

    Called once per poll cycle. This exists because the failure it fixes is
    invisible from inside FireWatch: the ngrok agent is shared with anything else
    on this machine that uses ngrok, and when it restarts it drops every tunnel
    it was carrying. The poller carries on writing fresh files into PUBLIC_DIR
    and the local map keeps working, so the only symptom is that the public URL -
    already sent out in SMS alerts, and unrecoverable once gone, because free-tier
    URLs are random - quietly stops resolving.

    Returns the public URL, or None if publishing is off or unavailable.
    """
    global _next_retry
    if not CFG.get("auto_expose"):
        return None
    t = find_tunnel()
    if t:
        return t.get("public_url")
    if time.monotonic() < _next_retry:
        return None
    try:
        t = expose(remember=False)
    except Exception as exc:
        _next_retry = time.monotonic() + RETRY_AFTER_S
        log.warning("could not re-publish the fire map: %s", exc)
        return None
    url = t.get("public_url")
    log.info("re-published the fire map at %s (the tunnel had gone away)", url)
    return url


def status() -> dict:
    return {
        "agent_up": agent_up(),
        "auto_expose": bool(CFG.get("auto_expose")),
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
