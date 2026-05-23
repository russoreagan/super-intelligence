"""
Tests for DecisionLog: records flow to disk (eval JSONL) and UI emitter.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from brain.observability.decisions import DecisionLog
from eval.turn_logger import EvalLogger


def test_decision_log_writes_disk_record(tmp_path):
    log_path = tmp_path / "turns.jsonl"
    eval_logger = EvalLogger(log_path=log_path)
    dl = DecisionLog()
    # Pass a None emitter explicitly so the singleton-fallback doesn't fire
    dl.configure(eval_logger=eval_logger, emitter=_NullEmitter())

    dl.log("skip_executive_integrator", turn_id="t1", cluster="frontal",
           reason="test", cost_saved_est=0.001)

    content = log_path.read_text().strip().splitlines()
    assert len(content) == 1
    record = json.loads(content[0])
    assert record["type"] == "decision"
    assert record["decision"] == "skip_executive_integrator"
    assert record["turn_id"] == "t1"
    assert record["cluster"] == "frontal"
    assert record["reason"] == "test"
    assert "ts" in record


def test_decision_log_emits_to_ui_when_loop_running(tmp_path):
    """When a running asyncio loop exists, decisions are pushed to the emitter queue."""

    async def _run():
        log_path = tmp_path / "turns.jsonl"
        eval_logger = EvalLogger(log_path=log_path)
        emitter = _CaptureEmitter()
        dl = DecisionLog()
        dl.configure(eval_logger=eval_logger, emitter=emitter)

        dl.log("weighted_drafter_selection", turn_id="t2", cluster="frontal",
               picked=["B"], weights={"A": 1.0, "B": 1.3})
        # Allow the task created by log() to run
        await asyncio.sleep(0.01)

        assert len(emitter.events) == 1
        ev = emitter.events[0]
        assert ev["type"] == "decision"
        assert ev["picked"] == ["B"]

    asyncio.run(_run())


def test_decision_log_safe_without_emitter_or_eval_logger():
    """Calling log() before configure() must not raise."""
    dl = DecisionLog()
    record = dl.log("noop", turn_id="x")
    assert record["decision"] == "noop"


def test_decision_log_hebbian_fields_preserved(tmp_path):
    log_path = tmp_path / "turns.jsonl"
    eval_logger = EvalLogger(log_path=log_path)
    dl = DecisionLog()
    dl.configure(eval_logger=eval_logger, emitter=_NullEmitter())

    dl.log("hebbian_update_applied",
           src="frontal.executive", tgt="frontal.drafter_A",
           from_weight=1.0, to_weight=1.04, delta=0.04,
           outcome=0.8)
    record = json.loads(log_path.read_text().strip())
    assert record["src"] == "frontal.executive"
    assert record["tgt"] == "frontal.drafter_A"
    assert record["delta"] == 0.04


# ── Test helpers ────────────────────────────────────────────────────────────

class _NullEmitter:
    async def emit_event(self, event): pass


class _CaptureEmitter:
    def __init__(self): self.events: list = []
    async def emit_event(self, event): self.events.append(event)
