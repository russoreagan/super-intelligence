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
    from brain.settings import settings
    from brain.sleep import SleepConsolidation

    # Legacy binary-skip behaviour applies only when graded_plasticity is OFF.
    monkeypatch.setitem(settings._data, "graded_plasticity", 0)
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
    # A PLAIN path edge. executive→drafter_* is owned by the drafter competition and
    # is deliberately excluded from path credit (see credit_purity), so using it here
    # would test nothing.
    w.add("temporal.understanding_integrator", "frontal.executive", weight=1.0)
    w.snapshot_baseline()
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    # prior_DA=0.5 → positive delta; critic_ran=True so critic term contributes
    trace = _make_trace(DA=0.9, prior_DA=0.5, critic_overall=0.95)
    trace.fired_path = [
        {"name": "temporal.understanding_integrator", "cluster": "temporal", "kind": "integrator"},
        {"name": "frontal.executive", "cluster": "frontal", "kind": "integrator"},
    ]
    sc._run_hebbian_pass("session_x", [trace])
    new_weight = w.get_edge_weight("temporal.understanding_integrator", "frontal.executive")
    # Decay (1% toward 1.0, weight was already 1.0 so no change) + positive Hebbian
    assert new_weight > 1.0


def test_hebbian_pass_decreases_on_negative_outcome(monkeypatch, tmp_path):
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("temporal.understanding_integrator", "frontal.executive", weight=1.5)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    # prior_DA=0.7 → DA dropped to 0.1 → strong negative delta
    trace = _make_trace(DA=0.1, prior_DA=0.7, critic_overall=0.2)
    trace.fired_path = [
        {"name": "temporal.understanding_integrator", "cluster": "temporal", "kind": "integrator"},
        {"name": "frontal.executive", "cluster": "frontal", "kind": "integrator"},
    ]
    sc._run_hebbian_pass("session_y", [trace])
    new_weight = w.get_edge_weight("temporal.understanding_integrator", "frontal.executive")
    # Decay (1.5 → 1.495) + negative Hebbian push it further down
    assert new_weight < 1.495


def test_frozen_wiring_freezes_weight_learning(monkeypatch, tmp_path):
    # BRAIN_WIRING_FROZEN is a true panic switch: a potent positive-outcome trace
    # must NOT move weights (nor decay them) nor rewrite wiring.json. Fails today
    # because the topology weight-learning pass runs regardless of FROZEN.
    from pathlib import Path

    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("temporal.understanding_integrator", "frontal.executive", weight=1.0)
    w.save()
    wiring_path = Path(tmp_path / "wiring.json")
    before_bytes = wiring_path.read_bytes()

    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    trace = _make_trace(DA=0.9, prior_DA=0.5, critic_overall=0.95)
    trace.fired_path = [
        {"name": "temporal.understanding_integrator", "cluster": "temporal", "kind": "integrator"},
        {"name": "frontal.executive", "cluster": "frontal", "kind": "integrator"},
    ]
    edge = ("temporal.understanding_integrator", "frontal.executive")

    # FROZEN: weights untouched and the file is byte-identical.
    monkeypatch.setenv("BRAIN_WIRING_FROZEN", "true")
    sc._run_hebbian_pass("s_frozen", [trace])
    assert w.get_edge_weight(*edge) == pytest.approx(1.0)
    assert wiring_path.read_bytes() == before_bytes

    # Control: same trace unfrozen DOES move the weight (proves the trace is potent).
    monkeypatch.setenv("BRAIN_WIRING_FROZEN", "false")
    sc._run_hebbian_pass("s_live", [trace])
    assert w.get_edge_weight(*edge) > 1.0


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


# ── Phase 1: graded plasticity (correctness fix) ──────────────────────────────


def _sc(monkeypatch, tmp_path):
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    return SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)


def test_turn_plasticity_identity_when_flag_off(monkeypatch, tmp_path):
    """graded_plasticity off → _turn_plasticity is exactly 1.0 (legacy preserved)."""
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "graded_plasticity", 0)
    sc = _sc(monkeypatch, tmp_path)
    trace = _make_trace(DA=0.95, prior_DA=0.1, ACh=0.9, emotion="happy")
    assert sc._hebbian._turn_plasticity(trace) == 1.0


