"""
Tier 2 structural plasticity — reserve node recruitment + demotion.

Covers the dormant reserve pool, per-persona recruitment of a reserve drafter when a host has a
stable proven fragment cluster, fragment-based demotion, the node-registry reconcile with a
recruited reserve, and the kill-switches.
"""

from __future__ import annotations

import importlib

from brain.wiring import Wiring
from brain.wiring_bootstrap import bootstrap


def _isolated_wiring(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_WIRING_PATH", str(tmp_path / "wiring.json"))
    monkeypatch.setenv("BRAIN_WIRING_HISTORY_DIR", str(tmp_path / "history"))
    import brain.wiring as w_mod

    importlib.reload(w_mod)
    return w_mod.Wiring()


class _Router:
    async def call(self, *a, **kw):
        return "{}"

    def supports(self, *a, **kw):
        return True

    async def embed(self, *a, **kw):
        return [0.0] * 768


def _hermetic_wiring(tmp_path, monkeypatch):
    import brain.wiring as _wiring_mod

    monkeypatch.setattr(_wiring_mod, "WIRING_PATH", tmp_path / "wiring.json", raising=False)
    return Wiring()


def _build_frontal(tmp_path, monkeypatch, reserve=3):
    from brain.brainstem import Brainstem
    from brain.bus import Bus
    from brain.clusters.frontal import FrontalCluster
    from brain.clusters.temporal import TemporalCluster
    from brain.node_registry import get_node_registry, register_manifest
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "node_reserve_pool", reserve)
    get_node_registry().clear()
    w = _hermetic_wiring(tmp_path, monkeypatch)
    bootstrap(w)
    bus = Bus()
    router = _Router()
    brainstem = Brainstem(bus, router)
    # Construct temporal too so its object-backed nodes register (the full graph then reconciles).
    TemporalCluster(bus, router, wiring=w)
    frontal = FrontalCluster(bus, brainstem, router, wiring=w)
    register_manifest(get_node_registry())
    return frontal, w


# ── reserve pool + eligibility ───────────────────────────────────────────────


def test_reserve_pool_created_dormant(tmp_path, monkeypatch):
    from brain.clusters.frontal_prompts import RESERVE_DRAFTER_SYSTEM

    frontal, _w = _build_frontal(tmp_path, monkeypatch, reserve=3)
    assert len(frontal._drafters) == 8  # 5 fixed + 3 reserve
    assert frontal._n_fixed_drafters == 5
    # reserve slots carry the reserve base prompt; fixed slots do not
    assert frontal._drafters[5].system_prompt == RESERVE_DRAFTER_SYSTEM
    assert frontal._drafters[0].system_prompt != RESERVE_DRAFTER_SYSTEM


def test_unrecruited_reserves_not_eligible(tmp_path, monkeypatch):
    frontal, _w = _build_frontal(tmp_path, monkeypatch, reserve=3)
    picked = frontal._select_drafters(8, "t")  # ask for all
    assert all(i < 5 for i in picked)  # only the fixed drafters fire


def test_recruited_reserve_becomes_eligible(tmp_path, monkeypatch):
    frontal, w = _build_frontal(tmp_path, monkeypatch, reserve=3)
    w.add("frontal.executive", "frontal.drafter_F", weight=1.0)  # recruit F
    picked = frontal._select_drafters(8, "t")
    assert 5 in picked  # drafter_F (index 5) now eligible
    assert 6 not in picked and 7 not in picked  # G/H still dormant


# ── recruitment + demotion (Hebbian) ─────────────────────────────────────────


def _proven(w, host, ids, weight=2.5):
    for sid in ids:
        w.add(f"fragment.{sid}", host, weight=weight)


def test_recruitment_crystallizes_proven_cluster(monkeypatch, tmp_path):
    from brain.hebbian import HebbianUpdater

    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    _proven(w, "frontal.drafter_A", ["alpha", "beta"])
    HebbianUpdater(w)._maybe_recruit_nodes("s")
    # a reserve is wired in and carries the copied proven fragments
    assert w.has("frontal.executive", "frontal.drafter_F")
    assert w.has("frontal.drafter_F", "frontal.critic")
    assert w.has("hypothalamus.threat_to_GABA", "frontal.drafter_F")
    assert set(dict(w.attached_fragments("frontal.drafter_F"))) == {"alpha", "beta"}


