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
    def __init__(self, store):
        self._store = store
        self._filters: list[tuple] = []
        self._not_null: list[str] = []
        self._op = None
        self._payload = None

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

    def upsert(self, row, on_conflict=None):  # noqa: ARG002
        self._op = "upsert"
        self._payload = row
        return self

    def update(self, patch):
        self._op = "update"
        self._payload = patch
        return self

    def _matching(self):
        out = []
        for r in self._store.values():
            if all(r.get(k) == v for k, v in self._filters) and all(
                r.get(k) is not None for k in self._not_null
            ):
                out.append(r)
        return out

    def execute(self):
        if self._op == "select":
            return _Result([dict(r) for r in self._matching()])
        if self._op == "upsert":
            key = (self._payload["org_id"], self._payload["id"])
            self._store[key] = dict(self._payload)
            return _Result([dict(self._payload)])
        if self._op == "update":
            rows = self._matching()
            for r in rows:
                r.update(self._payload)
            return _Result([dict(r) for r in rows])
        return _Result([])


class _FakeClient:
    def __init__(self, store):
        self._store = store

    def table(self, _name):
        return _QB(self._store)


@pytest.fixture()
def store(monkeypatch):
    st: dict = {}
    monkeypatch.setattr(sr, "_sb", lambda: (_FakeClient(st), "org1"))
    return st


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
