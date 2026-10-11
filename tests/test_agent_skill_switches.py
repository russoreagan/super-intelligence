"""Per-agent skill switches: the agent page's Skills card.

Each agent can switch any skill off, built-in or org (agents.permissions
`skills_disabled`, a disable list so new skills still reach every agent). A
"specific agents" org skill is on where it's mapped (agent_skills). The selector
applies both for the agent the turn speaks as.

Also pins a pre-existing bug: brain.agent_ctx is bound only for motor dispatch and
jobs, never for a conversational turn, so the scope gate saw no agent and a
"specific agents" skill was never picked in conversation, even for its own agent.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brain import agents
from brain.clusters import skill_selector as ss
from brain.clusters.skill_selector import SkillSelector, _Index, bind_turn_agent, turn_agent_id
from brain.turn_ctx import bind_turn

EMPATH = "the_empath.therapy_listener"
ADMIN = "the_admin.app_admin"


def _entry(name, keywords=(), *, partner=False, all_agents=True, agents_=()):
    e = {
        "name": name,
        "description": "",
        "category": "partner" if partner else "native",
        "tier": 2,
        "is_router": False,
        "keywords": list(keywords),
        "embedding": [],
        "_native": True,
    }
    if partner:
        e.update(_partner=True, _all_agents=all_agents, _agents=set(agents_))
    return e


def _selector(*entries) -> SkillSelector:
    sel = SkillSelector.__new__(SkillSelector)
    idx = _Index.__new__(_Index)
    idx.skills = list(entries)
    idx._by_name = {e["name"]: e for e in entries}
    idx.tier1_names = []
    sel._index = idx
    sel._router = SimpleNamespace(embed=AsyncMock(return_value=None))
    return sel


@pytest.fixture
def disabled(monkeypatch):
    table: dict[str, set[str]] = {}
    monkeypatch.setattr(agents, "disabled_skills", lambda aid: frozenset(table.get(aid, set())))
    return table


# ── which agent is this turn? ─────────────────────────────────────────────────


def test_turn_agent_resolution_order():
    assert turn_agent_id() == ""
    with bind_turn("agent", session_id="s", agent_id=EMPATH, end_user_id="u"):
        assert turn_agent_id() == EMPATH
        with bind_turn_agent(ADMIN):  # the frontal's binding beats the session's
            assert turn_agent_id() == ADMIN
    with bind_turn_agent(None):
        assert turn_agent_id() == ""


# ── the gate ──────────────────────────────────────────────────────────────────


def test_switched_off_builtin_skill_is_blocked_only_for_that_agent(disabled):
    sel = _selector(_entry("trading-analyst", ["journal"]))
    disabled[EMPATH] = {"trading-analyst"}
    with bind_turn_agent(EMPATH):
        assert not sel.allowed_for_current_agent("trading-analyst")
        assert sel._index.keyword_match("my journal", allowed=sel._agent_allowed) is None
    with bind_turn_agent(ADMIN):
        assert sel.allowed_for_current_agent("trading-analyst")
    assert sel.allowed_for_current_agent("trading-analyst")  # no agent: owner chat


def test_specific_agent_skill_is_picked_for_its_agent_in_conversation(disabled):
    # Regression: with nothing bound in agent_ctx this returned False for everyone.
    sel = _selector(_entry("haven-safety", partner=True, all_agents=False, agents_=[EMPATH]))
    with bind_turn_agent(EMPATH):
        assert sel.allowed_for_current_agent("haven-safety")
    with bind_turn_agent(ADMIN):
        assert not sel.allowed_for_current_agent("haven-safety")
    assert not sel.allowed_for_current_agent("haven-safety")


def test_all_agents_org_skill_can_be_switched_off_per_agent(disabled):
    sel = _selector(_entry("house-style", partner=True))
    disabled[ADMIN] = {"house-style"}
    with bind_turn_agent(ADMIN):
        assert not sel.allowed_for_current_agent("house-style")
    with bind_turn_agent(EMPATH):
        assert sel.allowed_for_current_agent("house-style")


async def test_llm_cannot_pick_a_switched_off_skill(disabled):
    # Two close allowed candidates force the LLM step; it names the switched-off
    # skill anyway (it can name any index skill), and the pick is refused.
    sel = _selector(_entry("a"), _entry("b"), _entry("c"))
    disabled[EMPATH] = {"b"}
    sel._router = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
    sel._index.rank = lambda vec, include_tier_1=False: [
        (sel._index.get("a"), 0.5),
        (sel._index.get("b"), 0.5),
        (sel._index.get("c"), 0.49),
    ]
    sel._conversational_cell = SimpleNamespace(
        reset_turn=lambda t: None, call=AsyncMock(return_value='{"skill": "b"}')
    )
    with bind_turn_agent(EMPATH):
        bundle, _, extras = await sel.select_conversational(
            "help", {"response_type": "informative"}, "", [], None
        )
    assert [n for n, _ in extras["candidates"]] == ["a", "c"]
    assert extras["pick_path"] == "llm_null" and bundle.chosen == []


def test_frontal_executive_pick_respects_the_switch(disabled):
    from brain.clusters.frontal import FrontalCluster

    disabled[EMPATH] = {"trading-analyst"}
    f = FrontalCluster.__new__(FrontalCluster)
    f._current_skill_bundle = None
    f._current_query_vec = None
    f.last_turn_skills = {}
    f._router = SimpleNamespace(embed=AsyncMock(return_value=None))
    f._parietal = SimpleNamespace(
        active_skill_context=None,
        recent_turns=lambda n: [],
        set_active_skill_context=lambda a: None,
    )
    sel = _selector(_entry("trading-analyst"))
    sel.select_conversational = AsyncMock(return_value=(None, None, {"gated": True}))
    f._skill_selector = sel
    with bind_turn_agent(EMPATH):
        asyncio.run(
            f._select_skills_for_turn({"raw_text": "x"}, {"skill": "trading-analyst"}, "t1")
        )
    assert f.last_turn_skills["path"] != "executive_pick"
    assert f._current_skill_bundle is None


# ── storage ───────────────────────────────────────────────────────────────────


def test_disabled_skills_cache_accepts_string_or_list(monkeypatch):
    agents._skills_disabled_cache.clear()
    monkeypatch.setattr(agents, "permissions", lambda aid: {"skills_disabled": "a\nb\n"})
    assert agents.disabled_skills(EMPATH) == {"a", "b"}
    agents._skills_disabled_cache.clear()
    monkeypatch.setattr(agents, "permissions", lambda aid: {"skills_disabled": ["c"]})
    assert agents.disabled_skills(EMPATH) == {"c"}
    agents._skills_disabled_cache.clear()


def test_disabled_skills_fails_open(monkeypatch):
    agents._skills_disabled_cache.clear()

    def boom(aid):
        raise RuntimeError("store down")

    monkeypatch.setattr(agents, "permissions", boom)
    assert agents.disabled_skills(EMPATH) == frozenset()


def test_set_disabled_skills_keeps_other_permissions(monkeypatch):
    written = {}
    monkeypatch.setattr(
        agents, "permissions", lambda aid: {"answer_only": 1, "skills_disabled": "x"}
    )
    monkeypatch.setattr(agents, "set_permissions", lambda aid, p: written.update(p) or {})
    agents.set_disabled_skills(EMPATH, ["b", "a", "a"])
    assert written == {"answer_only": 1, "skills_disabled": "a\nb"}
    written.clear()
    agents.set_disabled_skills(EMPATH, [])
    assert written == {"answer_only": 1}


def test_skills_disabled_is_a_stored_permission_key():
    assert "skills_disabled" in agents.PERMISSION_KEYS
    assert agents._clean_permissions({"skills_disabled": "a", "bogus": 1}) == {
        "skills_disabled": "a"
    }


# ── the agent page view + save ────────────────────────────────────────────────


@pytest.fixture
def registry(monkeypatch):
    from brain import skills_registry as sr

    state = {
        "perms": {"skills_disabled": "supabase"},
        "mapped": {},  # agent_id -> [skill ids]
    }
    org_skills = [
        {
            "id": "house-style",
            "display_name": "House style",
            "all_agents": True,
            "agents": [],
            "allowed_tools": ["connector:notion", "network"],
        },
        {"id": "haven-safety", "display_name": "Safety", "all_agents": False, "agents": [EMPATH]},
        {"id": "grief", "display_name": "Grief", "all_agents": False, "agents": []},
    ]
    monkeypatch.setattr(
        ss,
        "native_skill_catalog",
        lambda: [
            {"id": "supabase", "name": "supabase", "description": ""},
            {"id": "trading-analyst", "name": "trading-analyst", "description": ""},
        ],
    )

    def list_skills(include_inactive=False, status=None):
        for s in org_skills:
            s["agents"] = [a for a, ids in state["mapped"].items() if s["id"] in ids] or s["agents"]
        return org_skills

    monkeypatch.setattr(sr, "list_skills", list_skills)
    monkeypatch.setattr(agents, "permissions", lambda aid: dict(state["perms"]))

    def set_disabled(aid, ids):
        state["perms"] = {"skills_disabled": "\n".join(sorted(ids))} if ids else {}
        return {}

    monkeypatch.setattr(agents, "set_disabled_skills", set_disabled)

    def set_agent_skills(persona, mandate, ids):
        aid = f"{persona}.{mandate}"
        state["mapped"][aid] = list(ids)
        for s in org_skills:
            if s["id"] not in ids and aid in s["agents"]:
                s["agents"] = [a for a in s["agents"] if a != aid]
        return {}

    monkeypatch.setattr(sr, "set_agent_skills", set_agent_skills)
    return state


def test_agent_skill_view(registry):
    from brain import skills_registry as sr

    rows = {r["id"]: r for r in sr.agent_skill_view(EMPATH)}
    assert rows["supabase"]["enabled"] is False  # switched off for this agent
    assert rows["trading-analyst"]["enabled"] is True
    assert rows["house-style"] == {
        "id": "house-style",
        "name": "House style",
        "description": "",
        "source": "org",
        "scope": "all",
        "enabled": True,
        "tools": ["connector:notion", "network"],
    }
    assert rows["supabase"]["tools"] == []
    assert rows["haven-safety"]["enabled"] is True  # mapped to this agent
    assert rows["grief"]["enabled"] is False and rows["grief"]["scope"] == "specific"


def test_set_switches_maps_specific_and_disables_the_rest(registry):
    from brain import skills_registry as sr

    rows = sr.set_agent_skill_switches(EMPATH, ["supabase", "grief"])
    on = {r["id"] for r in rows if r["enabled"]}
    assert on == {"supabase", "grief"}
    assert registry["mapped"][EMPATH] == ["grief"]
    assert registry["perms"]["skills_disabled"].splitlines() == ["house-style", "trading-analyst"]


def test_set_switches_rejects_unknown_ids(registry):
    from brain import skills_registry as sr
    from brain.skills_registry import SkillError

    with pytest.raises(SkillError):
        sr.set_agent_skill_switches(EMPATH, ["not-a-skill"])


def test_native_catalog_lists_builtins_without_stances():
    ids = {n["id"] for n in ss.native_skill_catalog()}
    assert "trading-analyst" in ids
    assert not any(i.startswith("stance-") for i in ids)


# ── console routes ────────────────────────────────────────────────────────────


@pytest.fixture
def console(monkeypatch, registry):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from brain.second_brain import supabase_client
    from brain.ui import auth as ui_auth
    from brain.ui.server import UIServer

    monkeypatch.setattr(ui_auth, "is_disabled", lambda: False)
    monkeypatch.setattr(ui_auth, "is_public_path", lambda p: True)
    monkeypatch.setattr(ui_auth, "is_admin", lambda claims: False)
    monkeypatch.setattr(ui_auth, "owner_mismatch", lambda claims: False)
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    rewarms: list[int] = []

    async def rewarm():
        rewarms.append(1)

    srv = UIServer(emitter_queue=asyncio.Queue(), skill_rewarm_fn=rewarm)
    client = TestClient(srv._build_app())

    def as_role(role):
        monkeypatch.setattr(ui_auth, "is_org_admin", lambda claims: role == "admin")

    return SimpleNamespace(client=client, as_role=as_role, rewarms=rewarms)


def test_routes_read_and_write_switches(console):
    console.as_role("admin")
    r = console.client.get(f"/agents/{EMPATH}/skills")
    assert r.status_code == 200 and r.json()["enabled"] is True
    r = console.client.post(f"/agents/{EMPATH}/skills", json={"enabled": ["supabase"]})
    assert r.status_code == 200
    on = {s["id"] for s in r.json()["skills"] if s["enabled"]}
    assert on == {"supabase"}
    assert console.rewarms == [1]
    assert console.client.post(f"/agents/{EMPATH}/skills", json={"enabled": "x"}).status_code == 400
    assert (
        console.client.post(f"/agents/{EMPATH}/skills", json={"enabled": ["nope"]}).status_code
        == 400
    )


def test_routes_are_org_admin_only(console):
    console.as_role("member")
    assert console.client.get(f"/agents/{EMPATH}/skills").status_code == 403
    assert console.client.post(f"/agents/{EMPATH}/skills", json={"enabled": []}).status_code == 403
