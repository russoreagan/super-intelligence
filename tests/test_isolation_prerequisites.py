"""The four prerequisites the structural investigation added (plan §0.4).

1. Same-slug double-serve guard: a persona promoted to its own instance is refused
   on the shared instance (409 / WS 1008); a pinned instance is never refused.
2. Durable chemistry for bound personas: a per-persona ClientChemRegistry cache,
   seeded from THAT persona's temperament, persisted under its state root,
   bounded LRU with flush-on-evict — a buyer's companion mood survives a restart.
3. The eval log is per org on a multi-tenant host (BRAIN_EVAL_LOG under the org
   root), not the repo-relative file every tenant process shared.
4. The /v1 lane refuses to spawn a brain for an org with no Anthropic vault key,
   like the UI catch-all.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import placement_client as pc
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry
from brain.bus import Bus
from brain.persona_key import persona_state_root
from brain.session_turn import _TurnMixin

# ── 1. double-serve guard ─────────────────────────────────────────────────────


@pytest.fixture
def placement(tmp_path, monkeypatch):
    f = tmp_path / ".placement.json"
    f.write_text(json.dumps({"promoted": ["ahab"]}))
    monkeypatch.setenv("BRAIN_PLACEMENT_FILE", str(f))
    monkeypatch.delenv("BRAIN_PERSONA_PINNED", raising=False)
    monkeypatch.setattr(pc, "_cached_at", 0.0)
    monkeypatch.setattr(pc, "_cached_mtime", -1.0)
    monkeypatch.setattr(pc, "_cached", set())
    return f


def test_guard_refuses_promoted_persona_on_shared_instance(placement, monkeypatch):
    assert pc.is_promoted_elsewhere("ahab") is True
    assert pc.is_promoted_elsewhere("Ahab") is True  # slugified
    assert pc.is_promoted_elsewhere("ishmael") is False
    assert pc.is_promoted_elsewhere("") is False and pc.is_promoted_elsewhere(None) is False
    with pytest.raises(pc.PersonaPromotedElsewhere, match="X-Brain-Persona: ahab"):
        pc.refuse_if_promoted("ahab")
    pc.refuse_if_promoted("ishmael")  # no raise
    # The dedicated instance itself is pinned and never refuses.
    monkeypatch.setenv("BRAIN_PERSONA_PINNED", "1")
    assert pc.is_promoted_elsewhere("ahab") is False


def test_guard_fails_open_without_a_placement_file(monkeypatch):
    monkeypatch.delenv("BRAIN_PLACEMENT_FILE", raising=False)
    monkeypatch.setattr(pc, "_cached_at", 0.0)
    assert pc.is_promoted_elsewhere("ahab") is False


def _resolver(h):
    return {"partner_id": None, "owner": True} if h == "Bearer ko" else None


def _api(runner=None, consolidate=None):
    calls: list = []

    async def _turn(message, end_user_id, mandate_id=None, persona=None):
        calls.append(persona)
        return "ok", {"emotion": "warm"}

    async def _cons(reason):
        return {"ran": True}

    ids = iter(["s_ahab", "s_ish"])
    app = FastAPI()
    app.include_router(
        build_api_router(
            runner or _turn,
            ApiSessionRegistry(id_fn=lambda: next(ids)),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
            consolidate_runner=consolidate or _cons,
        )
    )
    return TestClient(app), calls


def test_api_routes_answer_409_for_a_promoted_persona(placement, monkeypatch):
    import brain.agents as agents

    monkeypatch.setattr(agents, "resolve", lambda aid: tuple(aid.split(".", 1)))
    c, calls = _api()
    hdr = {"Authorization": "Bearer ko"}
    c.post("/v1/sessions", json={"agent_id": "ahab.companion", "end_user_id": "u1"}, headers=hdr)
    c.post("/v1/sessions", json={"agent_id": "ishmael.companion", "end_user_id": "u1"}, headers=hdr)
    r = c.post("/v1/sessions/s_ahab/turns", json={"message": "hi"}, headers=hdr)
    assert r.status_code == 409 and "dedicated instance" in r.json()["detail"]
    r = c.post("/v1/sessions/s_ahab/turns/stream", json={"message": "hi"}, headers=hdr)
    assert r.status_code == 409
    r = c.post("/v1/sessions/s_ahab/consolidate", json={}, headers=hdr)
    assert r.status_code == 409
    assert calls == []  # the runner was never bound to the promoted persona
    r = c.post("/v1/sessions/s_ish/turns", json={"message": "hi"}, headers=hdr)
    assert r.status_code == 200 and calls == ["ishmael"]


def test_turn_runner_exception_maps_to_409(placement, monkeypatch):
    import brain.agents as agents

    monkeypatch.setattr(agents, "resolve", lambda aid: tuple(aid.split(".", 1)))

    async def _runner(message, end_user_id, mandate_id=None, persona=None):
        pc.refuse_if_promoted("ahab")  # what process_turn does for defence in depth
        return "ok", {}

    c, _ = _api(runner=_runner)
    hdr = {"Authorization": "Bearer ko"}
    c.post("/v1/sessions", json={"agent_id": "ishmael.companion", "end_user_id": "u1"}, headers=hdr)
    r = c.post("/v1/sessions/s_ahab/turns", json={"message": "hi"}, headers=hdr)
    assert r.status_code == 409


# ── 2. durable chemistry for bound personas ───────────────────────────────────


class _Brain(_TurnMixin):
    def __init__(self):
        self.bus = Bus()
        self.persona_name = "home_p"


@pytest.fixture
def chem_fs(tmp_path, monkeypatch):
    from brain import persona_chem, personas

    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("BRAIN_PERSONA_CHEM_RESIDENT", raising=False)
    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path / "personas")
    personas.upsert("ahab", {"display_name": "Ahab", "disposition": "x", "baseline": {"DA": 0.7}})
    personas.upsert("ishmael", {"display_name": "I", "disposition": "y", "baseline": {"DA": 0.2}})
    return tmp_path


def test_bound_persona_pair_seeds_from_its_own_temperament(chem_fs):
    b = _Brain()
    home_slot = b.bus._chem_registry if hasattr(b.bus, "_chem_registry") else None
    pair_a = b._persona_chem_pair("ahab", "buyer1")
    pair_i = b._persona_chem_pair("ishmael", "buyer2")
    assert pair_a.snapshot()["neuromod"]["DA"] == pytest.approx(0.7, abs=1e-6)
    assert pair_i.snapshot()["neuromod"]["DA"] == pytest.approx(0.2, abs=1e-6)
    # Same (persona, customer) → the same live pair; the cache is keyed by persona.
    assert b._persona_chem_pair("ahab", "buyer1") is pair_a
    assert set(b._persona_chem) == {"ahab", "ishmael"}
    # A bound persona's registry never takes the bus's home-registry slot.
    assert getattr(b.bus, "_chem_registry", None) is home_slot


def test_bound_persona_mood_survives_a_restart(chem_fs):
    b = _Brain()
    reg = b._persona_chem_registry("ahab")
    pair = reg.get_or_create("buyer1")
    pair.neuromod.restore({"DA": 0.95})
    reg.persist("buyer1", force=True)
    files = list((persona_state_root("ahab") / "client_chem").glob("*.json"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text())
    assert rec["key"] == "ahab:buyer1"
    # New process: the customer's mood is restored (no absence elapsed → same DA).
    b2 = _Brain()
    restored = b2._persona_chem_pair("ahab", "buyer1")
    assert restored.snapshot()["neuromod"]["DA"] == pytest.approx(0.95, abs=1e-6)
    # Nothing landed under the home persona's root.
    assert not (persona_state_root("") / "client_chem").exists()


def test_registry_cache_is_bounded_and_flushes_on_evict(chem_fs, monkeypatch):
    monkeypatch.setenv("BRAIN_PERSONA_CHEM_RESIDENT", "1")
    b = _Brain()
    reg_a = b._persona_chem_registry("ahab")
    reg_a.get_or_create("buyer1").neuromod.restore({"DA": 0.9})
    b._persona_chem_registry("ishmael")  # evicts ahab → flushed
    assert set(b._persona_chem) == {"ishmael"}
    assert list((persona_state_root("ahab") / "client_chem").glob("*.json"))
    assert b.flush_persona_chem() == 1


def test_purge_eviction_reaches_the_registry_cache(chem_fs):
    b = _Brain()
    b._persona_chem_registry("ahab")
    b._persona_chem_registry("ishmael")
    assert b._evict_persona_chem("ahab") == 1
    assert set(b._persona_chem) == {"ishmael"}


# ── 3. eval log per org ───────────────────────────────────────────────────────


@pytest.fixture
def clean_environ():
    """_route_persona_state writes os.environ directly (SECOND_BRAIN_PATH, wiring
    paths, BRAIN_PERSONA_NAME); restore the whole environment afterwards so later
    tests are not routed into this test's tmp tree."""
    import os

    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def test_multitenant_boot_routes_eval_log_under_the_org_root(tmp_path, monkeypatch, clean_environ):
    import brain.run as brun

    org_root = tmp_path / "org-1" / "second_brain"
    org_root.mkdir(parents=True)
    settings_path = tmp_path / "org-1" / "settings.json"
    settings_path.write_text(json.dumps({"persona_name": "the_analyst"}))
    monkeypatch.setenv("BRAIN_MULTITENANT", "1")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(org_root))
    monkeypatch.setenv("BRAIN_SETTINGS_PATH", str(settings_path))
    monkeypatch.delenv("BRAIN_EVAL_LOG", raising=False)
    brun._route_persona_state()
    import os

    assert os.environ["BRAIN_EVAL_LOG"] == str(org_root / "eval" / "turns.jsonl")
    from eval.turn_logger import log_path

    assert log_path() == org_root / "eval" / "turns.jsonl"
    # An explicit path from the provisioner wins.
    monkeypatch.setenv("BRAIN_EVAL_LOG", str(tmp_path / "explicit.jsonl"))
    brun._route_persona_state()
    assert os.environ["BRAIN_EVAL_LOG"] == str(tmp_path / "explicit.jsonl")


