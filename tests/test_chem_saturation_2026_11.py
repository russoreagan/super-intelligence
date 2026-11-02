"""Chemistry saturation guards from the 2026-11-01 prod check.

Prod: farrah went DA 0.61 → 1.0 and ACh → 1.0 in 8 small-talk turns (20–30 s
gaps), and the idle resting pair held DA = GABA = 1.0 from 10-21.

1. pace_factor / soft_bound primitives (brain/bus.py).
2. Turn close settles the turn's net impulses (chem_turn_impulse_settle).
3. The idle loop relaxes every channel for the elapsed time on the turn clock
   (dmn_idle_relax_all) and settles each tick's impulses (dmn_idle_settle_impulses).
4. A restored level pinned at a bound resumes at baseline (chem_restore_unpin_bounds).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from datetime import UTC, datetime

import pytest

from brain.bus import Bus, ChemPair, Neuromodulators, pace_factor, soft_bound
from brain.settings import settings

R_DA = Neuromodulators._DEF_DECAY["DA"]

# ── 1. Primitives ─────────────────────────────────────────────────────────────


def test_pace_factor_never_amplifies_and_matches_decay_at_short_gaps():
    assert pace_factor(R_DA, 1.0) == 1.0
    assert pace_factor(R_DA, 10.0) == 1.0  # a long silence never enlarges an impulse
    assert pace_factor(R_DA, 0.0) == 0.0
    t = 0.4
    # One impulse per gap t then settles where one per reference interval does.
    assert pace_factor(R_DA, t) / (1 - R_DA**t) == pytest.approx(1 / (1 - R_DA))


def test_soft_bound_is_linear_up_to_baseline_then_never_reaches_the_bound():
    assert soft_bound(0.2, 0.1, baseline=0.5, lo=0.02) == pytest.approx(0.3)
    assert soft_bound(0.5, 0.0, baseline=0.5, lo=0.02) == 0.5
    x = 0.5
    for _ in range(200):
        x = soft_bound(x, 0.1, baseline=0.5, lo=0.02)
    assert 0.99 < x < 1.0
    y = 0.5
    for _ in range(200):
        y = soft_bound(y, -0.1, baseline=0.5, lo=0.02)
    assert 0.02 < y < 0.03
    # Gain past the baseline equals the headroom share left.
    small = soft_bound(0.75, 1e-4, baseline=0.5, lo=0.02) - 0.75
    assert small == pytest.approx(1e-4 * (1 - 0.75) / (1 - 0.5), rel=1e-3)


# ── 2. Turn close ─────────────────────────────────────────────────────────────

SMALL_TALK = {
    "sentiment": 0.3,
    "hostility": 0.0,
    "salience": 0.4,
    "surprise_score": 0.2,
    "intent": "statement",
    "topic_summary": "small talk",
}
POST_DRAFT_REWARD = {"DA": 0.09, "ACh": 0.06}  # pride/critic, once per turn


def _small_talk(monkeypatch, *, gap_s: float, turns: int = 30, settle: int = 1) -> dict:
    from brain.clusters.hypothalamus import HypothalamusCluster

    monkeypatch.setitem(settings._data, "chem_turn_impulse_settle", settle)
    bus = Bus()
    hyp = HypothalamusCluster(bus)
    for _ in range(turns):
        hyp._last_decay_time = time.monotonic() - gap_s
        asyncio.run(hyp.process(dict(SMALL_TALK)))
        for ch, d in POST_DRAFT_REWARD.items():
            bus.neuromod.add(ch, d)
        hyp.decay_turn()
    return bus.neuromod.snapshot()


def test_fast_small_talk_pinned_without_the_guard(monkeypatch):
    """The prod failure, reproduced: once-per-turn rewards outrun a 25 s gap's decay."""
    snap = _small_talk(monkeypatch, gap_s=25, settle=0)
    assert snap["DA"] == 1.0 and snap["ACh"] == 1.0


def test_fast_small_talk_does_not_pin(monkeypatch):
    snap = _small_talk(monkeypatch, gap_s=25)
    assert snap["DA"] < 0.95
    assert snap["ACh"] < 0.9
    assert snap["NE"] < 0.9


