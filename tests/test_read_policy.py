"""brain/read_policy — who may read a persona's learned CONTENT.

Isolated org: never for a non-home persona (each is one buyer's companion).
Consolidated: org admins only, audited. Unknown mode: deny (fail closed). The
kill switch allows but still audits. Projections are allowlists.
"""

from __future__ import annotations

import json

import pytest

from brain import learning_mode, org_settings, read_policy
from brain.settings import settings

ADMIN = {"source": "console", "user": "admin@x", "org_admin": True, "platform_admin": False}
MEMBER = {"source": "console", "user": "m@x", "org_admin": False, "platform_admin": False}
PLATFORM = {"source": "console", "user": "root@x", "org_admin": True, "platform_admin": True}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setitem(settings._data, "content_read_policy", 1)
    monkeypatch.setitem(settings._data, "content_read_audit", 1)
    monkeypatch.setitem(settings._data, "content_read_audit_window_s", 300)
    monkeypatch.setattr(read_policy, "_recent_reads", {})
    monkeypatch.setattr(
        learning_mode, "audit_log_path", lambda: tmp_path / "governance_audit.jsonl"
    )


def _mode(monkeypatch, mode):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: mode)


def test_unknown_mode_denies_everything(monkeypatch):
    _mode(monkeypatch, org_settings.UNKNOWN)
    d = read_policy.content_read_allowed(ADMIN, "home_p", "turns")
    assert d.allow is False and d.reason == "org_mode_unknown"


def test_consolidated_requires_org_admin(monkeypatch):
    _mode(monkeypatch, "consolidated")
    ok = read_policy.content_read_allowed(ADMIN, "ahab", "turns")
    assert ok.allow and ok.scope == "all" and ok.reason == "ok"
    no = read_policy.content_read_allowed(MEMBER, "ahab", "turns")
    assert no.allow is False and no.reason == "org_admin_required"


def test_isolated_denies_non_home_for_admin_and_platform_admin(monkeypatch):
    _mode(monkeypatch, "isolated")
    for actor in (ADMIN, PLATFORM):
        d = read_policy.content_read_allowed(actor, "ahab", "self_model")
        assert d.allow is False and d.reason == "isolated_persona"


def test_isolated_home_allows_owner_lane_scope(monkeypatch):
    _mode(monkeypatch, "isolated")
    d = read_policy.content_read_allowed(ADMIN, "home_p", "turns")
    assert d.allow and d.scope == "owner_lane"


def test_unscoped_persona_is_non_home_in_isolated(monkeypatch):
    _mode(monkeypatch, "isolated")
    assert read_policy.content_read_allowed(ADMIN, "", "jobs").reason == "isolated_persona"
    assert read_policy.content_read_allowed(ADMIN, "__probe__", "jobs").allow is False


def test_kill_switch_allows_admins_but_audits(monkeypatch, tmp_path):
    _mode(monkeypatch, "isolated")
    monkeypatch.setitem(settings._data, "content_read_policy", 0)
    d = read_policy.content_read_allowed(ADMIN, "ahab", "turns")
    assert d.allow and d.reason == "policy_off"
    assert read_policy.audit_read(ADMIN, "turns", "ahab", "/agents/turns", decision=d) is True
    rec = json.loads((tmp_path / "governance_audit.jsonl").read_text().splitlines()[-1])
    assert rec["event"] == "content_read" and rec["reason"] == "policy_off"
    assert rec["persona"] == "ahab" and "prompt" not in rec and "text" not in rec
    # The escape hatch also works when the org row was never read.
    _mode(monkeypatch, org_settings.UNKNOWN)
    assert read_policy.content_read_allowed(ADMIN, "ahab", "turns").reason == "policy_off"


def test_kill_switch_never_widens_a_member(monkeypatch):
    """Audit 2026-09-13: policy_off short-circuited BEFORE the org-admin gate, so
    any member could read every persona's content once the switch was off — and
    the switch itself was a plain preference any member could flip."""
    monkeypatch.setitem(settings._data, "content_read_policy", 0)
    for mode in ("consolidated", "isolated", org_settings.UNKNOWN):
        _mode(monkeypatch, mode)
        for persona in ("ahab", "home_p", ""):
            d = read_policy.content_read_allowed(MEMBER, persona, "turns")
            assert d.allow is False and d.reason == "org_admin_required", (mode, persona)


