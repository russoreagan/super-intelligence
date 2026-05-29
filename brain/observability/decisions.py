"""
DecisionLog — single unified channel for predict-and-surprise + Hebbian decisions.

Every decision is:
  1. Written to eval/turns.jsonl (or BRAIN_EVAL_LOG path) as a "decision" record.
  2. Emitted to the UI WebSocket via ActivationEmitter.emit_event().

Configure once at session boot:
    from brain.observability.decisions import decisions
    decisions.configure(eval_logger=eval_logger)

Then call from anywhere:
    decisions.log("skip_executive_integrator",
                  turn_id=turn_id, cluster="frontal",
                  reason="predictor confidence 0.82, surprise 0.18",
                  predicted={"response_type": "chitchat"},
                  emotional_context={"emotion": "content"})
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eval.turn_logger import EvalLogger

logger = logging.getLogger(__name__)


class DecisionLog:
    """Singleton; configure once at session boot."""

    def __init__(self) -> None:
        self._eval_logger: EvalLogger | None = None
        self._emitter = None

    def configure(self, eval_logger: EvalLogger | None = None, emitter: Any = None) -> None:
        self._eval_logger = eval_logger
        # If emitter not supplied, fall back to the module-level singleton
        if emitter is not None:
            self._emitter = emitter
        else:
            try:
                from brain.ui.emitter import emitter as _emitter

                self._emitter = _emitter
            except Exception:
                self._emitter = None

    def log(self, decision: str, *, turn_id: str = "", cluster: str = "", **fields: Any) -> dict:
        """Record a decision. Returns the record for inline use."""
        record = {
            "type": "decision",
            "decision": decision,
            "turn_id": turn_id,
            "cluster": cluster,
            "ts": time.time(),
            **fields,
        }
        # Disk: eval JSONL (synchronous)
        if self._eval_logger is not None:
            try:
                self._eval_logger._append(record)  # noqa: SLF001 — same record path
            except Exception as e:
                logger.debug("DecisionLog disk write failed: %s", e)
        # UI: emit via WebSocket (async — fire-and-forget if loop running)
        if self._emitter is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._emitter.emit_event(record))
            except RuntimeError:
                # No running loop — skip UI emit (e.g. during tests or sleep consolidation off-thread)
                pass
            except Exception as e:
                logger.debug("DecisionLog UI emit failed: %s", e)
        return record


# Module-level singleton — import and use directly
decisions = DecisionLog()
