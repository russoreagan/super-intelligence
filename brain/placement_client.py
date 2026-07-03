"""Consumer side of elastic persona placement.

The gateway derives, per org, which personas currently run on DEDICATED brain
instances and publishes them to the org's placement file (provisioner.
write_placement_files). The org's SHARED instance reads that file here and
drops those personas from its DMN roster — a promoted persona thinks in its
own process, not as a time-slice of the shared loop, and never in both at once.

Same shape as the RunPod host-file consumer (runpod_manager._consumer_host_
refresh): env-injected path, mtime-checked cache, fail-open. A missing or
unreadable file means "nobody is promoted" — the shared instance simply serves
everyone, which is always safe (it's the fallback by design).
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 30.0

_cached: set[str] = set()
_cached_at: float = 0.0
_cached_mtime: float = -1.0


def promoted_personas() -> set[str]:
    """Persona slugs currently hosted by dedicated instances of this org.
    Empty set when placement isn't in play (no env, no file, any error)."""
    global _cached, _cached_at, _cached_mtime
    path = os.environ.get("BRAIN_PLACEMENT_FILE", "").strip()
    if not path:
        return set()
    now = time.time()
    if now - _cached_at < _CACHE_TTL_S:
        return _cached
    _cached_at = now
    try:
        mtime = os.path.getmtime(path)
    except OSError:  # gone = nobody promoted (gateway removes the empty file)
        _cached, _cached_mtime = set(), -1.0
        return _cached
    if mtime == _cached_mtime:
        return _cached
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _cached = {str(p) for p in (data.get("promoted") or []) if p}
        _cached_mtime = mtime
        logger.info("[placement] promoted personas: %s", sorted(_cached) or "(none)")
    except Exception as e:
        logger.debug("[placement] read failed (%s) — keeping previous view", e)
    return _cached
