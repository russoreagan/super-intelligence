"""Unit tests for the additive Vertex AI provider in brain/model_router.py.

No network: these cover routing (_provider_for), the cloud_provider=vertex remap,
the Vertex config resolution, and the SA-JSON → ADC materialization. Live Gemini-
on-Vertex is verified out of band (needs ADC + an enabled project).
"""

from __future__ import annotations

import os

import pytest

from brain.model_router import (
    MODEL_MAP,
    ModelRouter,
    _provider_for,
    _remap_cloud_provider,
)


def test_provider_for_routes_vertex():
    assert _provider_for("vertex-gemini-2.5-flash") == "vertex"
    assert _provider_for(MODEL_MAP["vertex-claude"]) == "vertex"
    # existing routes unchanged (additive guarantee)
    assert _provider_for("claude-haiku-4-5-20251001") == "anthropic"
    assert _provider_for("gemini-2.5-flash") == "google"
    assert _provider_for("gpt-5.1") == "openai"


def test_model_map_has_vertex_keys():
    for k in ("vertex-gemini-flash", "vertex-gemini-pro", "vertex-claude", "vertex-claude-haiku"):
        assert MODEL_MAP[k].startswith("vertex-")


def test_remap_cloud_provider_vertex():
    from brain import settings as S

    try:
        S.settings.update({"cloud_provider": "vertex", "enable_vertex": 1})
        assert _remap_cloud_provider("claude-sonnet-4-6", "frontal").startswith("vertex-")
        assert "haiku" in _remap_cloud_provider("claude-haiku-4-5", "frontal")
        # motor cluster is exempt; non-Claude routes untouched
        assert _remap_cloud_provider("claude-sonnet-4-6", "motor") == "claude-sonnet-4-6"
        assert _remap_cloud_provider("gemini-2.5-flash", "frontal") == "gemini-2.5-flash"
        # enable_vertex off → stays on Anthropic even if cloud_provider=vertex
        S.settings.update({"enable_vertex": 0})
        assert _remap_cloud_provider("claude-sonnet-4-6", "frontal") == "claude-sonnet-4-6"
    finally:
        S.settings.update({"cloud_provider": "anthropic", "enable_vertex": 0})


def test_remap_openai_path_unchanged():
    from brain import settings as S

    try:
        S.settings.update({"cloud_provider": "openai"})
        assert _remap_cloud_provider("claude-sonnet-4-6", "frontal") == MODEL_MAP["gpt"]
        assert _remap_cloud_provider("claude-haiku-4-5", "frontal") == MODEL_MAP["gpt-mini"]
    finally:
        S.settings.update({"cloud_provider": "anthropic"})


def test_vertex_cfg_requires_project(monkeypatch):
    from brain import settings as S

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    r = ModelRouter()
    S.settings.update({"vertex_project": "", "vertex_location": "us-central1"})
    try:
        with pytest.raises(RuntimeError):
            r._vertex_cfg()
        S.settings.update({"vertex_project": "elyceum-ai"})
        assert r._vertex_cfg() == ("elyceum-ai", "us-central1")
    finally:
        S.settings.update({"vertex_project": ""})


def test_vertex_settings_and_vault_wiring():
    from brain import settings as S
    from brain import vault

    assert S.DEFAULTS["enable_vertex"] == 0  # default OFF
    assert S.DEFAULTS["cloud_provider"] == "anthropic"  # default unchanged
    assert S.API_KEY_ENV["api_key_google_vertex_sa"] == "GOOGLE_VERTEX_SA_JSON"
    assert "google_vertex_sa" in vault.VALID_PROVIDERS


def test_materialize_vertex_credentials_writes_file(monkeypatch):
    from brain import settings as S

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GOOGLE_VERTEX_SA_JSON", '{"type":"service_account","project_id":"x"}')
    S._materialize_vertex_credentials()
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    assert path and os.path.exists(path)
    assert '"service_account"' in open(path, encoding="utf-8").read()
    os.unlink(path)


def test_materialize_skips_when_adc_already_set(monkeypatch):
    from brain import settings as S

    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/preset/path/adc.json")
    monkeypatch.setenv("GOOGLE_VERTEX_SA_JSON", '{"type":"service_account"}')
    S._materialize_vertex_credentials()
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/preset/path/adc.json"


# ── prompt caching (Claude on Vertex) ───────────────────────────────────────────
def _fake_anthropic_capture(captured: dict):
    """A stand-in AnthropicVertex client that records messages.create() kwargs."""

    class _Resp:
        content = [type("C", (), {"text": "ok"})()]
        usage = type(
            "U", (), {"input_tokens": 5, "output_tokens": 3, "cache_read_input_tokens": 100}
        )()

    class _Msgs:
        async def create(self, **kw):
            captured.update(kw)
            return _Resp()

    class _Client:
        messages = _Msgs()

    return _Client()


def test_vertex_claude_marks_cache_control():
    import asyncio

    captured: dict = {}
    r = ModelRouter()
    r._vertex_anthropic_client = _fake_anthropic_capture(captured)  # bypass _get_vertex_anthropic
    text, _i, _o = asyncio.run(
        r._call_vertex_claude(
            "claude-sonnet-4-5@x",
            "SYSTEM",
            [{"role": "user", "content": "hello"}],
            50,
            cached_context="CONTEXT",
        )
    )
    assert text == "ok"
    # system carries two cached blocks: identity + per-session context
    sys_blocks = captured["system"]
    assert [b["text"] for b in sys_blocks] == ["SYSTEM", "CONTEXT"]
    assert all(b["cache_control"]["type"] == "ephemeral" for b in sys_blocks)
    # last user message is cache-marked for intra-turn reuse
    last = captured["messages"][-1]["content"]
    assert isinstance(last, list) and last[-1]["cache_control"]["type"] == "ephemeral"


def test_vertex_gemini_folds_context_into_system(monkeypatch):
    import asyncio

    r = ModelRouter()
    captured: dict = {}

    async def _fake_gen(client, model_id, system_prompt, messages, max_tokens):
        captured["model"] = model_id
        captured["system"] = system_prompt
        return ("g", 1, 1)

    monkeypatch.setattr(r, "_gemini_generate", _fake_gen)
    monkeypatch.setattr(r, "_get_vertex_gemini", lambda: object())
    out = asyncio.run(
        r._call_vertex(
            "vertex-gemini-2.5-flash",
            "SYSTEM",
            [{"role": "user", "content": "hi"}],
            10,
            cached_context="CONTEXT",
        )
    )
    assert out[0] == "g"
    assert captured["model"] == "gemini-2.5-flash"  # "vertex-" stripped
    assert "SYSTEM" in captured["system"] and "CONTEXT" in captured["system"]
