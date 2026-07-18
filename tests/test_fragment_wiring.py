"""
Tier 1 structural plasticity — learned-attached fragments.

Covers the representation (fragment nodes + attachment edges), the contrastive
sleep-time learning, the drafter injection/exploration consumer, the local RunPod
downshift, safety-by-construction, per-persona isolation, and the kill-switch.
"""

from __future__ import annotations

import importlib
import time

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────


def _pod_ready(monkeypatch, ready: int = 1, age_s: float = 0.0):
    """Publish the downshift liveness flag the way RunPodManager does: the flag
    plus its freshness stamp (frontal._local_available treats an un-stamped or
    stale flag as unavailable)."""
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "runpod_pod_ready", ready)
    monkeypatch.setitem(settings._data, "runpod_pod_ready_at", time.time() - age_s)


def _isolated_wiring(monkeypatch, tmp_path):
    """Wiring instance whose JSON persistence is isolated to tmp_path."""
    monkeypatch.setenv("BRAIN_WIRING_PATH", str(tmp_path / "wiring.json"))
    monkeypatch.setenv("BRAIN_WIRING_HISTORY_DIR", str(tmp_path / "history"))
    import brain.wiring as w_mod

    importlib.reload(w_mod)
    return w_mod.Wiring()


class _StubSchema:
    async def aappend_fact(self, *a, **kw):
        pass

    def read(self, name):
        return ""

    async def awrite(self, name, content):
        pass


class _StubEpisodic:
    def encode(self, ep):
        pass

    def recall(self, vec, limit=4):
        return []

    def recall_recent(self, limit=6):
        return []


class _StubRouter:
    def __init__(self):
        self._call_log = []

    async def call(self, *a, **kw):
        return "{}"

    async def embed(self, text):
        return [0.0] * 16


class _CaptureDecisions:
    def __init__(self):
        self.records = []

    def log(self, decision, *, turn_id="", cluster="", **fields):
        rec = {"decision": decision, "turn_id": turn_id, "cluster": cluster, **fields}
        self.records.append(rec)
        return rec

    def of(self, decision):
        return [r for r in self.records if r["decision"] == decision]


def _sc(monkeypatch, tmp_path, wiring):
    from brain.sleep import SleepConsolidation

    return SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=wiring)


def _win_trace(
    turn_id="t",
    *,
    winner_idx=2,
    drafter_fragments=None,
    DA=0.9,
    prior_DA=0.5,
    critic_overall=0.95,
    persona_name="",
):
    """A positive-outcome turn whose SELECTED draft is drafter `winner_idx`."""
    from brain.observability.timeline import TurnTrace

    winner_letter = chr(65 + winner_idx)
    t = TurnTrace(turn_id=turn_id, session_id="s", user_input="x")
    t.fired_path = [
        {"name": "frontal.executive", "cluster": "frontal", "kind": "integrator"},
        {"name": f"frontal.drafter_{winner_letter}", "cluster": "frontal", "kind": "integrator"},
    ]
    t.neuromod = {"DA": DA, "GABA": 0.0, "ACh": 0.3, "Glu": 0.3}
    t.prior_neuromod = {"DA": prior_DA, "GABA": 0.0, "ACh": 0.3, "Glu": 0.3}
    t.draft_scores = [
        {
            "draft_id": f"draft_{winner_idx}_{turn_id}",
            "overall": critic_overall,
            "selected": True,
            "critic_ran": True,
        },
        {
            "draft_id": f"draft_0_{turn_id}",
            "overall": 0.6,
            "selected": False,
            "critic_ran": True,
        },
    ]
    t.emotion = "content"
    t.user_emotion = ""
    t.persona_name = persona_name
    t.drafter_fragments = drafter_fragments or {}
    return t


class _FakeSelector:
    def __init__(self, bodies, partners):
        self._b = dict(bodies)
        self._p = set(partners)

    def native_skill_body(self, n):
        return self._b.get(n, "")

    def is_partner_skill(self, n):
        return n in self._p

    def attachable_fragment_ids(self):
        return [n for n in self._b if n in self._p]


