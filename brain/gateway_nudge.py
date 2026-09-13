"""Tenant → gateway nudge: tell the reconciler something changed, right now.

The gateway's reconciler (brain/gateway/reconciler.py) is event-driven. A tenant
process is the source of most of the events that matter — it wrote a placement
row, it wants its GPU pod, it just got output back, its pressure changed — so it
tells the gateway over the loopback route the gateway exposes for exactly this
(`POST /__nudge`, token-gated). Fire-and-forget from a daemon thread with a
short timeout, throttled per reason so a hot dispatch path never becomes a
request storm; a missing URL (single-brain / local mode) or any failure is a
no-op — the gateway's resync covers a lost nudge.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

URL_ENV = "BRAIN_GATEWAY_NUDGE_URL"
TOKEN_ENV = "BRAIN_GATEWAY_NUDGE_TOKEN"  # noqa: S105 - env var NAME, not a secret

# Minimum spacing per reason (seconds). Structural changes go immediately; the
# dispatch-path signals are coalesced — the gateway debounces too.
THROTTLE_S: dict[str, float] = {
    "placement": 0.0,
    "budget": 0.0,
    "sleep": 0.0,
    "demand": 5.0,
    "use": 5.0,
    "pressure": 10.0,
}

_last_sent: dict[str, float] = {}
_lock = threading.Lock()


def configured() -> bool:
    return bool(os.environ.get(URL_ENV, "").strip())


def _send(url: str, token: str, body: dict) -> None:
    try:
        import httpx

        httpx.post(url, json=body, headers={"x-brain-nudge-token": token}, timeout=2.0)
    except Exception as e:
        logger.debug("[nudge] %s failed: %s", body.get("reason"), e)


def nudge(reason: str, *, now: float | None = None, **fields) -> bool:
    """Send one nudge (throttled per reason). Returns True when a request was
    dispatched, False when skipped (unconfigured or throttled). Never raises."""
    url = os.environ.get(URL_ENV, "").strip()
    if not url:
        return False
    ts = time.time() if now is None else now
    gap = THROTTLE_S.get(reason, 5.0)
    with _lock:
        last = _last_sent.get(reason, 0.0)
        if gap > 0 and ts - last < gap:
            return False
        _last_sent[reason] = ts
    body = {"reason": str(reason)[:32], "key": os.environ.get("BRAIN_PROC_KEY", "")}
    body.update({k: v for k, v in fields.items() if v is not None})
    token = os.environ.get(TOKEN_ENV, "")
    threading.Thread(
        target=_send, args=(url, token, body), daemon=True, name=f"nudge-{reason}"
    ).start()
    return True


def reset() -> None:
    with _lock:
        _last_sent.clear()


__all__ = ["THROTTLE_S", "TOKEN_ENV", "URL_ENV", "configured", "nudge", "reset"]
