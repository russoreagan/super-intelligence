"""Pure-function tests for brain/mandates.py validation helpers (no Supabase needed)."""

from __future__ import annotations

from brain.mandates import _valid_reward_weights


def test_valid_reward_weights_clips_and_drops_unknown_keys():
    result = _valid_reward_weights(
        {"correctness": 999, "connection": -5, "bogus": 1.0, "novelty": "not-a-number"}
    )
    assert result["correctness"] == 3.0
    assert result["connection"] == 0.1
    assert "bogus" not in result
    assert "novelty" not in result


def test_valid_reward_weights_none_is_empty():
    assert _valid_reward_weights(None) == {}


def test_valid_reward_weights_passes_through_in_range_values():
    result = _valid_reward_weights({"mastery": 1.5, "levity": 0.8})
    assert result == {"mastery": 1.5, "levity": 0.8}
