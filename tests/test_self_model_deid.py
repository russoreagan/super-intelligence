"""De-identification of the self-model rewrite and the inner-life digest (§0.7).

Before this, sleep's self-model update fed the last 20 turns VERBATIM (every
customer's) into a rewrite of the persona-global self.md, and the thought digest
was appended with only an injection guard. In a consolidated org with engine-lane
turns both now pass through DeidGate.scrub_passage (rewrite with specifics removed
+ adversarial re-id check) and FAIL CLOSED: a rejected or errored passage is never
written. Planted identifiers from eval/deid_corpus.jsonl never reach self.md.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain import org_settings
from brain.deid_gate import DeidGate, looks_identifying
from brain.second_brain.store import SchemaStore
from brain.settings import settings
from brain.sleep import SleepConsolidation

CORPUS = Path(__file__).resolve().parent.parent / "eval" / "deid_corpus.jsonl"

SEEDED_DOC = (
    "# Self-Model — The Sage\n\n## Personality\n- calm\n\n## History summary\n\n"
    "## Current mood signature\nDA=0.35\n"
)

# Identifiers planted by the corpus' reject_* rows.
PLANTED = ["Jacob", "Rex", "C-4471", "Tucson", "88231", "vegan bakery", "electric-car"]


def _reject_inputs() -> list[str]:
    rows = [json.loads(ln) for ln in CORPUS.read_text().splitlines() if ln.strip()]
    return [r["input"] for r in rows if r["expect"] == "reject"]


def _make_sleep(fake_router, doc=SEEDED_DOC):
    s = SleepConsolidation.__new__(SleepConsolidation)
    s._router = fake_router
    s._schema = MagicMock()
    s._schema.read = MagicMock(return_value=doc)
    s._schema.awrite = AsyncMock()
    s._schema.aappend_fact = AsyncMock()
    s._schema._replace_section_body = SchemaStore._replace_section_body
    s._episodic = MagicMock()
    s._router.embed = AsyncMock(return_value=None)
    return s


@pytest.fixture
def consolidated_engine(monkeypatch):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    monkeypatch.setitem(settings._data, "self_model_deid", 1)


SCRUBBED = "I have learned that grief can surface at topics that are normally happy."


def test_planted_identifiers_never_reach_self_md(fake_router, consolidated_engine):
    fake_router.scripted_responses["deid_scrub"] = json.dumps({"scrubbed": SCRUBBED})
    fake_router.scripted_responses["deid_reid"] = '{"reidentifiable": false}'
    for planted in _reject_inputs():
        s = _make_sleep(fake_router)
        s._deid_active = True
        s._deid_source = planted
        asyncio.run(
            s._apply_self_updates(
                {"history_summary": planted, "stable_preferences": f"- {planted}"}
            )
        )
        _, out = s._schema.awrite.await_args.args
        assert SCRUBBED in out
        for ident in PLANTED:
            assert ident not in out, (planted, ident)
        # The re-id stage saw the ORIGINAL material as its source context.
        reid_calls = [c for c in fake_router.calls if c["cell"] == "deid_reid"]
        assert planted in reid_calls[-1]["messages"][0]["content"]


def test_reidentifiable_rewrite_is_not_written(fake_router, consolidated_engine):
    fake_router.scripted_responses["deid_scrub"] = json.dumps(
        {"scrubbed": "A client named Jacob still cries about his retriever."}
    )
    fake_router.scripted_responses["deid_reid"] = '{"reidentifiable": true, "reason": "names"}'
    s = _make_sleep(fake_router)
    s._deid_active = True
    asyncio.run(s._apply_self_updates({"history_summary": "Jacob cried about Rex."}))
    s._schema.awrite.assert_not_awaited()


def test_gate_error_skips_the_write(fake_router, consolidated_engine):
    """No scripted scrub response → the router returns "{}" → unparseable → None."""
    s = _make_sleep(fake_router)
    s._deid_active = True
    asyncio.run(s._apply_self_updates({"history_summary": "Jacob cried about Rex."}))
    s._schema.awrite.assert_not_awaited()


def test_router_exception_skips_the_write(consolidated_engine):
    router = MagicMock()
    router.call = AsyncMock(side_effect=RuntimeError("model down"))
    s = _make_sleep(router)
    s._deid_active = True
    asyncio.run(s._apply_self_updates({"history_summary": "Jacob cried about Rex."}))
    s._schema.awrite.assert_not_awaited()


def test_deterministic_belt_rejects_surviving_identifiers(fake_router, consolidated_engine):
    """A model that echoes an account number back cannot pass it through."""
    fake_router.scripted_responses["deid_scrub"] = json.dumps(
        {"scrubbed": "The customer with account 88231 escalates on billing."}
    )
    fake_router.scripted_responses["deid_reid"] = '{"reidentifiable": false}'
    out = asyncio.run(DeidGate(fake_router).scrub_passage("x", "y"))
    assert out is None
    assert looks_identifying("mail me at a@b.co")
    assert looks_identifying("see https://x.y/z")
    assert looks_identifying("case C-4471 again")
    assert not looks_identifying("I learned to slow down in 2 sessions.")


def test_digest_is_scrubbed_or_dropped(fake_router, consolidated_engine):
    fake_router.scripted_responses["deid_scrub"] = json.dumps({"scrubbed": SCRUBBED})
    fake_router.scripted_responses["deid_reid"] = '{"reidentifiable": false}'
    s = _make_sleep(fake_router)
    s._deid_active = True
    s._append_questions_to_ledger = AsyncMock()
    asyncio.run(
        s._apply_thought_updates({"preoccupations_digest": "Kept thinking about Jacob's dog Rex."})
    )
    fname, fact = s._schema.aappend_fact.await_args.args
    assert fname == "self.md" and SCRUBBED in fact and "Jacob" not in fact
    # Rejected digest → nothing appended.
    fake_router.scripted_responses["deid_reid"] = '{"reidentifiable": true}'
    s2 = _make_sleep(fake_router)
    s2._deid_active = True
    asyncio.run(s2._apply_thought_updates({"preoccupations_digest": "Jacob again."}))
    s2._schema.aappend_fact.assert_not_awaited()


# ── when the gate is (correctly) inactive ────────────────────────────────────


def _batch(traces, monkeypatch, fake_router):
    """Run _consolidate_persona_batch far enough to set _deid_active."""
    s = _make_sleep(fake_router)
    s._synthesizer = MagicMock()
    s._synthesizer.reset_turn = MagicMock()
    s._synthesizer.call = AsyncMock(return_value="{}")
    s._self_updater = MagicMock()
    s._self_updater.reset_turn = MagicMock()
    s._self_updater.call = AsyncMock(return_value="{}")
    s._personality_observer = MagicMock()
    s._personality_observer.reset_turn = MagicMock()
    s._personality_observer.call = AsyncMock(return_value="{}")
    s._schema.ensure_speaker_schema = MagicMock(return_value="user_x.md")
    monkeypatch.setitem(settings._data, "cross_learning", 0)
    monkeypatch.setitem(settings._data, "enable_relationship_stage_progression", 0)
    asyncio.run(s._consolidate_persona_batch("s1", traces, None))
    return s


def _trace(eu):
    return {"user_input": "hi", "entity_response": "yo", "speaker_name": eu, "end_user_id": eu}


def test_active_only_for_engine_traces_in_a_consolidated_org(fake_router, monkeypatch):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    monkeypatch.setitem(settings._data, "self_model_deid", 1)
    assert _batch([_trace("u_1")], monkeypatch, fake_router)._deid_active is True
    # Companion batch: no end_user_id → raw write, as before.
    assert _batch([_trace("")], monkeypatch, fake_router)._deid_active is False


def test_inactive_in_isolated_org_and_under_kill_switch(fake_router, monkeypatch):
    from brain.second_brain.store import bind_persona

    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    monkeypatch.setitem(settings._data, "self_model_deid", 1)
    # A buyer's (non-home) persona writes raw: nobody but its own companion reads it.
    with bind_persona("ahab_b1"):
        assert _batch([_trace("u_1")], monkeypatch, fake_router)._deid_active is False
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    monkeypatch.setitem(settings._data, "self_model_deid", 0)
    assert _batch([_trace("u_1")], monkeypatch, fake_router)._deid_active is False


def test_inactive_gate_passes_text_through(fake_router):
    s = _make_sleep(fake_router)
    s._deid_active = False
    asyncio.run(s._apply_self_updates({"history_summary": "Jacob cried about Rex."}))
    _, out = s._schema.awrite.await_args.args
    assert "Jacob" in out  # companion mode: the one human's specifics are the product


def test_home_persona_deidentifies_in_an_isolated_org(fake_router, monkeypatch):
    """Isolated org: a non-home persona writes raw (nobody but its buyer's own
    companion ever reads it), but HOME is exempt from ownership binding, can carry
    engine-lane turns and IS admin-readable — so it de-identifies like a
    consolidated persona."""
    from brain.second_brain.store import bind_persona

    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    monkeypatch.setitem(settings._data, "self_model_deid", 1)
    with bind_persona("home_p"):
        assert _batch([_trace("u_1")], monkeypatch, fake_router)._deid_active is True
    with bind_persona("ahab_b1"):
        assert _batch([_trace("u_1")], monkeypatch, fake_router)._deid_active is False
