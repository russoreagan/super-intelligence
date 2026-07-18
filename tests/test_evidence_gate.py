"""
Unit tests for EvidenceGate — bounded evidence accumulation with hysteresis and
reward-modulated drift learning (drift-diffusion / sequential sampling).

Covers:
  * temporal summation (sub-threshold nudges eventually commit)
  * half-life leak (evidence fades over wall-clock time)
  * hysteresis band (arm high, hold, release low — no chatter)
  * fired_edge fire-once debounce
  * refractory on fire_reset mode
  * chemistry-modulated commit bound (gain control reused)
  * scalar mode (habituation-style leaky level, the satiation use)
  * cue-weight drift learning: confirmed commit strengthens its cues, refuted weakens
  * anti-farming: sub-confidence / uninformative resolve moves nothing and pays 0 DA
  * external vs self confirmation: external gets more plasticity; DA tally is audited
  * per-client isolation via the store round-trip
  * transient snapshot/restore
"""

from __future__ import annotations

import pytest

from brain.bus import Bus
from brain.evidence_gate import MODE_FIRE_RESET, MODE_LATCH, EvidenceGate


def _gate(**kw):
    kw.setdefault("arm_threshold", 1.0)
    kw.setdefault("release_ratio", 0.5)
    kw.setdefault("half_life_s", 100.0)
    return EvidenceGate(name="test_gate", cluster="test", **kw)


# ── accumulate / leak ─────────────────────────────────────────────────────────


def test_temporal_summation_commits_when_no_single_obs_would():
    g = _gate()
    # Three sub-threshold nudges (each 0.4 < 1.0) at the same instant sum to 1.2.
    assert g.observe(0.4, now=0.0) is None and not g.armed
    assert g.observe(0.4, now=0.0) is None and not g.armed
    payload = g.observe(0.4, now=0.0)
    assert g.armed
    assert payload is not None  # commit emitted a switch activation
    assert g.level == pytest.approx(1.2, abs=1e-9)


def test_half_life_leak_fades_evidence():
    g = _gate(half_life_s=10.0)
    g.observe(0.8, now=0.0)
    assert g.level == pytest.approx(0.8, abs=1e-9)
    # one half-life later, no new evidence → level halves
    assert g.peek(now=10.0) == pytest.approx(0.4, abs=1e-9)
    # two half-lives from start
    assert g.peek(now=20.0) == pytest.approx(0.2, abs=1e-9)


def test_leak_lets_a_slow_signal_never_reach_bound():
    g = _gate(arm_threshold=1.0, half_life_s=1.0)
    # Feed 0.4 once per half-life; each prior contribution halves before the next.
    # Steady-state ceiling = 0.4 / (1 - 0.5) = 0.8 < 1.0, so it never arms.
    armed_ever = False
    t = 0.0
    for _ in range(50):
        g.observe(0.4, now=t)
        armed_ever = armed_ever or g.armed
        t += 1.0
    assert not armed_ever
    assert g.level < 0.85


# ── hysteresis ────────────────────────────────────────────────────────────────


def test_hysteresis_holds_between_arm_and_release():
    g = _gate(arm_threshold=1.0, release_ratio=0.5, half_life_s=1e9)  # no leak
    g.observe(1.0, now=0.0)
    assert g.armed
    # decay the level into the band (0.5, 1.0) by... we can't decay w/o leak, so
    # instead push it down with nothing and simulate partial: use a fresh gate.
    g2 = _gate(arm_threshold=1.0, release_ratio=0.5, half_life_s=10.0)
    g2.observe(1.0, now=0.0)
    assert g2.armed
    # at t=10 level=0.5 → exactly release bound → releases (<=)
    g2.observe(0.0, now=10.0)
    assert not g2.armed


def test_stays_armed_through_a_single_contrary_dip_in_the_band():
    g = _gate(arm_threshold=1.0, release_ratio=0.4, half_life_s=20.0)
    g.observe(1.2, now=0.0)
    assert g.armed
    # a short gap decays it into the band but not below release (0.4) → still armed
    lvl = g.peek(now=10.0)
    assert 0.4 < lvl < 1.0
    g.observe(0.0, now=10.0)
    assert g.armed  # hysteresis: did not chatter off


