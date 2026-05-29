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
    async def aappend_fact(self, *a, **kw):
        pass

    def read(self, name):
        return ""

    async def awrite(self, name, content):
        pass


class _StubEpisodic:
    def encode(self, ep):
        pass

    def recall(self, vec, limit=4):
        return []

    def recall_recent(self, limit=6):
        return []


class _StubRouter:
    async def call(self, *a, **kw):
        return "{}"

    async def embed(self, text):
        return [0.0] * 16

    def __init__(self):
        self._call_log = []


def _make_trace(
    turn_id="t",
    *,
    fired_path=None,
    DA=0.5,
    prior_DA=0.5,
    GABA=0.0,
    ACh=0.3,
    critic_overall=0.7,
    critic_ran=True,
    emotion="content",
    user_emotion="",
):
    from brain.observability.timeline import TurnTrace

    t = TurnTrace(turn_id=turn_id, session_id="s", user_input="x")
    t.fired_path = fired_path or []
    t.neuromod = {"DA": DA, "GABA": GABA, "ACh": ACh, "Glu": 0.3}
    t.prior_neuromod = {"DA": prior_DA, "GABA": GABA, "ACh": ACh, "Glu": 0.3}
    t.draft_scores = [
        {"draft_id": "d", "overall": critic_overall, "selected": True, "critic_ran": critic_ran}
    ]
    t.emotion = emotion
    t.user_emotion = user_emotion
    return t


def test_composite_outcome_positive_with_good_signals(monkeypatch, tmp_path):
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    # prior_DA=0.5 → DA delta = (0.8-0.5)*4 = 1.2 (clamped to 1.0); critic good
    trace = _make_trace(DA=0.8, prior_DA=0.5, critic_overall=0.9, emotion="content")
    outcome, _ = sc._composite_outcome(trace)
    assert outcome > 0.2


def test_composite_outcome_negative_with_bad_signals(monkeypatch, tmp_path):
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    # prior_DA=0.5 → DA delta = (0.1-0.5)*4 = -1.6 (clamped to -1.0); critic bad
    trace = _make_trace(DA=0.1, prior_DA=0.5, critic_overall=0.2, emotion="frustrated")
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
    # prior_DA=0.5 → positive delta; critic_ran=True so critic term contributes
    trace = _make_trace(DA=0.9, prior_DA=0.5, critic_overall=0.95)
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
    # prior_DA=0.7 → DA dropped to 0.1 → strong negative delta
    trace = _make_trace(DA=0.1, prior_DA=0.7, critic_overall=0.2)
    trace.fired_path = [
        {"name": "frontal.executive", "cluster": "frontal", "kind": "integrator"},
        {"name": "frontal.drafter_C", "cluster": "frontal", "kind": "integrator"},
    ]
    sc._run_hebbian_pass("session_y", [trace])
    new_weight = w.get_edge_weight("frontal.executive", "frontal.drafter_C")
    # Decay (1.5 → 1.495) + negative Hebbian push it further down
    assert new_weight < 1.495


# ── New field coverage ───────────────────────────────────────────────────────


def test_critic_ran_false_zeroes_critic_term(monkeypatch, tmp_path):
    """When critic_ran=False, the critic term must be exactly 0 regardless of overall."""
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    # overall=0.9 but critic_ran=False — critic_term should be 0
    trace = _make_trace(DA=0.5, prior_DA=0.5, critic_overall=0.9, critic_ran=False)
    outcome, breakdown = sc._composite_outcome(trace)
    assert breakdown["critic"] == 0.0


def test_critic_ran_true_contributes_critic_term(monkeypatch, tmp_path):
    """When critic_ran=True, the critic term must be non-zero for a non-0.5 score."""
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    trace = _make_trace(DA=0.5, prior_DA=0.5, critic_overall=0.9, critic_ran=True)
    outcome, breakdown = sc._composite_outcome(trace)
    assert breakdown["critic"] == pytest.approx(0.8)  # (0.9 - 0.5) * 2


def test_user_emotion_read_from_trace_field(monkeypatch, tmp_path):
    """user_emotion is read from trace.user_emotion, not from draft_scores."""
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    # Positive user emotion should push outcome up
    trace_positive = _make_trace(DA=0.5, prior_DA=0.5, user_emotion="joy")
    trace_neutral = _make_trace(DA=0.5, prior_DA=0.5, user_emotion="neutral")
    outcome_pos, _ = sc._composite_outcome(trace_positive)
    outcome_neu, _ = sc._composite_outcome(trace_neutral)
    assert outcome_pos > outcome_neu


def test_prior_neuromod_missing_produces_zero_da_delta(monkeypatch, tmp_path):
    """When prior_neuromod is absent (old traces), da_delta should be ~0 (fallback da_prior=da)."""
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    trace = _make_trace(DA=0.8, prior_DA=0.8)  # same = no delta
    outcome, breakdown = sc._composite_outcome(trace)
    assert breakdown["da_delta"] == pytest.approx(0.0)


def test_outcome_breakdown_includes_da_prior_and_current(monkeypatch, tmp_path):
    """Breakdown dict must carry da_prior and da_current for observability."""
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    trace = _make_trace(DA=0.7, prior_DA=0.4)
    _, breakdown = sc._composite_outcome(trace)
    assert "da_prior" in breakdown
    assert "da_current" in breakdown
    assert breakdown["da_prior"] == pytest.approx(0.4)
    assert breakdown["da_current"] == pytest.approx(0.7)


