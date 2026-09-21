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
from brain.second_brain.store import Episode, EpisodicStore, is_dmn_idle_episode
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


# ── Retention ────────────────────────────────────────────────────────────────
#
# Every conclusion the hippocampus writes carries the SAME user_input,
# "(idle — concluded)", whatever its source. Retention must therefore read
# topic_tags. These rows are the shapes hippocampus.py and sleep.py actually write.
_ROWS = [
    # id, topic_tags, prunable
    (1, ["idle_thought", "reinforced"], True),
    (2, ["deferred_question", "normal"], True),
    (3, ["conclusion", "knowledge", "dmn"], True),
    (4, ["conclusion", "knowledge", "confirmed"], False),  # the user said yes
    (5, ["conclusion", "knowledge", "job"], False),  # a successful agent run
    (6, ["conclusion", "knowledge", "turn"], False),  # a notable turn learning
    (7, ["conclusion", "knowledge", "landed"], False),  # a thread that landed
    (8, ["conclusion", "knowledge", "sleep"], False),  # a sleep insight
    (9, ["trading", "AAOI"], False),  # an ordinary turn
    (10, [], False),
]


class _RowTable:
    """Table double backed by real rows, so the test asserts WHICH rows die."""

    def __init__(self, sink):
        self.sink = sink
        self.rows = [{"id": i, "topic_tags": t} for i, t, _ in _ROWS]
        self._mode = None
        self._after = 0
        self._ids = None

    def select(self, cols):
        self._mode = "select"
        self.sink["selected"] = cols
        return self

    def delete(self):
        self._mode = "delete"
        return self

    def eq(self, col, val):
        self.sink.setdefault("eq", []).append((col, val))
        return self

    def lt(self, col, val):
        self.sink["lt"] = (col, val)
        return self

    def gt(self, col, val):
        self._after = val
        return self

    def order(self, col):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def in_(self, col, vals):
        self._ids = list(vals)
        self.sink.setdefault("deleted_ids", []).extend(self._ids)
        return self

    def execute(self):
        if self._mode == "delete":
            self.rows = [r for r in self.rows if r["id"] not in (self._ids or [])]
            return MagicMock(data=[])
        page = [r for r in self.rows if r["id"] > self._after][: self._limit]
        return MagicMock(data=page)


def _row_store(sink, org="11111111-1111-1111-1111-111111111111"):
    s = EpisodicStore.__new__(EpisodicStore)
    s._use_supabase = True
    table = _RowTable(sink)
    client = MagicMock()
    client.table = lambda name: table
    s._sb = lambda: (client, org)
    sink["table"] = table
    return s


def test_prune_deletes_dmn_idle_output_and_nothing_else():
    sink = {}
    now = time.time()
    n = _row_store(sink).prune_idle_older_than(30)

    expected = sorted(i for i, _, prunable in _ROWS if prunable)
    assert sorted(sink["deleted_ids"]) == expected == [1, 2, 3]
    assert n == len(expected)
    # Survivors include every conclusion the DMN did not reach by itself.
    assert sorted(r["id"] for r in sink["table"].rows) == [4, 5, 6, 7, 8, 9, 10]

    assert ("org_id", "11111111-1111-1111-1111-111111111111") in sink["eq"]
    # Org-wide: a persona that no longer runs must not keep its backlog forever.
    assert not any(col == "persona" for col, _ in sink["eq"])
    col, cutoff = sink["lt"]
    assert col == "ts" and abs(cutoff - (now - 30 * 86400)) < 5
    # The delete is scoped to the org too, never a bare id list.
    assert sum(1 for c, _ in sink["eq"] if c == "org_id") >= 2


def test_prune_never_deletes_a_user_confirmed_conclusion():
    """The bug this guards: encode_conclusion writes "(idle — concluded)" for
    source="confirmed" too, so a user_input match deleted knowledge the console
    promises is kept forever."""
    assert is_dmn_idle_episode(["conclusion", "knowledge", "dmn"]) is True
    for source in ("confirmed", "job", "turn", "landed", "sleep"):
        assert is_dmn_idle_episode(["conclusion", "knowledge", source]) is False, source


def test_prune_is_off_at_zero_days():
    sink = {}
    assert _row_store(sink).prune_idle_older_than(0) == 0
    assert "deleted_ids" not in sink


def test_prune_pages_past_the_first_batch(monkeypatch):
    """A backlog larger than one page must not stop after the first page."""
    monkeypatch.setattr(st, "_PRUNE_PAGE", 2)
    sink = {}
    n = _row_store(sink).prune_idle_older_than(30)
    assert sorted(sink["deleted_ids"]) == [1, 2, 3] and n == 3


def test_idle_tags_match_what_the_dmn_actually_writes():
    """If hippocampus changes its tags, retention silently stops working."""
    from pathlib import Path

    root = Path(st.__file__).resolve().parents[1]
    hippo = (root / "clusters" / "hippocampus.py").read_text()
    for tag in st.IDLE_TAGS_ANY:
        assert f'"{tag}"' in hippo, tag
    assert '"conclusion"' in hippo and 'source: str = "dmn"' in hippo
    # A sleep insight is a conclusion, but never tagged "dmn" — so never pruned.
    sleep_src = (root / "sleep.py").read_text()
    assert '["conclusion", "knowledge", source]' in sleep_src
    assert is_dmn_idle_episode(["conclusion", "knowledge", "sleep"]) is False


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


def test_consolidate_actually_calls_the_prune():
    """The prune is step 8 of consolidate(). Nothing else asserts the wiring, so
    deleting the call left the whole suite green while retention silently stopped."""
    import inspect

    from brain.sleep import SleepConsolidation

    src = inspect.getsource(SleepConsolidation.consolidate)
    assert "prune_idle_episodes" in src, "consolidate() no longer prunes"
    # Off the event loop: the prune pages and blocks on PostgREST.
    assert "asyncio.to_thread(self.prune_idle_episodes)" in src


def test_retention_setting_is_declared_and_admin_only():
    from brain.org_permissions import ADMIN_ONLY_KEYS
    from brain.settings import DEFAULTS

    assert DEFAULTS["dmn_idle_retention_days"] == 30
    assert "dmn_idle_retention_days" in ADMIN_ONLY_KEYS
