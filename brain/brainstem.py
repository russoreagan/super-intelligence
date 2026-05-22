"""
Brainstem — autonomic functions. Pure code, no LLMs.
- Heartbeat / turn lifecycle
- LLM call budget enforcer (hard cap per turn)
- Articulation gate (emits response on quiescence or T_max timeout)
- Cost tracker
- Idle / sleep trigger
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)

MAX_LLM_CALLS = int(os.environ.get("BRAIN_MAX_LLM_CALLS_PER_TURN", "60"))
TURN_TIMEOUT = float(os.environ.get("BRAIN_TURN_TIMEOUT_SECONDS", "30"))
QUIESCENCE_WINDOW = 0.8  # seconds of no new drafts before articulating


@dataclass
class TurnState:
    turn_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    started_at: float = field(default_factory=time.time)
    llm_calls: int = 0
    drafts: list[dict] = field(default_factory=list)
    endorsed: list[dict] = field(default_factory=list)
    vetoed: set[str] = field(default_factory=set)
    last_draft_ts: float = field(default_factory=time.time)
    committed: bool = False
    response: str = ""

    def elapsed(self) -> float:
        return time.time() - self.started_at

    def timed_out(self) -> bool:
        return self.elapsed() > TURN_TIMEOUT

    def quiescent(self) -> bool:
        if not self.drafts:
            return False
        return (time.time() - self.last_draft_ts) > QUIESCENCE_WINDOW

    def budget_exhausted(self) -> bool:
        return self.llm_calls >= MAX_LLM_CALLS


class Brainstem:
    def __init__(self, bus, model_router) -> None:
        self._bus = bus
        self._router = model_router
        self._current_turn: TurnState | None = None
        self._session_cost_calls: int = 0
        self._on_emit: list[Callable[[str, TurnState], None]] = []

    def register_emit_handler(self, fn: Callable[[str, TurnState], None]) -> None:
        self._on_emit.append(fn)

    def begin_turn(self) -> TurnState:
        self._current_turn = TurnState()
        self._router.reset_turn_log()
        logger.debug("Turn %s started", self._current_turn.turn_id)
        return self._current_turn

    def end_turn(self) -> TurnState:
        t = self._current_turn
        t.llm_calls = len(self._router._call_log)
        self._session_cost_calls += t.llm_calls
        logger.debug(
            "Turn %s ended: %d LLM calls, %.2fs, response length %d",
            t.turn_id, t.llm_calls, t.elapsed(), len(t.response)
        )
        return t

    def check_budget(self) -> bool:
        """Returns True if we're still within budget."""
        if self._current_turn and self._current_turn.budget_exhausted():
            logger.warning(
                "Turn %s: per-turn LLM call limit reached (max=%d) — set BRAIN_MAX_LLM_CALLS_PER_TURN to allow more",
                self._current_turn.turn_id, MAX_LLM_CALLS,
            )
            return False
        return True

    def add_draft(self, draft_id: str, text: str, score: float) -> None:
        t = self._current_turn
        if t is None:
            return
        t.drafts.append({"id": draft_id, "text": text, "score": score})
        t.last_draft_ts = time.time()

    def endorse(self, draft_id: str) -> None:
        t = self._current_turn
        if t is None:
            return
        matching = [d for d in t.drafts if d["id"] == draft_id]
        if matching:
            t.endorsed.extend(matching)

    def veto(self, draft_id: str) -> None:
        if self._current_turn:
            self._current_turn.vetoed.add(draft_id)

    async def articulation_gate(self, turn: TurnState) -> str:
        """
        Poll until quiescence OR T_max. Then pick the best endorsed draft.
        If no endorsed drafts, pick the highest-scored draft.
        If no drafts at all, emit a fallback.
        """
        while not turn.committed:
            await asyncio.sleep(0.1)
            if turn.timed_out():
                logger.warning("Turn %s: hit max wait time — using best draft ready so far", turn.turn_id)
                break
            if turn.quiescent() and turn.endorsed:
                logger.debug("Turn %s: quiescent with endorsed drafts", turn.turn_id)
                break

        # Select best response
        if turn.endorsed:
            # Pick highest-scoring endorsed draft not vetoed
            candidates = [d for d in turn.endorsed if d["id"] not in turn.vetoed]
            if candidates:
                best = max(candidates, key=lambda d: d["score"])
                turn.response = best["text"]
                turn.committed = True
                return turn.response

        if turn.drafts:
            not_vetoed = [d for d in turn.drafts if d["id"] not in turn.vetoed]
            if not_vetoed:
                best = max(not_vetoed, key=lambda d: d["score"])
                turn.response = best["text"]
                turn.committed = True
                return turn.response

        turn.response = "..."  # silent fallback; shouldn't happen if T_max is set
        turn.committed = True

        for fn in self._on_emit:
            fn(turn.response, turn)

        return turn.response

    async def heartbeat(self) -> None:
        """Scheduled keepalive — logs health every 60s during a session."""
        while True:
            await asyncio.sleep(60)
            logger.info("Heartbeat: %d total LLM calls this session", self._session_cost_calls)
