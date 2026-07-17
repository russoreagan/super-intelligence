"""
Tests for the wiring node registry (brain/node_registry.py).

The registry makes the implicit wiring-name → runtime-object mapping explicit and enforced.
Coverage:
  - register / register_object / resolve / classify round-trips + kind validation
  - the reconciliation audit flags a deliberately-orphaned graph name AND a deliberately-unwired
    registered object
  - the REAL bootstrap graph reconciles cleanly: every object-backed node resolves to a live
    IntegratorCell / SwitchNeuron, every non-object node is classified, zero orphans, zero
    unwired. This is the proof that the mapping is now explicit + correct, and the drift alarm.
"""

from __future__ import annotations

import pytest

from brain.cell import IntegratorCell
from brain.neuron import SwitchNeuron
from brain.node_registry import (
    NON_OBJECT_NODES,
    NodeRegistry,
    audit_node_registry,
    get_node_registry,
    register_manifest,
)
from brain.wiring import Wiring
from brain.wiring_bootstrap import bootstrap

# The 16 object-backed nodes the clusters register at construction (== their wiring names).
EXPECTED_CELLS = {
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
}
EXPECTED_SWITCHES = {
    "temporal.template_match",
    "temporal.length_bucket",
    "temporal.salience_prefilter",
    "temporal.self_reference",
    "temporal.epistemic_action",
    "temporal.integrator_inhibitor",
}


class _Router:
    """Minimal ModelRouter stand-in — clusters only .set_router() it at construction."""

    async def call(self, *a, **kw):
        return "{}"

    def supports(self, *a, **kw):
        return True

    async def embed(self, *a, **kw):
        return [0.0] * 768


# ---------------------------------------------------------------------------
# register / resolve / classify round-trips
# ---------------------------------------------------------------------------


class TestRegistryRoundTrips:
    def test_register_object_derives_canonical_name_and_resolves(self):
        reg = NodeRegistry()
        sw = SwitchNeuron("template_match", "temporal")
        reg.register_object(sw, kind="switch")
        assert reg.resolve("temporal.template_match") is sw
        assert reg.classify("temporal.template_match") == "switch"
        assert reg.is_object_backed("temporal.template_match") is True

    def test_non_object_kinds_classify_but_resolve_to_none(self):
        reg = NodeRegistry()
        reg.register("sensory.text", None, kind="channel", cluster="sensory")
        reg.register("hippocampus.cosine_recall", None, kind="strategy", cluster="hippocampus")
        reg.register("hypothalamus", None, kind="subsystem", cluster="hypothalamus")
        for name, kind in [
            ("sensory.text", "channel"),
            ("hippocampus.cosine_recall", "strategy"),
            ("hypothalamus", "subsystem"),
        ]:
            assert reg.classify(name) == kind
            assert reg.resolve(name) is None
            assert reg.is_object_backed(name) is False

    def test_unknown_name_is_unclassified(self):
        reg = NodeRegistry()
        assert reg.classify("frontal.nope") is None
        assert reg.resolve("frontal.nope") is None
        assert reg.is_object_backed("frontal.nope") is False

    def test_all_names_and_by_kind(self):
        reg = NodeRegistry()
        reg.register_object(SwitchNeuron("length_bucket", "temporal"), kind="switch")
        reg.register("mem.recall", None, kind="channel", cluster="mem")
        assert reg.all_names() == {"temporal.length_bucket", "mem.recall"}
        assert reg.by_kind("switch") == ["temporal.length_bucket"]
        assert reg.by_kind("channel") == ["mem.recall"]
        assert reg.by_kind("cell") == []

    def test_register_overwrites_by_name(self):
        reg = NodeRegistry()
        first = SwitchNeuron("template_match", "temporal")
        second = SwitchNeuron("template_match", "temporal")
        reg.register_object(first, kind="switch")
        reg.register_object(second, kind="switch")
        assert len(reg) == 1
        assert reg.resolve("temporal.template_match") is second

    def test_invalid_kind_raises(self):
        reg = NodeRegistry()
        with pytest.raises(ValueError):
            reg.register("x.y", None, kind="bogus", cluster="x")

    def test_register_object_rejects_non_object_kind(self):
        reg = NodeRegistry()
        with pytest.raises(ValueError):
            reg.register_object(SwitchNeuron("s", "x"), kind="channel")


# ---------------------------------------------------------------------------
# reconciliation audit — synthetic orphan + unwired
# ---------------------------------------------------------------------------


