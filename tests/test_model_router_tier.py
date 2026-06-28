"""
Per-brain tier gate in ModelRouter — the single enforcement of local-permission.
A 'lite' brain holds no local pod, so _resolve_model_id remaps any local route to
cloud; a 'full' brain keeps local. The cloud-vs-local truth itself (cell config +
_provider_for) is unchanged — this only gates whether THIS brain may use local.
"""

from __future__ import annotations

import datetime

import pytest

from brain.model_router import CloudBudgetExceeded, ModelRouter, _provider_for


def _router(local_disabled: bool) -> ModelRouter:
    # Tests construct via __new__ to skip client/init; the gate only needs the flag.
    r = ModelRouter.__new__(ModelRouter)
    r._local_disabled = local_disabled
    return r


def test_lite_brain_remaps_local_to_cloud():
    r = _router(True)
    _mk, mid = r._resolve_model_id("local-code", cluster="dmn")
    assert _provider_for(mid) != "local"  # forced off the (nonexistent) pod


def test_lite_brain_remaps_runpod_to_cloud():
    r = _router(True)
    _mk, mid = r._resolve_model_id("runpod-general", cluster="dmn")
    assert _provider_for(mid) != "local"


def test_lite_brain_leaves_cloud_models_untouched():
    r = _router(True)
    _mk, mid = r._resolve_model_id("haiku", cluster="frontal")
    assert _provider_for(mid) == "anthropic"


def test_full_brain_keeps_local():
    r = _router(False)
    _mk, mid = r._resolve_model_id("local-code", cluster="dmn")
    assert _provider_for(mid) == "local"


def test_full_brain_runpod_stays_local():
    r = _router(False)
    _mk, mid = r._resolve_model_id("runpod-general", cluster="dmn")
    assert _provider_for(mid) == "local"


# ── Daily-USD ceiling: lite HARD-STOPS, full degrades to local ────────────────────
# Regression: a lite brain (no local pod) used to BYPASS the daily USD cap entirely
# (the `and not self._local_disabled` guard), so a runaway job could bill unbounded.
# It must now raise instead.


def _budget_router(local_disabled: bool, spent: float) -> ModelRouter:
    r = ModelRouter.__new__(ModelRouter)
    r._local_disabled = local_disabled
    r._cloud_usd_today = spent
    # Pin the date to today so _refresh_cloud_usd_today() doesn't reload from disk
    # and clobber the spend we set here.
    r._cloud_usd_date = datetime.date.today().isoformat()
    return r


def test_lite_over_cap_raises(monkeypatch):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "cloud_daily_usd_budget", 5.0)
    r = _budget_router(local_disabled=True, spent=6.0)
    with pytest.raises(CloudBudgetExceeded):
        r._enforce_cloud_budget("api", "extract")


def test_full_over_cap_degrades_to_local(monkeypatch):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "cloud_daily_usd_budget", 5.0)
    r = _budget_router(local_disabled=False, spent=6.0)
    # True = caller should redirect this call to local (no raise — it has a pod).
    assert r._enforce_cloud_budget("dmn", "rumination") is True


def test_under_cap_proceeds_on_cloud(monkeypatch):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "cloud_daily_usd_budget", 5.0)
    r = _budget_router(local_disabled=True, spent=1.0)
    assert r._enforce_cloud_budget("api", "extract") is False


def test_lite_keeps_hard_backstop_when_budget_disabled(monkeypatch):
    # Setting the budget to 0 disables the soft cap, but a lite brain still must not
    # run unbounded — the BRAIN_LITE_DAILY_USD_CAP backstop applies.
    from brain import model_router
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "cloud_daily_usd_budget", 0.0)
    monkeypatch.setattr(model_router, "_LITE_DEFAULT_DAILY_USD_CAP", 25.0)
    with pytest.raises(CloudBudgetExceeded):
        _budget_router(local_disabled=True, spent=30.0)._enforce_cloud_budget("api", "x")
    assert _budget_router(local_disabled=True, spent=10.0)._enforce_cloud_budget("api", "x") is False


def test_full_brain_unbounded_when_budget_disabled(monkeypatch):
    # A full brain with the cap disabled is intentionally unbounded on cloud — it can
    # always shed to its local pod, and the lite backstop does not apply to it.
    from brain import model_router
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "cloud_daily_usd_budget", 0.0)
    monkeypatch.setattr(model_router, "_LITE_DEFAULT_DAILY_USD_CAP", 25.0)
    r = _budget_router(local_disabled=False, spent=999.0)
    assert r._enforce_cloud_budget("dmn", "x") is False


def test_per_agent_cap_tightens_org_ceiling(monkeypatch):
    # A bound agent's tighter per-agent cap wins over a looser org ceiling.
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "cloud_daily_usd_budget", 100.0)
    monkeypatch.setattr(
        "brain.agent_ctx.current_agent",
        lambda: {"permissions": {"cloud_daily_usd_budget": 5.0}},
        raising=False,
    )
    # $6 spent: under the $100 org ceiling but over the agent's $5 cap → blocked.
    with pytest.raises(CloudBudgetExceeded):
        _budget_router(local_disabled=True, spent=6.0)._enforce_cloud_budget("api", "x")


# ── External cloud usage (CMA path): gate + record without going through call() ───
# CMA managed-agent inference bills the API key directly and never routes through
# call(), so it bypassed _enforce_cloud_budget (the cap) AND _meter_agent (the
# dashboard). cloud_budget_exhausted()/record_cloud_usage() are the seam that closes
# both gaps. Regression for ~$200/2d real spend showing ~$1 in the ledger.


def test_cloud_budget_exhausted_gates_external_consumers(monkeypatch):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "cloud_daily_usd_budget", 5.0)
    assert _budget_router(local_disabled=True, spent=6.0).cloud_budget_exhausted() is True
    assert _budget_router(local_disabled=True, spent=1.0).cloud_budget_exhausted() is False


def test_cloud_budget_exhausted_false_when_no_cap(monkeypatch):
    from brain import model_router
    from brain.settings import settings

    # No org budget and no lite backstop → a full brain is intentionally unbounded.
    monkeypatch.setitem(settings._data, "cloud_daily_usd_budget", 0.0)
    monkeypatch.setattr(model_router, "_LITE_DEFAULT_DAILY_USD_CAP", 0.0)
    assert _budget_router(local_disabled=False, spent=9999.0).cloud_budget_exhausted() is False


def test_record_cloud_usage_charges_tally_and_attributes_to_agent(monkeypatch):
    # External (CMA) usage must hit BOTH meters it used to bypass: the daily USD
    # tally (→ the cap binds) and the per-agent dashboard ledger (→ attribution).
    r = _budget_router(local_disabled=True, spent=0.0)
    r._persist_cloud_usd = lambda: None  # don't touch the tenant volume from a test
    r._agent_usage = {}
    monkeypatch.setattr(
        "brain.turn_ctx.current_turn",
        lambda: {"agent_id": "the_analyst.day_trading_analyst"},
    )
    # 1M input + 1M output of Haiku 4.5 = $1 + $5 = $6.00.
    usd = r.record_cloud_usage("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert round(usd, 2) == 6.00
    assert round(r._cloud_usd_today, 2) == 6.00  # day-tally charged
    usage = r.agent_usage()
    assert "owner" not in usage  # NOT bucketed to the hidden owner lane
    u = usage["the_analyst.day_trading_analyst"]
    assert u["cloud_calls"] == 1 and u["in_tok"] == 1_000_000 and u["out_tok"] == 1_000_000
    assert round(u["cloud_usd"], 2) == 6.00
