"""A skill one persona writes for itself reaches no other persona, by any path.

Isolated orgs (guide §20): a self-written skill is stamped with its persona
(skills.persona, migration 049) and the selector's index, the partner-body
registry and the org's own skill views all apply one rule (brain/skill_scope.py):
persona-less = org-level, visible to all; scoped = visible only while its persona
is bound. Consolidated orgs (no scoped rows) behave exactly as before.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brain import org_settings
from brain.clusters.skill_selector import SkillSelector, _Index
from brain.second_brain.store import bind_persona
from brain.skill_loader import SkillLoader

A, B, HOME = "buyer_a1", "buyer_b2", "home_p"
SA, SB, ORG = "self-aaaa1111-a-way", "self-bbbb2222-b-way", "org-wide-guide"


def _live(*rows):
    def _row(sid, persona, kw):
        return {
            "id": sid,
            "description": f"description of {sid}",
            "body": f"BODY OF {sid}",
            "keywords": [kw],
            "allowed_tools": [],
            "tier": 2,
            "all_agents": True,
            "agents": [],
            "persona": persona,
        }

    return [_row(*r) for r in rows]


@pytest.fixture
def selector(monkeypatch):
    monkeypatch.setenv("BRAIN_PERSONA_NAME", HOME)
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    from brain import skills_registry

    monkeypatch.setattr(
        skills_registry,
        "live_skills",
        lambda: _live((SA, A, "alphaword"), (SB, B, "betaword"), (ORG, None, "orgword")),
    )
    sel = SkillSelector.__new__(SkillSelector)
    idx = _Index.__new__(_Index)
    idx.skills, idx._by_name, idx.tier1_names = [], {}, []
    sel._index = idx
    sel._native_body_cache = {}
    sel._router = SimpleNamespace(embed=AsyncMock(return_value=None))
    asyncio.run(sel.warm_partner_skills())
    yield sel
    SkillLoader.clear_partner()


def _names(sel) -> set[str]:
    return {s["name"] for s in sel._index.skills}


@pytest.mark.parametrize(("me", "mine", "theirs"), [(A, SA, SB), (B, SB, SA)])
def test_every_selector_path_sees_only_its_own_skill(selector, me, mine, theirs):
    with bind_persona(me):
        assert mine in _names(selector) and theirs not in _names(selector)
        assert ORG in _names(selector)  # org-level stays visible to everyone
        assert selector._index.get(theirs) is None
        assert theirs not in selector._index._by_name
        assert selector.native_skill_body(theirs) == ""
        assert selector.native_skill_body(mine) == f"BODY OF {mine}"
        assert selector.allowed_for_current_agent(theirs) is False
        frags = selector.attachable_fragment_ids()
        assert mine in frags and theirs not in frags
        manifest = selector.capability_manifest(include_frameworks=False)
        assert mine in manifest and theirs not in manifest
        other_kw = "betaword" if me == A else "alphaword"
        assert selector._index.keyword_match(f"use the {other_kw} please") is None


def test_home_persona_sees_neither_purchase_personas_skill(selector):
    with bind_persona(HOME):
        assert SA not in _names(selector) and SB not in _names(selector)
        assert ORG in _names(selector)


@pytest.mark.parametrize(("me", "mine", "theirs"), [(A, SA, SB), (B, SB, SA)])
def test_partner_body_registry_hides_other_personas_bodies(selector, me, mine, theirs):
    """model_router injects partner bodies through SkillLoader on local AND cloud routes."""
    with bind_persona(me):
        assert SkillLoader.is_partner(mine) and not SkillLoader.is_partner(theirs)
        assert SkillLoader.load(theirs) == ""
        assert SkillLoader.load(mine) == f"BODY OF {mine}"
        block = SkillLoader.load_partner_block([mine, theirs])
        assert f"BODY OF {mine}" in block and f"BODY OF {theirs}" not in block


def test_consolidated_org_without_scoped_rows_is_unchanged(monkeypatch):
    from brain import skills_registry

    monkeypatch.setattr(skills_registry, "live_skills", lambda: _live((ORG, None, "orgword")))
    sel = SkillSelector.__new__(SkillSelector)
    idx = _Index.__new__(_Index)
    idx.skills, idx._by_name, idx.tier1_names = [], {}, []
    sel._index = idx
    sel._native_body_cache = {}
    sel._router = SimpleNamespace(embed=AsyncMock(return_value=None))
    asyncio.run(sel.warm_partner_skills())
    assert idx._scoped == 0 and idx.skills is idx._all  # no filtering cost at all
    assert ORG in {s["name"] for s in idx.skills}
    SkillLoader.clear_partner()


# ── registry: the org's own views and writes ─────────────────────────────────


class _Res:
    def __init__(self, data):
        self.data = data


class _FakeSkillsDb:
    """PostgREST double for the skills table, with an optional missing column."""

    def __init__(self, rows, *, has_persona=True):
        self.rows = rows
        self.has_persona = has_persona
        self.deleted: list[dict] = []
        self.upserts: list[dict] = []

    def table(self, name):
        return _Q(self, name)


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.filters, self.op, self.cols = db, name, {}, "select", ""

    def select(self, cols="*"):
        self.cols = cols
        return self

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def limit(self, *_a):
        return self

    def order(self, *_a, **_k):
        return self

    @property
    def not_(self):
        return self

    def is_(self, *_a):
        return self

    def delete(self):
        self.op = "delete"
        return self

    def upsert(self, row, **_k):
        self.op, self.row = "upsert", row
        return self

    def execute(self):
        if self.name != "skills":
            return _Res([])
        if not self.db.has_persona and (
            "persona" in self.cols or "persona" in self.filters or self.op == "delete"
        ):
            raise RuntimeError("column skills.persona does not exist (42703)")
        match = [r for r in self.db.rows if all(r.get(k) == v for k, v in self.filters.items())]
        if self.op == "delete":
            self.db.deleted += match
            self.db.rows = [r for r in self.db.rows if r not in match]
            return _Res(match)
        if self.op == "upsert":
            self.db.upserts.append(self.row)
            return _Res([self.row])
        return _Res([dict(r) for r in match])


@pytest.fixture
def db(monkeypatch):
    from brain import skills_registry as sr

    rows = [
        {
            "org_id": "o",
            "id": SA,
            "persona": A,
            "status": "enabled",
            "active": True,
            "submitted_by": "brain",
            "all_agents": True,
            "approved_body": "b",
        },
        {
            "org_id": "o",
            "id": "self-home-way",
            "persona": HOME,
            "status": "enabled",
            "active": True,
            "submitted_by": "brain",
            "all_agents": True,
            "approved_body": "b",
        },
        {
            "org_id": "o",
            "id": ORG,
            "persona": None,
            "status": "enabled",
            "active": True,
            "submitted_by": "acme",
            "all_agents": True,
            "approved_body": "b",
        },
    ]
    fake = _FakeSkillsDb(rows)
    monkeypatch.setattr(sr, "_sb", lambda: (fake, "o"))
    monkeypatch.setattr(sr, "_agent_skill_map", lambda sb, org: {})
    monkeypatch.setattr(sr, "_PERSONA_COL", None)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", HOME)
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    return fake


def test_org_views_hide_a_purchase_personas_skill_in_isolated_orgs(db):
    from brain import skills_registry as sr

    ids = {r["id"] for r in sr.list_skills()}
    assert SA not in ids and ORG in ids and "self-home-way" in ids
    assert sr.get_skill(SA) is None
    assert sr.get_skill(ORG) is not None


def test_consolidated_views_are_unfiltered(db, monkeypatch):
    from brain import skills_registry as sr

    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    assert SA in {r["id"] for r in sr.list_skills()}


def test_stage_refuses_another_owners_id(db):
    from brain import skills_registry as sr

    with pytest.raises(sr.SkillError, match="another owner"):
        sr.stage_skill(SA, "body", "d", submitted_by="acme")  # partner squatting
    with pytest.raises(sr.SkillError, match="another owner"):
        sr.stage_skill(ORG, "body", "d", submitted_by="brain", persona=B)
    row = sr.stage_skill("self-cccc-new", "body", "d", submitted_by="brain", persona=B)
    assert row["persona"] == B and db.upserts[-1]["persona"] == B


def test_persona_purge_hard_deletes_only_that_personas_skills(db):
    from brain import skills_registry as sr

    assert sr.delete_persona_skills(A) == 1
    assert [r["id"] for r in db.deleted] == [SA]
    assert {r["id"] for r in db.rows} == {"self-home-way", ORG}


def test_missing_049_column_degrades_to_org_level(db, monkeypatch):
    from brain import skills_registry as sr

    db.has_persona = False
    for r in db.rows:
        r.pop("persona", None)
    assert {r["id"] for r in sr.live_skills()} == {SA, "self-home-way", ORG}
    assert all(r["persona"] is None for r in sr.live_skills())
    assert sr.delete_persona_skills(A) == 0  # nothing can be scoped yet
    with pytest.raises(sr.SkillError, match="049"):
        sr.stage_skill("self-dddd-x", "b", "d", submitted_by="brain", persona=A)


def test_agent_card_lists_a_scoped_skill_only_on_its_personas_agents(db, monkeypatch):
    from brain import agents
    from brain import skills_registry as sr

    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    monkeypatch.setattr(agents, "permissions", lambda aid: {})
    monkeypatch.setattr("brain.clusters.skill_selector.native_skill_catalog", lambda: [])
    own = {r["id"] for r in sr.agent_skill_view(f"{A}.companion")}
    other = {r["id"] for r in sr.agent_skill_view(f"{B}.companion")}
    assert SA in own and SA not in other and ORG in own and ORG in other


def test_put_skill_reserves_the_self_prefix(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from brain import skills_registry as sr
    from brain.api.server import build_api_router
    from brain.api.sessions import ApiSessionRegistry
    from brain.second_brain import supabase_client
    from tests.test_org_learning_mode import _resolver

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(sr, "get_skill", lambda sid: None)
    staged = []
    monkeypatch.setattr(sr, "stage_skill", lambda *a, **k: staged.append(a) or {"id": a[0]})
    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
        )
    )
    c = TestClient(app)
    r = c.put("/v1/skills/self-squat", headers={"Authorization": "Bearer kp"}, json={"body": "x"})
    assert r.status_code == 400 and "reserved" in r.json()["detail"], r.text
    assert staged == []


# ── clone ────────────────────────────────────────────────────────────────────


def test_clone_drops_only_the_templates_own_skill_edges():
    from brain import personas
    from brain.node_authoring import scoped_id

    own = "fragment." + scoped_id("tmpl", "self-way")
    other = "fragment." + scoped_id("someone_else", "self-way")
    assert personas._carried_edge(own, "tmpl") is False
    assert personas._carried_edge(other, "tmpl") is True
    assert personas._carried_edge("fragment.self-legacy-org-skill", "tmpl") is True
    assert personas._carried_edge("frontal.executive", "tmpl") is True
