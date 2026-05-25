"""
Unit tests for neuromodulator-aware SwitchNeuron threshold modulation.

Covers:
  * effective_threshold math (linear shift centered at 0.5)
  * identity default (empty modulators → unchanged behaviour)
  * snapshot=None passthrough
  * min_threshold / max_threshold clamping (incl. safety-critical floor)
  * should_fire() decision under shifted thresholds
  * near-miss decision emission when chemistry suppresses a would-be fire
"""
from __future__ import annotations

import pytest

from brain.neuron import StatefulSwitch, SwitchNeuron
from brain.observability.decisions import decisions


def test_empty_modulators_is_identity():
    s = SwitchNeuron("plain", "test", threshold=0.5)
    snap = {"DA": 0.9, "GABA": 0.1}
    assert s.effective_threshold(snap) == 0.5
    assert s.effective_threshold(None) == 0.5
    assert s.modulation_delta(snap) == 0.0


def test_snapshot_none_returns_base_threshold():
    s = SwitchNeuron("p", "test", threshold=0.4, modulators={"DA": +0.2})
    assert s.effective_threshold(None) == 0.4


def test_positive_coefficient_raises_threshold_under_high_channel():
    s = SwitchNeuron("p", "test", threshold=0.5, modulators={"ACh": +0.20})
    # ACh at 1.0 → shift = 0.20 * (1.0 - 0.5) = +0.10
    assert s.effective_threshold({"ACh": 1.0}) == pytest.approx(0.60, abs=1e-9)
    # ACh at 0.0 → shift = 0.20 * (-0.5) = -0.10
    assert s.effective_threshold({"ACh": 0.0}) == pytest.approx(0.40, abs=1e-9)
    # Neutral chemistry (0.5) is a no-op
    assert s.effective_threshold({"ACh": 0.5}) == pytest.approx(0.50, abs=1e-9)


def test_negative_coefficient_lowers_threshold_under_high_channel():
    s = SwitchNeuron("p", "test", threshold=0.5, modulators={"GABA": -0.20})
    assert s.effective_threshold({"GABA": 1.0}) == pytest.approx(0.40, abs=1e-9)
    assert s.effective_threshold({"GABA": 0.0}) == pytest.approx(0.60, abs=1e-9)


def test_multiple_modulators_sum():
    s = SwitchNeuron("p", "test", threshold=0.5,
                     modulators={"DA": +0.10, "NE": -0.20})
    # DA shift +0.10*(0.8-0.5) = +0.03; NE shift -0.20*(0.7-0.5) = -0.04 → net -0.01
    assert s.effective_threshold({"DA": 0.8, "NE": 0.7}) == pytest.approx(0.49, abs=1e-9)


def test_missing_channel_in_snapshot_ignored():
    s = SwitchNeuron("p", "test", threshold=0.5,
                     modulators={"DA": +0.20, "NE": +0.20})
    # NE absent → only DA contributes
    assert s.effective_threshold({"DA": 1.0}) == pytest.approx(0.60, abs=1e-9)


def test_min_threshold_floor_clamps():
    # safety-critical switch: even with maximum suppressive chemistry, threshold cannot fall below 0.40.
    s = SwitchNeuron("safety", "motor", threshold=0.50,
                     modulators={"NE": -0.5, "CORT": -0.5},
                     min_threshold=0.40)
    eff = s.effective_threshold({"NE": 1.0, "CORT": 1.0})
    assert eff >= 0.40
    assert eff == 0.40  # clamp engaged


def test_max_threshold_ceiling_clamps():
    s = SwitchNeuron("p", "test", threshold=0.50,
                     modulators={"ACh": +1.0},
                     max_threshold=0.80)
    eff = s.effective_threshold({"ACh": 1.0})
    assert eff <= 0.80
    assert eff == 0.80


def test_should_fire_uses_effective_threshold():
    s = SwitchNeuron("p", "test", threshold=0.5, modulators={"ACh": +0.20})
    # Under neutral chemistry, input 0.55 fires.
    assert s.should_fire(0.55, {"ACh": 0.5}) is True
    # Under high ACh, effective threshold rises to 0.60 → 0.55 does NOT fire.
    assert s.should_fire(0.55, {"ACh": 1.0}) is False
    # Under low ACh, effective threshold falls to 0.40 → 0.45 fires.
    assert s.should_fire(0.45, {"ACh": 0.0}) is True


def test_should_fire_backwards_compatible_no_snapshot():
    s = SwitchNeuron("p", "test", threshold=0.5)
    assert s.should_fire(0.6) is True
    assert s.should_fire(0.4) is False


