"""
Tests for FrontalCluster.set_capabilities() — ensures that the entity's
actual tool capabilities are surfaced into the drafter prompt so drafters
can answer "what tools do you have?" accurately instead of confabulating.
"""
from __future__ import annotations

from brain.clusters.frontal import FrontalCluster


def _make_frontal_skeleton():
    """Build a FrontalCluster skeleton bypassing __init__ — we only need
    the prompt-building methods and the _capabilities_summary attribute."""
    cluster = FrontalCluster.__new__(FrontalCluster)
    cluster._capabilities_summary = ""
    return cluster


def test_capabilities_starts_empty():
    f = _make_frontal_skeleton()
    assert f._capabilities_summary == ""


def test_set_capabilities_stores_string():
    f = _make_frontal_skeleton()
    f.set_capabilities("can read files, can ask Claude")
    assert f._capabilities_summary == "can read files, can ask Claude"


def test_set_capabilities_strips_whitespace():
    f = _make_frontal_skeleton()
    f.set_capabilities("  \n  capable\n  ")
    assert f._capabilities_summary == "capable"


def test_set_capabilities_none_becomes_empty():
    f = _make_frontal_skeleton()
    f.set_capabilities(None)  # type: ignore[arg-type]
    assert f._capabilities_summary == ""


def test_drafter_prompt_includes_capabilities_when_set():
    f = _make_frontal_skeleton()
    f.set_capabilities("Tool use ENABLED. Can read files. Can invoke Claude Code.")
    # _build_drafter_prompt needs other methods; call it with minimal args
    prompt = f._build_drafter_prompt(
        features={"raw_text": "hi"},
        memory={},
        parietal="",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction={"response_type": "chitchat", "target_length": "brief",
                     "tone": "warm", "key_points": [], "drafter_count": 1},
    )
    assert "Your capabilities this session" in prompt
    assert "Tool use ENABLED" in prompt
    assert "Claude Code" in prompt


def test_drafter_prompt_omits_capabilities_section_when_empty():
    f = _make_frontal_skeleton()
    # default: _capabilities_summary == ""
    prompt = f._build_drafter_prompt(
        features={"raw_text": "hi"},
        memory={},
        parietal="",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction={"response_type": "chitchat", "target_length": "brief",
                     "tone": "warm", "key_points": [], "drafter_count": 1},
    )
    assert "Your capabilities this session" not in prompt


def test_drafter_prompt_capabilities_appears_before_other_context():
    """If both capabilities and parietal context are present, capabilities
    should come first so it's prominent in the drafter's context window."""
    f = _make_frontal_skeleton()
    f.set_capabilities("Tool use ENABLED")
    prompt = f._build_drafter_prompt(
        features={"raw_text": "hi"},
        memory={},
        parietal="prior turn 1\nprior turn 2",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction={"response_type": "chitchat", "target_length": "brief",
                     "tone": "warm", "key_points": [], "drafter_count": 1},
    )
    cap_pos = prompt.find("Your capabilities this session")
    parietal_pos = prompt.find("Recent conversation")
    assert cap_pos >= 0 and parietal_pos >= 0
    assert cap_pos < parietal_pos
