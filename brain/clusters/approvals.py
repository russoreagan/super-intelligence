"""
PendingApprovals — disk-backed ledger of sensitive tool actions awaiting the
user's go-ahead.

When the cloud executor classifies a tool call as 'ask' (destructive, a code
change, a communication, a very large write, or anything unrecognized), it is
skipped in-session and recorded here so it survives the job ending and a process
restart — the "save it for later" half of the approval loop. The user approves
or skips it from the Self-directed work panel (or a tenant app over the engine
API); an approval re-queues the action so the brain runs it on the next idle
cycle, this time pre-authorized via its signature.

Mirrors PersistentTaskQueue: a flat JSON file next to the schema markdown, atomic
writes (temp-file → rename), single-threaded asyncio use.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal

from brain.second_brain.store import SECOND_BRAIN_ROOT

logger = logging.getLogger(__name__)

APPROVALS_PATH = SECOND_BRAIN_ROOT / "approvals.json"

Status = Literal["pending", "approved", "skipped"]

# Cap stored items; resolved (approved/skipped) entries are trimmed when over.
MAX_APPROVALS = 50
# A one-time approval is honored only this long after it's granted, so a stale
# signature can't silently green-light a much-later action.
APPROVAL_TTL_S = 24 * 60 * 60
# Right after approval the work is re-queued; the re-run may phrase the same tool
# call with slightly different args, so within this short window a same-tool call
# is also accepted (still one-time). Outside it, only an exact signature matches.
RESUME_WINDOW_S = 10 * 60


def action_signature(tool: str, tool_input) -> str:
    """Stable id for a (tool, input) pair so an approval can be matched to the
    same action on the re-run. Hashes the tool name plus a canonical view of the
    input (sorted keys); falls back to the tool name alone if input is unhashable."""
    try:
        blob = json.dumps(tool_input, sort_keys=True, default=str)[:4000]
    except Exception:
        blob = str(tool_input)[:4000]
    return hashlib.sha256(f"{(tool or '').strip().lower()}|{blob}".encode()).hexdigest()[:16]


def _preview(tool_input, limit: int = 240) -> str:
    """Short human-readable view of the action's input for the UI."""
    if isinstance(tool_input, dict):
        bits = []
        for k in ("path", "file_path", "filename", "to", "recipient", "url", "symbol", "command"):
            if tool_input.get(k):
                bits.append(f"{k}={tool_input[k]}")
        content = tool_input.get("content") or tool_input.get("text") or tool_input.get("body")
        if content:
            bits.append(f"{len(str(content))} chars")
        if bits:
            return ", ".join(bits)[:limit]
    return str(tool_input or "")[:limit]


@dataclass
class Approval:
    id: str
    tool: str
    signature: str
    reason: str = ""
    preview: str = ""
    turn_id: str = ""
    end_user_id: str = ""  # "" = owner/autonomous; else the engine-API end-user
    status: Status = "pending"
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Approval:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


class PendingApprovals:
    """Disk-backed list of action approvals. Not thread-safe (asyncio single-thread)."""

    def __init__(self) -> None:
        self._items: list[Approval] = []
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            if APPROVALS_PATH.exists():
                raw = json.loads(APPROVALS_PATH.read_text())
                self._items = [Approval.from_dict(a) for a in raw]
        except Exception as e:
            logger.warning("[Approvals] load failed — starting empty: %s", e)
            self._items = []

    def _save(self) -> None:
        try:
            APPROVALS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = APPROVALS_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps([a.to_dict() for a in self._items], indent=2))
            os.replace(tmp, APPROVALS_PATH)
        except Exception as e:
            logger.warning("[Approvals] save failed: %s", e)

    # ── Operations ─────────────────────────────────────────────────────────────
    def record(
        self, tool: str, tool_input, reason: str = "", turn_id: str = "", end_user_id: str = ""
    ) -> Approval:
        """Record a sensitive action as pending. Deduplicates against an existing
        pending item with the same signature (returns that one instead)."""
        sig = action_signature(tool, tool_input)
        for a in self._items:
            if a.status == "pending" and a.signature == sig:
                return a
        item = Approval(
            id=str(uuid.uuid4())[:8],
            tool=tool or "",
            signature=sig,
            reason=reason or "",
            preview=_preview(tool_input),
            turn_id=turn_id or "",
            end_user_id=end_user_id or "",
        )
        self._items.append(item)
        self._trim()
        self._save()
        logger.info("[Approvals] recorded [%s] %s (%s)", item.id, item.tool, item.reason)
        return item

    def is_approved(self, tool: str, tool_input) -> bool:
        """True if this action was approved and is still live. Prefers an exact
        signature match; within RESUME_WINDOW_S a same-tool approval also matches
        (the re-run may differ slightly). Consumes the approval — one use only."""
        sig = action_signature(tool, tool_input)
        name = (tool or "").strip().lower()
        now = time.time()
        match: Approval | None = None
        for a in self._items:
            if a.status != "approved" or a.resolved_at is None:
                continue
            if now - a.resolved_at > APPROVAL_TTL_S:
                continue
            if a.signature == sig:
                match = a
                break  # exact match wins
            if (
                match is None
                and a.tool.strip().lower() == name
                and now - a.resolved_at <= RESUME_WINDOW_S
            ):
                match = a
        if match is not None:
            self._items.remove(match)
            self._save()
            logger.info("[Approvals] consumed approval for %s", tool)
            return True
        return False

    @staticmethod
    def _scoped(a: Approval, end_user_id: str | None) -> bool:
        """end_user_id=None → no scope (owner, sees all); else must match exactly."""
        return end_user_id is None or a.end_user_id == end_user_id

    def approve(self, approval_id: str, end_user_id: str | None = None) -> Approval | None:
        """Mark a pending item approved. Returns it (caller re-queues the work).
        With end_user_id set, only that end-user's own item can be approved."""
        for a in self._items:
            if a.id == approval_id and a.status == "pending" and self._scoped(a, end_user_id):
                a.status = "approved"
                a.resolved_at = time.time()
                self._save()
                logger.info("[Approvals] approved [%s] %s", a.id, a.tool)
                return a
        return None

    def skip(self, approval_id: str, end_user_id: str | None = None) -> bool:
        for a in self._items:
            if a.id == approval_id and a.status == "pending" and self._scoped(a, end_user_id):
                a.status = "skipped"
                a.resolved_at = time.time()
                self._save()
                return True
        return False

    def pending(self, end_user_id: str | None = None) -> list[dict]:
        return [
            a.to_dict()
            for a in self._items
            if a.status == "pending" and self._scoped(a, end_user_id)
        ]

    def clear_pending(self) -> int:
        n = sum(1 for a in self._items if a.status == "pending")
        for a in self._items:
            if a.status == "pending":
                a.status = "skipped"
                a.resolved_at = time.time()
        if n:
            self._save()
        return n

    def _trim(self) -> None:
        if len(self._items) <= MAX_APPROVALS:
            return
        resolved = [a for a in self._items if a.status != "pending"]
        resolved.sort(key=lambda a: a.resolved_at or 0)
        drop = len(self._items) - MAX_APPROVALS
        for a in resolved[:drop]:
            self._items.remove(a)
