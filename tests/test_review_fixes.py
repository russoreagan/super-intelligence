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

    from brain.gateway import org_token

    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    # Legacy HS256 project (symmetric secret still accepted): a token is minted.
    monkeypatch.setattr(org_token, "_uses_asymmetric_signing", lambda: False)
    org = "11111111-2222-3333-4444-555555555555"
    tok = org_token.mint_org_token(org)
    claims = pyjwt.decode(tok, "test-secret", algorithms=["HS256"], audience="authenticated")
    assert claims["sub"] == org
    assert claims["role"] == "authenticated"
    assert claims["exp"] > claims["iat"]


def test_mint_org_token_empty_without_secret(monkeypatch):
    from brain.gateway.org_token import mint_org_token

    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    assert mint_org_token("some-org") == ""


def test_mint_org_token_empty_under_asymmetric_signing(monkeypatch):
    """When the project signs JWTs asymmetrically the legacy HS256 secret is inert,
    so no token is minted — the tenant falls back to the service-role key instead of
    booting with a credential Supabase rejects (the gateway-wedge / stuck-load bug)."""
    from brain.gateway import org_token

    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    monkeypatch.setattr(org_token, "_uses_asymmetric_signing", lambda: True)
    assert org_token.mint_org_token("some-org") == ""


def _mock_jwks(monkeypatch, keys):
    from brain.gateway import org_token

    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setattr(org_token, "_asymmetric", None)  # reset cache

    class _Resp:
        def read(self_inner):
            return json.dumps({"keys": keys}).encode()

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

    monkeypatch.setattr(org_token.urllib.request, "urlopen", lambda req, timeout=5: _Resp())


def test_uses_asymmetric_signing_true_for_ec_keys(monkeypatch):
    from brain.gateway import org_token

    _mock_jwks(monkeypatch, [{"kty": "EC", "alg": "ES256", "kid": "k1"}])
    assert org_token._uses_asymmetric_signing() is True


def test_uses_asymmetric_signing_false_when_no_asymmetric_keys(monkeypatch):
    from brain.gateway import org_token

    _mock_jwks(monkeypatch, [])  # legacy HS256 project publishes no JWKS keys
    assert org_token._uses_asymmetric_signing() is False


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
    from brain.persona_chem import (
        _NONCHEM_DIAL_MAP,
        CHANNELS,
        PERSONA_CHEMISTRY,
        PERSONA_COG_POSITIONS,
    )

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

    # 4. Cognitive dials only reference real settings keys, and every persona's
    #    cog positions only reference real dials (persona-system v2 mechanism).
    from brain.settings import DEFAULTS

    for dial_id, rows in _NONCHEM_DIAL_MAP.items():
        for key, *_rest in rows:
            assert key in DEFAULTS, f"dial {dial_id} references unknown settings key {key}"
    for name, positions in PERSONA_COG_POSITIONS.items():
        assert name in chem_names, f"cog positions for unknown persona {name}"
        for dial_id in positions:
            assert dial_id in _NONCHEM_DIAL_MAP, f"{name} references unknown dial {dial_id}"


def test_persona_trait_defaults_materialize(tmp_path, monkeypatch):
    import brain.persona_chem as pc

    monkeypatch.setattr(pc, "_PERSONAS_ROOT", tmp_path)
    # The Poet's high lingering dial (0.88) shifts affect_carryover_da_threshold
    # below its 0.10 default: 0.10 - 0.06·(0.88-0.5)·2 = 0.0544.
    out = pc.materialize_into_settings("The Poet", {})
    assert out["affect_carryover_da_threshold"] == 0.0544
    # The Stoic is the flat-neutral control — absent from PERSONA_COG_POSITIONS,
    # so no cognitive-dial keys are materialized at all.
    out2 = pc.materialize_into_settings("The Stoic", {})
    assert "affect_carryover_da_threshold" not in out2
    # The Visionary's lingering dial sits at the neutral midpoint (0.50) → the key
    # is written at its base default with zero offset.
    out3 = pc.materialize_into_settings("The Visionary", {})
    assert out3["affect_carryover_da_threshold"] == 0.10


# ── OpenAI provider integration (router remap + dispatch) ─────────────────────


