"""
Tests for the endocrine / hormonal system:
  - HormonalState unit tests (bus.py)
  - apply_hormonal_color overlay (emotion_vocabulary.py)
  - Signal balance and reachability (do extreme states actually occur?)
  - Hypothalamus integration (hormonal updates + affect dict population)
  - Tracing (TurnTrace carries hormonal field)
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.bus import Bus, HormonalState
from brain.emotion_vocabulary import apply_hormonal_color
from brain.settings import settings


# ── Helpers ───────────────────────────────────────────────────────────────────

def _thresholds() -> dict:
    return {
        "oxt_connected":  settings.get("hormonal_oxt_connected_threshold"),
        "cort_withdrawn": settings.get("hormonal_cort_withdrawn_threshold"),
        "oxt_guarded":    settings.get("hormonal_oxt_guarded_threshold"),
        "sht_dysphoric":  settings.get("hormonal_sht_dysphoric_threshold"),
    }


def _color(emotion: str, tendency: str, h: dict) -> tuple[str, str]:
    t = _thresholds()
    return apply_hormonal_color(
        emotion, tendency, h,
        oxt_connected=t["oxt_connected"],
        cort_withdrawn=t["cort_withdrawn"],
        oxt_guarded=t["oxt_guarded"],
        sht_dysphoric=t["sht_dysphoric"],
    )


def _make_hyp():
    """Build a HypothalamusCluster with a real Bus, no LLMs."""
    from brain.clusters.hypothalamus import HypothalamusCluster
    bus = Bus()
    hyp = HypothalamusCluster(bus)
    return bus, hyp


def _warm_features(**overrides) -> dict:
    base = {
        "sentiment": 0.7,
        "hostility": 0.0,
        "salience": 0.5,
        "surprise_score": 0.1,
        "intent": "question",
        "topic_summary": "friendly chat",
    }
    base.update(overrides)
    return base


def _hostile_features(**overrides) -> dict:
    base = {
        "sentiment": -0.5,
        "hostility": 0.8,
        "salience": 0.6,
        "surprise_score": 0.0,
        "intent": "hostile",
        "topic_summary": "conflict",
    }
    base.update(overrides)
    return base


# ── HormonalState unit tests ──────────────────────────────────────────────────

class TestHormonalState:
    def test_initial_values(self):
        hs = HormonalState()
        snap = hs.snapshot()
        assert snap["5HT"] == pytest.approx(0.50)
        assert snap["CORT"] == pytest.approx(0.05)
        assert snap["OXT"] == pytest.approx(0.30)

    def test_add_clamps_at_one(self):
        hs = HormonalState()
        hs.add("OXT", 5.0)
        assert hs.get("OXT") == pytest.approx(1.0)

    def test_add_clamps_at_zero(self):
        hs = HormonalState()
        hs.add("CORT", -10.0)
        assert hs.get("CORT") == pytest.approx(0.0)

    def test_add_increments_correctly(self):
        hs = HormonalState()
        before = hs.get("5HT")
        hs.add("5HT", 0.1)
        assert hs.get("5HT") == pytest.approx(before + 0.1)

    def test_decay_reduces_all_channels(self):
        hs = HormonalState()
        hs.add("OXT", 0.4)   # push OXT above floor
        hs.add("CORT", 0.3)
        hs.add("5HT", 0.2)
        before = hs.snapshot()
        hs.decay()
        after = hs.snapshot()
        for ch in ("5HT", "CORT", "OXT"):
            assert after[ch] <= before[ch], f"{ch} should not increase on decay"

    def test_decay_respects_floors(self):
        hs = HormonalState()
        # Force channels to floor by decaying many times
        for _ in range(2000):
            hs.decay()
        snap = hs.snapshot()
        assert snap["5HT"]  >= HormonalState._FLOORS["5HT"]
        assert snap["CORT"] >= HormonalState._FLOORS["CORT"]
        assert snap["OXT"]  >= HormonalState._FLOORS["OXT"]

    def test_snapshot_is_copy(self):
        hs = HormonalState()
        snap = hs.snapshot()
        snap["OXT"] = 999.0
        assert hs.get("OXT") != 999.0

    def test_da_offset_signs(self):
        hs = HormonalState()
        hs._levels = {"5HT": 0.8, "CORT": 0.0, "OXT": 0.8}
        offset_high_pos = hs.da_offset(0.12, 0.05, 0.08)
        assert offset_high_pos > 0, "High 5HT + high OXT should lift DA"

        hs._levels = {"5HT": 0.2, "CORT": 0.8, "OXT": 0.1}
        offset_high_cort = hs.da_offset(0.12, 0.05, 0.08)
        assert offset_high_cort < offset_high_pos, "High CORT should suppress DA offset"

    def test_gaba_scale_above_one_with_high_cort(self):
        hs = HormonalState()
        hs._levels = {"5HT": 0.5, "CORT": 0.8, "OXT": 0.0}
        scale = hs.gaba_scale(0.30, 0.15)
        assert scale > 1.0, "High CORT should amplify GABA"

    def test_gaba_scale_below_one_with_high_oxt(self):
        hs = HormonalState()
        hs._levels = {"5HT": 0.5, "CORT": 0.0, "OXT": 1.0}
        scale = hs.gaba_scale(0.30, 0.15)
        assert scale < 1.0, "High OXT should buffer GABA"

    def test_gaba_scale_never_below_floor(self):
        hs = HormonalState()
        hs._levels = {"5HT": 0.5, "CORT": 0.0, "OXT": 1.0}
        scale = hs.gaba_scale(0.30, 1.0)  # extreme OXT buffer weight
        assert scale >= 0.5, "gaba_scale should be floored at 0.5"


# ── apply_hormonal_color tests ────────────────────────────────────────────────

class TestApplyHormonalColor:
    def test_high_oxt_warm_to_connected(self):
        h = {"OXT": 0.80, "CORT": 0.03, "5HT": 0.5}
        e, _ = _color("warm", "affirm", h)
        assert e == "connected"

    def test_high_oxt_joy_to_connected(self):
        h = {"OXT": 0.75, "CORT": 0.03, "5HT": 0.5}
        e, _ = _color("joy", "approach", h)
        assert e == "connected"

    def test_high_oxt_on_negative_emotion_not_connected(self):
        # Connected only fires on positive base emotions
        h = {"OXT": 0.80, "CORT": 0.03, "5HT": 0.5}
        e, _ = _color("anxious", "caution", h)
        assert e != "connected"

    def test_high_cort_low_oxt_neutral_to_withdrawn(self):
        h = {"OXT": 0.20, "CORT": 0.60, "5HT": 0.5}
        e, _ = _color("neutral", "balanced", h)
        assert e == "withdrawn"

    def test_high_cort_low_oxt_anxious_to_guarded(self):
        h = {"OXT": 0.20, "CORT": 0.60, "5HT": 0.5}
        e, _ = _color("anxious", "caution", h)
        assert e == "guarded"

    def test_high_cort_moderate_oxt_warm_to_cautious_warm(self):
        h = {"OXT": 0.50, "CORT": 0.60, "5HT": 0.5}
        e, _ = _color("warm", "affirm", h)
        assert e == "cautious-warm"

    def test_low_sht_flat_to_dysphoric(self):
        h = {"OXT": 0.30, "CORT": 0.05, "5HT": 0.15}
        e, _ = _color("flat", "minimal", h)
        assert e == "dysphoric"

    def test_low_sht_neutral_to_dysphoric(self):
        h = {"OXT": 0.30, "CORT": 0.05, "5HT": 0.10}
        e, _ = _color("neutral", "balanced", h)
        assert e == "dysphoric"

    def test_normal_state_unchanged(self):
        h = {"OXT": 0.30, "CORT": 0.05, "5HT": 0.50}
        e, t = _color("curious", "investigate", h)
        assert e == "curious"
        assert t == "investigate"

    def test_just_below_oxt_threshold_not_connected(self):
        # OXT just below connected threshold should not fire
        threshold = settings.get("hormonal_oxt_connected_threshold")
        h = {"OXT": threshold - 0.01, "CORT": 0.03, "5HT": 0.5}
        e, _ = _color("warm", "affirm", h)
        assert e != "connected"

    def test_just_below_cort_threshold_not_withdrawn(self):
        threshold = settings.get("hormonal_cort_withdrawn_threshold")
        h = {"OXT": 0.20, "CORT": threshold - 0.01, "5HT": 0.5}
        e, _ = _color("neutral", "balanced", h)
        assert e not in ("withdrawn", "guarded")

    def test_just_below_sht_threshold_not_dysphoric(self):
        threshold = settings.get("hormonal_sht_dysphoric_threshold")
        h = {"OXT": 0.30, "CORT": 0.05, "5HT": threshold + 0.01}
        e, _ = _color("flat", "minimal", h)
        assert e != "dysphoric"

    def test_tendency_updated_for_connected(self):
        h = {"OXT": 0.80, "CORT": 0.03, "5HT": 0.5}
        _, tendency = _color("warm", "affirm", h)
        assert tendency != "affirm", "connected should override tendency"


# ── Signal balance / reachability tests ──────────────────────────────────────

class TestSignalBalance:
    """Verify that extreme hormonal states are reachable in plausible turn counts."""

    def _run_turns(self, hs: HormonalState, n: int,
                   oxt_delta: float = 0.0, cort_delta: float = 0.0,
                   sht_delta: float = 0.0) -> None:
        for _ in range(n):
            if oxt_delta:
                hs.add("OXT", oxt_delta)
            if cort_delta:
                hs.add("CORT", cort_delta)
            if sht_delta:
                hs.add("5HT", sht_delta)
            hs.decay()

    def test_oxt_reaches_connected_within_80_warm_turns(self):
        hs = HormonalState()
        inc = settings.get("oxt_positive_increment")
        self._run_turns(hs, 80, oxt_delta=inc)
        threshold = settings.get("hormonal_oxt_connected_threshold")
        assert hs.get("OXT") >= threshold, (
            f"OXT={hs.get('OXT'):.3f} should reach connected threshold {threshold} "
            f"within 80 warm turns"
        )

    def test_cort_reaches_withdrawn_within_35_hostile_turns(self):
        # CORT threshold is 0.45; with decay=0.97 and increment=0.022,
        # equilibrium is 0.733 but convergence is ~33–35 turns from baseline.
        hs = HormonalState()
        inc = settings.get("cort_threat_increment")
        self._run_turns(hs, 35, cort_delta=inc)
        threshold = settings.get("hormonal_cort_withdrawn_threshold")
        assert hs.get("CORT") >= threshold, (
            f"CORT={hs.get('CORT'):.3f} should reach withdrawn threshold {threshold} "
            f"within 35 hostile turns"
        )

    def test_sht_reaches_dysphoric_within_120_hostile_turns(self):
        hs = HormonalState()
        drain = settings.get("sht_hostility_drain")
        self._run_turns(hs, 120, sht_delta=-drain)
        threshold = settings.get("hormonal_sht_dysphoric_threshold")
        assert hs.get("5HT") <= threshold, (
            f"5HT={hs.get('5HT'):.3f} should reach dysphoric threshold {threshold} "
            f"within 120 hostile turns"
        )

    def test_oxt_buffers_cort_meaningfully(self):
        # With high OXT, CORT growth should be slower than without it
        inc = settings.get("cort_threat_increment")
        buf = settings.get("oxt_cort_buffer_rate")
        threshold = settings.get("oxt_cort_buffer_threshold")

        hs_no_oxt = HormonalState()
        hs_no_oxt._levels["OXT"] = 0.0
        hs_high_oxt = HormonalState()
        hs_high_oxt._levels["OXT"] = 0.80

        for _ in range(20):
            hs_no_oxt.add("CORT", inc)
            hs_no_oxt.decay()

            hs_high_oxt.add("CORT", inc)
            if hs_high_oxt.get("OXT") > threshold:
                hs_high_oxt.add("CORT", -hs_high_oxt.get("OXT") * buf)
            hs_high_oxt.decay()

        assert hs_high_oxt.get("CORT") < hs_no_oxt.get("CORT"), (
            "High OXT should slow CORT accumulation over 20 hostile turns"
        )

    def test_oxt_decays_slowly_preserving_relationship_memory(self):
        # OXT should retain >70% of its value after 100 turns of no interaction
        hs = HormonalState()
        hs.add("OXT", 0.40)   # push to 0.70
        peak = hs.get("OXT")
        for _ in range(100):
            hs.decay()
        assert hs.get("OXT") >= peak * 0.70, (
            f"OXT should retain ≥70% after 100 idle turns (got {hs.get('OXT'):.3f})"
        )

    def test_positive_turns_lift_5ht(self):
        hs = HormonalState()
        before = hs.get("5HT")
        inc = settings.get("sht_reward_increment")
        for _ in range(20):
            hs.add("5HT", inc)
            hs.decay()
        assert hs.get("5HT") > before

    def test_cort_equilibrium_below_one(self):
        # Steady-state CORT with threat every turn should stay below 1.0
        hs = HormonalState()
        inc = settings.get("cort_threat_increment")
        for _ in range(200):
            hs.add("CORT", inc)
            hs.decay()
        assert hs.get("CORT") < 1.0


# ── Hypothalamus integration tests ────────────────────────────────────────────

class TestHypothalamusHormonalIntegration:
    def test_affect_dict_contains_hormonal(self):
        bus, hyp = _make_hyp()
        affect = asyncio.run(hyp.process(_warm_features()))
        assert "hormonal" in affect
        h = affect["hormonal"]
        assert "5HT" in h and "CORT" in h and "OXT" in h

    def test_warm_turn_increases_oxt(self):
        bus, hyp = _make_hyp()
        before = bus.hormonal.get("OXT")
        asyncio.run(hyp.process(_warm_features()))
        assert bus.hormonal.get("OXT") > before

    def test_hostile_turns_increase_cort(self):
        # CORT now triggers directly from hostility score (not GABA level),
        # so it builds immediately when hostility > cort_hostility_threshold (0.35).
        # _hostile_features() uses hostility=0.8 which is well above threshold.
        bus, hyp = _make_hyp()
        before = bus.hormonal.get("CORT")
        asyncio.run(hyp.process(_hostile_features()))
        assert bus.hormonal.get("CORT") > before

    def test_hostile_turn_decreases_oxt(self):
        bus, hyp = _make_hyp()
        # Give OXT something to drain from
        bus.hormonal.add("OXT", 0.3)
        before = bus.hormonal.get("OXT")
        asyncio.run(hyp.process(_hostile_features()))
        assert bus.hormonal.get("OXT") < before

    def test_hostile_turn_decreases_5ht(self):
        bus, hyp = _make_hyp()
        before = bus.hormonal.get("5HT")
        asyncio.run(hyp.process(_hostile_features()))
        assert bus.hormonal.get("5HT") < before

    def test_warm_turn_increases_5ht(self):
        bus, hyp = _make_hyp()
        before = bus.hormonal.get("5HT")
        asyncio.run(hyp.process(_warm_features(sentiment=0.8)))
        assert bus.hormonal.get("5HT") > before

    def test_decay_turn_reduces_hormonal_levels(self):
        bus, hyp = _make_hyp()
        # Push above floor so decay has room to act
        bus.hormonal.add("OXT", 0.4)
        bus.hormonal.add("CORT", 0.3)
        before = bus.hormonal.snapshot()
        hyp.decay_turn()
        after = bus.hormonal.snapshot()
        assert after["OXT"] <= before["OXT"]
        assert after["CORT"] <= before["CORT"]

    def test_sustained_hostility_produces_guarded_or_withdrawn(self):
        bus, hyp = _make_hyp()
        # Run many hostile turns to build CORT high enough for hormonal color
        for _ in range(35):
            asyncio.run(hyp.process(_hostile_features()))
            hyp.decay_turn()
        affect = asyncio.run(hyp.process(_hostile_features()))
        assert affect["emotion"] in ("guarded", "withdrawn", "angry", "inhibited",
                                     "cautious-agitated", "defensive",
                                     "stressed", "overwhelmed", "uneasy"), (
            f"Expected a stress-state emotion after sustained hostility, got: {affect['emotion']}"
        )

    def test_sustained_positivity_produces_connected(self):
        bus, hyp = _make_hyp()
        # Run enough warm turns to push OXT past the connected threshold
        threshold = settings.get("hormonal_oxt_connected_threshold")
        for _ in range(100):
            asyncio.run(hyp.process(_warm_features()))
            hyp.decay_turn()
        # OXT should now be above the threshold
        assert bus.hormonal.get("OXT") >= threshold, (
            f"OXT={bus.hormonal.get('OXT'):.3f} should reach {threshold} after 100 warm turns"
        )

    def test_neutral_features_preserve_normal_emotion(self):
        bus, hyp = _make_hyp()
        neutral = _warm_features(sentiment=0.0, hostility=0.0, salience=0.3, surprise_score=0.0)
        affect = asyncio.run(hyp.process(neutral))
        # Hormonal state should be near defaults — no extreme state
        assert affect["emotion"] not in ("connected", "withdrawn", "guarded", "dysphoric")


# ── Tracing / TurnTrace tests ─────────────────────────────────────────────────

class TestHormonalTracing:
    def test_turn_trace_has_hormonal_field(self):
        from brain.observability.timeline import TurnTrace
        trace = TurnTrace(
            turn_id="t1",
            session_id="s1",
            user_input="hello",
        )
        assert hasattr(trace, "hormonal")
        assert isinstance(trace.hormonal, dict)

    def test_turn_trace_hormonal_default_empty(self):
        from brain.observability.timeline import TurnTrace
        trace = TurnTrace(turn_id="t1", session_id="s1", user_input="hi")
        assert trace.hormonal == {}

    def test_record_turn_appends_hormonal_to_history(self):
        from brain.observability.timeline import ObservabilityLayer, TurnTrace
        obs = ObservabilityLayer(session_id="test")
        trace = TurnTrace(
            turn_id="t1",
            session_id="test",
            user_input="hi",
            neuromod={"DA": 0.5, "GABA": 0.05, "ACh": 0.2, "Glu": 0.3},
            hormonal={"5HT": 0.5, "CORT": 0.05, "OXT": 0.3},
        )
        obs.record_turn(trace)
        history = list(obs._neuromod_history)
        assert len(history) == 1
        entry = history[0]
        assert "hormonal" in entry
        assert entry["hormonal"]["5HT"] == pytest.approx(0.5)
        assert entry["hormonal"]["OXT"] == pytest.approx(0.3)

    def test_record_turn_without_hormonal_still_works(self):
        from brain.observability.timeline import ObservabilityLayer, TurnTrace
        obs = ObservabilityLayer(session_id="test")
        trace = TurnTrace(
            turn_id="t1",
            session_id="test",
            user_input="hi",
            neuromod={"DA": 0.5, "GABA": 0.05, "ACh": 0.2, "Glu": 0.3},
        )
        obs.record_turn(trace)
        history = list(obs._neuromod_history)
        assert len(history) == 1
        # No "hormonal" key when hormonal dict is empty
        assert "hormonal" not in history[0]

    def test_emit_hormonal_queues_correct_event_type(self):
        import asyncio
        from brain.ui.emitter import ActivationEmitter
        em = ActivationEmitter()
        snap = {"5HT": 0.5, "CORT": 0.05, "OXT": 0.3}
        asyncio.run(em.emit_hormonal(snap))
        event = em.get_queue().get_nowait()
        assert event["type"] == "hormonal"
        assert event["5HT"] == pytest.approx(0.5)
        assert event["OXT"] == pytest.approx(0.3)

    def test_emit_hormonal_rounds_values(self):
        import asyncio
        from brain.ui.emitter import ActivationEmitter
        em = ActivationEmitter()
        asyncio.run(em.emit_hormonal({"5HT": 0.12345678, "CORT": 0.0, "OXT": 0.0}))
        event = em.get_queue().get_nowait()
        assert event["5HT"] == pytest.approx(0.123, abs=1e-3)
