"""
Per-cluster contracts for neuromodulator-aware switches.

These tests assert that the modulator profiles wired up in Temporal,
Hippocampus, and Motor cortex actually shift switch behaviour in the
expected direction. Coefficients are conservative (≤0.15) so the
expected differences are small but directionally significant.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Temporal cluster switches
# ---------------------------------------------------------------------------

def _make_temporal():
    from brain.bus import Bus
    from brain.clusters.temporal import TemporalCluster

    class _Router:
        async def call(self, *a, **kw):
            return "{}"
        def supports(self, *a, **kw):
            return True
    bus = Bus()
    return TemporalCluster(bus, _Router()), bus


class TestTemporalSwitchModulation:
    def test_template_match_harder_to_fire_under_high_ACh(self):
        temporal, _ = _make_temporal()
        sw = temporal._template_switch
        thr_low = sw.effective_threshold({"ACh": 0.0})
        thr_high = sw.effective_threshold({"ACh": 1.0})
        # High ACh (curiosity) raises threshold → canned responses suppressed.
        assert thr_high > thr_low

    def test_epistemic_action_easier_to_fire_under_high_ACh(self):
        temporal, _ = _make_temporal()
        sw = temporal._epistemic_switch
        # Negative coefficient: high ACh lowers threshold (invites introspection).
        assert sw.effective_threshold({"ACh": 1.0}) < \
               sw.effective_threshold({"ACh": 0.0})

    def test_self_reference_easier_under_warm_chemistry(self):
        temporal, _ = _make_temporal()
        sw = temporal._self_ref_switch
        # High OXT + 5HT → social safety → introspection easier.
        warm = sw.effective_threshold({"OXT": 1.0, "5HT": 1.0})
        cold = sw.effective_threshold({"OXT": 0.0, "5HT": 0.0})
        assert warm < cold

    def test_integrator_inhibitor_harder_to_fire_under_high_ACh(self):
        """The integrator_inhibitor now keys off ACh (curiosity), not GABA.
        High ACh → harder to fire the inhibitor → integrator stays awake even
        on routine input. GABA handling is delegated to should_bypass_gating()
        which forces the integrator awake at high GABA — having the inhibitor
        also fire at high GABA contradicted the bypass."""
        temporal, _ = _make_temporal()
        sw = temporal._integrator_inhibitor
        assert sw.effective_threshold({"ACh": 1.0}) > \
               sw.effective_threshold({"ACh": 0.0})

    def test_integrator_inhibitor_chemistry_neutral_on_GABA(self):
        temporal, _ = _make_temporal()
        sw = temporal._integrator_inhibitor
        # GABA no longer in the modulator dict — should not affect threshold.
        assert sw.effective_threshold({"GABA": 1.0}) == \
               sw.effective_threshold({"GABA": 0.0})

    def test_length_bucket_is_chemistry_neutral(self):
        temporal, _ = _make_temporal()
        sw = temporal._length_switch
        # Granularity should not depend on chemistry.
        assert sw.effective_threshold({"DA": 1.0, "GABA": 1.0}) == sw.threshold
        assert sw.effective_threshold({"DA": 0.0, "GABA": 0.0}) == sw.threshold

    def test_chem_snapshot_merges_neuromod_and_hormonal(self):
        temporal, bus = _make_temporal()
        bus.neuromod.add("DA", 0.2)
        bus.hormonal.add("OXT", 0.3)
        snap = temporal._chem_snapshot()
        # Both channels present in merged snapshot.
        assert "DA" in snap
        assert "OXT" in snap


# ---------------------------------------------------------------------------
# Hippocampus cluster switches (promoted from inline constants)
# ---------------------------------------------------------------------------

def _make_hippo():
    from brain.bus import Bus
    from brain.clusters.hippocampus import HippocampusCluster

    class _Router:
        async def call(self, *a, **kw):
            return "{}"
        def supports(self, *a, **kw):
            return True
    bus = Bus()
    return HippocampusCluster(bus, _Router()), bus


class TestHippocampusSwitchModulation:
    def test_encoder_gate_harder_to_skip_under_high_engagement(self):
        hippo, _ = _make_hippo()
        sw = hippo._encoder_gate
        # High DA+NE → threshold rises → encoder skip harder → encode more thoroughly.
        thr_engaged = sw.effective_threshold({"DA": 1.0, "NE": 1.0})
        thr_flat = sw.effective_threshold({"DA": 0.0, "NE": 0.0})
        assert thr_engaged > thr_flat

    def test_recall_cache_reuse_easier_under_high_DA(self):
        hippo, _ = _make_hippo()
        sw = hippo._recall_cache_reuse
        # Negative coefficient: high DA lowers threshold → cache reuse easier.
        assert sw.effective_threshold({"DA": 1.0}) < \
               sw.effective_threshold({"DA": 0.0})

    def test_fanout_total_budget_neutral_chemistry_is_8(self):
        hippo, _ = _make_hippo()
        assert hippo._fanout_total_budget({}) == 8
        assert hippo._fanout_total_budget({"ACh": 0.5, "Glu": 0.5}) == 8

    def test_fanout_total_budget_widens_under_high_ACh_Glu(self):
        hippo, _ = _make_hippo()
        # High ACh + Glu → wider net (more total recall lookups).
        assert hippo._fanout_total_budget({"ACh": 1.0, "Glu": 1.0}) > 8

    def test_fanout_total_budget_bounded(self):
        hippo, _ = _make_hippo()
        # Even at chemistry extremes the budget stays in [4, 12].
        for chem in ({"ACh": 1.0, "Glu": 1.0}, {"ACh": 0.0, "Glu": 0.0}):
            b = hippo._fanout_total_budget(chem)
            assert 4 <= b <= 12

    def test_entity_grep_depth_widens_under_high_ACh(self):
        hippo, _ = _make_hippo()
        # Schema_k baseline 3 → base depth max(2,3)=3. Chemistry shifts ±1.
        base = hippo._entity_grep_depth({}, schema_k=3)
        wide = hippo._entity_grep_depth({"ACh": 1.0}, schema_k=3)
        narrow = hippo._entity_grep_depth({"ACh": 0.0}, schema_k=3)
        assert wide >= base
        assert narrow <= base

    def test_chem_snapshot_present(self):
        hippo, bus = _make_hippo()
        bus.neuromod.add("ACh", 0.3)
        snap = hippo._chem_snapshot()
        assert "ACh" in snap


# ---------------------------------------------------------------------------
# Cross-cluster: a single chemistry snapshot affects multiple clusters consistently
# ---------------------------------------------------------------------------

class TestChemistryPropagation:
    def test_high_ACh_makes_temporal_inhibitor_harder(self):
        """End-to-end: pushing ACh up on the bus actually raises the temporal
        integrator inhibitor's threshold via _chem_snapshot, so the integrator
        is harder to skip when the brain is curious."""
        temporal, bus = _make_temporal()
        bus.neuromod.add("ACh", 0.5)  # default 0.2 → 0.7 after clamping
        chem = temporal._chem_snapshot()
        baseline = temporal._integrator_inhibitor.effective_threshold({"ACh": 0.5})
        elevated = temporal._integrator_inhibitor.effective_threshold(chem)
        assert elevated > baseline  # ACh above neutral → threshold rises

    def test_high_CORT_makes_hippocampus_encoder_harder_to_skip(self):
        """High CORT (chronic stress) raises the encoder_gate threshold so the
        LLM encoder is harder to skip — threat memories get encoded thoroughly."""
        from brain.clusters.hippocampus import HippocampusCluster
        from brain.bus import Bus
        class _Router:
            async def call(self, *a, **kw): return "{}"
            def supports(self, *a, **kw): return True
        bus = Bus()
        hippo = HippocampusCluster(bus, _Router())
        # Hold DA + NE at neutral, vary CORT — isolates the CORT effect.
        neutral = {"DA": 0.5, "NE": 0.5, "CORT": 0.5}
        high_cort = {"DA": 0.5, "NE": 0.5, "CORT": 1.0}
        assert hippo._encoder_gate.effective_threshold(high_cort) > \
               hippo._encoder_gate.effective_threshold(neutral)
