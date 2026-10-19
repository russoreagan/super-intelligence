"""Lessons from an org's own traffic can become established.

2026-10-19 prod audit: cross-learning admitted ~25 de-identified principles in the
owner's org and established none. Promotion needed 3 DISTINCT sources, and nearly all
of that org's traffic is one person arriving as one end user through their own app, so
every lesson stayed at support 1 and never reached a turn. An org can now declare its
traffic its own (organizations.lesson_corroboration = 'occasions', migration 048):
there a lesson also promotes once it has been re-derived on cross_learning_promote_days
distinct UTC days. Established lessons reach only the persona that learned them.
"""

from __future__ import annotations

import json

import pytest

from brain import cross_learning
from brain import org_settings as os_
from brain.hypothesis_store import OCCASIONS, PEOPLE, HypothesisStore
from brain.session_turn import _TurnMixin
from tests.test_org_learning_mode import _FakeSb, _resolver

DAY = 86400.0
LESSON = "When evaluating a prediction, state its time horizon explicitly."


class _Clock:
    def __init__(self, t: float = 1_760_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _store(mode: str, clock: _Clock, days: int = 3) -> HypothesisStore:
    return HypothesisStore(salt="t", now_fn=clock, corroboration=mode, promote_days=days)


# ── the store ────────────────────────────────────────────────────────────────


def test_people_mode_ignores_days():
    clock = _Clock()
    st = _store(PEOPLE, clock)
    for _ in range(5):
        st.add(LESSON, "eu:russ", persona="the_analyst")
        clock.t += DAY
    (h,) = st.all()
    assert h.days == 5 and h.support == 1
    assert st.established() == [] and st.status_of(h) == "provisional"


def test_occasions_mode_promotes_on_distinct_days():
    clock = _Clock()
    st = _store(OCCASIONS, clock)
    st.add(LESSON, "eu:russ")
    clock.t += DAY
    st.add(LESSON, "eu:russ")
    assert st.established() == []
    clock.t += DAY
    st.add(LESSON, "eu:russ")
    assert [h.principle for h in st.established()] == [LESSON]


def test_same_day_replays_do_not_corroborate():
    """A consolidation that replays one batch seven times in a day (seen in prod
    2026-10-11) is one occasion, not seven."""
    clock = _Clock()
    st = _store(OCCASIONS, clock)
    for _ in range(7):
        st.add(LESSON, "eu:russ")
        clock.t += 60
    (h,) = st.all()
    assert h.days == 1 and st.established() == []


def test_distinct_people_still_promote_in_occasions_mode():
    clock = _Clock()
    st = _store(OCCASIONS, clock)
    for who in ("eu:a", "eu:b", "eu:c"):
        st.add(LESSON, who)
    assert len(st.established()) == 1


def test_promote_days_is_configurable():
    clock = _Clock()
    st = _store(OCCASIONS, clock, days=2)
    st.add(LESSON, "primary")
    clock.t += DAY
    st.add(LESSON, "primary")
    assert len(st.established()) == 1


def test_erasure_removes_the_sources_occasions():
    clock = _Clock()
    st = _store(OCCASIONS, clock)
    for who in ("eu:russ", "eu:other", "eu:russ"):
        st.add(LESSON, who)
        clock.t += DAY
    (h,) = st.all()
    assert h.days == 3 and len(st.established()) == 1
    st.purge_source("eu:russ")
    assert h.days == 1 and st.established() == []
    assert all(not o.startswith(st._token("eu:russ")) for o in h.occasions)


def test_occasions_round_trip_and_mode_is_not_persisted():
    clock = _Clock()
    st = _store(OCCASIONS, clock)
    for _ in range(3):
        st.add(LESSON, "eu:russ", persona="the_analyst")
        clock.t += DAY
    data = json.loads(json.dumps(st.to_dict()))
    assert "corroboration" not in data
    assert len(HypothesisStore.from_dict(data, corroboration=OCCASIONS).established()) == 1
    assert HypothesisStore.from_dict(data).established() == []  # default rule: people


def test_legacy_single_source_record_is_backfilled_from_its_sightings():
    t0 = 1_760_000_000.0
    legacy = {
        "promote_k": 3,
        "hypotheses": [
            {
                "id": "a",
                "principle": "one",
                "contributors": ["tok"],
                "first_seen": t0,
                "last_seen": t0 + 4 * DAY,
                "personas": ["the_analyst"],
            },
            {
                "id": "b",
                "principle": "two",
                "contributors": ["t1", "t2"],
                "first_seen": t0,
                "last_seen": t0 + 4 * DAY,
                "personas": [],
            },
            {
                "id": "c",
                "principle": "three",
                "contributors": ["tok"],
                "first_seen": 0.0,
                "last_seen": 0.0,
            },
        ],
    }
    st = HypothesisStore.from_dict(legacy, corroboration=OCCASIONS)
    assert st.get("a").days == 2  # two honest occasions; the next re-derivation promotes
    assert st.get("b").days == 0  # several sources: nothing guessed
    assert st.get("c").days == 0  # no timestamps: nothing invented
    assert st.established() == []


# ── load_store reads the org's rule ──────────────────────────────────────────


def test_load_store_applies_the_org_rule(monkeypatch, tmp_path):
    clock = _Clock()
    st = _store(PEOPLE, clock)
    for _ in range(3):
        st.add(LESSON, "eu:russ", persona="the_analyst")
        clock.t += DAY
    path = tmp_path / "hypotheses.json"
    cross_learning.save_store(st, path)
    monkeypatch.setattr(os_, "lesson_corroboration", lambda: "people")
    assert cross_learning.established_principles(path=path) == []
    monkeypatch.setattr(os_, "lesson_corroboration", lambda: "occasions")
    assert cross_learning.established_principles(path=path) == [LESSON]


def test_load_store_fails_to_people_when_the_org_row_is_unreadable(monkeypatch, tmp_path):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(os_, "lesson_corroboration", _boom)
    assert cross_learning.load_store(tmp_path / "none.json").corroboration == PEOPLE


# ── injection is persona-scoped ──────────────────────────────────────────────


def _three_day_store(path):
    clock = _Clock()
    st = _store(OCCASIONS, clock)
    for _ in range(3):
        st.add(LESSON, "eu:russ", persona="the_analyst")
        st.add("Name the feeling before offering a fix.", "eu:russ", persona="the_empath")
        st.add("A legacy lesson with no persona.", "eu:russ")
        clock.t += DAY
    cross_learning.save_store(st, path)


def test_established_principles_scopes_to_persona(monkeypatch, tmp_path):
    path = tmp_path / "hypotheses.json"
    _three_day_store(path)
    monkeypatch.setattr(os_, "lesson_corroboration", lambda: "occasions")
    got = cross_learning.established_principles(n=5, path=path, persona="the_analyst")
    assert LESSON in got and "A legacy lesson with no persona." in got
    assert "Name the feeling before offering a fix." not in got
    assert len(cross_learning.established_principles(n=5, path=path)) == 3  # None = all


class _Brain(_TurnMixin):
    pass


def test_turn_injection_reloads_when_the_bound_persona_changes(monkeypatch):
    from brain import org_settings, persona_key

    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    seen: list[str | None] = []

    def _principles(n=3, path=None, persona=None):
        seen.append(persona)
        return [f"lesson for {persona}"]

    monkeypatch.setattr(cross_learning, "established_principles", _principles)
    who = {"p": "the_analyst"}
    monkeypatch.setattr(persona_key, "active_or_home_persona", lambda: who["p"])
    b = _Brain()
    assert b._established_principles_for_turn() == ["lesson for the_analyst"]
    assert b._established_principles_for_turn() == ["lesson for the_analyst"]  # cached
    who["p"] = "the_empath"
    assert b._established_principles_for_turn() == ["lesson for the_empath"]
    assert seen == ["the_analyst", "the_empath"]


# ── the org setting and its API ──────────────────────────────────────────────


@pytest.fixture
def sb(monkeypatch, tmp_path):
    from brain.second_brain import supabase_client

    fake = _FakeSb({"id": "org-1", "learning_mode": "consolidated", "instance_seed": "default"})
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: fake)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    os_.invalidate()
    yield fake
    os_.invalidate()


