"""
B8 — route open threads into live work + close-the-loop-on-use.
B9 — learned routing weights + load-aware surfacing budget.
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
    dmn._router = MagicMock()
    dmn._router.embed = AsyncMock(return_value=None)
    hip = MagicMock()
    hip.encode_conclusion = AsyncMock()
    hip._schema = MagicMock()
    hip._schema.upsert_section = AsyncMock()
    dmn._hippocampus = hip
    dmn._bus = MagicMock()
    dmn._bus.neuromod.snapshot = MagicMock(return_value={"ACh": 0.3})
    dmn._session_id = "test"
    dmn._open_threads = []
    dmn._recent_conclusions = deque(maxlen=5)
    dmn._routing_weights = {}
    dmn._last_routed_ids = []
    dmn._user_msg_lens = deque(maxlen=6)
    dmn._user_topics = deque(maxlen=6)
    dmn._last_projects = ""
    return dmn


# ── B8 routing ────────────────────────────────────────────────────────────────


def test_routes_thread_matching_activity_by_bears_on():
    dmn = _make_dmn()
    dmn._open_threads, t = ot.open_thread(
        [], "is emotional gating cheaper?", bears_on=["efficiency-question"], bearing="affects-measurement"
    )
    dmn._open_threads, other = ot.open_thread(
        dmn._open_threads, "unrelated musing about bees", bears_on=["colony"]
    )
    routed = dmn.route_threads_for_turn(
        "let's talk about the efficiency-question and token cost", budget=2
    )
    assert t.id in [x.id for x in routed]
    assert other.id not in [x.id for x in routed]


def test_no_route_when_nothing_matches():
    dmn = _make_dmn()
    dmn._open_threads, _ = ot.open_thread([], "bee colony coordination", bears_on=["colony"])
    routed = dmn.route_threads_for_turn("how's the weather today", budget=2)
    assert routed == []


def test_budget_zero_holds_everything():
    dmn = _make_dmn()
    dmn._open_threads, _ = ot.open_thread([], "efficiency idea", bears_on=["efficiency"])
    assert dmn.route_threads_for_turn("efficiency efficiency", budget=0) == []


def test_routing_bounded_to_budget():
    dmn = _make_dmn()
    for i in range(4):
        dmn._open_threads, _ = ot.open_thread(
            dmn._open_threads, f"efficiency idea {i}", bears_on=["efficiency"]
        )
    routed = dmn.route_threads_for_turn("efficiency efficiency efficiency", budget=2)
    assert len(routed) <= 2


@pytest.mark.asyncio
async def test_close_loop_resolves_used_thread():
    dmn = _make_dmn()
    dmn._open_threads, t = ot.open_thread(
        [], "emotional gating reduces token cost", bears_on=["efficiency"], bearing="affects-measurement"
    )
    routed = [t]
    # Response clearly engages the thread.
    events = await dmn.note_threads_used(
        routed, "Right — emotional gating reduces token cost by cutting redundant context."
    )
    await asyncio.sleep(0.05)
    assert any(e["action"] == "resolved_by_use" for e in events)
    assert ot.find(dmn._open_threads, t.id) is None
    dmn._hippocampus.encode_conclusion.assert_awaited()
    assert dmn._hippocampus.encode_conclusion.await_args.kwargs["source"] == "landed"


@pytest.mark.asyncio
async def test_close_loop_keeps_ignored_thread():
    dmn = _make_dmn()
    dmn._open_threads, t = ot.open_thread(
        [], "emotional gating reduces token cost", bears_on=["efficiency"], bearing="affects-measurement"
    )
    events = await dmn.note_threads_used([t], "Let's talk about something totally different.")
    assert events == []
    assert ot.find(dmn._open_threads, t.id) is not None  # still open


# ── B9 learned weights + load gate ──────────────────────────────────────────────


def test_routing_weight_reinforce_and_clamp():
    dmn = _make_dmn()
    # Used → weight rises; ignored → falls; both stay clamped.
    for _ in range(50):
        dmn._reinforce_routing("affects-measurement", used=True)
    assert dmn._routing_weight("affects-measurement") <= DefaultModeNetwork._ROUTE_W_CEIL
    assert dmn._routing_weight("affects-measurement") > 1.0
    for _ in range(100):
        dmn._reinforce_routing("affects-measurement", used=False)
    assert dmn._routing_weight("affects-measurement") >= DefaultModeNetwork._ROUTE_W_FLOOR


def test_load_budget_holds_under_focus_and_user_shift():
    f = DefaultModeNetwork._routing_budget_from
    # Calm, baseline → full budget.
    assert f(focus_ach=0.3, verbosity_trend=0.0, topic_jump_rate=0.0) == 2
    # AI in deep focus → back off one.
    assert f(focus_ach=0.7, verbosity_trend=0.0, topic_jump_rate=0.0) == 1
    # User turned terser AND is jumping topics, plus focus → hold everything.
    assert f(focus_ach=0.7, verbosity_trend=-0.5, topic_jump_rate=0.8) == 0


def test_user_load_signals_detect_terseness_shift():
    dmn = _make_dmn()
    # Long, on-topic messages, then short scattered ones → negative verbosity trend.
    for _ in range(2):
        dmn.observe_user_turn({"topic_summary": "design"}, "a fairly long and detailed message here about design")
    for _ in range(2):
        dmn.observe_user_turn({"topic_summary": "x"}, "k")
    verbosity_trend, _ = dmn._user_load_signals()
    assert verbosity_trend < 0
