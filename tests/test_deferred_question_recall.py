"""
The RECALL half of the deferred-questions pipeline — the one side of it no test
exercised.

Write-side coverage exists (test_dmn_threads asserts the DMN fans an uncertain
conclusion into encode_deferred_question). But nothing drove the resurfacing: the
second, budgeted episodic search in hippocampus.recall that pulls deferred_question
episodes on their own limit of 2 and renders them under the "PENDING QUESTIONS
(from idle reflection — now relevant)" header, with the "[PENDING QUESTION] "
sentinel stripped. This pins that contract so a change to the budget, tag, or
header is caught.

Drives the REAL recall() (as test_hippocampus_spotlight does), with only the
episodic store faked so the two searches return scripted rows.
"""

from __future__ import annotations

import pytest


class _Router:
    async def call(self, *a, **kw):
        return "{}"

    def supports(self, *a, **kw):
        return True


class _FakeEpisodic:
    """Scripts the two searches recall() runs and records the deferred budget."""

    def __init__(self, main_rows, deferred_rows):
        self._main = main_rows
        self._deferred = deferred_rows
        self.deferred_call = None  # (tag, limit) actually requested

    def recall(self, vec, limit, exclude_tags=None, end_user_id=None):
        # Conversation memories must exclude the deferred tag (own lane).
        assert exclude_tags == ["deferred_question"]
        return list(self._main)

    def recall_by_tag(self, vec, tag, limit=3, end_user_id=None):
        self.deferred_call = (tag, limit)
        return list(self._deferred)[:limit]

    def recall_structural(self, *a, **kw):
        return []


def _make_hippo(main_rows, deferred_rows):
    from brain.bus import Bus
    from brain.clusters.hippocampus import HippocampusCluster

    hippo = HippocampusCluster(Bus(), _Router())
    hippo._schema.grep = lambda keyword: []  # hermetic — no schema file I/O
    fake = _FakeEpisodic(main_rows, deferred_rows)
    hippo._episodic = fake
    return hippo, fake


async def _embed(_query):
    return [0.1] * 768


def _deferred_ep(question: str, ts: float = 1000.0) -> dict:
    # Shape encode_deferred_question writes: the sentinel prefix + curious framing.
    return {"entity_response": f"[PENDING QUESTION] {question}", "ts": ts, "user_input": "(idle)"}


@pytest.mark.asyncio
async def test_deferred_questions_resurface_under_header_within_budget():
    q1 = "Does emotional gating reduce token cost — worth measuring?"
    q2 = "Is the DMN under-speaking at rest?"
    # Three available, but the budget is 2 — the third must not appear.
    hippo, fake = _make_hippo(
        main_rows=[],
        deferred_rows=[_deferred_ep(q1), _deferred_ep(q2), _deferred_ep("third — dropped")],
    )

    result = await hippo.recall(
        query="efficiency and idle behaviour",
        entities=["efficiency"],
        turn_id="t1",
        embedding_fn=_embed,
        novelty=False,
    )

    text = result["episodes"]
    assert "PENDING QUESTIONS (from idle reflection — now relevant):" in text
    assert f"- {q1}" in text
    assert f"- {q2}" in text
    assert "[PENDING QUESTION]" not in text  # sentinel stripped from the surfaced text
    assert "third — dropped" not in text  # budget of 2 enforced
    # The deferred search ran on its OWN budget of 2, on the discriminator tag.
    assert fake.deferred_call == ("deferred_question", 2)


@pytest.mark.asyncio
async def test_no_deferred_questions_leaves_no_header():
    """No deferred episodes → no PENDING QUESTIONS header (neutral when empty)."""
    hippo, _ = _make_hippo(main_rows=[], deferred_rows=[])
    result = await hippo.recall(
        query="anything",
        entities=[],
        turn_id="t2",
        embedding_fn=_embed,
        novelty=False,
    )
    assert "PENDING QUESTIONS" not in result["episodes"]
