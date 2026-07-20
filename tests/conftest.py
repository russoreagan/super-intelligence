"""
Shared test fixtures and doubles for the brain test suite.
"""

from __future__ import annotations

import pytest
import pytest_asyncio  # noqa: F401 — ensures asyncio_mode is respected

pytest_plugins = ("pytest_asyncio",)


# ---------------------------------------------------------------------------
# pytest-asyncio global config
# ---------------------------------------------------------------------------
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "asyncio: mark test as async (auto-used via asyncio_mode=auto)"
    )


# Tell pytest-asyncio to treat every async test as asyncio automatically.


def pytest_collection_modifyitems(items):
    pass  # placeholder; asyncio_mode handled via ini option below


# ---------------------------------------------------------------------------
# FakeRouter — lightweight stand-in for brain.model_router.ModelRouter
# ---------------------------------------------------------------------------
class FakeRouter:
    """
    A scripted test double for ModelRouter.

    Usage::

        router = FakeRouter()
        router.scripted_responses["frontal"] = '{"decision": "yes"}'
        result = await router.call("claude", "sys", [], cluster="frontal", cell="frontal")
        assert result == '{"decision": "yes"}'
    """

    def __init__(self) -> None:
        self.scripted_responses: dict[str, str] = {}
        self.calls: list[dict] = []
        self._call_log: list[dict] = self.calls  # mirror real router interface

    # ------------------------------------------------------------------
    # Core interface
    # ------------------------------------------------------------------

    async def call(
        self,
        model_key: str,
        system_prompt: str,
        messages: list,
        *,
        cluster: str = "",
        cell: str = "",
        turn_id: str = "",
    ) -> str:
        entry = {
            "model_key": model_key,
            "system_prompt": system_prompt,
            "messages": messages,
            "cluster": cluster,
            "cell": cell,
            "turn_id": turn_id,
        }
        self.calls.append(entry)

        # Look up scripted response: prefer cell name, fall back to model_key.
        for key in (cell, model_key):
            if key and key in self.scripted_responses:
                return self.scripted_responses[key]
        return "{}"

    async def embed(self, text: str) -> list[float] | None:
        return [0.0] * 768

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def reset_call_log(self) -> None:
        self.calls.clear()

    @property
    def total_calls_this_turn(self) -> int:
        return len(self.calls)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_router() -> FakeRouter:
    """Return a fresh FakeRouter for each test."""
    return FakeRouter()


@pytest.fixture(autouse=True)
def _isolate_second_brain_root(tmp_path, monkeypatch):
    """Keep call-time persona-state writes out of the real ``second_brain/``.

    persona_key.persona_state_root (learning ledger/stories, sequence weights,
    angle synonyms, per-persona wiring siblings) resolves SECOND_BRAIN_PATH at
    CALL time, so any test that binds a persona and triggers a save would
    otherwise write real ``second_brain/personas/<slug>/`` files (this bit —
    a DMN test truncated the_analyst's tracked sequence_weights.json). Route
    the root to tmp; tests needing a specific layout setenv their own (their
    fixtures run after this autouse one and win)."""
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "second_brain"))


@pytest.fixture(autouse=True)
def _isolate_job_store_dir(tmp_path, monkeypatch):
    """Keep job-record writes out of the real ``second_brain/jobs/``.

    ``job_store.JOBS_DIR`` is a module-level constant computed at IMPORT time
    (``SECOND_BRAIN_ROOT / "jobs"``, and SECOND_BRAIN_ROOT itself reads
    SECOND_BRAIN_PATH at import). By the time _isolate_second_brain_root sets
    the env var the constant is already frozen to the repo tree, so anything
    that drives a real JobStore — a MotorCortexCluster job, execute_internal_job
    — wrote genuine-looking records into the live tree (this bit: 83 fixture
    jobs accumulated there, and one got git-committed). Point the constant at
    tmp instead.

    This matters beyond tidiness: second_brain/jobs/ is the input corpus for
    sleep's chunk_mining_pass, so fixture jobs reached real chunk mining and
    could influence what gets promoted to a ballistic reflex.

    Patching the attribute (not just the env) is what makes this stick, and it
    also covers ``sleep.py``'s function-local ``from ... import JOBS_DIR``,
    which re-reads the module attribute on each call. Tests that need their own
    jobs dir monkeypatch JOBS_DIR themselves; they run after this autouse
    fixture and win.

    The dir is namespaced under a private subdir rather than ``tmp_path`` root
    or ``tmp_path/second_brain``: tmp_path belongs to the test, and a test is
    free to mkdir its own layout there (one builds a bare second_brain/ with
    exist_ok=False). Creating it here keeps a glob-before-any-write from seeing
    a missing dir.
    """
    try:
        import brain.clusters.job_store as _js
    except Exception:
        return
    jobs_dir = tmp_path / "_job_store_isolation" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_js, "JOBS_DIR", jobs_dir, raising=False)


