"""Tests for the motor-cortex general-purpose task-skill selector.

Hybrid selection: cosine top-K for obvious matches; LLM menu-pick below a high
threshold; graceful no-op when the index or embedder is absent.
"""

from __future__ import annotations

import asyncio
import json

from brain.clusters.task_skill_selector import TaskSkillSelector


class FakeRouter:
    def __init__(self, embed_vec, call_return="NONE"):
        self._embed_vec = embed_vec
        self._call_return = call_return
        self.embed_calls = 0
        self.llm_calls = 0

    async def embed(self, text):
        self.embed_calls += 1
        return self._embed_vec

    async def call(self, *a, **k):
        self.llm_calls += 1
        return self._call_return


def _index(tmp_path, skills):
    p = tmp_path / "_task_skills_index.json"
    p.write_text(json.dumps({"skills": skills}))
    return p


def test_obvious_match_skips_llm(tmp_path):
    idx = _index(
        tmp_path,
        [
            {"name": "a", "category": "a", "description": "alpha", "embedding": [1.0, 0.0]},
            {"name": "b", "category": "b", "description": "beta", "embedding": [0.0, 1.0]},
        ],
    )
    r = FakeRouter(embed_vec=[1.0, 0.0])
    sel = TaskSkillSelector(r, index_path=idx)
    assert sel.available
    assert asyncio.run(sel.select("goal", k=1)) == ["a"]
    assert r.llm_calls == 0  # cosine was decisive


def test_below_threshold_falls_back_to_llm(tmp_path):
    idx = _index(
        tmp_path,
        [
            {"name": "a", "category": "a", "description": "x", "embedding": [1.0, -1.0]},
            {"name": "b", "category": "b", "description": "y", "embedding": [-1.0, 1.0]},
        ],
    )
    r = FakeRouter(embed_vec=[1.0, 1.0], call_return="b")  # cosine ~0 to both → fallback
    sel = TaskSkillSelector(r, index_path=idx)
    assert asyncio.run(sel.select("goal", k=2)) == ["b"]
    assert r.llm_calls == 1


def test_llm_pick_filters_hallucinated_names(tmp_path):
    idx = _index(
        tmp_path,
        [
            {"name": "a", "category": "a", "description": "x", "embedding": [1.0, -1.0]},
        ],
    )
    r = FakeRouter(embed_vec=[1.0, 1.0], call_return="not-a-real-skill, a")
    sel = TaskSkillSelector(r, index_path=idx)
    assert asyncio.run(sel.select("goal", k=2)) == ["a"]


def test_no_index_is_noop(tmp_path):
    sel = TaskSkillSelector(FakeRouter([1.0]), index_path=tmp_path / "missing.json")
    assert not sel.available
    assert asyncio.run(sel.select("goal")) == []


def test_embed_unavailable_returns_empty(tmp_path):
    idx = _index(
        tmp_path,
        [
            {"name": "a", "category": "a", "description": "x", "embedding": [1.0, 0.0]},
        ],
    )
    sel = TaskSkillSelector(FakeRouter(embed_vec=None), index_path=idx)
    assert asyncio.run(sel.select("goal")) == []