def test_audit_coalesces_polling(monkeypatch, tmp_path):
    _mode(monkeypatch, "consolidated")
    d = read_policy.content_read_allowed(ADMIN, "ahab", "turns")
    assert read_policy.audit_read(ADMIN, "turns", "ahab", "/agents/turns", decision=d)
    assert read_policy.audit_read(ADMIN, "turns", "ahab", "/agents/turns", decision=d) is False
    assert read_policy.audit_read(ADMIN, "jobs", "ahab", "/tasks/jobs", decision=d)
    lines = (tmp_path / "governance_audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert read_policy.count_content_reads("ahab") == 2
    assert read_policy.count_content_reads("other") == 0


def test_require_content_read_raises_403_with_reason(monkeypatch):
    from fastapi import HTTPException

    _mode(monkeypatch, "isolated")
    with pytest.raises(HTTPException) as ei:
        read_policy.require_content_read(ADMIN, "ahab", "user_model", "/user-model")
    assert ei.value.status_code == 403 and ei.value.detail["detail"] == "isolated_persona"
    d = read_policy.require_content_read(ADMIN, "home_p", "user_model", "/user-model")
    assert d.allow


def test_persona_resolution_from_agent_id_and_rows():
    assert read_policy.persona_of_event({"agent_id": "ahab.trading"}) == "ahab"
    assert read_policy.persona_of_event({"agent_id": "ahab.trading", "persona": "Luna"}) == "luna"
    assert read_policy.persona_of_job({"origin_agent_id": "x_1.m"}) == "x_1"
    assert (
        read_policy.persona_of_job({"origin_persona": "The Poet", "agent_id": "a.b"}) == "the_poet"
    )
    assert read_policy.owner_lane_row({"end_user_id": ""}) is True
    assert read_policy.owner_lane_row({"end_user_id": "u1"}) is False


_TEXT_KEYS = {
    "user_input",
    "response",
    "prompt",
    "thought",
    "goal",
    "summary",
    "reason_human",
    "tool_input",
    "text",
    "steps_json",
    "results_json",
    "end_user_id",
}


def _no_text(d: dict):
    assert not (_TEXT_KEYS & set(d)), sorted(_TEXT_KEYS & set(d))


def test_project_event_allowlist():
    start = {
        "type": "turn_start",
        "channel": "agent",
        "route_sid": "s",
        "agent_id": "ahab.m",
        "end_user_id": "cust-1",
        "user_input": "buy AAPL",
        "turn_id": "t1",
        "ts": 1,
    }
    p = read_policy.project_event(start)
    _no_text(p)
    assert p["input_len"] == 8 and p["persona"] == "ahab" and p["content"] is False
    assert (
        p["end_user_hash"] == read_policy.end_user_hash("cust-1") and len(p["end_user_hash"]) == 12
    )
    end = {**start, "type": "turn_end", "response": "bought", "elapsed_s": 0.2}
    assert read_policy.project_event(end)["response_len"] == 6
    th = {"type": "stream_thought", "thought": "secret musing", "persona": "ahab", "salience": 0.4}
    pt = read_policy.project_event(th)
    assert pt["type"] == "stream_thought_withheld" and "thought" not in pt and pt["salience"] == 0.4
    task = {
        "type": "task_complete",
        "agent_id": "ahab.m",
        "goal": "do x",
        "state": "done",
        "cloud_usd": 0.1,
    }
    ptask = read_policy.project_event(task)
    _no_text(ptask)
    assert ptask["state"] == "done" and ptask["cloud_usd"] == 0.1
    assert read_policy.project_event({"type": "proactive_speech", "text": "hi"}) is None
    assert read_policy.project_event({"type": "neuromod", "DA": 0.5}) == {
        "type": "neuromod",
        "DA": 0.5,
    }


def test_project_rows():
    turn = {
        "agent_id": "a.m",
        "end_user_id": "u",
        "prompt": "hello",
        "response": "hi there",
        "ts": "t",
    }
    pt = read_policy.project_turn(turn)
    _no_text(pt)
    assert (pt["prompt_len"], pt["response_len"]) == (5, 8)
    job = {
        "job_id": "j1",
        "agent_id": "a.m",
        "goal": "g",
        "summary": "s",
        "state": "completed",
        "steps_json": [1, 2],
        "results_json": ["r"],
        "cloud_usd": 0.3,
        "end_user_id": "u",
    }
    pj = read_policy.project_job(job)
    _no_text(pj)
    assert pj["steps"] == 2 and pj["state"] == "completed" and pj["persona"] == "a"
    ap = {
        "id": "x",
        "tool": "send_email",
        "tool_input": {"to": "a@b"},
        "status": "pending",
        "end_user_id": "u",
    }
    pa = read_policy.project_approval(ap)
    _no_text(pa)
    assert pa["tool"] == "send_email"


def test_actors(monkeypatch):
    from brain.ui import auth as ui_auth

    monkeypatch.setattr(ui_auth, "is_disabled", lambda: False)
    monkeypatch.setattr(ui_auth, "is_admin", lambda c: False)
    monkeypatch.setattr(ui_auth, "is_org_admin", lambda c: c.get("email") == "a@x")
    assert read_policy.actor_from_claims({"email": "a@x"})["org_admin"] is True
    assert read_policy.actor_from_claims({"email": "m@x"})["org_admin"] is False
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: True)
    assert read_policy.actor_from_claims(None)["org_admin"] is True  # local dev
    assert read_policy.actor_from_api_ctx({"owner": True})["org_admin"] is True
    assert read_policy.actor_from_api_ctx({"owner": False, "partner_id": "p"})["org_admin"] is False
