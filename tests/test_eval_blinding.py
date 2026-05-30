"""Unit tests for the blinded quality-judge un-blinding logic (eval/scorer.py).

The judge scores anonymized Response A / Response B; _unblind_quality maps the result
back to brain/baseline terms while preserving the keys report.py consumes.
"""

from __future__ import annotations

from eval.scorer import _unblind_quality


def _parsed(a_overall, b_overall, pref, a_extra=None, b_extra=None):
    a = {"overall": a_overall, "personality": 0.8, "self_awareness": None, "memory_use": 0.6}
    b = {"overall": b_overall, "personality": 0.4, "self_awareness": None, "memory_use": None}
    a.update(a_extra or {})
    b.update(b_extra or {})
    return {"a": a, "b": b, "preference": pref, "reasoning": "because"}


def test_brain_is_a_maps_correctly():
    out = _unblind_quality(_parsed(0.9, 0.5, "A"), brain_is_a=True)
    assert out["brain_overall"] == 0.9
    assert out["baseline_overall"] == 0.5
    assert out["delta"] == 0.4
    # Brain-intrinsic dims come from the brain side (A).
    assert out["personality_consistency"] == 0.8
    assert out["memory_utilization"] == 0.6
    assert out["judge_preference"] == "brain"  # preference "A" == brain
    assert out["brain_position"] == "A"
    assert out["judge_blinded"] is True


def test_brain_is_b_maps_correctly():
    out = _unblind_quality(_parsed(0.5, 0.9, "A"), brain_is_a=False)
    # Brain is B here.
    assert out["brain_overall"] == 0.9
    assert out["baseline_overall"] == 0.5
    assert out["delta"] == 0.4
    # Preference "A" now points at the baseline, not the brain.
    assert out["judge_preference"] == "baseline"
    assert out["personality_consistency"] == 0.4  # B side
    assert out["brain_position"] == "B"


def test_tie_preference():
    out = _unblind_quality(_parsed(0.7, 0.7, "tie"), brain_is_a=True)
    assert out["judge_preference"] == "tie"
    assert out["delta"] == 0.0


def test_null_dims_become_none_not_05():
    out = _unblind_quality(_parsed(0.8, 0.6, "A"), brain_is_a=True)
    # self_awareness was null in the sample → None, NOT 0.5 (so it's excluded from aggregates).
    assert out["self_awareness"] is None


def test_missing_overall_omits_comparative_keys():
    parsed = _parsed(None, 0.6, "B")
    out = _unblind_quality(parsed, brain_is_a=True)
    # Brain overall absent → no fabricated comparative numbers.
    assert "brain_overall" not in out
    assert "delta" not in out
    # Intrinsic dims still reported.
    assert out["personality_consistency"] == 0.8
