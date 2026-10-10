"""What Connect will actually do for each catalogue entry, checked against the
live provider.

The catalogue says who a server is; this says how an org gets in today:

  one_click    the provider registers this console itself (RFC 7591)
  platform     no self-registration, but this deployment holds an app for the
               vendor (catalog.platform_app) — also one click for the org
  own_app      no self-registration and no platform app: the org creates an
               OAuth app in the vendor's console and pastes its id + secret
  api_key      the server takes a bearer the org already holds
  unreachable  the server published no OAuth metadata when last checked

A static guess (from `auth` / `app`) is served until a live check lands. Checks
run in the background when the Connectors page asks for the catalogue and the
last one is older than CHECK_TTL_S, or on demand from "Re-check". Results are
process-local: vendors change this rarely, and a cold process re-learns it in
one page load.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from brain.connectors import oauth
from brain.connectors.catalog import platform_app

logger = logging.getLogger(__name__)

CHECK_TTL_S = 12 * 3600
_PER_SERVER_TIMEOUT_S = 25.0

_results: dict[str, dict] = {}  # catalog id -> {setup, detail, checked_ts}
_inflight: asyncio.Task | None = None


def static_setup(entry: dict) -> str:
    if entry.get("auth") == "api_key":
        return "api_key"
    if entry.get("app"):
        return "platform" if platform_app(entry["app"]) else "own_app"
    return "one_click"


async def check_entry(entry: dict) -> dict:
    """Probe one entry's discovery chain. Never raises."""
    now = time.time()
    if entry.get("auth") == "api_key":
        return {"setup": "api_key", "detail": "", "checked_ts": now}
    try:
        meta = await asyncio.wait_for(oauth.discover(entry["url"]), _PER_SERVER_TIMEOUT_S)
    except Exception as e:
        detail = str(e) if isinstance(e, oauth.OAuthError) else f"no answer ({type(e).__name__})"
        return {"setup": "unreachable", "detail": detail, "checked_ts": now}
    if meta.registration_endpoint:
        setup = "one_click"
    elif platform_app(entry.get("app")):
        setup = "platform"
    else:
        setup = "own_app"
    return {"setup": setup, "detail": meta.issuer, "checked_ts": now}


async def recheck(entries: list[dict]) -> None:
    results = await asyncio.gather(*(check_entry(e) for e in entries))
    for e, r in zip(entries, results, strict=True):
        _results[e["id"]] = r


def is_stale(entries: list[dict]) -> bool:
    now = time.time()
    return any(
        now - (_results.get(e["id"]) or {}).get("checked_ts", 0) > CHECK_TTL_S for e in entries
    )


def kick(entries: list[dict]) -> None:
    """Start a background re-check if one is due and none is running."""
    global _inflight
    if _inflight is not None and not _inflight.done():
        return
    if not is_stale(entries):
        return
    with contextlib.suppress(RuntimeError):  # no running loop (sync caller)
        _inflight = asyncio.get_running_loop().create_task(recheck(entries))


def annotate(entries: list[dict]) -> list[dict]:
    """Copy `setup`, `setup_detail`, `checked_ts` onto each entry — the live
    result when there is one, the static guess otherwise. A platform app set
    or unset since the last check is honoured immediately."""
    out = []
    for e in entries:
        e = dict(e)
        live = _results.get(e["id"])
        setup = live["setup"] if live else static_setup(e)
        if setup in ("platform", "own_app"):
            setup = "platform" if platform_app(e.get("app")) else "own_app"
        e["setup"] = setup
        e["setup_detail"] = (live or {}).get("detail", "")
        e["checked_ts"] = (live or {}).get("checked_ts")
        out.append(e)
    return out


def reset() -> None:
    """Tests: forget every result."""
    global _inflight
    _results.clear()
    _inflight = None
