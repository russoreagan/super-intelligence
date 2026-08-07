"""
Co-activation credit: the surface that reaches edges path credit structurally cannot.

Path credit only ever touches CONSECUTIVE pairs of `fired_path`, and only
SwitchNeuron.fire() / IntegratorCell.call() ever append to that list. So an edge whose
endpoint is a bus channel, a chemistry mapper, a state holder or a bookkeeping node
could never be credited at all — 35 of the 72 bootstrap edges, of which two
hand-written helpers covered 8 and nothing covered the other 27.
"""

from __future__ import annotations

import importlib

import pytest

from brain.wiring_bootstrap import bootstrap


def _isolated_wiring(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_WIRING_PATH", str(tmp_path / "wiring.json"))
    monkeypatch.setenv("BRAIN_WIRING_HISTORY_DIR", str(tmp_path / "history"))
    import brain.wiring as w_mod

    importlib.reload(w_mod)
    return w_mod.Wiring()


class _StubSchema:
    async def aappend_fact(self, *a, **kw): ...
    def read(self, name):
        return ""

    async def awrite(self, name, content): ...


class _StubEpisodic:
    def encode(self, ep): ...
    def recall(self, vec, limit=4):
        return []

    def recall_recent(self, limit=6):
        return []


class _StubRouter:
    async def call(self, *a, **kw):
        return "{}"

    async def embed(self, text):
        return [0.0] * 16


def _sleep(w):
    from brain.sleep import SleepConsolidation

    return SleepConsolidation(_StubRouter(), _StubSchema(), _StubEpisodic(), wiring=w)


# Nodes that DO reach fired_path (a SwitchNeuron or IntegratorCell exists for them).
_FIREABLE = {
    "temporal.template_match",
    "temporal.length_bucket",
    "temporal.salience_prefilter",
    "temporal.self_reference",
    "temporal.epistemic_action",
    "temporal.integrator_inhibitor",
    "temporal.understanding_integrator",
    "frontal.executive",
    "frontal.drafter_A",
    "frontal.drafter_B",
    "frontal.drafter_C",
    "frontal.drafter_D",
    "frontal.drafter_E",
    "frontal.critic",
    "frontal.empathy_critic",
    "frontal.stoic_reframer",
    "frontal.commitment_extractor",
    "frontal.approach_A",
    "frontal.approach_B",
    "frontal.approach_C",
    "frontal.approach_critic",
    "hippocampus.structural_recall",
    "motor_cortex.tool_planner",
}


def _full_trace(w, turn_id="cov"):
    """A trace in which EVERY node in the graph participated, so the only reason an
    edge can fail to move is that some rule excluded it."""
    from brain.observability.timeline import TurnTrace

    nodes = {n for pair in _edge_pairs(w) for n in pair}

    t = TurnTrace(turn_id=turn_id, session_id="s", user_input="x")
    t.neuromod = {"DA": 0.9, "GABA": 0.0, "ACh": 0.3, "Glu": 0.3}
    t.prior_neuromod = {"DA": 0.5, "GABA": 0.0, "ACh": 0.3, "Glu": 0.3}
    t.emotion = "content"
    t.user_emotion = ""
    t.draft_scores = [
        {"draft_id": f"draft_0_{turn_id}", "overall": 0.9, "selected": True, "critic_ran": True}
    ]
    # Everything fireable fires; everything else participates.
    t.fired_path = [
        {
            "name": n,
            "cluster": n.split(".")[0],
            "kind": "switch"
            if n.startswith("temporal.") and "integrator" not in n
            else "integrator",
            "level": 1.0,
        }
        for n in sorted(nodes & _FIREABLE)
    ]
    t.coactive = dict.fromkeys(nodes - _FIREABLE, 1.0)
    # Feed the two per-family helpers so their 8 edges are credited too.
    t.recall_contrib = {"schema": 3, "episode": 3, "structural": 2}
    return t


def _edge_pairs(w):
    # top_edges is the public enumeration; a limit above edge_count returns them all.
    return [(e["src"], e["tgt"]) for e in w.top_edges(w.edge_count() + 1)]


# ── The requirement ──────────────────────────────────────────────────────────


def test_every_bootstrap_edge_is_creditable(monkeypatch, tmp_path):
    """ "All edges should be possible for learning" — enforced, not aspirational.

    Every seeded edge must either move under a turn in which all its endpoints
    participated, or appear in the ONE documented exclusion set (edges owned by an
    explicit contrastive competition, which are credited by that competition instead
    of by path/co-activation credit). Growing the exclusion set has to be deliberate.
    """
    from brain.hebbian import _competition_owned
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "fragment_wiring", 0)
    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    all_edges = set(_edge_pairs(w))
    assert len(all_edges) > 60, f"bootstrap looks wrong: only {len(all_edges)} edges"

    before = {e: w.get_edge_weight(*e) for e in all_edges}
    _sleep(w)._run_hebbian_pass("s_cov", [_full_trace(w)])

    unmoved = {e for e in all_edges if w.get_edge_weight(*e) == pytest.approx(before[e])}
    excluded = _competition_owned(int(settings.get("node_reserve_pool", 3)))
    orphans = unmoved - excluded
    assert not orphans, (
        f"{len(orphans)} edge(s) can never be credited by anything: {sorted(orphans)}"
    )


