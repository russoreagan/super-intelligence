"""
Tests for Hebbian learning: Wiring graph, sleep consolidation Hebbian pass,
weight clamping, plasticity modulator, decay, and skip rules.
"""
from __future__ import annotations

import pytest

from brain.wiring import WEIGHT_MAX, WEIGHT_MIN, Edge, Wiring
from brain.wiring_bootstrap import bootstrap

# ── Wiring core ─────────────────────────────────────────────────────────────

def _isolated_wiring(monkeypatch, tmp_path) -> Wiring:
    """Wiring instance whose JSON persistence is isolated to tmp_path."""
    monkeypatch.setenv("BRAIN_WIRING_PATH", str(tmp_path / "wiring.json"))
    monkeypatch.setenv("BRAIN_WIRING_HISTORY_DIR", str(tmp_path / "history"))
    import importlib

    import brain.wiring as w_mod
    importlib.reload(w_mod)
    return w_mod.Wiring()


def test_edge_effective_weight_polarity():
    e = Edge("a", "b", weight=1.5, polarity="excitatory")
    assert e.effective_weight() == 1.5
    e2 = Edge("a", "b", weight=1.5, polarity="inhibitory")
    assert e2.effective_weight() == -1.5


def test_wiring_add_idempotent(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("x", "y", weight=1.5)
    w.add("x", "y", weight=2.0)  # should be ignored — edge already exists
    assert w.get_edge_weight("x", "y") == 1.5


def test_hebbian_update_positive(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("a", "b", weight=1.0)
    w.add("b", "c", weight=1.0)
    updated = w.hebbian_update(["a", "b", "c"], delta=0.1)
    assert updated == 2
    assert w.get_edge_weight("a", "b") == pytest.approx(1.1)
    assert w.get_edge_weight("b", "c") == pytest.approx(1.1)


def test_hebbian_update_negative_decrements(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("a", "b", weight=1.0)
    w.hebbian_update(["a", "b"], delta=-0.2)
    assert w.get_edge_weight("a", "b") == pytest.approx(0.8)


def test_hebbian_update_clamps_to_bounds(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("a", "b", weight=2.95)
    w.hebbian_update(["a", "b"], delta=0.5)
    assert w.get_edge_weight("a", "b") == WEIGHT_MAX

    w.add("c", "d", weight=0.15)
    w.hebbian_update(["c", "d"], delta=-0.5)
    assert w.get_edge_weight("c", "d") == WEIGHT_MIN


def test_decay_toward_rest(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("a", "b", weight=2.0)
    w.add("c", "d", weight=0.5)
    w.decay_toward_rest(rest=1.0, rate=0.1)
    # New weight = old * 0.9 + 1.0 * 0.1
    assert w.get_edge_weight("a", "b") == pytest.approx(2.0 * 0.9 + 0.1)
    assert w.get_edge_weight("c", "d") == pytest.approx(0.5 * 0.9 + 0.1)


def test_session_baseline_and_deltas(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("a", "b", weight=1.0)
    w.snapshot_baseline()
    w.hebbian_update(["a", "b"], delta=0.05)
    deltas = w.session_deltas()
    assert len(deltas) == 1
    assert deltas[0]["delta"] == pytest.approx(0.05)


def test_save_and_load_roundtrip(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("a", "b", weight=1.7)
    w.save()
    w2 = _isolated_wiring(monkeypatch, tmp_path)  # reload from disk
    assert w2.get_edge_weight("a", "b") == pytest.approx(1.7)


def test_bootstrap_adds_expected_edges(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    # Spot check a few critical edges
    assert w.has("frontal.executive", "frontal.drafter_A")
    assert w.has("frontal.executive", "frontal.drafter_B")
    assert w.has("frontal.executive", "frontal.drafter_C")
    assert w.has("temporal.understanding_integrator", "frontal.executive")
    assert w.has("mem.recall", "hippocampus.cosine_recall")
    assert w.edge_count() > 15


# ── Sleep consolidation Hebbian pass ────────────────────────────────────────

class _StubSchema:
    async def aappend_fact(self, *a, **kw): pass
    def read(self, name): return ""
    async def awrite(self, name, content): pass


class _StubEpisodic:
    def encode(self, ep): pass
    def recall(self, vec, limit=4): return []
    def recall_recent(self, limit=6): return []


class _StubRouter:
    async def call(self, *a, **kw): return "{}"
    async def embed(self, text): return [0.0] * 16
    def __init__(self): self._call_log = []


def _make_trace(turn_id="t", *, fired_path=None, DA=0.5, GABA=0.0, ACh=0.3,
                critic_overall=0.7, emotion="content"):
    from brain.observability.timeline import TurnTrace
    t = TurnTrace(turn_id=turn_id, session_id="s", user_input="x")
    t.fired_path = fired_path or []
    t.neuromod = {"DA": DA, "GABA": GABA, "ACh": ACh, "Glu": 0.3}
    t.draft_scores = [{"draft_id": "d", "overall": critic_overall, "selected": True}]
    t.emotion = emotion
    return t


def test_composite_outcome_positive_with_good_signals(monkeypatch, tmp_path):
    from brain.sleep import SleepConsolidation
    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    trace = _make_trace(DA=0.8, critic_overall=0.9, emotion="content")
    outcome, _ = sc._composite_outcome(trace)
    assert outcome > 0.2


def test_composite_outcome_negative_with_bad_signals(monkeypatch, tmp_path):
    from brain.sleep import SleepConsolidation
    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    trace = _make_trace(DA=0.1, critic_overall=0.2, emotion="frustrated")
    outcome, _ = sc._composite_outcome(trace)
    assert outcome < -0.1


def test_plasticity_modulator_scales_with_DA(monkeypatch, tmp_path):
    from brain.sleep import SleepConsolidation
    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    happy = [_make_trace(DA=0.9, ACh=0.6) for _ in range(3)]
    flat = [_make_trace(DA=0.1, ACh=0.1) for _ in range(3)]
    assert sc._plasticity_modulator(happy) > sc._plasticity_modulator(flat)
    assert 0.3 <= sc._plasticity_modulator(flat) <= 1.2
    assert 0.3 <= sc._plasticity_modulator(happy) <= 1.2


def test_should_skip_hebbian_near_zero_outcome(monkeypatch, tmp_path):
    from brain.sleep import SleepConsolidation
    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    trace = _make_trace()
    skip, reason = sc._should_skip_hebbian(trace, outcome=0.01)
    assert skip is True
    assert "near_zero" in reason


def test_should_skip_hebbian_defuse_path(monkeypatch, tmp_path):
    from brain.sleep import SleepConsolidation
    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    trace = _make_trace(GABA=0.7)
    trace.draft_scores = [{"draft_id": "defuse", "overall": 0.9, "selected": True}]
    skip, reason = sc._should_skip_hebbian(trace, outcome=0.5)
    assert skip is True
    assert "defuse" in reason


def test_hebbian_pass_applies_updates_along_path(monkeypatch, tmp_path):
    from brain.sleep import SleepConsolidation
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("frontal.executive", "frontal.drafter_A", weight=1.0)
    w.snapshot_baseline()
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    trace = _make_trace(DA=0.9, critic_overall=0.95)
    trace.fired_path = [
        {"name": "frontal.executive", "cluster": "frontal", "kind": "integrator"},
        {"name": "frontal.drafter_A", "cluster": "frontal", "kind": "integrator"},
    ]
    sc._run_hebbian_pass("session_x", [trace])
    new_weight = w.get_edge_weight("frontal.executive", "frontal.drafter_A")
    # Decay (1% toward 1.0, weight was already 1.0 so no change) + positive Hebbian
    assert new_weight > 1.0


def test_hebbian_pass_decreases_on_negative_outcome(monkeypatch, tmp_path):
    from brain.sleep import SleepConsolidation
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("frontal.executive", "frontal.drafter_C", weight=1.5)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    trace = _make_trace(DA=0.1, critic_overall=0.2)
    trace.fired_path = [
        {"name": "frontal.executive", "cluster": "frontal", "kind": "integrator"},
        {"name": "frontal.drafter_C", "cluster": "frontal", "kind": "integrator"},
    ]
    sc._run_hebbian_pass("session_y", [trace])
    new_weight = w.get_edge_weight("frontal.executive", "frontal.drafter_C")
    # Decay would bring 1.5 → 1.495; negative Hebbian then pushes it lower
    assert new_weight < 1.5
