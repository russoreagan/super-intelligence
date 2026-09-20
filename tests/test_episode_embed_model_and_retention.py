"""One embedding space per row, and idle thoughts that age out.

Two 768-dim models wrote into episodes.vector — Google's until 2026-09-13, Ollama's
nomic after — and nothing recorded which, so a nomic query ranked Google rows by a
distance that means nothing. Embeddings are now local-only and every write says
which model made it; the search RPCs compare only same-model rows (migration 044).
A failed embed stores NULL, never a zero vector: pgvector's cosine distance to zero
is NaN, so those rows could never be returned and nothing could tell them apart
from real ones.

Retention: the DMN writes an episode per deferred question and per conclusion while
nobody is talking. In prod they outnumbered real turns ten to one, so they age out
after dmn_idle_retention_days. Conversations, agent runs and sleep insights never do.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import brain.second_brain.store as st
from brain.second_brain.store import IDLE_EPISODE_MARKERS, Episode, EpisodicStore
from brain.settings import settings


class _Table:
    """Minimal supabase-py table double recording the built query."""

    def __init__(self, sink):
        self.sink = sink
        self.op = None

    def insert(self, row):
        self.sink["insert"] = row
        return self

    def delete(self):
        self.sink["delete"] = True
        return self

    def eq(self, col, val):
        self.sink.setdefault("eq", []).append((col, val))
        return self

    def lt(self, col, val):
        self.sink["lt"] = (col, val)
        return self

    def in_(self, col, vals):
        self.sink["in"] = (col, list(vals))
        return self

    def execute(self):
        return MagicMock(data=self.sink.get("returns", [{"id": 1}, {"id": 2}]))


def _store(sink, org="11111111-1111-1111-1111-111111111111"):
    s = EpisodicStore.__new__(EpisodicStore)
    s._use_supabase = True
    client = MagicMock()
    client.table = lambda name: _Table(sink)
    s._sb = lambda: (client, org)
    s._sb_persona = lambda: "the_analyst"
    return s


def _episode(vector):
    return Episode(
        session_id="s1",
        turn_id="t1",
        ts=time.time(),
        user_input="hello",
        entity_response="hi",
        topic_tags=[],
        emotion_state="neutral",
        user_emotion="neutral",
        entities=[],
        neuromod_snapshot={},
        surprise_score=0.0,
        vector=vector,
    )


def test_write_tags_the_model_that_made_the_vector():
    sink = {}
    _store(sink)._sb_encode(_episode([0.2] * st.EMBEDDING_DIM))
    row = sink["insert"]
    assert row["embed_model"] == st._embed_model_name() == "nomic-embed-text"
    assert row["vector"].startswith("[0.2,")


def test_failed_embed_stores_null_not_a_zero_vector():
    sink = {}
    _store(sink)._sb_encode(_episode(None))
    row = sink["insert"]
    assert row["vector"] is None and row["embed_model"] is None


def test_recall_asks_only_for_rows_from_this_model(monkeypatch):
    calls = {}
    s = EpisodicStore.__new__(EpisodicStore)
    s._use_supabase = True
    client = MagicMock()
    client.rpc = lambda name, params: MagicMock(
        execute=lambda: MagicMock(data=calls.setdefault(name, params) and [])
    )
    s._sb = lambda: (client, "org")
    s._sb_persona = lambda: "the_analyst"
    s._parse_rows = lambda rows: list(rows)
    s._sb_recall([0.1] * st.EMBEDDING_DIM, 3, None, None)
    s._sb_recall_by_tag([0.1] * st.EMBEDDING_DIM, "trading", 3, None)
    assert calls["match_episodes"]["embed_model_param"] == "nomic-embed-text"
    assert calls["match_episodes_by_tag"]["embed_model_param"] == "nomic-embed-text"


def test_prune_targets_only_idle_markers_older_than_the_window():
    sink = {}
    now = time.time()
    n = _store(sink).prune_idle_older_than(30)
    assert n == 2  # rows the delete returned
    assert sink["delete"] is True
    assert ("org_id", "11111111-1111-1111-1111-111111111111") in sink["eq"]
    # Org-wide: a persona that no longer runs must not keep its backlog forever.
    assert not any(col == "persona" for col, _ in sink["eq"])
    col, cutoff = sink["lt"]
    assert col == "ts" and abs(cutoff - (now - 30 * 86400)) < 5
    assert sink["in"] == ("user_input", list(IDLE_EPISODE_MARKERS))


def test_prune_is_off_at_zero_days():
    sink = {}
    assert _store(sink).prune_idle_older_than(0) == 0
    assert sink == {}


def test_idle_markers_match_what_the_dmn_actually_writes():
    """If hippocampus changes its marker text, retention silently stops working."""
    from pathlib import Path

    src = Path(st.__file__).resolve().parents[1] / "clusters" / "hippocampus.py"
    text = src.read_text()
    for marker in IDLE_EPISODE_MARKERS:
        assert f'"{marker}"' in text, marker
    # A sleep insight is NOT an idle thought — it must never be pruned.
    assert "(sleep — concluded)" not in IDLE_EPISODE_MARKERS


def test_sleep_prunes_at_the_end_of_consolidation(monkeypatch):
    from brain.sleep import SleepConsolidation

    s = SleepConsolidation.__new__(SleepConsolidation)
    seen = {}
    fake = MagicMock()
    fake.prune_idle_older_than = lambda days: seen.setdefault("days", days) and 7
    monkeypatch.setattr(st, "EpisodicStore", lambda *a, **k: fake)
    monkeypatch.setitem(settings._data, "dmn_idle_retention_days", 30)
    assert s.prune_idle_episodes() == 7
    assert seen["days"] == 30
    # Off, and a store error is never fatal to a consolidation.
    monkeypatch.setitem(settings._data, "dmn_idle_retention_days", 0)
    assert s.prune_idle_episodes() == 0
    monkeypatch.setitem(settings._data, "dmn_idle_retention_days", 30)
    boom = MagicMock()
    boom.prune_idle_older_than = MagicMock(side_effect=RuntimeError("supabase down"))
    monkeypatch.setattr(st, "EpisodicStore", lambda *a, **k: boom)
    assert s.prune_idle_episodes() == 0


def test_retention_setting_is_declared_and_admin_only():
    from brain.org_permissions import ADMIN_ONLY_KEYS
    from brain.settings import DEFAULTS

    assert DEFAULTS["dmn_idle_retention_days"] == 30
    assert "dmn_idle_retention_days" in ADMIN_ONLY_KEYS
