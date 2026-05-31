"""Colony Layer II — N3 (sensory-filter gain) and N1 (live trail reinforcement)."""

from __future__ import annotations

import importlib

import pytest

from brain.neuron import sensory_gain
from brain.settings import settings

# ── N3: sensory-filter specialization ─────────────────────────────────────────


def test_sensory_gain_identity_when_off(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 0)
    assert sensory_gain("the_empath", "affective") == 1.0


def test_sensory_gain_identity_when_filter_off(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 1)
    monkeypatch.setitem(settings._data, "colony_sensory_filter", 0)
    assert sensory_gain("the_empath", "affective") == 1.0


def test_sensory_gain_persona_differentiation(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 1)
    monkeypatch.setitem(settings._data, "colony_sensory_filter", 1)
    monkeypatch.setitem(settings._data, "colony_sensory_gain_span", 0.30)
    # Empath detects affective cues more readily; Analyst less so.
    assert sensory_gain("the_empath", "affective") > 1.0
    assert sensory_gain("the_analyst", "affective") < 1.0
    # ...and the reverse for analytic cues.
    assert sensory_gain("the_analyst", "analytic") > 1.0
    assert sensory_gain("the_empath", "analytic") < 1.0


def test_sensory_gain_unknown_is_identity(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 1)
    monkeypatch.setitem(settings._data, "colony_sensory_filter", 1)
    assert sensory_gain("nobody", "affective") == 1.0
    assert sensory_gain("the_empath", "no_such_category") == 1.0


def test_sensory_gain_deterministic(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 1)
    monkeypatch.setitem(settings._data, "colony_sensory_filter", 1)
    assert sensory_gain("the_empath", "affective") == sensory_gain("the_empath", "affective")


# ── N1: live trail reinforcement ──────────────────────────────────────────────


def _wiring(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_WIRING_PATH", str(tmp_path / "wiring.json"))
    monkeypatch.setenv("BRAIN_WIRING_HISTORY_DIR", str(tmp_path / "history"))
    import brain.wiring as w_mod

    importlib.reload(w_mod)
    w = w_mod.Wiring()
    w.add("a", "b", weight=1.0)
    w.add("b", "c", weight=1.0)
    return w


@pytest.fixture
def trail_on(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 1)
    monkeypatch.setitem(settings._data, "colony_trail_clamp", 0.5)
    monkeypatch.setitem(settings._data, "colony_trail_half_life_s", 100.0)


def test_trail_applied_when_enabled(trail_on, monkeypatch, tmp_path):
    monkeypatch.setitem(settings._data, "colony_trail_apply", 1)
    w = _wiring(monkeypatch, tmp_path)
    n = w.reinforce_trail(["a", "b", "c"], 0.2, now=0.0)
    assert n == 2
    assert w.get_edge_weight("a", "b") == pytest.approx(1.2)  # base + overlay
    assert w.get_edge_weight("b", "c") == pytest.approx(1.2)


def test_trail_shadow_mode_records_but_does_not_apply(trail_on, monkeypatch, tmp_path):
    monkeypatch.setitem(settings._data, "colony_trail_apply", 0)  # shadow
    w = _wiring(monkeypatch, tmp_path)
    w.reinforce_trail(["a", "b"], 0.2, now=0.0)
    assert w.get_edge_weight("a", "b") == pytest.approx(1.0)  # live read unchanged
    assert w.trail_snapshot()  # ...but the would-be overlay IS recorded for the audit
    assert w.trail_snapshot()["a→b"] == pytest.approx(0.2)


def test_trail_clamped(trail_on, monkeypatch, tmp_path):
    monkeypatch.setitem(settings._data, "colony_trail_apply", 1)
    w = _wiring(monkeypatch, tmp_path)
    w.reinforce_trail(["a", "b"], 5.0, now=0.0)  # huge
    assert w.get_edge_weight("a", "b") == pytest.approx(1.5)  # clamped to base + 0.5


def test_trail_decays(trail_on, monkeypatch, tmp_path):
    monkeypatch.setitem(settings._data, "colony_trail_apply", 1)
    w = _wiring(monkeypatch, tmp_path)
    w.reinforce_trail(["a", "b"], 0.4, now=0.0)
    w.decay_trails(now=100.0)  # one half-life
    assert w.get_edge_weight("a", "b") == pytest.approx(1.2, abs=1e-3)  # overlay 0.4 → 0.2


def test_trail_does_not_mutate_persisted_weight(trail_on, monkeypatch, tmp_path):
    monkeypatch.setitem(settings._data, "colony_trail_apply", 1)
    w = _wiring(monkeypatch, tmp_path)
    w.reinforce_trail(["a", "b"], 0.3, now=0.0)
    assert w._edges[("a", "b")].weight == pytest.approx(1.0)  # persisted store untouched


def test_trail_noop_when_colony_off(monkeypatch, tmp_path):
    monkeypatch.setitem(settings._data, "colony_features", 0)
    w = _wiring(monkeypatch, tmp_path)
    assert w.reinforce_trail(["a", "b"], 0.2, now=0.0) == 0
    assert w.get_edge_weight("a", "b") == pytest.approx(1.0)
