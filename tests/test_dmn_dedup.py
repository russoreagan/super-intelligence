"""
Tests for DMN thought-deduplication:
  - recent thoughts are shown to the LLM in the prompt so it varies
  - near-duplicate output is suppressed via word-overlap (Jaccard)
  - genuinely different thoughts pass through and join the recent buffer
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

from brain.sequence_predictor import SequencePredictor


def _make_dmn():
    """Build a DefaultModeNetwork skeleton bypassing __init__."""
    from collections import deque

    from brain.dmn import DefaultModeNetwork

    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._seq_predictor = SequencePredictor()
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
    dmn._monologue_cell.call = AsyncMock(
        return_value="I'm noticing the audio bleed issue keeps coming up."
    )
    asyncio.run(dmn._tick())
    assert len(dmn._recent_thoughts) == 1
    assert dmn._recent_thoughts[0].startswith("I'm noticing")
    assert dmn._bus.publish_dict.await_count == 1
    assert dmn._suppressed_count == 0


def test_duplicate_thought_is_suppressed():
    dmn = _make_dmn()
    dmn._recent_thoughts.append("Audio bleed was killing the conversation flow every single time.")
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
    dmn._recent_thoughts.append("I keep thinking about how the user phrases tool requests.")
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
    dmn._monologue_cell.call = AsyncMock(return_value="The audio is finally working correctly now.")
    asyncio.run(dmn._tick())
    assert len(dmn._recent_thoughts) == 1  # not added
    assert dmn._suppressed_count == 1


def test_memory_seed_injected_when_idle_and_relevant():
    """Every Nth idle tick, a sampled episode RELATED to the live context is
    surfaced into _memory_seed."""
    import brain.dmn as dmn_mod

    dmn = _make_dmn()
    dmn._memory_seed = ""
    # Context shares content words with the episode below, so it clears the
    # relevance floor and gets surfaced.
    dmn._last_context = "Recent: working on novelty scoring for the recall gate"
    dmn._hippocampus = MagicMock()
    dmn._hippocampus._episodic.sample_random = MagicMock(
        return_value=[
            {
                "user_input": "can you reuse the pitch detection?",
                "entity_response": "yes, the recall gate already does it.",
                "topic_tags": ["recall-gate", "novelty-scoring"],
            }
        ]
    )
    dmn._thought_count = dmn_mod.DMN_MEMORY_SEED_EVERY  # lands on the interval
    # Force "idle" so the gate allows surfacing.
    dmn._tick_idle_s = 999.0
    dmn._tick_idle_phase = dmn._idle_phase(999.0)
    dmn._maybe_inject_memory_seed()
    assert "pitch detection" in dmn._memory_seed
    assert "recall-gate" in dmn._memory_seed


def test_memory_seed_skipped_when_irrelevant():
    """A sampled episode with no overlap with the live context is NOT surfaced —
    this is the relevance gate that keeps proactive thoughts grounded in the
    moment instead of drifting onto unrelated old material."""
    import brain.dmn as dmn_mod

    dmn = _make_dmn()
    dmn._memory_seed = ""
    dmn._last_context = "Recent: debugging the audio output device selection"
    dmn._hippocampus = MagicMock()
    dmn._hippocampus._episodic.sample_random = MagicMock(
        return_value=[
            {
                "user_input": "what's your favorite kind of poem?",
                "entity_response": "I'm partial to a tight little haiku.",
                "topic_tags": ["poetry", "haiku"],
            }
        ]
    )
    dmn._thought_count = dmn_mod.DMN_MEMORY_SEED_EVERY
    dmn._tick_idle_s = 999.0
    dmn._tick_idle_phase = dmn._idle_phase(999.0)
    dmn._maybe_inject_memory_seed()
    assert dmn._memory_seed == ""


def test_memory_seed_skipped_when_user_active():
    import brain.dmn as dmn_mod

    dmn = _make_dmn()
    dmn._memory_seed = ""
    dmn._hippocampus = MagicMock()
    dmn._hippocampus._episodic.sample_random = MagicMock(
        return_value=[
            {
                "user_input": "x",
                "entity_response": "y",
                "topic_tags": [],
            }
        ]
    )
    dmn._thought_count = dmn_mod.DMN_MEMORY_SEED_EVERY
    dmn._tick_idle_s = 5.0  # user present
    dmn._tick_idle_phase = dmn._idle_phase(5.0)
    dmn._maybe_inject_memory_seed()
    assert dmn._memory_seed == ""
    dmn._hippocampus._episodic.sample_random.assert_not_called()


def test_frame_repetition_gate_catches_template_collapse():
    """Template collapse — same opening frame, different topic noun — slips past
    the word-overlap and cosine gates but must be caught by the frame gate."""
    dmn = _make_dmn()
    # Four thoughts sharing the INQUIRE frame with distinct nouns (so word-overlap
    # stays low and the gates that catch wording don't fire). Deliberately varied
    # hedges/modals/inflections: before the frame normalization each of these
    # produced a DIFFERENT signature and the gate never fired at all.
    templates = [
        "I should investigate recent papers on voice diarization quality.",
        "Maybe I could explore recent studies on Hebbian plasticity dynamics.",
        "Perhaps examining recent research on episodic memory consolidation.",
        "It might be worth surveying recent work on sparse autoencoder features.",
    ]
    for i, t in enumerate(templates):
        dmn._monologue_cell.call = AsyncMock(return_value=t)
        asyncio.run(dmn._tick())
        if i < 3:
            # First three share the frame but are under the repeat ceiling → pass
            assert dmn._suppressed_count == 0, f"thought {i} wrongly suppressed"
    # The fourth occurrence of the same frame must be suppressed.
    assert dmn._suppressed_count == 1
    assert templates[3] not in dmn._recent_thoughts

    # A genuinely different frame still passes right after.
    dmn._monologue_cell.call = AsyncMock(
        return_value="Why did Russ go quiet about the flock-dynamics idea?"
    )
    asyncio.run(dmn._tick())
    assert dmn._suppressed_count == 1  # unchanged — the new frame passed
    assert any("flock-dynamics" in t for t in dmn._recent_thoughts)


def test_frame_signature_ignores_hedges_modals_and_inflection():
    """The regression the gate was built to catch and silently didn't: a modal
    swap, a leading hedge, or a gerund used to yield distinct signatures, so
    template collapse walked straight through."""
    from brain.dmn_dedup import _frame_signature

    same_frame = [
        "I should investigate the market data",
        "I could investigate the market data",
        "Maybe I should investigate the market data",
        "Perhaps exploring the shipping backlog would help",
        "It might be worth examining the retry logic",
    ]
    sigs = {_frame_signature(t) for t in same_frame}
    assert sigs == {"INQUIRE"}, f"hedged/inflected openers did not collapse: {sigs}"

    # Distinct rhetorical moves must still separate.
    assert _frame_signature("I keep wondering about the retention curve") == "WONDER"
    assert _frame_signature("Planning the migration seems premature") == "WANT"
    assert _frame_signature("I remembered the earlier voice bug") == "RECALL"


def _frame_escape_dmn(idle_phase):
    """A DMN parked one suppression short of the dedup escape hatch, with a fully
    grooved frame window."""
    from collections import deque

    from brain.dmn import DMN_FRAME_REPEAT_MAX

    dmn = _make_dmn()
    dmn._ensure_runtime_state()
    dmn._recent_thoughts.append("An earlier thought worth keeping.")
    dmn._recent_embeddings = deque([None], maxlen=5)
    dmn._recent_frames = deque(["INQUIRE"] * DMN_FRAME_REPEAT_MAX, maxlen=6)
    dmn._consec_suppressed = int(os.environ.get("BRAIN_DMN_SUPPRESS_ESCAPE", "5")) - 1
    dmn._tick_idle_phase = idle_phase
    dmn._open_threads = []
    return dmn


_ESCAPE_META = {
    "angle": "",
    "spoken_form": None,
    "task_goal": None,
    "is_propose": False,
    "is_plan": False,
    "defer_text": None,
    "defer_urgency": "low",
    "defer_tags": [],
    "chem_delta": {},
}


def test_frame_collapse_escape_queues_rumination_instead_of_wiping_memory():
    """The escape hatch's old answer to a groove was amnesia. When the groove is
    frame collapse and the agent is idle, the answer should be to go deeper: queue
    rumination and clear only the frame window, keeping the novelty memory."""
    from brain.dmn import IdlePhase

    dmn = _frame_escape_dmn(IdlePhase.WANDERING)
    asyncio.run(dmn._process_thought("I should investigate the pricing tiers.", _ESCAPE_META, "t1"))

    assert dmn._pending_frame_escape is True
    assert list(dmn._recent_frames) == []  # block lifted
    assert len(dmn._recent_thoughts) == 1  # novelty memory SURVIVES
    assert dmn._consec_suppressed == 0


def test_frame_collapse_escape_falls_back_to_clearing_when_not_idle():
    """Rumination can't run mid-conversation, so the escape must still fall through
    to the memory clear — otherwise suppression could become permanent silence."""
    from brain.dmn import IdlePhase

    dmn = _frame_escape_dmn(IdlePhase.ENGAGED)
    asyncio.run(dmn._process_thought("I should investigate the pricing tiers.", _ESCAPE_META, "t1"))

    assert dmn._pending_frame_escape is False
    assert list(dmn._recent_thoughts) == []  # full amnesia, as before
    assert dmn._consec_suppressed == 0


def test_frame_signature_falls_back_to_literal_prefix_without_a_frame_verb():
    """No recognized opening move → topic-specific literal prefix, which rarely
    repeats. The gate is intentionally near-inert here; the angle and
    cluster-saturation gates cover topic attractors."""
    from brain.dmn_dedup import _frame_signature

    assert _frame_signature("What caused the drop in signups") == "what caused drop"
    assert _frame_signature("Russ went quiet about flock dynamics") == "russ went quiet"
