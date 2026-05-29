"""
Phase 3: Anticipatory DMN. When the brain's last response ended with a
question, the DMN's anticipator pre-thinks 2-3 likely user answers and
sketches a response for each. Surfaced to next turn's drafter as
'scenarios you pre-thought'.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from brain.clusters.frontal import FrontalCluster

# ── note_last_response heuristic ─────────────────────────────────────────


def _make_dmn():
    from brain.dmn import DefaultModeNetwork

    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._bus = MagicMock()
    dmn._bus.publish_dict = AsyncMock()
    dmn._anticipator_cell = MagicMock()
    dmn._anticipator_cell.reset_turn = MagicMock()
    dmn._anticipator_cell.call = AsyncMock(return_value="{}")
    dmn._last_context = ""
    dmn.last_was_question = False
    dmn.last_assistant_message = ""
    dmn.anticipations = []
    return dmn


def test_note_last_response_detects_question_mark():
    dmn = _make_dmn()
    dmn.note_last_response("What's your favourite ableton plugin?")
    assert dmn.last_was_question is True
    assert dmn.last_assistant_message.endswith("?")


def test_note_last_response_detects_tag_questions():
    dmn = _make_dmn()
    dmn.note_last_response("Sounds like you fixed it, yeah?")
    assert dmn.last_was_question is True

    dmn2 = _make_dmn()
    dmn2.note_last_response("That's the issue, right?")
    assert dmn2.last_was_question is True


def test_note_last_response_no_question_for_statement():
    dmn = _make_dmn()
    dmn.note_last_response("Got it, noting that for next time.")
    assert dmn.last_was_question is False


def test_note_last_response_clears_stale_anticipations():
    dmn = _make_dmn()
    dmn.anticipations = [{"user_answer": "stale", "response_sketch": "old"}]
    dmn.note_last_response("Anything else?")
    # New turn arriving = previous round's anticipations no longer relevant
    assert dmn.anticipations == []


# ── take_anticipations ───────────────────────────────────────────────────


def test_take_anticipations_returns_and_clears():
    dmn = _make_dmn()
    dmn.anticipations = [
        {"user_answer": "yes", "response_sketch": "ok then"},
        {"user_answer": "no", "response_sketch": "alright"},
    ]
    taken = dmn.take_anticipations()
    assert len(taken) == 2
    assert dmn.anticipations == []


def test_take_anticipations_empty():
    dmn = _make_dmn()
    assert dmn.take_anticipations() == []


# ── anticipator LLM tick ─────────────────────────────────────────────────


def test_run_anticipator_parses_scenarios():
    import json

    dmn = _make_dmn()
    dmn.last_assistant_message = "What kind of plugin do you mean?"
    dmn._anticipator_cell.call = AsyncMock(
        return_value=json.dumps(
            {
                "scenarios": [
                    {
                        "user_answer": "synth",
                        "response_sketch": "Try Wavetable or Operator",
                        "context_needed": [],
                    },
                    {
                        "user_answer": "effects",
                        "response_sketch": "Reverb or Echo are good",
                        "context_needed": ["which Ableton version"],
                    },
                ]
            }
        )
    )
    asyncio.run(dmn._run_anticipator(turn_id="t1"))
    assert len(dmn.anticipations) == 2
    assert dmn.anticipations[0]["user_answer"] == "synth"
    assert "Wavetable" in dmn.anticipations[0]["response_sketch"]
    assert dmn.anticipations[1]["context_needed"] == ["which Ableton version"]


def test_run_anticipator_caps_at_three_scenarios():
    import json

    dmn = _make_dmn()
    dmn._anticipator_cell.call = AsyncMock(
        return_value=json.dumps(
            {
                "scenarios": [
                    {"user_answer": f"answer {i}", "response_sketch": f"sketch {i}"}
                    for i in range(7)
                ]
            }
        )
    )
    asyncio.run(dmn._run_anticipator(turn_id="t1"))
    assert len(dmn.anticipations) == 3


def test_run_anticipator_skips_incomplete_scenarios():
    import json

    dmn = _make_dmn()
    dmn._anticipator_cell.call = AsyncMock(
        return_value=json.dumps(
            {
                "scenarios": [
                    {"user_answer": "valid", "response_sketch": "sketch"},
                    {"user_answer": "missing sketch"},
                    {"response_sketch": "missing answer"},
                    {},
                ]
            }
        )
    )
    asyncio.run(dmn._run_anticipator(turn_id="t1"))
    assert len(dmn.anticipations) == 1
    assert dmn.anticipations[0]["user_answer"] == "valid"


def test_run_anticipator_survives_bad_json():
    dmn = _make_dmn()
    dmn._anticipator_cell.call = AsyncMock(return_value="not json at all")
    asyncio.run(dmn._run_anticipator(turn_id="t1"))
    # Should not raise; just leaves anticipations empty
    assert dmn.anticipations == []


# ── Drafter prompt surfaces anticipations ───────────────────────────────


def _frontal_skel():
    f = FrontalCluster.__new__(FrontalCluster)
    f._capabilities_summary = ""
    return f


def test_drafter_prompt_includes_anticipations():
    f = _frontal_skel()
    prompt = f._build_drafter_prompt(
        features={"raw_text": "synth"},
        memory={
            "anticipations": [
                {"user_answer": "synth", "response_sketch": "Try Wavetable or Operator"},
                {"user_answer": "effects", "response_sketch": "Reverb is good"},
            ]
        },
        parietal="",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction={
            "response_type": "chitchat",
            "target_length": "brief",
            "tone": "warm",
            "key_points": [],
            "drafter_count": 1,
        },
    )
    assert "Scenarios you pre-thought" in prompt
    assert "Wavetable" in prompt
    assert "Reverb" in prompt


def test_drafter_prompt_omits_anticipations_when_none():
    f = _frontal_skel()
    prompt = f._build_drafter_prompt(
        features={"raw_text": "synth"},
        memory={},
        parietal="",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction={
            "response_type": "chitchat",
            "target_length": "brief",
            "tone": "warm",
            "key_points": [],
            "drafter_count": 1,
        },
    )
    assert "pre-thought" not in prompt
