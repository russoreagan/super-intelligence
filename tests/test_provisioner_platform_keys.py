"""Tenants are BYO-key for every provider, not only Anthropic.

Only ANTHROPIC_API_KEY was stripped from a tenant spawn, so an org whose vault
held no Google key inherited the platform's GOOGLE_API_KEY and billed it for every
embedding. The strip list now covers every provider the vault knows, with an
explicit opt-in (BRAIN_TENANT_PLATFORM_KEYS) for providers the platform chooses
to share.
"""

from __future__ import annotations

import brain.provisioner as pv


def test_every_provider_key_is_stripped_by_default(monkeypatch):
    monkeypatch.delenv("BRAIN_TENANT_PLATFORM_KEYS", raising=False)
    stripped = set(pv.platform_secrets_to_strip(has_org_jwt=True))
    for name in (
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "DEEPGRAM_API_KEY",
        "ELEVENLABS_API_KEY",
        "RUNPOD_API_KEY",
        "RESEND_API_KEY",
        "BRAIN_API_KEYS",
        "BRAIN_API_KEY",
        "SUPABASE_SERVICE_KEY",
    ):
        assert name in stripped, name


def test_service_key_survives_without_an_org_jwt(monkeypatch):
    monkeypatch.delenv("BRAIN_TENANT_PLATFORM_KEYS", raising=False)
    assert "SUPABASE_SERVICE_KEY" not in pv.platform_secrets_to_strip(has_org_jwt=False)


def test_opt_in_keeps_named_platform_keys():
    stripped = set(pv.platform_secrets_to_strip(has_org_jwt=True, allow="deepgram, Google"))
    assert "DEEPGRAM_API_KEY" not in stripped
    assert "GOOGLE_API_KEY" not in stripped
    assert "ANTHROPIC_API_KEY" in stripped
    assert "ELEVENLABS_API_KEY" in stripped


def test_env_opt_in_is_read_when_allow_is_omitted(monkeypatch):
    monkeypatch.setenv("BRAIN_TENANT_PLATFORM_KEYS", "elevenlabs")
    stripped = set(pv.platform_secrets_to_strip(has_org_jwt=True))
    assert "ELEVENLABS_API_KEY" not in stripped
    assert "GOOGLE_API_KEY" in stripped