def _frontal(wiring, *, selector=None, router=None, frozen=False, bundle=None):
    """A FrontalCluster shell with only the attributes the fragment helpers touch."""
    from brain.clusters.frontal import FrontalCluster
    from brain.clusters.skill_selector import SkillBundle

    f = FrontalCluster.__new__(FrontalCluster)
    f._wiring = wiring
    f._wiring_frozen = frozen
    f._skill_selector = selector
    f._router = router
    f._current_skill_bundle = bundle if bundle is not None else SkillBundle(tier1=[], chosen=[])
    f._record_trace_bypass = lambda: None  # type: ignore[method-assign]
    return f


class _FakeRouter:
    def __init__(self, lite=False):
        self._local_disabled = lite


# ── fragment_pool: admissibility / safety ────────────────────────────────────


def test_admissible_hosts_and_safety_rejection():
    from brain.fragment_pool import fragment_node_name, is_admissible

    assert is_admissible("x", "frontal.drafter_A")
    assert is_admissible("x", "frontal.critic")
    assert is_admissible("x", "frontal.executive")
    # Safety nodes are never hosts.
    assert not is_admissible("x", "temporal.integrator_inhibitor")
    assert not is_admissible("x", "motor_cortex.tool_planner")
    assert not is_admissible("x", "hypothalamus.threat_to_GABA")
    # Not on the allowlist at all.
    assert not is_admissible("x", "hippocampus.recall")
    assert fragment_node_name("trading-analyst") == "fragment.trading-analyst"


def test_registered_switch_is_rejected_even_if_allowlisted(monkeypatch):
    """Belt-and-suspenders: a node the registry classifies as a switch is rejected
    even if wrongly present on HOST_RECEPTORS."""
    from brain import fragment_pool
    from brain.node_registry import NodeRegistry

    reg = NodeRegistry()
    reg.register("frontal.fake_switch", None, kind="switch", cluster="frontal")
    monkeypatch.setattr("brain.node_registry.get_node_registry", lambda: reg)
    # Force it onto the allowlist; classify()=="switch" must still reject it.
    fragment_pool.HOST_RECEPTORS["frontal.fake_switch"] = frozenset({"draft_slot"})
    try:
        assert not fragment_pool.is_admissible("x", "frontal.fake_switch")
    finally:
        fragment_pool.HOST_RECEPTORS.pop("frontal.fake_switch", None)


# ── wiring: attachment edges, decay, prune ───────────────────────────────────


