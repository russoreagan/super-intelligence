"""Neuromodulator dynamics fixes from the 2026-10-19 prod learning audit.

1. 5-HT writes go to the hormonal layer (Neuromodulators has no 5HT channel).
2. DMN idle decay relaxes ACh/Glu toward the persona baseline and respects the
   dmn_enabled kill switch.
3. Boot restores the home persona's saved chemistry "current".
4. Decay for the idle gap applies before a turn's impulses, per chemistry pair.
5. Off-turn episodes record live neuromod levels, not literals.
"""

from __future__ import annotations

import ast
import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.bus import Bus, ChemPair, HormonalState, Neuromodulators
from brain.settings import settings

REPO = Path(__file__).resolve().parent.parent

# ── 1. 5-HT is hormonal ───────────────────────────────────────────────────────


def test_neuromodulators_has_no_hormonal_channels():
    nm = Neuromodulators()
    for ch in HormonalState.CHANNELS:
        with pytest.raises(KeyError):
            nm.add(ch, -0.01)


def _neuromod_receiver(node: ast.expr) -> bool:
    """True for `<x>.neuromod`, `neuromod`, `nm`, `_nm` — a Neuromodulators handle."""
    if isinstance(node, ast.Attribute):
        return node.attr == "neuromod"
    return isinstance(node, ast.Name) and node.id in ("neuromod", "nm", "_nm")


def test_no_hormonal_channel_written_through_neuromod():
    """Static guard: `.neuromod.add("5HT", …)` raised KeyError at runtime (the
    draft-quality-low path aborted the rest of the turn body)."""
    hormonal = set(HormonalState.CHANNELS)
    offenders: list[str] = []
    for path in (REPO / "brain").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add"
                and _neuromod_receiver(node.func.value)
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in hormonal
            ):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert offenders == []


# ── 2. DMN idle decay ─────────────────────────────────────────────────────────


def _dmn_on(bus):
    from brain.dmn import DefaultModeNetwork

    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._bus = bus
    return dmn


def test_idle_decay_relaxes_toward_persona_baseline(monkeypatch):
    monkeypatch.setitem(settings._data, "dmn_idle_decay_to_baseline", 1)
    bus = Bus()
    bus.neuromod.set_baseline({"ACh": 0.45, "Glu": 0.30})
    bus.neuromod.restore({"ACh": 0.85, "Glu": 0.20})
    _dmn_on(bus)._idle_decay()
    assert bus.neuromod.get("ACh") == pytest.approx(0.85 - (0.85 - 0.45) * 0.15)
    # Below its own baseline: left to the turn decay, never pulled further down.
    assert bus.neuromod.get("Glu") == pytest.approx(0.20)


def test_idle_decay_equilibrium_is_baseline_plus_bounded_excess(monkeypatch):
    """Docstring math: x = 0.85x + 0.15b + 0.02  →  x = b + 0.02/0.15."""
    monkeypatch.setitem(settings._data, "dmn_idle_decay_to_baseline", 1)
    bus = Bus()
    bus.neuromod.set_baseline({"ACh": 0.45})
    bus.neuromod.restore({"ACh": 0.45})
    dmn = _dmn_on(bus)
    for _ in range(300):
        bus.neuromod.add("ACh", 0.02)  # continuous outward thinking
        dmn._idle_decay()
    assert bus.neuromod.get("ACh") == pytest.approx(0.45 + 0.02 / 0.15 * 0.85, abs=0.01)


def test_idle_decay_legacy_floors_behind_kill_switch(monkeypatch):
    monkeypatch.setitem(settings._data, "dmn_idle_decay_to_baseline", 0)
    bus = Bus()
    bus.neuromod.set_baseline({"ACh": 0.45})
    bus.neuromod.restore({"ACh": 0.30})
    _dmn_on(bus)._idle_decay()
    assert bus.neuromod.get("ACh") == pytest.approx(0.30 - (0.30 - 0.10) * 0.15)


def _loop_dmn(monkeypatch, *, enabled: int):
    from brain.dmn import DefaultModeNetwork

    monkeypatch.setitem(settings._data, "dmn_enabled", enabled)
    monkeypatch.setattr(DefaultModeNetwork, "dormant", property(lambda self: True))
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._running = True
    dmn._skip_next_tick = False
    dmn._current_interval = lambda: 0.0
    dmn._drain_attention_focus = MagicMock()
    dmn._log_dormancy_edge = MagicMock()
    dmn._idle_decay = MagicMock()

    async def _sweep():
        dmn._running = False  # one loop iteration

    dmn._residency_sweep = _sweep
    return dmn


