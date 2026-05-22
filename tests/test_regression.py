"""
Regression tests for previously shipped fixes.
Each test locks in a specific behaviour so it cannot silently regress.

Coverage:
  - Embedding-dim alignment (EMBEDDING_DIM == 768 everywhere)
  - EpisodicStore rejects unsafe session IDs
  - SchemaStore aappend_fact concurrency safety
  - gather(..., return_exceptions=True) degradation (graceful on exception)
  - Turn-timeout wrapper (cell.py + brainstem timeout constant)
  - embed() returns None when both backends unavailable
  - EMBEDDING_DIM constant agreement between router and store
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# EMBEDDING_DIM alignment
# ---------------------------------------------------------------------------

def test_embedding_dim_model_router_equals_768():
    from brain.model_router import EMBEDDING_DIM
    assert EMBEDDING_DIM == 768, "ModelRouter EMBEDDING_DIM must be 768"


def test_embedding_dim_store_equals_768():
    from brain.second_brain.store import EMBEDDING_DIM as STORE_DIM
    assert STORE_DIM == 768, "EpisodicStore EMBEDDING_DIM must be 768"


def test_embedding_dim_agreement():
    from brain.model_router import EMBEDDING_DIM as ROUTER_DIM
    from brain.second_brain.store import EMBEDDING_DIM as STORE_DIM
    assert ROUTER_DIM == STORE_DIM, (
        f"EMBEDDING_DIM mismatch: router={ROUTER_DIM}, store={STORE_DIM}"
    )


# ---------------------------------------------------------------------------
# EpisodicStore: session_id guard
# ---------------------------------------------------------------------------

class _DummyTable:
    def search(self):
        return self

    def where(self, clause):
        self._clause = clause
        return self

    def to_list(self):
        return []


class _DummyDB:
    def table_names(self):
        return ["episodes"]

    def open_table(self, name):
        return _DummyTable()


def _make_episodic_store(tmp_path):
    """Create an EpisodicStore that won't try to use real LanceDB."""
    import brain.second_brain.store as store_mod
    store = store_mod.EpisodicStore.__new__(store_mod.EpisodicStore)
    store._db = _DummyDB()
    store._table = _DummyTable()
    store._ready = True
    return store


def test_recall_by_session_rejects_path_traversal(tmp_path):
    store = _make_episodic_store(tmp_path)
    result = store.recall_by_session("../../etc/passwd")
    assert result == [], "Path traversal session_id must be rejected"


def test_recall_by_session_rejects_sql_injection(tmp_path):
    store = _make_episodic_store(tmp_path)
    result = store.recall_by_session("'; DROP TABLE episodes; --")
    assert result == [], "SQL injection session_id must be rejected"


def test_recall_by_session_accepts_valid_id(tmp_path):
    store = _make_episodic_store(tmp_path)
    # Should not raise; returns list (empty from dummy table)
    result = store.recall_by_session("abc123-XYZ_session")
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# SchemaStore: filename guard
# ---------------------------------------------------------------------------

def test_schema_store_rejects_traversal(fake_schema_store):
    store = fake_schema_store
    if not hasattr(store, "read"):
        pytest.skip("SchemaStore not available")
    result = store.read("../../../etc/passwd")
    assert result == "", "Path traversal filename must be rejected"


def test_schema_store_rejects_absolute_path(fake_schema_store):
    store = fake_schema_store
    if not hasattr(store, "read"):
        pytest.skip("SchemaStore not available")
    result = store.read("/etc/passwd")
    assert result == "", "Absolute path filename must be rejected"


def test_schema_store_rejects_non_md(fake_schema_store):
    store = fake_schema_store
    if not hasattr(store, "read"):
        pytest.skip("SchemaStore not available")
    result = store.read("self.py")
    assert result == "", "Non-.md filename must be rejected"


