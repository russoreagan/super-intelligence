"""
ClientChemRegistry — the per-(persona, end_user) chemistry contract.

Covers: get-or-create (baseline seed + caching), restore-with-absence-decay on a
returning customer, persist round-trip across registries, the interaction-mass
weighted average for cycle consolidation, and the one-way valve (the aggregate
never mutates a client pair).
"""

from __future__ import annotations

import pytest

from brain.bus import Bus
from brain.client_chem import ClientChemRegistry, InMemoryChemStore


def _clock(start: float = 1000.0):
    box = {"t": start}
    return box, (lambda: box["t"])


def test_get_or_create_seeds_baseline_and_caches():
    bus = Bus()
    reg = ClientChemRegistry(bus, persona="empath")
    a1 = reg.get_or_create("alice")
    a2 = reg.get_or_create("alice")
    assert a1 is a2  # cached — same live pair across the session
    # A fresh customer starts at the persona's init levels (not the resting pair).
    assert a1 is not bus.resting_chem


def test_returning_customer_restored_with_absence_decay():
    bus = Bus()
    store = InMemoryChemStore()
    box, now = _clock()

    reg1 = ClientChemRegistry(bus, store, persona="empath", now_fn=now)
    pair = reg1.get_or_create("alice")
    pair.neuromod.add("DA", 0.4)  # an uplifting session
    elevated = pair.neuromod.get("DA")
    reg1.persist("alice")  # snapshot at t0

    # Ten "turns" worth of time passes before she returns.
    box["t"] += 10 * 180.0

    reg2 = ClientChemRegistry(bus, store, persona="empath", now_fn=now)
    returned = reg2.get_or_create("alice")
    baseline = returned.neuromod._baseline["DA"]
    da = returned.neuromod.get("DA")

    # Mood relaxed toward the temperament baseline while away, but isn't reset:
    assert baseline < da < elevated


def test_persist_roundtrip_across_registries():
    bus = Bus()
    store = InMemoryChemStore()
    box, now = _clock()

    reg1 = ClientChemRegistry(bus, store, persona="empath", now_fn=now)
    p = reg1.get_or_create("bob")
    p.hormonal.add("OXT", 0.2)
    saved_oxt = p.hormonal.get("OXT")
    reg1.persist("bob")

    # No time passes → restored value should match (no absence-decay).
    reg2 = ClientChemRegistry(bus, store, persona="empath", now_fn=now)
    p2 = reg2.get_or_create("bob")
    assert p2.hormonal.get("OXT") == pytest.approx(saved_oxt)


def test_weighted_average_by_interaction_mass():
    bus = Bus()
    reg = ClientChemRegistry(bus, persona="empath")

    a = reg.get_or_create("alice")
    b = reg.get_or_create("bob")
    a.neuromod.add("DA", 0.1)
    b.neuromod.add("DA", 0.2)
    da_a = a.neuromod.get("DA")
    da_b = b.neuromod.get("DA")

    # Alice had a long session (weight 3), Bob a brief one (weight 1).
    for _ in range(3):
        reg.note_interaction("alice")
    reg.note_interaction("bob")

    avg = reg.weighted_average()
    assert avg is not None
    assert avg["neuromod"]["DA"] == pytest.approx((3 * da_a + 1 * da_b) / 4)


def test_weighted_average_is_one_way_does_not_mutate_clients():
    """The aggregate is read-only over client pairs — it must never write back."""
    bus = Bus()
    reg = ClientChemRegistry(bus, persona="empath")
    a = reg.get_or_create("alice")
    a.neuromod.add("DA", 0.15)
    before = a.neuromod.snapshot()

    reg.note_interaction("alice")
    _ = reg.weighted_average()

    assert a.neuromod.snapshot() == before  # unchanged


def test_weighted_average_empty_returns_none():
    bus = Bus()
    reg = ClientChemRegistry(bus, persona="empath")
    assert reg.weighted_average() is None


def test_is_fanned_out_threshold():
    """Mode-emergent engine signal: fan-out only once ≥2 distinct customers are
    live, so all engine-only behaviour stays inert in companion mode (0–1)."""
    bus = Bus()
    reg = ClientChemRegistry(bus, persona="empath")
    assert reg.active_client_count() == 0
    assert not reg.is_fanned_out()

    reg.get_or_create("alice")
    assert reg.active_client_count() == 1
    assert not reg.is_fanned_out()  # one customer = still companion-like

    reg.get_or_create("bob")
    assert reg.active_client_count() == 2
    assert reg.is_fanned_out()  # ≥2 → engine mode


def test_consolidate_into_resting_blends_and_is_one_way():
    """The cycle average pulls the resting mood toward it by alpha, leaves every
    client pair untouched (one-way valve), and clears cycle mass."""
    bus = Bus()
    reg = ClientChemRegistry(bus, persona="empath")
    a = reg.get_or_create("alice")
    b = reg.get_or_create("bob")
    a.neuromod.add("DA", 0.1)
    b.neuromod.add("DA", 0.2)
    reg.note_interaction("alice")
    reg.note_interaction("bob")

    avg_da = reg.weighted_average()["neuromod"]["DA"]
    resting_before = bus.resting_chem.neuromod.get("DA")
    a_before, b_before = a.neuromod.snapshot(), b.neuromod.snapshot()

    snap = reg.consolidate_into_resting(alpha=0.5)
    assert snap is not None

    # Resting moved halfway (alpha=0.5) toward the weighted average.
    assert bus.resting_chem.neuromod.get("DA") == pytest.approx(
        resting_before + 0.5 * (avg_da - resting_before)
    )
    # One-way valve: the aggregate never wrote back onto a client.
    assert a.neuromod.snapshot() == a_before
    assert b.neuromod.snapshot() == b_before
    # Cycle mass cleared for the next cycle.
    assert reg._mass == {}


def test_consolidate_into_resting_noop_without_fanout():
    """With only one customer (not fanned out) there is no separate 'overall' mood
    to fold in — companion-equivalent, so resting is left untouched."""
    bus = Bus()
    reg = ClientChemRegistry(bus, persona="empath")
    reg.get_or_create("alice")
    reg.note_interaction("alice")
    resting_before = bus.resting_chem.neuromod.snapshot()
    assert reg.consolidate_into_resting(0.5) is None
    assert bus.resting_chem.neuromod.snapshot() == resting_before