def test_recruitment_one_per_pass_and_dedup(monkeypatch, tmp_path):
    from brain.hebbian import HebbianUpdater

    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    h = HebbianUpdater(w)
    _proven(w, "frontal.drafter_A", ["alpha", "beta"])
    h._maybe_recruit_nodes("s1")
    assert w.has("frontal.executive", "frontal.drafter_F")
    # same cluster still proven → NOT re-recruited into another slot
    h._maybe_recruit_nodes("s2")
    assert not w.has("frontal.executive", "frontal.drafter_G")
    # a DIFFERENT proven cluster → a second reserve is recruited
    _proven(w, "frontal.drafter_C", ["gamma", "delta"])
    h._maybe_recruit_nodes("s3")
    assert w.has("frontal.executive", "frontal.drafter_G")


def test_demotion_on_lost_specialization(monkeypatch, tmp_path):
    from brain.hebbian import HebbianUpdater

    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    h = HebbianUpdater(w)
    _proven(w, "frontal.drafter_A", ["alpha", "beta"])
    h._maybe_recruit_nodes("s1")
    assert w.has("frontal.executive", "frontal.drafter_F")
    # fade the whole cluster (F's copies AND drafter_A's originals) below the inject threshold
    for k in list(w._edges):
        if k[0] in ("fragment.alpha", "fragment.beta"):
            w._edges[k].weight = 1.1
    h._maybe_recruit_nodes("s2")
    assert not w.has("frontal.executive", "frontal.drafter_F")  # demoted, returned to the pool
    assert w.attached_fragments("frontal.drafter_F") == []


def test_recruitment_gated_by_flag_and_frozen(monkeypatch, tmp_path):
    from brain.hebbian import HebbianUpdater
    from brain.settings import settings

    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    _proven(w, "frontal.drafter_A", ["alpha", "beta"])
    monkeypatch.setitem(settings._data, "node_recruitment", 0)
    HebbianUpdater(w)._maybe_recruit_nodes("s")
    assert not w.has("frontal.executive", "frontal.drafter_F")
    monkeypatch.setitem(settings._data, "node_recruitment", 1)
    monkeypatch.setenv("BRAIN_WIRING_FROZEN", "true")
    HebbianUpdater(w)._maybe_recruit_nodes("s")
    assert not w.has("frontal.executive", "frontal.drafter_F")


# ── ALTERNATIVE trigger: sustained Global-Workspace ignition ─────────────────


def _arm_ignition(n=4):
    """Record n ignited turns (4, not 3: continuous decay leaves 3×+1 fractionally
    under the 3.0 floor by read time). Flag ships ON, so record() is live."""
    from brain import ignition_tally

    for _ in range(n):
        ignition_tally.record("threat")


def test_ignition_recruits_relaxed_cluster(monkeypatch, tmp_path):
    from brain.hebbian import HebbianUpdater

    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    # Established but NOT fully proven: 1.9 sits between the 1.75 midpoint and 2.2.
    _proven(w, "frontal.drafter_A", ["alpha", "beta"], weight=1.9)
    HebbianUpdater(w)._maybe_recruit_nodes("s0")
    assert not w.has("frontal.executive", "frontal.drafter_F")  # no pressure → no recruit
    _arm_ignition()
    HebbianUpdater(w)._maybe_recruit_nodes("s1")
    assert w.has("frontal.executive", "frontal.drafter_F")
    assert set(dict(w.attached_fragments("frontal.drafter_F"))) == {"alpha", "beta"}


def test_ignition_flag_off_no_recruit(monkeypatch, tmp_path):
    from brain.hebbian import HebbianUpdater
    from brain.settings import settings

    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    _proven(w, "frontal.drafter_A", ["alpha", "beta"], weight=1.9)
    _arm_ignition()  # pressure recorded while the switch was on…
    monkeypatch.setitem(settings._data, "node_recruit_from_ignition", 0)
    HebbianUpdater(w)._maybe_recruit_nodes("s")
    assert not w.has("frontal.executive", "frontal.drafter_F")


def test_ignition_respects_frozen(monkeypatch, tmp_path):
    from brain.hebbian import HebbianUpdater

    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    _proven(w, "frontal.drafter_A", ["alpha", "beta"], weight=1.9)
    _arm_ignition()
    monkeypatch.setenv("BRAIN_WIRING_FROZEN", "true")
    HebbianUpdater(w)._maybe_recruit_nodes("s")
    assert not w.has("frontal.executive", "frontal.drafter_F")


def test_ignition_bar_above_inject_threshold(monkeypatch, tmp_path):
    """Anti-churn invariant: the relaxed bar is the inject/promote MIDPOINT (1.75),
    not the inject threshold itself — a 1.5 cluster stays unrecruited even under
    full ignition pressure, so a fresh recruit can never sit at the demotion floor."""
    from brain.hebbian import HebbianUpdater

    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    _proven(w, "frontal.drafter_A", ["alpha", "beta"], weight=1.5)
    _arm_ignition()
    HebbianUpdater(w)._maybe_recruit_nodes("s")
    assert not w.has("frontal.executive", "frontal.drafter_F")


