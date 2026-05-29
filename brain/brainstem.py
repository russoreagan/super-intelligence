"""
Brainstem — autonomic functions. Pure code, no LLMs.
- Heartbeat / turn lifecycle
- LLM call budget enforcer (hard cap per turn)
- Articulation gate (emits response on quiescence or T_max timeout)
- Cost tracker
- Idle / sleep trigger
- Background loop registry (supervised restart, crash tracking)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MAX_LLM_CALLS = int(os.environ.get("BRAIN_MAX_LLM_CALLS_PER_TURN", "60"))
TURN_TIMEOUT = float(os.environ.get("BRAIN_TURN_TIMEOUT_SECONDS", "30"))
QUIESCENCE_WINDOW = 0.8  # seconds of no new drafts before articulating

# Restart backoff: delay = min(MAX_BACKOFF, BASE ** crash_count) seconds
_BACKOFF_BASE = 2.0
_BACKOFF_MAX = 30.0


@dataclass
class LoopState:
    name: str
    restart_on_crash: bool
    started_at: float = field(default_factory=time.time)
    crash_count: int = 0
    last_crash_at: float | None = None
    last_error: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def status(self) -> str:
        if self.task is None:
            return "not_started"
        if self.task.done():
            return "crashed" if self.task.exception() else "done"
        return "running"

    def uptime_s(self) -> float:
        return time.time() - self.started_at


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
        self._loops: dict[str, LoopState] = {}

    def register_emit_handler(self, fn: Callable[[str, TurnState], None]) -> None:
        self._on_emit.append(fn)

    # ── Background loop registry ──────────────────────────────────────────────

    def register_loop(
        self,
        name: str,
        coro_fn: Callable[[], Coroutine[Any, Any, None]],
        restart_on_crash: bool = True,
    ) -> LoopState:
        """
        Register and immediately start a long-running coroutine under brainstem
        supervision. If it crashes (raises anything other than CancelledError),
        it is restarted after exponential backoff unless restart_on_crash=False.

        coro_fn must be a zero-argument callable that returns a fresh coroutine
        each time it is called (i.e. an async def function, not an awaitable).

        Returns the LoopState so callers can inspect or cancel later.
        """
        state = LoopState(name=name, restart_on_crash=restart_on_crash)
        self._loops[name] = state
        state.task = asyncio.create_task(self._supervise(state, coro_fn), name=f"bs:{name}")
        logger.debug("[Brainstem] Loop registered: %s", name)
        return state

    async def _supervise(
        self,
        state: LoopState,
        coro_fn: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Run coro_fn(), restarting on crash with exponential backoff."""
        while True:
            try:
                await coro_fn()
                # Coroutine returned normally — treat as done, don't restart.
                logger.debug("[Brainstem] Loop %r finished cleanly", state.name)
                return
            except asyncio.CancelledError:
                logger.debug("[Brainstem] Loop %r cancelled", state.name)
                raise
            except Exception as exc:
                state.crash_count += 1
                state.last_crash_at = time.time()
                state.last_error = str(exc)
                logger.error(
                    "[Brainstem] Loop %r crashed (crash #%d): %s",
                    state.name,
                    state.crash_count,
                    exc,
                    exc_info=True,
                )
                if not state.restart_on_crash:
                    return
                delay = min(_BACKOFF_MAX, _BACKOFF_BASE ** min(state.crash_count, 5))
                logger.info("[Brainstem] Restarting loop %r in %.1fs", state.name, delay)
                await asyncio.sleep(delay)

    def loop_status(self) -> dict[str, dict]:
        """Snapshot of all registered loops — for heartbeat and observability."""
        return {
            name: {
                "status": s.status,
                "uptime_s": round(s.uptime_s(), 1),
                "crash_count": s.crash_count,
                "last_error": s.last_error or None,
            }
            for name, s in self._loops.items()
        }

    def cancel_all_loops(self) -> None:
        """Cancel every registered loop. Call on shutdown."""
        for name, state in self._loops.items():
            if state.task and not state.task.done():
                state.task.cancel()
                logger.debug("[Brainstem] Cancelled loop %r", name)

    def begin_turn(self) -> TurnState:
        self._current_turn = TurnState()
        self._router.reset_turn_log()
        logger.debug("Turn %s started", self._current_turn.turn_id)
        return self._current_turn

    def end_turn(self) -> TurnState:
        t = self._current_turn
        # Exclude DMN-cluster calls — those tick continuously during/between
        # turns and shouldn't inflate this turn's budget or telemetry.
        t.llm_calls = self._router.turn_calls_excluding_background()
        self._session_cost_calls += t.llm_calls
        logger.debug(
            "Turn %s ended: %d LLM calls, %.2fs, response length %d",
            t.turn_id,
            t.llm_calls,
            t.elapsed(),
            len(t.response),
        )
        return t

    def check_budget(self) -> bool:
        """Returns True if we're still within budget."""
        if self._current_turn and self._current_turn.budget_exhausted():
            logger.warning(
                "Turn %s: per-turn LLM call limit reached (max=%d) — set BRAIN_MAX_LLM_CALLS_PER_TURN to allow more",
                self._current_turn.turn_id,
                MAX_LLM_CALLS,
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
                logger.warning(
                    "Turn %s: hit max wait time — using best draft ready so far", turn.turn_id
                )
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

    async def heartbeat_once(self, emitter=None) -> None:
        """One heartbeat tick — log health, pulse UI if emitter present."""
        logger.info("Heartbeat: %d total LLM calls this session", self._session_cost_calls)
        status = self.loop_status()
        crashed = {n: s for n, s in status.items() if s["status"] == "crashed"}
        if crashed:
            logger.warning(
                "[Brainstem] Crashed loops: %s", {n: s["last_error"] for n, s in crashed.items()}
            )
        else:
            logger.debug("[Brainstem] All %d loop(s) healthy", len(status))
        if emitter:
            await emitter.emit("brainstem", 0.2, "heartbeat", "hb")
            await asyncio.sleep(0.8)
            await emitter.emit("brainstem", 0.0, "done", "hb")
