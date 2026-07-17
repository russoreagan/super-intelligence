"""
End-to-end materialization of the open-threads ledger — the seam the write bug
lived in and no other test covered.

Two existing layers guard the pieces but never meet:
  - tests/test_open_threads.py drives SchemaStore._replace_section_body directly
    (the writer in isolation, with a pre-rendered non-ASCII body).
  - tests/test_dmn_threads.py drives a live DMN's open/advance/conclude but with
    a MOCKED upsert_section — the mock never touches the real writer, exactly as
    the swallowed-warning never surfaced the raise in production.

So nothing proved that a real DMN, opening/advancing/concluding a thread whose
prose carries ordinary LLM punctuation (a curly apostrophe, an em dash), actually
MATERIALIZES `## Open threads` on disk through the real store — while leaving the
hand-authored sections of open_questions.md untouched. That is the regression this
file pins. Before the _replace_section_body fix (commit 56f5a4c) the first assertion
below failed: render_section_body → json.dumps(ensure_ascii) turned the apostrophe
into \\uXXXX, re.subn parsed it as a bad escape, _save_threads swallowed the raise,
and the section was never created.

The DMN is driven exactly as in test_dmn_threads (crafted monologue metadata, no
LLM), but its hippocampus._schema is a REAL SchemaStore over a temp dir, so every
_save_threads() runs the real read → _replace_section_body → atomic write.
"""

from __future__ import annotations

import asyncio

import pytest

import brain.open_threads as ot

# Reuse the exact DMN skeleton the mocked-store integration tests use, then swap in
# a real store — so any drift in how the DMN drives the ledger is shared, and this
# test differs from test_dmn_threads in one axis only: the store is real.
from tests.test_dmn_threads import _make_dmn, _meta

# A hand-authored open_questions.md with NO `## Open threads` section yet — the real
# file's state, and so the state of the DMN's very first save. Carries non-ASCII in
# both prose and a bullet so we also prove the writer preserves hand-authored
# non-ASCII, not just the JSON body.
_SEED = (
    "# Open Questions & Projects\n"
    "\n"
    "This is Russ’s working list of unresolved threads.\n"
    "\n"
    "## Architecture & self-improvement\n"
    "\n"
    "- Does the Hebbian weight system differentiate responses over time — or decay-flatten?\n"
    "\n"
    "## Projects assigned by Russ\n"
    "\n"
    "### Self-code review (PRIMARY)\n"
    "- **Task**: Review my own codebase.\n"
    "- **Status**: In progress.\n"
)

# Ordinary LLM prose punctuation — the input that raised pre-fix.
_NON_ASCII_SUMMARY = "Does emotional gating reduce token cost — worth measuring, per Russ’s note?"


def _hand_authored_intact(text: str) -> None:
    """Every hand-authored section and its content survived the managed write."""
    assert "## Architecture & self-improvement" in text
    assert "Does the Hebbian weight system differentiate" in text
    assert "## Projects assigned by Russ" in text
    assert "### Self-code review (PRIMARY)" in text
    assert "**Status**: In progress." in text
    assert "This is Russ’s working list" in text  # intro prose, incl. curly apostrophe


@pytest.fixture
def dmn_with_real_store(fake_schema_store):
    """A DMN skeleton whose hippocampus persists through a REAL SchemaStore over a
    temp dir, seeded with a hand-authored (no `## Open threads`) open_questions.md."""
    fake_schema_store.write(ot.LEDGER_FILE, _SEED)
    dmn = _make_dmn()
    # Swap the mock schema for the real one; _schema_store() reaches it via hippocampus._schema.
    dmn._hippocampus._schema = fake_schema_store
    return dmn, fake_schema_store


@pytest.mark.asyncio
async def test_open_advance_conclude_materializes_section_end_to_end(dmn_with_real_store):
    dmn, store = dmn_with_real_store

    # ── OPEN — the first save must CREATE `## Open threads`, non-ASCII and all.
    # This is the exact call that silently no-op'd before the writer fix.
    await dmn._process_thought(
        _NON_ASCII_SUMMARY,
        _meta(open_thread=True, angle="efficiency", bears_on=["efficiency-question"]),
        "t-open",
    )
    text = store.read(ot.LEDGER_FILE)
    assert f"## {ot.SECTION}" in text, "the managed section must materialize on the first save"
    threads = ot.parse_threads(ot.extract_section(text))
    assert len(threads) == 1
    opened = threads[0]
    assert opened.summary == _NON_ASCII_SUMMARY  # non-ASCII round-tripped through disk
    assert opened.bears_on == ["efficiency-question"]
    _hand_authored_intact(text)

    # ── ADVANCE — a later save rewrites the section in place.
    await dmn._process_thought(
        "Instrument tokens-per-useful-response with and without gating — A/B it.",
        _meta(advance_thread_id=opened.id, angle="measurement"),
        "t-advance",
    )
    text = store.read(ot.LEDGER_FILE)
    threads = ot.parse_threads(ot.extract_section(text))
    assert len(threads) == 1
    assert threads[0].advances == 1
    assert threads[0].progress and "A/B it" in threads[0].progress[-1]
    _hand_authored_intact(text)

    # ── CONCLUDE (confident) — the thread retires from the file and the conclusion
    # commits to episodic memory; the section stays (now an empty list).
    await dmn._process_thought(
        "Settled: gating trims redundant context, lowering tokens-per-useful-response.",
        _meta(
            conclude_thread_id=opened.id,
            conclusion="Emotional gating reduces tokens-per-useful-response.",
            conclusion_confidence="confident",
        ),
        "t-conclude",
    )
    text = store.read(ot.LEDGER_FILE)
    assert f"## {ot.SECTION}" in text  # section persists even when empty
    assert ot.parse_threads(ot.extract_section(text)) == []  # thread retired on disk
    _hand_authored_intact(text)
    # The confident conclusion was committed to memory. encode_conclusion is fired
    # fire-and-forget via asyncio.create_task, so yield to the loop before asserting.
    await asyncio.sleep(0.05)
    dmn._hippocampus.encode_conclusion.assert_awaited()


@pytest.mark.asyncio
async def test_uncertain_conclusion_persists_pending_status_to_disk(dmn_with_real_store):
    """The uncertain branch parks the thread as pending_confirmation — and that
    status must survive to disk, since a restart reloads the ledger from the file.
    (deferred_thoughts.md itself is written via a plain append the bug never touched;
    _append_deferred_thought is mocked in the skeleton, so only the ledger write is
    exercised here.)"""
    dmn, store = dmn_with_real_store
    dmn._open_threads, t = ot.open_thread([], _NON_ASCII_SUMMARY, bears_on=["efficiency"])
    await dmn._save_threads()

    await dmn._process_thought(
        "I lean yes but I'm not certain gating is the cause.",
        _meta(
            conclude_thread_id=t.id,
            conclusion="Gating probably reduces cost.",
            conclusion_confidence="uncertain",
        ),
        "t-uncertain",
    )
    text = store.read(ot.LEDGER_FILE)
    parked = ot.parse_threads(ot.extract_section(text))
    assert len(parked) == 1
    assert parked[0].status == ot.STATUS_PENDING
    assert parked[0].pending_conclusion == "Gating probably reduces cost."
    _hand_authored_intact(text)
