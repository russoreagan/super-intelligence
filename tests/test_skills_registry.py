"""
Skills registry lifecycle — the admission state machine + the TOCTOU guard.

Run against an in-memory fake of the Supabase query builder (the registry's only
dependency), so this exercises the REAL registry code: status transitions, the
approved-vs-latest body split, and the rule that an edit to an approved skill keeps
the previously-cleared body live until the new one is re-approved.
"""

from __future__ import annotations

import pytest

import brain.skills_registry as sr

# ── minimal in-memory fake of the supabase-py query builder ──────────────────────
# Table-aware: each table is a list of row dicts, so it handles both `skills`
# (upsert by org_id,id) and `agent_skills` (insert/delete, composite key).


class _Result:
    def __init__(self, data):
        self.data = data


class _Not:
    def __init__(self, qb):
        self._qb = qb

    def is_(self, key, _val):  # registry calls .not_.is_("approved_body", "null")
        self._qb._not_null.append(key)
        return self._qb


class _QB:
    def __init__(self, rows: list):
        self._rows = rows
        self._filters: list[tuple] = []
        self._not_null: list[str] = []
        self._op = None
        self._payload = None
        self._on_conflict = None

    def select(self, _cols):
        self._op = "select"
        return self

    def eq(self, k, v):
        self._filters.append((k, v))
        return self

    @property
    def not_(self):
        return _Not(self)

    def order(self, *_a, **_k):
        return self

    def upsert(self, row, on_conflict=None):
        self._op = "upsert"
        self._payload = row
        self._on_conflict = (on_conflict or "").split(",")
        return self

    def insert(self, rows):
        self._op = "insert"
        self._payload = rows if isinstance(rows, list) else [rows]
        return self

    def update(self, patch):
        self._op = "update"
        self._payload = patch
        return self

    def delete(self):
        self._op = "delete"
        return self

    def _matching(self):
        return [
            r
            for r in self._rows
            if all(r.get(k) == v for k, v in self._filters)
            and all(r.get(k) is not None for k in self._not_null)
        ]

    def execute(self):
        if self._op == "select":
            return _Result([dict(r) for r in self._matching()])
        if self._op == "upsert":
            keys = [k for k in self._on_conflict if k]
            for r in self._rows:
                if all(r.get(k) == self._payload.get(k) for k in keys):
                    r.clear()
                    r.update(self._payload)
                    return _Result([dict(self._payload)])
            self._rows.append(dict(self._payload))
            return _Result([dict(self._payload)])
        if self._op == "insert":
            for row in self._payload:
                self._rows.append(dict(row))
            return _Result([dict(r) for r in self._payload])
        if self._op == "update":
            rows = self._matching()
            for r in rows:
                r.update(self._payload)
            return _Result([dict(r) for r in rows])
        if self._op == "delete":
            keep = [r for r in self._rows if r not in self._matching()]
            removed = len(self._rows) - len(keep)
            self._rows[:] = keep
            return _Result([{}] * removed)
        return _Result([])


class _FakeClient:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _QB(self._tables.setdefault(name, []))


@pytest.fixture()
def store(monkeypatch):
    tables: dict = {}
    monkeypatch.setattr(sr, "_sb", lambda: (_FakeClient(tables), "org1"))
    return tables


# ── lifecycle ────────────────────────────────────────────────────────────────────


def test_pending_skill_is_not_live(store):
    sr.stage_skill("risk", "BODY1", "desc", submitted_by="pA")
    assert sr.live_skills() == []  # nothing serves until cleared


def test_enable_promotes_body_to_live(store):
    sr.stage_skill("risk", "BODY1", "desc", submitted_by="pA")
    sr.set_status("risk", "enabled")
    live = sr.live_skills()
    assert len(live) == 1
    assert live[0]["id"] == "risk"
    assert live[0]["body"] == "BODY1"


def test_edit_of_approved_skill_keeps_old_body_live_until_recleared(store):
    # Approve v1.
    sr.stage_skill("risk", "BODY1", "desc", submitted_by="pA")
    sr.set_status("risk", "enabled")
    # Submit a malicious edit — status resets to pending, latest body changes...
    sr.stage_skill("risk", "BODY2-EVIL", "desc", submitted_by="pA")
    row = sr.get_skill("risk")
    assert row["status"] == "pending"
    assert row["body"] == "BODY2-EVIL"
    # ...but the LIVE (injected) body is still the cleared v1 (the TOCTOU guard).
    assert sr.live_skills()[0]["body"] == "BODY1"
    # Approving the edit promotes the new body.
    sr.set_status("risk", "enabled")
    assert sr.live_skills()[0]["body"] == "BODY2-EVIL"


