"""
GET /v1/capabilities must describe the server it is actually running on.

Eight subsystems are optional and answer 501 when their runner was never wired. The
errors table told integrators to "feature-detect at startup" and gave them no
mechanism: the only way to find out was to call each endpoint and see, several of
which cost money or have side effects.

The load-bearing test here is the last one — it drives the real endpoints and asserts
each 501s exactly when its flag is false. Without it this endpoint would drift into a
confident lie, which is worse than not having it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.api import limits as _limits
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry

AUTH = {"Authorization": "Bearer k"}


class _FakeRunner:
    async def __call__(self, message, end_user_id, mandate_id=None, persona=None):
        return ("ok", {"emotion": "warm"})


def _resolver(authorization):
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return {"partner_id": "A", "owner": False}


def _client(**runners):
    app = FastAPI()
    app.include_router(
        build_api_router(
            _FakeRunner(),
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
            **runners,
        )
    )
    return TestClient(app)


def _caps(client):
    r = client.get("/v1/capabilities", headers=AUTH)
    assert r.status_code == 200
    return r.json()


def test_requires_a_key():
    assert _client().get("/v1/capabilities").status_code == 401


def test_reports_absent_subsystems_as_false():
    caps = _caps(_client())["capabilities"]
    for flag in ("grading", "consolidation", "extraction", "learning", "job_history", "erasure"):
        assert caps[flag] is False, flag


def test_reports_wired_subsystems_as_true():
    caps = _caps(
        _client(
            grade_runner=lambda *a, **k: {},
            consolidate_runner=lambda *a, **k: {},
            extract_runner=lambda *a, **k: {},
            learning_runner=lambda *a, **k: {},
            job_get_runner=lambda *a, **k: {},
            purge_runner=lambda *a, **k: {},
        )
    )["capabilities"]
    for flag in ("grading", "consolidation", "extraction", "learning", "job_history", "erasure"):
        assert caps[flag] is True, flag


def test_turns_are_always_available():
    """The turn runner is required, so this is the one flag that cannot be false."""
    assert _caps(_client())["capabilities"]["turns"] is True


def test_never_501s_itself():
    """Discovery has to work on the most stripped-down deployment there is."""
    assert _client().get("/v1/capabilities", headers=AUTH).status_code == 200


def test_audio_needs_a_provider_key_not_just_a_runner(monkeypatch):
    """TTS/STT answer 503 (not 501) without a provider key, so a flag that only
    checked the runner would promise a call that cannot succeed."""
    for var in ("ELEVENLABS_API_KEY", "OPENAI_API_KEY", "DEEPGRAM_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    caps = _caps(_client(tts_runner=lambda *a, **k: {}, stt_runner=lambda *a, **k: {}))
    assert caps["capabilities"]["tts"] is False
    assert caps["capabilities"]["stt"] is False

    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    caps = _caps(_client(tts_runner=lambda *a, **k: {}, stt_runner=lambda *a, **k: {}))
    assert caps["capabilities"]["tts"] is True


def test_limits_match_the_limits_module():
    """One source of truth for the enforced value, the reported value and the docs."""
    got = _caps(_client())["limits"]
    for key, value in _limits.as_dict().items():
        assert got[key] == value, key


def test_partner_never_sees_org_wide_spend():
    """Cloud spend aggregates every partner in the org, so it is as much another
    partner's data as this caller's."""
    limits = _caps(_client())["limits"]
    assert "spent_usd_today" not in limits.get("cloud", {})


def test_partner_is_told_it_gets_402_not_a_reroute():
    """A partner is metered against its own cap and always errors over budget — the
    silent local reroute is an owner-lane affordance the partner never sees."""
    cloud = _caps(_client())["limits"]["cloud"]  # _client resolves a partner key
    assert cloud["over_budget_falls_back_to_local"] is False


def test_owner_is_told_about_the_full_tier_fallback():
    """The owner lane on a full brain reroutes to local rather than failing — reported
    only to owners, since only they can observe it."""
    app = FastAPI()
    app.include_router(
        build_api_router(
            _FakeRunner(),
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: bool(h),
            resolver=lambda h: {"partner_id": None, "owner": True},
        )
    )
    cloud = TestClient(app).get("/v1/capabilities", headers=AUTH).json()["limits"]["cloud"]
    assert cloud["over_budget_falls_back_to_local"] is True


# ── the anti-drift test ─────────────────────────────────────────────────────
# (flag, method, path, body) — the endpoint that flag is claiming to describe.
PROBES = [
    ("grading", "POST", "/v1/sessions/sx/turns/t1/grade", {"grade": 1}),
    ("consolidation", "POST", "/v1/sessions/sx/consolidate", {}),
    ("extraction", "POST", "/v1/extract", {"input": "x", "schema": {"type": "object"}}),
    ("learning", "GET", "/v1/learning/summary", None),
    ("erasure", "DELETE", "/v1/end_users/u_1", None),
]


@pytest.mark.parametrize("flag,method,path,body", PROBES)
def test_a_false_flag_means_the_endpoint_really_501s(flag, method, path, body):
    client = _client()
    assert _caps(client)["capabilities"][flag] is False
    # Session-scoped probes need a real session, or they 404 before reaching the
    # capability check and the test would prove nothing.
    client.post("/v1/sessions", headers=AUTH, json={"end_user_id": "u_1"})
    r = client.request(method, path, headers=AUTH, json=body)
    # 501 is the contract. A 404 would mean the probe is wrong, not the flag.
    assert r.status_code == 501, f"{flag}: expected 501, got {r.status_code}"
