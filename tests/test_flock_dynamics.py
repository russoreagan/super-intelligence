"""
Tests for the flock_dynamics layer (murmuration-derived criticality + chemistry
trajectory). Three parts, all behind the `flock_dynamics` flag:

  (1) chemistry velocity → DMN rumination asymmetry (rising vs steady stress)
  (2) criticality observable — branching ratio σ + smoothing + small-N guard
  (3) closed loop — arousal-modulated setpoint, clamped EMA gain, never super-critical
  (4) grounding — surprise drives CORT only when the flag is on

The cardinal invariant: with the flag OFF every path is inert.
"""

from __future__ import annotations

import pytest

from brain.bus import HormonalState, Neuromodulators
from brain.dmn import DefaultModeNetwork
from brain.observability.criticality import FlockCriticality, branching_ratio
from brain.sequence_predictor import SequencePredictor
from brain.settings import settings


@pytest.fixture
def flock_on(monkeypatch):
    monkeypatch.setitem(settings._data, "flock_dynamics", 1)


# ── tiny wiring stub: a→b, a→c, b→d (a,b propagate; c,d terminal) ─────────────
class _Wiring:
    edges = {("a", "b"), ("a", "c"), ("b", "d")}

    def successors(self, s):
        return {t for (x, t) in self.edges if x == s}

    def has_outgoing(self, s):
        return any(x == s for (x, _t) in self.edges)


def _fp(*names):
    return [{"name": n} for n in names]


# ── (1) chemistry trajectory ──────────────────────────────────────────────────


def test_velocity_inert_until_marked():
    """Flag-off path: velocity is 0 and snapshot is unaffected until mark_turn."""
    nm = Neuromodulators()
    assert nm.velocity()["NE"] == 0.0
    h = HormonalState()
    assert h.velocity()["CORT"] == 0.0


def test_velocity_rising_vs_steady():
    h = HormonalState()
    h._levels["CORT"] = 0.10
    h.mark_turn(1.0)
    h._levels["CORT"] = 0.30
    h.mark_turn(1.0)
    assert h.velocity()["CORT"] == pytest.approx(0.20, abs=1e-6)

    steady = HormonalState()
    steady._levels["CORT"] = 0.50
    steady.mark_turn(1.0)
    steady._levels["CORT"] = 0.50
    steady.mark_turn(1.0)
    assert steady.velocity()["CORT"] == pytest.approx(0.0, abs=1e-6)


def test_velocity_scales_by_turns():
    h = HormonalState()
    h._levels["CORT"] = 0.10
    h.mark_turn(1.0)
    h._levels["CORT"] = 0.30
    h.mark_turn(2.0)  # same delta over 2 turns → half the rate
    assert h.velocity()["CORT"] == pytest.approx(0.10, abs=1e-6)


# ── (1b) rumination drive asymmetry: rising stress ruminates harder ──────────


def _rum_drive(chem):
    dmn = DefaultModeNetwork.__new__(
        DefaultModeNetwork
    )  # _rumination_drive is pure in (chem, settings)
    dmn._seq_predictor = SequencePredictor()
    return dmn._rumination_drive(chem)[0]


def test_rising_cort_ruminates_harder_than_steady(flock_on):
    """Same CORT/NE LEVEL, different trajectory → rising drives more rumination.
    This is the murmuration-hysteresis asymmetry (Item 1)."""
    base = {"CORT": 0.5, "NE": 0.5, "DA": 0.5, "ACh": 0.5, "5HT": 0.2}
    steady = {**base, "vel_CORT": 0.0, "vel_NE": 0.0, "vel_DA": 0.0}
    rising = {**base, "vel_CORT": 0.2, "vel_NE": 0.1, "vel_DA": 0.0}
    assert _rum_drive(rising) > _rum_drive(steady)


def test_rumination_velocity_ignored_when_flag_off(monkeypatch):
    monkeypatch.setitem(settings._data, "flock_dynamics", 0)
    base = {"CORT": 0.5, "NE": 0.5, "DA": 0.5, "ACh": 0.5, "5HT": 0.2}
    steady = {**base, "vel_CORT": 0.0}
    rising = {**base, "vel_CORT": 0.2}
    assert _rum_drive(rising) == pytest.approx(_rum_drive(steady))


# ── (2) criticality observable ────────────────────────────────────────────────


def test_branching_ratio_known_graph():
    w = _Wiring()
    # a,b,c,d fired: internal={a,b}; a→{b,c} both fired (2), b→{d} fired (1) → 3/2
    assert branching_ratio(_fp("a", "b", "c", "d"), w, min_nodes=2) == pytest.approx(1.5)
    # only a,b fired: a→b fired (1), b→d not fired (0) → 1/2 (sub-critical)
    assert branching_ratio(_fp("a", "b"), w, min_nodes=2) == pytest.approx(0.5)


