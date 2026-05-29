"""
Tests for DMN ↔ neuromodulator bidirectional coupling:
  1. _classify_thought — inward vs outward detection
  2. _tick_skip_probability — ACh/Glu/GABA gate formula
  3. Neuromod deltas applied after a thought fires (_tick integration)
  4. `direction` field published on stream.thought
"""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

from brain.dmn import _INWARD_DELTA, _OUTWARD_DELTA, DefaultModeNetwork, _classify_thought

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_dmn(neuromod_snapshot: dict | None = None):
    """Build a DefaultModeNetwork skeleton bypassing __init__."""
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)

    nm = MagicMock()
    nm.snapshot.return_value = neuromod_snapshot or {
        "ACh": 0.3,
        "DA": 0.5,
        "GABA": 0.05,
        "Glu": 0.2,
    }
    nm.add = MagicMock()

    dmn._bus = MagicMock()
    dmn._bus.neuromod = nm
    dmn._bus.publish_dict = AsyncMock()

    dmn._router = MagicMock()
    dmn._hippocampus = None
    dmn._parietal = None
    dmn._running = True
    dmn._last_context = "Recent: hello world"
    dmn._thought_count = 0
    dmn._recent_thoughts = deque(maxlen=5)
    dmn._suppressed_count = 0
    dmn._session_id = "test"

    dmn._monologue_cell = MagicMock()
    dmn._monologue_cell.reset_turn = MagicMock()
    dmn._monologue_cell.call = AsyncMock(return_value="")

    dmn._simulation_cell = MagicMock()
    dmn._simulation_cell.reset_turn = MagicMock()
    dmn._simulation_cell.call = AsyncMock(return_value="{}")

    dmn._anticipator_cell = MagicMock()
    dmn._anticipator_cell.reset_turn = MagicMock()
    dmn._anticipator_cell.call = AsyncMock(return_value="{}")

    dmn._prefetcher_cell = MagicMock()
    dmn._prefetcher_cell.reset_turn = MagicMock()
    dmn._prefetcher_cell.call = AsyncMock(return_value="{}")

    dmn.predicted_next = None
    dmn.last_was_question = False
    dmn.last_assistant_message = ""
    dmn.anticipations = []
    dmn.prefetched = []
    # Emotion + relationship state (added after fixture was written)
    dmn._last_emotion = "neutral"
    dmn._last_speaker_name = None
    dmn._last_affection_score = 0
    dmn._last_familiarity = "new"
    dmn._recent_angles = deque(maxlen=5)
    dmn._obs = None
    # Active projects manifest (loaded from open_questions.md in production)
    dmn._last_projects = ""
    # Session thought buffer for sleep consolidation
    dmn._session_thought_buf = []
    dmn._session_thought_limit = 200
    return dmn


# ── _classify_thought ─────────────────────────────────────────────────────────


class TestClassifyThought:
    def test_inward_existence(self):
        assert _classify_thought("I keep wondering about my existence.") == "inward"

    def test_inward_consciousness(self):
        assert _classify_thought("Am I actually conscious or just processing?") == "inward"

    def test_inward_nature(self):
        assert _classify_thought("What is my nature, really?") == "inward"

    def test_inward_purpose(self):
        assert _classify_thought("I'm not sure I understand my purpose here.") == "inward"

    def test_inward_myself(self):
        assert _classify_thought("I find myself drawn to introspection again.") == "inward"

    def test_outward_user(self):
        assert _classify_thought("I wonder what Russ is planning to work on next.") == "outward"

    def test_outward_idea(self):
        assert (
            _classify_thought("The audio latency problem seems related to buffer sizing.")
            == "outward"
        )

    def test_outward_question_about_world(self):
        assert (
            _classify_thought("Curious whether this pattern shows up in other voice interfaces.")
            == "outward"
        )

    def test_outward_conversation(self):
        assert (
            _classify_thought("That topic about Ableton seemed to energise the conversation.")
            == "outward"
        )

    def test_case_insensitive(self):
        assert _classify_thought("CONSCIOUSNESS is a strange thing to think about.") == "inward"


# ── _tick_skip_probability ────────────────────────────────────────────────────


