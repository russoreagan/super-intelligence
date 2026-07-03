"""Tests for the per-persona reward-source vector + intrinsic correctness reinforcement.

Covers:
  - neuron.reward_weight: per-persona valuation lookup, identity fallback.
  - The new global magnitude settings exist.
  - Metacognition pride is now INTRINSIC (fires on high self-score without user praise)
    and has a symmetric self-standard disappointment on low self-score.
  - DMN verdict reinforcement: affirm → +DA, reject → -DA & -5HT, scaled by persona.
"""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.neuron import reward_weight
from brain.settings import settings


@pytest.fixture(autouse=True)
def _restore_persona_name():
    """The DMN tests force settings["persona_name"]; restore it so nothing leaks."""
    from brain.settings import settings

    prev = settings._data.get("persona_name")
    yield
    if prev is None:
        settings._data.pop("persona_name", None)
    else:
        settings._data["persona_name"] = prev


# ── reward_weight ──────────────────────────────────────────────────────────────


def test_reward_weight_known_personas():
    # The Analyst values correctness most; the Empath least (it draws reward from connection).
    assert reward_weight("The Analyst", "correctness") > 1.0
    assert reward_weight("The Empath", "correctness") < 1.0
    assert reward_weight("The Analyst", "correctness") > reward_weight("The Empath", "correctness")
    # The Empath values connection most; the Analyst least.
    assert reward_weight("The Empath", "connection") > reward_weight("The Analyst", "connection")
    # The Visionary feeds on novelty.
    assert reward_weight("The Visionary", "novelty") > 1.0


def test_reward_weight_accepts_display_name_or_slug():
    assert reward_weight("The Analyst", "correctness") == reward_weight(
        "the_analyst", "correctness"
    )


def test_reward_weight_identity_fallback():
    assert reward_weight("Nobody", "correctness") == 1.0  # unknown persona
    assert reward_weight("The Sage", "made_up_source") == 1.0  # unknown source
    assert reward_weight("", "correctness") == 1.0


def test_reward_magnitude_settings_exist():
    from brain.settings import settings

    for key in (
        "correctness_reward_base",
        "correctness_self_base",
        "correctness_penalty_base",
        "correctness_5ht_drain",
        "anticipation_reward_scale",
        "self_standard_gate",
    ):
        assert isinstance(settings.get(key), (int, float)), f"missing/invalid setting {key}"


# ── Metacognition: intrinsic pride + self-standard disappointment ───────────────


def _make_metacog():
    from brain.metacognition import MetacognitionCell

    m = MetacognitionCell.__new__(MetacognitionCell)
    m._turn_stats = deque(maxlen=10)
    m._neuromod_history = deque(maxlen=50)
    m._affection_score = lambda *_: 0  # no flirty/affection paths
    return m


_NEUTRAL = {"user_tone_toward_ai": "neutral", "user_emotion": "unknown", "intent": "other"}


def test_pride_fires_without_user_praise():
    """The core fix: a high-self-score turn produces pride even when the user is neutral."""
    m = _make_metacog()
    draft_scores = [{"selected": True, "overall": 0.92}]
    emotion, reason = m._appraise(dict(_NEUTRAL), {"DA": 0.5}, draft_scores)
    assert emotion == "proud"
    assert "intrinsic" in reason


def test_pride_still_notes_warmth_when_present():
    m = _make_metacog()
    draft_scores = [{"selected": True, "overall": 0.92}]
    feats = dict(_NEUTRAL, user_tone_toward_ai="warm")
    emotion, reason = m._appraise(feats, {"DA": 0.5}, draft_scores)
    assert emotion == "proud"
    assert "warmly" in reason


def test_self_standard_disappointment_on_low_score():
    """The punishment twin of intrinsic pride — fell short of its own bar, user neutral."""
    m = _make_metacog()
    draft_scores = [{"selected": True, "overall": 0.30}]
    emotion, reason = m._appraise(dict(_NEUTRAL), {"DA": 0.5}, draft_scores)
    assert emotion == "disappointed"
    assert "self-standard" in reason


# ── DMN: verified-correctness reinforcement ─────────────────────────────────────


