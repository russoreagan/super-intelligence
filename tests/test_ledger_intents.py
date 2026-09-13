"""
B5 — conversational ledger intents:
  - detect_manual_project / classify_confirmation (pure)
  - DMN.add_manual_project appends to the Projects section + refreshes context
  - DMN.process_user_message_for_ledger resolves pending conclusions
    (affirm → memory + retire; reject → drop; correct → re-open)
"""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

import brain.open_threads as ot
from brain.clusters import ledger_intents as li
from brain.dmn import DefaultModeNetwork
from brain.sequence_predictor import SequencePredictor

# ── pure detector ────────────────────────────────────────────────────────────


def test_detect_manual_project_explicit_phrases():
    assert li.detect_manual_project("work on the engine API review")["task"].startswith(
        "the engine API review"
    )
    assert li.detect_manual_project("new project: profile the audio pipeline") is not None
    assert li.detect_manual_project("add this to your open threads: investigate decay") is not None
    assert li.detect_manual_project("I want you to review the Evolution App") is not None


def test_detect_manual_project_ignores_casual_mentions():
    assert li.detect_manual_project("I did some work on my car today") is None
    assert li.detect_manual_project("how does the prefetcher work?") is None
    assert li.detect_manual_project("") is None


def test_classify_confirmation():
    assert li.classify_confirmation("yes, exactly right") == "affirm"
    assert li.classify_confirmation("no, that's not quite it") == "reject"
    assert li.classify_confirmation("actually it's more about latency than cost") == "correct"


# ── DMN integration ───────────────────────────────────────────────────────────


def _make_dmn():
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._seq_predictor = SequencePredictor()
    dmn._router = MagicMock()
    dmn._router.embed = AsyncMock(return_value=None)
    hip = MagicMock()
    hip.encode_conclusion = AsyncMock()
    schema = MagicMock()
    schema.read = MagicMock(
        return_value="# Open Questions & Projects\n\n## Assigned projects\n### Existing\n- **Task**: stuff\n"
    )
    schema.awrite = AsyncMock()
    schema.upsert_section = AsyncMock()
    hip._schema = schema
    dmn._hippocampus = hip
    dmn._session_id = "test"
    dmn._open_threads = []
    dmn._recent_conclusions = deque(maxlen=5)
    dmn._last_projects = ""
    return dmn


@pytest.mark.asyncio
async def test_add_manual_project_writes_the_table_and_refreshes(monkeypatch):
    from brain import agent_projects_store as store

    monkeypatch.setenv("BRAIN_STORAGE_BACKEND", "local")
    store.invalidate_cache()
    dmn = _make_dmn()
    evt = await dmn.process_user_message_for_ledger("work on the engine API review")
    assert evt["action"] == "project_added"
    rows = store.list_for_personas(dmn._project_personas())
    assert any("engine API" in r["title"] for r in rows)
    # The user asked for it in conversation → they are waiting on it.
    assert all(r["user_waiting"] for r in rows if "engine API" in r["title"])
    # Digest rebuilt from the table.
    assert "engine API" in dmn._last_projects


@pytest.mark.asyncio
async def test_affirm_pending_conclusion_commits_and_retires():
    dmn = _make_dmn()
    dmn._open_threads, t = ot.open_thread([], "is gating cheaper?", bears_on=["efficiency"])
    dmn._open_threads, t = ot.mark_pending(dmn._open_threads, t.id)
    t.pending_conclusion = "Gating reduces tokens-per-response."
    evt = await dmn.process_user_message_for_ledger("yes, that's right")
    await asyncio.sleep(0.05)
    assert evt["action"] == "conclusion_confirmed"
    assert ot.find(dmn._open_threads, t.id) is None
    dmn._hippocampus.encode_conclusion.assert_awaited()
    assert dmn._hippocampus.encode_conclusion.await_args.kwargs["source"] == "confirmed"


@pytest.mark.asyncio
async def test_reject_pending_conclusion_drops_thread():
    dmn = _make_dmn()
    dmn._open_threads, t = ot.open_thread([], "is gating cheaper?")
    dmn._open_threads, t = ot.mark_pending(dmn._open_threads, t.id)
    evt = await dmn.process_user_message_for_ledger("no, not really")
    assert evt["action"] == "conclusion_rejected"
    assert ot.find(dmn._open_threads, t.id) is None
    dmn._hippocampus.encode_conclusion.assert_not_awaited()


@pytest.mark.asyncio
async def test_correct_pending_conclusion_reopens_with_advance():
    dmn = _make_dmn()
    dmn._open_threads, t = ot.open_thread([], "is gating cheaper?")
    dmn._open_threads, t = ot.mark_pending(dmn._open_threads, t.id)
    evt = await dmn.process_user_message_for_ledger(
        "it's more about reducing hallucination than token cost"
    )
    assert evt["action"] == "conclusion_corrected"
    reopened = ot.find(dmn._open_threads, t.id)
    assert reopened is not None
    assert reopened.status == ot.STATUS_OPEN
    assert reopened.advances == 1
    assert "user correction" in reopened.progress[-1]


@pytest.mark.asyncio
async def test_project_assignment_is_refused_on_answer_only_turns(monkeypatch):
    """A project pre-authorizes background work; an answer-only turn means none."""
    from brain import agent_projects_store as store

    monkeypatch.setenv("BRAIN_STORAGE_BACKEND", "local")
    store.invalidate_cache()
    dmn = _make_dmn()
    dmn.add_manual_project = AsyncMock()
    evt = await dmn.process_user_message_for_ledger(
        "work on the engine API review", answer_only=True
    )
    assert evt is None
    dmn.add_manual_project.assert_not_called()


@pytest.mark.asyncio
async def test_project_assignment_is_an_owner_act_not_an_end_user_one(monkeypatch):
    """A partner's end user talking to an agent over the API never assigns it
    projects; the owner UI still can. Confirming a conclusion stays open to both."""
    from brain.turn_ctx import bind_turn

    dmn = _make_dmn()
    dmn.add_manual_project = AsyncMock(return_value=True)
    with bind_turn(channel="agent", session_id="s1", end_user_id="u_1"):
        assert await dmn.process_user_message_for_ledger("work on the engine API review") is None
    dmn.add_manual_project.assert_not_called()
    with bind_turn(channel="owner", session_id="s2"):
        evt = await dmn.process_user_message_for_ledger("work on the engine API review")
    assert evt["action"] == "project_added"
    dmn.add_manual_project.assert_called_once()
