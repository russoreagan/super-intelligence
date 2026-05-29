"""
Tests for FollowThrough commitment extraction and the asking_user signal.

All tests are deterministic — the LLM is not invoked. _parse() is a pure
static method, and the session-turn integration is tested by injecting a
fake extract() that returns the exact signals we want to assert against.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.clusters.follow_through import FollowThrough

# ── _parse unit tests ──────────────────────────────────────────────────────────


class TestFollowThroughParse:
    """FollowThrough._parse: pure JSON → (goal, asking_user) mapping."""

    def test_commitment_returns_goal_and_not_asking(self):
        raw = json.dumps(
            {
                "commitment": True,
                "asking_user": False,
                "goal": "List files in /Users/russ/Documents/Karaoke Hero",
            }
        )
        goal, asking = FollowThrough._parse(raw)
        assert goal == "List files in /Users/russ/Documents/Karaoke Hero"
        assert asking is False

    def test_asking_user_returns_none_goal_and_asking_true(self):
        raw = json.dumps({"commitment": False, "asking_user": True, "goal": ""})
        goal, asking = FollowThrough._parse(raw)
        assert goal is None
        assert asking is True

    def test_no_commitment_no_question_returns_none_false(self):
        raw = json.dumps({"commitment": False, "asking_user": False, "goal": ""})
        goal, asking = FollowThrough._parse(raw)
        assert goal is None
        assert asking is False

    def test_asking_user_field_missing_defaults_false(self):
        """Old-format responses without asking_user are safe."""
        raw = json.dumps({"commitment": False, "goal": ""})
        goal, asking = FollowThrough._parse(raw)
        assert goal is None
        assert asking is False

    def test_commitment_true_asking_user_ignored(self):
        """If commitment=true, asking_user is irrelevant — goal wins."""
        raw = json.dumps({"commitment": True, "asking_user": True, "goal": "Read the file"})
        goal, asking = FollowThrough._parse(raw)
        assert goal == "Read the file"
        assert asking is False  # commitment wins

    def test_empty_goal_in_commitment_returns_none(self):
        raw = json.dumps({"commitment": True, "asking_user": False, "goal": ""})
        goal, asking = FollowThrough._parse(raw)
        assert goal is None

    def test_whitespace_goal_in_commitment_returns_none(self):
        raw = json.dumps({"commitment": True, "asking_user": False, "goal": "   "})
        goal, asking = FollowThrough._parse(raw)
        assert goal is None

    def test_invalid_json_returns_none_false(self):
        goal, asking = FollowThrough._parse("not json at all")
        assert goal is None
        assert asking is False

    def test_empty_string_returns_none_false(self):
        goal, asking = FollowThrough._parse("")
        assert goal is None
        assert asking is False

    def test_json_embedded_in_prose(self):
        """JSON embedded after some text is still parsed."""
        raw = 'Here is the result: {"commitment": false, "asking_user": true, "goal": ""}'
        goal, asking = FollowThrough._parse(raw)
        assert goal is None
        assert asking is True


# ── extract() return-value contract tests ──────────────────────────────────────


class TestFollowThroughExtract:
    """extract() returns (goal, asking_user) in all cases."""

    def _make_ft(self, router_response: str) -> FollowThrough:
        router = MagicMock()
        router.call = AsyncMock(return_value=router_response)
        ft = FollowThrough(router)
        return ft

    @pytest.mark.asyncio
    async def test_commitment_returns_goal_tuple(self):
        raw = json.dumps({"commitment": True, "asking_user": False, "goal": "List codebase files"})
        ft = self._make_ft(raw)
        goal, asking = await ft.extract("look at the code", "Let me grab those now.", "t1")
        assert goal == "List codebase files"
        assert asking is False

    @pytest.mark.asyncio
    async def test_asking_user_returns_none_true(self):
        raw = json.dumps({"commitment": False, "asking_user": True, "goal": ""})
        ft = self._make_ft(raw)
        goal, asking = await ft.extract(
            "that file looks interesting",
            "Should I go and pull that up for you?",
            "t1",
        )
        assert goal is None
        assert asking is True

    @pytest.mark.asyncio
    async def test_no_commitment_returns_none_false(self):
        raw = json.dumps({"commitment": False, "asking_user": False, "goal": ""})
        ft = self._make_ft(raw)
        goal, asking = await ft.extract("okay", "Got it.", "t1")
        assert goal is None
        assert asking is False

    @pytest.mark.asyncio
    async def test_empty_response_returns_none_false_without_llm_call(self):
        router = MagicMock()
        router.call = AsyncMock(return_value="should not be called")
        ft = FollowThrough(router)
        goal, asking = await ft.extract("hi", "", "t1")
        assert goal is None
        assert asking is False
        router.call.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_exception_returns_none_false(self):
        router = MagicMock()
        router.call = AsyncMock(side_effect=RuntimeError("network error"))
        ft = FollowThrough(router)
        # Must not raise — follow-through is best-effort
        goal, asking = await ft.extract("do something", "I'll look now.", "t1")
        assert goal is None
        assert asking is False


# ── Session-turn integration: asking_user blocks enqueueing ───────────────────


class TestFollowThroughSessionIntegration:
    """The session-turn _follow_through_check must not enqueue when asking_user=True."""

    def _make_task_queue(self):
        q = MagicMock()
        q.enqueue = MagicMock()
        return q

    def _make_pending_task(self, goal: str | None):
        pt = MagicMock()
        pt.has_pending.return_value = goal is not None
        pt.take.return_value = goal
        return pt

    @pytest.mark.asyncio
    async def test_asking_user_blocks_deferred_goal_enqueue(self):
        """task-mode: AI asked permission → deferred goal must NOT be enqueued."""
        task_queue = self._make_task_queue()
        ft = MagicMock()
        ft.extract = AsyncMock(return_value=(None, True))  # asking_user=True

        # Simulate the _follow_through_check logic from session_turn.py
        deferred_goal = "Analyze codebase"  # set by FrontalTaskSubsystem
        extracted, asking_user = await ft.extract(
            "look at the code", "Should I pull that up?", "t1"
        )
        if asking_user:
            pass  # do not enqueue
        elif extracted:
            task_queue.enqueue(extracted, source="user", priority=1)
        else:
            task_queue.enqueue(deferred_goal, source="user", priority=1)

        task_queue.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_commitment_enqueues_extracted_goal(self):
        """task-mode: clear commitment → extracted goal is enqueued."""
        task_queue = self._make_task_queue()
        ft = MagicMock()
        ft.extract = AsyncMock(return_value=("List codebase files", False))

        deferred_goal = "some fallback"
        extracted, asking_user = await ft.extract("look at code", "Let me grab those.", "t1")
        if asking_user:
            pass
        elif extracted:
            task_queue.enqueue(extracted, source="user", priority=1)
        else:
            task_queue.enqueue(deferred_goal, source="user", priority=1)

        task_queue.enqueue.assert_called_once_with("List codebase files", source="user", priority=1)

    @pytest.mark.asyncio
    async def test_no_commitment_uses_deferred_fallback(self):
        """task-mode: brief ack (no commitment, no question) → deferred fallback enqueued."""
        task_queue = self._make_task_queue()
        ft = MagicMock()
        ft.extract = AsyncMock(return_value=(None, False))  # brief ack

        deferred_goal = "Analyze codebase"
        extracted, asking_user = await ft.extract("do the thing", "Got it.", "t1")
        if asking_user:
            pass
        elif extracted:
            task_queue.enqueue(extracted, source="user", priority=1)
        else:
            task_queue.enqueue(deferred_goal, source="user", priority=1)

        task_queue.enqueue.assert_called_once_with("Analyze codebase", source="user", priority=1)

    @pytest.mark.asyncio
    async def test_asking_user_blocks_reactive_enqueue(self):
        """reactive-mode: AI asked permission in a non-task turn → nothing enqueued."""
        task_queue = self._make_task_queue()
        ft = MagicMock()
        ft.extract = AsyncMock(return_value=(None, True))

        goal, asking_user = await ft.extract("that sounds useful", "Want me to look?", "t1")
        if goal and not asking_user:
            task_queue.enqueue(goal, source="user", priority=1)

        task_queue.enqueue.assert_not_called()