def test_turn_plasticity_arousal_direction(monkeypatch, tmp_path):
    """High-arousal turn learns harder than a flat low-arousal turn."""
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "graded_plasticity", 1)
    sc = _sc(monkeypatch, tmp_path)
    aroused = _make_trace(DA=0.95, prior_DA=0.40, ACh=0.80, emotion="neutral")
    flat = _make_trace(DA=0.30, prior_DA=0.30, ACh=0.05, emotion="neutral")
    p_aroused = sc._hebbian._turn_plasticity(aroused)
    p_flat = sc._hebbian._turn_plasticity(flat)
    assert p_aroused > 1.0 > p_flat


def test_turn_plasticity_inverted_u_high_stress_dampens(monkeypatch, tmp_path):
    """Extreme stress (above the knee) dampens plasticity vs. moderate stress."""
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "graded_plasticity", 1)
    sc = _sc(monkeypatch, tmp_path)
    moderate = _make_trace(DA=0.5, prior_DA=0.5, ACh=0.3, GABA=0.30, emotion="neutral")
    extreme = _make_trace(DA=0.5, prior_DA=0.5, ACh=0.3, GABA=0.95, emotion="neutral")
    assert sc._hebbian._turn_plasticity(extreme) < sc._hebbian._turn_plasticity(moderate)


def test_turn_plasticity_intense_aversive_still_learns(monkeypatch, tmp_path):
    """Emotionally intense aversive turns imprint hard (not zeroed) below the knee —
    the fear-learning case the old binary skip got wrong."""
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "graded_plasticity", 1)
    sc = _sc(monkeypatch, tmp_path)
    # high arousal + intense negative emotion, stress below the knee
    intense_neg = _make_trace(DA=0.7, prior_DA=0.3, ACh=0.7, GABA=0.4, emotion="anger")
    assert sc._hebbian._turn_plasticity(intense_neg) > 1.0


def test_turn_plasticity_bounds(monkeypatch, tmp_path):
    """Always clamped to [plasticity_turn_min, plasticity_turn_max]."""
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "graded_plasticity", 1)
    # Tighten the bounds so the clamp is exercised deterministically regardless
    # of how a given emotion label resolves to valence.
    monkeypatch.setitem(settings._data, "plasticity_turn_min", 0.50)
    monkeypatch.setitem(settings._data, "plasticity_turn_max", 1.05)
    sc = _sc(monkeypatch, tmp_path)
    huge = _make_trace(DA=1.0, prior_DA=0.0, ACh=1.0, emotion="happy")  # would exceed 1.05
    tiny = _make_trace(
        DA=0.5, prior_DA=0.5, ACh=0.0, GABA=1.0, emotion="neutral"
    )  # would fall below 0.5
    assert sc._hebbian._turn_plasticity(huge) == pytest.approx(1.05)
    assert sc._hebbian._turn_plasticity(tiny) == pytest.approx(0.50)


def test_graded_plasticity_disables_binary_defuse_skip(monkeypatch, tmp_path):
    """With graded_plasticity on, the all-or-nothing defuse_path skip no longer fires —
    high-stress turns are dampened (not skipped)."""
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "graded_plasticity", 1)
    sc = _sc(monkeypatch, tmp_path)
    trace = _make_trace(GABA=0.7)
    trace.draft_scores = [{"draft_id": "defuse", "overall": 0.9, "selected": True}]
    skip, reason = sc._should_skip_hebbian(trace, outcome=0.5)
    assert skip is False  # graded dampener handles it instead of skipping


def test_graded_plasticity_keeps_hard_skips(monkeypatch, tmp_path):
    """near_zero and dissociated_emotion remain hard skips even when graded is on."""
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "graded_plasticity", 1)
    sc = _sc(monkeypatch, tmp_path)
    near_zero = _make_trace()
    skip, reason = sc._should_skip_hebbian(near_zero, outcome=0.005)
    assert skip is True and "near_zero" in reason
    flat = _make_trace(emotion="flat")
    skip2, reason2 = sc._should_skip_hebbian(flat, outcome=0.5)
    assert skip2 is True and "dissociated" in reason2


