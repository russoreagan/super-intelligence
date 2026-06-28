"""
URL fetching reaches the engine/API caller.

The capability (motor's fetch_url) always existed, but reactive tools were
*deferred* to a background task whose result surfaced only via proactive speech —
a channel the synchronous engine API never reads and the agent lane gates silent.
So an API consumer that pasted a URL got "I'm working on this" and never the page.

Two guards here:
  1. A pasted http(s) URL flips requires_action so the planner runs on it.
  2. On the API path the reactive tool runs INLINE and its output is folded into
     memory["tool_result"], which the drafter injects into the synchronous reply.
"""

from __future__ import annotations

import asyncio

from brain.clusters.temporal import _looks_like_tool_request
from brain.session_turn import _TurnMixin


# ── 1. A bare/pasted URL is itself a fetch request ────────────────────────────


def test_pasted_url_forces_tool_request():
    assert _looks_like_tool_request("https://news.ycombinator.com")
    assert _looks_like_tool_request("summarize https://example.com/article")
    assert _looks_like_tool_request("look at http://foo.io/x what do you think")


def test_plain_chat_does_not_force_tool_request():
    # No URL, no tool verb → must NOT trip the action path (no false fetches).
    assert not _looks_like_tool_request("what do you think about the ocean")
    assert not _looks_like_tool_request("I love this idea")


# ── 2. Inline motor folds tool output into the drafter context ────────────────


class _FakeNeuromod:
    def add(self, *a, **k):
        pass

    def snapshot(self):
        return {}


class _FakeBus:
    def __init__(self):
        self.neuromod = _FakeNeuromod()
        self.hormonal = _FakeNeuromod()


class _FakeMotor:
    def __init__(self, result):
        self._result = result
        self.reset_called = None

    def reset_turn(self, turn_id):
        self.reset_called = turn_id

    async def execute(self, features, turn_id):
        return self._result


class _FakeSelf:
    """Minimal surface _run_motor_inline touches — avoids a full BrainSession."""

    def __init__(self, result):
        self.motor = _FakeMotor(result)
        self.bus = _FakeBus()
        self._emitter = None
        self._recent_task_results: list = []


def test_inline_motor_folds_fetch_url_output():
    fake = _FakeSelf({"tool": "fetch_url", "output": "PAGE TEXT HERE", "success": True})
    features = {"raw_text": "summarize https://example.com", "requires_action": True}

    out = asyncio.run(_TurnMixin._run_motor_inline(fake, features, "t1"))

    # The page content reaches the drafter (the whole point — not "working on it").
    assert "PAGE TEXT HERE" in out
    assert out.startswith("[fetch_url]")
    assert fake.motor.reset_called == "t1"
    # Recorded for "what did you just do" follow-ups.
    assert fake._recent_task_results
    assert fake._recent_task_results[0]["success"] is True


def test_inline_motor_pending_write_is_left_parked():
    # A write needing confirmation must NOT be recorded as a completed result; the
    # executor's pending slot stays for api_turn to harvest.
    fake = _FakeSelf(
        {
            "tool": "cloud_action",
            "output": "CONFIRMATION_NEEDED: send email to Bob",
            "success": None,
            "pending": True,
        }
    )
    out = asyncio.run(_TurnMixin._run_motor_inline(fake, {"raw_text": "email bob"}, "t2"))

    assert "[action_pending]" in out
    assert "send email to Bob" in out
    assert fake._recent_task_results == []


def test_inline_motor_no_tool_returns_empty():
    # Planner said "none" (or tool produced nothing) → no tool_result; turn proceeds.
    assert asyncio.run(_TurnMixin._run_motor_inline(_FakeSelf(None), {"raw_text": "hi"}, "t3")) == ""


def test_inline_motor_failure_does_not_raise():
    class _Boom(_FakeMotor):
        async def execute(self, features, turn_id):
            raise RuntimeError("planner exploded")

    fake = _FakeSelf(None)
    fake.motor = _Boom(None)
    out = asyncio.run(_TurnMixin._run_motor_inline(fake, {"raw_text": "x"}, "t4"))
    assert out.startswith("[tool_error]")
