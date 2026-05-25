"""
Tests for DMN feedback into the brain's response pipeline:
  Phase 1 — recent_thoughts() accessor + drafter-prompt surfacing
  Phase 2a — stream.prediction → temporal predictor (covered separately)
  Phase 2b — useful idle thought → episode (covered separately)
"""
from __future__ import annotations

from collections import deque

from brain.clusters.frontal import FrontalCluster

# ── DMN.recent_thoughts() accessor ────────────────────────────────────────

def test_dmn_recent_thoughts_returns_list():
    from brain.dmn import DefaultModeNetwork
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._recent_thoughts = deque(["a", "b", "c"], maxlen=5)
    assert dmn.recent_thoughts() == ["a", "b", "c"]


def test_dmn_recent_thoughts_caps_at_n():
    from brain.dmn import DefaultModeNetwork
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._recent_thoughts = deque(["1", "2", "3", "4", "5"], maxlen=5)
    assert dmn.recent_thoughts(n=2) == ["4", "5"]
    assert dmn.recent_thoughts(n=10) == ["1", "2", "3", "4", "5"]


def test_dmn_recent_thoughts_empty():
    from brain.dmn import DefaultModeNetwork
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._recent_thoughts = deque(maxlen=5)
    assert dmn.recent_thoughts() == []


# ── Drafter prompt surfaces recent_thoughts in memory dict ────────────────

def _frontal_skel():
    f = FrontalCluster.__new__(FrontalCluster)
    f._capabilities_summary = ""
    return f


def test_drafter_prompt_includes_recent_thoughts_when_provided():
    f = _frontal_skel()
    prompt = f._build_drafter_prompt(
        features={"raw_text": "hi"},
        memory={"recent_thoughts": [
            "Wondering whether Russ has finished debugging the audio.",
            "Considering how memory consolidation runs at session end.",
        ]},
        parietal="",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction={"response_type": "chitchat", "target_length": "brief",
                     "tone": "warm", "key_points": [], "drafter_count": 1},
    )
    assert "Idle thoughts you had between turns" in prompt
    assert "Wondering whether Russ" in prompt
    assert "memory consolidation" in prompt


def test_drafter_prompt_omits_thoughts_block_when_none():
    f = _frontal_skel()
    prompt = f._build_drafter_prompt(
        features={"raw_text": "hi"},
        memory={},  # no recent_thoughts
        parietal="",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction={"response_type": "chitchat", "target_length": "brief",
                     "tone": "warm", "key_points": [], "drafter_count": 1},
    )
    assert "Idle thoughts" not in prompt


def test_drafter_prompt_filters_empty_thoughts():
    f = _frontal_skel()
    prompt = f._build_drafter_prompt(
        features={"raw_text": "hi"},
        memory={"recent_thoughts": ["real thought", "", "  ", "another real one"]},
        parietal="",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction={"response_type": "chitchat", "target_length": "brief",
                     "tone": "warm", "key_points": [], "drafter_count": 1},
    )
    assert "real thought" in prompt
    assert "another real one" in prompt
    # Empty-string lines shouldn't produce '- ' bullets with empty content
    assert "- \n" not in prompt