@pytest.fixture(autouse=True)
def _isolate_dmn_novelty_state(tmp_path, monkeypatch):
    """Keep DMN tests from polluting the real ``second_brain/`` directory.

    The DMN persists its novelty/dedup state to disk on every accepted thought,
    so any test that drives ``_process_thought`` would otherwise write a real
    ``second_brain/dmn_novelty.json``. Redirect that path to a per-test temp file.

    ROUTING_WEIGHTS_PATH is the same story one file over: it's frozen at import
    from SECOND_BRAIN_ROOT, and the idle-routing save path rewrote the real
    ``second_brain/dmn_routing_weights.json`` during the suite.
    """
    try:
        import brain.dmn as _dmn
    except Exception:
        return
    monkeypatch.setattr(_dmn, "NOVELTY_STATE_PATH", tmp_path / "dmn_novelty.json", raising=False)
    monkeypatch.setattr(
        _dmn, "ROUTING_WEIGHTS_PATH", tmp_path / "dmn_routing_weights.json", raising=False
    )


@pytest.fixture(autouse=True)
def _isolate_criticality_gains():
    """Clear the per-persona modulation gains between tests.

    ``criticality._gain_by_persona`` is module-level and deliberately outlives any
    one turn — it is what SwitchNeuron.effective_threshold reads. Without a reset a
    test that drives the controller leaves a gain behind and every later test's
    thresholds are silently scaled by it (test_flock_dynamics was shifting
    test_recruitment's expected 0.3 to 0.309).

    This did not bite before the gain moved off ``settings`` only because the tests
    that drive the controller monkeypatch ``modulation_gain``, and monkeypatch
    restores it on teardown — masking the same leak rather than preventing it.
    """
    try:
        from brain.observability.criticality import reset_gains
    except Exception:
        return
    reset_gains()
    yield
    reset_gains()


@pytest.fixture(autouse=True)
def _isolate_tool_log(tmp_path, monkeypatch):
    """Keep the executor audit trail out of the real ``second_brain/schema/``.

    ``_executor_common._TOOL_LOG_PATH`` is another import-time constant off
    SECOND_BRAIN_ROOT. Every executor tool call appends one entry, so the suite
    was appending fixture rows ("loop forever", "read passwd", …) straight into
    the real schema/tool_log.md — the file is gitignored, so git status never
    showed it. Tests that assert on log contents set exe._log_path themselves.
    """
    try:
        import brain.clusters._executor_common as _ec
    except Exception:
        return
    monkeypatch.setattr(_ec, "_TOOL_LOG_PATH", tmp_path / "tool_log.md", raising=False)


@pytest.fixture
def fake_schema_store(tmp_path, monkeypatch):
    """
    Return a SchemaStore backed by a temporary directory.

    Monkeypatches SCHEMA_DIR so no writes land in the real second_brain/.
    If SchemaStore cannot be imported the fixture returns tmp_path instead.
    """
    try:
        import brain.second_brain.store as store_mod  # type: ignore

        schema_dir = tmp_path / "schema"
        schema_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(store_mod, "SCHEMA_DIR", schema_dir)
        return store_mod.SchemaStore()
    except Exception:
        return tmp_path
