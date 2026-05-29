"""
Tests for features added in the startup + consolidation work:

  DMN startup priming:
  - prime_startup() fires a tick and queues a speak candidate
  - prime_startup() is silent when the monologue returns nothing
  - prime_startup() survives a tick that raises an exception

  DMN memory seeding:
  - update_context() with last-session text is visible in the monologue prompt
  - seeded context is overwritten by a live parietal update on next tick

  Temporal affect lexicon (_heuristic_affect):
  - positive words return positive sentiment and a matching user_emotion
  - negative/frustration words return negative sentiment and hostile tone
  - empty input returns neutral defaults
  - multiple exclamation marks amplify sentiment
  - double-question-mark with no other signal returns confused emotion

  Sleep consolidation single-flight guard:
  - consolidate_now() returns {ran: False} while a lock is already held
  - consolidate_now() returns {ran: False} when trace buffer is empty
"""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Shared DMN skeleton
# ---------------------------------------------------------------------------


def _make_dmn(monologue_response: str = ""):
    from brain.dmn import DefaultModeNetwork

    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._bus = MagicMock()
    dmn._bus.publish_dict = AsyncMock()
    dmn._bus.neuromod.snapshot.return_value = {
        "DA": 0.5,
        "GABA": 0.1,
        "ACh": 0.3,
        "Glu": 0.3,
        "NE": 0.2,
        "5HT": 0.5,
        "CORT": 0.1,
        "OXT": 0.3,
        "AEA": 0.2,
    }
    dmn._router = MagicMock()
    dmn._hippocampus = None
    dmn._parietal = None
    dmn._running = True
    dmn._last_context = ""
    dmn._last_self_schema = ""
    dmn._last_emotion = "neutral"
    dmn._last_speaker_name = None
    dmn._last_affection_score = 0
    dmn._last_familiarity = "new"
    dmn._thought_count = 0
    dmn._recent_thoughts = deque(maxlen=10)
    dmn._recent_angles = deque(maxlen=5)
    dmn._suppressed_count = 0
    dmn._candidate_q = deque(maxlen=8)
    dmn._proactive_q = deque(maxlen=2)
    dmn._self_task_q = deque(maxlen=4)
    dmn._session_thought_buf = []
    dmn._session_thought_limit = 200
    dmn._last_projects = ""
    dmn.last_was_question = False
    dmn.last_assistant_message = ""
    dmn.anticipations = []
    dmn.prefetched = []
    dmn.predicted_next = None
    dmn._obs = None
    dmn._session_id = "test"

    dmn._monologue_cell = MagicMock()
    dmn._monologue_cell.reset_turn = MagicMock()
    dmn._monologue_cell.call = AsyncMock(return_value=monologue_response)

    dmn._simulation_cell = MagicMock()
    dmn._simulation_cell.reset_turn = MagicMock()
    dmn._simulation_cell.call = AsyncMock(return_value="{}")

    dmn._anticipator_cell = MagicMock()
    dmn._anticipator_cell.reset_turn = MagicMock()
    dmn._anticipator_cell.call = AsyncMock(return_value="{}")

    dmn._prefetcher_cell = MagicMock()
    dmn._prefetcher_cell.reset_turn = MagicMock()
    dmn._prefetcher_cell.call = AsyncMock(return_value="{}")

    return dmn


# ---------------------------------------------------------------------------
# prime_startup()
# ---------------------------------------------------------------------------


class TestPrimeStartup:
    def test_prime_startup_fires_tick_and_queues_candidate(self):
        """When the monologue returns a spoken form, prime_startup queues a candidate."""
        response = '{"thought": "Where were we last time?", "speak": true, "spoken": "Hey, where were we?", "angle": "recall", "task": "", "propose": false, "plan": false, "defer": ""}'
        dmn = _make_dmn(monologue_response=response)
        asyncio.run(dmn.prime_startup())
        assert dmn._thought_count == 1
        assert len(dmn._candidate_q) == 1
        assert "where were we" in dmn._candidate_q[0]["spoken"].lower()

    def test_prime_startup_silent_when_monologue_empty(self):
        """No candidate is queued when the monologue returns nothing."""
        dmn = _make_dmn(monologue_response="")
        asyncio.run(dmn.prime_startup())
        assert dmn._thought_count == 1
        assert len(dmn._candidate_q) == 0

    def test_prime_startup_survives_tick_exception(self):
        """A crashing tick must not propagate — prime_startup swallows it."""
        dmn = _make_dmn()
        dmn._monologue_cell.call = AsyncMock(side_effect=RuntimeError("model offline"))
        # Should not raise
        asyncio.run(dmn.prime_startup())

    def test_prime_startup_increments_thought_count(self):
        dmn = _make_dmn(monologue_response="")
        assert dmn._thought_count == 0
        asyncio.run(dmn.prime_startup())
        assert dmn._thought_count == 1


# ---------------------------------------------------------------------------
# Memory seeding via update_context
# ---------------------------------------------------------------------------


