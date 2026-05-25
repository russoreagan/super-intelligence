"""
Phase 3b: Proactive pre-fetcher. The DMN identifies topics likely to come
back up, queries hippocampus for related memory, and caches it as
prefetched_context for the next turn's drafter.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from brain.clusters.frontal import FrontalCluster


def _make_dmn():
    from brain.dmn import DefaultModeNetwork
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._bus = MagicMock()
    dmn._bus.publish_dict = AsyncMock()
    dmn._router = MagicMock()
    dmn._router.embed = AsyncMock(return_value=[0.0] * 16)

    dmn._prefetcher_cell = MagicMock()
    dmn._prefetcher_cell.reset_turn = MagicMock()
    dmn._prefetcher_cell.call = AsyncMock(return_value="{}")

    dmn._hippocampus = MagicMock()
    dmn._hippocampus.recall = AsyncMock(return_value={
        "episodes": "episode text about audio bleed",
        "schema": "schema entry for audio bleed",
        "core": {},
    })

    dmn._last_context = "User has been debugging audio bleed."
    dmn.prefetched = []
    return dmn


def test_take_prefetched_returns_and_clears():
    dmn = _make_dmn()
    dmn.prefetched = [{"topic": "audio bleed", "snippets": "stuff"}]
    taken = dmn.take_prefetched()
    assert taken == [{"topic": "audio bleed", "snippets": "stuff"}]
    assert dmn.prefetched == []


def test_take_prefetched_empty():
    dmn = _make_dmn()
    assert dmn.take_prefetched() == []


def test_prefetcher_runs_each_query_through_hippocampus():
    dmn = _make_dmn()
    dmn._prefetcher_cell.call = AsyncMock(return_value=json.dumps({
        "queries": [
            {"topic": "audio bleed troubleshooting", "reason": "open thread"},
            {"topic": "Ableton plugins", "reason": "user mentioned earlier"},
        ]
    }))
    asyncio.run(dmn._run_prefetcher(turn_id="t1"))
    assert dmn._hippocampus.recall.await_count == 2
    assert len(dmn.prefetched) == 2
    assert dmn.prefetched[0]["topic"] == "audio bleed troubleshooting"


def test_prefetcher_caps_at_three_queries():
    dmn = _make_dmn()
    dmn._prefetcher_cell.call = AsyncMock(return_value=json.dumps({
        "queries": [
            {"topic": f"topic_{i}", "reason": "r"} for i in range(7)
        ]
    }))
    asyncio.run(dmn._run_prefetcher(turn_id="t1"))
    assert dmn._hippocampus.recall.await_count == 3
    assert len(dmn.prefetched) == 3


def test_prefetcher_skips_empty_topics():
    dmn = _make_dmn()
    dmn._prefetcher_cell.call = AsyncMock(return_value=json.dumps({
        "queries": [
            {"topic": "real topic", "reason": "r"},
            {"topic": "", "reason": "r"},
            {"reason": "r"},   # no topic key
        ]
    }))
    asyncio.run(dmn._run_prefetcher(turn_id="t1"))
    assert dmn._hippocampus.recall.await_count == 1
    assert len(dmn.prefetched) == 1


def test_prefetcher_drops_queries_with_empty_memory_results():
    dmn = _make_dmn()
    # Hippocampus returns empty everything → nothing to prefetch
    dmn._hippocampus.recall = AsyncMock(return_value={
        "episodes": "", "schema": "", "core": {},
    })
    dmn._prefetcher_cell.call = AsyncMock(return_value=json.dumps({
        "queries": [{"topic": "ghost topic", "reason": "r"}]
    }))
    asyncio.run(dmn._run_prefetcher(turn_id="t1"))
    assert dmn.prefetched == []


def test_prefetcher_survives_bad_json():
    dmn = _make_dmn()
    dmn._prefetcher_cell.call = AsyncMock(return_value="garbage")
    asyncio.run(dmn._run_prefetcher(turn_id="t1"))
    assert dmn.prefetched == []
    assert dmn._hippocampus.recall.await_count == 0


def test_prefetcher_survives_hippocampus_failure():
    dmn = _make_dmn()
    dmn._hippocampus.recall = AsyncMock(side_effect=RuntimeError("db down"))
    dmn._prefetcher_cell.call = AsyncMock(return_value=json.dumps({
        "queries": [{"topic": "x", "reason": "r"}]
    }))
    # Should not raise
    asyncio.run(dmn._run_prefetcher(turn_id="t1"))
    assert dmn.prefetched == []


# ── Drafter prompt surfaces prefetched_context ──────────────────────────

def _frontal_skel():
    f = FrontalCluster.__new__(FrontalCluster)
    f._capabilities_summary = ""
    return f


def test_drafter_prompt_includes_prefetched_context():
    f = _frontal_skel()
    prompt = f._build_drafter_prompt(
        features={"raw_text": "what was that?"},
        memory={"prefetched_context": [
            {"topic": "audio bleed", "snippets": "we discussed bleed earlier"},
            {"topic": "ableton synths", "snippets": "Wavetable is good"},
        ]},
        parietal="",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction={"response_type": "chitchat", "target_length": "brief",
                     "tone": "warm", "key_points": [], "drafter_count": 1},
    )
    assert "proactively pulled" in prompt
    assert "audio bleed" in prompt
    assert "Wavetable" in prompt


def test_drafter_prompt_omits_prefetched_when_none():
    f = _frontal_skel()
    prompt = f._build_drafter_prompt(
        features={"raw_text": "hello"},
        memory={},
        parietal="",
        affect={"emotion": "neutral", "appraisal": ""},
        instruction={"response_type": "chitchat", "target_length": "brief",
                     "tone": "warm", "key_points": [], "drafter_count": 1},
    )
    assert "proactively pulled" not in prompt
