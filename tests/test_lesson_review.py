"""Lesson review: read and correct the hypothesis store remotely.

The store lives on the tenant volume (hypotheses.json), so until now the only
remote handle on what cross-learning learned was an all-or-nothing purge. The
2026-10-19 audit needed to see lessons, re-attribute misfiled ones, fold one
person's two ids together (`<uuid>:trading` → `<uuid>@trading`) and drop test
users' contributions — including legacy ids the erase API rejects.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import cross_learning
from brain import learning_mode as lm
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry
from brain.hypothesis_store import OCCASIONS, HypothesisStore
from tests.test_org_learning_mode import _resolver

DAY = 86400.0
OLD = "742c0dfd:trading"
CUR = "742c0dfd@trading"


class _Clock:
    def __init__(self) -> None:
        self.t = 1_760_000_000.0

    def __call__(self) -> float:
        return self.t


# ── store edits ──────────────────────────────────────────────────────────────


def test_merge_source_folds_one_person_into_one_token():
    clock = _Clock()
    st = HypothesisStore(salt="s", now_fn=clock, corroboration=OCCASIONS)
    a = st.add("lesson A", f"eu:{OLD}", persona="the_analyst")
    clock.t += DAY
    st.add("lesson A", f"eu:{CUR}", persona="the_analyst")
    b = st.add("lesson B", f"eu:{OLD}", persona="the_analyst")
    st.add("lesson B", "eu:someone-else")
    assert a.support == 2  # the same person counted twice
    touched = st.merge_source(f"eu:{OLD}", f"eu:{CUR}")
    assert sorted(touched) == sorted([a.id, b.id])
    assert a.support == 1 and a.days == 2  # once, and both days kept
    assert b.support == 2
    assert all(o.startswith(st._token(f"eu:{CUR}")) for o in a.occasions)
    # Erasing the current id now takes the old contributions with it.
    st.purge_source(f"eu:{CUR}")
    assert st.get(a.id) is None and st.get(b.id).support == 1


def test_merge_source_same_token_is_a_no_op():
    st = HypothesisStore()
    st.add("x", "eu:a")
    assert st.merge_source("eu:a", "eu:a") == []


def test_remove_and_set_personas():
    st = HypothesisStore()
    h = st.add("x", "eu:a", persona="the_empath")
    assert st.set_personas(h.id, {"the_analyst"}).personas == {"the_analyst"}
    assert st.set_personas("nope", set()) is None
    assert st.remove(h.id) is True and st.remove(h.id) is False
    st.add("x", "eu:b")  # the content key was released with it
    assert len(st.all()) == 1


def test_source_for_matches_the_sleep_pass(monkeypatch):
    monkeypatch.setenv("BRAIN_USER_ID", "org-owner")
    assert cross_learning.source_for("owner") == "org-owner"
    assert cross_learning.source_for(OLD) == f"eu:{OLD}"
    monkeypatch.delenv("BRAIN_USER_ID")
    assert cross_learning.source_for("owner") == "primary"


def test_edit_store_saves_only_on_change(tmp_path, monkeypatch):
    path = tmp_path / "h.json"
    monkeypatch.setattr(cross_learning, "_default_store_path", lambda: path)
    assert cross_learning.edit_store(lambda st: len(st.all())) == 0
    assert not path.exists()
    cross_learning.edit_store(lambda st: st.add("x", "eu:a"))
    assert path.exists() and len(cross_learning.load_store(path).all()) == 1


# ── API ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def store_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setattr(lm, "audit_log_path", lambda: tmp_path / "governance_audit.jsonl")
    from brain import org_settings

    monkeypatch.setattr(org_settings, "lesson_corroboration", lambda: "occasions")
    clock = _Clock()
    st = HypothesisStore(now_fn=clock)
    for _ in range(3):
        st.add("Analyst lesson", f"eu:{OLD}", persona="the_analyst")
        clock.t += DAY
    st.add("Empath lesson", "eu:latency-bench.0", persona="the_empath")
    st.add("Legacy lesson", "eu:x")
    path = tmp_path / "hypotheses.json"
    cross_learning.save_store(st, path)
    return path


def _client(runner=None):
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


OWN = {"Authorization": "Bearer ko"}
PARTNER = {"Authorization": "Bearer kp"}


def _id(c, principle):
    rows = c.get("/v1/org/hypotheses", headers=OWN).json()["lessons"]
    return next(r["id"] for r in rows if r["principle"] == principle)


def _events(kind):
    return [e for e in lm.tail_audit() if e["event"] == kind]


def test_list_is_owner_only_and_never_shows_contributors(store_path):
    c = _client()
    assert c.get("/v1/org/hypotheses", headers=PARTNER).status_code == 403
    body = c.get("/v1/org/hypotheses", headers=OWN).json()
    assert body["corroboration"] == "occasions" and body["count"] == 3
    top = body["lessons"][0]
    assert top["principle"] == "Analyst lesson" and top["status"] == "established"
    assert top["days"] == 3 and top["personas"] == ["the_analyst"]
    assert "contributors" not in top and "occasions" not in top
    only = c.get("/v1/org/hypotheses?persona=the_empath", headers=OWN).json()["lessons"]
    assert {r["principle"] for r in only} == {"Empath lesson", "Legacy lesson"}
    est = c.get("/v1/org/hypotheses?status=established", headers=OWN).json()
    assert est["count"] == 1
    assert c.get("/v1/org/hypotheses?status=maybe", headers=OWN).status_code == 400


def test_reattribute(store_path):
    c = _client()
    hid = _id(c, "Legacy lesson")
    assert (
        c.put(f"/v1/org/hypotheses/{hid}", headers=PARTNER, json={"personas": []}).status_code
        == 403
    )
    r = c.put(f"/v1/org/hypotheses/{hid}", headers=OWN, json={"personas": ["The Sage"]})
    assert r.status_code == 200 and r.json()["personas"] == ["the_sage"]
    assert (
        c.put(f"/v1/org/hypotheses/{hid}", headers=OWN, json={"personas": "x"}).status_code == 400
    )
    assert (
        c.put(f"/v1/org/hypotheses/{hid}", headers=OWN, json={"personas": ["a", "A"]}).status_code
        == 400
    )
    assert c.put("/v1/org/hypotheses/zzz", headers=OWN, json={"personas": []}).status_code == 400
    assert (
        c.put("/v1/org/hypotheses/0123456789ab", headers=OWN, json={"personas": []}).status_code
        == 404
    )
    ev = _events("hypothesis_reattributed")
    assert ev and ev[0]["from"] == [] and ev[0]["to"] == ["the_sage"]


def test_remove_one(store_path):
    c = _client()
    hid = _id(c, "Empath lesson")
    assert c.delete(f"/v1/org/hypotheses/{hid}", headers=PARTNER).status_code == 403
    assert c.delete(f"/v1/org/hypotheses/{hid}", headers=OWN).json() == {"ok": True, "id": hid}
    assert c.delete(f"/v1/org/hypotheses/{hid}", headers=OWN).status_code == 404
    assert _events("hypothesis_removed")[0]["principle"] == "Empath lesson"
    assert len(cross_learning.load_store(store_path).all()) == 2


def test_merge_and_forget_sources_accept_legacy_ids(store_path):
    c = _client()
    r = c.post("/v1/org/hypotheses/sources/merge", headers=OWN, json={"from": OLD, "to": CUR})
    assert r.status_code == 200 and len(r.json()["touched"]) == 1
    st = cross_learning.load_store(store_path)
    (h,) = [h for h in st.all() if h.principle == "Analyst lesson"]
    assert h.contributors == {st._token(f"eu:{CUR}")} and h.days == 3
    r = c.post(
        "/v1/org/hypotheses/sources/forget",
        headers=OWN,
        json={"end_user_id": "latency-bench.0"},
    )
    assert r.status_code == 200 and len(r.json()["retired"]) == 1
    assert (
        c.post(
            "/v1/org/hypotheses/sources/merge", headers=OWN, json={"from": "a b", "to": CUR}
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/v1/org/hypotheses/sources/merge", headers=OWN, json={"from": CUR, "to": CUR}
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/v1/org/hypotheses/sources/forget", headers=PARTNER, json={"end_user_id": "x"}
        ).status_code
        == 403
    )
    assert _events("hypothesis_sources_merged") and _events("hypothesis_source_forgotten")


def test_edits_run_through_the_runner_and_a_held_lock_is_409(store_path):
    calls = []

    async def runner(fn):
        calls.append(fn)
        return await asyncio.to_thread(cross_learning.edit_store, fn)

    c = _client(runner)
    assert c.get("/v1/org/hypotheses", headers=OWN).status_code == 200
    assert calls

    async def busy(fn):
        raise TimeoutError

    assert _client(busy).get("/v1/org/hypotheses", headers=OWN).status_code == 409


def test_brain_runner_waits_for_the_consolidation_lock(tmp_path, monkeypatch):
    from brain.session_turn import _TurnMixin

    monkeypatch.setattr(cross_learning, "_default_store_path", lambda: tmp_path / "h.json")

    class _B(_TurnMixin):
        pass

    async def go():
        b = _B()
        b._consolidation_lock = asyncio.Lock()
        assert await b.api_edit_hypotheses(lambda st: "ok") == "ok"
        await b._consolidation_lock.acquire()
        with pytest.raises(TimeoutError):
            await b.api_edit_hypotheses(lambda st: "late", timeout_s=0.05)
        b._consolidation_lock.release()

    asyncio.run(go())


def test_routes_are_registered_owner_only():
    from brain.api.reference import is_owner_route

    for m, p in (
        ("GET", "/v1/org/hypotheses"),
        ("PUT", "/v1/org/hypotheses/{hyp_id}"),
        ("DELETE", "/v1/org/hypotheses/{hyp_id}"),
        ("POST", "/v1/org/hypotheses/sources/merge"),
        ("POST", "/v1/org/hypotheses/sources/forget"),
    ):
        assert is_owner_route(m, p), (m, p)