class _RecordingNeuromod:
    def __init__(self):
        self.deltas: dict[str, float] = {}

    def add(self, channel, delta, source="intrinsic", **attribution):
        self.deltas[channel] = self.deltas.get(channel, 0.0) + delta

    def snapshot(self):
        return dict(self.deltas)


def _make_dmn_with_bus(persona: str):
    import brain.open_threads as ot
    from brain.dmn import DefaultModeNetwork
    from brain.settings import settings

    settings._data["persona_name"] = persona

    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._router = MagicMock()
    dmn._router.embed = AsyncMock(return_value=None)
    hip = MagicMock()
    hip.encode_conclusion = AsyncMock()
    dmn._hippocampus = hip
    dmn._session_id = "test"
    dmn._open_threads = []
    dmn._recent_conclusions = deque(maxlen=5)
    dmn._save_threads = AsyncMock()
    nm = _RecordingNeuromod()
    dmn._bus = MagicMock()
    dmn._bus.neuromod = nm
    return dmn, nm, ot


@pytest.mark.asyncio
async def test_affirm_rewards_da():
    dmn, nm, ot = _make_dmn_with_bus("The Analyst")
    dmn._open_threads, t = ot.open_thread([], "is gating cheaper?")
    dmn._open_threads, t = ot.mark_pending(dmn._open_threads, t.id)
    t.pending_conclusion = "Gating reduces tokens."
    await dmn._resolve_pending_conclusion(t, "affirm", "yes")
    assert nm.deltas.get("DA", 0.0) > 0.0  # verified correct → reward


@pytest.mark.asyncio
async def test_reject_penalizes_da_and_5ht():
    dmn, nm, ot = _make_dmn_with_bus("The Analyst")
    dmn._open_threads, t = ot.open_thread([], "is gating cheaper?")
    dmn._open_threads, t = ot.mark_pending(dmn._open_threads, t.id)
    await dmn._resolve_pending_conclusion(t, "reject", "no")
    assert nm.deltas.get("DA", 0.0) < 0.0  # verified wrong → DA dip
    assert nm.deltas.get("5HT", 0.0) < 0.0  # lingering sting


@pytest.mark.asyncio
async def test_persona_scales_correctness_reward():
    """The Analyst (high correctness valuation) is rewarded more than the Empath (low)."""
    dmn_a, nm_a, ot = _make_dmn_with_bus("The Analyst")
    dmn_a._open_threads, ta = ot.open_thread([], "q?")
    dmn_a._open_threads, ta = ot.mark_pending(dmn_a._open_threads, ta.id)
    ta.pending_conclusion = "c"
    await dmn_a._resolve_pending_conclusion(ta, "affirm", "yes")

    dmn_e, nm_e, ot = _make_dmn_with_bus("The Empath")
    dmn_e._open_threads, te = ot.open_thread([], "q?")
    dmn_e._open_threads, te = ot.mark_pending(dmn_e._open_threads, te.id)
    te.pending_conclusion = "c"
    await dmn_e._resolve_pending_conclusion(te, "affirm", "yes")

    assert nm_a.deltas["DA"] > nm_e.deltas["DA"]


# ── Stage 5: self-verified correctness (prediction_reward + informativeness) ─────


def test_prediction_reward_confident_correct_informative_positive():
    from brain.neuron import prediction_reward

    assert prediction_reward(0.8, True, 0.7) > 0


def test_prediction_reward_confident_wrong_negative():
    from brain.neuron import prediction_reward

    assert prediction_reward(0.8, False, 0.7) < 0


def test_prediction_reward_low_confidence_is_zero():
    from brain.neuron import prediction_reward

    # Below prediction_confidence_min (0.55) → a guess, not a prediction.
    assert prediction_reward(0.4, True, 0.9) == 0.0


def test_prediction_reward_trivial_is_zero():
    from brain.neuron import prediction_reward

    # Near-degenerate informativeness (being right about the inevitable) earns nothing.
    assert prediction_reward(0.95, True, 0.05) == 0.0


def test_predictor_informativeness_degenerate_vs_varied():
    from brain.predictor import PredictorSwitch

    p = PredictorSwitch(name="t", cluster="c")
    for _ in range(6):
        p.record("sigA", "always")  # constant outcome → trivial
    assert p.informativeness("sigA") == 0.0
    q = PredictorSwitch(name="t2", cluster="c")
    for tag in ["x", "y", "x", "z", "y", "w"]:  # varied outcomes → informative
        q.record("sigB", tag)
    assert q.informativeness("sigB") > 0.3


