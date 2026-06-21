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
