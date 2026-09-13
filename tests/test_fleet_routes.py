"""Console /fleet/* routes: org-admin only, content-free by construction."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from brain import learning_mode, org_settings  # noqa: E402
from brain.settings import settings  # noqa: E402
from brain.ui import auth as ui_auth  # noqa: E402

CONTENT_KEYS = {
    "prompt",
    "response",
    "goal",
    "summary",
    "thought",
    "tool_input",
    "end_user_id",
    "steps_json",
    "results_json",
    "reason_human",
    "user_input",
    "text",
}

JOBS = [
    {
        "job_id": "j1",
        "agent_id": "ahab.m",
        "goal": "secret",
        "summary": "s",
        "state": "running",
        "updated_at": "2020-01-01T00:00:00Z",
        "steps_json": [1],
        "results_json": ["x"],
        "cloud_usd": 0.5,
        "end_user_id": "cust-1",
    },
    {
        "job_id": "j2",
        "agent_id": "home_p.m",
        "goal": "g2",
        "state": "completed",
        "updated_at": "2026-09-13T00:00:00Z",
        "cloud_usd": 0.0,
        "end_user_id": "",
    },
]


def _walk(o, found: set):
    if isinstance(o, dict):
        for k, v in o.items():
            if k in CONTENT_KEYS:
                found.add(k)
            _walk(v, found)
    elif isinstance(o, list):
        for v in o:
            _walk(v, found)


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.delenv("BRAIN_AUTH_DISABLED", raising=False)
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: False)
    monkeypatch.setattr(ui_auth, "is_public_path", lambda p: True)
    monkeypatch.setattr(ui_auth, "is_admin", lambda claims: False)
    monkeypatch.setattr(ui_auth, "owner_mismatch", lambda claims: False)
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    monkeypatch.setattr(org_settings, "instance_seed", lambda: "default")
    monkeypatch.setattr(
        learning_mode, "audit_log_path", lambda: tmp_path / "governance_audit.jsonl"
    )
    monkeypatch.setitem(settings._data, "fleet_roster_cadence_warn_s", 600)
    from brain.ui.server import UIServer

    signals = {
        "dmn": {
            "enabled": True,
            "dormant": False,
            "idle_s": 12.0,
            "pause_after_idle_s": 259200.0,
            "roster": {"mode": "active", "size": 3, "days": 7, "cadence_s": 45.0},
        },
        "tasks": {"pending": 1, "deferred": 0, "running": 0},
        "breaker": {"anthropic": {"kind": "auth"}},
        "pod_budget": {"exhausted": False, "usd_today": 1.0, "usd_budget": 10.0},
    }
    server = UIServer(
        emitter_queue=asyncio.Queue(),
        jobs_list_fn=lambda limit=20, state=None: [dict(j) for j in JOBS],
        usage_fn=lambda since, until, scope: {
            "scope": "org",
            "usage": {"ahab.m": {"cloud_usd": 0.25}},
        },
        fleet_fn=lambda: signals,
    )
    client = TestClient(server._build_app())

    def as_role(role):
        monkeypatch.setattr(ui_auth, "is_org_admin", lambda claims: role == "admin")

    return client, as_role


@pytest.mark.parametrize(
    "route",
    ["/fleet/health", "/fleet/summary", "/fleet/partners", "/fleet/jobs", "/fleet/governance"],
)
def test_fleet_routes_are_org_admin_only(console, route):
    client, as_role = console
    as_role("member")
    assert client.get(route).status_code == 403


def test_fleet_health_and_summary_are_content_free(console):
    client, as_role = console
    as_role("admin")
    h = client.get("/fleet/health").json()
    found: set = set()
    _walk(h, found)
    assert not found, found
    assert h["learning_mode"] == "isolated" and h["dmn"]["roster"]["size"] == 3
    codes = {a["code"] for a in h["alerts"]}
    assert "breaker_open" in codes and "stuck_jobs" in codes and h["health"] == "crit"
    assert h["stuck_jobs"][0]["job_id"] == "j1"
    s = client.get("/fleet/summary").json()
    found = set()
    _walk(s, found)
    assert not found
    assert s["jobs"] == {"open": 1, "stuck": 1} and s["cost"]["today_usd"] == 0.25
    assert s["roster"]["mode"] == "active"


def test_fleet_jobs_projected_and_filtered(console):
    client, as_role = console
    as_role("admin")
    rows = client.get("/fleet/jobs").json()["jobs"]
    found: set = set()
    _walk(rows, found)
    assert not found
    assert {r["job_id"] for r in rows} == {"j1", "j2"} and rows[0]["persona"] in ("ahab", "home_p")
    assert [r["job_id"] for r in client.get("/fleet/jobs?state=stuck").json()["jobs"]] == ["j1"]
    assert [r["job_id"] for r in client.get("/fleet/jobs?persona=home_p").json()["jobs"]] == ["j2"]
    assert [r["job_id"] for r in client.get("/fleet/jobs?state=open").json()["jobs"]] == ["j1"]


def test_fleet_governance_tails_the_audit_log(console, tmp_path):
    client, as_role = console
    as_role("admin")
    learning_mode.audit(
        "learning_mode_switch",
        {"source": "console", "user": "a@x"},
        **{"from": "consolidated", "to": "isolated"},
    )
    learning_mode.audit(
        "content_read", {"source": "console", "user": "a@x"}, kind="turns", persona="home_p"
    )
    evs = client.get("/fleet/governance?limit=10").json()["events"]
    assert [e["event"] for e in evs] == ["content_read", "learning_mode_switch"]  # newest first
    only = client.get("/fleet/governance?event=learning_mode_switch").json()["events"]
    assert len(only) == 1 and only[0]["to"] == "isolated"
    before = client.get(f"/fleet/governance?before={evs[0]['ts']}").json()["events"]
    assert [e["event"] for e in before] == ["learning_mode_switch"]


def test_health_route_carries_dmn_fields(console):
    client, _ = console
    h = client.get("/health").json()
    assert h["dmn"] == {"dormant": False, "idle_s": 12.0, "roster_size": 3}


def test_agents_route_pages_and_searches(console, monkeypatch):
    from brain import agents, mandates
    from brain.second_brain import supabase_client

    client, as_role = console
    as_role("admin")
    rows = [
        {
            "persona": f"p{i}",
            "mandate_id": "m",
            "agent_id": f"p{i}.m",
            "name": f"Agent {i}",
            "enabled": True,
        }
        for i in range(7)
    ]
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(
        agents,
        "list_agents",
        lambda **kw: [r for r in rows if not kw.get("persona") or r["persona"] == kw["persona"]],
    )
    monkeypatch.setattr(mandates, "list_mandates", lambda include_inactive=False: [])
    d = client.get("/agents?limit=3&offset=3").json()
    assert d["agents_total"] == 7 and [a["agent_id"] for a in d["agents"]] == [
        "p3.m",
        "p4.m",
        "p5.m",
    ]
    assert [a["agent_id"] for a in client.get("/agents?q=agent 6").json()["agents"]] == ["p6.m"]
    assert [a["agent_id"] for a in client.get("/agents?persona=p2").json()["agents"]] == ["p2.m"]


def test_fleet_signals_shape(monkeypatch):
    """session_loops.fleet_signals never raises and returns only states/counts."""
    from types import SimpleNamespace

    from brain.session_loops import _LoopsMixin

    sess = _LoopsMixin.__new__(_LoopsMixin)
    sess.dmn = None
    sess._task_queue = SimpleNamespace(pending_count=lambda: 2, deferred_count=lambda: 1)
    sess._running_task_id = "t"
    sess.router = SimpleNamespace(provider_outages=lambda: {"anthropic": {"kind": "credits"}})
    out = sess.fleet_signals()
    assert out["tasks"] == {"pending": 2, "deferred": 1, "running": 1}
    assert out["breaker"] == {"anthropic": {"kind": "credits"}}
    assert json.dumps(out)  # serialisable