def test_idle_decay_skipped_when_dmn_disabled(monkeypatch):
    dmn = _loop_dmn(monkeypatch, enabled=0)
    asyncio.run(dmn._loop())
    dmn._idle_decay.assert_not_called()


def test_idle_decay_runs_when_dmn_enabled(monkeypatch):
    dmn = _loop_dmn(monkeypatch, enabled=1)
    asyncio.run(dmn._loop())
    dmn._idle_decay.assert_called_once()


# ── 3. Boot restore of chemistry "current" ────────────────────────────────────


@pytest.fixture
def chem_root(tmp_path, monkeypatch):
    from brain import persona_chem

    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path)
    return tmp_path


def _write_chem(root: Path, slug: str, current: dict, updated: str) -> None:
    from brain import persona_chem

    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    resting = dict(persona_chem.PERSONA_CHEMISTRY["The Analyst"])
    (d / "chemistry.json").write_text(
        json.dumps({"resting": resting, "current": current, "updated": updated})
    )


def test_restore_current_resumes_saved_mood(chem_root):
    from brain import persona_chem

    _write_chem(
        chem_root,
        "the_analyst",
        {"DA": 0.81, "NE": 0.52, "ACh": 0.40, "GABA": 0.30, "Glu": 0.25, "5HT": 0.33},
        datetime.now(UTC).isoformat(),
    )
    pair = ChemPair.fresh()
    levels = persona_chem.restore_current(pair, "The Analyst")
    assert levels is not None
    assert pair.neuromod.get("DA") == pytest.approx(0.81, abs=0.01)
    assert pair.neuromod.get("NE") == pytest.approx(0.52, abs=0.01)
    assert pair.hormonal.get("5HT") == pytest.approx(0.33, abs=0.01)


def test_restore_current_decays_for_time_away_capped(chem_root, monkeypatch):
    from brain import persona_chem

    monkeypatch.setitem(settings._data, "decay_max_turns", 10.0)
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    _write_chem(chem_root, "the_analyst", {"DA": 0.90}, old)
    pair = ChemPair.fresh()
    pair.neuromod.set_baseline({"DA": 0.30})
    persona_chem.restore_current(pair, "The Analyst")
    # Three days away decays like the longest gap a turn applies (10 turns), not 4320.
    r = Neuromodulators._DEF_DECAY["DA"] ** 10
    assert pair.neuromod.get("DA") == pytest.approx(0.30 + (0.90 - 0.30) * r, abs=1e-6)


def test_restore_current_clamps_out_of_range(chem_root):
    from brain import persona_chem

    _write_chem(chem_root, "the_analyst", {"DA": 7.0, "NE": -3.0}, datetime.now(UTC).isoformat())
    pair = ChemPair.fresh()
    persona_chem.restore_current(pair, "The Analyst")
    assert pair.neuromod.get("DA") <= 1.0
    assert pair.neuromod.get("NE") >= Neuromodulators._HARD_MIN["NE"]


def test_restore_current_without_file_is_a_noop(chem_root):
    from brain import persona_chem

    pair = ChemPair.fresh()
    before = pair.snapshot()
    assert persona_chem.restore_current(pair, "The Analyst") is None
    assert pair.snapshot() == before
    assert not (chem_root / "the_analyst" / "chemistry.json").exists()  # no seed write


def test_setup_restores_home_persona_on_resting_pair(chem_root, monkeypatch):
    from brain.session_setup import _SetupMixin

    _write_chem(chem_root, "the_analyst", {"DA": 0.77}, datetime.now(UTC).isoformat())
    monkeypatch.setitem(settings._data, "persona_name", "The Analyst")
    monkeypatch.setitem(settings._data, "chem_restore_current_at_boot", 1)
    host = MagicMock()
    host.bus = Bus()
    _SetupMixin._restore_home_chemistry(host)
    assert host.bus.resting_chem.neuromod.get("DA") == pytest.approx(0.77, abs=0.01)

    monkeypatch.setitem(settings._data, "chem_restore_current_at_boot", 0)
    host.bus = Bus()
    init = host.bus.neuromod.get("DA")
    _SetupMixin._restore_home_chemistry(host)
    assert host.bus.neuromod.get("DA") == init


# ── 4. Decay before impulses, per pair ────────────────────────────────────────


def _warm():
    return {
        "sentiment": 0.8,
        "hostility": 0.0,
        "salience": 0.5,
        "surprise_score": 0.1,
        "intent": "question",
        "topic_summary": "friendly chat",
    }