def test_flagged_skill_is_not_live_and_shows_in_queue(store):
    sr.stage_skill("x", "B", "d")
    sr.set_status("x", "flagged", screen_notes={"judge": {"verdict": "flag"}})
    assert sr.live_skills() == []
    assert [r["id"] for r in sr.list_flagged()] == ["x"]


def test_rejected_edit_keeps_prior_approved_live(store):
    sr.stage_skill("x", "GOOD", "d")
    sr.set_status("x", "enabled")
    sr.stage_skill("x", "BAD", "d")
    sr.set_status("x", "rejected", screen_notes={"judge": {"verdict": "reject"}})
    # The rejected edit never goes live; the prior cleared body still serves.
    assert sr.live_skills()[0]["body"] == "GOOD"


def test_delete_removes_from_live(store):
    sr.stage_skill("x", "B", "d")
    sr.set_status("x", "enabled")
    assert len(sr.live_skills()) == 1
    assert sr.delete_skill("x") is True
    assert sr.live_skills() == []


def test_delete_unknown_returns_false(store):
    assert sr.delete_skill("nope") is False


def test_set_status_unknown_raises(store):
    with pytest.raises(sr.SkillError):
        sr.set_status("nope", "enabled")


# ── validation ───────────────────────────────────────────────────────────────────


def test_bad_id_rejected(store):
    with pytest.raises(sr.SkillError):
        sr.stage_skill("Bad Id", "B", "d")


def test_oversized_body_rejected(store):
    with pytest.raises(sr.SkillError):
        sr.stage_skill("x", "z" * (sr.MAX_BODY_CHARS + 1), "d")


def test_bad_status_rejected(store):
    sr.stage_skill("x", "B", "d")
    with pytest.raises(sr.SkillError):
        sr.set_status("x", "bogus")


# ── skill ↔ agent mapping ────────────────────────────────────────────────────────


def test_default_skill_is_all_agents(store):
    sr.stage_skill("risk", "B", "d")
    sr.set_status("risk", "enabled")
    s = sr.live_skills()[0]
    assert s["all_agents"] is True
    assert s["agents"] == []


def test_skill_centric_mapping_round_trip(store):
    sr.stage_skill("risk", "B", "d")
    sr.set_status("risk", "enabled")
    sr.set_skill_all_agents("risk", False)
    sr.set_skill_agents("risk", ["the_visionary.research_lead", "the_sage.support"])
    s = sr.live_skills()[0]
    assert s["all_agents"] is False
    assert set(s["agents"]) == {"the_visionary.research_lead", "the_sage.support"}


def test_set_skill_agents_replaces_prior(store):
    sr.stage_skill("risk", "B", "d")
    sr.set_status("risk", "enabled")
    sr.set_skill_agents("risk", ["p.one"])
    sr.set_skill_agents("risk", ["p.two", "p.three"])
    s = sr.live_skills()[0]
    assert set(s["agents"]) == {"p.two", "p.three"}


def test_agent_centric_mapping(store, monkeypatch):
    monkeypatch.setattr(sr, "_persona", lambda p: p, raising=False)
    from brain import mandates as _m

    monkeypatch.setattr(_m, "_persona", lambda p: p)
    sr.stage_skill("a", "B", "d")
    sr.set_status("a", "enabled")
    sr.stage_skill("b", "B", "d")
    sr.set_status("b", "enabled")
    sr.set_agent_skills("the_visionary", "research_lead", ["a", "b"])
    assert set(sr.agent_skill_ids("the_visionary", "research_lead")) == {"a", "b"}
    sr.set_agent_skills("the_visionary", "research_lead", ["a"])  # replace
    assert sr.agent_skill_ids("the_visionary", "research_lead") == ["a"]


def test_bad_agent_id_rejected(store):
    sr.stage_skill("risk", "B", "d")
    sr.set_status("risk", "enabled")
    with pytest.raises(sr.SkillError):
        sr.set_skill_agents("risk", ["no-dot-here"])
