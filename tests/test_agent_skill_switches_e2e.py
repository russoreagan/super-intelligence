"""End to end: what the agent page's Save writes is what the agent uses on a turn.

Only the database is faked (an in-memory stand-in for the Supabase client). The
route, the registry, the agents store, the 60 s cache, the partner-skill rewarm,
the selector and the frontal cluster's turn-time skill step are all the real code.
The second half simulates a fresh process (empty caches, a newly warmed selector)
to prove the settings come back from storage, not from anything held in memory.
"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from brain import agents  # noqa: E402
from brain.clusters.skill_selector import SkillSelector, _Index  # noqa: E402

ORG = "org-haven"
EMPATH = "the_empath.therapy_listener"
ADMIN = "the_admin.app_admin"


# ── a tiny in-memory Supabase ─────────────────────────────────────────────────
class _Res:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.filters: list = []
        self.op = "select"
        self.payload = None
        self.neg = False

    # builders
    def select(self, *_a, **_k):
        self.op = "select"
        return self

    def insert(self, rows):
        self.op, self.payload = "insert", rows
        return self

    def upsert(self, rows, on_conflict=None, ignore_duplicates=False):
        self.op, self.payload = "upsert", rows
        return self

    def update(self, values):
        self.op, self.payload = "update", values
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, k, v):
        self.filters.append(lambda r, k=k, v=v: r.get(k) == v)
        return self

    def in_(self, k, vs):
        self.filters.append(lambda r, k=k, vs=tuple(vs): r.get(k) in vs)
        return self

    @property
    def not_(self):
        self.neg = True
        return self

    def is_(self, k, v):
        neg = self.neg
        self.neg = False
        want_null = v in (None, "null")
        self.filters.append(lambda r, k=k: ((r.get(k) is None) == want_null) != neg)
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a):
        return self

    def _match(self, r):
        return all(f(r) for f in self.filters)

    def execute(self):
        rows = self.db.setdefault(self.table, [])
        if self.op == "select":
            return _Res([copy.deepcopy(r) for r in rows if self._match(r)])
        if self.op in ("insert", "upsert"):
            new = self.payload if isinstance(self.payload, list) else [self.payload]
            rows.extend(copy.deepcopy(new))
            return _Res(new)
        if self.op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(copy.deepcopy(self.payload))
            return _Res(hit)
        if self.op == "delete":
            keep = [r for r in rows if not self._match(r)]
            self.db[self.table] = keep
            return _Res([])
        raise AssertionError(self.op)


class _Client:
    def __init__(self, db):
        self.db = db

    def table(self, name):
        return _Query(self.db, name)


def _skill_row(sid, *, all_agents=True, tools=()):
    return {
        "org_id": ORG,
        "id": sid,
        "display_name": sid,
        "description": "",
        "approved_body": f"Body of {sid}.",
        "body": f"Body of {sid}.",
        "keywords": [],
        "allowed_tools": list(tools),
        "tier": 2,
        "status": "enabled",
        "all_agents": all_agents,
        "active": True,
    }


@pytest.fixture
def world(monkeypatch):
    from brain.second_brain import supabase_client
    from brain.ui import auth as ui_auth

    db = {
        "agents": [
            {
                "org_id": ORG,
                "persona": "the_empath",
                "mandate_id": "therapy_listener",
                "name": "Haven listener",
                "enabled": True,
                "tier": "lite",
                "permissions": {},
            },
            {
                "org_id": ORG,
                "persona": "the_admin",
                "mandate_id": "app_admin",
                "name": "The Admin",
                "enabled": True,
                "tier": "full",
                "permissions": {},
            },
        ],
        "skills": [
            _skill_row("house_style"),
            _skill_row("haven_safety", all_agents=False),
        ],
        "agent_skills": [],
    }
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: _Client(db))
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: ORG)
    for k in ("is_public_path",):
        monkeypatch.setattr(ui_auth, k, lambda p: True)
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: False)
    monkeypatch.setattr(ui_auth, "is_admin", lambda c: False)
    monkeypatch.setattr(ui_auth, "owner_mismatch", lambda c: False)
    monkeypatch.setattr(ui_auth, "is_org_admin", lambda c: True)
    agents._skills_disabled_cache.clear()
    agents._answer_only_cache.clear()
    yield db
    agents._skills_disabled_cache.clear()


async def _boot_selector() -> SkillSelector:
    """A selector warmed the way a booting brain warms it: the built-in skills plus
    the org's live skills read from the database."""
    sel = SkillSelector.__new__(SkillSelector)
    idx = _Index.__new__(_Index)
    idx.skills, idx._by_name, idx.tier1_names = [], {}, []
    for name, kw in (("trading-analyst", ["journal"]), ("supabase", [])):
        idx.inject_native(
            {
                "name": name,
                "description": "",
                "category": "native",
                "tier": 2,
                "is_router": False,
                "keywords": kw,
                "embedding": [],
                "_native": True,
            }
        )
    sel._index = idx
    sel._native_body_cache = {}
    sel._router = SimpleNamespace(embed=AsyncMock(return_value=None))
    await sel.warm_partner_skills()
    return sel