def test_the_previously_inert_families_now_move(monkeypatch, tmp_path):
    """The 17 edges that no surface credited at all: hypothalamus (9), parietal (3),
    and the recall aggregator (5)."""
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "fragment_wiring", 0)
    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    probes = [
        ("hypothalamus.threat_to_GABA", "frontal.drafter_A"),
        ("hypothalamus.valence_to_DA", "frontal.executive"),
        ("hypothalamus.novelty_to_ACh", "frontal.executive"),
        ("hypothalamus.arousal_homeostat", "frontal.executive"),
        ("parietal.recent_turns_ringbuffer", "frontal.executive"),
        ("parietal.topic_vector_holder", "frontal.executive"),
        ("parietal.entity_tracker", "frontal.executive"),
        ("hippocampus.cosine_recall", "hippocampus.recall_aggregator"),
        ("hippocampus.schema_grep", "hippocampus.recall_aggregator"),
        ("sensory.text", "temporal.length_bucket"),
        ("sensory.text", "temporal.salience_prefilter"),
        ("sensory.text", "temporal.integrator_inhibitor"),
    ]
    for e in probes:
        assert w.has(*e), f"probe edge missing from bootstrap: {e}"
    _sleep(w)._run_hebbian_pass("s_inert", [_full_trace(w)])
    for e in probes:
        assert w.get_edge_weight(*e) > 1.0, f"{e} still inert"


def test_credit_is_graded_by_participation(monkeypatch, tmp_path):
    """min(level_src, level_tgt) scaling. A blanket "both active" delta would land
    identically on ~50 edges, and since every consumer reads RELATIVE weight, a
    common-mode delta carries exactly zero information."""
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "fragment_wiring", 0)
    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    t = _full_trace(w)
    # Same source, two targets, one participating a quarter as much.
    t.coactive["parietal.recent_turns_ringbuffer"] = 1.0
    t.coactive["parietal.topic_vector_holder"] = 0.25
    _sleep(w)._run_hebbian_pass("s_grade", [t])

    full = w.get_edge_weight("parietal.recent_turns_ringbuffer", "frontal.executive") - 1.0
    quarter = w.get_edge_weight("parietal.topic_vector_holder", "frontal.executive") - 1.0
    assert full > 0 and quarter > 0
    assert full / quarter == pytest.approx(4.0, rel=1e-3)


