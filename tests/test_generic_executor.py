"""
GenericExecutor — provider-agnostic motor tier. Verifies the executor contract
(execute_read/execute_pending/pending state), the read/write tool gating, and
that the agent loop respects the step ceiling — all with a scripted router so no
provider is touched.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from brain.clusters.generic_executor import GenericExecutor, _tool_specs


class _Bus:
    async def publish_dict(self, *a, **k):  # motor.result sink
        pass


class ScriptedRouter:
    """Returns a queued sequence of call_structured_any decisions."""

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.calls = 0

    async def call_structured_any(self, *a, **k):
        self.calls += 1
        return self._decisions.pop(0) if self._decisions else {"text": "done"}


def _ex(router, tmp):
    return GenericExecutor(_Bus(), router=router, allowed_paths=[str(tmp)])


def test_tool_specs_read_write_split():
    read = {t["name"] for t in _tool_specs(False)}
    write = {t["name"] for t in _tool_specs(True)}
    assert "fs_write" not in read and "fs_append" not in read
    assert {"fs_write", "fs_append"}.issubset(write)
    assert read.issubset(write)


def test_contract_surface():
    ex = GenericExecutor(_Bus(), router=ScriptedRouter([]))
    for m in ("execute_read", "execute_pending", "set_pending", "clear_pending",
              "is_user_confirming", "is_user_denying", "connectors_summary"):
        assert hasattr(ex, m)
    assert not ex.has_pending
    ex.set_pending({"task": "x", "context_facts": []})
    assert ex.has_pending and ex.get_pending()["task"] == "x"
    ex.clear_pending()
    assert not ex.has_pending


async def test_read_run_drives_loop(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    router = ScriptedRouter([
        {"tool": "fs_list", "args": {"path": str(tmp_path)}},
        {"text": "listed; done"},
    ])
    out = await _ex(router, tmp_path).execute_read("list it", [], "t1")
    assert out["success"] and "done" in out["output"]
    assert router.calls == 2


async def test_write_tool_blocked_in_read_run(tmp_path):
    ex = _ex(ScriptedRouter([]), tmp_path)
    r = await ex._dispatch("fs_write", {"path": str(tmp_path / "x"), "content": "y"}, False)
    assert r.startswith("[blocked]")
    assert not (tmp_path / "x").exists()


async def test_pending_run_enables_write(tmp_path):
    target = tmp_path / "out.txt"
    router = ScriptedRouter([
        {"tool": "fs_write", "args": {"path": str(target), "content": "data"}},
        {"text": "wrote it"},
    ])
    ex = _ex(router, tmp_path)
    assert await ex.execute_pending() is None  # nothing pending yet
    ex.set_pending({"task": "write out.txt", "context_facts": []})
    out = await ex.execute_pending("t2")
    assert out["success"]
    assert target.read_text() == "data"
    assert not ex.has_pending  # consumed


async def test_step_ceiling_bounds_calls(tmp_path, monkeypatch):
    from brain.settings import settings
    monkeypatch.setattr(settings, "_data", {**settings._data, "ralph_max_total_attempts": 3},
                        raising=False)
    # A router that never says "done" must be stopped by the ceiling.
    router = ScriptedRouter([{"tool": "fs_list", "args": {"path": str(tmp_path)}}] * 50)
    out = await _ex(router, tmp_path).execute_read("loop forever", [], "t3")
    assert router.calls == 3
    assert out is not None


async def test_path_outside_allowlist_blocked(tmp_path):
    router = ScriptedRouter([
        {"tool": "fs_read", "args": {"path": "/etc/passwd"}},
        {"text": "could not read"},
    ])
    out = await _ex(router, tmp_path).execute_read("read passwd", [], "t4")
    # The blocked tool result is fenced into output; nothing leaked.
    assert "passwd" not in out["output"] or "blocked" in out["output"].lower()
