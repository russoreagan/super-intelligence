"""Phase D: listings, learned-state, roster and usage read the index / rollup
in O(page), with the spec scan and raw deltas as fallbacks."""

from __future__ import annotations

import time

import pytest

from brain import (
    agent_projects_store,
    agent_usage_store,
    agents,
    learning_mode,
    org_settings,
    persona_index,
    personas,
)
from brain.settings import settings


class _Q:
    """Minimal fake PostgREST query that records calls."""

    def __init__(self, log, data=None, count=None):
        self.log, self._data, self._count = log, data or [], count

    def __getattr__(self, name):
        def _m(*a, **k):
            self.log.append((name, a, k))
            return self

        return _m

    def execute(self):
        return type("R", (), {"data": self._data, "count": self._count})()


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "sb" / "personas" / "home_p"))
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")


# ── personas.list_for_ui pages, index first ──────────────────────────────────


def test_list_for_ui_pages_from_index_then_scan(home, monkeypatch):
    monkeypatch.setitem(settings._data, "persona_index_read", 1)
    specs = {
        f"c{i}": {"display_name": f"Clone {i}", "template": "t", "vals": {"x": i}} for i in range(5)
    }
    monkeypatch.setattr(personas, "_read_all_specs", lambda: dict(specs))
    monkeypatch.setattr(personas, "read_spec", lambda s: specs.get(s))
    calls = {"page": []}

    def fake_page(**kw):
        calls["page"].append(kw)
        return {
            "personas": [{"slug": "c3", "builtin": False}, {"slug": "c4", "builtin": False}],
            "total": 5,
        }

    monkeypatch.setattr(persona_index, "page", fake_page)
    entries, total = personas.list_for_ui(2, 3, None)
    customs = [e for e in entries if not e["builtin"]]
    assert [e["slug"] for e in customs] == ["c3", "c4"] and total == 5
    assert customs[0]["vals"] == {"x": 3} and customs[0]["template"] == "t"
    assert (
        calls["page"][0]["limit"] == 2
        and calls["page"][0]["offset"] == 3
        and calls["page"][0]["include_clones"]
    )
    # Index cannot answer → sliced spec scan with the same contract.
    monkeypatch.setattr(persona_index, "page", lambda **kw: None)
    entries, total = personas.list_for_ui(2, 1, "clone")
    assert [e["slug"] for e in entries if not e["builtin"]] == ["c1", "c2"] and total == 5
    # No limit → the whole catalogue, unchanged shape.
    assert isinstance(personas.list_for_ui(), list)


def test_learned_state_list_uses_index(home, monkeypatch):
    monkeypatch.setitem(settings._data, "persona_index_read", 1)
    monkeypatch.setattr(
        persona_index,
        "slugs",
        lambda **kw: ["a", "home_p", "b"] if kw.get("learned_state") else None,
    )
    assert learning_mode.personas_with_learned_state() == ["a", "b"]
    monkeypatch.setattr(persona_index, "slugs", lambda **kw: None)
    monkeypatch.setattr(personas, "list_all", lambda: [{"slug": "z"}])
    from brain import persona_audit

    monkeypatch.setattr(persona_audit, "has_learned_state", lambda s: s == "z")
    assert learning_mode.personas_with_learned_state() == ["z"]


# ── usage: daily rollup first, raw fallback ──────────────────────────────────


def _sb(monkeypatch, rpc):
    from brain.second_brain import supabase_client

    class _C:
        def rpc(self, name, params):
            return rpc(name, params)

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: _C())
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")


def test_aggregate_prefers_daily_and_falls_back(monkeypatch):
    monkeypatch.setitem(settings._data, "agent_usage_read_daily", 1)
    seen = []

    def rpc(name, params):
        seen.append((name, params))
        if name == "agent_usage_totals_daily":
            return _Q(seen, data=[{"agent_id": "a.m", "calls": 3, "cloud_usd": "1.5"}])
        raise AssertionError("raw must not be called when daily answers")

    _sb(monkeypatch, rpc)
    out = agent_usage_store.aggregate("2026-09-06T00:00:00+00:00", None)
    assert out["a.m"]["calls"] == 3 and out["a.m"]["cloud_usd"] == 1.5
    assert seen[0][1]["p_since"] == "2026-09-06"  # DATE param, not a datetime

    def rpc2(name, params):
        seen.append((name, params))
        if name == "agent_usage_totals_daily":
            raise RuntimeError("function does not exist")
        return _Q(seen, data=[{"agent_id": "b.m", "calls": 1}])

    _sb(monkeypatch, rpc2)
    assert "b.m" in agent_usage_store.aggregate(None, None)
    monkeypatch.setitem(settings._data, "agent_usage_read_daily", 0)
    seen.clear()

    def rpc3(name, params):
        seen.append(name)
        return _Q(seen, data=[])

    _sb(monkeypatch, rpc3)
    agent_usage_store.aggregate(None, None)
    assert seen == ["agent_usage_totals"]


