"""Per-key agent allowlist (migration 036), whoami, and turn cost telemetry.

Any partner key could open a session on ANY enabled agent and read every persona
spec in the org. `api_keys.allowed_agents` pins a key: a restricted key must name
an agent at session open and may only name one on its list (404 otherwise —
indistinguishable from an unknown agent, so the key learns nothing about agents
it was not given), and the agent/persona listings are filtered to its agents and
their personas. NULL = unrestricted = every pre-036 row = today's behaviour.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api import auth as _auth
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry

# ── the auth module ─────────────────────────────────────────────────────────


def test_clean_allowed_agents_validates_shape():
    assert _auth._clean_allowed_agents(None) is None
    assert _auth._clean_allowed_agents(["p.m", " p.m ", "q.n"]) == ["p.m", "q.n"]
    with pytest.raises(ValueError):
        _auth._clean_allowed_agents("p.m")  # not a list
    with pytest.raises(ValueError):
        _auth._clean_allowed_agents(["no_dot"])  # malformed id
    with pytest.raises(ValueError):
        _auth._clean_allowed_agents([f"p.m{i}" for i in range(_auth.MAX_ALLOWED_AGENTS + 1)])


def test_allowed_agents_reads_null_list_and_serialised_array():
    assert _auth._allowed_agents({}) is None  # pre-036 row
    assert _auth._allowed_agents({"allowed_agents": None}) is None
    assert _auth._allowed_agents({"allowed_agents": ["a.b", "c.d"]}) == ["a.b", "c.d"]
    assert _auth._allowed_agents({"allowed_agents": "{a.b,c.d}"}) == ["a.b", "c.d"]


class _Insert:
    def __init__(self):
        self.rows: list[dict] = []

    def table(self, name):
        assert name == "api_keys"
        return self

    def insert(self, row):
        self.rows.append(row)
        return self

    def execute(self):
        return type("R", (), {"data": []})()


@pytest.fixture
def sb(monkeypatch):
    from brain.second_brain import supabase_client

    rec = _Insert()
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: rec)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    return rec


def test_mint_omits_column_when_unrestricted_and_stores_list_otherwise(sb):
    out = _auth.mint_partner_key("acme")
    assert out["allowed_agents"] is None
    assert "allowed_agents" not in sb.rows[-1]  # pre-036 deployments keep minting
    out = _auth.mint_partner_key("acme", allowed_agents=["p.m"])
    assert out["allowed_agents"] == ["p.m"]
    assert sb.rows[-1]["allowed_agents"] == ["p.m"]
    assert sb.rows[-1]["org_id"] == "org-1"


def test_mint_refuses_list_on_owner_key_and_empty_list(sb):
    with pytest.raises(ValueError):
        _auth.mint_partner_key("acme", role="owner", allowed_agents=["p.m"])
    with pytest.raises(ValueError):
        _auth.mint_partner_key("acme", allowed_agents=[])


def test_resolve_partner_carries_allowlist_and_key_id(monkeypatch):
    monkeypatch.setattr(_auth, "configured_keys", lambda: set())
    monkeypatch.setattr(
        _auth,
        "_lookup_partner_key",
        lambda tok: {
            "id": "k1",
            "partner_id": "acme",
            "role": "partner",
            "allowed_agents": ["p.m"],
        },
    )
    ctx = _auth.resolve_partner("Bearer sk_x")
    assert ctx == {"partner_id": "acme", "owner": False, "allowed_agents": ["p.m"], "key_id": "k1"}
    monkeypatch.setattr(_auth, "configured_keys", lambda: {"sk_env"})
    assert _auth.resolve_partner("Bearer sk_env") == {
        "partner_id": None,
        "owner": True,
        "allowed_agents": None,
        "key_id": None,
    }


# ── the routes ──────────────────────────────────────────────────────────────


class _FakeRunner:
    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        return ("echo", {"emotion": "warm", "turn_id": "t1", "elapsed_s": 1.25, "llm_calls": 4})


def _resolver(authorization):
    tok = (
        authorization[7:].strip()
        if authorization and authorization.lower().startswith("bearer ")
        else None
    )
    return {
        "kr": {"partner_id": "A", "owner": False, "allowed_agents": ["p.one"], "key_id": "k-r"},
        "ka": {"partner_id": "A", "owner": False, "allowed_agents": None, "key_id": "k-a"},
        "ko": {"partner_id": None, "owner": True, "allowed_agents": None, "key_id": None},
    }.get(tok)


RESTRICTED = {"Authorization": "Bearer kr"}
OPEN = {"Authorization": "Bearer ka"}
OWNER = {"Authorization": "Bearer ko"}

_AGENTS = [
    {"persona": "p", "mandate_id": "one", "enabled": True, "agent_id": "p.one"},
    {"persona": "p", "mandate_id": "two", "enabled": True, "agent_id": "p.two"},
    {"persona": "q", "mandate_id": "one", "enabled": True, "agent_id": "q.one"},
]


@pytest.fixture
def client(monkeypatch):
    from brain import agents, personas
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    monkeypatch.setattr(agents, "list_agents", lambda: [dict(a) for a in _AGENTS])
    monkeypatch.setattr(
        agents,
        "get",
        lambda aid: next((dict(a) for a in _AGENTS if a["agent_id"] == aid), None),
    )
    monkeypatch.setattr(agents, "resolve", lambda aid: tuple(aid.split(".", 1)))
    monkeypatch.setattr(
        personas, "list_all", lambda: [{"slug": "p", "display_name": "P"}, {"slug": "q"}]
    )
    monkeypatch.setattr(
        personas, "get", lambda slug: {"slug": slug} if slug in ("p", "q") else None
    )
    monkeypatch.setattr(personas, "capacity_limits", lambda: {})
    app = FastAPI()
    app.include_router(
        build_api_router(
            _FakeRunner(),
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
        )
    )
    return TestClient(app)


def test_restricted_key_must_name_an_allowed_agent(client):
    r = client.post("/v1/sessions", headers=RESTRICTED, json={"end_user_id": "u"})
    assert r.status_code == 400 and "agent_id" in r.json()["detail"]
    r = client.post(
        "/v1/sessions", headers=RESTRICTED, json={"end_user_id": "u", "agent_id": "p.two"}
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "unknown or disabled agent 'p.two'"  # same as resolve()'s
    r = client.post(
        "/v1/sessions", headers=RESTRICTED, json={"end_user_id": "u", "agent_id": "p.one"}
    )
    assert r.status_code == 200 and r.json()["agent_id"] == "p.one"


def test_unrestricted_and_owner_keys_are_unchanged(client):
    for hdr in (OPEN, OWNER):
        assert (
            client.post("/v1/sessions", headers=hdr, json={"end_user_id": "u"}).status_code == 200
        )
        r = client.post("/v1/sessions", headers=hdr, json={"end_user_id": "u", "agent_id": "q.one"})
        assert r.status_code == 200


def test_listings_are_filtered_for_a_restricted_key(client):
    ids = [a["agent_id"] for a in client.get("/v1/agents", headers=RESTRICTED).json()["agents"]]
    assert ids == ["p.one"]
    assert client.get("/v1/agents/p.one", headers=RESTRICTED).status_code == 200
    assert client.get("/v1/agents/p.two", headers=RESTRICTED).status_code == 404
    slugs = [p["slug"] for p in client.get("/v1/personas", headers=RESTRICTED).json()["personas"]]
    assert slugs == ["p"]
    assert client.get("/v1/personas/p", headers=RESTRICTED).status_code == 200
    assert client.get("/v1/personas/q", headers=RESTRICTED).status_code == 404
    # Unrestricted: the whole roster.
    ids = [a["agent_id"] for a in client.get("/v1/agents", headers=OPEN).json()["agents"]]
    assert ids == ["p.one", "p.two", "q.one"]
    assert client.get("/v1/personas/q", headers=OPEN).status_code == 200


def test_agents_ceilings_include_partner_budget(client):
    ceilings = client.get("/v1/agents", headers=OPEN).json()["ceilings"]
    assert "partner_cloud_daily_usd_budget" in ceilings
    assert "answer_only" in ceilings


def test_engine_whoami(client):
    r = client.get("/v1/whoami", headers=RESTRICTED)
    assert r.status_code == 200
    assert r.json() == {
        "org_id": "org-1",
        "partner_id": "A",
        "role": "partner",
        "key_id": "k-r",
        "allowed_agents": ["p.one"],
    }
    assert client.get("/v1/whoami", headers=OWNER).json()["role"] == "owner"
    assert client.get("/v1/whoami").status_code == 401


def test_partner_keys_route_passes_allowed_agents(client, monkeypatch):
    seen = {}

    def _mint(pid, label=None, role="partner", allowed_agents=None):
        seen.update(pid=pid, role=role, allowed_agents=allowed_agents)
        return {
            "id": "k9",
            "partner_id": pid,
            "role": role,
            "allowed_agents": allowed_agents,
            "token": "sk_t",
        }

    monkeypatch.setattr(_auth, "mint_partner_key", _mint)
    r = client.post(
        "/v1/partner_keys", headers=OWNER, json={"partner_id": "acme", "allowed_agents": ["p.one"]}
    )
    assert r.status_code == 200 and seen["allowed_agents"] == ["p.one"]


# ── turn cost telemetry ─────────────────────────────────────────────────────


def test_post_turn_carries_elapsed_and_llm_calls(client):
    sid = client.post("/v1/sessions", headers=OPEN, json={"end_user_id": "u"}).json()["session_id"]
    r = client.post(f"/v1/sessions/{sid}/turns", headers=OPEN, json={"message": "hi"})
    body = r.json()
    assert body["elapsed_s"] == 1.25 and body["llm_calls"] == 4
    assert "elapsed_s" not in body["affect"] and "elapsed_s" not in body["mood"]


def test_sse_done_carries_elapsed_and_llm_calls():
    class _Src:
        def add_tap(self, q):
            pass

        def remove_tap(self, q):
            pass

    app = FastAPI()
    app.include_router(
        build_api_router(
            _FakeRunner(),
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
            event_source=_Src(),
        )
    )
    c = TestClient(app)
    sid = c.post("/v1/sessions", headers=OPEN, json={"end_user_id": "u"}).json()["session_id"]
    with c.stream(
        "POST", f"/v1/sessions/{sid}/turns/stream", headers=OPEN, json={"message": "hi"}
    ) as r:
        text = "".join(r.iter_text())
    done = next(
        json.loads(ln[6:])
        for ln in text.splitlines()
        if ln.startswith("data: ") and '"elapsed_s"' in ln
    )
    assert done["elapsed_s"] == 1.25 and done["llm_calls"] == 4
