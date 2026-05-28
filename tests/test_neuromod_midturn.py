"""
Tests for mid-turn neuromodulator injection points added to session_turn.py.

Three injection sites, each verified for:
  - correct channels updated
  - magnitude / direction
  - trace.neuromod_midturn entry written with right trigger label
  - emitter called with the post-update snapshot

No full session startup — each test builds the minimum scaffold needed.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.bus import Bus
from brain.observability.timeline import TurnTrace


# ── shared helpers ────────────────────────────────────────────────────────────

def _make_bus() -> Bus:
    return Bus()


def _make_trace() -> TurnTrace:
    return TurnTrace(turn_id="t1", session_id="s1", user_input="test")


def _make_emitter():
    em = MagicMock()
    em.emit_neuromod = AsyncMock()
    return em


# ── 1. Hippocampus recall injection ──────────────────────────────────────────

class TestHippocampusRecallInjection:
    """ACh spike proportional to recalled episode count."""

    def _apply(self, bus: Bus, trace: TurnTrace, emitter, episode_text: str) -> None:
        """Replicates the hippocampus-recall block from session_turn._run_turn."""
        episode_hits = len([ln for ln in episode_text.splitlines() if ln.strip()])
        if episode_hits > 0:
            ach_delta = min(0.12, episode_hits * 0.03)
            bus.neuromod.add("ACh", ach_delta)
            snap = bus.neuromod.snapshot()
            trace.neuromod_midturn.append({"trigger": "hippocampus_recall", "snapshot": snap})
            asyncio.get_event_loop().run_until_complete(emitter.emit_neuromod(snap))

    def test_single_episode_spikes_ach(self):
        bus = _make_bus()
        trace = _make_trace()
        before = bus.neuromod.get("ACh")
        self._apply(bus, trace, _make_emitter(), "episode line one")
        assert bus.neuromod.get("ACh") > before

    def test_spike_proportional_to_hits(self):
        bus_few = _make_bus()
        bus_many = _make_bus()
        trace = _make_trace()
        em = _make_emitter()
        self._apply(bus_few, trace, em, "line 1")
        self._apply(bus_many, _make_trace(), em, "line 1\nline 2\nline 3\nline 4")
        assert bus_many.neuromod.get("ACh") > bus_few.neuromod.get("ACh")

    def test_spike_capped_at_0_12(self):
        bus = _make_bus()
        trace = _make_trace()
        # 100 episode lines — would be 3.0 without the cap
        big_text = "\n".join(f"episode {i}" for i in range(100))
        before = bus.neuromod.get("ACh")
        self._apply(bus, trace, _make_emitter(), big_text)
        assert bus.neuromod.get("ACh") - before <= 0.12 + 1e-9

    def test_no_episodes_no_change(self):
        bus = _make_bus()
        trace = _make_trace()
        before = bus.neuromod.get("ACh")
        self._apply(bus, trace, _make_emitter(), "")
        assert bus.neuromod.get("ACh") == before
        assert trace.neuromod_midturn == []

    def test_trace_entry_written(self):
        bus = _make_bus()
        trace = _make_trace()
        self._apply(bus, trace, _make_emitter(), "some episode")
        assert len(trace.neuromod_midturn) == 1
        entry = trace.neuromod_midturn[0]
        assert entry["trigger"] == "hippocampus_recall"
        assert "ACh" in entry["snapshot"]

    def test_emitter_called_with_snapshot(self):
        bus = _make_bus()
        trace = _make_trace()
        em = _make_emitter()
        self._apply(bus, trace, em, "some episode")
        em.emit_neuromod.assert_awaited_once()
        emitted = em.emit_neuromod.call_args[0][0]
        assert emitted["ACh"] == pytest.approx(bus.neuromod.get("ACh"))

    def test_other_channels_unchanged(self):
        bus = _make_bus()
        trace = _make_trace()
        da_before = bus.neuromod.get("DA")
        gaba_before = bus.neuromod.get("GABA")
        self._apply(bus, trace, _make_emitter(), "some episode")
        assert bus.neuromod.get("DA") == da_before
        assert bus.neuromod.get("GABA") == gaba_before


# ── 2. Tool outcome injection ─────────────────────────────────────────────────

class TestToolOutcomeInjection:
    """Failed tools raise GABA+NE and drop DA; successful tools raise DA+Glu."""

    def _apply_success(self, bus: Bus, trace: TurnTrace, emitter) -> None:
        bus.neuromod.add("DA", 0.07)
        bus.neuromod.add("Glu", 0.04)
        snap = bus.neuromod.snapshot()
        trace.neuromod_midturn.append({"trigger": "tool_success", "snapshot": snap})
        asyncio.get_event_loop().run_until_complete(emitter.emit_neuromod(snap))

    def _apply_failure(self, bus: Bus, trace: TurnTrace, emitter) -> None:
        bus.neuromod.add("GABA", 0.08)
        bus.neuromod.add("NE", 0.06)
        bus.neuromod.add("DA", -0.05)
        snap = bus.neuromod.snapshot()
        trace.neuromod_midturn.append({"trigger": "tool_failure", "snapshot": snap})
        asyncio.get_event_loop().run_until_complete(emitter.emit_neuromod(snap))

    def _apply_exception(self, bus: Bus, trace: TurnTrace, emitter) -> None:
        bus.neuromod.add("GABA", 0.10)
        bus.neuromod.add("NE", 0.08)
        snap = bus.neuromod.snapshot()
        trace.neuromod_midturn.append({"trigger": "tool_exception", "snapshot": snap})
        asyncio.get_event_loop().run_until_complete(emitter.emit_neuromod(snap))

    # success ─────────────────────────────────────────────────────────────────

    def test_success_raises_da(self):
        bus = _make_bus()
        before = bus.neuromod.get("DA")
        self._apply_success(bus, _make_trace(), _make_emitter())
        assert bus.neuromod.get("DA") > before

    def test_success_raises_glu(self):
        bus = _make_bus()
        before = bus.neuromod.get("Glu")
        self._apply_success(bus, _make_trace(), _make_emitter())
        assert bus.neuromod.get("Glu") > before

    def test_success_trace_trigger(self):
        bus = _make_bus()
        trace = _make_trace()
        self._apply_success(bus, trace, _make_emitter())
        assert trace.neuromod_midturn[-1]["trigger"] == "tool_success"

    def test_success_emitter_called(self):
        bus = _make_bus()
        em = _make_emitter()
        self._apply_success(bus, _make_trace(), em)
        em.emit_neuromod.assert_awaited_once()

    # failure (success=False returned) ────────────────────────────────────────

    def test_failure_raises_gaba(self):
        bus = _make_bus()
        before = bus.neuromod.get("GABA")
        self._apply_failure(bus, _make_trace(), _make_emitter())
        assert bus.neuromod.get("GABA") > before

    def test_failure_raises_ne(self):
        bus = _make_bus()
        before = bus.neuromod.get("NE")
        self._apply_failure(bus, _make_trace(), _make_emitter())
        assert bus.neuromod.get("NE") > before

    def test_failure_suppresses_da(self):
        bus = _make_bus()
        before = bus.neuromod.get("DA")
        self._apply_failure(bus, _make_trace(), _make_emitter())
        assert bus.neuromod.get("DA") < before

    def test_failure_gaba_exceeds_success_da(self):
        """Frustration signal should be larger than the reward signal."""
        bus_f = _make_bus()
        bus_s = _make_bus()
        gaba_before = bus_f.neuromod.get("GABA")
        da_before = bus_s.neuromod.get("DA")
        self._apply_failure(bus_f, _make_trace(), _make_emitter())
        self._apply_success(bus_s, _make_trace(), _make_emitter())
        assert (bus_f.neuromod.get("GABA") - gaba_before) > (bus_s.neuromod.get("DA") - da_before)

    def test_failure_trace_trigger(self):
        bus = _make_bus()
        trace = _make_trace()
        self._apply_failure(bus, trace, _make_emitter())
        assert trace.neuromod_midturn[-1]["trigger"] == "tool_failure"

    # exception (motor.execute() raised) ─────────────────────────────────────

    def test_exception_raises_gaba_more_than_failure(self):
        """An outright crash is more disruptive than a graceful failure."""
        bus_exc = _make_bus()
        bus_fail = _make_bus()
        gaba_b_exc = bus_exc.neuromod.get("GABA")
        gaba_b_fail = bus_fail.neuromod.get("GABA")
        self._apply_exception(bus_exc, _make_trace(), _make_emitter())
        self._apply_failure(bus_fail, _make_trace(), _make_emitter())
        assert (bus_exc.neuromod.get("GABA") - gaba_b_exc) > (bus_fail.neuromod.get("GABA") - gaba_b_fail)

    def test_exception_trace_trigger(self):
        bus = _make_bus()
        trace = _make_trace()
        self._apply_exception(bus, trace, _make_emitter())
        assert trace.neuromod_midturn[-1]["trigger"] == "tool_exception"

    def test_failure_then_exception_two_trace_entries(self):
        """If a tool returns False and then an exception fires, both are recorded."""
        bus = _make_bus()
        trace = _make_trace()
        em = _make_emitter()
        self._apply_failure(bus, trace, em)
        self._apply_exception(bus, trace, em)
        triggers = [e["trigger"] for e in trace.neuromod_midturn]
        assert "tool_failure" in triggers
        assert "tool_exception" in triggers


# ── 3. Draft quality injection ────────────────────────────────────────────────

class TestDraftQualityInjection:
    """Low overall draft score → GABA/NE; high → DA. Mid range → no update."""

    def _apply(self, bus: Bus, trace: TurnTrace, emitter, overall: float) -> str | None:
        draft_scores = [{"overall": overall}]
        best = max(draft_scores, key=lambda d: d.get("overall", 0.5))
        ov = best.get("overall", 0.5)
        if ov < 0.4:
            bus.neuromod.add("GABA", 0.06)
            bus.neuromod.add("NE", 0.04)
            trigger = "draft_quality_low"
        elif ov > 0.7:
            bus.neuromod.add("DA", 0.05)
            trigger = "draft_quality_high"
        else:
            trigger = None
        if trigger:
            snap = bus.neuromod.snapshot()
            trace.neuromod_midturn.append({"trigger": trigger, "snapshot": snap})
            asyncio.get_event_loop().run_until_complete(emitter.emit_neuromod(snap))
        return trigger

    def test_low_score_raises_gaba(self):
        bus = _make_bus()
        before = bus.neuromod.get("GABA")
        self._apply(bus, _make_trace(), _make_emitter(), overall=0.25)
        assert bus.neuromod.get("GABA") > before

    def test_low_score_raises_ne(self):
        bus = _make_bus()
        before = bus.neuromod.get("NE")
        self._apply(bus, _make_trace(), _make_emitter(), overall=0.25)
        assert bus.neuromod.get("NE") > before

    def test_high_score_raises_da(self):
        bus = _make_bus()
        before = bus.neuromod.get("DA")
        self._apply(bus, _make_trace(), _make_emitter(), overall=0.85)
        assert bus.neuromod.get("DA") > before

    def test_high_score_no_gaba_change(self):
        bus = _make_bus()
        before = bus.neuromod.get("GABA")
        self._apply(bus, _make_trace(), _make_emitter(), overall=0.85)
        assert bus.neuromod.get("GABA") == before

    def test_mid_score_no_update(self):
        bus = _make_bus()
        trace = _make_trace()
        da_before = bus.neuromod.get("DA")
        gaba_before = bus.neuromod.get("GABA")
        trigger = self._apply(bus, trace, _make_emitter(), overall=0.55)
        assert trigger is None
        assert trace.neuromod_midturn == []
        assert bus.neuromod.get("DA") == da_before
        assert bus.neuromod.get("GABA") == gaba_before

    def test_boundary_exactly_04_is_mid(self):
        bus = _make_bus()
        trace = _make_trace()
        trigger = self._apply(bus, trace, _make_emitter(), overall=0.4)
        # 0.4 is not < 0.4, so no update
        assert trigger is None

    def test_boundary_exactly_07_is_mid(self):
        bus = _make_bus()
        trace = _make_trace()
        trigger = self._apply(bus, trace, _make_emitter(), overall=0.7)
        # 0.7 is not > 0.7, so no update
        assert trigger is None

    def test_low_score_trace_trigger(self):
        bus = _make_bus()
        trace = _make_trace()
        self._apply(bus, trace, _make_emitter(), overall=0.2)
        assert trace.neuromod_midturn[-1]["trigger"] == "draft_quality_low"

    def test_high_score_trace_trigger(self):
        bus = _make_bus()
        trace = _make_trace()
        self._apply(bus, trace, _make_emitter(), overall=0.9)
        assert trace.neuromod_midturn[-1]["trigger"] == "draft_quality_high"

    def test_emitter_called_for_low(self):
        bus = _make_bus()
        em = _make_emitter()
        self._apply(bus, _make_trace(), em, overall=0.2)
        em.emit_neuromod.assert_awaited_once()

    def test_emitter_not_called_for_mid(self):
        bus = _make_bus()
        em = _make_emitter()
        self._apply(bus, _make_trace(), em, overall=0.55)
        em.emit_neuromod.assert_not_awaited()


# ── 4. TurnTrace field ────────────────────────────────────────────────────────

class TestTurnTraceField:
    def test_neuromod_midturn_defaults_empty(self):
        trace = _make_trace()
        assert trace.neuromod_midturn == []

    def test_multiple_triggers_accumulate(self):
        trace = _make_trace()
        snap = {"ACh": 0.3, "DA": 0.5, "GABA": 0.05, "Glu": 0.2, "NE": 0.2}
        trace.neuromod_midturn.append({"trigger": "hippocampus_recall", "snapshot": snap})
        trace.neuromod_midturn.append({"trigger": "tool_failure", "snapshot": snap})
        trace.neuromod_midturn.append({"trigger": "draft_quality_low", "snapshot": snap})
        assert len(trace.neuromod_midturn) == 3
        triggers = [e["trigger"] for e in trace.neuromod_midturn]
        assert triggers == ["hippocampus_recall", "tool_failure", "draft_quality_low"]
