"""
Integration tests for the DMN open-threads ledger + memory-of-conclusions
(B1/B2/B6/B7). Drives _process_thought with crafted monologue metadata and
asserts ledger mutations, the confident/uncertain conclusion branch, the
advancing-thread dedup exemption, and the Langfuse outcome.

The LLM is never called — we hand _process_thought the parsed metadata directly.
"""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

import brain.open_threads as ot
from brain.dmn import DefaultModeNetwork
from brain.sequence_predictor import SequencePredictor


def _meta(**over) -> dict:
    base = {
        "angle": None,
        "spoken_form": None,
        "task_goal": None,
        "is_propose": False,
        "is_plan": False,
        "defer_text": None,
        "defer_urgency": "high",
        "defer_tags": [],
        "chem_delta": {},
        "open_thread": False,
        "advance_thread_id": "",
        "conclude_thread_id": "",
        "conclusion": "",
        "conclusion_confidence": "confident",
        "bears_on": [],
        "bearing": "",
    }
    base.update(over)
    return base


def _make_dmn():
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._seq_predictor = SequencePredictor()
    dmn._bus = MagicMock()
    dmn._bus.publish_dict = AsyncMock()
    dmn._bus.neuromod.snapshot = MagicMock(return_value={"DA": 0.5})
    dmn._bus.neuromod.add = MagicMock()
    dmn._bus.hormonal.add = MagicMock()
    dmn._router = MagicMock()
    dmn._router.embed = AsyncMock(return_value=None)

    # Fake hippocampus capturing encode calls + a no-op async schema store.
    hip = MagicMock()
    hip.encode_conclusion = AsyncMock()
    hip.encode_deferred_question = AsyncMock()
    schema = MagicMock()
    schema.read = MagicMock(return_value="")
    schema.upsert_section = AsyncMock()
    hip._schema = schema
    dmn._hippocampus = hip

    dmn._parietal = None
    dmn._obs = MagicMock()  # capture record_thought(outcome=...)
    dmn._running = True
    dmn._last_context = "ctx"
    dmn._thought_count = 0
    dmn._recent_thoughts = deque(maxlen=10)
    dmn._recent_embeddings = deque(maxlen=10)
    dmn._recent_angles = deque(maxlen=8)
    dmn._recent_frames = deque(maxlen=6)
    dmn._suppressed_count = 0
    dmn._session_id = "test"
    dmn._last_emotion = "neutral"
    dmn._session_thought_buf = []
    dmn._session_thought_limit = 200
    dmn._open_threads = []
    dmn._recent_conclusions = deque(maxlen=5)
    # Don't touch the real deferred_thoughts.md file.
    dmn._append_deferred_thought = MagicMock()
    return dmn


@pytest.mark.asyncio
async def test_open_thread_appends_to_ledger():
    dmn = _make_dmn()
    await dmn._process_thought(
        "Does emotional gating actually reduce token cost?",
        _meta(open_thread=True, angle="efficiency", bears_on=["efficiency-question"],
              bearing="affects-measurement"),
        "t1",
    )
    assert len(dmn._open_threads) == 1
    t = dmn._open_threads[0]
    assert t.summary.startswith("Does emotional gating")
    assert t.bears_on == ["efficiency-question"]
    # outcome recorded on the trace
    outcome = dmn._obs.record_thought.call_args.kwargs["outcome"]
    assert outcome["action"] == "opened_thread"
    assert outcome["thread_id"] == t.id


