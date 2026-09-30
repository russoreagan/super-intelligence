"""ProcedureStore tags every vector with the embed model that produced it.

The 2026-09-13 switch from gemini-embedding-001 to nomic-embed-text left both
768-dim spaces in the untagged LanceDB `procedures` table, so recall compared a
nomic query against Google vectors. Real LanceDB in a temp dir: the point is the
on-disk migration of a table written before the column existed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import lancedb
import pyarrow as pa
import pytest

from brain.clusters import motor_memory as mm

DIM = mm._EMBEDDING_DIM


def _v(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


@pytest.fixture
def episodes(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "_EPISODES_DIR", tmp_path)
    _use_model(monkeypatch, "nomic")
    return tmp_path


def _use_model(monkeypatch, name):
    monkeypatch.setattr(mm.ModelRouter, "embed_model_name", staticmethod(lambda: name))


def _legacy_table(path, goal="old goal", vec=None):
    """The pre-tag schema, holding one row with a vector from the old model."""
    schema = pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("goal", pa.string()),
            pa.field("steps", pa.string()),
            pa.field("results", pa.string()),
            pa.field("success", pa.bool_()),
            pa.field("recorded_at", pa.string()),
            pa.field("use_count", pa.int32()),
            pa.field("vector", pa.list_(pa.float32(), DIM)),
        ]
    )
    t = lancedb.connect(str(path)).create_table("procedures", schema=schema)
    t.add(
        [
            {
                "id": "old1",
                "goal": goal,
                "steps": '[{"tool": "read_file", "args": {}}]',
                "results": '["ok"]',
                "success": True,
                "recorded_at": "2026-09-01T00:00:00+00:00",
                "use_count": 3,
                "vector": vec or _v(0),
            }
        ]
    )


def _embedder(mapping):
    async def embed(text):
        return mapping.get(text)

    return embed


def test_legacy_rows_are_foreign_until_reembedded(episodes):
    _legacy_table(episodes)
    s = mm.ProcedureStore()
    assert s.recall(_v(0)) == []  # untagged vector is never compared
    assert asyncio.run(s.reembed_foreign(_embedder({"old goal": _v(1)}))) == 1
    got = s.recall(_v(1))
    assert [r["id"] for r in got] == ["old1"] and got[0]["embed_model"] == "nomic"
    assert got[0]["use_count"] == 3  # history kept, only the vector changed
    assert s.recall(_v(0)) == []


def test_reembed_leaves_rows_alone_when_chain_is_down(episodes):
    _legacy_table(episodes)
    s = mm.ProcedureStore()
    assert asyncio.run(s.reembed_foreign(_embedder({}))) == 0
    row = s._table.search().limit(1).to_list()[0]
    assert row["embed_model"] is None and row["vector"][0] == 1.0
    assert asyncio.run(s.reembed_foreign(_embedder({"old goal": _v(1)}))) == 1


def test_other_model_rows_ignored_and_unembedded_rows_filled_later(episodes, monkeypatch):
    s = mm.ProcedureStore()
    step = [{"tool": "read_file", "args": {}}]
    _use_model(monkeypatch, "gemini")
    s.save("google goal", step, ["ok"], True, _v(2))
    _use_model(monkeypatch, "nomic")
    s.save("chain down goal", step, ["ok"], True, [])
    assert s.recall(_v(2)) == []  # same vector, other model's space
    n = asyncio.run(s.reembed_foreign(_embedder({"google goal": _v(3), "chain down goal": _v(4)})))
    assert n == 2
    assert [r["goal"] for r in s.recall(_v(4))] == ["chain down goal"]


def test_before_plan_reembeds_then_recalls(episodes, monkeypatch):
    monkeypatch.setattr(mm, "_isolated_non_home", lambda: False)
    _legacy_table(episodes, goal="summarise the repo")
    sub = mm.MuscleMemorySubsystem()
    router = MagicMock()
    router.embed = _embedder({"summarise the repo": _v(5)})
    out = asyncio.run(sub.before_plan("summarise the repo", router))
    assert '"summarise the repo"' in out and "similarity: 1.0" in out