def test_persona_totals_is_o_page(monkeypatch):
    monkeypatch.setitem(settings._data, "agent_usage_read_daily", 1)
    seen = []

    def rpc(name, params):
        seen.append((name, params))
        return _Q(seen, data=[{"persona": "c1", "cloud_usd": "0.25", "calls": 2}])

    _sb(monkeypatch, rpc)
    out = agent_usage_store.persona_totals(["c1", "c2", "c1"], "2026-09-06T01:02:03Z", None)
    assert out == {"c1": {**out["c1"]}} and out["c1"]["cloud_usd"] == 0.25
    assert seen[0][0] == "persona_usage_totals" and seen[0][1]["p_personas"] == ["c1", "c2"]
    assert seen[0][1]["p_since"] == "2026-09-06"


def test_agent_spend_today_reads_through_the_store(monkeypatch):
    monkeypatch.setattr(agent_projects_store, "_backend", lambda: "supabase")
    monkeypatch.setattr(agent_projects_store, "_spend_cache", None)
    monkeypatch.setattr(
        agent_usage_store,
        "aggregate",
        lambda since, until: {"a.m": {"cloud_usd": 2.0}, "owner": {"cloud_usd": 9}},
    )
    assert agent_projects_store.agent_spend_today() == {"a.m": 2.0, "owner": 9.0}


# ── projects IN chunking ─────────────────────────────────────────────────────


def test_list_for_personas_chunks_the_in_list(monkeypatch):
    monkeypatch.setitem(settings._data, "agent_projects_in_chunk", 100)
    monkeypatch.setattr(agent_projects_store, "_backend", lambda: "supabase")
    agent_projects_store.invalidate_cache()
    calls = []

    class _T:
        def __init__(self):
            self.chunk = None

        def select(self, *a):
            return self

        def eq(self, *a):
            return self

        def in_(self, col, vals):
            calls.append(list(vals))
            return self

        def execute(self):
            return type(
                "R",
                (),
                {"data": [{"id": f"p-{len(calls)}", "persona": calls[-1][0], "state": "ready"}]},
            )()

    class _C:
        def table(self, name):
            return _T()

    monkeypatch.setattr(agent_projects_store, "_sb", lambda: (_C(), "org-1"))
    rows = agent_projects_store.list_for_personas([f"p{i}" for i in range(250)])
    assert [len(c) for c in calls] == [100, 100, 50] and len(rows) == 3


# ── DMN roster: index-backed active set, server-side agent filter, cap ───────


def _dmn(home_name="home_p"):
    from brain.dmn import DefaultModeNetwork

    d = DefaultModeNetwork.__new__(DefaultModeNetwork)
    d._pstate, d._home, d._hydrated_personas = {}, home_name, set()
    d._roster_cache, d._roster_ts, d._rr_idx = [], 0.0, 0
    return d


def test_isolated_active_roster_uses_index_and_filtered_agents(home, monkeypatch):
    from brain import human_activity

    monkeypatch.delenv("BRAIN_PERSONA_PINNED", raising=False)
    monkeypatch.delenv("BRAIN_PLACEMENT_FILE", raising=False)
    monkeypatch.setitem(settings._data, "persona_index_read", 1)
    monkeypatch.setitem(settings._data, "dmn_isolated_roster", "active")
    monkeypatch.setitem(settings._data, "dmn_active_roster_days", 7)
    seen = {}
    monkeypatch.setattr(persona_index, "slugs", lambda **kw: seen.setdefault("slugs", kw) and ["b"])
    rows = [{"persona": p, "enabled": True, "tier": "full"} for p in ("b", "c")]
    monkeypatch.setattr(agents, "list_agents", lambda **kw: seen.setdefault("agents", kw) and rows)
    stamps = {"n": 0}
    monkeypatch.setattr(
        human_activity,
        "persona_active",
        lambda *a, **k: stamps.__setitem__("n", stamps["n"] + 1) or True,
    )
    assert _dmn()._roster() == ["home_p", "b"]
    assert seen["slugs"]["active_days"] == 7 and seen["agents"] == {"enabled": True, "tier": "full"}
    assert stamps["n"] == 0  # no stamp files read when the index answers
    # Index cannot answer → stamp files decide, as before.
    monkeypatch.setattr(persona_index, "slugs", lambda **kw: None)
    monkeypatch.setattr(human_activity, "persona_active", lambda p, d, now=None: p == "c")
    assert _dmn()._roster() == ["home_p", "c"]


