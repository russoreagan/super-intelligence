"""
The gateway's pre-spawn Anthropic-key gate (_has_anthropic).

Tenants are BYO-key: the gateway won't spawn a brain until the org has an Anthropic
key on file. That check must read the key the SAME way the provisioner injects it —
via the service role, keyed by tenant id — not via a user-token status RPC that can
report no key even when one exists (which silently blocked every spawn and left the
UI stuck on the booting screen).
"""

from __future__ import annotations

from types import SimpleNamespace

from brain.gateway import server as gw


def _req(user):
    return SimpleNamespace(state=SimpleNamespace(user=user))


async def _async_tenant(sub):
    return sub


async def test_gate_true_when_service_role_finds_anthropic(monkeypatch):
    import brain.vault as vault

    monkeypatch.setattr(gw, "_tenant_for", _async_tenant)
    monkeypatch.setattr(
        vault, "fetch_user_keys", lambda uid: {"anthropic": "sk-x", "deepgram": "d"}
    )
    assert await gw._has_anthropic(_req({"sub": "org-1"})) is True


async def test_gate_false_when_no_anthropic_key(monkeypatch):
    import brain.vault as vault

    monkeypatch.setattr(gw, "_tenant_for", _async_tenant)
    monkeypatch.setattr(vault, "fetch_user_keys", lambda uid: {"deepgram": "d"})
    assert await gw._has_anthropic(_req({"sub": "org-1"})) is False


async def test_gate_false_without_user():
    assert await gw._has_anthropic(_req(None)) is False


async def test_gate_false_when_lookup_errors(monkeypatch):
    import brain.vault as vault

    def _boom(uid):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(gw, "_tenant_for", _async_tenant)
    monkeypatch.setattr(vault, "fetch_user_keys", _boom)
    assert await gw._has_anthropic(_req({"sub": "org-1"})) is False