# ── Eligibility trace observability ──────────────────────────────────────────


class _CaptureDecisions:
    """Stands in for the decisions singleton; keeps every record."""

    def __init__(self):
        self.records = []

    def log(self, decision, *, turn_id="", cluster="", **fields):
        rec = {"decision": decision, "turn_id": turn_id, "cluster": cluster, **fields}
        self.records.append(rec)
        return rec

    def of(self, decision):
        return [r for r in self.records if r["decision"] == decision]


def _elig_session(monkeypatch, tmp_path, lookback=2):
    """Two good-outcome turns on DIFFERENT paths → turn 2 pays eligibility credit
    back to turn 1's path. Returns (wiring, capture)."""
    from brain.settings import settings
    from brain.sleep import SleepConsolidation

    monkeypatch.setitem(settings._data, "eligibility_lookback", lookback)
    monkeypatch.setitem(settings._data, "eligibility_tau_turns", 2.0)

    w = _isolated_wiring(monkeypatch, tmp_path)
    # Two DIFFERENT plain path edges — eligibility skips when the past path equals the
    # current one, and executive→drafter_* is competition-owned (see credit_purity).
    w.add("temporal.understanding_integrator", "frontal.executive", weight=1.0)
    w.add("temporal.understanding_integrator", "hippocampus.recall", weight=1.0)

    cap = _CaptureDecisions()
    monkeypatch.setattr("brain.hebbian.decisions", cap)

    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    t1 = _make_trace("turn_1", DA=0.9, prior_DA=0.5, critic_overall=0.95)
    t1.fired_path = [
        {"name": "temporal.understanding_integrator", "cluster": "temporal", "kind": "integrator"},
        {"name": "frontal.executive", "cluster": "frontal", "kind": "integrator"},
    ]
    t2 = _make_trace("turn_2", DA=0.9, prior_DA=0.5, critic_overall=0.95)
    t2.fired_path = [
        {"name": "temporal.understanding_integrator", "cluster": "temporal", "kind": "integrator"},
        {"name": "hippocampus.recall", "cluster": "hippocampus", "kind": "integrator"},
    ]
    sc._run_hebbian_pass("session_elig", [t1, t2])
    return w, cap


def test_eligibility_credit_is_logged(monkeypatch, tmp_path):
    """The defect: eligibility updates moved weights but emitted no record at all."""
    _, cap = _elig_session(monkeypatch, tmp_path)
    elig = cap.of("hebbian_eligibility_applied")
    assert elig, "eligibility credit must emit a decision record"


def test_eligibility_record_is_distinguishable_from_direct_credit(monkeypatch, tmp_path):
    """A ledger reader must be able to tell delayed credit from this-turn credit,
    and see WHICH turn earned it, how old it was, and how much it was decayed."""
    _, cap = _elig_session(monkeypatch, tmp_path)
    rec = cap.of("hebbian_eligibility_applied")[0]
    # Distinct kind — every aggregate that filters hebbian_update_applied excludes it.
    assert rec["decision"] != "hebbian_update_applied"
    assert rec["turn_id"] == "turn_2"  # the turn whose outcome paid out
    assert rec["source_turn_id"] == "turn_1"  # the turn whose path earned it
    assert rec["age"] == 1
    assert rec["decay"] == pytest.approx(__import__("math").exp(-1 / 2.0), abs=1e-3)
    # …and it names the edge it actually moved.
    moved = {(e["src"], e["tgt"]) for e in rec["edges"]}
    assert ("temporal.understanding_integrator", "frontal.executive") in moved


