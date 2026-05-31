"""Phase 5 (colony features): deterministic persona-seeded threshold spread."""

from __future__ import annotations

import pytest

from brain.neuron import spread_threshold
from brain.settings import settings


@pytest.fixture
def colony_on(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 1)
    monkeypatch.setitem(settings._data, "colony_threshold_spread", 0.08)


def test_noop_when_off(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 0)
    assert spread_threshold(0.5, "the_visionary", "detector") == 0.5


def test_deterministic_same_seed(colony_on):
    a = spread_threshold(0.5, "the_visionary", "detector")
    b = spread_threshold(0.5, "the_visionary", "detector")
    assert a == b  # reproducible — no RNG


def test_within_spread_bounds(colony_on):
    for name in ("a", "b", "c", "detector", "recruiter", "action", "x9"):
        v = spread_threshold(0.5, "the_analyst", name)
        assert 0.5 - 0.08 - 1e-9 <= v <= 0.5 + 0.08 + 1e-9


def test_different_personas_diverge(colony_on):
    """Same switch, different personas → generally different thresholds (diversity)."""
    names = ["s1", "s2", "s3", "s4", "s5"]
    vis = [spread_threshold(0.5, "the_visionary", n) for n in names]
    emp = [spread_threshold(0.5, "the_empath", n) for n in names]
    assert vis != emp


def test_clamped_to_global_bounds(colony_on):
    # base near the edge can't escape [0.05, 0.95]
    assert spread_threshold(0.97, "p", "s") <= 0.95
    assert spread_threshold(0.02, "p", "s") >= 0.05


def test_zero_spread_is_identity(colony_on, monkeypatch):
    monkeypatch.setitem(settings._data, "colony_threshold_spread", 0.0)
    assert spread_threshold(0.5, "p", "s") == 0.5
