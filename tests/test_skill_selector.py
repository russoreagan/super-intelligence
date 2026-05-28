"""
Unit tests for SkillSelector — the conversational/autonomous skill router.

These tests stub out LLM calls but use the real embedding index produced by
brain/skills/_import_humanity.py. The index must exist for tests to run.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from brain.clusters.skill_selector import (
    INDEX_PATH,
    ActiveSkillContext,
    SkillSelector,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

INDEX_AVAILABLE = INDEX_PATH.exists()
pytestmark = pytest.mark.skipif(
    not INDEX_AVAILABLE,
    reason="brain/skills/_humanity_index.json missing — run `python -m brain.skills._import_humanity` first.",
)


def _stub_router():
    """Minimal router stub — no HTTP, no Ollama. Embed returns canned vectors."""
    from types import SimpleNamespace
    router = SimpleNamespace()
    router.embed = AsyncMock(return_value=None)
    return router


def _selector_with_stubbed_cells():
    """Construct a SkillSelector but replace its LLM cells with mocks so no
    network calls happen during tests."""
    s = SkillSelector(_stub_router())
    s._conversational_cell.call = AsyncMock(return_value='{"skill": null, "why": "test"}')
    s._autonomous_cell.call = AsyncMock(return_value='{"primary": null, "secondary": null}')
    s._meta_cell.call = AsyncMock(return_value='{"mode": "stop", "skill": null, "base_idx": 0}')
    return s


def _embedding_for(selector: SkillSelector, name: str) -> list[float]:
    """Pull the precomputed embedding for a known skill from the index."""
    e = selector.get_skill(name)
    if e is None:
        raise KeyError(f"{name} not in index")
    return e["embedding"]


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

def test_gate_chitchat_off():
    s = _selector_with_stubbed_cells()
    assert s.gate_conversational(response_type="chitchat", user_emotion="") is False


def test_gate_informative_on():
    s = _selector_with_stubbed_cells()
    assert s.gate_conversational(response_type="informative") is True


def test_gate_user_emotion_on_even_for_chitchat():
    s = _selector_with_stubbed_cells()
    assert s.gate_conversational(response_type="chitchat", user_emotion="anxious") is True


# ---------------------------------------------------------------------------
# Gated-out path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_simple_hi_gates_out():
    s = _selector_with_stubbed_cells()
    bundle, active, extras = await s.select_conversational(
        user_input="hi",
        executive_out={"response_type": "chitchat", "tone": "warm"},
        user_emotion="",
        recent_turns=[],
        active=None,
    )
    assert bundle is None
    assert extras.get("gated") is True


# ---------------------------------------------------------------------------
# Tier 1 baseline
# ---------------------------------------------------------------------------

def test_tier1_contains_expected_names():
    s = _selector_with_stubbed_cells()
    assert set(s.tier1_names) == {
        "logic-check", "communication-clarity-audit", "ethics-bias-check", "emotional",
    }


# ---------------------------------------------------------------------------
# Index ranking (embedding-only, no LLM)
# ---------------------------------------------------------------------------

def test_index_ranks_skill_against_itself_highest():
    """The skill that matches the query exactly (by reusing its own embedding)
    should rank as the top candidate."""
    s = _selector_with_stubbed_cells()
    query_vec = _embedding_for(s, "decision-criteria-weighting")
    ranked = s._index.rank(query_vec, include_tier_1=False)
    top_names = [r[0]["name"] for r in ranked[:3]]
    assert "decision-criteria-weighting" in top_names


def test_tier3_filter_kicks_in_below_threshold():
    """Tier 3 skills are excluded when cosine < TIER_3_MIN_SCORE."""
    s = _selector_with_stubbed_cells()
    # Construct an orthogonal query so nothing scores high
    dim = len(_embedding_for(s, "logic-check"))
    query_vec = [0.0] * dim
    query_vec[0] = 1.0
    ranked = s._index.rank(query_vec, include_tier_1=False)
    for entry, score in ranked:
        if entry["tier"] == 3:
            # Either no tier-3 made it, OR the ones that did cleared the threshold.
            assert score >= 0.55


# ---------------------------------------------------------------------------
# Sticky-context drift
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_active_context_drift_clears_when_topic_changes():
    s = _selector_with_stubbed_cells()
    # Active context anchored to "decision-premortem-analysis"
    anchor_vec = _embedding_for(s, "decision-premortem-analysis")
    active = ActiveSkillContext(
        category="decision",
        current_leaf="decision-premortem-analysis",
        anchor_topic_embedding=anchor_vec,
        turns_active=1,
    )
    # Query that's far from the anchor — use an orthogonal vector
    dim = len(anchor_vec)
    far_vec = [0.0] * dim
    far_vec[0] = 1.0
    s._router.embed = AsyncMock(return_value=far_vec)

    bundle, updated, extras = await s.select_conversational(
        user_input="weather",
        executive_out={"response_type": "informative"},
        user_emotion="",
        recent_turns=[],
        active=active,
    )
    # Sticky context should have been dropped (drift below threshold)
    assert extras.get("sticky_action") in {"drifted_or_expired", None}


@pytest.mark.asyncio
async def test_active_context_reused_when_topic_persists():
    s = _selector_with_stubbed_cells()
    anchor_vec = _embedding_for(s, "decision-premortem-analysis")
    active = ActiveSkillContext(
        category="decision",
        current_leaf="decision-premortem-analysis",
        anchor_topic_embedding=anchor_vec,
        turns_active=1,
    )
    # Query that perfectly matches the anchor
    s._router.embed = AsyncMock(return_value=anchor_vec)
    bundle, updated, extras = await s.select_conversational(
        user_input="what could go wrong?",
        executive_out={"response_type": "task"},
        user_emotion="",
        recent_turns=[],
        active=active,
    )
    assert bundle is not None
    assert bundle.pick_source == "active_reuse"
    assert "decision-premortem-analysis" in bundle.chosen
    assert extras.get("sticky_action") == "reused"


# ---------------------------------------------------------------------------
# Leaf lock-in after a guided question
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lock_leaf_from_reply_picks_leaf_in_active_category():
    s = _selector_with_stubbed_cells()
    # Active router-level context awaiting direction under the "logic" category
    active = ActiveSkillContext(
        category="logic",
        current_leaf=None,
        anchor_topic_embedding=_embedding_for(s, "logic"),
        awaiting_user_direction=True,
    )
    # User reply that semantically matches argument validation
    s._router.embed = AsyncMock(return_value=_embedding_for(s, "logic-argument-validation"))
    updated = await s.lock_leaf_from_reply("check the argument structure", active)
    assert updated.current_leaf is not None
    assert updated.current_leaf.startswith("logic")
    assert updated.awaiting_user_direction is False


# ---------------------------------------------------------------------------
# Background-explore writes candidate list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_background_explore_writes_candidates():
    s = _selector_with_stubbed_cells()
    active = ActiveSkillContext(
        category="decision",
        current_leaf="decision-criteria-weighting",
        anchor_topic_embedding=_embedding_for(s, "decision-criteria-weighting"),
    )
    s._router.embed = AsyncMock(return_value=_embedding_for(s, "decision-criteria-weighting"))
    result = await s.background_explore(active, "weighing options")
    assert isinstance(result, list)
    assert len(result) <= 5
    assert active.background_candidates == result
