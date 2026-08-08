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
# Pending items expire too: an approval nobody acted on for this long is stale — the
# job it belonged to has deferred and will re-raise a fresh one if still relevant
# (the autonomous spend pool resets at the UTC day boundary), and approving an old
# action later would fire it with dead context. Expired items are auto-skipped.
# Override via BRAIN_APPROVAL_PENDING_TTL_S.
PENDING_TTL_S = float(os.environ.get("BRAIN_APPROVAL_PENDING_TTL_S", 24 * 60 * 60))
# Right after approval the work is re-queued; the re-run may phrase the same tool
# call with slightly different args, so within this short window a same-tool call
# is also accepted (still one-time). Outside it, only an exact signature matches.
RESUME_WINDOW_S = 10 * 60
# Job-scope grant: approving ONE action from a job pre-authorizes the whole re-run
# (the job re-plans, so per-action signatures can't survive the round-trip — one
# approval used to unlock exactly one write and the job ping-ponged back to
# awaiting_approval for each subsequent one). The grant is carried by the re-queued
# task, honored non-consumingly while that job runs, revoked when it ends, and
# TTL-bounded as a backstop in case the revoke never runs (crash mid-job).
# Override via BRAIN_APPROVAL_GRANT_TTL_S.
JOB_GRANT_TOOL = "__job_approval_grant__"
GRANT_TTL_S = float(os.environ.get("BRAIN_APPROVAL_GRANT_TTL_S", 2 * 60 * 60))


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
        self.expire_stale()  # a stale twin must not dedupe-block a fresh request
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
    def _scoped(a: Approval, end_user_id: str | None, include_autonomous: bool = False) -> bool:
        """Which approvals an end-user query may see.

        end_user_id=None → no scope (the owner UI, which sees the whole ledger).
        Otherwise the item's end_user_id must match exactly — except when
        include_autonomous is set, which also admits the autonomous/owner lane ("")
        so an owner-key tenant query (e.g. the trading app) surfaces the actions the
        brain queued while unattended, not just the ones it raised in-session."""
        if end_user_id is None:
            return True
        if a.end_user_id == end_user_id:
            return True
        return include_autonomous and a.end_user_id == ""

    def approve(
        self, approval_id: str, end_user_id: str | None = None, include_autonomous: bool = False
    ) -> Approval | None:
        """Mark a pending item approved. Returns it (caller re-queues the work).
        With end_user_id set, only that end-user's own item can be approved — plus
        the autonomous/owner lane when include_autonomous is set."""
        for a in self._items:
            if (
                a.id == approval_id
                and a.status == "pending"
                and self._scoped(a, end_user_id, include_autonomous)
            ):
                a.status = "approved"
                a.resolved_at = time.time()
                self._save()
                logger.info("[Approvals] approved [%s] %s", a.id, a.tool)
                return a
        return None

    def skip(
        self, approval_id: str, end_user_id: str | None = None, include_autonomous: bool = False
    ) -> bool:
        for a in self._items:
            if (
                a.id == approval_id
                and a.status == "pending"
                and self._scoped(a, end_user_id, include_autonomous)
            ):
                a.status = "skipped"
                a.resolved_at = time.time()
                self._save()
                return True
        return False

    # ── Job-scope grants ───────────────────────────────────────────────────────
    def grant_for(self, turn_id: str = "") -> str:
        """Mint a job-scope grant: a token the re-queued job carries so EVERY ask it
        raises while running is allowed — one approval clears the whole task. Stored
        as a resolved ledger item (tool=JOB_GRANT_TOOL) so it survives a restart and
        rides the existing persistence; never listed as pending."""
        token = uuid.uuid4().hex[:16]
        self._items.append(
            Approval(
                id=str(uuid.uuid4())[:8],
                tool=JOB_GRANT_TOOL,
                signature=token,
                reason="job-scope grant (one approval covers the whole task)",
                turn_id=turn_id or "",
                status="approved",
                resolved_at=time.time(),
            )
        )
        self._trim()
        self._save()
        return token

    def token_valid(self, token: str) -> bool:
        """Non-consuming check: is this job-scope grant still live? (The job holds
        the token for its whole run; revoke_token() ends it when the job ends.)"""
        if not token:
            return False
        now = time.time()
        for a in self._items:
            if (
                a.tool == JOB_GRANT_TOOL
                and a.signature == token
                and a.status == "approved"
                and a.resolved_at is not None
                and now - a.resolved_at <= GRANT_TTL_S
            ):
                return True
        return False

    def revoke_token(self, token: str) -> None:
        """Drop a job-scope grant (the granted job finished). TTL is the backstop
        if the job crashes before this runs."""
        if not token:
            return
        keep = [a for a in self._items if not (a.tool == JOB_GRANT_TOOL and a.signature == token)]
        if len(keep) != len(self._items):
            self._items = keep
            self._save()

    def consume_item(self, approval_id: str) -> None:
        """Remove a resolved item outright. Used when a job-scope grant supersedes a
        just-approved action: leaving the approved item in the ledger would hand out
        one extra same-tool pass (the RESUME_WINDOW match) after the grant is gone."""
        keep = [a for a in self._items if a.id != approval_id]
        if len(keep) != len(self._items):
            self._items = keep
            self._save()

    def resolve_siblings(self, turn_id: str, exclude_id: str = "") -> list[str]:
        """Skip the OTHER pending items raised by the same job (turn_id): the job-scope
        grant supersedes them — the re-run will redo those actions pre-authorized, so
        leaving them pending would show dead cards the user keeps approving into
        duplicate re-runs. Returns the resolved ids so UIs can clear their cards."""
        if not turn_id:
            return []
        now = time.time()
        out: list[str] = []
        for a in self._items:
            if (
                a.status == "pending"
                and a.turn_id == turn_id
                and a.id != exclude_id
                and a.tool != JOB_GRANT_TOOL
            ):
                a.status = "skipped"
                a.resolved_at = now
                out.append(a.id)
        if out:
            self._save()
            logger.info(
                "[Approvals] %d sibling approval(s) superseded by job grant (%s)",
                len(out),
                turn_id,
            )
        return out

    def expire_stale(self) -> int:
        """Auto-skip pending items older than PENDING_TTL_S. Without this, an
        unactioned approval (e.g. yesterday's continue-spending sentinel) dangles in
        the panel forever, and approving it much later would fire an action whose
        job context is long gone. Returns how many were expired."""
        now = time.time()
        n = 0
        for a in self._items:
            if a.status == "pending" and now - a.created_at > PENDING_TTL_S:
                a.status = "skipped"
                a.resolved_at = now
                n += 1
                logger.info(
                    "[Approvals] expired stale pending [%s] %s (unactioned for >%dh)",
                    a.id,
                    a.tool,
                    int(PENDING_TTL_S // 3600),
                )
        if n:
            self._save()
        return n

    def pending(
        self, end_user_id: str | None = None, include_autonomous: bool = False
    ) -> list[dict]:
        self.expire_stale()
        return [
            a.to_dict()
            for a in self._items
            if a.status == "pending" and self._scoped(a, end_user_id, include_autonomous)
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

    def forget_end_user(self, end_user_id: str) -> int:
        """Drop every approval belonging to one end-user (right-to-erasure).

        Approvals are not just metadata: each carries the `tool_input` it was raised
        for, which is the actual content of the pending action (recipients, message
        bodies). Returns how many were removed."""
        if not end_user_id:
            return 0
        keep = [a for a in self._items if a.end_user_id != end_user_id]
        n = len(self._items) - len(keep)
        if n:
            self._items = keep
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