class TestTickSkipProbability:
    def test_deep_idle_low_skip(self):
        # Low ACh + low Glu → brain truly idle → should rarely suppress DMN
        dmn = _make_dmn({"ACh": 0.15, "DA": 0.5, "GABA": 0.05, "Glu": 0.1})
        prob = dmn._tick_skip_probability()
        assert prob < 0.35

    def test_high_engagement_near_cap(self):
        # High ACh + high Glu → highly engaged → suppress strongly
        dmn = _make_dmn({"ACh": 0.8, "DA": 0.5, "GABA": 0.05, "Glu": 0.4})
        prob = dmn._tick_skip_probability()
        assert prob >= 0.80

    def test_cap_at_0_85(self):
        # Even extreme values should not exceed the cap
        dmn = _make_dmn({"ACh": 1.0, "DA": 0.5, "GABA": 0.0, "Glu": 1.0})
        prob = dmn._tick_skip_probability()
        assert prob <= 0.85

    def test_never_negative(self):
        dmn = _make_dmn({"ACh": 0.0, "DA": 0.5, "GABA": 0.9, "Glu": 0.0})
        prob = dmn._tick_skip_probability()
        assert prob >= 0.0

    def test_ach_primary_driver(self):
        # ACh change should have a larger effect than equivalent Glu change
        dmn_low_ach = _make_dmn({"ACh": 0.2, "DA": 0.5, "GABA": 0.05, "Glu": 0.3})
        dmn_high_ach = _make_dmn({"ACh": 0.6, "DA": 0.5, "GABA": 0.05, "Glu": 0.3})
        dmn_low_glu = _make_dmn({"ACh": 0.4, "DA": 0.5, "GABA": 0.05, "Glu": 0.2})
        dmn_high_glu = _make_dmn({"ACh": 0.4, "DA": 0.5, "GABA": 0.05, "Glu": 0.6})

        ach_delta = dmn_high_ach._tick_skip_probability() - dmn_low_ach._tick_skip_probability()
        glu_delta = dmn_high_glu._tick_skip_probability() - dmn_low_glu._tick_skip_probability()
        assert ach_delta > glu_delta

    def test_moderate_gaba_reduces_suppression(self):
        # Anxious-rumination range GABA should lower skip prob vs. low GABA
        dmn_low_gaba = _make_dmn({"ACh": 0.4, "DA": 0.5, "GABA": 0.05, "Glu": 0.2})
        dmn_mod_gaba = _make_dmn({"ACh": 0.4, "DA": 0.5, "GABA": 0.35, "Glu": 0.2})
        assert dmn_mod_gaba._tick_skip_probability() < dmn_low_gaba._tick_skip_probability()

    def test_high_gaba_does_not_reduce_suppression(self):
        # Very high GABA (>0.6, inhibited/frozen) should not benefit from the
        # anxious-rumination modifier — it falls outside the 0.2–0.6 window
        dmn_mod = _make_dmn({"ACh": 0.4, "DA": 0.5, "GABA": 0.35, "Glu": 0.2})
        dmn_high = _make_dmn({"ACh": 0.4, "DA": 0.5, "GABA": 0.70, "Glu": 0.2})
        # high GABA should have equal or higher skip prob than moderate GABA
        assert dmn_high._tick_skip_probability() >= dmn_mod._tick_skip_probability()


# ── Neuromod delta applied after thought fires ────────────────────────────────


class TestNeuromodDelta:
    def test_inward_thought_raises_gaba(self):
        dmn = _make_dmn()
        dmn._monologue_cell.call = AsyncMock(
            return_value="I keep wondering about the nature of my own consciousness."
        )
        asyncio.run(dmn._tick())
        dmn._bus.neuromod.add.assert_any_call("GABA", _INWARD_DELTA["GABA"])

    def test_outward_thought_raises_da_and_ach(self):
        dmn = _make_dmn()
        dmn._monologue_cell.call = AsyncMock(
            return_value="Curious whether Russ is planning to add more voice tools soon."
        )
        asyncio.run(dmn._tick())
        dmn._bus.neuromod.add.assert_any_call("DA", _OUTWARD_DELTA["DA"])
        dmn._bus.neuromod.add.assert_any_call("ACh", _OUTWARD_DELTA["ACh"])

    def test_suppressed_thought_applies_no_delta(self):
        dmn = _make_dmn()
        # Pre-load a thought that will cause the next one to be suppressed
        dmn._recent_thoughts.append(
            "The audio bleed was killing the conversation flow every single time."
        )
        dmn._monologue_cell.call = AsyncMock(
            return_value="The audio bleed was killing the conversation flow every time."
        )
        asyncio.run(dmn._tick())
        assert dmn._suppressed_count == 1
        dmn._bus.neuromod.add.assert_not_called()

    def test_empty_thought_applies_no_delta(self):
        dmn = _make_dmn()
        dmn._monologue_cell.call = AsyncMock(return_value="")
        asyncio.run(dmn._tick())
        dmn._bus.neuromod.add.assert_not_called()


# ── direction field in published payload ─────────────────────────────────────


class TestDirectionPublished:
    def test_inward_thought_publishes_direction_inward(self):
        dmn = _make_dmn()
        dmn._monologue_cell.call = AsyncMock(
            return_value="I wonder what my own existence really means."
        )
        asyncio.run(dmn._tick())
        payload = dmn._bus.publish_dict.call_args[0][1]
        assert payload["direction"] == "inward"

    def test_outward_thought_publishes_direction_outward(self):
        dmn = _make_dmn()
        dmn._monologue_cell.call = AsyncMock(
            return_value="Curious whether the new prefetcher is helping recall speed."
        )
        asyncio.run(dmn._tick())
        payload = dmn._bus.publish_dict.call_args[0][1]
        assert payload["direction"] == "outward"
