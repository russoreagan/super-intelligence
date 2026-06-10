"""
Tests for the 2026-06 full-app review fixes.

Covers: multitenant persona hard-fail (no silent persona='default'), LanceDB
filter escaping, sequence-predictor bookkeeping preservation + synonym hot-reload,
chunk suppression recovery + success reinforcement, cross-learning persistence /
read surface, and the routing-weight convergence metric.
"""

from __future__ import annotations

import json

import pytest

# ── persona resolution (store.py) ─────────────────────────────────────────────


def test_resolve_persona_local_falls_back_to_default(monkeypatch):
    from brain.second_brain.store import _resolve_persona

    monkeypatch.delenv("BRAIN_MULTITENANT", raising=False)
    monkeypatch.delenv("BRAIN_PERSONA_NAME", raising=False)
    assert _resolve_persona("") == "default"


def test_resolve_persona_multitenant_refuses_default(monkeypatch):
    from brain.second_brain.store import _resolve_persona

    monkeypatch.setenv("BRAIN_MULTITENANT", "1")
    monkeypatch.delenv("BRAIN_PERSONA_NAME", raising=False)
    with pytest.raises(RuntimeError, match="cross-contaminates"):
        _resolve_persona("")
    # An explicit persona still resolves fine in multitenant mode.
    assert _resolve_persona("The Visionary") == "The Visionary"


def test_sql_quote_escapes_filter_metacharacters():
    from brain.second_brain.store import _sql_quote

    assert _sql_quote("plain-tag") == "plain-tag"
    assert _sql_quote("o'brien") == "o''brien"
    # Backslashes are stripped, quotes doubled — nothing can terminate the literal.
    assert "'" not in _sql_quote("x' OR '1'='1").replace("''", "")


# ── sequence predictor (bookkeeping + synonym hot-reload) ─────────────────────


def _predictor(tmp_path, monkeypatch):
    import brain.sequence_predictor as sp

    monkeypatch.setattr(sp, "_WEIGHTS_PATH", str(tmp_path / "sequence_weights.json"))
    monkeypatch.setattr(sp, "_SYNONYMS_PATH", str(tmp_path / "angle_synonyms.json"))
    return sp.SequencePredictor()


def test_save_preserves_foreign_bookkeeping_keys(tmp_path, monkeypatch):
    import brain.sequence_predictor as sp

    p = _predictor(tmp_path, monkeypatch)
    p.record("alpha-one")
    p.record("beta-two")
    p.save()
    # Another writer (the sleep synonym pass) stamps a bookkeeping key.
    path = tmp_path / "sequence_weights.json"
    data = json.loads(path.read_text())
    data["last_synonym_pass_ts"] = 12345.0
    path.write_text(json.dumps(data))
    # A later predictor save must carry the stamp through, not drop it.
    p.record("gamma-three")
    p.save()
    assert json.loads(path.read_text())["last_synonym_pass_ts"] == 12345.0
    # And a fresh load round-trips it into _extra.
    p2 = sp.SequencePredictor()
    monkeypatch.setattr(sp, "_WEIGHTS_PATH", str(path))
    p2.load()
    assert p2._extra.get("last_synonym_pass_ts") == 12345.0


def test_synonyms_hot_reload_on_file_change(tmp_path, monkeypatch):
    p = _predictor(tmp_path, monkeypatch)
    assert p._canonical("system-architecture") == "system-architecture"
    syn = tmp_path / "angle_synonyms.json"
    syn.write_text(json.dumps({"system-architecture": "architecture"}))
    # No restart, no explicit load() — the next canonicalization picks it up.
    assert p._canonical("system-architecture") == "architecture"


# ── chunk memory (suppression recovery + reinforcement) ───────────────────────


def _active_chunk_sub():
    from brain.clusters.chunk_memory import ChunkMemorySubsystem

    sub = ChunkMemorySubsystem()
    sub._chunks = {
        "k1": {"sequence": [], "occurrences": 5, "state": "active"},
        "k2": {"sequence": [], "occurrences": 9, "state": "active"},
    }
    return sub