def test_fire_payload_includes_modulation_evidence():
    s = SwitchNeuron("p", "test", threshold=0.5, modulators={"GABA": -0.20})
    payload = s.fire(0.7, "tag", evidence={"src": "x"}, snapshot={"GABA": 1.0})
    ev = payload["evidence"]
    assert ev["base_threshold"] == 0.5
    assert ev["effective_threshold"] == pytest.approx(0.4, abs=1e-9)
    assert ev["modulation_delta"] == pytest.approx(-0.1, abs=1e-9)
    assert ev["src"] == "x"  # original evidence preserved


def test_inhibitory_polarity_signs_level():
    s = SwitchNeuron("p", "test", polarity="inhibitory", threshold=0.5)
    payload = s.fire(0.6, "tag")
    assert payload["level"] < 0


def test_stateful_switch_inherits_modulation():
    s = StatefulSwitch("recall_fanout", "hippocampus", decay=0.95,
                       modulators={"ACh": -0.10})
    # Inherits effective_threshold from SwitchNeuron.
    assert s.effective_threshold({"ACh": 1.0}) == pytest.approx(0.45, abs=1e-9)
    # State machinery still works.
    s.update(0.3)
    assert s.state == pytest.approx(0.3)
    s.tick()
    assert s.state == pytest.approx(0.3 * 0.95)


def test_near_miss_emits_suppression_decision(monkeypatch):
    captured: list[dict] = []

    def fake_log(decision: str, **fields):
        captured.append({"decision": decision, **fields})
        return {"decision": decision, **fields}

    monkeypatch.setattr(decisions, "log", fake_log)

    s = SwitchNeuron("template_match", "temporal", threshold=0.5,
                     modulators={"ACh": +0.20})
    # Under high ACh, effective threshold = 0.60. Input 0.55 would fire at base
    # threshold but is suppressed by modulation → should emit a decision.
    assert s.should_fire(0.55, {"ACh": 1.0}) is False
    assert len(captured) == 1
    rec = captured[0]
    assert rec["decision"] == "switch_suppressed_by_modulation"
    assert rec["switch"] == "template_match"
    assert rec["cluster"] == "temporal"
    assert rec["base_threshold"] == 0.5
    assert rec["effective_threshold"] == pytest.approx(0.60, abs=1e-9)
    assert rec["chemistry"] == {"ACh": 1.0}


def test_near_miss_does_not_emit_when_below_base_threshold(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(decisions, "log",
                         lambda d, **f: captured.append({"d": d, **f}))
    s = SwitchNeuron("p", "test", threshold=0.5, modulators={"ACh": +0.20})
    # Input 0.30 wouldn't fire even at base threshold — not a chemistry suppression.
    assert s.should_fire(0.30, {"ACh": 1.0}) is False
    assert captured == []


def test_near_miss_does_not_emit_when_no_modulators(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(decisions, "log",
                         lambda d, **f: captured.append({"d": d, **f}))
    s = SwitchNeuron("p", "test", threshold=0.5)
    assert s.should_fire(0.4, {"ACh": 1.0}) is False
    assert captured == []


# ---------------------------------------------------------------------------
# Modulation gain knob
# ---------------------------------------------------------------------------

def test_modulation_gain_zero_disables_all_chemistry(monkeypatch):
    """gain=0 → switches behave as if modulators were empty everywhere."""
    from brain.settings import settings as _settings
    monkeypatch.setattr(_settings, "get",
                         lambda k, d=None: 0.0 if k == "modulation_gain"
                         else _settings._data.get(k, d if d is not None else 0))
    s = SwitchNeuron("p", "test", threshold=0.5,
                     modulators={"ACh": +0.20, "GABA": -0.20})
    # Even at chemistry extremes, gain=0 means threshold stays at 0.5.
    assert s.effective_threshold({"ACh": 1.0, "GABA": 1.0}) == 0.5
    assert s.effective_threshold({"ACh": 0.0, "GABA": 0.0}) == 0.5


def test_modulation_gain_two_doubles_shift(monkeypatch):
    """gain=2 → modulation shift is twice as strong."""
    from brain.settings import settings as _settings
    monkeypatch.setattr(_settings, "get",
                         lambda k, d=None: 2.0 if k == "modulation_gain"
                         else _settings._data.get(k, d if d is not None else 0))
    s = SwitchNeuron("p", "test", threshold=0.5, modulators={"ACh": +0.10})
    # ACh=1.0 with coeff 0.10 → shift = 0.10*0.5 = 0.05; doubled = 0.10
    assert s.effective_threshold({"ACh": 1.0}) == pytest.approx(0.60, abs=1e-9)


def test_modulation_gain_default_is_one(monkeypatch):
    """Without override, gain=1.0 — modulation operates at declared strength."""
    s = SwitchNeuron("p", "test", threshold=0.5, modulators={"ACh": +0.10})
    # Default gain 1.0: ACh=1.0 → shift = 0.05 → threshold = 0.55
    assert s.effective_threshold({"ACh": 1.0}) == pytest.approx(0.55, abs=1e-9)
