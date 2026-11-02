"""The persona's running expectation of its own critic scores.

Self-graded reward used to pay on an absolute bar: every reply the critic scored
above 0.7 earned DA (draft quality), every craft score above the gate earned more
(aesthetic), and every overall above self_standard_gate fired intrinsic pride. A
persona that writes well paid itself on almost every turn: prod 2026-11-01, about
+0.09 DA a turn in mundane small talk and "proud" on 12 of farrah's 31 turns.

Reward prediction error is the fix. The persona keeps an expectation of its own
scores (an EMA per persona and metric), and a self-graded payout scales with how
far a reply beat that expectation. Doing what it always does well is expected
and pays almost nothing; a standout reply still pays in full. The expectation
moves once per turn whatever the number of readers (appraise is idempotent per
turn_id), and it is process-local: after a restart it starts again from
self_standard_prior. self_reward_relative=0 restores the absolute bars.
"""

from __future__ import annotations

import threading

from brain.settings import settings

_LOCK = threading.Lock()
# (persona, metric) → expectation after the last observed turn
_EXPECTED: dict[tuple[str, str], float] = {}
# (persona, metric) → (turn_id, expectation before that turn)
_LAST_TURN: dict[tuple[str, str], tuple[str, float]] = {}


def enabled() -> bool:
    return bool(settings.get("self_reward_relative", 1))


def _persona_key(persona: str | None) -> str:
    if persona:
        return persona
    try:
        from brain.persona_key import active_or_home_persona, persona_slug

        return persona_slug(active_or_home_persona()) or ""
    except Exception:
        return ""


def appraise(metric: str, score: float, turn_id: str = "", persona: str | None = None) -> float:
    """Return the expectation as it stood before this turn, and fold ``score`` in.

    Every reader of the same turn (draft reward, pride, craft) gets the same
    pre-turn expectation, and the turn moves it once. An empty turn_id updates
    on every call."""
    key = (_persona_key(persona), metric)
    s = max(0.0, min(1.0, float(score)))
    with _LOCK:
        seen = _LAST_TURN.get(key)
        if turn_id and seen is not None and seen[0] == turn_id:
            return seen[1]
        expected = _EXPECTED.get(key, float(settings.get("self_standard_prior", 0.8)))
        alpha = float(settings.get("self_standard_alpha", 0.1))
        _EXPECTED[key] = expected + alpha * (s - expected)
        if turn_id:
            _LAST_TURN[key] = (turn_id, expected)
        return expected


def surprise(score: float, expected: float) -> float:
    """How far ``score`` beat ``expected``, as a share of the room above it (0..1)."""
    room = 1.0 - expected
    if room <= 1e-6:
        return 0.0
    return max(0.0, min(1.0, (float(score) - expected) / room))


def reset() -> None:
    """Forget every expectation (tests)."""
    with _LOCK:
        _EXPECTED.clear()
        _LAST_TURN.clear()