def _turn_after_gap(monkeypatch, *, decay_first: int, gap_s: float) -> float:
    from brain.clusters.hypothalamus import HypothalamusCluster

    monkeypatch.setitem(settings._data, "chem_decay_before_impulses", decay_first)
    bus = Bus()
    bus.neuromod.restore({"DA": 0.60})
    hyp = HypothalamusCluster(bus)
    hyp._last_decay_time = time.monotonic() - gap_s
    asyncio.run(hyp.process(_warm()))
    hyp.decay_turn()
    return bus.neuromod.get("DA")


def test_turn_impulses_survive_the_pre_turn_gap(monkeypatch):
    """Old order: (L + d − b)·r^t + b. New: (L − b)·r^t + b + d. The difference is
    d·(1 − r^t) — this turn's appraisal no longer shrinks by the gap before it."""
    new = _turn_after_gap(monkeypatch, decay_first=1, gap_s=600)
    old = _turn_after_gap(monkeypatch, decay_first=0, gap_s=600)
    assert new > old + 0.01


def test_decay_turn_does_not_decay_when_decay_first(monkeypatch):
    from brain.clusters.hypothalamus import HypothalamusCluster

    monkeypatch.setitem(settings._data, "chem_decay_before_impulses", 1)
    bus = Bus()
    hyp = HypothalamusCluster(bus)
    asyncio.run(hyp.process(_warm()))
    snap = bus.neuromod.snapshot()
    hyp.decay_turn()
    assert bus.neuromod.snapshot() == snap  # the end-of-turn snapshot is the turn's state


def test_one_lane_turn_does_not_reset_another_lanes_gap(monkeypatch):
    from brain.clusters.hypothalamus import HypothalamusCluster

    monkeypatch.setitem(settings._data, "chem_decay_before_impulses", 1)
    max_turns = float(settings.get("decay_max_turns"))
    bus = Bus()
    hyp = HypothalamusCluster(bus)
    a, b = ChemPair.fresh(), ChemPair.fresh()
    b.decay_clock["last"] = time.monotonic() - 3600  # customer B last spoke an hour ago

    with bus.bind(a):  # customer A speaks now
        asyncio.run(hyp.process(_warm()))
        hyp.decay_turn()
    assert a.decay_clock["turns"] == pytest.approx(float(settings.get("decay_min_turns")))

    with bus.bind(b):
        asyncio.run(hyp.process(_warm()))
    assert b.decay_clock["turns"] == pytest.approx(max_turns)


# ── 5. Live snapshots on off-turn episodes ────────────────────────────────────


def _live_bus() -> Bus:
    bus = Bus()
    bus.neuromod.restore({"DA": 0.71, "ACh": 0.22, "GABA": 0.33, "Glu": 0.44, "NE": 0.55})
    return bus


@pytest.mark.parametrize(
    "method,args",
    [
        ("encode_idle_thought", ("s", "a musing", 0.4)),
        ("encode_deferred_question", ("s", "what next?")),
        ("encode_conclusion", ("s", "settled")),
    ],
)
def test_hippocampus_offturn_episodes_record_live_levels(method, args):
    from brain.clusters.hippocampus import HippocampusCluster

    hip = HippocampusCluster.__new__(HippocampusCluster)
    hip._bus = _live_bus()
    hip._episodic = MagicMock()
    asyncio.run(getattr(hip, method)(*args))
    ep = hip._episodic.encode.call_args[0][0]
    assert ep.neuromod_snapshot == hip._bus.neuromod.snapshot()
    assert ep.neuromod_snapshot["NE"] == pytest.approx(0.55)


def test_sleep_conclusion_records_live_levels():
    from brain.sleep import SleepConsolidation

    sl = SleepConsolidation.__new__(SleepConsolidation)
    sl._bus = _live_bus()
    sl._router = MagicMock()
    sl._router.embed = AsyncMock(return_value=None)
    sl._episodic = MagicMock()
    asyncio.run(sl._encode_conclusion("an insight"))
    ep = sl._episodic.encode.call_args[0][0]
    assert ep.neuromod_snapshot == sl._bus.neuromod.snapshot()


def test_trading_journal_episode_records_live_levels():
    from brain.clusters.trading.journal import _safe_encode_episode

    hip = MagicMock(spec=["encode", "_bus"])
    hip._bus = _live_bus()
    _safe_encode_episode(hip, user_input="buy", entity_response="ok")
    ep = hip.encode.call_args[0][0]
    assert ep.neuromod_snapshot["NE"] == pytest.approx(0.55)