@pytest.mark.asyncio
async def test_advance_increments_and_is_exempt_from_cluster_gate():
    dmn = _make_dmn()
    # Saturate angles in one cluster so the cluster-saturation gate WOULD fire.
    for a in ("colony-bees", "colony-pheromones", "colony-recruitment"):
        dmn._recent_angles.append(a)
    # Open a thread to advance.
    dmn._open_threads, t = ot.open_thread([], "colony coordination idea", angle="colony-x")
    await dmn._process_thought(
        "Phase 2 concentration queues act like pheromone gradients.",
        _meta(advance_thread_id=t.id, angle="colony-phase"),
        "t2",
    )
    # Not suppressed despite cluster saturation, and the thread advanced.
    assert dmn._suppressed_count == 0
    assert dmn._open_threads[0].advances == 1
    outcome = dmn._obs.record_thought.call_args.kwargs["outcome"]
    assert outcome["action"] == "advanced_thread"


@pytest.mark.asyncio
async def test_confident_conclusion_encodes_and_retires():
    dmn = _make_dmn()
    dmn._open_threads, t = ot.open_thread([], "is gating cheaper?", bears_on=["efficiency"])
    await dmn._process_thought(
        "Settled: gating cuts redundant context, lowering tokens-per-response.",
        _meta(conclude_thread_id=t.id,
              conclusion="Emotional gating reduces tokens-per-useful-response.",
              conclusion_confidence="confident"),
        "t3",
    )
    await asyncio.sleep(0.05)  # let the create_task encode fire
    assert ot.find(dmn._open_threads, t.id) is None  # retired
    dmn._hippocampus.encode_conclusion.assert_awaited()
    kwargs = dmn._hippocampus.encode_conclusion.await_args.kwargs
    assert kwargs["source"] == "dmn"
    assert "efficiency" in kwargs["tags"]
    # _recent_conclusions entries are (ts, text) tuples for age-decay.
    assert "Emotional gating" in dmn._recent_conclusions[-1][1]
    outcome = dmn._obs.record_thought.call_args.kwargs["outcome"]
    assert outcome["action"] == "concluded"


@pytest.mark.asyncio
async def test_uncertain_conclusion_defers_and_stays_pending():
    dmn = _make_dmn()
    dmn._open_threads, t = ot.open_thread([], "is gating cheaper?", bears_on=["efficiency"])
    await dmn._process_thought(
        "I think gating helps but I'm not certain.",
        _meta(conclude_thread_id=t.id,
              conclusion="Gating probably reduces cost.",
              conclusion_confidence="uncertain"),
        "t4",
    )
    await asyncio.sleep(0.05)
    # Thread is NOT retired — it parks as pending_confirmation.
    parked = ot.find(dmn._open_threads, t.id)
    assert parked is not None
    assert parked.status == ot.STATUS_PENDING
    # Routed through the deferred-question pipeline, tagged with the thread id.
    dmn._append_deferred_thought.assert_called_once()
    dmn._hippocampus.encode_deferred_question.assert_awaited()
    dq_tags = dmn._hippocampus.encode_deferred_question.await_args.kwargs["tags"]
    assert "pending_conclusion" in dq_tags
    assert t.id in dq_tags
    # Not encoded as a known conclusion yet.
    dmn._hippocampus.encode_conclusion.assert_not_awaited()
    outcome = dmn._obs.record_thought.call_args.kwargs["outcome"]
    assert outcome["action"] == "deferred_conclusion"


@pytest.mark.asyncio
async def test_advance_cap_forces_conclusion():
    dmn = _make_dmn()
    dmn._open_threads, t = ot.open_thread([], "deepening idea", bears_on=["x"])
    # Pre-advance to one below the cap.
    for i in range(ot.THREAD_MAX_ADVANCES - 1):
        dmn._open_threads, t = ot.advance_thread(dmn._open_threads, t.id, f"step {i}")
    # One more advance hits the cap → auto-conclude.
    await dmn._process_thought(
        "Final synthesis of the idea.",
        _meta(advance_thread_id=t.id),
        "t5",
    )
    await asyncio.sleep(0.05)
    assert ot.find(dmn._open_threads, t.id) is None  # retired by cap
    dmn._hippocampus.encode_conclusion.assert_awaited()
    outcome = dmn._obs.record_thought.call_args.kwargs["outcome"]
    assert outcome["action"] == "concluded"