def test_fired_edge_is_fire_once():
    g = _gate()
    g.observe(1.0, now=0.0)
    assert g.fired_edge() is True
    assert g.fired_edge() is False  # consumed


# ── refractory (fire_reset) ───────────────────────────────────────────────────


def test_fire_reset_resets_and_refractory_blocks_immediate_recommit():
    g = _gate(mode=MODE_FIRE_RESET, refractory_s=5.0, half_life_s=1e9)
    p1 = g.observe(1.0, now=0.0)
    assert p1 is not None
    assert g.level == 0.0 and not g.armed  # reset
    # within refractory, even enough evidence does not re-commit
    p2 = g.observe(1.0, now=2.0)
    assert p2 is None
    # after refractory it can commit again
    p3 = g.observe(1.0, now=6.0)
    assert p3 is not None


# ── chemistry modulation of the bound ─────────────────────────────────────────


def test_chemistry_raises_commit_bound():
    # GABA (threat) coeff +0.5 → high GABA raises the bound, so the same evidence
    # that would commit under neutral chemistry does not under threat.
    g = _gate(arm_threshold=1.0, modulators={"GABA": +0.5})
    # neutral: bound = 1.0; evidence 1.0 commits
    g_neu = _gate(arm_threshold=1.0, modulators={"GABA": +0.5})
    assert g_neu.observe(1.0, snapshot={"GABA": 0.5}, now=0.0) is not None
    # high GABA (1.0): shift = +0.5*(1.0-0.5)=+0.25 → bound 1.25; evidence 1.0 holds off
    assert g.observe(1.0, snapshot={"GABA": 1.0}, now=0.0) is None
    assert not g.armed


# ── scalar (habituation) mode ─────────────────────────────────────────────────


def test_scalar_mode_is_a_leaky_level_no_cues():
    g = _gate(cue_names=(), half_life_s=10.0)
    g.observe(0.5, now=0.0)
    g.observe(0.3, now=0.0)
    assert g.level == pytest.approx(0.8, abs=1e-9)
    assert g.peek(now=10.0) == pytest.approx(0.4, abs=1e-9)
    assert g.cue_weights() == {}  # no learning surface in scalar mode


# ── learning ──────────────────────────────────────────────────────────────────


def _cue_gate():
    return EvidenceGate(
        name="avoidance",
        cluster="frontal",
        arm_threshold=1.0,
        half_life_s=1e9,
        cue_names=("deflect", "topic_change"),
    )


def test_confirmed_external_commit_strengthens_its_cues():
    bus = Bus()
    g = _cue_gate()
    # commit driven by both cues
    g.observe({"deflect": 0.6, "topic_change": 0.6}, now=0.0)
    assert g.armed
    before = g.cue_weights()
    da = g.resolve(correct=True, informativeness=1.0, bus=bus, external=True)
    after = g.cue_weights()
    assert da > 0  # confirmed prediction paid positive DA
    assert after["deflect"] > before["deflect"]
    assert after["topic_change"] > before["topic_change"]


def test_refuted_commit_weakens_its_cues():
    bus = Bus()
    g = _cue_gate()
    g.observe({"deflect": 0.7, "topic_change": 0.5}, now=0.0)
    assert g.armed
    before = g.cue_weights()
    da = g.resolve(correct=False, informativeness=1.0, bus=bus, external=True)
    after = g.cue_weights()
    assert da < 0  # a confident wrong call is a loss
    assert after["deflect"] < before["deflect"]


def test_anti_farm_uninformative_resolve_moves_nothing():
    bus = Bus()
    g = _cue_gate()
    g.observe({"deflect": 0.6, "topic_change": 0.6}, now=0.0)
    before = g.cue_weights()
    # informativeness below the gate → prediction_reward returns 0 → no learning, no DA
    da = g.resolve(correct=True, informativeness=0.0, bus=bus, external=True)
    assert da == 0.0
    assert g.cue_weights() == before


def test_external_confirmation_gets_more_plasticity_than_self():
    bus = Bus()
    g_ext = _cue_gate()
    g_self = _cue_gate()
    for g in (g_ext, g_self):
        g.observe({"deflect": 0.6, "topic_change": 0.6}, now=0.0)
    g_ext.resolve(correct=True, informativeness=1.0, bus=bus, external=True)
    g_self.resolve(correct=True, informativeness=1.0, bus=bus, external=False)
    # same confirmation, external moves the weight more (grounded signal weighted up)
    assert (g_ext.cue_weights()["deflect"] - 1.0) > (g_self.cue_weights()["deflect"] - 1.0)