def test_suppression_lifted_by_new_mining_pass(tmp_path, monkeypatch):
    import brain.clusters.chunk_memory as cm

    chunks_path = tmp_path / "chunks.json"
    chunks_path.write_text(
        json.dumps({"chunks": {"k1": {"sequence": [], "occurrences": 5, "state": "active"}}})
    )
    monkeypatch.setattr(cm, "_CHUNKS_PATH", chunks_path)
    sub = cm.ChunkMemorySubsystem()
    sub.suppress("chunk:k1")
    assert "k1" in sub._suppressed
    # A new mining pass rewrites chunks.json (new mtime) → suppression lifts.
    import os
    import time

    data = {"chunks": {"k1": {"sequence": [], "occurrences": 6, "state": "active"}}}
    chunks_path.write_text(json.dumps(data))
    os.utime(chunks_path, (time.time() + 5, time.time() + 5))
    sub._load()
    assert "k1" not in sub._suppressed


def test_reinforce_biases_priming_order():
    sub = _active_chunk_sub()
    # k2 leads on mined occurrences (9 vs 5); reinforce k1 past it.
    for _ in range(5):
        sub.reinforce("chunk:k1")
    ranked = sorted(
        sub._chunks.items(),
        key=lambda kc: kc[1].get("occurrences", 0) + sub._session_success.get(kc[0], 0),
        reverse=True,
    )
    assert ranked[0][0] == "k1"
    # Suppression wipes the session credit.
    sub.suppress("chunk:k1")
    assert "k1" not in sub._session_success


# ── cross-learning persistence + read surface ─────────────────────────────────


def test_hypothesis_store_persists_and_reads_established(tmp_path):
    from brain import cross_learning
    from brain.hypothesis_store import HypothesisStore

    path = tmp_path / "hypotheses.json"
    store = HypothesisStore()
    for src in ("cust_a", "cust_b", "cust_c"):  # k=3 distinct → established
        store.add("grief can surface at a normally-happy topic", src)
    store.add("a provisional one-off", "cust_a")
    cross_learning.save_store(store, path)

    loaded = cross_learning.load_store(path)
    assert len(loaded.established()) == 1
    assert len(loaded.provisional()) == 1
    principles = cross_learning.established_principles(n=3, path=path)
    assert principles == ["grief can surface at a normally-happy topic"]


def test_established_principles_empty_without_store(tmp_path):
    from brain import cross_learning

    assert cross_learning.established_principles(path=tmp_path / "nope.json") == []


# ── learning monitor: routing convergence + chunk metrics ─────────────────────


def test_session_metrics_routing_and_chunk_surfaces():
    from eval.learning_monitor import LearningMonitor

    mon = LearningMonitor()
    mon._turn_metrics = [{"llm_calls_saved": 0}, {"llm_calls_saved": 0}]

    class FakeDmn:
        _routing_weights = {"craft": 1.6, "people": 0.8}
        _routing_weights_loaded = {"craft": 1.2, "people": 1.0}

    class FakeChunks:
        name = "chunk_memory"
        _session_success = {"k1": 3}
        _suppressed = {"k2"}
        _chunks = {"k1": {"state": "active"}, "k2": {"state": "candidate"}}

    s = mon.session_metrics(dmn=FakeDmn(), chunks=FakeChunks())
    # |1.6-1.2| + |0.8-1.0| = 0.6 total movement
    assert s["routing_weight_session_delta"] == pytest.approx(0.6)
    # distance-from-rest now (0.6+0.2) vs at load (0.2+0.0) → net +0.6 learning
    assert s["routing_weight_net_learning"] == pytest.approx(0.6)
    assert s["routing_weights_tracked"] == 2
    assert s["chunk_reuse_count"] == 3
    assert s["chunks_reused_distinct"] == 1
    assert s["chunks_suppressed"] == 1
    assert s["chunks_active"] == 1


# ── Stage 1: org JWT + tenant env hygiene + mandates ──────────────────────────


def test_mint_org_token_claims(monkeypatch):
    import jwt as pyjwt

    from brain.gateway.org_token import mint_org_token

    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    org = "11111111-2222-3333-4444-555555555555"
    tok = mint_org_token(org)
    claims = pyjwt.decode(tok, "test-secret", algorithms=["HS256"], audience="authenticated")
    assert claims["sub"] == org
    assert claims["role"] == "authenticated"
    assert claims["exp"] > claims["iat"]