def test_eligibility_edges_updated_reconciles_with_logged_records(monkeypatch, tmp_path):
    """edges_updated used to count eligibility updates that no record explained.
    The eligibility share is now broken out and equals the sum over the records."""
    _, cap = _elig_session(monkeypatch, tmp_path)
    summary = cap.of("session_plasticity_summary")[0]
    logged = sum(int(r["edges_updated"]) for r in cap.of("hebbian_eligibility_applied"))
    assert summary["eligibility_edges_updated"] == logged
    assert logged > 0
    # …and the eligibility share is part of, not on top of, the headline total.
    assert summary["eligibility_edges_updated"] <= summary["edges_updated"]


def test_eligibility_credit_reaches_top_gainers(monkeypatch, tmp_path):
    """Weight moved by delayed credit must show up in the session summary's
    gainers, not vanish from it."""
    _, cap = _elig_session(monkeypatch, tmp_path)
    summary = cap.of("session_plasticity_summary")[0]
    gained = {g["edge"] for g in summary["top_gainers"]}
    assert "temporal.understanding_integrator→frontal.executive" in gained


def test_eligibility_lookback_zero_logs_nothing(monkeypatch, tmp_path):
    """Lookback 0 disables the trace — and must stay silent, not log empties."""
    _, cap = _elig_session(monkeypatch, tmp_path, lookback=0)
    assert cap.of("hebbian_eligibility_applied") == []
    assert cap.of("session_plasticity_summary")[0]["eligibility_edges_updated"] == 0


def test_eligibility_records_route_to_ledger_and_edge_query(monkeypatch, tmp_path):
    """The new kind is ledger-routed, and the ledger's edge filter matches on the
    aggregate's `edges` list (it has no top-level src/tgt)."""
    from brain.observability import learning_ledger

    assert "hebbian_eligibility_applied" in learning_ledger.LEDGER_TYPES

    path = tmp_path / "led.jsonl"
    rec = {
        "decision": "hebbian_eligibility_applied",
        "session_id": "s1",
        "edges": [{"src": "a", "tgt": "b", "delta": 0.02}],
    }
    path.write_text(__import__("json").dumps(rec) + "\n", encoding="utf-8")
    hits = learning_ledger.read(edge="a→b", path=path)
    assert len(hits) == 1
    assert learning_ledger.read(edge="x→y", path=path) == []


def test_wiring_view_edge_records_flatten_eligibility(monkeypatch, tmp_path):
    """The edge-drift explanations must include delayed credit, flattened to the
    same per-edge shape the UI renders, still tagged as eligibility."""
    from brain.observability.learning_reader import _edge_records

    recs = [
        {"decision": "hebbian_update_applied", "src": "a", "tgt": "b", "delta": 0.05},
        {
            "decision": "hebbian_eligibility_applied",
            "turn_id": "t2",
            "source_turn_id": "t1",
            "age": 2,
            "decay": 0.37,
            "outcome": 0.6,
            "edges": [
                {"src": "a", "tgt": "b", "from_weight": 1.0, "to_weight": 1.02, "delta": 0.02},
                {"src": "c", "tgt": "d", "from_weight": 1.0, "to_weight": 1.02, "delta": 0.02},
            ],
        },
    ]
    out = _edge_records(recs, "a", "b")
    assert len(out) == 2  # direct + the a→b half of the aggregate only
    assert out[0]["decision"] == "hebbian_update_applied"
    assert out[1]["decision"] == "hebbian_eligibility_applied"
    assert out[1]["delta"] == 0.02
    assert out[1]["age"] == 2 and out[1]["source_turn_id"] == "t1"
    # c→d must not leak into a→b's explanations.
    assert all(r.get("tgt") == "b" for r in out)