def test_da_tally_audits_provenance():
    bus = Bus()
    g = _cue_gate()
    g.observe({"deflect": 0.8, "topic_change": 0.8}, now=0.0)
    g.resolve(correct=True, informativeness=1.0, bus=bus, external=True)
    tally = bus.da_source_tally()
    assert tally["external"] > 0
    # a self-graded resolve lands in the intrinsic bucket, not external
    g2 = _cue_gate()
    g2.observe({"deflect": 0.8, "topic_change": 0.8}, now=0.0)
    g2.resolve(correct=True, informativeness=1.0, bus=bus, external=False)
    assert bus.da_source_tally()["intrinsic"] > 0


# ── per-client isolation + serialization ──────────────────────────────────────


def test_store_backed_state_is_per_client_isolated():
    g = _gate(half_life_s=1e9)  # one process-global gate object
    store_a: dict = {}
    store_b: dict = {}
    g.observe(0.6, now=0.0, store=store_a)
    g.observe(0.6, now=0.0, store=store_a)  # A: 1.2 → armed
    g.observe(0.3, now=0.0, store=store_b)  # B: 0.3 → not armed
    assert store_a["test_gate"]["armed"] is True
    assert store_b["test_gate"]["armed"] is False
    assert store_a["test_gate"]["level"] == pytest.approx(1.2, abs=1e-9)
    assert store_b["test_gate"]["level"] == pytest.approx(0.3, abs=1e-9)


def test_snapshot_restore_roundtrip():
    g = _gate(half_life_s=50.0)
    g.observe(0.7, now=0.0)
    snap = g.snapshot()
    g2 = _gate(half_life_s=50.0)
    g2.restore(snap)
    assert g2.level == pytest.approx(0.7, abs=1e-9)
    assert g2.peek(now=50.0) == pytest.approx(0.35, abs=1e-9)


def test_chempair_carries_evidence_through_snapshot():
    from brain.bus import ChemPair

    pair = ChemPair.fresh()
    g = _gate(half_life_s=1e9)
    g.observe(0.9, now=0.0, store=pair.evidence)
    snap = pair.snapshot()
    assert "evidence" in snap and snap["evidence"]["test_gate"]["level"] == pytest.approx(0.9)
    # a fresh pair restored from the snapshot resumes the accumulator
    pair2 = ChemPair.fresh()
    pair2.restore(snap)
    assert pair2.evidence["test_gate"]["level"] == pytest.approx(0.9)


# ── satiation reframe: the one live, flag-gated call site ──────────────────────


def test_satiation_flag_off_uses_statefulswitch_unchanged(monkeypatch):
    from brain.clusters.hypothalamus import HypothalamusCluster
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "evidence_gates", 0)
    h = HypothalamusCluster(Bus())
    h._satiation_update(0.05)
    h._satiation_update(0.05)
    # flag-off writes the StatefulSwitch, leaves the gate untouched
    assert h._satiation_inhibitor.state == pytest.approx(0.10, abs=1e-9)
    assert h._satiation_read() == pytest.approx(0.10, abs=1e-9)
    assert not h._bus.evidence  # gate store never populated


def test_satiation_flag_on_uses_gate_and_relaxes_over_idle_time(monkeypatch):
    from brain.clusters.hypothalamus import HypothalamusCluster
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "evidence_gates", 1)
    monkeypatch.setitem(settings._data, "satiation_half_life_s", 100.0)
    h = HypothalamusCluster(Bus())
    # accumulate habituation; flag-on writes the gate (per-client store), not the switch
    h._satiation_gate.observe(0.8, now=0.0, store=h._bus.evidence)
    assert h._bus.evidence["satiation_inhibitor"]["level"] == pytest.approx(0.8, abs=1e-9)
    assert h._satiation_inhibitor.state == 0.0  # legacy switch untouched
    # the RFC's fix: idle time RELAXES satiation (dead tick() is gone). Read after a
    # half-life shows decay the StatefulSwitch could never produce on its own.
    assert h._satiation_gate.peek(now=100.0, store=h._bus.evidence) == pytest.approx(0.4, abs=1e-9)