def test_pre_migration_row_reads_as_people(sb):
    assert os_.lesson_corroboration() == "people"
    sb.row["lesson_corroboration"] = "nonsense"
    os_.invalidate()
    assert os_.lesson_corroboration() == "people"


def test_set_lesson_corroboration_writes_and_validates(sb):
    assert os_.set_lesson_corroboration("Occasions") == "occasions"
    assert sb.updates[-1] == {"lesson_corroboration": "occasions"}
    with pytest.raises(os_.OrgSettingsError, match="must be one of"):
        os_.set_lesson_corroboration("everyone")
    sb.fail_updates = True
    with pytest.raises(os_.OrgSettingsError, match="048"):
        os_.set_lesson_corroboration("people")


@pytest.fixture
def client(sb, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from brain.api.server import build_api_router
    from brain.api.sessions import ApiSessionRegistry
    from brain.settings import settings

    monkeypatch.setattr(settings, "save", lambda patch=None: settings.update(patch or {}))
    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
        )
    )
    return TestClient(app)


def test_put_org_permissions_sets_it_owner_only_and_audited(client, sb):
    from brain import learning_mode as lm

    r = client.get("/v1/org/permissions", headers={"Authorization": "Bearer kp"})
    assert r.json()["lesson_corroboration"] == "people"
    r = client.put(
        "/v1/org/permissions",
        headers={"Authorization": "Bearer kp"},
        json={"lesson_corroboration": "occasions"},
    )
    assert r.status_code == 403
    hdr = {"Authorization": "Bearer ko"}
    r = client.put("/v1/org/permissions", headers=hdr, json={"lesson_corroboration": "always"})
    assert r.status_code == 400
    r = client.put("/v1/org/permissions", headers=hdr, json={"lesson_corroboration": 1})
    assert r.status_code == 400
    r = client.put("/v1/org/permissions", headers=hdr, json={"lesson_corroboration": "occasions"})
    assert r.status_code == 200, r.text
    assert r.json()["lesson_corroboration"] == "occasions"
    events = [e for e in lm.tail_audit() if e["event"] == "lesson_corroboration_changed"]
    assert events and events[0]["from"] == "people" and events[0]["to"] == "occasions"
    sb.fail_updates = True
    r = client.put("/v1/org/permissions", headers=hdr, json={"lesson_corroboration": "people"})
    assert r.status_code == 503


def test_settings_default_declared():
    from brain.settings import DEFAULTS

    assert DEFAULTS["cross_learning_promote_days"] == 3