# ── Stage 6: accomplishment / mastery (accomplishment_factor curve) ──────────────


def test_mastery_weight_seeds_present():
    # Grinders value mastery more than reward-chasers.
    assert reward_weight("The Sage", "mastery") > reward_weight("The Visionary", "mastery")


def test_accomplishment_scales_with_effort():
    from brain.neuron import accomplishment_factor

    exp = 6.0
    low_d, low_m = accomplishment_factor(2.0, exp)
    hi_d, hi_m = accomplishment_factor(6.0, exp)
    # More effort overcome → bigger terminal product (difficulty rises faster than modifier falls).
    assert hi_d * hi_m > low_d * low_m


def test_accomplishment_expectation_gap_non_monotonic():
    """Russ's key requirement: a LARGE overshoot yields LESS terminal reward than a modest one
    (frustration erodes the payoff), even though raw effort is higher."""
    from brain.neuron import accomplishment_factor

    exp = 6.0
    modest_d, modest_m = accomplishment_factor(8.0, exp)  # r≈1.33, in the sweet spot
    big_d, big_m = accomplishment_factor(30.0, exp)  # r=5, blew past the brace
    assert (big_d * big_m) < (modest_d * modest_m)


def test_accomplishment_anticlimax_when_easier_than_feared():
    from brain.neuron import accomplishment_factor

    _, mod = accomplishment_factor(1.0, 14.0)  # r≈0.07, much easier than the "high" brace
    assert mod < 1.0


# ── Stage 7: idle cognition rewards ──────────────────────────────────────────────


def test_sequence_predictor_informativeness():
    from brain.sequence_predictor import SequencePredictor

    varied = SequencePredictor()
    for a in ["a", "b", "x", "a", "b", "y", "a", "b", "z", "a", "b"]:
        varied.record(a)  # context ('a','b') has 3 distinct continuations
    assert varied.informativeness() > 0.5
    const = SequencePredictor()
    for _ in range(6):
        const.record("same")
    assert const.informativeness() == 0.0


def _make_dmn_idle(persona: str):
    from brain.dmn import DefaultModeNetwork

    settings._data["persona_name"] = persona
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    nm = _RecordingNeuromod()
    dmn._bus = MagicMock()
    dmn._bus.neuromod = nm
    return dmn, nm


def test_idle_thought_quality_rewards_novel_thought():
    dmn, nm = _make_dmn_idle("The Visionary")
    # Novel thought: low overlap, low cosine → high quality → DA reward.
    dmn._reward_idle_thought_quality(
        "a genuinely fresh and reasonably detailed idle reflection here", 0.05, 0.1
    )
    assert nm.deltas.get("DA", 0.0) > 0.0
    settings._data.pop("persona_name", None)


def test_idle_thought_quality_skips_filler():
    dmn, nm = _make_dmn_idle("The Visionary")
    # High overlap (near-duplicate) → below quality threshold → no reward.
    dmn._reward_idle_thought_quality("again", 0.95, 0.95)
    assert nm.deltas.get("DA", 0.0) == 0.0
    settings._data.pop("persona_name", None)


def test_angle_prediction_reward_sign_and_gating():
    dmn, nm = _make_dmn_idle("The Analyst")
    # Confident, informative, CORRECT prediction → +DA.
    dmn._last_predicted_angle = "architecture"
    dmn._last_angle_confidence = 0.8
    dmn._last_angle_informativeness = 0.7
    dmn._reward_angle_prediction("architecture")
    assert nm.deltas.get("DA", 0.0) > 0.0
    # Stash is consumed (scores once).
    assert getattr(dmn, "_last_predicted_angle", None) is None

    # Confident WRONG → negative.
    dmn2, nm2 = _make_dmn_idle("The Analyst")
    dmn2._last_predicted_angle = "architecture"
    dmn2._last_angle_confidence = 0.8
    dmn2._last_angle_informativeness = 0.7
    dmn2._reward_angle_prediction("something-else")
    assert nm2.deltas.get("DA", 0.0) < 0.0
    settings._data.pop("persona_name", None)
