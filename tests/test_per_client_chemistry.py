"""
Per-(persona, end_user) chemistry instancing — the Bus contextvar seam.

These tests pin the foundational guarantees of the multi-tenant "one persona
serving many customers" mode (see reports/per_client_chemistry_design.md):

  • Companion mode (no bind anywhere) resolves every chemistry access to a single
    resting pair — byte-for-byte the original single-instance brain.
  • A bound pair isolates from the resting pair (and reverts on block exit).
  • snapshot()/restore() round-trips a pair's levels (persist/restore per client).
  • THE KEYSTONE: two concurrently-interleaved async "turns", each bound to its own
    client pair, accumulate chemistry with ZERO cross-client bleed — proving the
    concurrent design is safe via contextvars, with no real traffic needed.
"""

from __future__ import annotations

import asyncio

import pytest

from brain.bus import Bus, ChemPair


def test_companion_mode_no_bind_uses_resting():
    """With no bind, bus.neuromod/hormonal ARE the resting pair's — one instance."""
    bus = Bus()
    assert bus.neuromod is bus.resting_chem.neuromod
    assert bus.hormonal is bus.resting_chem.hormonal

    before = bus.resting_chem.neuromod.get("DA")
    bus.neuromod.add("DA", 0.1)
    # The mutation landed on the single resting instance — nothing else exists.
    assert bus.resting_chem.neuromod.get("DA") == pytest.approx(before + 0.1)


def test_bind_isolates_from_resting_and_reverts():
    """Inside a bind, accesses resolve to the bound pair; resting is untouched;
    the binding reverts cleanly on block exit (companion default restored)."""
    bus = Bus()
    resting_da = bus.resting_chem.neuromod.get("DA")

    client = bus.new_chem()
    with bus.bind(client):
        assert bus.neuromod is client.neuromod
        bus.neuromod.add("DA", 0.2)
        assert client.neuromod.get("DA") == pytest.approx(resting_da + 0.2)
        # resting must not have moved
        assert bus.resting_chem.neuromod.get("DA") == pytest.approx(resting_da)

    # outside the block, back to resting
    assert bus.neuromod is bus.resting_chem.neuromod
    assert bus.resting_chem.neuromod.get("DA") == pytest.approx(resting_da)


def test_snapshot_restore_roundtrip():
    """A client's chemistry can be snapshotted and restored exactly (persist path)."""
    pair = ChemPair.fresh()
    pair.neuromod.add("DA", 0.13)
    pair.hormonal.add("OXT", 0.07)
    snap = pair.snapshot()

    # drift away, then restore
    pair.neuromod.add("DA", 0.25)
    pair.hormonal.add("OXT", -0.05)
    assert pair.neuromod.get("DA") != pytest.approx(snap["neuromod"]["DA"])

    pair.restore(snap)
    assert pair.neuromod.get("DA") == pytest.approx(snap["neuromod"]["DA"])
    assert pair.hormonal.get("OXT") == pytest.approx(snap["hormonal"]["OXT"])


async def _client_turn(bus: Bus, pair: ChemPair, da: float, ach: float, iters: int):
    """Simulate a client's turn: bind their chemistry, accumulate across awaits.
    The await on each step forces interleaving with the other concurrent turn."""
    with bus.bind(pair):
        for _ in range(iters):
            bus.neuromod.add("DA", da)
            bus.neuromod.add("ACh", ach)
            await asyncio.sleep(0)  # yield → the other task runs in between


async def test_interleaved_concurrent_turns_no_bleed():
    """KEYSTONE: two interleaved turns bound to different client pairs accumulate
    independently. If the contextvar leaked across tasks, A would pick up B's
    deltas (and vice versa) and the asserted totals would be wrong."""
    bus = Bus()
    a = bus.new_chem()
    b = bus.new_chem()
    a0_da, a0_ach = a.neuromod.get("DA"), a.neuromod.get("ACh")
    b0_da, b0_ach = b.neuromod.get("DA"), b.neuromod.get("ACh")

    iters = 100
    da_a, ach_a = 0.001, 0.002
    da_b, ach_b = 0.003, 0.001

    await asyncio.gather(
        _client_turn(bus, a, da_a, ach_a, iters),
        _client_turn(bus, b, da_b, ach_b, iters),
    )

    # Each pair shows exactly its own accumulation — no contribution from the other.
    assert a.neuromod.get("DA") == pytest.approx(a0_da + iters * da_a)
    assert a.neuromod.get("ACh") == pytest.approx(a0_ach + iters * ach_a)
    assert b.neuromod.get("DA") == pytest.approx(b0_da + iters * da_b)
    assert b.neuromod.get("ACh") == pytest.approx(b0_ach + iters * ach_b)


async def test_resting_untouched_by_client_turns():
    """A separate, explicit check that client turns never perturb the resting pair."""
    bus = Bus()
    resting_da_before = bus.resting_chem.neuromod.get("DA")
    a = bus.new_chem()
    await _client_turn(bus, a, 0.002, 0.002, 50)
    assert bus.resting_chem.neuromod.get("DA") == pytest.approx(resting_da_before)