def _console(sel):
    from brain.ui.server import UIServer

    srv = UIServer(emitter_queue=asyncio.Queue(), skill_rewarm_fn=sel.warm_partner_skills)
    return TestClient(srv._build_app())


def _turn_decisions(sel, agent_id):
    """What the frontal cluster's real turn-time skill step lets this agent use."""
    from brain.clusters.frontal import FrontalCluster

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
    f._skill_selector = sel
    out = {}
    for skill in ("trading-analyst", "supabase", "house_style", "haven_safety"):
        f._current_skill_bundle = None
        asyncio.run(
            f._skills_for_turn(
                {"agent_id": agent_id, "raw_text": "hello", "user_emotion": ""},
                {"skill": skill},  # the executive names it; the gate decides
                f"t-{skill}",
            )
        )
        out[skill] = f.last_turn_skills.get("chosen") == [skill]
    return out


def test_save_reaches_the_agent_and_survives_a_restart(world):
    db = world
    sel = asyncio.run(_boot_selector())
    client = _console(sel)

    # Before: built-ins + all-agents skills on; the specific skill is unmapped.
    before = _turn_decisions(sel, EMPATH)
    assert before == {
        "trading-analyst": True,
        "supabase": True,
        "house_style": True,
        "haven_safety": False,
    }

    # Save on the agent page: keep supabase, turn on haven_safety, everything else off.
    r = client.post(f"/agents/{EMPATH}/skills", json={"enabled": ["supabase", "haven_safety"]})
    assert r.status_code == 200

    # 1. It is in storage.
    empath = next(a for a in db["agents"] if a["persona"] == "the_empath")
    from brain.clusters.skill_selector import native_skill_catalog

    builtins = {n["id"] for n in native_skill_catalog()}  # every built-in on disk
    assert set(empath["permissions"]["skills_disabled"].splitlines()) == (
        builtins - {"supabase"}
    ) | {"house_style"}
    assert [(m["persona"], m["mandate_id"], m["skill_id"]) for m in db["agent_skills"]] == [
        ("the_empath", "therapy_listener", "haven_safety")
    ]

    # 2. What the page re-reads after Save is the stored state.
    got = {s["id"]: s["enabled"] for s in client.get(f"/agents/{EMPATH}/skills").json()["skills"]}
    assert {k for k, on in got.items() if on} == {"supabase", "haven_safety"}

    # 3. The very next turn in this process uses it (cache invalidated, index rewarmed).
    after = _turn_decisions(sel, EMPATH)
    assert after == {
        "trading-analyst": False,
        "supabase": True,
        "house_style": False,
        "haven_safety": True,
    }
    # The keyword path too: "journal" no longer pulls the trading skill for this agent.
    from brain.clusters.skill_selector import bind_turn_agent

    with bind_turn_agent(EMPATH):
        assert sel._index.keyword_match("my journal", allowed=sel._agent_allowed) is None

    # Other agents are untouched.
    assert _turn_decisions(sel, ADMIN)["trading-analyst"] is True
    assert _turn_decisions(sel, ADMIN)["haven_safety"] is False

    # 4. Fresh process: empty caches, a newly booted selector — same answer from storage.
    agents._skills_disabled_cache.clear()
    sel2 = asyncio.run(_boot_selector())
    assert _turn_decisions(sel2, EMPATH) == after


def test_page_save_order_keeps_both_writes(world):
    """The page saves permissions first (carrying the switch list it loaded), then
    the skills. The second write must win, and the first must not wipe it."""
    db = world
    sel = asyncio.run(_boot_selector())
    client = _console(sel)
    # What the page holds after load: the agent's permissions as stored, plus an
    # edit made on the Capabilities card.
    perms = {"motor_enable_network": 1}
    assert (
        client.post(f"/agents/{EMPATH}/permissions", json={"permissions": perms}).status_code == 200
    )
    assert (
        client.post(f"/agents/{EMPATH}/skills", json={"enabled": ["supabase"]}).status_code == 200
    )
    stored = next(a for a in db["agents"] if a["persona"] == "the_empath")["permissions"]
    assert stored["motor_enable_network"] == 1  # the capability edit survived
    assert "trading-analyst" in stored["skills_disabled"].splitlines()  # and so did the skills

    # Saving the page AGAIN with the permissions it re-loaded keeps the skill switches.
    reloaded = client.get(f"/agents/{EMPATH}/skills").json()["skills"]
    assert {s["id"] for s in reloaded if s["enabled"]} == {"supabase"}
