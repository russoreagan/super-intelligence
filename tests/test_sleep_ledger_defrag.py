"""
B3 — sleep's thought-consolidation output goes to the unified stores:
  - open_questions → open_questions.md ## Open threads (NOT self.md)
  - insights → episodic memory as [CONCLUDED] conclusion episodes
  - the legacy self.md "## Open Questions" writeback is gone
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import brain.open_threads as ot
from brain.sleep import SleepConsolidation


def _make_sleep():
    s = SleepConsolidation.__new__(SleepConsolidation)
    s._router = MagicMock()
    s._router.embed = AsyncMock(return_value=None)
    s._schema = MagicMock()
    s._schema.read = MagicMock(return_value="")  # empty open_questions.md / self.md
    s._schema.awrite = AsyncMock()
    s._schema.aappend_fact = AsyncMock()
    s._schema.upsert_section = AsyncMock()
    s._episodic = MagicMock()
    s._episodic.encode = MagicMock()
    return s


def test_open_questions_go_to_ledger_not_selfmd():
    s = _make_sleep()
    result = {
        "open_questions": ["Does the prefetcher weight by emotional valence?"],
        "insights": [],
    }
    asyncio.run(s._apply_thought_updates(result))

    # Wrote the ledger section, never rewrote self.md with an Open Questions block.
    s._schema.upsert_section.assert_awaited()
    fname, section, body = s._schema.upsert_section.await_args.args
    assert fname == ot.LEDGER_FILE
    assert section == ot.SECTION
    threads = ot.parse_threads(body)
    assert any("prefetcher" in t.summary for t in threads)
    # self.md was NOT awritten with open-questions content.
    for call in s._schema.awrite.await_args_list:
        assert call.args[0] != "self.md" or "Open Questions" not in call.args[1]


def test_insights_encoded_as_conclusions():
    s = _make_sleep()
    result = {"open_questions": [], "insights": ["Gating reduces redundant context."]}
    asyncio.run(s._apply_thought_updates(result))

    s._episodic.encode.assert_called()
    ep = s._episodic.encode.call_args.args[0]
    assert "conclusion" in ep.topic_tags
    assert "knowledge" in ep.topic_tags
    assert "sleep" in ep.topic_tags
    assert ep.entity_response.startswith("[CONCLUDED]")


def test_ledger_dedup_skips_existing_question():
    s = _make_sleep()
    # open_questions.md already has a thread covering the same ground.
    existing_threads, _ = ot.open_thread([], "Does the prefetcher weight by valence?")
    s._schema.read = MagicMock(
        return_value=f"# x\n\n## Open threads\n{ot.render_section_body(existing_threads)}\n"
    )
    result = {"open_questions": ["the prefetcher weight by valence"], "insights": []}
    asyncio.run(s._apply_thought_updates(result))
    # Near-duplicate → no new write (nothing added).
    s._schema.upsert_section.assert_not_awaited()
