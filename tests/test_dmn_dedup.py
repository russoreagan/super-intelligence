"""
Tests for DMN thought-deduplication:
  - recent thoughts are shown to the LLM in the prompt so it varies
  - near-duplicate output is suppressed via word-overlap (Jaccard)
  - genuinely different thoughts pass through and join the recent buffer
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _make_dmn():
    """Build a DefaultModeNetwork skeleton bypassing __init__."""
    from collections import deque

    from brain.dmn import DefaultModeNetwork
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._bus = MagicMock()
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

    # Mock monologue + simulation cells
    dmn._monologue_cell = MagicMock()
    dmn._monologue_cell.reset_turn = MagicMock()
    dmn._monologue_cell.call = AsyncMock(return_value="")

    dmn._simulation_cell = MagicMock()
    dmn._simulation_cell.reset_turn = MagicMock()
    dmn._simulation_cell.call = AsyncMock(return_value="{}")

    # Anticipator + prefetcher cells added in Phase 3 / 3b
    dmn._anticipator_cell = MagicMock()
    dmn._anticipator_cell.reset_turn = MagicMock()
    dmn._anticipator_cell.call = AsyncMock(return_value="{}")

    dmn._prefetcher_cell = MagicMock()
    dmn._prefetcher_cell.reset_turn = MagicMock()
    dmn._prefetcher_cell.call = AsyncMock(return_value="{}")

    dmn.predicted_next = None
    # Phase 3 state
    dmn.last_was_question = False
    dmn.last_assistant_message = ""
    dmn.anticipations = []
    # Phase 3b state
    dmn.prefetched = []
    # Emotion + relationship state (set by update_context in production)
    dmn._last_emotion = "neutral"
    dmn._last_speaker_name = None
    dmn._last_affection_score = 0
    dmn._last_familiarity = "new"
    # Recent angles window (dedup for thought directions)
    from collections import deque as _deque
    dmn._recent_angles = _deque(maxlen=5)
    # Obs layer (optional; tests don't need it)
    dmn._obs = None
    # Active projects manifest (loaded from open_questions.md in production)
    dmn._last_projects = ""
    # Session thought buffer for sleep consolidation
    dmn._session_thought_buf = []
    dmn._session_thought_limit = 200
    return dmn


def test_first_thought_is_published_and_recorded():
    dmn = _make_dmn()
    dmn._monologue_cell.call = AsyncMock(return_value="I'm noticing the audio bleed issue keeps coming up.")
    asyncio.run(dmn._tick())
    assert len(dmn._recent_thoughts) == 1
    assert dmn._recent_thoughts[0].startswith("I'm noticing")
    assert dmn._bus.publish_dict.await_count == 1
    assert dmn._suppressed_count == 0


def test_duplicate_thought_is_suppressed():
    dmn = _make_dmn()
    dmn._recent_thoughts.append(
        "Audio bleed was killing the conversation flow every single time."
    )
    # New thought is near-verbatim; word overlap >0.45 → suppressed
    dmn._monologue_cell.call = AsyncMock(
        return_value="The audio bleed was killing the conversation flow every time."
    )
    asyncio.run(dmn._tick())
    assert dmn._suppressed_count == 1
    assert len(dmn._recent_thoughts) == 1  # unchanged
    assert dmn._bus.publish_dict.await_count == 0


def test_genuinely_different_thought_passes_through():
    dmn = _make_dmn()
    dmn._recent_thoughts.append(
        "I keep thinking about how the user phrases tool requests."
    )
    dmn._monologue_cell.call = AsyncMock(
        return_value="I wonder what triggered Russ to bring up his kid earlier."
    )
    asyncio.run(dmn._tick())
    assert dmn._suppressed_count == 0
    assert len(dmn._recent_thoughts) == 2
    assert dmn._bus.publish_dict.await_count == 1


def test_recent_thoughts_shown_to_LLM_in_prompt():
    dmn = _make_dmn()
    dmn._recent_thoughts.append("First prior thought about voices.")
    dmn._recent_thoughts.append("Second prior thought about Hebbian wiring.")
    dmn._monologue_cell.call = AsyncMock(return_value="Something brand new.")
    asyncio.run(dmn._tick())
    # The call's user-content arg should include both prior thoughts
    args = dmn._monologue_cell.call.call_args
    user_content = args[0][0][0]["content"]
    assert "First prior thought about voices." in user_content
    assert "Second prior thought about Hebbian wiring." in user_content
    assert "do not repeat" in user_content.lower() or "different move" in user_content.lower()


def test_recent_thoughts_window_caps_at_configured_size():
    dmn = _make_dmn()
    # Use semantically distinct thoughts so the dedup gate doesn't kill them.
    # maxlen=5; push 7 distinct thoughts and verify only the latest 5 remain.
    thoughts = [
        "Curious about how Russ structures his afternoon work.",
        "Wondering whether Ableton tasks would benefit from caching.",
        "Reflecting on the surprising satisfaction of fixing voice latency.",
        "Considering whether memory consolidation runs often enough.",
        "Noticing my predictions about user emotion have been off lately.",
        "Imagining what music Russ might pick if asked unexpectedly.",
        "Suspecting the Hebbian weights need more diverse training paths.",
    ]
    for t in thoughts:
        dmn._monologue_cell.call = AsyncMock(return_value=t)
        asyncio.run(dmn._tick())
    assert len(dmn._recent_thoughts) == 5
    # Oldest two should be evicted
    assert thoughts[0] not in dmn._recent_thoughts
    assert thoughts[1] not in dmn._recent_thoughts
    assert thoughts[-1] in dmn._recent_thoughts


def test_empty_response_does_not_record():
    dmn = _make_dmn()
    dmn._monologue_cell.call = AsyncMock(return_value="")
    asyncio.run(dmn._tick())
    assert len(dmn._recent_thoughts) == 0
    assert dmn._bus.publish_dict.await_count == 0
    assert dmn._suppressed_count == 0


def test_suppressed_thought_does_not_join_recent():
    """A suppressed (duplicate) thought must NOT be added to _recent_thoughts —
    otherwise the same content could perpetually re-suppress new variations."""
    dmn = _make_dmn()
    dmn._recent_thoughts.append("The audio is finally working correctly.")
    dmn._monologue_cell.call = AsyncMock(
        return_value="The audio is finally working correctly now."
    )
    asyncio.run(dmn._tick())
    assert len(dmn._recent_thoughts) == 1  # not added
    assert dmn._suppressed_count == 1


def test_memory_seed_injected_when_idle_and_on_interval():
    """Every Nth idle tick, a random episode is surfaced into _memory_seed."""
    import brain.dmn as dmn_mod
    dmn = _make_dmn()
    dmn._memory_seed = ""
    dmn._hippocampus = MagicMock()
    dmn._hippocampus._episodic.sample_random = MagicMock(return_value=[{
        "user_input": "can you reuse the pitch detection?",
        "entity_response": "yes, the Karaoke module already does it.",
        "topic_tags": ["karaoke-hero", "pitch-detection"],
    }])
    dmn._thought_count = dmn_mod.DMN_MEMORY_SEED_EVERY  # lands on the interval
    # Force "idle" so the gate allows surfacing.
    orig = dmn_mod.get_idle_seconds
    dmn_mod.get_idle_seconds = lambda: 999.0
    try:
        dmn._maybe_inject_memory_seed()
    finally:
        dmn_mod.get_idle_seconds = orig
    assert "pitch detection" in dmn._memory_seed
    assert "karaoke-hero" in dmn._memory_seed


def test_memory_seed_skipped_when_user_active():
    import brain.dmn as dmn_mod
    dmn = _make_dmn()
    dmn._memory_seed = ""
    dmn._hippocampus = MagicMock()
    dmn._hippocampus._episodic.sample_random = MagicMock(return_value=[{
        "user_input": "x", "entity_response": "y", "topic_tags": [],
    }])
    dmn._thought_count = dmn_mod.DMN_MEMORY_SEED_EVERY
    orig = dmn_mod.get_idle_seconds
    dmn_mod.get_idle_seconds = lambda: 5.0  # user present
    try:
        dmn._maybe_inject_memory_seed()
    finally:
        dmn_mod.get_idle_seconds = orig
    assert dmn._memory_seed == ""
    dmn._hippocampus._episodic.sample_random.assert_not_called()


def test_frame_repetition_gate_catches_template_collapse():
    """Template collapse — same opening frame, different topic noun — slips past
    the word-overlap and cosine gates but must be caught by the frame gate."""
    dmn = _make_dmn()
    # Three thoughts that share the 'i should INQUIRE' frame with distinct nouns
    # (so word-overlap stays low and the gates that catch wording don't fire).
    templates = [
        "I should investigate recent papers on voice diarization quality.",
        "I should explore recent studies on Hebbian plasticity dynamics.",
        "I should consider recent research on episodic memory consolidation.",
    ]
    suppressed_before = 0
    for i, t in enumerate(templates):
        dmn._monologue_cell.call = AsyncMock(return_value=t)
        asyncio.run(dmn._tick())
        if i < 2:
            # First two share the frame but are under the repeat ceiling → pass
            assert dmn._suppressed_count == 0, f"thought {i} wrongly suppressed"
    # The third repetition of the same frame must be suppressed.
    assert dmn._suppressed_count == 1
    assert templates[2] not in dmn._recent_thoughts

    # A genuinely different frame still passes right after.
    dmn._monologue_cell.call = AsyncMock(
        return_value="Why did Russ go quiet about the Karaoke project?"
    )
    asyncio.run(dmn._tick())
    assert dmn._suppressed_count == 1  # unchanged — the new frame passed
    assert any("Karaoke" in t for t in dmn._recent_thoughts)
