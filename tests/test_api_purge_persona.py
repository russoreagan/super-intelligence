"""Persona hard purge — DELETE /v1/personas/{p}?purge=true (guide §20).

The isolated-org erasure for a purchase: every store keyed by the persona goes.
The coverage test asserts on the exact _PERSONA_PURGE_TABLES tuple so a new
persona-keyed table cannot appear without a purge decision (same guard shape as
tests/test_api_purge_end_user.py). Refuses built-ins and the home persona (400),
unknown (404); in-memory eviction runs first; ok is False on any failed step.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry
from brain.persona_key import persona_state_root
from brain.session_turn import _TurnMixin


class _Recorder:
    def __init__(self):
        self.deleted: list[str] = []
        self.likes: list[tuple[str, str, str]] = []
        self.eqs: list[tuple[str, str, object]] = []
        self.fail_table: str | None = None
        self._table = ""

    def table(self, name):
        self._table = name
        return self

    def delete(self):
        self.deleted.append(self._table)
        return self

    def select(self, *a, **k):
        return self

    def eq(self, k, v):
        self.eqs.append((self._table, k, v))
        return self

    def like(self, k, v):
        self.likes.append((self._table, k, v))
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        if self.fail_table and self._table == self.fail_table:
            raise RuntimeError("table gone")
        return type("R", (), {"data": [], "count": 0})()


class _Wiring:
    def __init__(self):
        self.forgotten: list[str] = []

    def forget_persona(self, slug):
        self.forgotten.append(slug)
        return True


class _Dmn:
    def __init__(self):
        self.forgotten: list[str] = []

    def forget_persona(self, slug):
        self.forgotten.append(slug)
        return True


class _Jobs:
    def __init__(self):
        self.purged: list[str] = []

    def purge_persona(self, slug):
        self.purged.append(slug)
        return 3


class _Trace:
    def __init__(self, persona):
        self.persona_name = persona


class _Brain(_TurnMixin):
    def __init__(self):
        self.persona_name = "home_p"
        self.motor = type("M", (), {"job_store": _Jobs()})()
        self.wiring = _Wiring()
        self.dmn = _Dmn()
        self._api_registry = ApiSessionRegistry(id_fn=iter(f"s{i}" for i in range(10)).__next__)
        self._api_registry.create("u1", "ahab_b1.companion")
        self._api_registry.create("u2", "ahab_b2.companion")
        self._session_traces = [
            {"user_input": "a", "persona": "ahab_b1", "end_user_id": "u1"},
            {"user_input": "b", "persona": "ahab_b2", "end_user_id": "u2"},
        ]
        self._session_traces_full = [_Trace("ahab_b1"), _Trace("ahab_b2"), _Trace("ahab_b1")]
        self._persona_chem = {"ahab_b1:u1": object(), "ahab_b1": object(), "ahab_b2:u2": object()}
        self._engine_um_cache = {("ahab_b1", "u1"): "x", ("ahab_b2", "u2"): "y"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    from brain import persona_chem
    from brain.second_brain import supabase_client

    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("BRAIN_TRACE_JOURNAL", raising=False)
    monkeypatch.setenv("BRAIN_EVAL_LOG", str(tmp_path / "eval" / "turns.jsonl"))
    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path / "personas")
    rec = _Recorder()
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: rec)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    from brain import agents, personas

    agents._answer_only_cache["ahab_b1.companion"] = (1e12, False)
    agents._answer_only_cache["ahab_b2.companion"] = (1e12, False)
    agents._owning_mandate_cache["ahab_b1"] = (1e12, "companion")
    personas.upsert("ahab_b1", {"display_name": "Ahab B1", "disposition": "x"})
    personas.upsert("ahab_b2", {"display_name": "Ahab B2", "disposition": "y"})
    root = persona_state_root("ahab_b1")
    (root / "wiring.json").write_text("[]")
    (root / "learning_stories.jsonl").write_text("{}\n")
    return rec


def _purge(b, slug="ahab_b1"):
    return asyncio.run(b.api_purge_persona(slug))


def test_covers_every_persona_keyed_table(env):
    """The regression guard: every table in _PERSONA_PURGE_TABLES is hit by
    (org_id, persona); api_sessions / agent_jobs by agent_id prefix."""
    b = _Brain()
    out = _purge(b)
    assert out["ok"] is True, out
    assert set(_TurnMixin._PERSONA_PURGE_TABLES) <= set(env.deleted)
    for t in _TurnMixin._PERSONA_PURGE_TABLES:
        assert (t, "org_id", "org-1") in env.eqs and (t, "persona", "ahab_b1") in env.eqs
    assert {"api_sessions", "agent_jobs"} <= set(env.deleted)
    assert ("api_sessions", "agent_id", "ahab_b1.%") in env.likes
    assert ("agent_jobs", "agent_id", "ahab_b1.%") in env.likes
    assert "persona_owners" in env.deleted
    assert out["deleted"]["kept"] == ["agent_usage", "speaker_profiles"]
    assert "agent_usage" not in env.deleted and "speaker_profiles" not in env.deleted


def test_in_memory_eviction_first(env):
    b = _Brain()
    out = _purge(b)
    d = out["deleted"]
    assert d["api_sessions_memory"] == 1
    assert [s.agent_id for s in b._api_registry._sessions.values()] == ["ahab_b2.companion"]
    assert d["session_traces"] == 3
    assert [t["persona"] for t in b._session_traces] == ["ahab_b2"]
    assert set(b._persona_chem) == {"ahab_b2:u2"} and d["persona_chem_cache"] == 2
    assert set(b._engine_um_cache) == {("ahab_b2", "u2")}
    assert b.wiring.forgotten == ["ahab_b1"] and b.dmn.forgotten == ["ahab_b1"]
    from brain import agents

    assert "ahab_b1.companion" not in agents._answer_only_cache
    assert "ahab_b2.companion" in agents._answer_only_cache
    assert "ahab_b1" not in agents._owning_mandate_cache
    assert b.motor.job_store.purged == ["ahab_b1"] and d["local_jobs"] == 3


def test_files_go_and_siblings_stay(env, tmp_path):
    b = _Brain()
    out = _purge(b)
    assert not persona_state_root("ahab_b1").exists()
    assert not (tmp_path / "personas" / "ahab_b1").exists()
    assert (tmp_path / "personas" / "ahab_b2" / "persona.json").exists()
    assert out["deleted"]["state_root"]
    assert out["deleted"]["spec"] is True
    from brain import personas

    assert personas.read_spec("ahab_b1") is None
    assert personas.read_spec("ahab_b2") is not None


def test_trace_journal_and_eval_log_are_rewritten(env, tmp_path):
    from brain.observability import trace_journal
    from brain.observability.timeline import TurnTrace

    for pid, text in (("ahab_b1", "secret"), ("ahab_b2", "other")):
        tr = TurnTrace(turn_id=f"t_{pid}", session_id="s", user_input=text, persona_name=pid)
        trace_journal.append(tr, {"user_input": text, "persona": pid})
    ev = tmp_path / "eval" / "turns.jsonl"
    ev.parent.mkdir(parents=True)
    ev.write_text(
        json.dumps({"type": "turn", "persona_name": "ahab_b1"})
        + "\n"
        + json.dumps({"type": "turn", "persona_name": "ahab_b2"})
        + "\n"
    )
    out = _purge(_Brain())
    assert out["deleted"]["trace_journal"] == 1
    _, sums = trace_journal.load_orphans()
    assert [s["persona"] for s in sums] == ["ahab_b2"]
    assert out["deleted"]["eval_log"] == 1
    assert [json.loads(ln)["persona_name"] for ln in ev.read_text().splitlines()] == ["ahab_b2"]


def test_refusals(env):
    b = _Brain()
    assert _purge(b, "the_sage")["refused"] == 400
    assert _purge(b, "home_p")["refused"] == 400
    assert _purge(b, "nobody")["refused"] == 404
    assert _purge(b, "  ")["refused"] == 400
    assert env.deleted == []  # nothing touched on a refusal


def test_a_failing_table_makes_ok_false(env):
    env.fail_table = "wiring_edges"
    out = _purge(_Brain())
    assert out["ok"] is False and "wiring_edges" in out["failed"]
    # Everything else still ran.
    assert "episodes" in env.deleted and out["deleted"]["spec"] is True


def test_local_backend_still_removes_files(env, monkeypatch):
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    out = _purge(_Brain())
    assert out["ok"] is True
    assert not persona_state_root("ahab_b1").exists()
    assert "episodes" not in out["deleted"]


# ── route ────────────────────────────────────────────────────────────────────


def _resolver(authorization):
    return {
        "Bearer kp": {"partner_id": "A", "owner": False, "key_id": "kp"},
        "Bearer ko": {"partner_id": None, "owner": True, "key_id": "ko"},
    }.get(authorization)


def _client(runner):
    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
            persona_purge_runner=runner,
        )
    )
    return TestClient(app)


def test_route_maps_refusals_and_requires_owner(env):
    seen: list[str] = []

    async def _runner(slug):
        seen.append(slug)
        if slug == "the_sage":
            return {"ok": False, "refused": 400, "error": "built-in"}
        if slug == "nobody":
            return {"ok": False, "refused": 404, "error": "unknown"}
        return {"ok": True, "persona": slug, "deleted": {}}

    c = _client(_runner)
    hdr = {"Authorization": "Bearer ko"}
    assert (
        c.delete(
            "/v1/personas/ahab_b1?purge=true", headers={"Authorization": "Bearer kp"}
        ).status_code
        == 403
    )
    assert c.delete("/v1/personas/the_sage?purge=true", headers=hdr).status_code == 400
    assert c.delete("/v1/personas/nobody?purge=true", headers=hdr).status_code == 404
    r = c.delete("/v1/personas/ahab_b1?purge=true", headers=hdr)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert seen == ["the_sage", "nobody", "ahab_b1"]
    # Without the flag the soft delete still runs (spec removed, learned state kept).
    r = c.delete("/v1/personas/ahab_b2", headers=hdr)
    assert r.status_code == 200 and r.json() == {"ok": True, "persona": "ahab_b2"}


def test_route_501_without_runner(env):
    c = _client(None)
    r = c.delete("/v1/personas/ahab_b1?purge=true", headers={"Authorization": "Bearer ko"})
    assert r.status_code == 501