def test_sleep_evidence_does_not_double_count_eligibility(monkeypatch, tmp_path):
    """by_edge groups stay direct-credit-only (their mean outcome describes those
    turns); eligibility gets its own entry instead of inflating theirs."""
    import json as _json

    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)

    led = tmp_path / "ledger.jsonl"
    rows = [
        {
            "decision": "hebbian_update_applied",
            "session_id": "s9",
            "src": "a",
            "tgt": "b",
            "from_weight": 1.0,
            "to_weight": 1.05,
            "delta": 0.05,
            "outcome": 0.5,
            "turn_id": "t1",
        },
        {
            "decision": "hebbian_eligibility_applied",
            "session_id": "s9",
            "turn_id": "t2",
            "source_turn_id": "t1",
            "age": 1,
            "decay": 0.61,
            "outcome": 0.5,
            "edges_updated": 1,
            "edges": [{"src": "a", "tgt": "b", "delta": 0.03}],
        },
    ]
    led.write_text("\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "brain.observability.learning_ledger.ledger_path", lambda persona="": led
    )

    ev = sc._learning_evidence("s9", "")
    routing = [e for e in ev if "hebbian_update_applied" in e["decision_types"]]
    assert len(routing) == 1
    # The direct group reports ONLY its own 0.05 — not 0.08.
    assert routing[0]["edges"][0]["delta"] == pytest.approx(0.05)
    assert routing[0]["metrics"]["n_updates"] == 1
    # …and the delayed credit is present, separately.
    delayed = [e for e in ev if "hebbian_eligibility_applied" in e["decision_types"]]
    assert len(delayed) == 1
    assert delayed[0]["metrics"]["net_delta"] == pytest.approx(0.03)


# ── Per-persona attribution (mixed-persona trace buffers) ───────────────────


def test_hebbian_pass_credits_each_traces_own_persona(monkeypatch, tmp_path):
    """One consolidation over a mixed-persona buffer: each trace's update lands
    on ITS persona's graph, regardless of what's bound when the pass runs (the
    /consolidate route binds the session persona; the idle loop binds home —
    neither may sweep the other persona's turns onto its own wiring)."""
    from brain.second_brain.store import bind_persona
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    edge = ("temporal.understanding_integrator", "frontal.executive")
    for p in ("persona_home", "persona_analyst"):
        with bind_persona(p):
            w.add(*edge, weight=1.0)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    fired = [
        {"name": "temporal.understanding_integrator", "cluster": "temporal", "kind": "integrator"},
        {"name": "frontal.executive", "cluster": "frontal", "kind": "integrator"},
    ]
    home_trace = _make_trace("t_home", DA=0.9, prior_DA=0.5, critic_overall=0.95)
    home_trace.fired_path = fired
    home_trace.persona_name = "persona_home"
    analyst_trace = _make_trace("t_analyst", DA=0.1, prior_DA=0.7, critic_overall=0.2)
    analyst_trace.fired_path = fired
    analyst_trace.persona_name = "persona_analyst"

    with bind_persona("persona_home"):  # trigger bound to home, buffer is mixed
        sc._run_hebbian_pass("session_mix", [home_trace, analyst_trace])

    with bind_persona("persona_home"):
        w_home = w.get_edge_weight(*edge)
    with bind_persona("persona_analyst"):
        w_analyst = w.get_edge_weight(*edge)
    assert w_home > 1.0  # home's positive outcome credited to home
    assert w_analyst < 1.0  # analyst's negative outcome credited to analyst


def test_hebbian_pass_unstamped_traces_use_ambient_binding(monkeypatch, tmp_path):
    """Traces without a persona stamp keep the old behavior: whatever the
    consolidation trigger bound (or the boot persona when nothing is)."""
    from brain.second_brain.store import bind_persona
    from brain.sleep import SleepConsolidation

    w = _isolated_wiring(monkeypatch, tmp_path)
    edge = ("temporal.understanding_integrator", "frontal.executive")
    with bind_persona("persona_bound"):
        w.add(*edge, weight=1.0)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    trace = _make_trace("t_anon", DA=0.9, prior_DA=0.5, critic_overall=0.95)
    trace.fired_path = [
        {"name": "temporal.understanding_integrator", "cluster": "temporal", "kind": "integrator"},
        {"name": "frontal.executive", "cluster": "frontal", "kind": "integrator"},
    ]
    with bind_persona("persona_bound"):
        sc._run_hebbian_pass("session_anon", [trace])
    with bind_persona("persona_bound"):
        assert w.get_edge_weight(*edge) > 1.0