def test_no_double_credit(monkeypatch, tmp_path):
    """An edge that is both path-adjacent and co-active is credited once."""
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "fragment_wiring", 0)
    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)

    records = []

    class _Cap:
        def log(self, decision, *, turn_id="", cluster="", **f):
            records.append({"decision": decision, **f})

    monkeypatch.setattr("brain.hebbian.decisions", _Cap())
    _sleep(w)._run_hebbian_pass("s_dc", [_full_trace(w)])

    path = {(r["src"], r["tgt"]) for r in records if r["decision"] == "hebbian_update_applied"}
    coact = {
        (r["src"], r["tgt"]) for r in records if r["decision"] == "coactivation_credit_applied"
    }
    assert path, "expected some path credit"
    assert coact, "expected some co-activation credit"
    assert not (path & coact), f"double-credited: {sorted(path & coact)}"


def test_neutral_when_off(monkeypatch, tmp_path):
    """Kill switch: with the new flags off, weights match the pre-change behaviour."""
    from brain.settings import settings

    def _run(coact, purity, tag):
        monkeypatch.setitem(settings._data, "coactivation_credit", coact)
        monkeypatch.setitem(settings._data, "credit_purity", purity)
        monkeypatch.setitem(settings._data, "fragment_wiring", 0)
        w = _isolated_wiring(monkeypatch, tmp_path / tag)
        bootstrap(w)
        _sleep(w)._run_hebbian_pass(f"s_{tag}", [_full_trace(w)])
        return {e: w.get_edge_weight(*e) for e in _edge_pairs(w)}

    off = _run(0, 0, "off")
    on = _run(1, 1, "on")
    # Off must leave the previously-inert families exactly at rest.
    assert off[("parietal.entity_tracker", "frontal.executive")] == pytest.approx(1.0)
    assert off[("hypothalamus.valence_to_DA", "frontal.executive")] == pytest.approx(1.0)
    # …and on must not.
    assert on[("parietal.entity_tracker", "frontal.executive")] > 1.0


def test_frozen_blocks_crediting_but_not_recording(monkeypatch, tmp_path):
    """BRAIN_WIRING_FROZEN stays a true panic switch: wiring.json byte-identical.
    Recording is free — it only ever writes to the TurnTrace."""
    from pathlib import Path

    from brain.settings import settings

    monkeypatch.setitem(settings._data, "fragment_wiring", 0)
    w = _isolated_wiring(monkeypatch, tmp_path)
    bootstrap(w)
    w.save()
    path = Path(tmp_path / "wiring.json")
    before = path.read_bytes()

    monkeypatch.setenv("BRAIN_WIRING_FROZEN", "true")
    trace = _full_trace(w)
    _sleep(w)._run_hebbian_pass("s_frozen", [trace])
    assert path.read_bytes() == before
    assert trace.coactive, "recording must still populate the trace under FROZEN"


def test_record_node_active_keeps_the_max(monkeypatch):
    """Repeat records for one node keep the strongest participation."""
    from brain.observability.firing_path import record_node_active, set_current_trace
    from brain.observability.timeline import TurnTrace

    t = TurnTrace(turn_id="t", session_id="s", user_input="x")
    tok = set_current_trace(t)
    try:
        record_node_active("parietal.entity_tracker", 0.2)
        record_node_active("parietal.entity_tracker", 0.9)
        record_node_active("parietal.entity_tracker", 0.4)
        record_node_active("sensory.text", 5.0)  # clamped
    finally:
        from brain.observability.firing_path import reset_current_trace

        reset_current_trace(tok)
    assert t.coactive["parietal.entity_tracker"] == pytest.approx(0.9)
    assert t.coactive["sensory.text"] == pytest.approx(1.0)


def test_record_node_active_without_a_trace_is_a_noop():
    from brain.observability.firing_path import record_node_active

    record_node_active("sensory.text", 1.0)  # must not raise


# ── Call-site integration ────────────────────────────────────────────────────
#
# The credit pass working proves nothing if no cluster ever calls the recorder.
# These drive the real cluster methods and assert the trace is populated.