def test_provider_for_dispatch():
    from brain.model_router import _provider_for

    assert _provider_for("claude-sonnet-4-6") == "anthropic"
    assert _provider_for("gemini-2.5-flash") == "google"
    assert _provider_for("local-general") == "local"
    assert _provider_for("runpod-code") == "local"
    assert _provider_for("gpt-5.1") == "openai"
    # Unknown ids route via the OpenAI-compatible client (base_url providers).
    assert _provider_for("llama-3.3-70b-versatile") == "openai"


def test_cloud_provider_remap_respects_motor_exemption(monkeypatch):
    from brain.model_router import MODEL_MAP, _remap_cloud_provider
    from brain.settings import settings

    monkeypatch.setattr(
        settings, "_data", {**settings._data, "cloud_provider": "openai"}, raising=False
    )
    # Cognition clusters reroute to the configured OpenAI models.
    assert _remap_cloud_provider("claude-sonnet-4-6", "frontal") == MODEL_MAP["gpt"]
    assert _remap_cloud_provider("claude-haiku-4-5-20251001", "temporal") == MODEL_MAP["gpt-mini"]
    # Motor keeps Anthropic — its tool-use loop is Anthropic-shaped.
    assert _remap_cloud_provider("claude-sonnet-4-6", "motor") == "claude-sonnet-4-6"
    # Non-Claude routes are untouched.
    assert _remap_cloud_provider("gemini-2.5-flash", "frontal") == "gemini-2.5-flash"
    assert _remap_cloud_provider("local-general", "frontal") == "local-general"


def test_cloud_provider_default_is_noop(monkeypatch):
    from brain.model_router import _remap_cloud_provider
    from brain.settings import settings

    monkeypatch.setattr(
        settings, "_data", {**settings._data, "cloud_provider": "anthropic"}, raising=False
    )
    assert _remap_cloud_provider("claude-sonnet-4-6", "frontal") == "claude-sonnet-4-6"


def test_openai_tts_instruction_mapping():
    from brain.pns import PNS

    # Same cluster vocabulary as Flash VoiceSettings — providers can't drift.
    assert "animated" in PNS._openai_instruction_from_emotion("excited")
    assert "warmly" in PNS._openai_instruction_from_emotion("affectionate")
    assert "weight" in PNS._openai_instruction_from_emotion("somber")
    assert PNS._openai_instruction_from_emotion(None) == PNS._OPENAI_TTS_DEFAULT_INSTRUCTION


