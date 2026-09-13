"""Allowlist PINS on a partner key (plan §3.7): template:<slug>[.<mandate>] and
prefix:<p>, so one key covers every clone of a template without re-minting per
purchase.

A pin widens a RESTRICTED key only along the family it names; a plain agent id
keeps its exact-match semantics; and the `api_key_template_pins` kill switch
makes pins inert — a key whose only entries are pins then opens nothing and lists
nothing (fail closed), never "unrestricted".
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api import auth as _auth
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry
from brain.settings import DEFAULTS, settings

# ── grammar ─────────────────────────────────────────────────────────────────


def test_parse_pin():
    assert _auth.parse_pin("p.m") is None
    assert _auth.parse_pin("template:t") == ("template", "t", None)
    assert _auth.parse_pin("template:t.sales") == ("template", "t", "sales")
    assert _auth.parse_pin("prefix:pf_") == ("prefix", "pf_", None)


def test_clean_allowed_agents_accepts_and_normalises_pins():
    out = _auth._clean_allowed_agents(
        ["template:t", " template:t.sales ", "template:u.*", "prefix:pf_", "p.m", "template:t"]
    )
    assert out == ["template:t", "template:t.sales", "template:u", "prefix:pf_", "p.m"]


@pytest.mark.parametrize(
    "bad",
    [
        "template:",  # no slug
        "template:Bad-Slug",  # not a slug
        "template:t.Bad Mandate",  # not a mandate id
        "prefix:",  # empty prefix
        "prefix:a.b",  # a prefix has no mandate half
        "prefix:PF",  # not slug chars
    ],
)
def test_clean_allowed_agents_rejects_malformed_pins(bad):
    with pytest.raises(ValueError):
        _auth._clean_allowed_agents([bad])


def test_each_pin_counts_as_one_entry():
    n = _auth.MAX_ALLOWED_AGENTS
    assert len(_auth._clean_allowed_agents([f"template:t{i}" for i in range(n)])) == n
    with pytest.raises(ValueError):
        _auth._clean_allowed_agents([f"template:t{i}" for i in range(n + 1)])
    with pytest.raises(ValueError):
        _auth._clean_allowed_agents([f"prefix:p{i}" for i in range(n)] + ["x.y"])


def test_kill_switch_declared():
    assert DEFAULTS["api_key_template_pins"] == 1


# ── the routes ──────────────────────────────────────────────────────────────


class _FakeRunner:
    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        return ("echo", {"emotion": "warm", "turn_id": "t1", "elapsed_s": 1.0, "llm_calls": 1})


_KEYS = {
    "kt": ["template:t"],
    "ktm": ["template:t.sales"],
    "kp": ["prefix:pf_"],
    "kmix": ["template:t", "zz.m"],
}


def _resolver(authorization):
    tok = (
        authorization[7:].strip()
        if authorization and authorization.lower().startswith("bearer ")
        else None
    )
    if tok in _KEYS:
        return {"partner_id": "A", "owner": False, "allowed_agents": _KEYS[tok], "key_id": tok}
    return None


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


_AGENTS = [
    {"persona": "t", "mandate_id": "sales", "enabled": True, "agent_id": "t.sales"},
    {"persona": "t_x", "mandate_id": "sales", "enabled": True, "agent_id": "t_x.sales"},
    {"persona": "t_x", "mandate_id": "support", "enabled": True, "agent_id": "t_x.support"},
    {"persona": "pf_1", "mandate_id": "m", "enabled": True, "agent_id": "pf_1.m"},
    {"persona": "zz", "mandate_id": "m", "enabled": True, "agent_id": "zz.m"},
]
_PERSONAS = [
    {"slug": "t", "display_name": "T"},
    {"slug": "t_x", "display_name": "T x", "template": "t", "seed": "default"},
    {"slug": "t_y", "display_name": "T y", "template": "t", "seed": "default"},
    {"slug": "pf_1", "display_name": "PF 1"},
    {"slug": "zz", "display_name": "ZZ"},
]
_SPECS = {"t_x": {"template": "t"}, "t_y": {"template": "t"}}


@pytest.fixture
def client(monkeypatch):
    from brain import agents, persona_index, personas
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
    monkeypatch.setattr(personas, "list_all", lambda: [dict(p) for p in _PERSONAS])
    monkeypatch.setattr(
        personas,
        "get",
        lambda slug: next(({"slug": slug} for p in _PERSONAS if p["slug"] == slug), None),
    )
    monkeypatch.setattr(personas, "read_spec", lambda slug: _SPECS.get(slug))
    monkeypatch.setattr(personas, "capacity_limits", lambda: {})
    # Index off in this fixture: the spec file answers `template`.
    monkeypatch.setattr(persona_index, "template_of", lambda slug: None)
    monkeypatch.setitem(settings._data, "api_key_template_pins", 1)
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


def _open(client, tok, agent_id):
    return client.post(
        "/v1/sessions", headers=_h(tok), json={"end_user_id": "u", "agent_id": agent_id}
    )


def test_template_pin_allows_any_mandate_on_a_clone(client):
    assert _open(client, "kt", "t_x.sales").status_code == 200
    assert _open(client, "kt", "t_x.support").status_code == 200
    # The template itself is not on its pin, and an unrelated persona is refused.
    assert _open(client, "kt", "t.sales").status_code == 404
    assert _open(client, "kt", "zz.m").status_code == 404


def test_template_pin_with_mandate_denies_other_mandates(client):
    assert _open(client, "ktm", "t_x.sales").status_code == 200
    r = _open(client, "ktm", "t_x.support")
    assert r.status_code == 404
    assert r.json()["detail"] == "unknown or disabled agent 't_x.support'"


def test_prefix_pin(client):
    assert _open(client, "kp", "pf_1.m").status_code == 200
    assert _open(client, "kp", "zz.m").status_code == 404
    assert _open(client, "kp", "t_x.sales").status_code == 404


def test_personas_listing_for_a_template_pinned_key_lists_the_clones(client):
    r = client.get("/v1/personas", headers=_h("kt"))
    assert r.status_code == 200
    assert [p["slug"] for p in r.json()["personas"]] == ["t_x", "t_y"]
    assert r.json()["total"] == 2
    # An explicit ?template= or ?include_clones= keeps its own meaning.
    r = client.get("/v1/personas?include_clones=true", headers=_h("kt"))
    assert [p["slug"] for p in r.json()["personas"]] == ["t_x", "t_y"]
    # A prefix-pinned key sees its templates (clones hidden by default, as usual).
    r = client.get("/v1/personas", headers=_h("kp"))
    assert [p["slug"] for p in r.json()["personas"]] == ["pf_1"]
    # Mixed pins + ids: the default listing hides clones (not a pure template key)...
    r = client.get("/v1/personas", headers=_h("kmix"))
    assert [p["slug"] for p in r.json()["personas"]] == ["zz"]
    # ...and ?template= shows the pinned template's clones.
    r = client.get("/v1/personas?template=t", headers=_h("kmix"))
    assert [p["slug"] for p in r.json()["personas"]] == ["t_x", "t_y"]


def test_persona_detail_and_agent_listing_follow_the_pins(client):
    assert client.get("/v1/personas/t_x", headers=_h("kt")).status_code == 200
    assert client.get("/v1/personas/t", headers=_h("kt")).status_code == 404
    ids = [a["agent_id"] for a in client.get("/v1/agents", headers=_h("ktm")).json()["agents"]]
    assert ids == ["t_x.sales"]
    ids = [a["agent_id"] for a in client.get("/v1/agents", headers=_h("kmix")).json()["agents"]]
    assert ids == ["t_x.sales", "t_x.support", "zz.m"]


def test_pins_resolve_the_template_through_the_index_when_it_answers(client, monkeypatch):
    from brain import persona_index, personas

    monkeypatch.setattr(persona_index, "template_of", lambda slug: "t" if slug == "zz" else "")

    def _no_spec(slug):
        raise AssertionError("spec file consulted although the index answered")

    monkeypatch.setattr(personas, "read_spec", _no_spec)
    assert _open(client, "kt", "zz.m").status_code == 200  # index says zz is a t clone
    assert _open(client, "kt", "t_x.sales").status_code == 404  # index says: not a clone


def test_pins_are_ignored_when_the_setting_is_off(client, monkeypatch):
    monkeypatch.setitem(settings._data, "api_key_template_pins", 0)
    assert _open(client, "kt", "t_x.sales").status_code == 404
    assert _open(client, "kp", "pf_1.m").status_code == 404
    assert client.get("/v1/personas", headers=_h("kt")).json()["personas"] == []
    assert client.get("/v1/agents", headers=_h("kt")).json()["agents"] == []
    # A plain id on the same key keeps working: pins are inert, not the key.
    assert _open(client, "kmix", "zz.m").status_code == 200
    assert _open(client, "kmix", "t_x.sales").status_code == 404
