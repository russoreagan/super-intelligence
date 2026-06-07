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
from datetime import datetime
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

_TOOL_LOG_PATH = Path("second_brain/schema/tool_log.md")


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
        """Append one entry to second_brain/schema/tool_log.md (fire-and-forget)."""
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            status = "✓" if success else "✗"
            preview = output[:200].replace("\n", " ").strip()
            if len(output) > 200:
                preview += "..."
            entry = f"\n## {ts} {status}\n**Task:** {task}\n**Result:** {preview}\n"
            async with asyncio.Lock():
                with open(_TOOL_LOG_PATH, "a") as f:
                    f.write(entry)
        except Exception as e:
            logger.debug("[executor] Could not write tool log: %s", e)
