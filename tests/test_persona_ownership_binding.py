"""Persona ownership binding in an isolated org (guide §20 rule 9).

"One persona per purchase" is structural, not partner discipline: the first
end_user_id to open a session on a persona owns it (first-writer-wins, like
end_users), and any other end user gets the same 404 an unknown agent gets.
Exempt: the home persona, owner keys, consolidated orgs, an org whose mode was
never read, and the persona_ownership_binding kill switch.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import org_settings, persona_owners
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry
from brain.settings import settings


def _resolver(authorization):
    return {
        "Bearer kp": {"partner_id": "A", "owner": False, "key_id": "kp"},
        "Bearer ko": {"partner_id": None, "owner": True, "key_id": "ko"},
    }.get(authorization)


P = {"Authorization": "Bearer kp"}
OWN = {"Authorization": "Bearer ko"}


@pytest.fixture
def client(monkeypatch):
    import brain.agents as agents

    monkeypatch.setattr(agents, "resolve", lambda aid: tuple(aid.split(".", 1)))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    ids = iter(f"s{i}" for i in range(100))
    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: next(ids)),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
        )
    )
    return TestClient(app)


@pytest.fixture
def registry(monkeypatch):
    """An in-memory persona_owners registry with first-writer-wins semantics."""
    owners: dict[str, str] = {}

    def _claim(persona, end_user_id):
        owners.setdefault(persona, end_user_id)
        return owners[persona]

    monkeypatch.setattr(persona_owners, "claim", _claim)
    return owners


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    monkeypatch.setitem(settings._data, "persona_ownership_binding", 1)


def _open(client, agent, eu, hdr=P):
    return client.post("/v1/sessions", json={"agent_id": agent, "end_user_id": eu}, headers=hdr)


def test_first_end_user_owns_second_gets_404(client, registry, isolated):
    assert _open(client, "ahab_b1.companion", "buyer1").status_code == 200
    assert registry == {"ahab_b1": "buyer1"}
    # The owner keeps opening sessions.
    assert _open(client, "ahab_b1.companion", "buyer1").status_code == 200
    r = _open(client, "ahab_b1.companion", "buyer2")
    assert r.status_code == 404
    assert r.json()["detail"] == "unknown or disabled agent 'ahab_b1.companion'"
    # Another persona is a fresh claim.
    assert _open(client, "ahab_b2.companion", "buyer2").status_code == 200
    assert registry == {"ahab_b1": "buyer1", "ahab_b2": "buyer2"}


def test_home_persona_is_exempt(client, registry, isolated):
    assert _open(client, "home_p.support", "u1").status_code == 200
    assert _open(client, "home_p.support", "u2").status_code == 200
    assert registry == {}


def test_owner_key_is_exempt(client, registry, isolated):
    assert _open(client, "ahab_b1.companion", "buyer1").status_code == 200
    assert _open(client, "ahab_b1.companion", "inspector", hdr=OWN).status_code == 200


def test_consolidated_org_never_enforces(client, registry, monkeypatch):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    assert _open(client, "ahab.support", "u1").status_code == 200
    assert _open(client, "ahab.support", "u2").status_code == 200
    assert registry == {}


def test_unknown_mode_never_refuses(client, registry, monkeypatch):
    """A process that could not read the org row must not 404 real customers."""
    monkeypatch.setattr(org_settings, "learning_mode", lambda: org_settings.UNKNOWN)
    assert _open(client, "ahab.support", "u1").status_code == 200
    assert _open(client, "ahab.support", "u2").status_code == 200


def test_kill_switch(client, registry, isolated, monkeypatch):
    monkeypatch.setitem(settings._data, "persona_ownership_binding", 0)
    assert _open(client, "ahab_b1.companion", "buyer1").status_code == 200
    assert _open(client, "ahab_b1.companion", "buyer2").status_code == 200


def test_registry_unavailable_does_not_refuse(client, isolated, monkeypatch):
    """Migration 037 not applied: claim() returns None → binding not enforced."""
    monkeypatch.setattr(persona_owners, "claim", lambda persona, eu: None)
    assert _open(client, "ahab_b1.companion", "buyer1").status_code == 200
    assert _open(client, "ahab_b1.companion", "buyer2").status_code == 200


def test_sessions_without_an_agent_are_untouched(client, registry, isolated):
    r = client.post("/v1/sessions", json={"end_user_id": "u1"}, headers=P)
    assert r.status_code == 200 and registry == {}


# ── the registry module itself ────────────────────────────────────────────────


class _Sb:
    def __init__(self):
        self.rows: dict[str, str] = {}
        self._persona = ""
        self._row = None
        self._op = ""
        self.sessions: list[dict] = []
        self._table = ""

    def table(self, name):
        self._table = name
        return self

    def select(self, *a, **k):
        self._op = "select"
        return self

    def upsert(self, row, **k):
        self._op = "upsert"
        self._row = row
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, k, v):
        if k == "persona":
            self._persona = v
        return self

    def limit(self, n):
        return self

    def execute(self):
        if self._table == "api_sessions":
            return type("R", (), {"data": list(self.sessions)})()
        if self._op == "upsert":
            self.rows.setdefault(self._row["persona"], self._row["end_user_id"])
            return type("R", (), {"data": []})()
        if self._op == "delete":
            had = self.rows.pop(self._persona, None)
            return type("R", (), {"data": [{}] if had else []})()
        eu = self.rows.get(self._persona)
        data = [{"persona": self._persona, "end_user_id": eu}] if eu else []
        return type("R", (), {"data": data})()


@pytest.fixture
def sb(monkeypatch):
    from brain.second_brain import supabase_client

    fake = _Sb()
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: fake)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    return fake


def test_claim_is_first_writer_wins(sb):
    assert persona_owners.claim("Ahab B1", "buyer1") == "buyer1"
    assert persona_owners.claim("ahab_b1", "buyer2") == "buyer1"  # insert-if-absent, read back
    assert persona_owners.owner_of("ahab_b1") == "buyer1"
    assert persona_owners.forget("ahab_b1") == 1
    assert persona_owners.owner_of("ahab_b1") is None


def test_multi_owner_report_excludes_home(sb):
    sb.sessions = [
        {"agent_id": "ahab_b1.c", "end_user_id": "u1"},
        {"agent_id": "ahab_b1.c", "end_user_id": "u2"},
        {"agent_id": "solo.c", "end_user_id": "u3"},
        {"agent_id": "home_p.c", "end_user_id": "u4"},
        {"agent_id": "home_p.c", "end_user_id": "u5"},
    ]
    out = persona_owners.multi_owner_personas()
    assert out == [{"persona": "ahab_b1", "end_users": 2, "status": "multi-owner, cannot be bound"}]


def test_local_backend_is_a_no_op(monkeypatch):
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    assert persona_owners.claim("p", "u") is None
    assert persona_owners.owner_of("p") is None
    assert persona_owners.registry_available() is False
