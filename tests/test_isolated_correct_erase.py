"""Isolated orgs: correcting a persona's private lessons and erasing what its owner
built.

Each isolated persona keeps its own lessons (hypotheses_private.json), self-written
skills (skills.persona) and muscle memory (episodes/procedures.lance). Until
2026-10-19 the owner's lesson review reached only the org-wide store, which is inert
in an isolated org, and an end user's erasure removed their lessons and bond but not
the skills and procedures a persona built from their sessions.

Decided with the owner: private lesson TEXT is readable by the org owner/admin (it
passed the de-identification gate before it was stored), each read is logged as
`lesson_read`, not `content_read`, so the isolation snapshot's content_reads_30d
stays at zero. Erasing a persona's recorded owner drops its self-written skills and
muscle memory and releases the ownership row; the persona itself stays.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import cross_learning, org_settings, persona_owners, read_policy
from brain import learning_mode as lm
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry
from brain.hypothesis_store import HypothesisStore
from brain.persona_key import persona_state_root
from brain.session_turn import _TurnMixin
from tests.test_org_learning_mode import _resolver

A, B, HOME = "the_poet_a1", "the_poet_b2", "home_p"
OWN = {"Authorization": "Bearer ko"}
PARTNER = {"Authorization": "Bearer kp"}


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """An isolated org: the org-wide store plus A's and B's private stores."""
    from brain import personas

    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", HOME)
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    monkeypatch.setattr(lm, "audit_log_path", lambda: tmp_path / "governance_audit.jsonl")
    monkeypatch.setattr(read_policy, "_recent_reads", {})
    known = {A, B, HOME}
    monkeypatch.setattr(personas, "get", lambda s: {} if s in known else None)
    ids = {}
    for name, path in (
        ("org", cross_learning.org_store_path()),
        (A, cross_learning.private_store_path(A)),
        (B, cross_learning.private_store_path(B)),
    ):
        st = HypothesisStore()
        ids[name] = st.add(f"Lesson of {name}", "eu:buyer", persona=name).id
        cross_learning.save_store(st, path)
    return ids


def _api(runner=None):
    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
            hypotheses_runner=runner,
        )
    )
    return TestClient(app)


def _principles(path):
    return [h.principle for h in cross_learning.load_store(path).all()]


def _events(kind):
    return [e for e in lm.tail_audit() if e["event"] == kind]


# ── per-lesson review of a private store ─────────────────────────────────────


def test_private_list_reads_that_persona_only_and_is_logged(stores):
    c = _api()
    assert c.get(f"/v1/org/hypotheses?private={A}", headers=PARTNER).status_code == 403
    body = c.get(f"/v1/org/hypotheses?private={A}", headers=OWN).json()
    assert body["store"] == "private" and body["persona"] == A
    assert [r["principle"] for r in body["lessons"]] == [f"Lesson of {A}"]
    assert "contributors" not in body["lessons"][0]
    # The org-wide store is still the default.
    org = c.get("/v1/org/hypotheses", headers=OWN).json()
    assert org["store"] == "org" and [r["principle"] for r in org["lessons"]] == ["Lesson of org"]
    # Logged as a lesson read, never as a content read.
    (ev,) = _events(read_policy.LESSON_READ_EVENT)
    assert ev["persona"] == A and ev["rows"] == 1 and "principle" not in ev
    assert read_policy.count_content_reads(A) == 0
    assert read_policy.count_content_reads(A, event=read_policy.LESSON_READ_EVENT) == 1


def test_private_list_unknown_persona_is_404_and_empty_store_is_empty(stores, tmp_path):
    c = _api()
    assert c.get("/v1/org/hypotheses?private=nobody", headers=OWN).status_code == 404
    assert c.get("/v1/org/hypotheses?private=", headers=OWN).status_code == 400
    body = c.get(f"/v1/org/hypotheses?private={HOME}", headers=OWN).json()
    assert body["count"] == 0
    # Reading an empty store creates nothing.
    assert not cross_learning.private_store_path(HOME).exists()


def test_private_remove_touches_only_that_store(stores):
    c = _api()
    hid = stores[A]
    assert c.delete(f"/v1/org/hypotheses/{hid}?private={A}", headers=PARTNER).status_code == 403
    # Not in B's store, not in the org-wide store.
    assert c.delete(f"/v1/org/hypotheses/{hid}?private={B}", headers=OWN).status_code == 404
    assert c.delete(f"/v1/org/hypotheses/{hid}", headers=OWN).status_code == 404
    r = c.delete(f"/v1/org/hypotheses/{hid}?private={A}", headers=OWN)
    assert r.json() == {"ok": True, "id": hid, "persona": A}
    assert _principles(cross_learning.private_store_path(A)) == []
    assert _principles(cross_learning.private_store_path(B)) == [f"Lesson of {B}"]
    assert _principles(cross_learning.org_store_path()) == ["Lesson of org"]
    (ev,) = _events("hypothesis_removed")
    assert ev["persona"] == A and ev["principle"] == f"Lesson of {A}"


def test_reattributing_a_private_lesson_is_refused(stores):
    r = _api().put(
        f"/v1/org/hypotheses/{stores[A]}?private={A}", headers=OWN, json={"personas": []}
    )
    assert r.status_code == 400


def test_private_edits_go_through_the_runner_with_the_path(stores):
    seen = []

    async def runner(fn, path=None):
        seen.append(path)
        return await asyncio.to_thread(cross_learning.edit_store, fn, path)

    c = _api(runner)
    assert c.get(f"/v1/org/hypotheses?private={A}", headers=OWN).json()["count"] == 1
    assert c.get("/v1/org/hypotheses", headers=OWN).json()["count"] == 1
    assert seen == [cross_learning.private_store_path(A), None]


def test_brain_runner_edits_a_private_store_under_the_lock(stores):
    class _B(_TurnMixin):
        pass

    async def go():
        b = _B()
        b._consolidation_lock = asyncio.Lock()
        path = cross_learning.private_store_path(A)
        assert await b.api_edit_hypotheses(lambda st: st.remove(stores[A]), path=path)
        await b._consolidation_lock.acquire()
        with pytest.raises(TimeoutError):
            await b.api_edit_hypotheses(lambda st: None, timeout_s=0.05, path=path)
        b._consolidation_lock.release()

    asyncio.run(go())
    assert _principles(cross_learning.private_store_path(A)) == []
    assert _principles(cross_learning.org_store_path()) == ["Lesson of org"]


def test_isolation_snapshot_reports_lesson_reads(stores, monkeypatch):
    from brain import persona_audit

    monkeypatch.setattr(persona_audit, "snapshot", lambda p: {"persona": p})
    c = _api()
    c.get(f"/v1/org/hypotheses?private={A}", headers=OWN)
    snap = c.get(f"/v1/personas/{A}/isolation", headers=OWN).json()
    assert snap["content_reads_30d"] == 0 and snap["lesson_reads_30d"] == 1


# ── console ──────────────────────────────────────────────────────────────────


@pytest.fixture
def console(stores, monkeypatch):
    from brain.ui import auth as ui_auth
    from brain.ui.server import UIServer

    monkeypatch.delenv("BRAIN_AUTH_DISABLED", raising=False)
    monkeypatch.setattr(ui_auth, "is_disabled", lambda: False)
    monkeypatch.setattr(ui_auth, "is_public_path", lambda p: True)
    monkeypatch.setattr(ui_auth, "is_admin", lambda claims: False)
    monkeypatch.setattr(ui_auth, "owner_mismatch", lambda claims: False)
    role = {"v": "admin"}
    monkeypatch.setattr(ui_auth, "is_org_admin", lambda claims: role["v"] == "admin")
    server = UIServer(emitter_queue=asyncio.Queue())
    return TestClient(server._build_app()), role


def test_console_private_list_and_remove(console, stores):
    client, role = console
    role["v"] = "member"
    assert client.get(f"/org/hypotheses?private={A}").status_code == 403
    role["v"] = "admin"
    body = client.get(f"/org/hypotheses?private={A}").json()
    assert body["store"] == "private" and [r["principle"] for r in body["lessons"]] == [
        f"Lesson of {A}"
    ]
    assert _events(read_policy.LESSON_READ_EVENT)[0]["persona"] == A
    assert client.get("/org/hypotheses?private=nobody").status_code == 404
    hid = stores[A]
    assert client.delete(f"/org/hypotheses/{hid}?private={B}").status_code == 404
    assert client.delete(f"/org/hypotheses/{hid}?private={A}").json() == {"ok": True, "id": hid}
    assert _principles(cross_learning.private_store_path(A)) == []
    assert _principles(cross_learning.org_store_path()) == ["Lesson of org"]


def _corroboration_spy(monkeypatch):
    state = {"v": "people", "switched": 0}

    def _set(v):
        state["v"] = org_settings._canonical_corroboration(v)
        return state["v"]

    monkeypatch.setattr(org_settings, "lesson_corroboration", lambda: state["v"])
    monkeypatch.setattr(org_settings, "set_lesson_corroboration", _set)
    return state


def test_console_writes_corroboration_after_the_switch(console, monkeypatch):
    """A refused switch must leave nothing half-applied (same order as the API)."""
    client, _ = console
    state = _corroboration_spy(monkeypatch)

    def _refuse(body, actor):
        state["switched"] += 1
        raise lm.SwitchError(409, {"detail": "isolated personas exist: purge or archive first"})

    monkeypatch.setattr(lm, "switch", _refuse)
    r = client.post(
        "/org/learning_mode",
        json={
            "learning_mode": "consolidated",
            "confirm": True,
            "lesson_corroboration": "people_or_days",
        },
    )
    assert r.status_code == 409 and state["switched"] == 1
    assert state["v"] == "people" and not _events("lesson_corroboration_changed")
    # A bad value is refused before the switch runs.
    r = client.post(
        "/org/learning_mode",
        json={"learning_mode": "consolidated", "confirm": True, "lesson_corroboration": "bogus"},
    )
    assert r.status_code == 400 and state["switched"] == 1


def test_console_corroboration_alone_still_works(console, monkeypatch):
    client, _ = console
    state = _corroboration_spy(monkeypatch)
    monkeypatch.setattr(lm, "describe", lambda: {"lesson_corroboration": state["v"]})
    r = client.post("/org/learning_mode", json={"lesson_corroboration": "occasions"})
    assert r.status_code == 200 and r.json()["lesson_corroboration"] == "people_or_days"
    assert client.post("/org/learning_mode", json={}).status_code == 400


def test_api_checks_corroboration_before_the_switch(stores, monkeypatch):
    calls = []
    monkeypatch.setattr(lm, "switch", lambda body, actor: calls.append(body))
    r = _api().put(
        "/v1/org/permissions",
        headers=OWN,
        json={"learning_mode": "consolidated", "confirm": True, "lesson_corroboration": "bogus"},
    )
    assert r.status_code == 400 and calls == []


# ── end-user erasure reaches what an owned persona built ─────────────────────


class _Selector:
    def __init__(self):
        self.warmed = 0

    async def warm_partner_skills(self):
        self.warmed += 1


class _MuscleMemory:
    def __init__(self):
        self.closed: list[str] = []

    def forget_persona(self, slug):
        self.closed.append(slug)
        return True


class _Brain(_TurnMixin):
    def __init__(self):
        self.persona_name = HOME
        self.skill_selector = _Selector()
        self.motor = type("M", (), {"_subsystems": [_MuscleMemory()]})()


def _procedures(slug):
    from brain.persona_audit import PROCEDURES_DIR

    p = persona_state_root(slug) / PROCEDURES_DIR
    p.mkdir(parents=True, exist_ok=True)
    (p / "data.lance").write_text("x")
    return p


@pytest.fixture
def owned(stores, monkeypatch):
    from brain import skills_registry

    log = {"skills": [], "released": []}
    owners = {A: "buyer", HOME: "buyer", B: "someone-else"}
    monkeypatch.setattr(
        persona_owners, "owned_by", lambda eu: sorted(p for p, o in owners.items() if o == eu)
    )

    def _delete(slug):
        log["skills"].append(slug)
        return 2

    monkeypatch.setattr(skills_registry, "delete_persona_skills", _delete)
    monkeypatch.setattr(persona_owners, "release", lambda s: log["released"].append(s) or 1)
    return log


def test_erasing_the_owner_drops_skills_and_muscle_memory(owned):
    a, b, home = _procedures(A), _procedures(B), _procedures(HOME)
    brain = _Brain()
    out = asyncio.run(brain._purge_owned_personas("buyer"))
    # The home persona is never touched: its procedure store is the org's.
    assert out == {
        "owned_personas": [A],
        "owned_skills": 2,
        "owned_muscle_memory": 1,
        "persona_owners": 1,
    }
    assert not a.exists() and b.exists() and home.exists()
    assert owned["skills"] == [A] and owned["released"] == [A]
    assert brain.motor._subsystems[0].closed == [A]  # handle closed before the rmtree
    assert brain.skill_selector.warmed == 1


def test_someone_who_owns_nothing_changes_nothing(owned):
    a = _procedures(A)
    brain = _Brain()
    assert asyncio.run(brain._purge_owned_personas("stranger")) == {"owned_personas": []}
    assert a.exists() and owned["skills"] == [] and brain.skill_selector.warmed == 0


def test_a_failed_lookup_or_step_fails_the_erasure(owned, monkeypatch):
    from brain import skills_registry

    brain = _Brain()
    monkeypatch.setattr(persona_owners, "owned_by", lambda eu: None)
    out = asyncio.run(brain._purge_owned_personas("buyer"))
    assert out["owned_personas"].startswith("error")

    monkeypatch.setattr(persona_owners, "owned_by", lambda eu: [A])

    def _boom(slug):
        raise RuntimeError("skills table gone")

    monkeypatch.setattr(skills_registry, "delete_persona_skills", _boom)
    out = asyncio.run(brain._purge_owned_personas("buyer"))
    assert out["owned_skills"].startswith("error")


def test_full_erasure_reports_the_owned_personas(owned, monkeypatch):
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    _procedures(A)
    out = asyncio.run(_Brain().api_purge_end_user("buyer"))
    assert out["deleted"]["owned_personas"] == [A]
    assert out["deleted"]["owned_muscle_memory"] == 1


# ── persona_owners lookups ───────────────────────────────────────────────────


class _Rows:
    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.filters, self.deleted = rows, fail, [], False

    def table(self, name):
        return self

    def select(self, cols):
        return self

    def delete(self):
        self.deleted = True
        return self

    def eq(self, k, v):
        self.filters.append((k, v))
        return self

    def execute(self):
        if self.fail:
            raise RuntimeError("down")
        return type("R", (), {"data": self.rows})()


def test_owned_by_reads_the_registry(monkeypatch):
    rows = _Rows([{"persona": "The Poet A1"}, {"persona": A}, {"persona": ""}])
    monkeypatch.setattr(persona_owners, "_sb", lambda: (rows, "org-1"))
    assert persona_owners.owned_by("buyer") == [A]
    assert ("end_user_id", "buyer") in rows.filters and ("org_id", "org-1") in rows.filters
    assert persona_owners.owned_by("  ") == []
    monkeypatch.setattr(persona_owners, "_sb", lambda: (_Rows([], fail=True), "org-1"))
    assert persona_owners.owned_by("buyer") is None
    monkeypatch.setattr(persona_owners, "_sb", lambda: None)
    assert persona_owners.owned_by("buyer") == []


def test_release_removes_the_row_and_clears_the_index(monkeypatch):
    from brain import persona_index

    rows = _Rows([{"persona": A}])
    monkeypatch.setattr(persona_owners, "_sb", lambda: (rows, "org-1"))
    calls = []
    monkeypatch.setattr(persona_index, "set_owner", lambda *a, **k: calls.append(a) or True)
    assert persona_owners.release(A) == 1
    assert rows.deleted and calls == [(A, False)]
