"""Console content routes under the read policy (brain/read_policy.py).

Isolated org: a non-home persona's transcripts, jobs, approvals, living
self-model and user-model are never returned — projected or 403 — while its
dials and chemistry stay readable. Consolidated: org admins get content and a
governance line; a member gets the projection. Persona spec writes are admin-only.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from brain import agent_log, learning_mode, org_settings, read_policy  # noqa: E402
from brain.settings import settings  # noqa: E402
from brain.ui import auth as ui_auth  # noqa: E402

TURNS = [
    {
        "agent_id": "ahab.m",
        "end_user_id": "cust-1",
        "persona": "ahab",
        "prompt": "buy AAPL",
        "response": "bought 10",
        "turn_id": "t1",
        "session_id": "s1",
        "ts": "2026-09-13T00:00:00Z",
    },
    {
        "agent_id": "home_p.m",
        "end_user_id": "cust-2",
        "persona": "home_p",
        "prompt": "hello home",
        "response": "hi",
        "turn_id": "t2",
        "session_id": "s2",
        "ts": "2026-09-13T00:00:01Z",
    },
]
APPROVALS = [
    {
        "id": "a1",
        "tool": "send_email",
        "tool_input": {"to": "x@y"},
        "status": "pending",
        "end_user_id": "cust-1",
    },
    {
        "id": "a2",
        "tool": "write_file",
        "tool_input": {"path": "/tmp/x"},
        "status": "pending",
        "end_user_id": "",
    },
]
JOBS = [
    {
        "job_id": "j1",
        "agent_id": "ahab.m",
        "goal": "secret goal",
        "summary": "secret summary",
        "state": "completed",
        "steps_json": [{"tool": "x", "args": {"q": "secret"}}],
        "results_json": ["out"],
        "cloud_usd": 0.2,
        "end_user_id": "cust-1",
        "productive_steps": 1,
    },
    {
        "job_id": "j2",
        "agent_id": "home_p.m",
        "goal": "home goal",
        "summary": "home summary",
        "state": "completed",
        "steps_json": [],
        "results_json": [],
        "cloud_usd": 0.0,
        "end_user_id": "",
    },
]


@pytest.fixture
def console(tmp_path, monkeypatch):
    """TestClient over the UI app with the cookie gate skipped and the role
    decided per test via `as_role('admin'|'member')`."""
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.delenv("BRAIN_AUTH_DISABLED", raising=False)
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: False)
    monkeypatch.setattr(ui_auth, "is_public_path", lambda p: True)  # skip the cookie gate
    monkeypatch.setattr(ui_auth, "is_admin", lambda claims: False)
    monkeypatch.setattr(ui_auth, "owner_mismatch", lambda claims: False)
    monkeypatch.setattr(
        learning_mode, "audit_log_path", lambda: tmp_path / "governance_audit.jsonl"
    )
    monkeypatch.setattr(read_policy, "_recent_reads", {})
    monkeypatch.setitem(settings._data, "content_read_policy", 1)
    monkeypatch.setitem(settings._data, "content_read_audit", 1)
    monkeypatch.setattr(
        agent_log,
        "recent",
        lambda limit, agent_id=None, persona=None: [
            t
            for t in TURNS
            if (not agent_id or t["agent_id"] == agent_id)
            and (not persona or t["persona"] == persona)
        ],
    )
    import brain.second_brain.store as store_mod

    schema_dir = tmp_path / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store_mod, "SCHEMA_DIR", schema_dir)
    from brain.ui.server import UIServer

    server = UIServer(
        emitter_queue=asyncio.Queue(),
        approvals_fn=lambda: [dict(a) for a in APPROVALS],
        jobs_list_fn=lambda limit=20, state=None: [dict(j) for j in JOBS],
        job_get_fn=lambda jid: next((dict(j) for j in JOBS if j["job_id"] == jid), None),
    )
    client = TestClient(server._build_app())

    def as_role(role: str):
        monkeypatch.setattr(ui_auth, "is_org_admin", lambda claims: role == "admin")

    def audit_lines():
        p = tmp_path / "governance_audit.jsonl"
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []

    return client, as_role, audit_lines


def _mode(monkeypatch, mode):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: mode)


# ── /agents/turns ────────────────────────────────────────────────────────────


def test_turns_projected_for_member_in_consolidated(console, monkeypatch):
    client, as_role, _ = console
    _mode(monkeypatch, "consolidated")
    as_role("member")
    d = client.get("/agents/turns?limit=50").json()
    assert d["content"] is False
    assert all(
        "prompt" not in t and "response" not in t and "end_user_id" not in t for t in d["turns"]
    )
    assert d["turns"][0]["prompt_len"] == len("buy AAPL")


def test_turns_full_for_admin_in_consolidated_and_audited(console, monkeypatch):
    client, as_role, audit = console
    _mode(monkeypatch, "consolidated")
    as_role("admin")
    d = client.get("/agents/turns?agent_id=ahab.m").json()
    assert d["content"] is True and d["turns"][0]["prompt"] == "buy AAPL"
    rec = [r for r in audit() if r["event"] == "content_read"]
    assert rec and rec[-1]["kind"] == "turns" and rec[-1]["persona"] == "ahab"
    assert "buy AAPL" not in json.dumps(rec)


def test_turns_all_projected_in_isolated_even_for_admin(console, monkeypatch):
    client, as_role, audit = console
    _mode(monkeypatch, "isolated")
    as_role("admin")
    d = client.get("/agents/turns").json()
    assert d["content"] is False
    assert all("prompt" not in t for t in d["turns"])
    # Home's rows are engine-lane too (owner_lane scope) → still projected.
    d2 = client.get("/agents/turns?persona=home_p").json()
    assert d2["content"] is False and d2["reason"] == "owner_lane"
    assert not [r for r in audit() if r["event"] == "content_read"]


# ── documents ────────────────────────────────────────────────────────────────


def test_self_and_user_model_403_for_isolated_non_home(console, monkeypatch):
    client, as_role, _ = console
    _mode(monkeypatch, "isolated")
    as_role("admin")
    for route in ("/self-model?persona=ahab", "/user-model?persona=ahab"):
        r = client.get(route)
        assert r.status_code == 403, route
        assert r.json()["detail"]["detail"] == "isolated_persona"
    # Home stays readable by the admin, and is audited.
    assert client.get("/self-model?persona=home_p").status_code == 200
    assert client.get("/user-model?persona=home_p").status_code == 200


def test_documents_require_org_admin_in_consolidated(console, monkeypatch):
    client, as_role, _ = console
    _mode(monkeypatch, "consolidated")
    as_role("member")
    assert client.get("/self-model?persona=ahab").status_code == 403
    as_role("admin")
    assert client.get("/user-model?persona=ahab").status_code == 200


def test_dials_and_settings_are_never_gated(console, monkeypatch):
    """Configuration is not content: the settings catalogue stays readable in an
    isolated org (the Seed, temperament, chemistry baselines live there)."""
    client, as_role, _ = console
    _mode(monkeypatch, "isolated")
    as_role("member")
    r = client.get("/settings")
    assert r.status_code == 200 and "personas" in r.json()


# ── approvals + jobs ─────────────────────────────────────────────────────────


def test_approvals_isolated_owner_lane_only(console, monkeypatch):
    client, as_role, _ = console
    _mode(monkeypatch, "isolated")
    as_role("admin")
    d = client.get("/tasks/approvals").json()
    assert d["content"] is True and d["scope"] == "owner_lane"
    assert [a["id"] for a in d["approvals"]] == ["a2"]


def test_approvals_projected_for_member(console, monkeypatch):
    client, as_role, _ = console
    _mode(monkeypatch, "consolidated")
    as_role("member")
    d = client.get("/tasks/approvals").json()
    assert d["content"] is False and all("tool_input" not in a for a in d["approvals"])
    assert client.post("/tasks/approve", json={"id": "a1"}).status_code == 403


def test_jobs_projected_per_row_in_isolated(console, monkeypatch):
    client, as_role, _ = console
    _mode(monkeypatch, "isolated")
    as_role("admin")
    rows = client.get("/tasks/jobs").json()["jobs"]
    by = {r["job_id"]: r for r in rows}
    assert by["j1"]["content"] is False and "goal" not in by["j1"] and by["j1"]["steps"] == 1
    assert by["j2"]["goal"] == "home goal"  # home, owner lane → full
    det = client.get("/tasks/jobs/j1").json()
    assert det["content"] is False and det["reason"] == "isolated_persona" and "summary" not in det
    assert det["state"] == "completed" and det["cloud_usd"] == 0.2
    assert client.get("/tasks/jobs/j2").json()["summary"] == "home summary"


def test_jobs_full_for_admin_in_consolidated(console, monkeypatch):
    client, as_role, audit = console
    _mode(monkeypatch, "consolidated")
    as_role("admin")
    assert client.get("/tasks/jobs/j1").json()["goal"] == "secret goal"
    assert [r for r in audit() if r["event"] == "content_read" and r["kind"] == "jobs"]


# ── learning stories, auth/me, spec writes ───────────────────────────────────


def test_learning_stories_withheld_for_isolated_non_home(console, monkeypatch):
    client, as_role, _ = console
    _mode(monkeypatch, "isolated")
    as_role("admin")
    d = client.get("/learning/stories?persona=ahab").json()
    assert d["stories"] == [] and d["withheld"] == "isolated_persona"


def test_auth_me_reports_mode_and_content_reads(console, monkeypatch):
    client, as_role, _ = console
    _mode(monkeypatch, "isolated")
    as_role("admin")
    j = client.get("/auth/me").json()
    assert j["learning_mode"] == "isolated" and j["content_reads"] is False
    _mode(monkeypatch, "consolidated")
    assert client.get("/auth/me").json()["content_reads"] is True
    as_role("member")
    assert client.get("/auth/me").json()["content_reads"] is False


def test_persona_spec_delete_requires_org_admin(console, monkeypatch):
    client, as_role, _ = console
    _mode(monkeypatch, "consolidated")
    as_role("member")
    assert client.delete("/settings/personas/ahab").status_code == 403
