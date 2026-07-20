"""
The criticality controller must not leak one persona's trim onto another's thresholds.

The controller measures how excitable the brain is being and nudges a gain that scales
every switch's threshold shift. That gain used to be written into the process-global
settings singleton, which has no persona dimension — while one process serves many
personas (see the BrainSession docstring, session_turn.py). So one persona's trim landed
on every other persona's firing thresholds, and the σ window it steered on averaged
their firing paths together.
"""

from __future__ import annotations

import pytest

from brain.neuron import SwitchNeuron
from brain.observability.criticality import FlockCriticality, current_gain
from brain.second_brain.store import bind_persona
from brain.settings import settings


class _Wiring:
    """Minimal wiring stub: a simple chain, each node feeding exactly the next.

    A chain yields σ = 1.0, which sits between the controller's setpoints (0.90 at
    rest, 1.00 at full arousal). That placement is the point: a denser stub gives
    σ ≫ σ* for every arousal, the gain slams into flock_gain_min in both cases, and
    a test comparing arousal levels then compares two copies of the clamp.
    """

    def __init__(self, names):
        self._names = list(names)

    def has_outgoing(self, n):
        return n in self._names[:-1]

    def successors(self, n):
        i = self._names.index(n)
        return {self._names[i + 1]} if i + 1 < len(self._names) else set()


def _path(n=8):
    names = [f"c.n{i}" for i in range(n)]
    return [{"name": x} for x in names], _Wiring(names)


def _drive(fc, arousal, turns=6):
    """Run `turns` observe+control cycles under whatever persona is bound."""
    fired, wiring = _path()
    out = None
    for _ in range(turns):
        fc.observe(fired, wiring)
        out = fc.control(arousal)
    return out


def _threshold():
    """A switch's effective threshold under the gain in force right now."""
    sw = SwitchNeuron("probe", "frontal", threshold=0.5, modulators={"ACh": -0.4})
    return sw.effective_threshold({"ACh": 1.0})


# ── The regression ───────────────────────────────────────────────────────────


def test_one_personas_control_does_not_move_anothers_thresholds():
    """The bug: persona A drives the controller, and persona B's switches — which
    A never touched — start firing at a different threshold."""
    fc = FlockCriticality()

    with bind_persona("persona_b"):
        b_before = _threshold()

    with bind_persona("persona_a"):
        # arousal 0 → setpoint 0.90 against a measured σ of 1.0, so the controller
        # has real error to correct and the gain actually moves.
        _drive(fc, arousal=0.0, turns=10)
        a_after = _threshold()

    with bind_persona("persona_b"):
        b_after = _threshold()

    assert b_after == pytest.approx(b_before), (
        f"persona A's control leaked onto B: {b_before} → {b_after}"
    )
    # …and A really did move, so the test is not passing by nothing happening.
    assert a_after != pytest.approx(b_before), "A's own threshold should have moved"


def test_sigma_windows_do_not_blend_across_personas():
    """Second half of the defect: one window averaged unrelated personas' firing
    paths, so the loop closed on a signal belonging to neither."""
    fc = FlockCriticality()
    dense, dense_w = _path(10)
    sparse = [{"name": "c.n0"}, {"name": "c.n1"}]
    sparse_w = _Wiring(["c.n0", "c.n1"])

    for _ in range(6):
        with bind_persona("persona_dense"):
            fc.observe(dense, dense_w)
        with bind_persona("persona_sparse"):
            fc.observe(sparse, sparse_w)

    with bind_persona("persona_dense"):
        dense_sigma = fc.smoothed_sigma()
    with bind_persona("persona_sparse"):
        sparse_sigma = fc.smoothed_sigma()

    assert dense_sigma is not None
    # The sparse persona never reaches the min-nodes guard, so its window stays empty
    # rather than inheriting the dense persona's σ.
    assert sparse_sigma is None, f"sparse persona inherited a blended σ: {sparse_sigma}"


def test_each_persona_keeps_its_own_gain():
    """σ is 1.0 for both, so at full arousal (σ* = 1.00) there is no error and the
    gain holds, while at rest (σ* = 0.90) it is trimmed down. Two personas, same
    measurements, different temperaments, different gains — and neither clobbers
    the other."""
    fc = FlockCriticality()
    with bind_persona("p_high"):
        _drive(fc, arousal=1.0, turns=10)
        high = current_gain()
    with bind_persona("p_low"):
        _drive(fc, arousal=0.0, turns=10)
        low = current_gain()
    with bind_persona("p_high"):
        assert current_gain() == pytest.approx(high), "p_high's gain was overwritten"
    assert high > low, f"resting persona should be trimmed below the aroused one ({high} vs {low})"


# ── The fallback path, which is the easy thing to break while fixing this ────


def test_flag_off_personas_keep_the_emotionality_dial(monkeypatch):
    """With flock_dynamics off nothing publishes a gain, so `modulation_gain` IS the
    lever — that is what the Emotionality dial writes for those personas, and it has
    to keep moving thresholds."""
    monkeypatch.setitem(settings._data, "modulation_gain", 1.0)
    with bind_persona("p_static"):
        at_one = _threshold()
    monkeypatch.setitem(settings._data, "modulation_gain", 0.25)
    with bind_persona("p_static"):
        at_quarter = _threshold()
    assert at_quarter != pytest.approx(at_one), "the static dial stopped doing anything"


def test_unbound_context_falls_back_and_never_raises(monkeypatch):
    """Idle/boot work runs with no persona bound; that must degrade to the static
    setting rather than raising inside threshold computation."""
    monkeypatch.setitem(settings._data, "modulation_gain", 0.5)
    assert current_gain() == pytest.approx(0.5)
    _threshold()  # must not raise


def test_persona_without_a_published_gain_uses_the_static_setting(monkeypatch):
    monkeypatch.setitem(settings._data, "modulation_gain", 0.75)
    fc = FlockCriticality()
    with bind_persona("p_driven"):
        _drive(fc, arousal=1.0, turns=10)
    with bind_persona("p_never_ran"):
        assert current_gain() == pytest.approx(0.75)


# ── Kill switch ──────────────────────────────────────────────────────────────


def test_scoped_off_restores_the_shared_global_behaviour(monkeypatch):
    """0 = the old behaviour, including publishing back into settings."""
    monkeypatch.setitem(settings._data, "criticality_persona_scoped", 0)
    monkeypatch.setitem(settings._data, "modulation_gain", 1.0)
    fc = FlockCriticality()
    with bind_persona("persona_a"):
        _drive(fc, arousal=0.0, turns=10)  # real error, so the gain actually moves
    published = float(settings.get("modulation_gain"))
    assert published != pytest.approx(1.0), "flag-off should still write to settings"
    # …and it is shared, which is precisely the old behaviour.
    with bind_persona("persona_b"):
        assert current_gain() == pytest.approx(published)


def test_flock_criticality_reaches_the_learning_ledger():
    """The controller moves real thresholds; it was invisible on the Learning
    surface, which is how the leak went unnoticed."""
    from brain.observability.learning_ledger import LEDGER_TYPES

    assert "flock_criticality" in LEDGER_TYPES
