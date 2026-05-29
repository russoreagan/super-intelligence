"""
Phase 2b: HippocampusCluster.encode_idle_thought() — encodes DMN's idle
thoughts as low-priority episodes when the user's actual input has high
word-overlap with what the brain was musing about (the thought was useful).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from brain.clusters.hippocampus import HippocampusCluster


def _make_hippocampus_skeleton():
    """Build a HippocampusCluster skeleton bypassing __init__."""
    h = HippocampusCluster.__new__(HippocampusCluster)
    h._episodic = MagicMock()
    h._episodic.encode = MagicMock()
    h._schema = MagicMock()
    return h


def test_encode_idle_thought_calls_episodic_store():
    h = _make_hippocampus_skeleton()
    asyncio.run(
        h.encode_idle_thought(
            session_id="test_sess",
            thought="Wondering whether Russ has finished the audio debugging.",
            overlap_with_user_input=0.65,
            user_input="ok the audio is finally fixed",
        )
    )
    assert h._episodic.encode.called
    episode = h._episodic.encode.call_args[0][0]
    # The thought becomes the entity_response of this synthetic episode
    assert "audio debugging" in episode.entity_response
    # Tagged so it can be filtered out / treated specially in recall
    assert "idle_thought" in episode.topic_tags
    assert "reinforced" in episode.topic_tags


def test_encode_idle_thought_empty_thought_is_noop():
    h = _make_hippocampus_skeleton()
    asyncio.run(
        h.encode_idle_thought(
            session_id="test_sess",
            thought="   ",
            overlap_with_user_input=0.9,
        )
    )
    assert not h._episodic.encode.called


def test_encode_idle_thought_surprise_inversely_tracks_overlap():
    """High overlap = the brain was very right to think this; surprise low.
    Low overlap = the thought was tangentially related; surprise higher.
    surprise = 1 - overlap, clamped to [0, 1]."""
    h = _make_hippocampus_skeleton()
    asyncio.run(
        h.encode_idle_thought(
            session_id="s",
            thought="x",
            overlap_with_user_input=0.9,
        )
    )
    ep_high_overlap = h._episodic.encode.call_args[0][0]
    assert ep_high_overlap.surprise_score == pytest.approx(0.1)

    h._episodic.encode.reset_mock()
    asyncio.run(
        h.encode_idle_thought(
            session_id="s",
            thought="x",
            overlap_with_user_input=0.4,
        )
    )
    ep_low_overlap = h._episodic.encode.call_args[0][0]
    assert ep_low_overlap.surprise_score == pytest.approx(0.6)


def test_encode_idle_thought_handles_no_user_input():
    h = _make_hippocampus_skeleton()
    asyncio.run(
        h.encode_idle_thought(
            session_id="s",
            thought="A standalone musing.",
            overlap_with_user_input=1.0,
            user_input="",
        )
    )
    episode = h._episodic.encode.call_args[0][0]
    # Marker text so future recall makes the source legible
    assert "idle thought" in episode.user_input.lower()


def test_encode_idle_thought_survives_embed_failure():
    """If the embedding function raises, encoding still proceeds with vec=None
    so the thought is at least stored even if not searchable."""
    h = _make_hippocampus_skeleton()

    async def _broken_embed(text):
        raise RuntimeError("embed service down")

    asyncio.run(
        h.encode_idle_thought(
            session_id="s",
            thought="Some thought.",
            overlap_with_user_input=0.5,
            embedding_fn=_broken_embed,
        )
    )
    assert h._episodic.encode.called
    episode = h._episodic.encode.call_args[0][0]
    assert episode.vector is None


def test_encode_idle_thought_survives_episodic_store_failure():
    """If episodic.encode() raises, the function logs and returns — never
    crashes the calling turn loop."""
    h = _make_hippocampus_skeleton()
    h._episodic.encode = MagicMock(side_effect=RuntimeError("disk full"))
    # Should not raise
    asyncio.run(
        h.encode_idle_thought(
            session_id="s",
            thought="Some thought.",
            overlap_with_user_input=0.5,
        )
    )