def _hermetic_wiring(tmp_path, monkeypatch):
    """A Wiring() that loads NOTHING from disk. brain.wiring.WIRING_PATH is frozen at import
    from the real second_brain/wiring.json and is not isolated by conftest (which only redirects
    SECOND_BRAIN_PATH), so a bare Wiring() can otherwise pick up edges another test persisted.
    Point it at a nonexistent tmp file so bootstrap() is the sole source of topology."""
    import brain.wiring as _wiring_mod

    monkeypatch.setattr(_wiring_mod, "WIRING_PATH", tmp_path / "wiring.json", raising=False)
    return Wiring()


class TestAuditFlagsDrift:
    def test_audit_flags_orphan_and_unwired(self, tmp_path, monkeypatch):
        reg = NodeRegistry()
        w = _hermetic_wiring(tmp_path, monkeypatch)
        # Graph edge whose SOURCE has no backing object and no classification → orphan.
        w.add("frontal.ghost_node", "sensory.text")
        # sensory.text is a real classification (so it is NOT an orphan).
        reg.register("sensory.text", None, kind="channel", cluster="sensory")
        # A registered object whose name never appears in the graph → unwired.
        widget = SwitchNeuron("unwired_widget", "frontal")
        reg.register_object(widget, kind="switch")

        report = audit_node_registry(w, reg)

        assert "frontal.ghost_node" in report["orphans"]
        assert "sensory.text" not in report["orphans"]
        assert "frontal.unwired_widget" in report["unwired"]
        # The orphan resolves/classifies to nothing; the unwired object still resolves.
        assert reg.resolve("frontal.ghost_node") is None
        assert reg.classify("frontal.ghost_node") is None
        assert reg.resolve("frontal.unwired_widget") is widget

    def test_register_manifest_never_overwrites_a_live_object(self):
        reg = NodeRegistry()
        # Pretend an object already owns a name that the manifest also lists.
        obj = SwitchNeuron("recall_aggregator", "hippocampus")
        reg.register_object(obj, kind="switch")
        register_manifest(reg, {"hippocampus.recall_aggregator": "subsystem"})
        # The live object survives — the manifest entry was skipped.
        assert reg.resolve("hippocampus.recall_aggregator") is obj
        assert reg.classify("hippocampus.recall_aggregator") == "switch"


# ---------------------------------------------------------------------------
# the REAL graph reconciles cleanly (the actual proof)
# ---------------------------------------------------------------------------


class TestRealGraphReconciles:
    def _build(self, tmp_path, monkeypatch):
        from brain.brainstem import Brainstem
        from brain.bus import Bus
        from brain.clusters.frontal import FrontalCluster
        from brain.clusters.temporal import TemporalCluster

        reg = get_node_registry()
        reg.clear()  # process singleton — start from a clean slate for a deterministic audit

        w = _hermetic_wiring(tmp_path, monkeypatch)
        bootstrap(w)

        bus = Bus()
        router = _Router()
        brainstem = Brainstem(bus, router)
        # Construction registers the object-backed nodes into the process registry.
        TemporalCluster(bus, router, wiring=w)
        FrontalCluster(bus, brainstem, router, wiring=w)

        register_manifest(reg)
        return w, reg

    def test_zero_orphans_zero_unwired(self, tmp_path, monkeypatch):
        w, reg = self._build(tmp_path, monkeypatch)
        report = audit_node_registry(w, reg)
        assert report["orphans"] == []
        assert report["unwired"] == []
        assert report["object_backed"] == len(EXPECTED_CELLS) + len(EXPECTED_SWITCHES)

    def test_every_object_backed_node_resolves_to_a_live_object(self, tmp_path, monkeypatch):
        _w, reg = self._build(tmp_path, monkeypatch)
        for name in EXPECTED_CELLS:
            assert isinstance(reg.resolve(name), IntegratorCell), name
        for name in EXPECTED_SWITCHES:
            assert isinstance(reg.resolve(name), SwitchNeuron), name

    def test_every_non_object_node_is_classified(self, tmp_path, monkeypatch):
        _w, reg = self._build(tmp_path, monkeypatch)
        for name, kind in NON_OBJECT_NODES.items():
            assert reg.classify(name) == kind, name
            assert reg.resolve(name) is None, name

    def test_registry_covers_exactly_the_graph_node_set(self, tmp_path, monkeypatch):
        """Every wiring node is either an object-backed registration or a manifest entry, and
        vice versa — so the 35 bootstrap names are fully accounted for with no drift."""
        w, reg = self._build(tmp_path, monkeypatch)
        graph_names = {n for (s, t) in w._edges for n in (s, t)}
        expected = EXPECTED_CELLS | EXPECTED_SWITCHES | set(NON_OBJECT_NODES)
        assert graph_names == expected
        assert reg.all_names() == expected
