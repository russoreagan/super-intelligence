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
def _isolate_dmn_novelty_state(tmp_path, monkeypatch):
    """Keep DMN tests from polluting the real ``second_brain/`` directory.

    The DMN persists its novelty/dedup state to disk on every accepted thought,
    so any test that drives ``_process_thought`` would otherwise write a real
    ``second_brain/dmn_novelty.json``. Redirect that path to a per-test temp file.
    """
    try:
        import brain.dmn as _dmn
    except Exception:
        return
    monkeypatch.setattr(_dmn, "NOVELTY_STATE_PATH", tmp_path / "dmn_novelty.json", raising=False)


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