def test_parietal_records_on_read_not_on_update():
    """The edge means "this state fed the executive", so the record belongs on the
    READ. update() runs every turn unconditionally — recording there would emit a
    constant, and a constant carries no signal."""
    from brain.bus import Bus
    from brain.clusters.parietal import ParietalCluster
    from brain.observability.firing_path import reset_current_trace, set_current_trace
    from brain.observability.timeline import TurnTrace

    p = ParietalCluster(Bus())
    t = TurnTrace(turn_id="t", session_id="s", user_input="x")
    tok = set_current_trace(t)
    try:
        p.update({}, "hello there", "hi back")
        # update() alone must NOT have recorded participation
        assert "parietal.recent_turns_ringbuffer" not in t.coactive
        # …reading it does
        assert p.recent_turns_text()
        p.entity_last_seen()
    finally:
        reset_current_trace(tok)

    assert t.coactive.get("parietal.recent_turns_ringbuffer", 0) > 0


def test_hypothalamus_records_mappers_scaled_by_channel_movement():
    """Levels come from how far each mapper moved its channel, so a turn where a
    channel barely moved credits its edges barely."""
    from brain.clusters.hypothalamus import HypothalamusCluster
    from brain.observability.firing_path import reset_current_trace, set_current_trace
    from brain.observability.timeline import TurnTrace

    h = HypothalamusCluster.__new__(HypothalamusCluster)  # no bus needed for this helper
    t = TurnTrace(turn_id="t", session_id="s", user_input="x")
    tok = set_current_trace(t)
    try:
        h._record_coactivation(
            {"GABA": 0.0, "DA": 0.5, "ACh": 0.3, "NE": 0.1},
            {"GABA": 0.25, "DA": 0.55, "ACh": 0.3, "NE": 0.1},  # big GABA, small DA, no ACh
        )
    finally:
        reset_current_trace(tok)

    assert t.coactive["hypothalamus"] == pytest.approx(1.0)
    assert t.coactive["hypothalamus.threat_to_GABA"] == pytest.approx(1.0)  # 0.25/0.25
    assert t.coactive["hypothalamus.valence_to_DA"] == pytest.approx(0.2)  # 0.05/0.25
    assert t.coactive["hypothalamus.novelty_to_ACh"] == pytest.approx(0.0)


def test_hippocampus_records_recall_by_contribution_share():
    """MANDATORY grading: these weights set the recall budget, so crediting a
    strategy for merely running would close a positive feedback loop (returns
    nothing → gains weight → more budget → runs more → gains more)."""
    from brain.clusters.hippocampus import HippocampusCluster
    from brain.observability.firing_path import reset_current_trace, set_current_trace
    from brain.observability.timeline import TurnTrace

    h = HippocampusCluster.__new__(HippocampusCluster)
    t = TurnTrace(turn_id="t", session_id="s", user_input="x")
    tok = set_current_trace(t)
    try:
        h._record_recall_coactivation(schema_hits=6, episode_hits=2, structural_hits=0)
    finally:
        reset_current_trace(tok)

    assert t.coactive["mem.recall"] == pytest.approx(1.0)
    assert t.coactive["hippocampus.schema_grep"] == pytest.approx(0.75)  # 6/8
    assert t.coactive["hippocampus.cosine_recall"] == pytest.approx(0.25)  # 2/8
    assert t.coactive["hippocampus.structural_recall"] == pytest.approx(0.0)


def test_hippocampus_records_nothing_extra_when_recall_is_empty():
    """A recall that returned nothing must not credit any strategy — that is the
    exact input that would otherwise start the runaway."""
    from brain.clusters.hippocampus import HippocampusCluster
    from brain.observability.firing_path import reset_current_trace, set_current_trace
    from brain.observability.timeline import TurnTrace

    h = HippocampusCluster.__new__(HippocampusCluster)
    t = TurnTrace(turn_id="t", session_id="s", user_input="x")
    tok = set_current_trace(t)
    try:
        h._record_recall_coactivation(schema_hits=0, episode_hits=0, structural_hits=0)
    finally:
        reset_current_trace(tok)

    assert "hippocampus.schema_grep" not in t.coactive
    assert "hippocampus.recall_aggregator" not in t.coactive