def test_attached_fragments_and_prune(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("fragment.alpha", "frontal.drafter_A", weight=1.8)
    w.add("fragment.beta", "frontal.drafter_A", weight=1.02)
    w.add("fragment.gamma", "frontal.critic", weight=2.5)
    assert dict(w.attached_fragments("frontal.drafter_A")) == {"alpha": 1.8, "beta": 1.02}
    assert dict(w.attached_fragments("frontal.critic")) == {"gamma": 2.5}
    removed = w.prune_fragment_edges(1.05)
    assert removed == 1  # only beta (1.02) is at/below the floor
    assert dict(w.attached_fragments("frontal.drafter_A")) == {"alpha": 1.8}


def test_decay_toward_rest_skips_fragments(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("frontal.executive", "frontal.drafter_A", weight=2.0)  # topology
    w.add("fragment.alpha", "frontal.drafter_A", weight=2.0)  # attachment
    w.decay_toward_rest(rest=1.0, rate=0.1)
    assert w.get_edge_weight("frontal.executive", "frontal.drafter_A") == pytest.approx(1.9)
    # fragment edge untouched by topology homeostasis
    assert dict(w.attached_fragments("frontal.drafter_A"))["alpha"] == pytest.approx(2.0)
    # its own steeper decay does move it
    w.decay_fragment_edges(0.1)
    assert dict(w.attached_fragments("frontal.drafter_A"))["alpha"] == pytest.approx(1.9)


# ── node_registry: fragments reconcile cleanly ───────────────────────────────


def test_register_fragment_nodes_classifies_and_audits_clean(monkeypatch, tmp_path):
    from brain.node_registry import (
        NodeRegistry,
        audit_node_registry,
        register_fragment_nodes,
    )

    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("fragment.alpha", "frontal.drafter_A", weight=1.5)
    reg = NodeRegistry()
    # register the two endpoints so the audit is clean, then the fragment node
    reg.register("frontal.drafter_A", object(), kind="cell", cluster="frontal")
    n = register_fragment_nodes(w, reg)
    assert n == 1
    assert reg.classify("fragment.alpha") == "fragment"
    assert not reg.is_object_backed("fragment.alpha")  # never counted UNWIRED
    report = audit_node_registry(w, reg)
    assert report["orphans"] == []
    assert report["unwired"] == []


# ── learning: contrastive credit + decay/prune ───────────────────────────────


def test_winner_fragment_reinforced_loser_not_created(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = _sc(monkeypatch, tmp_path, w)
    trace = _win_trace(
        winner_idx=2,
        drafter_fragments={
            "frontal.drafter_C": ["alpha"],  # winner carried alpha
            "frontal.drafter_A": ["beta"],  # loser explored beta (no prior edge)
        },
    )
    sc._run_hebbian_pass("sess", [trace])
    won = dict(w.attached_fragments("frontal.drafter_C"))
    assert "alpha" in won and won["alpha"] > 1.05  # created + cleared the prune floor
    # a fresh losing exploration never establishes an edge
    assert w.attached_fragments("frontal.drafter_A") == []


def test_loser_penalty_demotes_existing_attachment(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("fragment.beta", "frontal.drafter_A", weight=1.60)  # pre-established
    sc = _sc(monkeypatch, tmp_path, w)
    trace = _win_trace(
        winner_idx=2,
        drafter_fragments={"frontal.drafter_C": ["alpha"], "frontal.drafter_A": ["beta"]},
    )
    sc._run_hebbian_pass("sess", [trace])
    beta = dict(w.attached_fragments("frontal.drafter_A")).get("beta")
    assert beta is not None and beta < 1.60  # demoted (loser penalty + forget decay)


def test_cold_attachment_decays_and_prunes(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("fragment.cold", "frontal.drafter_D", weight=1.03)  # already below the floor
    sc = _sc(monkeypatch, tmp_path, w)
    # a turn that does NOT carry 'cold' → no reinforcement → decay + prune remove it
    trace = _win_trace(winner_idx=2, drafter_fragments={"frontal.drafter_C": ["alpha"]})
    sc._run_hebbian_pass("sess", [trace])
    assert w.attached_fragments("frontal.drafter_D") == []


def test_safety_host_in_fragments_creates_no_edge(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = _sc(monkeypatch, tmp_path, w)
    # Even if a safety node somehow appears as a carried host, is_admissible blocks it.
    trace = _win_trace(
        winner_idx=2,
        drafter_fragments={
            "frontal.drafter_C": ["alpha"],
            "motor_cortex.tool_planner": ["evil"],
        },
    )
    sc._run_hebbian_pass("sess", [trace])
    assert w.attached_fragments("motor_cortex.tool_planner") == []
    assert "alpha" in dict(w.attached_fragments("frontal.drafter_C"))


def test_kill_switch_neutral_no_learning(monkeypatch, tmp_path):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "fragment_wiring", 0)
    cap = _CaptureDecisions()
    monkeypatch.setattr("brain.hebbian.decisions", cap)
    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = _sc(monkeypatch, tmp_path, w)
    trace = _win_trace(winner_idx=2, drafter_fragments={"frontal.drafter_C": ["alpha"]})
    sc._run_hebbian_pass("sess", [trace])
    assert w.attached_fragments("frontal.drafter_C") == []
    assert cap.of("attachment_learned") == []


def test_per_persona_isolation(monkeypatch, tmp_path):
    from brain.second_brain.store import bind_persona

    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = _sc(monkeypatch, tmp_path, w)
    trace = _win_trace(winner_idx=2, drafter_fragments={"frontal.drafter_C": ["alpha"]})
    with bind_persona("persona_a"):
        sc._run_hebbian_pass("sess", [trace])
        assert "alpha" in dict(w.attached_fragments("frontal.drafter_C"))
    with bind_persona("persona_b"):
        assert w.attached_fragments("frontal.drafter_C") == []


# ── consumer: injection + exploration ────────────────────────────────────────


def test_consumer_injects_established_fenced(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("fragment.alpha", "frontal.drafter_A", weight=1.9)  # established (≥ 1.3)
    sel = _FakeSelector({"alpha": "ALPHA BODY"}, partners=["alpha"])
    f = _frontal(w, selector=sel)
    block, injected = f._fragment_block_for_host(
        "frontal.drafter_A", explore=False, turn_id="t", seed_idx=0
    )
    assert injected == ["alpha"]
    assert "<data" in block and "ALPHA BODY" in block  # fenced untrusted content


def test_consumer_neutral_when_off(monkeypatch, tmp_path):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "fragment_wiring", 0)
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("fragment.alpha", "frontal.drafter_A", weight=1.9)
    sel = _FakeSelector({"alpha": "ALPHA BODY"}, partners=["alpha"])
    f = _frontal(w, selector=sel)
    assert f._fragment_block_for_host("frontal.drafter_A", explore=False, turn_id="t", seed_idx=0) == (
        "",
        [],
    )


def test_exploration_creates_cross_drafter_variance(monkeypatch, tmp_path):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "fragment_explore_rate", 1.0)  # force rolls true
    w = _isolated_wiring(monkeypatch, tmp_path)
    sel = _FakeSelector(
        {"c1": "BODY1", "c2": "BODY2", "c3": "BODY3"}, partners=["c1", "c2", "c3"]
    )
    f = _frontal(w, selector=sel)
    firing = [0, 1, 2, 3, 4]
    explore_set = f._select_explore_drafters(firing, "turn7")
    assert 0 < len(explore_set) <= 2  # bounded by fragment_explore_max_drafters, floor kept
    # exploring drafters carry a candidate; the fragment sets are not all identical
    carried = {}
    for i in firing:
        host = f"frontal.drafter_{chr(65 + i)}"
        _, inj = f._fragment_block_for_host(
            host, explore=(i in explore_set), turn_id="turn7", seed_idx=i
        )
        carried[host] = inj
    exploring = [h for h, inj in carried.items() if inj]
    non_exploring = [h for h, inj in carried.items() if not inj]
    assert exploring and non_exploring  # genuine variance across drafters


# ── downshift: local RunPod routing ──────────────────────────────────────────


def test_downshift_gates_on_proven_attachment_and_availability(monkeypatch, tmp_path):
    from brain.settings import settings

    _pod_ready(monkeypatch)  # confirmed pod → available
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("fragment.alpha", "frontal.drafter_A", weight=2.5)  # proven (≥ 2.2)
    w.add("fragment.beta", "frontal.drafter_B", weight=2.3)  # proven
    w.add("fragment.weak", "frontal.drafter_C", weight=1.4)  # not proven
    f = _frontal(w, selector=object(), router=_FakeRouter(lite=False))
    firing = [0, 1, 2, 3, 4]
    # A, B proven; cloud floor 2 → downshift both (cap len-floor=3)
    assert f._downshift_indices(firing, "t") == {0, 1}


def test_downshift_noop_on_lite_tier_and_pod_off(monkeypatch, tmp_path):
    from brain.settings import settings

    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("fragment.alpha", "frontal.drafter_A", weight=2.5)
    # lite tier silently reroutes local→cloud haiku — must be a no-op (no leak)
    _pod_ready(monkeypatch)
    f_lite = _frontal(w, selector=object(), router=_FakeRouter(lite=True))
    assert f_lite._downshift_indices([0, 1, 2, 3, 4], "t") == set()
    # pod-down: readiness flag cleared
    _pod_ready(monkeypatch, ready=0)
    f_off = _frontal(w, selector=object(), router=_FakeRouter(lite=False))
    assert f_off._downshift_indices([0, 1, 2, 3, 4], "t") == set()


def test_downshift_noop_on_cold_start_never_confirmed(monkeypatch, tmp_path):
    """No pod has ever been confirmed ready this process (runpod_pod_ready still
    at its registered default of 0) → downshift must be a clean no-op, not a
    false 'available' (the bug: runpod_host's default "" used to read as
    available since it only failed on the literal 'off' sentinel)."""
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("fragment.alpha", "frontal.drafter_A", weight=2.5)
    f = _frontal(w, selector=object(), router=_FakeRouter(lite=False))
    assert f._downshift_indices([0, 1, 2, 3, 4], "t") == set()


def test_downshift_liveness_flag_expires(monkeypatch, tmp_path):
    """runpod_pod_ready carries a TTL: a pod that dies between the manager's
    refreshes leaves the flag set, and without the TTL downshift would keep
    routing drafts at the dead host. A stale stamp → not available; a fresh
    re-stamp → available again."""
    from brain.settings import settings

    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("fragment.alpha", "frontal.drafter_A", weight=2.5)
    f = _frontal(w, selector=object(), router=_FakeRouter(lite=False))
    ttl = float(settings.get("runpod_pod_ready_ttl_s", 300.0))

    _pod_ready(monkeypatch, age_s=ttl + 1.0)  # flag set but stamp expired
    assert not f._local_available()
    _pod_ready(monkeypatch)  # manager refresh re-stamps → available again
    assert f._local_available()


def test_downshift_respects_cloud_floor(monkeypatch, tmp_path):
    from brain.settings import settings

    _pod_ready(monkeypatch)
    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("fragment.alpha", "frontal.drafter_A", weight=2.5)
    w.add("fragment.beta", "frontal.drafter_B", weight=2.5)
    f = _frontal(w, selector=object(), router=_FakeRouter(lite=False))
    # only two firing, both proven, floor 2 → keep both on cloud → downshift none
    assert f._downshift_indices([0, 1], "t") == set()


async def test_end_to_end_learn_then_inject_then_downshift(monkeypatch, tmp_path):
    """The full loop: repeated winning turns consolidate an attachment above the inject
    threshold (so the consumer injects it), and once it crosses the downshift threshold the
    drafter becomes eligible to run on the local model."""
    from brain.settings import settings

    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = _sc(monkeypatch, tmp_path, w)
    # drafter_C keeps winning while carrying "alpha" → the attachment climbs across both
    # the inject threshold and, with sustained success, the (higher) downshift threshold.
    for i in range(14):
        sc._run_hebbian_pass(
            f"sess{i}",
            [_win_trace(f"t{i}", winner_idx=2, drafter_fragments={"frontal.drafter_C": ["alpha"]})],
        )
    weight = dict(w.attached_fragments("frontal.drafter_C"))["alpha"]
    assert weight >= float(settings.get("fragment_inject_threshold"))  # injectable
    assert weight >= float(settings.get("fragment_downshift_threshold"))  # proven enough to downshift

    # consumer injects the now-established attachment (fenced)
    sel = _FakeSelector({"alpha": "ALPHA BODY"}, partners=["alpha"])
    f = _frontal(w, selector=sel, router=_FakeRouter(lite=False))
    block, injected = f._fragment_block_for_host(
        "frontal.drafter_C", explore=False, turn_id="t", seed_idx=2
    )
    assert injected == ["alpha"] and "<data" in block

    # and the proven drafter is now eligible to run on the local model
    _pod_ready(monkeypatch)
    assert 2 in f._downshift_indices([0, 1, 2, 3, 4], "t")


async def test_cell_model_override_forwarded():
    from brain.cell import IntegratorCell

    captured = {}

    class RecRouter:
        async def call(self, model, sysp, msgs, **kw):
            captured["model"] = model
            captured["locality"] = kw.get("locality")
            return "ok"

    c = IntegratorCell(name="drafter_A", cluster="frontal", model="haiku", system_prompt="x", topics=[])
    c.set_router(RecRouter())
    c.reset_turn("t")
    await c.call(
        [{"role": "user", "content": "hi"}], model_override="runpod", locality_override="local"
    )
    assert captured == {"model": "runpod", "locality": "local"}
    c.reset_turn("t2")
    await c.call([{"role": "user", "content": "hi"}])
    assert captured == {"model": "haiku", "locality": "either"}  # defaults when not overridden