def test_turn_equilibrium_is_pace_independent(monkeypatch):
    fast = _small_talk(monkeypatch, gap_s=25)
    slow = _small_talk(monkeypatch, gap_s=60)
    assert fast["DA"] == pytest.approx(slow["DA"], abs=0.03)
    assert fast["ACh"] == pytest.approx(slow["ACh"], abs=0.03)


def test_turn_close_scales_once_per_turn_rewards_by_pace(monkeypatch):
    """At a 15 s gap (turns 0.25, the floor) a post-draft reward lands at
    pace_factor's share, from a level at baseline (so no headroom shrink yet)."""
    from brain.clusters.hypothalamus import HypothalamusCluster

    monkeypatch.setitem(settings._data, "chem_turn_impulse_settle", 1)
    bus = Bus()
    hyp = HypothalamusCluster(bus)
    nm = bus.neuromod
    nm.restore({"DA": 0.2})
    neutral = {"sentiment": 0.0, "hostility": 0.0, "salience": 0.0, "surprise_score": 0.0}
    hyp._last_decay_time = time.monotonic() - 15
    asyncio.run(hyp.process(neutral))
    start = nm.get("DA")
    nm.add("DA", 0.05)
    hyp.decay_turn()
    turns = hyp._current_turns
    assert nm.get("DA") - start == pytest.approx(0.05 * pace_factor(R_DA, turns), abs=1e-6)


def test_turn_ledger_closes_with_the_turn(monkeypatch):
    from brain.clusters.hypothalamus import HypothalamusCluster

    bus = Bus()
    hyp = HypothalamusCluster(bus)
    pair = bus.resting_chem
    asyncio.run(hyp.process(dict(SMALL_TALK)))
    assert pair.turn_in_flight(time.monotonic()) and pair.turn_ledger["start"]
    hyp.decay_turn()
    assert not pair.turn_in_flight(time.monotonic()) and pair.turn_ledger == {}
    # The resting pair's end-of-turn stamp is mirrored for the idle loop.
    assert pair.decay_clock["last"] == pytest.approx(hyp._last_decay_time)


def test_bound_pair_settles_on_its_own_ledger(monkeypatch):
    from brain.clusters.hypothalamus import HypothalamusCluster

    bus = Bus()
    hyp = HypothalamusCluster(bus)
    client = ChemPair.fresh()
    with bus.bind(client):
        asyncio.run(hyp.process(dict(SMALL_TALK)))
        assert client.turn_ledger and not bus.resting_chem.turn_ledger
        hyp.decay_turn()
    assert client.turn_ledger == {}


# ── 3. Idle relaxation and tick settling ──────────────────────────────────────


def _dmn(bus):
    from brain.dmn import DefaultModeNetwork

    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._bus = bus
    return dmn


def test_relax_elapsed_first_call_only_starts_the_clock():
    pair = ChemPair.fresh()
    pair.neuromod.restore({"DA": 1.0})
    assert pair.relax_elapsed(100.0) == 0.0
    assert pair.neuromod.get("DA") == 1.0


def test_relax_elapsed_decays_every_channel_for_the_gap(monkeypatch):
    monkeypatch.setitem(settings._data, "decay_reference_interval_s", 60.0)
    pair = ChemPair.fresh()
    nm, hs = pair.neuromod, pair.hormonal
    nm.set_baseline({"DA": 0.3, "GABA": 0.12})
    nm.restore({"DA": 1.0, "GABA": 1.0})
    hs.restore({"CORT": 0.8})
    pair.decay_clock["relaxed_at"] = 0.0
    assert pair.relax_elapsed(120.0) == pytest.approx(2.0)
    assert nm.get("DA") == pytest.approx(0.3 + 0.7 * R_DA**2)
    assert nm.get("GABA") == pytest.approx(0.12 + 0.88 * Neuromodulators._DEF_DECAY["GABA"] ** 2)
    assert hs.get("CORT") < 0.8


