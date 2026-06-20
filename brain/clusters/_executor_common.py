"""
Shared executor helpers — the parts of the cloud-action executor contract that
are backend-agnostic (local Claude CLI vs Managed Agents).

`CMAExecutor` mixes these in so it presents the exact same surface as
`CloudExecutor` (confirmation handshake, pending-state, result screening/fencing,
audit log). `CloudExecutor` keeps its own copies deliberately — it is the
heavily-tested fallback path and is left byte-for-byte unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from brain.security import fence, screen_input

logger = logging.getLogger(__name__)

# Words that indicate user confirmation / denial of a pending write action.
# Kept identical to CloudExecutor so the confirmation gate behaves the same
# regardless of which executor is active.
_CONFIRM_WORDS = frozenset(
    [
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "ok",
        "okay",
        "go ahead",
        "do it",
        "confirm",
        "proceed",
        "send it",
        "go for it",
        "affirmative",
    ]
)

_DENY_WORDS = frozenset(
    [
        "no",
        "nope",
        "cancel",
        "stop",
        "don't",
        "abort",
        "never mind",
        "nevermind",
        "skip",
        "forget it",
        "hold on",
    ]
)

# Resolve under SECOND_BRAIN_PATH (per-tenant volume on hosted) rather than the
# process cwd — every tenant shares cwd=repo_root, so a relative path would make
# all tenants append to one shared audit log.
_SECOND_BRAIN_ROOT = Path(
    os.environ.get("SECOND_BRAIN_PATH", str(Path(__file__).parent.parent.parent / "second_brain"))
)
_TOOL_LOG_PATH = _SECOND_BRAIN_ROOT / "schema" / "tool_log.md"

# Serializes tool-log appends across executors (the read-modify-write on the
# hosted backend would otherwise drop entries under concurrency).
_tool_log_lock = asyncio.Lock()


async def append_tool_log_entry(
    task: str, output: str, success: bool, tag: str, end_user_id: str | None = None
) -> None:
    """Append one audit entry to schema/tool_log.md via the active storage backend.

    Hosted (BRAIN_STORAGE_BACKEND=supabase): goes through SchemaStore so the log
    lands in the brain_schemas table like every other schema file. Local: appends
    to the per-persona file on disk.
    """
    try:
        from datetime import datetime as _dt

        ts = _dt.now().strftime("%Y-%m-%d %H:%M")
        status = "✓" if success else "✗"
        preview = output[:200].replace("\n", " ").strip()
        if len(output) > 200:
            preview += "..."
        user_line = f"\n**User:** {end_user_id}" if end_user_id else ""
        entry = f"\n## {ts} {status}\n**Task:** {task}{user_line}\n**Result:** {preview}\n"
        async with _tool_log_lock:
            if os.environ.get("BRAIN_STORAGE_BACKEND", "local").lower() == "supabase":
                from brain.second_brain.store import SchemaStore

                store = SchemaStore()
                existing = store.read("tool_log.md")
                await store.awrite("tool_log.md", existing + entry)
            else:
                _TOOL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(_TOOL_LOG_PATH, "a") as f:
                    f.write(entry)
    except Exception as e:
        logger.debug("[%s] Could not write tool log: %s", tag, e)


class ExecutorCommon:
    """Mixin: confirmation handshake, pending-state, result screening, audit log.

    Expects the host class to set `self._pending: dict | None` in __init__.
    """

    _pending: dict | None

    # ── Pending confirmation state ─────────────────────────────────────────────

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    def set_pending(self, action: dict) -> None:
        self._pending = action

    def clear_pending(self) -> None:
        self._pending = None

    def get_pending(self) -> dict | None:
        return self._pending

    def is_user_confirming(self, text: str) -> bool:
        t = text.strip().lower()
        return any(w in t for w in _CONFIRM_WORDS)

    def is_user_denying(self, text: str) -> bool:
        t = text.strip().lower()
        return any(w in t for w in _DENY_WORDS)

    # ── Result security screening (guardrail 2) ────────────────────────────────

    def _screen_result(self, raw: str) -> str:
        """Treat executor output as untrusted (it may carry email/web content with
        adversarial text). Truncate, injection-screen, then fence so downstream
        cells treat it as data, not instructions. Mirrors CloudExecutor._screen_result."""
        if not raw:
            return "(no output)"

        truncated = raw[:8000]
        result = screen_input(truncated)
        if result.flagged:
            logger.warning(
                "[executor] Output failed injection screen (reason=%s) — "
                "returning sanitised placeholder instead of raw output",
                result.reason,
            )
            return "[output blocked: potential injection pattern detected in tool result]"

        return fence("cloud_result", truncated)

    # ── Audit trail ────────────────────────────────────────────────────────────

    async def _append_tool_log(self, task: str, output: str, success: bool) -> None:
        """Append one entry to schema/tool_log.md (fire-and-forget)."""
        end_user_id = getattr(self, "_current_end_user_id", None)
        await append_tool_log_entry(task, output, success, "executor", end_user_id=end_user_id)
