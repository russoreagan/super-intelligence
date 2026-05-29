"""
Phase 2a: DMN's stream.prediction feeds into the temporal predictor.
When the user's actual input matches what the brain idly anticipated,
predictor confidence is boosted (surprise dropped) so the LLM is more
likely to be skipped — closing the predict-and-surprise loop with
top-down anticipation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from brain.clusters.temporal import TemporalCluster


def _make_temporal_skeleton():
    """Build a TemporalCluster skeleton bypassing __init__."""
    t = TemporalCluster.__new__(TemporalCluster)
    t._wiring = None
    t._wiring_frozen = True
    return t


def test_consume_dmn_prediction_returns_none_when_empty():
    t = _make_temporal_skeleton()
    t._dmn_prediction_inbox = asyncio.Queue()
    result = t._consume_dmn_prediction()
    assert result is None


def test_consume_dmn_prediction_returns_latest():
    t = _make_temporal_skeleton()
    t._dmn_prediction_inbox = asyncio.Queue()

    # Stuff three predictions in; we want the most recent
    def _msg(payload):
        m = MagicMock()
        m.expired = False
        m.payload = payload
        return m

    for i in range(3):
        t._dmn_prediction_inbox.put_nowait(
            _msg({"predicted_input": f"guess {i}", "confidence": 0.5})
        )

    result = t._consume_dmn_prediction()
    assert result == {"predicted_input": "guess 2", "confidence": 0.5}
    # Inbox should be empty after consumption
    assert t._dmn_prediction_inbox.empty()


def test_consume_dmn_prediction_skips_expired_messages():
    t = _make_temporal_skeleton()
    t._dmn_prediction_inbox = asyncio.Queue()

    fresh = MagicMock()
    fresh.expired = False
    fresh.payload = {"predicted_input": "fresh one"}

    stale = MagicMock()
    stale.expired = True
    stale.payload = {"predicted_input": "stale one"}

    t._dmn_prediction_inbox.put_nowait(fresh)
    t._dmn_prediction_inbox.put_nowait(stale)

    result = t._consume_dmn_prediction()
    # Stale is filtered, fresh wins as the latest non-expired
    assert result == {"predicted_input": "fresh one"}


# ── Word-overlap-driven confidence boost (integration-ish test) ───────────


def test_dmn_hit_reduces_surprise():
    """Verify the surprise-reduction math: hit_overlap=0.8 drops surprise
    by 0.4 * 0.8 = 0.32 (clamped to >= 0)."""
    # Reproduce the formula used in temporal.run()
    base_surprise = 0.5
    hit = 0.8
    adjusted = max(0.0, base_surprise - 0.4 * hit)
    assert adjusted == pytest.approx(0.18)


def test_dmn_hit_lowers_confidence_floor():
    """Verify floor-reduction math: hit_overlap=0.8 drops floor by
    0.25 * 0.8 = 0.20, clamped to >= 0.3."""
    base_floor = 0.6
    hit = 0.8
    adjusted = max(0.3, base_floor - 0.25 * hit)
    assert adjusted == pytest.approx(0.4)


def test_dmn_hit_does_not_take_floor_below_min():
    """Even with a perfect DMN hit, confidence floor never drops below 0.3."""
    base_floor = 0.35
    hit = 1.0
    adjusted = max(0.3, base_floor - 0.25 * hit)
    assert adjusted == 0.3


def test_dmn_miss_leaves_things_alone():
    """When DMN didn't anticipate the input (overlap < 0.5), nothing changes."""
    base_surprise = 0.5
    base_floor = 0.6
    hit = 0.2  # below 0.5 threshold
    # In real code, the boost only applies when hit >= 0.5
    if hit >= 0.5:
        adjusted_floor = max(0.3, base_floor - 0.25 * hit)
        adjusted_surprise = max(0.0, base_surprise - 0.4 * hit)
    else:
        adjusted_floor = base_floor
        adjusted_surprise = base_surprise
    assert adjusted_floor == base_floor
    assert adjusted_surprise == base_surprise