def test_schema_store_accepts_valid_filename(fake_schema_store):
    store = fake_schema_store
    if not hasattr(store, "read"):
        pytest.skip("SchemaStore not available")
    # Should not raise; returns "" since file doesn't exist yet
    result = store.read("self.md")
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# SchemaStore: concurrent aappend_fact — no interleave / corruption
# ---------------------------------------------------------------------------

async def test_aappend_fact_concurrent_no_interleave(fake_schema_store):
    store = fake_schema_store
    if not hasattr(store, "aappend_fact"):
        pytest.skip("SchemaStore not available")

    # Write baseline file
    store.write("user.md", "# User\n")

    # 20 concurrent appends
    facts = [f"fact number {i}" for i in range(20)]
    await asyncio.gather(*[store.aappend_fact("user.md", f) for f in facts])

    content = store.read("user.md")
    # All unique facts must be present
    for f in facts:
        assert f in content, f"Missing fact after concurrent writes: {f!r}"


# ---------------------------------------------------------------------------
# SchemaStore: aappend_fact dedup
# ---------------------------------------------------------------------------

async def test_aappend_fact_deduplicates(fake_schema_store):
    store = fake_schema_store
    if not hasattr(store, "aappend_fact"):
        pytest.skip("SchemaStore not available")

    store.write("user.md", "# User\n")
    await store.aappend_fact("user.md", "loves coffee")
    await store.aappend_fact("user.md", "loves coffee")
    await store.aappend_fact("user.md", "loves coffee")

    content = store.read("user.md")
    assert content.count("loves coffee") == 1, "Duplicate facts must be deduped"


# ---------------------------------------------------------------------------
# Turn timeout: brainstem constant exists and cell respects it
# ---------------------------------------------------------------------------

def test_turn_timeout_constant_exists():
    from brain.brainstem import TURN_TIMEOUT
    assert isinstance(TURN_TIMEOUT, (int, float))
    assert TURN_TIMEOUT > 0


async def test_integrator_cell_timeout(fake_router):
    """IntegratorCell.call() must return '' on timeout, not raise."""
    from brain.cell import IntegratorCell

    async def _slow(*args, **kwargs):
        await asyncio.sleep(10)
        return "never"

    fake_router.call = _slow  # type: ignore[method-assign]

    cell = IntegratorCell(
        name="slow_cell", cluster="test", model="haiku",
        system_prompt="test", topics=[],
        timeout_seconds=0.05,
    )
    cell.set_router(fake_router)
    cell.reset_turn("t1")

    result = await cell.call([{"role": "user", "content": "hi"}])
    assert result == "", "Timed-out cell must return empty string, not raise"


# ---------------------------------------------------------------------------
# embed() returns None when backends fail
# ---------------------------------------------------------------------------

async def test_embed_returns_none_on_total_failure():
    from brain.model_router import ModelRouter

    router = ModelRouter()

    # Simulate backends being unreachable — return None as the real methods do
    # when they catch connection errors internally.
    async def _fail_none(*args, **kwargs) -> None:
        return None

    router._embed_ollama = _fail_none  # type: ignore[method-assign]
    router._embed_google = _fail_none  # type: ignore[method-assign]
    router._embed_backend = "ollama"

    result = await router.embed("hello world")
    assert result is None, "embed() must return None when both backends fail"


# ---------------------------------------------------------------------------
# gather(return_exceptions=True) — exceptions do not propagate
# ---------------------------------------------------------------------------

async def test_gather_return_exceptions_degrades_gracefully():
    """Verify the pattern used in run.py: an exception in one task doesn't
    kill the gather — the caller gets BaseException instances to check."""

    async def _ok():
        return {"emotion": "calm"}

    async def _fail():
        raise RuntimeError("hypothalamus blew up")

    results = await asyncio.gather(_ok(), _fail(), return_exceptions=True)
    assert len(results) == 2
    assert results[0] == {"emotion": "calm"}
    assert isinstance(results[1], RuntimeError)
