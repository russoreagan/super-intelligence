"""
Tests for the corrected LearningMonitor metrics:
- predictor_accuracy excludes correct=None from the denominator (was the source
  of the misleading 0.158 figure).
- gate_hit_rate is computed only from shadow-validated gated skips.
- predictor_match_frac aggregates partial-position match.
- prediction_match_frac helper behaves for tuple and string labels.
"""

from __future__ import annotations

from brain.observability.timeline import TurnTrace
from brain.predictor import prediction_match_frac
from eval.learning_monitor import LearningMonitor


def _trace(outcomes, **kw):
    t = TurnTrace(turn_id="t1", session_id="s1", user_input="hi")
    t.predictor_outcomes = outcomes
    for k, v in kw.items():
        setattr(t, k, v)
    return t


# ── prediction_match_frac helper ─────────────────────────────────────────────


def test_match_frac_tuple_partial():
    assert prediction_match_frac(("a", "b", "c"), ("a", "x", "c")) == 2 / 3


def test_match_frac_tuple_exact():
    assert prediction_match_frac(("a", "b"), ("a", "b")) == 1.0


def test_match_frac_string():
    assert prediction_match_frac("q", "q") == 1.0
    assert prediction_match_frac("q", "chitchat") == 0.0


def test_match_frac_none():
    assert prediction_match_frac(None, ("a",)) == 0.0
    assert prediction_match_frac(("a",), None) == 0.0


# ── predictor_accuracy denominator ───────────────────────────────────────────


def test_accuracy_excludes_none_from_denominator():
    # 1 correct, 1 wrong, 3 gated/no-prediction (correct=None).
    # Old buggy formula: 1/5 = 0.2. Correct formula: 1/2 = 0.5.
    outcomes = [
        {"correct": True, "integrator_woken": True},
        {"correct": False, "integrator_woken": True},
        {"correct": None, "integrator_woken": False},
        {"correct": None, "integrator_woken": False},
        {"correct": None, "integrator_woken": True},
    ]
    m = LearningMonitor().record_turn(_trace(outcomes))
    assert m["predictor_accuracy"] == 0.5
    assert m["gate_count"] == 2
    assert m["run_count"] == 3


def test_accuracy_none_when_no_validated_outcomes():
    outcomes = [{"correct": None, "integrator_woken": False}]
    m = LearningMonitor().record_turn(_trace(outcomes))
    assert m["predictor_accuracy"] is None


# ── gate_hit_rate (shadow only) ──────────────────────────────────────────────


def test_gate_hit_rate_from_shadow_only():
    outcomes = [
        {"correct": True, "shadow": True, "integrator_woken": False},
        {"correct": False, "shadow": True, "integrator_woken": False},
        {"correct": True, "integrator_woken": True},  # ran, not shadow — ignored
    ]
    m = LearningMonitor().record_turn(_trace(outcomes))
    assert m["gate_hit_rate"] == 0.5


def test_gate_hit_rate_none_without_shadow():
    outcomes = [{"correct": True, "integrator_woken": True}]
    m = LearningMonitor().record_turn(_trace(outcomes))
    assert m["gate_hit_rate"] is None


# ── predictor_match_frac aggregation ─────────────────────────────────────────


def test_match_frac_aggregation():
    outcomes = [
        {"correct": False, "match_frac": 0.667, "integrator_woken": True},
        {"correct": True, "match_frac": 1.0, "integrator_woken": True},
    ]
    m = LearningMonitor().record_turn(_trace(outcomes))
    assert abs(m["predictor_match_frac"] - (0.667 + 1.0) / 2) < 1e-9