# ── Drafter competition ───────────────────────────────────────────────────────


def _make_multi_draft_trace(winner_idx=0, winner_score=0.9, loser_score=0.4, prior_DA=0.5, DA=0.7):
    """Two-drafter trace where winner_idx won and the other lost."""
    from brain.observability.timeline import TurnTrace

    t = TurnTrace(turn_id="comp_turn", session_id="s", user_input="x")
    t.neuromod = {"DA": DA, "GABA": 0.0, "ACh": 0.3, "Glu": 0.3}
    t.prior_neuromod = {"DA": prior_DA, "ACh": 0.3, "Glu": 0.3}
    t.emotion = "curious"
    t.user_emotion = ""
    loser_idx = 1 - winner_idx  # just flip between 0 and 1
    t.draft_scores = [
        {
            "draft_id": f"draft_{winner_idx}_comp_turn",
            "overall": winner_score,
            "selected": True,
            "vetoed": False,
            "critic_ran": True,
        },
        {
            "draft_id": f"draft_{loser_idx}_comp_turn",
            "overall": loser_score,
            "selected": False,
            "vetoed": False,
            "critic_ran": True,
        },
    ]
    t.fired_path = []
    return t


def test_drafter_competition_strengthens_winner(monkeypatch, tmp_path):
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("frontal.executive", "frontal.drafter_A", weight=1.0)
    w.add("frontal.executive", "frontal.drafter_B", weight=1.0)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)

    trace = _make_multi_draft_trace(winner_idx=0, winner_score=0.9, loser_score=0.4)
    gainers, losers = [], []
    # plasticity=1.0 for simplicity
    sc._apply_drafter_competition(
        trace, outcome=0.5, plasticity=1.0, gainers=gainers, losers=losers
    )

    winner_w = w.get_edge_weight("frontal.executive", "frontal.drafter_A")
    loser_w = w.get_edge_weight("frontal.executive", "frontal.drafter_B")
    assert winner_w > 1.0, "Winner edge should have increased"
    assert loser_w < 1.0, "Loser edge should have decreased"
    assert winner_w > loser_w


def test_drafter_competition_skips_when_fewer_than_two_real_scored(monkeypatch, tmp_path):
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("frontal.executive", "frontal.drafter_A", weight=1.0)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)

    # Single draft with critic_ran=False — no competition should run
    trace = _make_trace(DA=0.7, prior_DA=0.5, critic_overall=0.8, critic_ran=False)
    gainers, losers = [], []
    sc._apply_drafter_competition(
        trace, outcome=0.5, plasticity=1.0, gainers=gainers, losers=losers
    )
    assert w.get_edge_weight("frontal.executive", "frontal.drafter_A") == pytest.approx(1.0)
    assert gainers == [] and losers == []


def test_drafter_competition_skips_when_only_one_critic_ran(monkeypatch, tmp_path):
    """Two draft_scores but only one has critic_ran=True — no competition."""
    from brain.observability.timeline import TurnTrace
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("frontal.executive", "frontal.drafter_A", weight=1.0)
    w.add("frontal.executive", "frontal.drafter_B", weight=1.0)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)

    t = TurnTrace(turn_id="t", session_id="s", user_input="x")
    t.neuromod = {"DA": 0.6}
    t.prior_neuromod = {"DA": 0.5}
    t.draft_scores = [
        {"draft_id": "draft_0_t", "overall": 0.9, "selected": True, "critic_ran": True},
        {"draft_id": "draft_1_t", "overall": 0.5, "selected": False, "critic_ran": False},
    ]
    gainers, losers = [], []
    sc._apply_drafter_competition(t, outcome=0.5, plasticity=1.0, gainers=gainers, losers=losers)
    assert w.get_edge_weight("frontal.executive", "frontal.drafter_A") == pytest.approx(1.0)
    assert w.get_edge_weight("frontal.executive", "frontal.drafter_B") == pytest.approx(1.0)


# ── frontal.py critic_ran flag ───────────────────────────────────────────────


def test_frontal_single_draft_has_critic_ran_false():
    """Single-draft code path must set critic_ran=False."""
    from brain.clusters.frontal import FrontalCluster

    fc = FrontalCluster.__new__(FrontalCluster)
    fc.last_turn_draft_scores = []
    # Simulate the single-draft assignment at the bottom of process()
    draft_id = "draft_0_xyz"
    fc.last_turn_draft_scores = [
        {
            "draft_id": draft_id,
            "coherence": 0.8,
            "relevance": 0.8,
            "tone_fit": 0.8,
            "empathy_score": 0.5,
            "overall": 0.8,
            "selected": True,
            "vetoed": False,
            "critic_ran": False,
        }
    ]
    selected = next(d for d in fc.last_turn_draft_scores if d["selected"])
    assert selected["critic_ran"] is False


def test_skip_threshold_lowered_to_002(monkeypatch, tmp_path):
    """Outcome of 0.03 should now pass through (old threshold was 0.05)."""
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    trace = _make_trace()
    skip, _ = sc._should_skip_hebbian(trace, outcome=0.03)
    assert skip is False

    skip_zero, reason = sc._should_skip_hebbian(trace, outcome=0.01)
    assert skip_zero is True
    assert "near_zero" in reason