# ── 4. /v1 lane anthropic gate ────────────────────────────────────────────────


def test_v1_refuses_to_spawn_without_an_anthropic_key(monkeypatch):
    import brain.api.auth as api_auth
    from brain.gateway import server as gw
    from tests.test_gateway_api_routing import _FakeProv, _FakeRunpod

    ctx = {"org_id": "org-1", "partner_id": "p", "role": "owner"}
    monkeypatch.setattr(api_auth, "resolve_key_context", lambda _auth: ctx)
    monkeypatch.setenv("BRAIN_TIER", "full")

    async def _no_key(_org):
        return False

    monkeypatch.setattr(gw, "_org_has_anthropic", _no_key)
    prov = _FakeProv(status=None)
    runpod = _FakeRunpod()
    app = gw.build_gateway_app(prov, [runpod])

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/v1/sessions", headers={"authorization": "Bearer good"}, json={})
        for _ in range(5):
            await asyncio.sleep(0)
        return r

    r = asyncio.run(run())
    assert r.status_code == 403
    assert r.json()["error"] == "no_anthropic_key"
    assert prov.ensured == [] and runpod.ensured is False


def test_org_has_anthropic_reads_the_vault_by_org_and_fails_closed(monkeypatch):
    import brain.vault as vault
    from brain.gateway import server as gw

    monkeypatch.setattr(
        vault, "fetch_user_keys", lambda uid: {"anthropic": "sk"} if uid == "org-1" else {}
    )
    assert asyncio.run(gw._org_has_anthropic("org-1")) is True
    assert asyncio.run(gw._org_has_anthropic("org-2")) is False

    def _boom(uid):
        raise RuntimeError("down")

    monkeypatch.setattr(vault, "fetch_user_keys", _boom)
    assert asyncio.run(gw._org_has_anthropic("org-1")) is False