def test_full_cluster_wins_same_pass(monkeypatch, tmp_path):
    from brain import ignition_tally
    from brain.hebbian import HebbianUpdater

    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    _proven(w, "frontal.drafter_A", ["alpha", "beta"], weight=2.5)  # fully proven
    _proven(w, "frontal.drafter_C", ["gamma", "delta"], weight=1.9)  # only established
    _arm_ignition()
    HebbianUpdater(w)._maybe_recruit_nodes("s")
    # Exactly one recruit per pass, and the proven-cluster path takes precedence.
    assert set(dict(w.attached_fragments("frontal.drafter_F"))) == {"alpha", "beta"}
    assert not w.has("frontal.executive", "frontal.drafter_G")
    # The full path must NOT consume the ignition tally.
    assert ignition_tally.pressure()[0] > 3.0


def test_ignition_consume_prevents_repeat(monkeypatch, tmp_path):
    from brain.hebbian import HebbianUpdater

    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    h = HebbianUpdater(w)
    _proven(w, "frontal.drafter_A", ["alpha", "beta"], weight=1.9)
    _arm_ignition()
    h._maybe_recruit_nodes("s1")
    assert w.has("frontal.executive", "frontal.drafter_F")
    # A second established cluster, but the recruit consumed the whole window —
    # no fresh ignitions, no second recruit.
    _proven(w, "frontal.drafter_C", ["gamma", "delta"], weight=1.9)
    h._maybe_recruit_nodes("s2")
    assert not w.has("frontal.executive", "frontal.drafter_G")
    # Fresh sustained ignition re-arms the path.
    _arm_ignition()
    h._maybe_recruit_nodes("s3")
    assert w.has("frontal.executive", "frontal.drafter_G")


def test_stale_tally_no_recruit(monkeypatch, tmp_path):
    from brain import ignition_tally
    from brain.hebbian import HebbianUpdater
    from brain.settings import settings

    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    _proven(w, "frontal.drafter_A", ["alpha", "beta"], weight=1.9)
    t0 = 1_000_000.0
    monkeypatch.setattr(ignition_tally, "_now", lambda: t0)
    _arm_ignition()
    hl_s = float(settings.get("ignition_tally_half_life_h", 72.0)) * 3600.0
    monkeypatch.setattr(ignition_tally, "_now", lambda: t0 + 3 * hl_s)  # 4 → 0.5
    HebbianUpdater(w)._maybe_recruit_nodes("s")
    assert not w.has("frontal.executive", "frontal.drafter_F")


def test_ignition_respects_admissibility(monkeypatch, tmp_path):
    from brain.hebbian import HebbianUpdater

    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    _proven(w, "frontal.drafter_A", ["evil", "alpha"], weight=1.9)
    _arm_ignition()
    monkeypatch.setattr(
        "brain.fragment_pool.is_admissible", lambda sid, host: sid != "evil"
    )
    # Admissible cluster shrinks to 1 < node_promote_min_cluster → no recruit.
    HebbianUpdater(w)._maybe_recruit_nodes("s")
    assert not w.has("frontal.executive", "frontal.drafter_F")


# ── node-registry reconcile with a recruited reserve ─────────────────────────


def test_recruited_reserve_reconciles_clean(tmp_path, monkeypatch):
    from brain.node_registry import audit_node_registry, get_node_registry

    frontal, w = _build_frontal(tmp_path, monkeypatch, reserve=3)
    reg = get_node_registry()
    from brain.node_registry import register_manifest

    register_manifest(reg)
    # unrecruited reserves must not be registered (would show as UNWIRED)
    assert reg.classify("frontal.drafter_F") is None
    report_before = audit_node_registry(w, reg)
    assert report_before["unwired"] == []

    # recruit F (wire it in), then register recruited reserves
    from brain.hebbian import HebbianUpdater

    _proven(w, "frontal.drafter_A", ["alpha", "beta"])
    HebbianUpdater(w)._maybe_recruit_nodes("s")
    frontal.register_recruited_reserves(reg)
    assert reg.classify("frontal.drafter_F") == "cell"
    # fragment.* endpoints must also be classified for a clean audit
    from brain.node_registry import register_fragment_nodes

    register_fragment_nodes(w, reg)
    report = audit_node_registry(w, reg)
    assert report["orphans"] == []
    assert report["unwired"] == []
