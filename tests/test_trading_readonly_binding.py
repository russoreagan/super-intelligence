"""
Connector binding for the trading debate agents.

The brain reaches the trading app over MCP connectors. The WRITE-capable
connector ("trading") is bound to the main trading agent only; the six read-only
reasoning/debate agents (bull/bear/risk/pm/mispricing/reflection) are bound to a
separate READ-ONLY connector ("trading-readonly").

A binding is the agent's permission overrides (motor_user_connectors /
motor_self_connectors). The LIVE enforcement path is:

    motor_cortex.execute -> bind_agent(agent_id)            # contextvar in scope
    -> _dispatch_cloud -> _mode_policy()                    # org allowlist ∩ agent
    -> cma_executor.set_connector_filter(names)             # scope the session
    -> _active_mcp_servers()/_mcp_server_decls()            # only those declared

These tests pin both ends of that path:
  • _mode_policy resolves a debate agent's filter to {trading-readonly} only
    (EXCLUDES the write connector), and the main agent's to {trading};
  • the CMA executor's session-build honours the filter, so the write
    connector's tools are never even declared for a debate session.
"""

from __future__ import annotations

import pytest

from brain.agent_ctx import bind_agent
from brain.clusters.cma_executor import CMAExecutor
from brain.clusters.motor_cortex import MotorCortexCluster
from brain.settings import settings

READONLY = "trading-readonly"
FULL = "trading"

DEBATE_PERMS = {"motor_user_connectors": READONLY, "motor_self_connectors": READONLY}
MAIN_PERMS = {"motor_user_connectors": FULL, "motor_self_connectors": FULL}


class _Probe(MotorCortexCluster):
    """Bypass the heavy cluster __init__; _mode_policy only reads _self_mode and
    the bound agent (via the inherited _bound_agent_perms staticmethod)."""

    def __init__(self, self_mode: bool = False) -> None:
        self._self_mode = self_mode


@pytest.fixture(autouse=True)
def _org_connectors_unset():
    # The org-level allowlist must stay empty (= "all configured") so the per-agent
    # allowlist is the binding and isn't intersected down to nothing. This is also
    # the deployment guard: never set org motor_*_connectors to just "trading", or
    # it would null out the debate agents' "trading-readonly" set.
    old = {k: settings.get(k) for k in ("motor_user_connectors", "motor_self_connectors")}
    settings.update({"motor_user_connectors": "", "motor_self_connectors": ""})
    yield
    settings.update(old)


def _resolve(perms, self_mode=False):
    probe = _Probe(self_mode)
    with bind_agent("persona.mandate", permissions=perms):
        return probe._mode_policy()["connectors"]


def test_debate_agent_user_directed_gets_readonly_only():
    conns = _resolve(DEBATE_PERMS, self_mode=False)
    assert conns == {READONLY}
    assert FULL not in conns  # write connector never reaches a debate dispatch


def test_debate_agent_self_directed_gets_readonly_only():
    # DMN / self-tasks use the motor_self_* column; same outcome.
    conns = _resolve(DEBATE_PERMS, self_mode=True)
    assert conns == {READONLY}
    assert FULL not in conns


def test_main_trading_agent_gets_full_connector():
    assert _resolve(MAIN_PERMS, self_mode=False) == {FULL}


def test_no_agent_bound_is_unrestricted():
    # Companion/local turn → no narrowing → all configured connectors (None).
    probe = _Probe(False)
    assert probe._mode_policy()["connectors"] is None


def _executor():
    ex = CMAExecutor.__new__(CMAExecutor)  # skip network-y __init__
    ex._mcp_servers = [
        {"name": FULL, "url": "https://app/api/mcp/trading", "identity": True},
        {"name": READONLY, "url": "https://app/api/mcp/trading-readonly", "identity": True},
    ]
    ex._connector_filter = None
    return ex


def test_executor_filter_scopes_debate_session_to_readonly():
    ex = _executor()
    ex.set_connector_filter({READONLY})
    active = {s["name"] for s in ex._active_mcp_servers()}
    assert active == {READONLY}
    # The write connector is not even declared to the agent for this session.
    declared = {d["name"] for d in ex._mcp_server_decls()}
    assert declared == {READONLY}


def test_executor_filter_scopes_main_session_to_full():
    ex = _executor()
    ex.set_connector_filter({FULL})
    assert {s["name"] for s in ex._active_mcp_servers()} == {FULL}


def test_executor_config_hash_differs_per_filter():
    # The filter participates in the config hash, so a warm session built with the
    # write connector is never reused for a read-only (debate) policy.
    ex = _executor()
    ex._model = "claude-test"
    ex.set_connector_filter({FULL})
    full_hash = ex._config_hash()
    ex.set_connector_filter({READONLY})
    assert ex._config_hash() != full_hash
