"""
Connector org-isolation — env-pinned MCP connectors (BRAIN_CMA_MCP_SERVERS) are
process-global, so in multi-tenant hosting they must apply ONLY to the owning org
(BRAIN_CMA_MCP_OWNER_ORG). Otherwise every tenant brain inherits another org's
connectors (e.g. russ's `trading` showing up in elyceum's tool menu).

Regression: an unowned tenant must NOT see the env-pinned registry.
"""

from __future__ import annotations

from brain.clusters.cma_executor import is_env_managed

_ENV_JSON = '{"servers":[{"name":"trading","url":"https://x/mcp"}]}'
_OWNER = "5d5b9e0b-0821-4dea-b493-6408bf3db463"  # russ's org
_OTHER = "ae3ca444-fe24-412f-9000-237967588823"  # elyceum's org


def _clear(monkeypatch):
    for k in ("BRAIN_CMA_MCP_SERVERS", "BRAIN_CMA_MCP_OWNER_ORG", "BRAIN_ORG_ID", "BRAIN_USER_ID"):
        monkeypatch.delenv(k, raising=False)


def test_no_env_means_not_managed(monkeypatch):
    _clear(monkeypatch)
    assert is_env_managed() is False


def test_env_without_owner_pin_applies_everywhere(monkeypatch):
    # Single-tenant / dev: no owner pin → env applies (backward compatible).
    _clear(monkeypatch)
    monkeypatch.setenv("BRAIN_CMA_MCP_SERVERS", _ENV_JSON)
    monkeypatch.setenv("BRAIN_ORG_ID", _OTHER)
    assert is_env_managed() is True


def test_owner_org_sees_env_connectors(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BRAIN_CMA_MCP_SERVERS", _ENV_JSON)
    monkeypatch.setenv("BRAIN_CMA_MCP_OWNER_ORG", _OWNER)
    monkeypatch.setenv("BRAIN_ORG_ID", _OWNER)
    assert is_env_managed() is True


def test_non_owner_org_is_isolated(monkeypatch):
    # THE FIX: elyceum's brain must not inherit russ's env-pinned connectors.
    _clear(monkeypatch)
    monkeypatch.setenv("BRAIN_CMA_MCP_SERVERS", _ENV_JSON)
    monkeypatch.setenv("BRAIN_CMA_MCP_OWNER_ORG", _OWNER)
    monkeypatch.setenv("BRAIN_ORG_ID", _OTHER)
    assert is_env_managed() is False


def test_owner_match_via_user_id_fallback(monkeypatch):
    # Personal orgs key org_id == user_id; BRAIN_USER_ID is accepted as the org.
    _clear(monkeypatch)
    monkeypatch.setenv("BRAIN_CMA_MCP_SERVERS", _ENV_JSON)
    monkeypatch.setenv("BRAIN_CMA_MCP_OWNER_ORG", _OWNER)
    monkeypatch.setenv("BRAIN_USER_ID", _OWNER)
    assert is_env_managed() is True


def test_owner_pin_but_unknown_org_fails_open(monkeypatch):
    # No org id on the process → can't prove mismatch → apply env (matches the
    # pre-fix single-tenant posture; isolation relies on tenant brains setting
    # BRAIN_ORG_ID, which the provisioner does).
    _clear(monkeypatch)
    monkeypatch.setenv("BRAIN_CMA_MCP_SERVERS", _ENV_JSON)
    monkeypatch.setenv("BRAIN_CMA_MCP_OWNER_ORG", _OWNER)
    assert is_env_managed() is True