def test_file_backend_persona_save_does_not_clobber_boot_file(monkeypatch, tmp_path):
    """File backend: a runtime-bound persona saves to its own sibling
    personas/<slug>/wiring.json, never over the boot persona's file."""
    from brain.second_brain.store import bind_persona

    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("a", "b", weight=1.0)
    w.save()
    boot_bytes = (tmp_path / "wiring.json").read_text()

    with bind_persona("persona_q"):
        w.add("a", "b", weight=1.0)
        w.hebbian_update(["a", "b"], 0.5)
        w.save()

    q_file = tmp_path / "personas" / "persona_q" / "wiring.json"
    assert q_file.exists()
    assert (tmp_path / "wiring.json").read_text() == boot_bytes
    assert any(e["src"] == "a" and e["w"] > 1.0 for e in __import__("json").loads(q_file.read_text()))


# ── Session-length-invariant decay ───────────────────────────────────────────
#
# Reinforcement accrues PER TURN but decay ran once per SESSION at a hardcoded
# 0.01, so the equilibrium w_eq = 1 + n_turns·gain/rate depended on how long the
# session happened to be: 1.15 for a 1-turn session against 3.92 (clamped at
# weight_max) for a 20-turn one. Same brain, same settings, 26x spread.


def test_batch_decay_scales_linearly_with_turns():
    """LINEAR (n·r), not compounded. The batch adds the SUM of its turns' deltas,
    so equilibrium is 1 + ΣG/E; for that to equal the per-turn 1 + ḡ/r at every n,
    E must be exactly n·r. Compounding is sublinear and leaves long batches
    settling ~10% high (measured 1.490/1.509/1.548 at n = 1/5/20)."""
    from brain.hebbian import HebbianUpdater

    r = 0.03
    assert HebbianUpdater._batch_decay(r, 1) == pytest.approx(r)
    assert HebbianUpdater._batch_decay(r, 5) == pytest.approx(5 * r)
    assert HebbianUpdater._batch_decay(r, 20) == pytest.approx(20 * r)
    # and it is strictly above the compounded form, which is the ~10% gap
    assert HebbianUpdater._batch_decay(r, 20) > 1 - (1 - r) ** 20


def test_batch_decay_capped_for_large_backlogs():
    """The idle loop can consolidate a large backlog in one pass. Uncapped, a
    linear rate exceeds 1.0 and would overshoot rest, inverting every edge's
    deviation instead of relaxing it."""
    from brain.hebbian import HebbianUpdater
    from brain.settings import settings

    assert 0.03 * 500 > 1.0  # what it would be without the cap
    cap = float(settings.get("decay_batch_max", 0.90))
    assert HebbianUpdater._batch_decay(0.03, 500) == pytest.approx(cap)
    assert HebbianUpdater._batch_decay(0.03, 500) <= 1.0


def test_equilibrium_is_session_length_invariant(monkeypatch, tmp_path):
    """The property that actually matters, driven through the real pass: an edge
    settles at the same weight whether its turns arrive one per batch or twenty.

    The synthetic outcome is deliberately mild (ō ~= 0.16, matching production
    traces) — a strong outcome saturates every configuration at weight_max and
    makes the comparison vacuous."""
    from brain.settings import settings
    from brain.sleep import SleepConsolidation

    monkeypatch.setitem(settings._data, "decay_toward_rest_rate_per_turn", 0.01)
    monkeypatch.setitem(settings._data, "fragment_wiring", 0)

    # Each config approaches its equilibrium with a time constant of 1/(n·r)
    # BATCHES, so a fixed batch count would compare a converged run against an
    # unconverged one (at 300 batches n=1 sits at 3τ ≈ 95%, reading 1.4747 against
    # a true 1.4992). Give every config the same number of time constants instead.
    def _settle(n_turns, tag, time_constants=15):
        rate = float(settings.get("decay_toward_rest_rate_per_turn"))
        batches = int(time_constants / (n_turns * rate))
        w = _isolated_wiring(monkeypatch, tmp_path / tag)
        w.add("temporal.understanding_integrator", "frontal.executive", weight=1.0)
        sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
        for _ in range(batches):
            traces = []
            for i in range(n_turns):
                t = _make_trace(f"t{i}", DA=0.55, prior_DA=0.50, critic_overall=0.60)
                t.fired_path = [
                    {
                        "name": "temporal.understanding_integrator",
                        "cluster": "temporal",
                        "kind": "integrator",
                    },
                    {"name": "frontal.executive", "cluster": "frontal", "kind": "integrator"},
                ]
                traces.append(t)
            sc._run_hebbian_pass("s", traces)
        return w.get_edge_weight("temporal.understanding_integrator", "frontal.executive")

    settled = {n: _settle(n, f"n{n}") for n in (1, 5, 20)}
    assert max(settled.values()) < 2.99, f"saturated at the cap, comparison is vacuous: {settled}"
    spread = max(settled.values()) - min(settled.values())
    assert spread < 1e-6, f"equilibrium still depends on session length: {settled}"


