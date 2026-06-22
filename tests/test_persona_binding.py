"""
Per-turn persona binding (multi-persona Path B). bind_persona() scopes persona
resolution for the duration of a turn so ONE process can serve many personas; the
store/mandate layers resolve through _resolve_persona, which now consults the
contextvar. Unbound → process env, byte-for-byte unchanged.
"""

from __future__ import annotations

from brain.second_brain.store import _persona_key, _resolve_persona, active_persona, bind_persona


def test_unbound_uses_env_default(monkeypatch):
    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Sage")
    assert _resolve_persona("") == "The Sage"
    assert active_persona() == ""  # nothing bound


def test_bind_persona_overrides_resolution(monkeypatch):
    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Sage")
    with bind_persona("The Visionary"):
        assert active_persona() == "The Visionary"
        assert _resolve_persona("") == "The Visionary"
        assert _persona_key(_resolve_persona("")) == "the_visionary"
    # Resets cleanly after the block → back to the process default.
    assert active_persona() == ""
    assert _resolve_persona("") == "The Sage"


def test_explicit_arg_still_wins_over_binding(monkeypatch):
    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Sage")
    with bind_persona("The Visionary"):
        # An explicit persona (e.g. a store constructed for a specific persona) wins.
        assert _resolve_persona("The Adversary") == "The Adversary"


def test_empty_bind_is_a_noop(monkeypatch):
    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Sage")
    with bind_persona(""):
        assert _resolve_persona("") == "The Sage"  # unchanged


def test_new_chem_for_applies_persona_baseline_and_levels():
    # Per-persona chemistry (Path B): a ChemPair can carry a specific persona's
    # resting BASELINES (what it decays toward) and current LEVELS, split across the
    # neuromod (DA…) and hormonal (OXT…) layers from one flat profile dict.
    from brain.bus import Bus

    bus = Bus()
    baseline = {"DA": 0.62, "OXT": 0.45}  # DA = neuromod, OXT = hormonal
    levels = {"DA": 0.70, "OXT": 0.50}
    pair = bus.new_chem_for(baseline, levels)
    assert abs(pair.neuromod._baseline["DA"] - 0.62) < 1e-6
    assert abs(pair.neuromod.get("DA") - 0.70) < 1e-6
    assert abs(pair.hormonal._baseline["OXT"] - 0.45) < 1e-6
    assert abs(pair.hormonal.snapshot()["OXT"] - 0.50) < 1e-6
    # Decay relaxes toward THIS persona's baseline, not the process default.
    pair.neuromod.add("DA", 0.25)
    pair.neuromod.decay(8)
    assert pair.neuromod.get("DA") < 0.80  # pulled back toward 0.62


def test_catalog_caches_per_persona(monkeypatch):
    # In local mode the catalog is empty, but the cache must be keyed per persona so
    # two personas don't share one entry (the Path B cache-by-persona invariant).
    import brain.mandates as mandates

    monkeypatch.setenv("BRAIN_STORAGE_BACKEND", "local")
    mandates._catalog = {}
    with bind_persona("The Visionary"):
        mandates.catalog()
    with bind_persona("The Adversary"):
        mandates.catalog()
    assert set(mandates._catalog.keys()) == {"the_visionary", "the_adversary"}
    mandates._catalog = {}  # don't leak into other tests
