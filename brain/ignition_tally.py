"""ignition_tally — content-free per-persona Global-Workspace ignition pressure.

The turn path records +1 per IGNITED turn (coalition label only — never focus content,
entities, or text); the Tier-2 recruiter reads the exponentially time-decayed total as an
ALTERNATIVE recruitment trigger (hebbian._maybe_recruit_nodes). File:
persona_state_root(persona)/"ignition_tally.json", shape
{"<coalition>": {"score": float, "last_ts": float}}. Killable via
node_recruit_from_ignition (record() checks the flag itself). Best-effort throughout —
a lost increment or reset only slows recruitment, never breaks a turn.

record() is ZERO-DISK-I/O: increments accumulate in an in-process pending list (turn
path stays off the filesystem, and there is no read-modify-write race between the turn
coroutine and the DMN). flush() merges them into the durable file with the same decay
math, using each increment's record-time timestamp — pressure() flushes first, so the
sleep/Hebbian recruiter keeps exact read-your-writes semantics. A process death loses
only the unflushed increments (slower recruitment, nothing broken).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from brain.bounded_ledger import decay
from brain.settings import settings

# The exact thalamus._COALITION vocabulary — anything else is clamped to "other" so the
# persisted file can never carry content even if the verdict shape drifts.
_COALITIONS = frozenset({"threat", "salience", "memory", "vision"})
_FILENAME = "ignition_tally.json"

# In-memory pending increments: persona → [(coalition, record-time ts), ...].
# Appended by record() on the turn path; drained by flush() (via pressure()) during
# the sleep/Hebbian pass. Single event loop + GIL make append/pop race-free.
_pending: dict[str, list[tuple[str, float]]] = {}


def _now() -> float:  # seam: tests patch brain.ignition_tally._now
    return time.time()


def _resolve(persona: str) -> str:
    if persona:
        return persona
    from brain.persona_key import active_or_home_persona

    return active_or_home_persona()


def _path(persona: str) -> Path:
    from brain.persona_key import persona_state_root

    return persona_state_root(persona) / _FILENAME


def _load(persona: str) -> dict:
    try:
        p = _path(persona)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save(persona: str, data: dict) -> None:
    try:
        p = _path(persona)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _decayed(score: float, last_ts: float, now: float) -> float:
    hl_s = max(0.1, float(settings.get("ignition_tally_half_life_h", 72.0))) * 3600.0
    return decay(score, last_ts, now, hl_s)


def record(coalition: str, persona: str = "") -> None:
    """+1 ignition for the bound persona — in-memory only (no disk I/O on the turn
    path; flush() persists later). No-op when the kill switch is off. Coalition is
    clamped to the fixed vocabulary so the tally stays content-free."""
    if not settings.get("node_recruit_from_ignition", 1):
        return
    try:
        who = _resolve(persona)
        c = coalition if coalition in _COALITIONS else "other"
        _pending.setdefault(who, []).append((c, _now()))
    except Exception:
        pass


def flush(persona: str = "") -> None:
    """Merge the pending in-memory increments into the durable file. Replays each
    increment at its record-time timestamp through the same decay math record()
    used when it wrote inline, so the file is byte-for-byte-semantics identical
    (existing tallies carry over unchanged). Never raises."""
    try:
        who = _resolve(persona)
        items = _pending.pop(who, [])
        if not items:
            return
        data = _load(who)
        for c, ts in items:
            entry = data.get(c) or {}
            score = _decayed(float(entry.get("score", 0.0)), float(entry.get("last_ts", ts)), ts)
            data[c] = {"score": score + 1.0, "last_ts": ts}
        _save(who, data)
    except Exception:
        pass


def pressure(persona: str = "") -> tuple[float, str]:
    """(total decayed score across coalitions, dominant coalition or ""). Flushes
    pending increments first — the recruiter reads exactly what was recorded
    (read-your-writes), and the sleep pass is where the tally goes durable.
    Never raises."""
    try:
        who = _resolve(persona)
        flush(who)
        now = _now()
        total = 0.0
        dominant, best = "", 0.0
        for c, entry in _load(who).items():
            s = _decayed(float(entry.get("score", 0.0)), float(entry.get("last_ts", now)), now)
            total += s
            if s > best:
                dominant, best = c, s
        return total, dominant
    except Exception:
        return 0.0, ""


def consume(persona: str = "") -> None:
    """Reset the whole tally (pending AND durable) — one accumulation window pays
    for at most one recruitment."""
    try:
        who = _resolve(persona)
        _pending.pop(who, None)
        if _path(who).exists():
            _save(who, {})
    except Exception:
        pass