def test_decay_rate_setting_is_actually_read(monkeypatch, tmp_path):
    """Regression guard on the original defect: hebbian.py hardcoded rate=0.01 at
    the call site, so the setting was never read on the production path and the
    Learning-Rate dial's stability half controlled nothing. Changing it must
    change the outcome."""
    from brain.settings import settings
    from brain.sleep import SleepConsolidation

    def _settle(rate, tag):
        w = _isolated_wiring(monkeypatch, tmp_path / tag)
        w.add("temporal.understanding_integrator", "frontal.executive", weight=2.0)
        monkeypatch.setitem(settings._data, "decay_toward_rest_rate_per_turn", rate)
        sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
        # Empty fired_path → the turn is skipped for reinforcement, so decay is
        # observed in isolation (it runs before the trace loop either way).
        t = _make_trace("t1")
        t.fired_path = []
        sc._run_hebbian_pass("s", [t])
        return w.get_edge_weight("temporal.understanding_integrator", "frontal.executive")

    slow = _settle(0.01, "slow")
    fast = _settle(0.30, "fast")
    assert slow == pytest.approx(2.0 - 0.01)  # 2.0*0.99 + 1.0*0.01
    assert fast < slow, "a higher per-turn decay rate must pull further toward rest"


def test_fragment_forget_also_compounds(monkeypatch, tmp_path):
    """Fragment attachments had the identical per-session defect, masked only by
    their 10x gain. They must compound over the batch too."""
    from brain.settings import settings
    from brain.sleep import SleepConsolidation

    def _settle(n_turns, tag):
        w = _isolated_wiring(monkeypatch, tmp_path / tag)
        w.add("fragment.skill_x", "frontal.drafter_A", weight=2.0)
        monkeypatch.setitem(settings._data, "fragment_forget_per_turn", 0.05)
        monkeypatch.setitem(settings._data, "fragment_wiring", 1)
        sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
        traces = []
        for i in range(n_turns):
            t = _make_trace(f"t{i}")
            t.fired_path = []
            traces.append(t)
        sc._run_hebbian_pass("s", traces)
        return w.get_edge_weight("fragment.skill_x", "frontal.drafter_A")

    one = _settle(1, "one")
    ten = _settle(10, "ten")
    assert ten < one, "a 10-turn batch must forget more than a 1-turn batch"
    # linear scaling: E = min(cap, 10 * 0.05) = 0.5 → 2.0*(1-0.5) + 1.0*0.5
    assert ten == pytest.approx(2.0 * 0.5 + 1.0 * 0.5, abs=1e-6)


# ── Credit purity ────────────────────────────────────────────────────────────
#
# frontal.executive→drafter_X used to collect BOTH the contrastive competition
# credit (winner-contingent, ~0.0012) and ordinary path credit (~0.024, twenty
# times larger) — and path credit goes to whichever drafter fired FIRST, not to
# whichever won. Observed in second_brain/wiring_history: executive→drafter_A
# reached 1.0088 while drafter_B sat at 0.9999, and A is simply first-fired.