class TestDMNMemorySeed:
    def test_seeded_context_appears_in_monologue_prompt(self):
        """After update_context with last-session text, the prompt includes it."""
        dmn = _make_dmn(monologue_response="")
        last_session = (
            "Last session:\n\n[code review]\n  User: check run.py\n  Me: found three issues"
        )
        dmn.update_context(last_session)
        asyncio.run(dmn._tick())
        call_args = dmn._monologue_cell.call.call_args
        user_content = call_args[0][0][0]["content"]
        assert "check run.py" in user_content or "last session" in user_content.lower()

    def test_seeded_context_overwritten_by_parietal_update(self):
        """A live parietal update should replace the seed, not append to it."""
        dmn = _make_dmn(monologue_response="")
        dmn.update_context("Last session: old stuff")
        dmn.update_context("Live conversation: new topic entirely")
        assert "new topic entirely" in dmn._last_context
        assert "old stuff" not in dmn._last_context

    def test_new_session_marker_in_seeded_context(self):
        """The seed text should indicate a new session started."""
        dmn = _make_dmn(monologue_response="")
        seed = "Last session:\n\n[topic]\n  User: hello\n  Me: hi\n\n(New session just started.)"
        dmn.update_context(seed)
        assert "New session just started" in dmn._last_context


# ---------------------------------------------------------------------------
# Temporal affect lexicon (_heuristic_affect)
# ---------------------------------------------------------------------------


class TestHeuristicAffect:
    def _affect(self, text):
        from brain.clusters.temporal import _heuristic_affect

        return _heuristic_affect(text)

    def test_love_is_positive_and_affectionate(self):
        r = self._affect("I love this")
        assert r["sentiment"] > 0
        assert r["user_emotion"] == "affectionate"
        assert r["user_tone_toward_ai"] == "warm"

    def test_frustration_word_is_negative(self):
        r = self._affect("this is so frustrated")
        assert r["sentiment"] < 0
        assert r["user_emotion"] in ("frustrated", "annoyed", "angry")

    def test_empty_input_returns_neutral(self):
        r = self._affect("")
        assert r["sentiment"] == 0.0
        assert r["hostility"] == 0.0
        assert r["user_emotion"] == "neutral"
        assert r["user_tone_toward_ai"] == "neutral"

    def test_whitespace_only_returns_neutral(self):
        r = self._affect("   ")
        assert r["user_emotion"] == "neutral"

    def test_exclamation_amplifies_positive(self):
        base = self._affect("great")
        excited = self._affect("great!")
        assert excited["sentiment"] >= base["sentiment"]

    def test_double_question_no_signal_returns_confused(self):
        r = self._affect("??")
        assert r["user_emotion"] == "confused"

    def test_awesome_is_excited_and_warm(self):
        r = self._affect("that is awesome")
        assert r["sentiment"] > 0
        assert r["user_emotion"] == "excited"

    def test_sentiment_clamped_to_range(self):
        r = self._affect("love love love amazing awesome great fantastic")
        assert -1.0 <= r["sentiment"] <= 1.0
        assert 0.0 <= r["hostility"] <= 1.0


# ---------------------------------------------------------------------------
# Sleep consolidation single-flight guard
# ---------------------------------------------------------------------------


class TestConsolidationGuard:
    def _make_session_stub(self, n_traces: int = 3):
        """Return a minimal object that has the consolidation methods mixed in."""
        from brain.session_loops import _LoopsMixin

        class _Stub(_LoopsMixin):
            def __init__(self):
                self._session_traces = [{"user_input": f"turn {i}"} for i in range(n_traces)]
                self._session_traces_full = list(self._session_traces)
                self._last_consolidation_ts = 0.0
                self._sleep = MagicMock()
                self._sleep.consolidate = AsyncMock(return_value=None)
                self._consolidation_lock = asyncio.Lock()
                self.dmn = None
                self.hippocampus = MagicMock()
                self.hippocampus._schema.read = MagicMock(return_value=None)
                self.session_id = "test-session"

        return _Stub()

    def test_consolidate_now_runs_when_traces_present(self):
        stub = self._make_session_stub(n_traces=3)
        result = asyncio.run(stub.consolidate_now(reason="test"))
        assert result["ran"] is True
        assert result["turns"] == 3
        stub._sleep.consolidate.assert_awaited_once()

    def test_consolidate_now_skips_when_no_traces(self):
        stub = self._make_session_stub(n_traces=0)
        result = asyncio.run(stub.consolidate_now(reason="test"))
        assert result["ran"] is False
        assert result["reason"] == "no_buffered_turns"

    def test_consolidate_now_skips_when_lock_held(self):
        stub = self._make_session_stub(n_traces=5)

        async def _run():
            async with stub._consolidation_lock:
                return await stub.consolidate_now(reason="concurrent")

        result = asyncio.run(_run())
        assert result["ran"] is False
        assert result["reason"] == "already_running"

    def test_consolidation_clears_trace_buffer(self):
        stub = self._make_session_stub(n_traces=4)
        asyncio.run(stub.consolidate_now(reason="test"))
        assert stub._session_traces == []
        assert stub._session_traces_full == []

    def test_consolidate_now_no_sleep_returns_disabled(self):
        stub = self._make_session_stub(n_traces=3)
        stub._sleep = None
        result = asyncio.run(stub.consolidate_now(reason="test"))
        assert result["ran"] is False
        assert result["reason"] == "sleep_loop_disabled"