def test_relax_elapsed_measures_from_the_later_turn_stamp():
    pair = ChemPair.fresh()
    pair.neuromod.restore({"DA": 0.9})
    pair.decay_clock.update({"relaxed_at": 0.0, "last": 110.0})  # a turn ended at 110
    assert pair.relax_elapsed(120.0) == pytest.approx(10.0 / 60.0)


def test_relax_elapsed_leaves_a_turn_in_flight_alone():
    pair = ChemPair.fresh()
    pair.neuromod.restore({"DA": 0.9})
    now = time.monotonic()
    pair.decay_clock.update({"relaxed_at": now - 120, "turn_open": now - 5})
    assert pair.relax_elapsed(now) == 0.0
    assert pair.neuromod.get("DA") == 0.9
    pair.decay_clock["turn_open"] = now - ChemPair.TURN_OPEN_STALE_S - 1  # never closed
    assert pair.relax_elapsed(now) > 0.0


def test_turn_gap_skips_time_the_idle_loop_already_relaxed():
    from brain.clusters.hypothalamus import HypothalamusCluster

    bus = Bus()
    hyp = HypothalamusCluster(bus)
    now = time.monotonic()
    hyp._last_decay_time = now - 300
    bus.resting_chem.decay_clock["relaxed_at"] = now - 20
    assert hyp._clock_last(now) == pytest.approx(now - 20)


def test_idle_decay_relaxes_da_and_gaba(monkeypatch):
    monkeypatch.setitem(settings._data, "dmn_idle_relax_all", 1)
    bus = Bus()
    nm = bus.neuromod
    nm.set_baseline({"DA": 0.3, "GABA": 0.12})
    nm.restore({"DA": 1.0, "GABA": 1.0})
    bus.resting_chem.decay_clock["relaxed_at"] = time.monotonic() - 300
    _dmn(bus)._idle_decay()
    # Five minutes idle: the same relaxation a turn after a 5 min gap applies.
    assert nm.get("DA") == pytest.approx(0.3 + 0.7 * R_DA**5, abs=2e-3)
    assert nm.get("GABA") < 0.6


def test_idle_decay_relax_kill_switch(monkeypatch):
    monkeypatch.setitem(settings._data, "dmn_idle_relax_all", 0)
    bus = Bus()
    bus.neuromod.restore({"DA": 1.0, "GABA": 1.0})
    bus.resting_chem.decay_clock["relaxed_at"] = time.monotonic() - 300
    _dmn(bus)._idle_decay()
    assert bus.neuromod.get("DA") == 1.0 and bus.neuromod.get("GABA") == 1.0


def _idle_hour(monkeypatch, *, tick_s: float, settle: int) -> dict:
    """DMN ticks with no turns, each thought nudging DA/GABA/5HT up (fake clock)."""
    monkeypatch.setitem(settings._data, "dmn_idle_settle_impulses", settle)
    bus = Bus()
    dmn = _dmn(bus)
    pair = bus.resting_chem
    t = 0.0
    pair.decay_clock["relaxed_at"] = t
    while t < 3600:
        t += tick_s
        pair.relax_elapsed(t)
        opened = dmn._open_idle_impulses()
        if opened is not None:
            opened["t0"] = t
        pair.neuromod.add("DA", 0.03)
        pair.neuromod.add("GABA", 0.04)
        pair.hormonal.add("5HT", 0.01)
        dmn._settle_idle_impulses(opened)
    return {**pair.neuromod.snapshot(), **pair.hormonal.snapshot()}


def test_idle_ticks_do_not_pin(monkeypatch):
    pinned = _idle_hour(monkeypatch, tick_s=8, settle=0)
    assert pinned["DA"] == 1.0 and pinned["GABA"] == 1.0  # relax alone is outrun
    for tick in (8, 45):
        snap = _idle_hour(monkeypatch, tick_s=tick, settle=1)
        assert snap["DA"] < 0.85 and snap["GABA"] < 0.7 and snap["5HT"] < 0.95


def test_idle_settle_equilibrium_is_tick_rate_independent(monkeypatch):
    fast = _idle_hour(monkeypatch, tick_s=8, settle=1)
    slow = _idle_hour(monkeypatch, tick_s=45, settle=1)
    assert fast["DA"] == pytest.approx(slow["DA"], abs=0.03)
    assert fast["GABA"] == pytest.approx(slow["GABA"], abs=0.03)


