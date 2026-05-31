"""
B4 — rumination steers toward the open-threads ledger:
  - _current_seed prefers the least-advanced open thread
  - a rumination pass ADVANCES that thread
  - when the consecutive-rumination depth cap is hit, it CONCLUDES the thread
"""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

import brain.open_threads as ot
from brain.dmn import DefaultModeNetwork


def _make_dmn():
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._bus = MagicMock()
    dmn._bus.publish_dict = AsyncMock()
    dmn._bus.neuromod.snapshot = MagicMock(return_value={"DA": 0.5})
    dmn._bus.neuromod.add = MagicMock()
    dmn._bus.hormonal.add = MagicMock()
    dmn._router = MagicMock()
    dmn._router.embed = AsyncMock(return_value=None)
    hip = MagicMock()
    hip.encode_conclusion = AsyncMock()
    hip.encode_deferred_question = AsyncMock()
    hip._schema = MagicMock()
    hip._schema.upsert_section = AsyncMock()
    dmn._hippocampus = hip
    dmn._obs = None
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
    dmn._append_deferred_thought = MagicMock()
    dmn._log_rumination = MagicMock()
    dmn._ruminations_in_progress = 0
    dmn._consecutive_ruminations = 0
    dmn._last_rumination_seed = ""
    return dmn


def test_current_seed_prefers_least_advanced_open_thread():
    dmn = _make_dmn()
    dmn._recent_thoughts.append("a stray last thought")
    dmn._open_threads, t1 = ot.open_thread([], "thread one", now=1.0)
    dmn._open_threads, t2 = ot.open_thread(dmn._open_threads, "thread two", now=2.0)
    dmn._open_threads, t1 = ot.advance_thread(dmn._open_threads, t1.id, "x", now=3.0)
    # t2 is least-advanced (0 advances) → it's the seed.
    assert dmn._current_seed() == "thread two"


@pytest.mark.asyncio
async def test_rumination_advances_then_concludes_at_cap():
    dmn = _make_dmn()
    dmn._open_threads, t = ot.open_thread([], "deepening idea", bears_on=["x"])
    selector = MagicMock()
    selector.ruminate = AsyncMock(return_value=("a deeper synthesis", ["seed", "step"]))
    dmn._skill_selector = selector

    # Default cap is 2. First pass → advance. Second pass → concludes.
    await dmn._run_rumination("r1", {}, "logic", 0.8)
    assert dmn._open_threads and dmn._open_threads[0].advances == 1

    await dmn._run_rumination("r2", {}, "logic", 0.8)
    await asyncio.sleep(0.05)
    assert ot.find(dmn._open_threads, t.id) is None  # concluded + retired
    dmn._hippocampus.encode_conclusion.assert_awaited()
