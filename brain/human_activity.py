"""Org-level "last human turn" clock, persisted to the volume.

The DMN keeps its own in-memory engagement clock (`_last_user_activity_ts`), which
starts at boot time — so every respawn looked like a fresh engagement and an org
nobody had talked to for weeks got another full run of idle thinking, self-tasks
and pod demand each time its brain came back. This module stamps the wall-clock
of the last REAL human turn (owner UI or engine API, any persona of the org) into
a small file at the org's state root, and the DMN seeds its clock from it at boot.

Org-scoped on purpose: "no human interactions with an agent on the platform" is
the abandonment signal, not "no turns with this persona". SECOND_BRAIN_PATH is
re-namespaced per persona in multi-tenant mode (tenants/<org>/second_brain/
personas/<slug>), so the file lives one level up, at the org root.

Writes are throttled and atomic; reads never raise.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

FILENAME = ".last_human_turn"
_WRITE_THROTTLE_S = 60.0
_last_write_ts: float = 0.0


def org_state_root() -> Path:
    """The org-canonical second_brain root, whatever persona this process is bound to."""
    root = Path(
        os.environ.get("SECOND_BRAIN_PATH")
        or str(Path(__file__).resolve().parent.parent / "second_brain")
    )
    if root.parent.name == "personas":
        root = root.parent.parent
    return root


def _path() -> Path:
    return org_state_root() / FILENAME


def stamp(now: float | None = None, *, force: bool = False) -> bool:
    """Record a human turn. Throttled to one write per minute unless `force`.
    Returns True when a write happened. Never raises."""
    global _last_write_ts
    ts = float(now if now is not None else time.time())
    if not force and (ts - _last_write_ts) < _WRITE_THROTTLE_S:
        return False
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(f"{ts:.3f}", encoding="utf-8")
        os.replace(tmp, p)
        _last_write_ts = ts
        return True
    except Exception as e:  # pragma: no cover - best effort
        logger.debug("[human_activity] stamp failed: %s", e)
        return False


def last_turn_ts() -> float | None:
    """Wall-clock of the last recorded human turn for this org, or None if unknown."""
    try:
        raw = _path().read_text(encoding="utf-8").strip()
        ts = float(raw)
        return ts if ts > 0 else None
    except Exception:
        return None


def seed_clock(default: float | None = None) -> float:
    """What a fresh process should treat as 'last human turn': the persisted stamp if
    there is one, else `default` (now). A stamp in the future (clock skew) is clamped."""
    now = time.time()
    persisted = last_turn_ts()
    if persisted is None:
        return float(default if default is not None else now)
    return min(persisted, now)