def test_idle_settle_skips_a_tick_a_turn_overlapped(monkeypatch):
    monkeypatch.setitem(settings._data, "dmn_idle_settle_impulses", 1)
    bus = Bus()
    dmn = _dmn(bus)
    pair = bus.resting_chem
    opened = dmn._open_idle_impulses()
    pair.decay_clock["turn_open"] = time.monotonic()  # a user turn started mid-tick
    pair.neuromod.add("DA", 0.2)
    level = pair.neuromod.get("DA")
    dmn._settle_idle_impulses(opened)
    assert pair.neuromod.get("DA") == level


def test_loop_settles_the_tick():
    """The loop opens before _tick and settles after it, even when the tick fails."""
    from brain.dmn import DefaultModeNetwork

    src = inspect.getsource(DefaultModeNetwork._loop)
    assert "_impulses = self._open_idle_impulses()" in src
    assert "self._settle_idle_impulses(_impulses)" in src


# ── 4. Restore of a pinned level ──────────────────────────────────────────────


@pytest.fixture
def chem_root(tmp_path, monkeypatch):
    from brain import persona_chem

    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path)
    return tmp_path


def _write_current(root, current: dict) -> None:
    from brain import persona_chem

    d = root / "the_analyst"
    d.mkdir(parents=True, exist_ok=True)
    resting = dict(persona_chem.PERSONA_CHEMISTRY["The Analyst"])
    (d / "chemistry.json").write_text(
        json.dumps(
            {"resting": resting, "current": current, "updated": datetime.now(UTC).isoformat()}
        )
    )


def test_boot_restore_unpins_bound_levels(chem_root, monkeypatch):
    from brain import persona_chem

    monkeypatch.setitem(settings._data, "chem_restore_unpin_bounds", 1)
    _write_current(chem_root, {"DA": 1.0, "GABA": 1.0, "NE": 0.52, "ACh": 0.0})
    pair = ChemPair.fresh()
    pair.neuromod.set_baseline({"DA": 0.3, "GABA": 0.12, "ACh": 0.2})
    persona_chem.restore_current(pair, "The Analyst")
    nm = pair.neuromod
    assert nm.get("DA") == pytest.approx(0.3, abs=0.01)
    assert nm.get("GABA") == pytest.approx(0.12, abs=0.01)
    assert nm.get("ACh") == pytest.approx(0.2, abs=0.01)  # pinned at the floor too
    assert nm.get("NE") == pytest.approx(0.52, abs=0.01)  # a real mood resumes


def test_boot_restore_unpin_kill_switch(chem_root, monkeypatch):
    from brain import persona_chem

    monkeypatch.setitem(settings._data, "chem_restore_unpin_bounds", 0)
    _write_current(chem_root, {"DA": 1.0})
    pair = ChemPair.fresh()
    persona_chem.restore_current(pair, "The Analyst")
    assert pair.neuromod.get("DA") > 0.99


def test_unpin_leaves_a_channel_whose_baseline_is_the_bound():
    from brain import persona_chem

    pair = ChemPair.fresh()
    pair.hormonal.set_baseline({"CORT": 0.0})
    pair.hormonal.restore({"CORT": 0.0})
    assert "CORT" not in persona_chem.unpin_bounds(pair)


def test_returning_client_pinned_level_resumes_at_baseline(monkeypatch):
    from brain.client_chem import ClientChemRegistry, InMemoryChemStore

    monkeypatch.setitem(settings._data, "chem_restore_unpin_bounds", 1)
    box = {"t": 1000.0}
    store = InMemoryChemStore()
    reg = ClientChemRegistry(Bus(), store, persona="farrah", now_fn=lambda: box["t"])
    pair = reg.get_or_create("russ")
    pair.neuromod.restore({"DA": 1.0})
    reg.persist("russ", force=True)
    returned = ClientChemRegistry(
        Bus(), store, persona="farrah", now_fn=lambda: box["t"]
    ).get_or_create("russ")
    assert returned.neuromod.get("DA") == pytest.approx(returned.neuromod.baseline("DA"))
