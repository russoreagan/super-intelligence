"""
Per-partner cloud budgets (migration 031).

The daily USD ceiling used to be per-org, so one partner could exhaust it for its
siblings, and a full-tier brain over budget silently rerouted to local models rather
than erroring — a partner paying for cloud-tier answers just got worse ones with no
signal. These pin the two fixes: per-partner accounting, and a hard 402 for a partner
over budget regardless of tier (the owner lane keeps the silent reroute).
"""

from __future__ import annotations

import datetime

import pytest

from brain.model_router import CloudBudgetExceeded, ModelRouter
from brain.turn_ctx import bind_turn

TODAY = datetime.date.today().isoformat()


def _router(*, local_disabled=False, org_spent=0.0, partner_spent=None) -> ModelRouter:
    """A router with just the budget state the gate reads. Supabase is not enabled in
    tests, so the partner read-through/bump helpers no-op and enforcement reads the
    in-memory totals set here."""
    r = ModelRouter.__new__(ModelRouter)
    r._local_disabled = local_disabled
    r._cloud_usd_today = org_spent
    r._cloud_usd_date = TODAY
    r._bg_mode = False
    r._partner_cloud_usd = dict(partner_spent or {})
    r._partner_cloud_date = TODAY
    r._partner_loaded = set(r._partner_cloud_usd)  # pretend already read-through
    return r


@pytest.fixture(autouse=True)
def _budgets(monkeypatch):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "cloud_daily_usd_budget", 100.0)
    monkeypatch.setitem(settings._data, "partner_cloud_daily_usd_budget", 5.0)


# ── the core isolation property ─────────────────────────────────────────────


def test_partner_over_cap_raises_even_on_a_full_brain():
    """The headline fix: a paying partner is told it stopped, not quietly downgraded."""
    r = _router(local_disabled=False, partner_spent={"A": 6.0})
    with bind_turn("agent", partner_id="A"), pytest.raises(CloudBudgetExceeded):
        r._enforce_cloud_budget("api", "turn")


def test_partner_under_cap_proceeds():
    r = _router(partner_spent={"A": 1.0})
    with bind_turn("agent", partner_id="A"):
        assert r._enforce_cloud_budget("api", "turn") is False


def test_one_partner_over_cap_does_not_block_another():
    r = _router(partner_spent={"A": 6.0, "B": 1.0})
    with bind_turn("agent", partner_id="B"):
        assert r._enforce_cloud_budget("api", "turn") is False
    with bind_turn("agent", partner_id="A"), pytest.raises(CloudBudgetExceeded):
        r._enforce_cloud_budget("api", "turn")


def test_a_fresh_partner_starts_at_zero():
    r = _router(partner_spent={"A": 6.0})
    with bind_turn("agent", partner_id="C"):
        assert r._enforce_cloud_budget("api", "turn") is False


# ── the owner lane is unchanged ─────────────────────────────────────────────


def test_owner_lane_full_brain_still_reroutes(monkeypatch):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "cloud_daily_usd_budget", 5.0)
    r = _router(local_disabled=False, org_spent=6.0)
    # No partner bound → owner lane → the silent local reroute is preserved.
    assert r._enforce_cloud_budget("dmn", "rumination") is True


def test_owner_lane_lite_brain_still_raises(monkeypatch):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "cloud_daily_usd_budget", 5.0)
    r = _router(local_disabled=True, org_spent=6.0)
    with pytest.raises(CloudBudgetExceeded):
        r._enforce_cloud_budget("api", "extract")


def test_partner_spend_does_not_trip_the_owner_counter():
    """A partner near its own cap but the org well under its ceiling: owner-lane work
    in the same process is unaffected."""
    r = _router(local_disabled=False, org_spent=1.0, partner_spent={"A": 6.0})
    # owner lane
    assert r._enforce_cloud_budget("dmn", "x") is False


# ── cap resolution: tighter wins ────────────────────────────────────────────


