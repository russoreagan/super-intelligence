"""POST /v1/personas/{template}/clone and the clone-aware listing.

The isolated-org primitive: one clone per purchase. Seeds: `default` = spec +
fresh self.md + baseline wiring; `current` = the template's learned competence
(wiring, chunks, sequence weights, ignition tally) and its self.md History summary /
Stable preferences through the de-id gate — never speaker files, episodes, threads,
chemistry pairs, DMN state or ledgers. Idempotent on slug + template; 409 otherwise
and at max_personas; built-in templates 400; owner-only.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import personas
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry
from brain.persona_key import persona_state_root


def _resolver(authorization):
    return {
        "Bearer kp": {"partner_id": "A", "owner": False, "key_id": "kp"},
        "Bearer ko": {"partner_id": None, "owner": True, "key_id": "ko"},
    }.get(authorization)


P = {"Authorization": "Bearer kp"}
OWN = {"Authorization": "Bearer ko"}


@pytest.fixture
def fs(tmp_path, monkeypatch):
    from brain import persona_chem

    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path / "personas")
    monkeypatch.delenv("BRAIN_MAX_PERSONAS", raising=False)
    return tmp_path


async def _deid_ok(text, source):
    return "I have learned to slow down with people who grieve."


async def _deid_reject(text, source):
    return None


def _client(deid=_deid_ok):
    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
            deid_runner=deid,
        )
    )
    return TestClient(app)


def _template(c, slug="ahab"):
    r = c.put(
        f"/v1/personas/{slug}",
        json={
            "display_name": "Captain Ahab",
            "disposition": "Consumed, magnetic, unbending.",
            "speaking": "- Grand cadence",
            "baseline": {"DA": 0.45, "NE": 0.55},
            "tag": "template",
        },
        headers=OWN,
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_clone_default_seed(fs, monkeypatch):
    c = _client()
    _template(c)
    # Plant learned state on the template that a default clone must NOT inherit.
    root = persona_state_root("ahab")
    (root / "wiring.json").write_text("[]")
    (root / "chunks.json").write_text('{"chunks": {}}')
    self_md = root / "schema" / "self.md"
    self_md.write_text(
        self_md.read_text().replace("## History summary\n", "## History summary\n\nJacob cried.\n")
    )
    r = c.post(
        "/v1/personas/ahab/clone",
        json={"suffix": "b1", "copy_agents": False, "seed": "default"},
        headers=OWN,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["created"] is True and out["seed"] == "default"
    spec = out["persona"]
    assert spec["slug"] == "ahab_b1" and spec["template"] == "ahab"
    assert spec["display_name"] == "Captain Ahab"
    assert spec["disposition"] == "Consumed, magnetic, unbending."
    assert spec["baseline"]["NE"] == 0.55
    croot = persona_state_root("ahab_b1")
    assert not (croot / "wiring.json").exists() and not (croot / "chunks.json").exists()
    clone_self = (croot / "schema" / "self.md").read_text()
    assert "Jacob" not in clone_self and "Captain Ahab" in clone_self
    chem = json.loads((fs / "personas" / "ahab_b1" / "chemistry.json").read_text())
    assert chem["current"] == chem["resting"]  # resting baseline, never the live mood
    assert out["copied"] == {}


def test_clone_current_seed_carries_competence_not_memories(fs):
    c = _client()
    _template(c)
    root = persona_state_root("ahab")
    (root / "wiring.json").write_text('[{"src": "a", "tgt": "b", "w": 0.7, "pol": 1}]')
    (root / "chunks.json").write_text('{"chunks": {"x": 1}}')
    (root / "sequence_weights.json").write_text('{"w": 1}')
    (root / "ignition_tally.json").write_text('{"t": 1}')
    (root / "learning_stories.jsonl").write_text('{"claim": "x"}\n')
    (root / "learning_ledger.jsonl").write_text('{"e": 1}\n')
    (root / "dmn_novelty.json").write_text("{}")
    (root / "client_chem").mkdir()
    (root / "client_chem" / "abc.json").write_text("{}")
    schema = root / "schema"
    (schema / "user_jacob_deadbeef.md").write_text("# User: jacob\n- Jacob's dog is Rex\n")
    (schema / "open_questions.md").write_text("## Open threads\n- Jacob's dog\n")
    self_md = schema / "self.md"
    self_md.write_text(
        self_md.read_text().replace(
            "## History summary\n", "## History summary\n\nJacob cried about Rex.\n"
        )
        + "\n## Stable preferences\n- slow with grief\n"
    )
    r = c.post(
        "/v1/personas/ahab/clone",
        json={"slug": "ahab_b2", "copy_agents": False, "seed": "current"},
        headers=OWN,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    croot = persona_state_root("ahab_b2")
    assert set(out["copied"]["files"]) == {
        "wiring.json",
        "chunks.json",
        "sequence_weights.json",
        "ignition_tally.json",
    }
    assert json.loads((croot / "wiring.json").read_text())[0]["w"] == 0.7
    for never in (
        "learning_stories.jsonl",
        "learning_ledger.jsonl",
        "dmn_novelty.json",
        "client_chem",
    ):
        assert not (croot / never).exists(), never
    assert not (croot / "schema" / "user_jacob_deadbeef.md").exists()
    assert not (croot / "schema" / "open_questions.md").exists()
    clone_self = (croot / "schema" / "self.md").read_text()
    assert out["copied"]["self_md_sections"] == ["History summary", "Stable preferences"]
    assert "slow down with people who grieve" in clone_self
    assert "Jacob" not in clone_self and "Rex" not in clone_self
    assert out["errors"] == []


def test_current_seed_fails_closed_on_deid(fs):
    c = _client(deid=_deid_reject)
    _template(c)
    root = persona_state_root("ahab")
    self_md = root / "schema" / "self.md"
    self_md.write_text(
        self_md.read_text().replace("## History summary\n", "## History summary\n\nJacob cried.\n")
    )
    r = c.post(
        "/v1/personas/ahab/clone",
        json={"suffix": "b3", "copy_agents": False, "seed": "current"},
        headers=OWN,
    )
    out = r.json()
    assert out["copied"]["self_md_sections"] == []
    assert any("de-id rejected" in e for e in out["errors"])
    assert "Jacob" not in (persona_state_root("ahab_b3") / "schema" / "self.md").read_text()
    # No gate at all → not carried either.
    c2 = _client(deid=None)
    r = c2.post(
        "/v1/personas/ahab/clone",
        json={"suffix": "b4", "copy_agents": False, "seed": "current"},
        headers=OWN,
    )
    assert any("unavailable" in e for e in r.json()["errors"])


def test_clone_uses_org_instance_seed_by_default(fs, monkeypatch):
    from brain import org_settings

    monkeypatch.setattr(org_settings, "instance_seed", lambda: "current")
    c = _client()
    _template(c)
    r = c.post("/v1/personas/ahab/clone", json={"suffix": "b5", "copy_agents": False}, headers=OWN)
    assert r.json()["seed"] == "current"


def test_clone_idempotent_and_conflicts(fs):
    c = _client()
    _template(c)
    _template(c, "ishmael")
    body = {"suffix": "b1", "copy_agents": False}
    first = c.post("/v1/personas/ahab/clone", json=body, headers=OWN).json()
    again = c.post("/v1/personas/ahab/clone", json=body, headers=OWN)
    assert again.status_code == 200 and again.json()["created"] is False
    assert again.json()["persona"]["slug"] == first["persona"]["slug"]
    # Same slug, different template → 409.
    r = c.post(
        "/v1/personas/ishmael/clone", json={"slug": "ahab_b1", "copy_agents": False}, headers=OWN
    )
    assert r.status_code == 409
    # Slug already an ordinary persona → 409.
    r = c.post(
        "/v1/personas/ahab/clone", json={"slug": "ishmael", "copy_agents": False}, headers=OWN
    )
    assert r.status_code == 409


def test_clone_validation(fs):
    c = _client()
    _template(c)
    assert c.post("/v1/personas/ahab/clone", json={"suffix": "x"}, headers=P).status_code == 403
    assert (
        c.post("/v1/personas/the_sage/clone", json={"suffix": "x"}, headers=OWN).status_code == 400
    )
    assert c.post("/v1/personas/nobody/clone", json={"suffix": "x"}, headers=OWN).status_code == 404
    assert c.post("/v1/personas/ahab/clone", json={}, headers=OWN).status_code == 400
    r = c.post("/v1/personas/ahab/clone", json={"suffix": "x", "seed": "latest"}, headers=OWN)
    assert r.status_code == 400
    r = c.post("/v1/personas/ahab/clone", json={"suffix": "Bad Slug!"}, headers=OWN)
    assert r.status_code == 400
    from brain.api.reference import is_owner_route

    assert is_owner_route("POST", "/v1/personas/{persona}/clone")


def test_max_personas_cap(fs, monkeypatch):
    monkeypatch.setenv("BRAIN_MAX_PERSONAS", "2")
    c = _client()
    _template(c)  # 1 custom
    assert c.get("/v1/personas", headers=OWN).json()["limits"]["max_personas"] == 2
    assert (
        c.post(
            "/v1/personas/ahab/clone", json={"suffix": "a", "copy_agents": False}, headers=OWN
        ).status_code
        == 200
    )  # 2 customs
    r = c.post("/v1/personas/ahab/clone", json={"suffix": "b", "copy_agents": False}, headers=OWN)
    assert r.status_code == 409 and "max_personas" in r.json()["detail"]


def test_copy_agents_errors_land_in_errors(fs):
    c = _client()
    _template(c)
    # No Supabase in tests → agents copy raises MandateError → reported, clone still created.
    r = c.post("/v1/personas/ahab/clone", json={"suffix": "b6"}, headers=OWN)
    assert r.status_code == 200
    assert r.json()["created"] is True
    assert any(e.startswith("agents:") for e in r.json()["errors"])


def test_copy_agents_copies_rows_and_skill_pairs(monkeypatch):
    from brain import agents
    from brain.second_brain import supabase_client

    log: list = []

    class _Q:
        def __init__(self):
            self._t = ""

        def table(self, n):
            self._t = n
            log.append(("table", n))
            return self

        def select(self, *a, **k):
            return self

        def eq(self, k, v):
            log.append(("eq", self._t, k, v))
            return self

        def order(self, *a, **k):
            return self

        def upsert(self, rows, **k):
            log.append(("upsert", self._t, rows, k.get("on_conflict")))
            return self

        def execute(self):
            if self._t == "agents":
                return type(
                    "R",
                    (),
                    {
                        "data": [
                            {
                                "persona": "ahab",
                                "mandate_id": "companion",
                                "name": "Ahab",
                                "enabled": True,
                                "permissions": {"answer_only": 1},
                                "sort_order": 0,
                                "tier": "full",
                            }
                        ]
                    },
                )()
            if self._t == "agent_skills":
                return type("R", (), {"data": [{"mandate_id": "companion", "skill_id": "house"}]})()
            return type("R", (), {"data": []})()

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: _Q())
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    from brain import mandates

    monkeypatch.setattr(mandates, "refresh", lambda: {})
    out = agents.copy_agents("ahab", "ahab_b1")
    assert out == {"agents": 1, "agent_skills": 1}
    ups = [e for e in log if e[0] == "upsert"]
    agent_rows = next(e for e in ups if e[1] == "agents")
    assert agent_rows[2][0]["persona"] == "ahab_b1"
    assert agent_rows[2][0]["permissions"] == {"answer_only": 1}
    assert agent_rows[2][0]["tier"] == "full"
    assert agent_rows[3] == "org_id,persona,mandate_id"
    skill_rows = next(e for e in ups if e[1] == "agent_skills")
    assert skill_rows[2] == [
        {"org_id": "org-1", "persona": "ahab_b1", "mandate_id": "companion", "skill_id": "house"}
    ]
    assert ("eq", "agents", "persona", "ahab") in log


# ── listing ──────────────────────────────────────────────────────────────────


def test_listing_hides_clones_by_default_and_pages(fs):
    c = _client()
    _template(c)
    for i in range(3):
        c.post(
            "/v1/personas/ahab/clone", json={"suffix": f"b{i}", "copy_agents": False}, headers=OWN
        )
    base = c.get("/v1/personas", headers=P).json()
    slugs = [p["slug"] for p in base["personas"]]
    assert "ahab" in slugs and not any(s.startswith("ahab_b") for s in slugs)
    assert base["total"] == len(slugs) and base["next_offset"] is None
    allp = c.get("/v1/personas?include_clones=true", headers=P).json()
    assert {"ahab_b0", "ahab_b1", "ahab_b2"} <= {p["slug"] for p in allp["personas"]}
    clones = c.get("/v1/personas?template=ahab", headers=P).json()
    assert [p["slug"] for p in clones["personas"]] == ["ahab_b0", "ahab_b1", "ahab_b2"]
    assert clones["total"] == 3 and all(p["template"] == "ahab" for p in clones["personas"])
    page1 = c.get("/v1/personas?template=ahab&limit=2", headers=P).json()
    assert [p["slug"] for p in page1["personas"]] == ["ahab_b0", "ahab_b1"]
    assert page1["next_offset"] == 2 and page1["total"] == 3
    page2 = c.get("/v1/personas?template=ahab&limit=2&offset=2", headers=P).json()
    assert [p["slug"] for p in page2["personas"]] == ["ahab_b2"] and page2["next_offset"] is None
    capped = c.get("/v1/personas?include_clones=true&limit=5000", headers=P).json()
    assert capped["limit"] == 1000


def test_page_helper_validates():
    with pytest.raises(personas.PersonaError):
        personas.page([], limit="lots")
    out = personas.page([{"slug": "a"}, {"slug": "b", "template": "a"}], limit=0, offset=-5)
    assert out["limit"] == 1 and out["offset"] == 0 and out["total"] == 1


def test_clone_is_async_callable_directly(fs):
    c = _client()
    _template(c)
    out = asyncio.run(personas.clone("ahab", {"suffix": "d1", "copy_agents": False}))
    assert out["created"] and out["persona"]["slug"] == "ahab_d1"
