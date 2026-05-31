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


# ── pure detector ────────────────────────────────────────────────────────────


def test_detect_manual_project_explicit_phrases():
    assert li.detect_manual_project("work on the Karaoke Hero review")["task"].startswith(
        "the Karaoke Hero review"
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
    dmn._router = MagicMock()
    dmn._router.embed = AsyncMock(return_value=None)
    hip = MagicMock()
    hip.encode_conclusion = AsyncMock()
    schema = MagicMock()
    schema.read = MagicMock(return_value="# Open Questions & Projects\n\n## Projects assigned by Russ\n### Existing\n- **Task**: stuff\n")
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
async def test_add_manual_project_appends_and_refreshes():
    dmn = _make_dmn()
    evt = await dmn.process_user_message_for_ledger("work on the Karaoke Hero review")
    assert evt["action"] == "project_added"
    dmn._hippocampus._schema.awrite.assert_awaited()
    written = dmn._hippocampus._schema.awrite.await_args.args[1]
    assert "## Projects assigned by Russ" in written
    assert "Karaoke Hero" in written
    # set_projects_context ran → digest populated.
    assert "Karaoke Hero" in dmn._last_projects or dmn._last_projects != ""


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
