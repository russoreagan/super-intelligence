"""Curated public affect/mood views for the engine API.

Shared by both transports — the SSE path (``server.py``) and the WebSocket path
(``ws.py``) — so the *chemistry-not-exposed* contract lives in exactly one place and
can't drift between them. Only the mood OUTPUT (emotion + the user's read emotion)
crosses the partner boundary; the neuromod/hormonal layer and every internal field
(enrollment, appraisal, …) are withheld so the affect model can't be reverse-
engineered from the API.

Deliberately depends on neither transport (ws.py historically avoided importing
server.py), and reaches the heavier PNS-backed ``affect_view`` via a lazy import so
this module stays cheap to load.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["affect_view", "mood_from_affect", "turn_stats"]


def turn_stats(affect: dict | None) -> dict:
    """The turn's cost telemetry for the partner's own latency accounting —
    {elapsed_s, llm_calls} when the turn stamped them (session_turn does, next to
    turn_id), else {}. Copied onto the top level of POST /turns, the SSE `done`
    frame and the WS `done` frame by the same helper so the three transports can't
    disagree. Not affect state: never part of the curated affect/mood views."""
    affect = affect or {}
    out: dict = {}
    if isinstance(affect.get("elapsed_s"), int | float):
        out["elapsed_s"] = float(affect["elapsed_s"])
    if isinstance(affect.get("llm_calls"), int):
        out["llm_calls"] = int(affect["llm_calls"])
    return out


def affect_view(text: str, affect: dict | None) -> tuple[str, dict]:
    """Clean display text + structured affect block for a turn response. Lazy import
    keeps this module free of the PNS dependency at load; on any failure fall back to
    the raw text with an empty affect block so a turn never 500s over presentation."""
    try:
        from brain.api.audio import affect_view as _impl

        return _impl(text, affect)
    except Exception:  # noqa: BLE001 — presentation must never break a turn
        logger.warning("affect_view failed; returning raw text", exc_info=True)
        return text, {"base_tag": None, "segments": []}


def mood_from_affect(affect: dict | None) -> dict:
    """Curate the public mood view from the internal affect dict — the mood OUTPUT
    only (emotion + the user's read emotion). The hormonal/chemical layer and every
    internal field (neuromod, enrollment, appraisal, …) are withheld so the affect
    model can't be reverse-engineered from the API."""
    affect = affect or {}
    mood: dict = {"emotion": affect.get("emotion", "neutral")}
    if affect.get("user_emotion"):
        mood["user_emotion"] = affect["user_emotion"]
    return mood