def test_org_cap_binds_a_partner_below_the_partner_cap(monkeypatch):
    from brain.settings import settings

    # Org cap $3 is tighter than the $5 partner default → partner is bound at $3.
    monkeypatch.setitem(settings._data, "cloud_daily_usd_budget", 3.0)
    r = _router(partner_spent={"A": 4.0})
    with bind_turn("agent", partner_id="A"), pytest.raises(CloudBudgetExceeded):
        r._enforce_cloud_budget("api", "turn")


def test_agent_narrowing_folds_into_the_partner_cap(monkeypatch):
    from brain import agent_ctx

    # A bound agent with a $2 cap tightens the $5 partner default.
    monkeypatch.setattr(
        agent_ctx, "current_agent", lambda: {"permissions": {"cloud_daily_usd_budget": 2.0}}
    )
    r = _router(partner_spent={"A": 3.0})
    with bind_turn("agent", partner_id="A"), pytest.raises(CloudBudgetExceeded):
        r._enforce_cloud_budget("api", "turn")


def test_no_caps_at_all_means_unbounded(monkeypatch):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "cloud_daily_usd_budget", 0.0)
    monkeypatch.setitem(settings._data, "partner_cloud_daily_usd_budget", 0.0)
    r = _router(partner_spent={"A": 999.0})
    with bind_turn("agent", partner_id="A"):
        assert r._enforce_cloud_budget("api", "turn") is False


# ── charging: the atomic bump ───────────────────────────────────────────────


class _FakeRPC:
    """Accumulates per (partner, date) exactly like the Postgres RPC, so two bumps
    sum instead of clobbering — the lost-update fix."""

    def __init__(self):
        self.totals: dict[str, float] = {}

    def rpc(self, name, params):
        self._name, self._params = name, params
        return self

    def execute(self):
        p = self._params
        if self._name == "bump_partner_cloud_usd":
            key = p["p_partner_id"]
            self.totals[key] = self.totals.get(key, 0.0) + float(p["p_usd"])
            return type("R", (), {"data": self.totals[key]})()
        if self._name == "get_partner_cloud_usd":
            return type("R", (), {"data": self.totals.get(p["p_partner_id"], 0.0)})()
        return type("R", (), {"data": None})()


@pytest.fixture
def fake_sb(monkeypatch):
    from brain.second_brain import supabase_client

    rpc = _FakeRPC()
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: rpc)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    return rpc


def test_charge_bumps_the_partner_counter(fake_sb):
    r = _router()
    r._cloud_usd_process_total = 0.0
    r._cloud_usd_autonomous_today = 0.0
    with bind_turn("agent", partner_id="A"):
        r._charge_cloud_usd("claude-sonnet-5", 1_000_000, 1_000_000, 0)
    assert r._partner_cloud_usd["A"] > 0
    assert fake_sb.totals["A"] == pytest.approx(r._partner_cloud_usd["A"])


def test_two_bumps_sum_not_clobber(fake_sb):
    r = _router()
    with bind_turn("agent", partner_id="A"):
        r._bump_partner_cloud_usd("A", 2.0, False)
        r._bump_partner_cloud_usd("A", 3.0, False)
    assert fake_sb.totals["A"] == pytest.approx(5.0)
    assert r._partner_cloud_usd["A"] == pytest.approx(5.0)


def test_owner_lane_charge_never_touches_a_partner_counter(fake_sb):
    r = _router()
    r._cloud_usd_process_total = 0.0
    r._cloud_usd_autonomous_today = 0.0
    # No partner bound.
    r._charge_cloud_usd("claude-sonnet-5", 1_000_000, 1_000_000, 0)
    assert r._partner_cloud_usd == {}
    assert fake_sb.totals == {}


def test_cma_gate_respects_the_partner_cap(fake_sb):
    """cloud_budget_exhausted is the pre-dispatch gate for the managed-agent path,
    which bills the key directly. A partner over its cap must be stopped there too."""
    r = _router(partner_spent={"A": 6.0})
    r._partner_loaded = set()  # force a read-through path (fake returns 6 via totals)
    fake_sb.totals["A"] = 6.0
    with bind_turn("agent", partner_id="A"):
        assert r.cloud_budget_exhausted() is True
    with bind_turn("agent", partner_id="B"):
        assert r.cloud_budget_exhausted() is False