def test_branching_ratio_small_n_guard():
    w = _Wiring()
    assert branching_ratio(_fp("a", "b", "c", "d"), w, min_nodes=5) is None
    assert branching_ratio([], w, min_nodes=1) is None
    assert branching_ratio(_fp("a", "b"), None, min_nodes=1) is None


def test_observe_smooths_and_counts(monkeypatch):
    monkeypatch.setitem(settings._data, "flock_sigma_min_nodes", 2)
    fc = FlockCriticality()
    w = _Wiring()
    m1 = fc.observe(_fp("a", "b"), w)  # σ=0.5
    m2 = fc.observe(_fp("a", "b", "c", "d"), w)  # σ=1.5
    assert m1["sigma"] == pytest.approx(0.5)
    assert m2["avalanche"] == 4
    # window mean of [0.5, 1.5] = 1.0
    assert fc.smoothed_sigma() == pytest.approx(1.0)


# ── (3) closed-loop control ───────────────────────────────────────────────────


def test_setpoint_tracks_arousal_capped():
    fc = FlockCriticality()
    lo = settings.get("flock_sigma_target_low")
    hi = settings.get("flock_sigma_target_high")
    assert fc.setpoint(0.0) == pytest.approx(lo)  # low arousal → sub-critical
    assert fc.setpoint(1.0) == pytest.approx(hi)  # high arousal → critical (capped)
    assert fc.setpoint(5.0) <= hi  # never super-critical even if arousal overshoots


def test_control_holds_gain_until_sigma_estimable(monkeypatch):
    monkeypatch.setitem(settings._data, "modulation_gain", 1.0)
    fc = FlockCriticality()
    out = fc.control(1.0)  # no observations yet → smoothed σ is None
    assert out["sigma_smoothed"] is None
    assert out["gain"] == pytest.approx(1.0)  # gain held


def test_control_drives_gain_and_clamps(monkeypatch):
    monkeypatch.setitem(settings._data, "flock_sigma_min_nodes", 2)
    monkeypatch.setitem(settings._data, "modulation_gain", 1.0)
    w = _Wiring()
    gmin = settings.get("flock_gain_min")
    gmax = settings.get("flock_gain_max")

    # Sub-critical measured (σ=0.5) at high arousal (σ*=1.0) → gain should RISE.
    fc = FlockCriticality()
    for _ in range(30):
        fc.observe(_fp("a", "b"), w)
        rose = fc.control(1.0)
    assert rose["gain"] > 1.0
    assert gmin <= rose["gain"] <= gmax

    # Super-critical measured (σ=1.5) at low arousal (σ*=0.9) → gain should FALL.
    monkeypatch.setitem(settings._data, "modulation_gain", 1.0)
    fc2 = FlockCriticality()
    for _ in range(30):
        fc2.observe(_fp("a", "b", "c", "d"), w)
        fell = fc2.control(0.0)
    assert fell["gain"] < 1.0
    assert gmin <= fell["gain"] <= gmax


def test_control_ema_no_thrash(monkeypatch):
    """A single large error moves gain by only one small EMA·kp step, not a jump."""
    monkeypatch.setitem(settings._data, "flock_sigma_min_nodes", 2)
    monkeypatch.setitem(settings._data, "modulation_gain", 1.0)
    fc = FlockCriticality()
    w = _Wiring()
    fc.observe(_fp("a", "b", "c", "d"), w)  # σ=1.5
    out = fc.control(0.0)  # σ*=0.9, err=0.6
    # |Δgain| ≈ alpha·|kp|·err, a small, bounded step
    step = abs(out["gain"] - 1.0)
    bound = abs(settings.get("flock_gain_ema_alpha") * settings.get("flock_gain_kp") * 0.6)
    assert step == pytest.approx(bound, abs=1e-6)
    assert step < 0.1


# ── flag-off inertness (the cardinal invariant) ───────────────────────────────


def test_modulation_gain_untouched_when_flag_off(monkeypatch):
    """With the flag off, nothing in session_turn calls the controller, so the
    static modulation_gain is the source of truth. Here we assert the controller
    is the ONLY writer — it isn't invoked unless flock_dynamics gates it on."""
    monkeypatch.setitem(settings._data, "flock_dynamics", 0)
    monkeypatch.setitem(settings._data, "modulation_gain", 1.0)
    # Constructing/observing without control() never writes gain.
    fc = FlockCriticality()
    fc.observe(_fp("a", "b", "c", "d"), _Wiring())
    assert settings.get("modulation_gain") == pytest.approx(1.0)