def test_roster_cap_keeps_home_and_logs_once(monkeypatch, caplog):
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.delenv("BRAIN_PERSONA_PINNED", raising=False)
    monkeypatch.delenv("BRAIN_PLACEMENT_FILE", raising=False)
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    monkeypatch.setitem(settings._data, "dmn_roster_max", 3)
    rows = [{"persona": f"p{i:02d}", "enabled": True, "tier": "full"} for i in range(10)]
    monkeypatch.setattr(agents, "list_agents", lambda **kw: rows)
    d = _dmn()
    with caplog.at_level("WARNING", logger="brain.dmn"):
        r = d._roster()
        d._roster_ts = 0.0
        d._roster()
    assert r == ["home_p", "p00", "p01"]
    assert sum("roster capped" in m.getMessage() for m in caplog.records) == 1
    monkeypatch.setitem(settings._data, "dmn_roster_max", 0)
    assert len(_dmn()._roster()) == 11


# ── sleep: bounded passes never scan the volume ──────────────────────────────


def test_bounded_sleep_passes_skip_the_directory_scan(monkeypatch):
    from brain.observability import learning_reader
    from brain.sleep import SleepConsolidation

    monkeypatch.setitem(settings._data, "sleep_bounded_passes", 1)

    def _boom():
        raise AssertionError("list_personas must not run for a bounded pass")

    monkeypatch.setattr(learning_reader, "list_personas", _boom)
    monkeypatch.setattr(learning_reader, "_active_slug", lambda: "home_p")
    assert SleepConsolidation._pass_candidates({"Ahab B1", "luna", "home_p"}) == [
        "home_p",
        "ahab_b1",
        "luna",
    ]
    monkeypatch.setattr(learning_reader, "list_personas", lambda: ["", "x", "y"])
    assert SleepConsolidation._pass_candidates(None) == ["", "x", "y"]
    monkeypatch.setitem(settings._data, "sleep_bounded_passes", 0)
    assert SleepConsolidation._pass_candidates({"x"}) == ["", "x"]


# ── agents paging (store + v1 route) ─────────────────────────────────────────


def test_list_agents_pages_server_side(monkeypatch):
    log = []
    q = _Q(log, data=[{"persona": "p", "mandate_id": "m"}])

    class _C:
        def table(self, n):
            return q

    monkeypatch.setattr(agents, "_sb", lambda: (_C(), "org-1"))
    out = agents.list_agents(limit=10, offset=20, persona="p")
    assert out[0]["agent_id"] == "p.m"
    assert ("range", (20, 29), {}) in log and ("eq", ("persona", "p"), {}) in log
    log.clear()
    agents.list_agents()
    assert not [c for c in log if c[0] == "range"]


def test_v1_personas_serves_the_index_when_it_answers(home, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from brain.api.server import build_api_router
    from brain.api.sessions import ApiSessionRegistry

    monkeypatch.setitem(settings._data, "persona_index_read", 1)
    seen = {}

    def fake_page(**kw):
        seen.update(kw)
        return {
            "personas": [{"slug": "t_c1", "template": "t"}],
            "total": 1,
            "limit": 50,
            "offset": 0,
            "next_offset": None,
        }

    monkeypatch.setattr(persona_index, "page", fake_page)
    monkeypatch.setattr(
        personas, "list_all", lambda: (_ for _ in ()).throw(AssertionError("scan must not run"))
    )

    def resolver(h):
        return (
            {"partner_id": None, "owner": True, "allowed_agents": None}
            if h == "Bearer ko"
            else None
        )

    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: "s"),
            auth=lambda h: resolver(h) is not None,
            resolver=resolver,
        )
    )
    c = TestClient(app)
    r = c.get("/v1/personas?q=c1&template=t&limit=50", headers={"Authorization": "Bearer ko"})
    assert (
        r.status_code == 200 and r.json()["personas"][0]["slug"] == "t_c1" and "limits" in r.json()
    )
    assert seen["q"] == "c1" and seen["template"] == "t" and seen["allowed"] is None
    # Restricted key → allowed slugs derived from its agent allowlist.

    def resolver2(h):
        return {"partner_id": "pf", "owner": False, "allowed_agents": ["t_c1.m", "t_c9.m"]}

    app2 = FastAPI()
    app2.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: "s"),
            auth=lambda h: True,
            resolver=resolver2,
        )
    )
    TestClient(app2).get("/v1/personas", headers={"Authorization": "Bearer kp"})
    assert seen["allowed"] == ["t_c1", "t_c9"]
    _ = time  # keep the import used
