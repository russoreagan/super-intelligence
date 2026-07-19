"""Sleep self-model updates must LAND, including into sections the doc doesn't
have yet. The seeded self-models carry "## History summary" but no
"## Stable preferences", and the old applier used a replace-only regex — so the
stable_preferences half of every sleep self-update was silently dropped since
the section never existed anywhere. The applier now routes through
SchemaStore._replace_section_body, which appends missing sections.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from brain.second_brain.store import SchemaStore
from brain.sleep import SleepConsolidation

SEEDED_DOC = (
    "# Self-Model — The Sage\n\n"
    "## Personality\n- calm\n\n"
    "## History summary\n\n"
    "## Current mood signature\nDA=0.35 GABA=0.28 ACh=0.18 dominant=baseline (The Sage)\n\n"
    "## Values\n- care without hurry\n"
)


def _make_sleep(doc: str = SEEDED_DOC):
    s = SleepConsolidation.__new__(SleepConsolidation)
    s._schema = MagicMock()
    s._schema.read = MagicMock(return_value=doc)
    s._schema.awrite = AsyncMock()
    # Real section helper — the applier's append-when-missing behavior IS the
    # thing under test, so don't mock it away.
    s._schema._replace_section_body = SchemaStore._replace_section_body
    return s


def test_history_summary_replaces_in_place():
    s = _make_sleep()
    asyncio.run(s._apply_self_updates({"history_summary": "Talked about time and rivers."}))
    fname, out = s._schema.awrite.await_args.args
    assert fname == "self.md"
    # Landed inside its section — between the heading and the next section.
    assert out.index("## History summary") < out.index("Talked about time and rivers.")
    assert out.index("Talked about time and rivers.") < out.index("## Current mood signature")


def test_stable_preferences_section_is_created_not_dropped():
    s = _make_sleep()
    asyncio.run(
        s._apply_self_updates(
            {
                "history_summary": "Two sessions on epistemology.",
                "stable_preferences": "- pauses before answering\n- prefers questions to claims",
            }
        )
    )
    _, out = s._schema.awrite.await_args.args
    assert "## Stable preferences" in out
    assert "- pauses before answering" in out
    assert "Two sessions on epistemology." in out


def test_empty_and_unknown_keys_are_ignored():
    s = _make_sleep()
    asyncio.run(
        s._apply_self_updates(
            {"history_summary": "", "stable_preferences": None, "grandiosity": "ignore me"}
        )
    )
    _, out = s._schema.awrite.await_args.args
    assert out == SEEDED_DOC  # nothing applied
    assert "grandiosity" not in out


def test_missing_doc_skips_write():
    s = _make_sleep(doc="")
    asyncio.run(s._apply_self_updates({"history_summary": "anything"}))
    s._schema.awrite.assert_not_awaited()
