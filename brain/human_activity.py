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

A second, PER-PERSONA stamp (`personas/<slug>/.last_human_turn` under the same org
root) records the last human turn with each persona. It does not feed dormancy —
that stays org-level — it decides which personas of an ISOLATED org sit on the
shared idle loop: a purchase persona thinks idle while somebody has talked to it in
the last `dmn_active_roster_days`, and drops off the roster (not the org) after.

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
# Per-persona throttle, keyed by slug (one persona's turn must not suppress the
# next persona's first stamp).
_persona_last_write_ts: dict[str, float] = {}

ROSTER_MODES = ("home", "active", "all")


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


def _persona_path(slug: str) -> Path:
    return org_state_root() / "personas" / slug / FILENAME


def _slug(persona: str) -> str:
    from brain.persona_key import persona_slug

    return persona_slug(persona)


def _write_stamp(p: Path, ts: float) -> bool:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(f"{ts:.3f}", encoding="utf-8")
    os.replace(tmp, p)
    return True


def _read_stamp(p: Path) -> float | None:
    try:
        raw = p.read_text(encoding="utf-8").strip()
        ts = float(raw)
        return ts if ts > 0 else None
    except Exception:
        return None


def stamp(now: float | None = None, *, force: bool = False) -> bool:
    """Record a human turn. Throttled to one write per minute unless `force`.
    Returns True when a write happened. Never raises."""
    global _last_write_ts
    ts = float(now if now is not None else time.time())
    if not force and (ts - _last_write_ts) < _WRITE_THROTTLE_S:
        return False
    try:
        _write_stamp(_path(), ts)
        _last_write_ts = ts
        return True
    except Exception as e:  # pragma: no cover - best effort
        logger.debug("[human_activity] stamp failed: %s", e)
        return False


def last_turn_ts() -> float | None:
    """Wall-clock of the last recorded human turn for this org, or None if unknown."""
    return _read_stamp(_path())


def stamp_persona(persona: str, now: float | None = None, *, force: bool = False) -> bool:
    """Record a human turn WITH `persona` (display name or slug). Same atomic write
    and one-per-minute throttle as `stamp`, keyed per slug. An empty persona is a
    no-op (the caller resolves home). Returns True when a write happened. Never
    raises."""
    slug = _slug(persona)
    if not slug:
        return False
    ts = float(now if now is not None else time.time())
    if not force and (ts - _persona_last_write_ts.get(slug, 0.0)) < _WRITE_THROTTLE_S:
        return False
    try:
        _write_stamp(_persona_path(slug), ts)
        _persona_last_write_ts[slug] = ts
        return True
    except Exception as e:  # pragma: no cover - best effort
        logger.debug("[human_activity] persona stamp failed for %s: %s", slug, e)
        return False


def persona_last_turn_ts(persona: str) -> float | None:
    """Wall-clock of the last recorded human turn with `persona`, or None if unknown."""
    slug = _slug(persona)
    if not slug:
        return None
    return _read_stamp(_persona_path(slug))


def persona_active(persona: str, days: float, now: float | None = None) -> bool:
    """Has a human taken a turn with `persona` in the last `days`? `days <= 0` means
    every persona counts as active. Unknown (never stamped) is inactive: a persona
    nobody has ever talked to has nothing to think about yet."""
    if days <= 0:
        return True
    ts = persona_last_turn_ts(persona)
    if ts is None:
        return False
    ref = float(now if now is not None else time.time())
    return (ref - ts) <= days * 86400.0


def isolated_roster_mode() -> str:
    """settings `dmn_isolated_roster`: which personas of an ISOLATED org share the
    idle loop. 'home' = the home persona only (today's behaviour, the kill switch);
    'active' = home + every full-tier persona with a human turn in the last
    `dmn_active_roster_days`; 'all' = every full-tier persona, as a consolidated org.
    Unknown values read as 'home' (fail closed)."""
    try:
        from brain.settings import settings

        mode = str(settings.get("dmn_isolated_roster", "active") or "active").strip().lower()
    except Exception:
        return "home"
    return mode if mode in ROSTER_MODES else "home"


def active_roster_days() -> float:
    """settings `dmn_active_roster_days` as a float (0 = all personas count as active)."""
    try:
        from brain.settings import settings

        return max(0.0, float(settings.get("dmn_active_roster_days", 7) or 0.0))
    except Exception:
        return 7.0


def seed_clock(default: float | None = None) -> float:
    """What a fresh process should treat as 'last human turn': the persisted stamp if
    there is one, else `default` (now). A stamp in the future (clock skew) is clamped."""
    now = time.time()
    persisted = last_turn_ts()
    if persisted is None:
        return float(default if default is not None else now)
    return min(persisted, now)