def _purity_trace(turn_id="p1"):
    """drafter_C WINS the competition, but drafter_A fires FIRST on the path."""
    from brain.observability.timeline import TurnTrace

    t = TurnTrace(turn_id=turn_id, session_id="s", user_input="x")
    t.neuromod = {"DA": 0.9, "GABA": 0.0, "ACh": 0.3, "Glu": 0.3}
    t.prior_neuromod = {"DA": 0.5, "ACh": 0.3, "Glu": 0.3}
    t.emotion = "curious"
    t.user_emotion = ""
    t.draft_scores = [
        {"draft_id": f"draft_0_{turn_id}", "overall": 0.40, "selected": False, "critic_ran": True},
        {"draft_id": f"draft_2_{turn_id}", "overall": 0.90, "selected": True, "critic_ran": True},
    ]
    # A fires first — under unfiltered path credit this alone made A the winner.
    t.fired_path = [
        {"name": "frontal.executive", "cluster": "frontal", "kind": "integrator"},
        {"name": "frontal.drafter_A", "cluster": "frontal", "kind": "integrator"},
        {"name": "frontal.drafter_C", "cluster": "frontal", "kind": "integrator"},
    ]
    return t


def _run_purity(monkeypatch, tmp_path, tag, purity, lookback=0):
    from brain.settings import settings
    from brain.sleep import SleepConsolidation

    monkeypatch.setitem(settings._data, "credit_purity", purity)
    monkeypatch.setitem(settings._data, "eligibility_lookback", lookback)
    w = _isolated_wiring(monkeypatch, tmp_path / tag)
    for label in ("A", "C"):
        w.add("frontal.executive", f"frontal.drafter_{label}", weight=1.0)
    sc = SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)
    traces = [_purity_trace("p1")]
    if lookback:
        # a second turn on a different path, so eligibility replays p1's path
        t2 = _purity_trace("p2")
        t2.fired_path = [
            {"name": "frontal.executive", "cluster": "frontal", "kind": "integrator"},
            {"name": "frontal.drafter_C", "cluster": "frontal", "kind": "integrator"},
        ]
        traces.append(t2)
    sc._run_hebbian_pass(f"s_{tag}", traces)
    return (
        w.get_edge_weight("frontal.executive", "frontal.drafter_A"),
        w.get_edge_weight("frontal.executive", "frontal.drafter_C"),
    )


def test_credit_purity_lets_the_winner_beat_the_first_fired(monkeypatch, tmp_path):
    """The regression this exists for: the drafter that WON must end heavier than
    the drafter that merely fired first."""
    a, c = _run_purity(monkeypatch, tmp_path, "on", purity=1)
    assert c > a, f"winner C ({c}) must beat first-fired A ({a})"


def test_credit_purity_off_reproduces_the_ordering_artifact(monkeypatch, tmp_path):
    """Control: with the flag off, first-fired A wins despite losing the
    competition. This is the pre-change behaviour, pinned so the fix can't be
    quietly reverted."""
    a, c = _run_purity(monkeypatch, tmp_path, "off", purity=0)
    assert a > c, f"unfiltered path credit should let first-fired A ({a}) beat winner C ({c})"


def test_credit_purity_also_filters_the_eligibility_replay(monkeypatch, tmp_path):
    """Filtering only the main pass would let every past-path replay re-inject the
    ordering artifact — the easiest way to ship this half-done."""
    a, c = _run_purity(monkeypatch, tmp_path, "elig", purity=1, lookback=2)
    assert c > a, f"winner C ({c}) must beat first-fired A ({a}) with eligibility on too"


def test_competition_owned_covers_reserve_drafters():
    """Recruited reserve drafters (Tier 2) are competitors too — the owned set must
    grow with node_reserve_pool or a recruited node silently collects path credit."""
    from brain.hebbian import _competition_owned

    owned = _competition_owned(3)
    for label in "ABCDEFGH":
        assert ("frontal.executive", f"frontal.drafter_{label}") in owned
    assert ("frontal.executive", "frontal.drafter_I") not in owned
    # judges and approach cells too
    assert ("frontal.drafter_A", "frontal.critic") in owned
    assert ("temporal.understanding_integrator", "frontal.approach_A") in owned
    # …but a plain path edge is NOT owned
    assert ("temporal.understanding_integrator", "frontal.executive") not in owned