# ── The weight economy actually in force ─────────────────────────────────────
#
# These assert the EFFECTIVE settings, not the DEFAULTS dict. brain/settings.json
# (and every hosted tenant's own settings.json) layers over DEFAULTS, so a stale
# pin there silently reverts a default with nothing failing — which is exactly what
# happened here: settings.json pinned hebbian_outcome_delta at 0.02 while the new
# default was 0.06, so raising the decay rate alone would have cut every equilibrium
# to a third and made two consumer thresholds permanently unreachable.


def _equilibrium(scale, share=1.0):
    """Steady-state weight for an edge on `scale`, at the settings in force.

    `share` applies to the RECALL consumers only: that credit is split by each
    strategy's contribution to the turn's hits, so a strategy returning a seventh of
    them earns a seventh of the credit. Switch-routing credit is not split."""
    from brain.settings import settings

    o_bar, c, m = 0.409, 0.993, 1.56  # measured; see eval/weight_economy_sim.py
    delta = float(settings.get("hebbian_outcome_delta"))
    r = float(settings.get("decay_toward_rest_rate_per_turn"))
    w_max = float(settings.get("weight_max"))
    return min(w_max, 1.0 + (c * o_bar * delta * m * scale * share) / r)


def test_effective_economy_reaches_the_switch_gate_thresholds():
    """Switch-routing credit is not share-split, so these are purely time-gated: an
    equilibrium below the band top means the gate can never learn to be fully eager."""
    routing = _equilibrium(0.5)
    assert routing > 1.400, f"self_reference band top unreachable (eq {routing:.3f})"
    assert routing > 1.250, f"epistemic_action band top unreachable (eq {routing:.3f})"
    assert _equilibrium(1.0) > 1.500, "inhibitor confidence floor never bottoms"


def test_recall_consumers_are_share_gated_not_time_gated():
    """A recall strategy earns credit in proportion to the hits it contributed, so
    `structural_limit` widens only when structural recall actually pulls its weight —
    intended, but it means a flat-scale reading overstates it about fourfold."""
    assert _equilibrium(0.5, share=1 / 7) < 1.167, "a 1/7-share strategy should NOT widen the limit"
    assert _equilibrium(0.5, share=1 / 3) > 1.167, "a 1/3-share strategy should widen it"


def test_effective_economy_does_not_saturate():
    """Siblings all pinned at weight_max is uniform routing — the failure mode the
    whole change exists to remove."""
    from brain.settings import settings

    w_max = float(settings.get("weight_max"))
    assert _equilibrium(1.0) < w_max - 0.05, "path edges saturate the weight ceiling"


def test_effective_time_constant_is_in_the_target_band():
    """~33 turns (about 7 sessions). Much slower and learning is invisible; much
    faster and a single bad session reshapes routing."""
    from brain.settings import settings

    tau = 1.0 / float(settings.get("decay_toward_rest_rate_per_turn"))
    assert 20 <= tau <= 60, f"time constant {tau:.0f} turns is outside the target band"


def test_fragment_economy_still_reaches_promote():
    """Guard on a real trap: raising fragment_forget_per_turn to match the topology
    rate leaves a proven attachment at ~1.55 — it would still inject but never reach
    node_promote_threshold, so Tier 2 recruitment would silently stop firing."""
    from brain.settings import settings

    o_bar, win_rate = 0.409, 1 / 5
    gain = float(settings.get("fragment_gain"))
    forget = float(settings.get("fragment_forget_per_turn"))
    eq = 1.0 + (win_rate * o_bar * gain) / forget
    assert eq > float(settings.get("node_promote_threshold")), (
        f"proven fragment settles at {eq:.2f}, below promote — Tier 2 can never recruit"
    )