def test_pcm_resample_length_and_type():
    import numpy as np

    from brain.pns import PNS

    src = (np.sin(np.linspace(0, 100, 24000)) * 10000).astype(np.int16).tobytes()
    out = PNS._pcm_resample(src, 24000, 22050)
    assert len(out) % 2 == 0
    assert abs(len(out) // 2 - 22050) <= 1  # 1s of audio stays ~1s


# ── per-persona cognitive fingerprint: JS↔Python dial-map sync ─────────────────


def _parse_js_dial_maps(js: str) -> dict:
    """Extract { dial_id: {key: (dir, span)} } from the COGNITIVE_DIALS + lingering
    blocks of settings-ui.js, so the test fails loudly if the JS and Python dial
    maps drift apart (they MUST match — both compute the same materialized values)."""
    import re

    out: dict[str, dict] = {}
    # Each dial block: id: 'x', ... map: [ {...}, {...} ]  (map ends at the next ']').
    for m in re.finditer(r"\{\s*id:\s*'([a-z-]+)'.*?map:\s*\[(.*?)\]", js, re.S):
        did, body = m.group(1), m.group(2)
        entries = {}
        for e in re.finditer(
            r"\{\s*key:\s*'([A-Za-z_0-9]+)',\s*dir:\s*([+-]?\d+),\s*span:\s*([0-9.]+)", body
        ):
            entries[e.group(1)] = (int(e.group(2)), float(e.group(3)))
        if entries:
            out[did] = entries
    return out


def test_cognitive_dial_map_js_python_sync():
    from pathlib import Path

    from brain.persona_chem import _NONCHEM_DIAL_MAP

    js = Path("brain/ui/settings-ui.js").read_text()
    js_maps = _parse_js_dial_maps(js)
    for dial_id, rows in _NONCHEM_DIAL_MAP.items():
        assert dial_id in js_maps, f"{dial_id} missing from settings-ui.js dial maps"
        py = {key: (d, s) for (key, d, s, _lo, _hi) in rows}
        js_entry = js_maps[dial_id]
        assert set(py) == set(js_entry), f"{dial_id} key drift: python={set(py)} js={set(js_entry)}"
        for key, (pd, ps) in py.items():
            jd, jsp = js_entry[key]
            assert pd == jd, f"{dial_id}.{key} dir drift: py={pd} js={jd}"
            assert abs(ps - jsp) < 1e-9, f"{dial_id}.{key} span drift: py={ps} js={jsp}"


def test_cog_positions_materialize_and_skip_stoic(tmp_path, monkeypatch):
    import brain.persona_chem as pc

    monkeypatch.setattr(pc, "_PERSONAS_ROOT", tmp_path)
    # High-focus persona raises the workspace bar; low-emotionality lowers the
    # flock target — both real per-persona behavior, not just UI cosmetics.
    sd = pc.materialize_into_settings("The Analyst", {})
    assert sd["salience_workspace_threshold"] > 0.6
    assert sd["flock_sigma_target_low"] < 0.9
    # The Stoic is the flat control — no cognitive keys materialized.
    sd2 = pc.materialize_into_settings("The Stoic", {})
    assert "salience_workspace_threshold" not in sd2
    assert "modulation_gain" not in sd2


# ── multitenant persona isolation (run.py _route_persona_state) ───────────────


def _mt_env(monkeypatch, tmp_path, persona_in_settings):
    import json as _json

    root = tmp_path / "second_brain"
    root.mkdir(parents=True, exist_ok=True)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(_json.dumps({"persona_name": persona_in_settings}))
    monkeypatch.setenv("BRAIN_MULTITENANT", "1")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(root))
    monkeypatch.setenv("BRAIN_SETTINGS_PATH", str(settings_path))
    monkeypatch.delenv("BRAIN_PERSONA_NAME", raising=False)
    monkeypatch.delenv("BRAIN_WIRING_PATH", raising=False)
    monkeypatch.delenv("BRAIN_WIRING_HISTORY_DIR", raising=False)
    return root


def test_multitenant_namespaces_state_per_persona(tmp_path, monkeypatch):
    """The org volume root must never be shared: jobs/research/wiring/dmn state
    all derive from SECOND_BRAIN_PATH, so it has to be personas/<slug>."""
    import os

    from brain.run import _route_persona_state

    root = _mt_env(monkeypatch, tmp_path, "The Companion")
    _route_persona_state()
    assert os.environ["BRAIN_PERSONA_NAME"] == "the_companion"
    assert os.environ["SECOND_BRAIN_PATH"] == str(root / "personas" / "the_companion")
    assert os.environ["BRAIN_WIRING_PATH"] == str(
        root / "personas" / "the_companion" / "wiring.json"
    )


def test_multitenant_settings_json_wins_over_stale_env(tmp_path, monkeypatch):
    """After a persona switch + /restart re-exec, the inherited env still names
    the OLD persona — settings.json is the source of truth."""
    import os

    from brain.run import _route_persona_state

    _mt_env(monkeypatch, tmp_path, "The Companion")
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Analyst")  # stale, raw-name form
    _route_persona_state()
    assert os.environ["BRAIN_PERSONA_NAME"] == "the_companion"


def test_multitenant_renamespacing_does_not_nest(tmp_path, monkeypatch):
    """A re-exec inherits the already-namespaced SECOND_BRAIN_PATH; the bootstrap
    must normalize back to the org root, not produce personas/x/personas/y."""
    import os

    from brain.run import _route_persona_state

    root = _mt_env(monkeypatch, tmp_path, "The Companion")
    monkeypatch.setenv(
        "SECOND_BRAIN_PATH", str(root / "personas" / "the_analyst")
    )  # prior persona's namespace, inherited across exec
    _route_persona_state()
    assert os.environ["SECOND_BRAIN_PATH"] == str(root / "personas" / "the_companion")
