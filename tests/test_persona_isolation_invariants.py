"""The isolation invariant (plan §3.7 / §8).

Template T, clones A and B in an isolated org. Learning on A — the same writers a
turn drives (wiring save, learning ledger, chemistry, self.md rewrite, stories)
plus a full sleep consolidation bound to A — must leave B's audit fingerprint and
its self.md / wiring.json / learning_stories.jsonl byte-identical while A's
fingerprint changes. Repeated after a forced POST /v1/sessions/{A}/consolidate
through the real route (which binds the session's persona around the runner).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import org_settings, persona_audit, persona_chem, personas
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry
from brain.observability import learning_ledger
from brain.persona_key import persona_state_root
from brain.second_brain.store import bind_persona
from brain.settings import settings
from brain.sleep import SleepConsolidation


@pytest.fixture
def org(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.delenv("BRAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("BRAIN_PLACEMENT_FILE", raising=False)
    monkeypatch.setattr(persona_chem, "_PERSONAS_ROOT", tmp_path / "personas")
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    monkeypatch.setitem(settings._data, "cross_learning", 1)
    monkeypatch.setitem(settings._data, "enable_relationship_stage_progression", 0)
    monkeypatch.setitem(settings._data, "learning_narrator", 1)
    monkeypatch.setitem(settings._data, "sleep_group_by_persona", 1)
    monkeypatch.setitem(settings._data, "sleep_scan_all_personas", 0)
    monkeypatch.setitem(settings._data, "node_self_authoring", 1)
    personas.upsert(
        "tmpl", {"display_name": "Template", "disposition": "Steady.", "baseline": {"DA": 0.5}}
    )
    asyncio.run(personas.clone("tmpl", {"suffix": "a", "copy_agents": False, "seed": "default"}))
    asyncio.run(personas.clone("tmpl", {"suffix": "b", "copy_agents": False, "seed": "default"}))
    return tmp_path


A, B = "tmpl_a", "tmpl_b"


def _files(slug) -> dict[str, bytes | None]:
    root = persona_state_root(slug)
    out = {}
    for rel in ("schema/self.md", "wiring.json", "learning_stories.jsonl", "chemistry.json"):
        p = root / rel
        out[rel] = p.read_bytes() if p.is_file() else None
    return out


def _learn_on(slug: str) -> None:
    """What a turn + its learning leave behind, written through the real writers
    bound to `slug`."""
    with bind_persona(slug):
        learning_ledger.append(
            {
                "type": "hebbian",
                "session_id": "s1",
                "edges": [{"src": "x", "tgt": "y", "delta": 0.1}],
            }
        )
        persona_chem.save_current(slug, {"DA": 0.9}, {})
        from brain import ignition_tally

        ignition_tally.record("memory", slug)
        ignition_tally.flush(slug)
    root = persona_state_root(slug)
    (root / "wiring.json").write_text(json.dumps([{"src": "x", "tgt": "y", "w": 0.6, "pol": 1}]))
    self_md = root / "schema" / "self.md"
    self_md.write_text(
        self_md.read_text().replace("## History summary\n", "## History summary\n\nWe talked.\n")
    )


def _sleep_double():
    """A SleepConsolidation whose LLM cells are scripted; the passes that touch
    disk (stories) run for real, bounded by the batch."""
    s = SleepConsolidation.__new__(SleepConsolidation)
    s._router = MagicMock()
    s._router.embed = AsyncMock(return_value=None)
    s._hebbian = None
    s._episodic = MagicMock()
    for cell in ("_synthesizer", "_self_updater", "_personality_observer", "_learning_narrator"):
        c = MagicMock()
        c.reset_turn = MagicMock()
        c.call = AsyncMock(return_value="{}")
        setattr(s, cell, c)
    schema = MagicMock()
    schema.read = MagicMock(return_value="# Self\n\n## History summary\n")
    schema.ensure_speaker_schema = MagicMock(side_effect=lambda sp: f"user_{sp}.md")
    schema.aappend_fact = AsyncMock()
    schema.awrite = AsyncMock()
    s._schema = schema
    s.consolidate_thoughts = AsyncMock()
    s.angle_synonym_pass = AsyncMock()
    s.chunk_mining_pass = AsyncMock()
    s._node_architect = MagicMock()
    return s


def _trace(slug):
    return {
        "user_input": "hello",
        "entity_response": "hi",
        "speaker_name": "buyer_a",
        "persona": slug,
        "end_user_id": "buyer_a",
    }


def test_learning_on_a_never_touches_b(org, monkeypatch):
    from brain import cross_learning, node_authoring

    learn = AsyncMock()
    monkeypatch.setattr(cross_learning, "learn_from_private", learn)
    author = AsyncMock()
    monkeypatch.setattr(node_authoring, "author_and_admit", author)

    b0 = persona_audit.snapshot(B)
    b_files0 = _files(B)
    a0 = persona_audit.snapshot(A)
    assert a0["fingerprint"] != b0["fingerprint"]  # different slugs, same content → still distinct

    # "Turns" on A, then a sleep pass triggered under A with A's traces.
    _learn_on(A)
    s = _sleep_double()
    with bind_persona(A):
        asyncio.run(s.consolidate("s1", [_trace(A)], [], []))
    # The isolated-org gates held during the pass.
    learn.assert_not_called()
    author.assert_not_called()
    # The narrator's persist writer, bound to A, lands in A's file only.
    s._persist_stories([{"id": "st_1", "claim": "I learned x", "persona": A}], A)
    assert (persona_state_root(A) / "learning_stories.jsonl").is_file()
    assert not (persona_state_root(B) / "learning_stories.jsonl").exists()

    a1 = persona_audit.snapshot(A)
    b1 = persona_audit.snapshot(B)
    assert a1["fingerprint"] != a0["fingerprint"]
    assert b1["fingerprint"] == b0["fingerprint"]
    assert _files(B) == b_files0
    assert b1["ledgers"] == {"learning_ledger.jsonl": 0, "learning_stories.jsonl": 0}
    # Neither clone is in the DMN roster; the home persona is.
    assert a1["in_dmn_roster"] is False and b1["in_dmn_roster"] is False
    assert persona_audit.snapshot("home_p")["in_dmn_roster"] is True
    # hypotheses.json never appeared (cross-learning write is gated).
    assert not (persona_state_root("") / "hypotheses.json").exists()


def test_forced_consolidate_route_keeps_b_identical(org, monkeypatch):
    from brain import cross_learning, node_authoring

    monkeypatch.setattr(cross_learning, "learn_from_private", AsyncMock())
    monkeypatch.setattr(node_authoring, "author_and_admit", AsyncMock())
    import brain.agents as agents

    monkeypatch.setattr(agents, "resolve", lambda aid: tuple(aid.split(".", 1)))
    _learn_on(A)
    b0 = persona_audit.snapshot(B)
    b_files0 = _files(B)
    s = _sleep_double()
    bound: list[str] = []

    async def _consolidate(reason):
        from brain.second_brain.store import active_persona

        bound.append(active_persona())
        await s.consolidate("s2", [_trace(active_persona())], [], [])
        return {"ran": True, "reason": reason}

    def _resolver(h):
        return {"partner_id": None, "owner": True} if h == "Bearer ko" else None

    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: "sess_a"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
            consolidate_runner=_consolidate,
        )
    )
    c = TestClient(app)
    hdr = {"Authorization": "Bearer ko"}
    r = c.post(
        "/v1/sessions", json={"agent_id": f"{A}.companion", "end_user_id": "buyer_a"}, headers=hdr
    )
    assert r.status_code == 200
    r = c.post("/v1/sessions/sess_a/consolidate", json={"reason": "debate_end"}, headers=hdr)
    assert r.status_code == 200, r.text
    assert bound == [A]
    b1 = persona_audit.snapshot(B)
    assert b1["fingerprint"] == b0["fingerprint"]
    assert _files(B) == b_files0
    # And the purge of A leaves B untouched too, then re-cloning A yields a fresh fingerprint.
    from brain.session_turn import _TurnMixin

    class _Brain(_TurnMixin):
        persona_name = "home_p"

    a_before = persona_audit.snapshot(A)["fingerprint"]
    out = asyncio.run(_Brain().api_purge_persona(A))
    assert out["ok"] is True
    assert not persona_state_root(A).exists()
    assert persona_audit.snapshot(B)["fingerprint"] == b0["fingerprint"]
    asyncio.run(personas.clone("tmpl", {"suffix": "a", "copy_agents": False, "seed": "default"}))
    assert persona_audit.snapshot(A)["fingerprint"] != a_before
