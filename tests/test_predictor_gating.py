"""
Tests for predict-and-surprise gating: PredictorSwitch, CompositePredictor,
should_bypass_gating helper, and firing-path capture under both skip and call paths.
"""

from __future__ import annotations

from brain.predictor import (
    CompositePredictor,
    PredictorSwitch,
    composite_signature,
    input_signature,
    should_bypass_gating,
)

# ── PredictorSwitch ──────────────────────────────────────────────────────────


def test_predictor_no_history_returns_max_surprise():
    p = PredictorSwitch(name="t", cluster="c")
    predicted, conf = p.predict("sig_x")
    assert predicted is None
    assert conf == 0.0
    assert p.surprise(predicted, "anything", conf) == 1.0
    assert p.should_wake_integrator(1.0) is True


def test_predictor_correct_prediction_low_surprise():
    p = PredictorSwitch(name="t", cluster="c", surprise_threshold=0.4)
    for _ in range(4):
        p.record("sig_a", "tag_a")
    predicted, conf = p.predict("sig_a")
    assert predicted == "tag_a"
    assert conf == 1.0
    surprise = p.surprise(predicted, "tag_a", conf)
    assert surprise == 0.0
    assert p.should_wake_integrator(surprise) is False


def test_predictor_wrong_prediction_high_surprise():
    p = PredictorSwitch(name="t", cluster="c")
    for _ in range(4):
        p.record("sig_a", "tag_a")
    predicted, conf = p.predict("sig_a")
    surprise = p.surprise(predicted, "tag_other", conf)
    assert surprise >= 0.6
    assert p.should_wake_integrator(surprise) is True


# ── CompositePredictor ──────────────────────────────────────────────────────


def test_composite_predictor_skips_only_with_high_confidence():
    cp = CompositePredictor(name="exec", cluster="frontal", confidence_skip_threshold=0.7)
    sig = ("chitchat", "casual", False, "mid", "low")
    # Record one weak example — confidence will be low
    cp.record(sig, ("chitchat", "brief", "warm"))
    pred, conf = cp.predict(sig)
    # Confidence on single sample is 1.0; with 0.7 threshold we'd skip.
    assert cp.should_skip_integrator(pred, conf) is True

    # No prediction → never skip
    pred2, conf2 = cp.predict(("hostile", "formal", True, "low", "high"))
    # Falls back to overall most-common with 0.4x penalty → confidence < threshold
    assert cp.should_skip_integrator(pred2, conf2) is False


def test_composite_predictor_partial_match_surprise():
    cp = CompositePredictor(name="x", cluster="c")
    predicted = ("chitchat", "brief", "warm")
    # 2/3 positions match
    actual = ("chitchat", "brief", "direct")
    s = cp.surprise(predicted, actual, confidence=1.0)
    assert 0.0 < s < 1.0
    # All wrong → higher
    s_wrong = cp.surprise(predicted, ("task", "detailed", "direct"), confidence=1.0)
    assert s_wrong > s


def test_composite_outcome_score_history():
    cp = CompositePredictor(name="critic", cluster="frontal")
    sig = ("chitchat", "casual", False, "mid", "low")
    assert cp.avg_recent_outcome(sig) is None
    cp.record_outcome(sig, 0.85)
    cp.record_outcome(sig, 0.90)
    avg = cp.avg_recent_outcome(sig)
    assert avg is not None
    assert 0.84 < avg < 0.91


# ── composite_signature ─────────────────────────────────────────────────────


def test_composite_signature_buckets_neuromods():
    features = {"intent": "task", "register": "formal", "requires_memory": True}
    affect = {"neuromod": {"DA": 0.8, "GABA": 0.05, "ACh": 0.5, "Glu": 0.3}}
    sig = composite_signature(features, affect)
    assert sig == ("task", "formal", True, "high", "low")


# ── should_bypass_gating ────────────────────────────────────────────────────


def test_bypass_when_GABA_flagged_high():
    bypass, reason = should_bypass_gating({"high_GABA": True}, {})
    assert bypass is True
    assert "GABA" in reason


def test_bypass_when_entity_emotion_is_reactive():
    bypass, reason = should_bypass_gating({"emotion": "frustrated"}, {})
    assert bypass is True
    assert "frustrated" in reason


def test_bypass_when_user_emotion_needs_care():
    bypass, reason = should_bypass_gating({}, {"user_emotion": "distressed"})
    assert bypass is True
    assert "distressed" in reason


def test_bypass_when_vocal_tone_stressed():
    bypass, reason = should_bypass_gating({"vocal_tone": "stressed"}, {})
    assert bypass is True
    assert "stressed" in reason


def test_bypass_when_enrollment_active():
    bypass, reason = should_bypass_gating({}, {"_enrollment_result": {"action": "enrolled"}})
    assert bypass is True
    assert "enrollment" in reason


def test_no_bypass_when_calm():
    bypass, reason = should_bypass_gating(
        {"emotion": "content", "high_GABA": False, "vocal_tone": "calm"},
        {"user_emotion": "neutral"},
    )
    assert bypass is False
    assert reason == ""


# ── input_signature ──────────────────────────────────────────────────────────


def test_input_signature_buckets_length_and_question():
    assert "tiny" in input_signature("hi")
    assert "long" in input_signature("word " * 20)
    assert "q=True" in input_signature("what?")
    assert "mem=True" in input_signature("do you remember last week?")


# ── Firing-path capture from a switch ───────────────────────────────────────


def test_switch_fire_appends_to_current_trace(monkeypatch):
    from brain.neuron import SwitchNeuron
    from brain.observability.firing_path import reset_current_trace, set_current_trace
    from brain.observability.timeline import TurnTrace

    trace = TurnTrace(turn_id="t1", session_id="s1", user_input="x")
    token = set_current_trace(trace)
    try:
        sw = SwitchNeuron(name="alpha", cluster="testcluster")
        sw.fire(0.8, "tag_x")
        assert len(trace.fired_path) == 1
        entry = trace.fired_path[0]
        assert entry["name"] == "testcluster.alpha"
        assert entry["kind"] == "switch"
        assert entry["tag"] == "tag_x"
    finally:
        reset_current_trace(token)


def test_switch_fire_noop_without_trace():
    """fire() must not crash when no trace context is bound."""
    from brain.neuron import SwitchNeuron

    sw = SwitchNeuron(name="alpha", cluster="testcluster")
    # Should simply return the payload without error.
    payload = sw.fire(0.5, "tag")
    assert payload["tag"] == "tag"