def test_mint_org_token_empty_without_secret(monkeypatch):
    from brain.gateway.org_token import mint_org_token

    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    assert mint_org_token("some-org") == ""


def test_mandate_catalog_empty_in_local_mode(monkeypatch):
    import brain.mandates as mandates

    monkeypatch.setenv("BRAIN_STORAGE_BACKEND", "local")
    mandates._catalog = None
    assert mandates.catalog() == {}
    mandates._catalog = None  # don't leak cache into other tests


def test_episode_carries_engine_fields():
    from brain.second_brain.store import Episode

    ep = Episode(
        session_id="s",
        turn_id="t",
        ts=0.0,
        user_input="hi",
        entity_response="hello",
        topic_tags=[],
        emotion_state="neutral",
        user_emotion="neutral",
        entities=[],
        neuromod_snapshot={},
        surprise_score=0.0,
        end_user_id="cust_42",
        mandate_id="billing",
    )
    assert ep.end_user_id == "cust_42" and ep.mandate_id == "billing"


# ── persona surface sync (chemistry table ↔ reward table ↔ UI mirrors) ────────


def test_persona_surfaces_in_sync():
    """Every persona must exist on all four surfaces: PERSONA_CHEMISTRY,
    _PERSONA_REWARD_WEIGHTS, the settings-ui.js PERSONA_CHEM mirror, and the
    settings-data.js personas list. Catches the drift the dead TRAIT_DIALS
    duplicate suffered."""
    import re
    from pathlib import Path

    from brain.neuron import _PERSONA_REWARD_WEIGHTS
    from brain.persona_chem import CHANNELS, PERSONA_CHEMISTRY, PERSONA_TRAIT_DEFAULTS

    def slug(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")

    chem_names = set(PERSONA_CHEMISTRY)

    # 1. Reward weights cover every persona, with the full source vocabulary.
    sources = {"correctness", "connection", "novelty", "aesthetic", "relief", "mastery", "levity"}
    for name in chem_names:
        weights = _PERSONA_REWARD_WEIGHTS.get(slug(name))
        assert weights, f"{name} missing from _PERSONA_REWARD_WEIGHTS"
        assert set(weights) == sources, f"{name} reward sources incomplete: {set(weights)}"

    # 2. UI chemistry mirror matches the python table exactly.
    ui = Path("brain/ui/settings-ui.js").read_text()
    for name, chem in PERSONA_CHEMISTRY.items():
        m = re.search(rf"'{re.escape(name)}':\s*\{{([^}}]*)\}}", ui)
        assert m, f"{name} missing from settings-ui.js PERSONA_CHEM"
        ui_vals = dict(re.findall(r"'?([A-Za-z5]+)'?:\s*([0-9.]+)", m.group(1)))
        for ch in CHANNELS:
            assert abs(float(ui_vals[ch]) - chem[ch]) < 1e-9, f"{name}.{ch} drifted in UI mirror"

    # 3. The persona picker lists every table persona.
    data = Path("brain/ui/settings-data.js").read_text()
    for name in chem_names:
        assert f"id: '{name}'" in data, f"{name} missing from settings-data.js personas"

    # 4. Trait defaults only reference real settings keys.
    from brain.settings import DEFAULTS

    for name, overrides in PERSONA_TRAIT_DEFAULTS.items():
        assert name in chem_names, f"trait defaults for unknown persona {name}"
        for key in overrides:
            assert key in DEFAULTS, f"{name} trait default {key} not in settings DEFAULTS"


def test_persona_trait_defaults_materialize(tmp_path, monkeypatch):
    import brain.persona_chem as pc

    monkeypatch.setattr(pc, "_PERSONAS_ROOT", tmp_path)
    out = pc.materialize_into_settings("The Poet", {})
    assert out["affect_carryover_da_threshold"] == 0.06
    out2 = pc.materialize_into_settings("The Stoic", {})
    assert out2["affect_carryover_da_threshold"] == 0.25
    # Personas without overrides leave the key untouched.
    out3 = pc.materialize_into_settings("The Visionary", {})
    assert "affect_carryover_da_threshold" not in out3
