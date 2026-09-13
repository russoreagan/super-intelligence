"""Read-path content policy — may THIS actor read THAT persona's learned content?

The console (brain/ui/server.py) and the owner-key persona views (brain/api/
server.py) used to gate content reads on org *membership* only. In an isolated org
(organizations.learning_mode, brain/org_settings.py) every non-home persona is one
buyer's companion, so a member of the partner's org reading its turns, jobs,
approvals, thoughts, living self-model or user-model is reading that buyer's
private conversation. This module is the one place that decides, and every
content read consults it:

  * kill switch `content_read_policy` = 0 → allow (reason policy_off) but STILL
    audit — a switch must never be quieter than the feature;
  * org mode unknown (row never read) → deny, fail closed (no-backend local mode
    reads as consolidated, so companion use is unaffected);
  * not an org admin → deny in both modes (the membership-only gate was the bug);
  * isolated + non-home persona → deny ("" persona counts as non-home: an unscoped
    read is denied and the caller must scope or project);
  * isolated + home → allow with scope "owner_lane" (home is exempt from ownership
    binding, so partner sessions can land on it; its engine-lane rows are buyer
    content and the caller filters end_user_id == "");
  * consolidated + org admin → allow, scope "all", audited.

What is NOT content and never passes through here: the persona spec (dials, Seed
self-model), the chemistry resting/current snapshot, counts, hashes, states,
costs — the operator's configuration and the persona's mood, not the buyer's
words. `self_model` here means the LIVING self.md only.

Projections (`project_*`) are allowlists: the content-free shape a denied list
read gets instead of a 403, so the view stays alive and shows activity.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import NamedTuple

logger = logging.getLogger(__name__)

KINDS = (
    "turns",
    "self_model",
    "user_model",
    "jobs",
    "approvals",
    "thoughts",
    "chemistry_roster",
    "learning_stories",
)

REASON_OK = "ok"
REASON_POLICY_OFF = "policy_off"
REASON_MODE_UNKNOWN = "org_mode_unknown"
REASON_ORG_ADMIN = "org_admin_required"
REASON_ISOLATED = "isolated_persona"

SCOPE_ALL = "all"
SCOPE_OWNER_LANE = "owner_lane"


class Decision(NamedTuple):
    allow: bool
    reason: str
    mode: str
    scope: str

    @property
    def content(self) -> bool:
        return self.allow


# ── actors ────────────────────────────────────────────────────────────────────


def actor_from_claims(claims: dict | None) -> dict:
    """Console actor from the verified Supabase claims the auth gate attached."""
    from brain.ui import auth as ui_auth

    c = claims or {}
    try:
        disabled = ui_auth.is_disabled()
    except Exception:
        disabled = False
    try:
        platform_admin = bool(ui_auth.is_admin(c))
    except Exception:
        platform_admin = False
    try:
        org_admin = disabled or bool(ui_auth.is_org_admin(c))
    except Exception:
        org_admin = False
    return {
        "source": "console",
        "owner": False,
        "partner_id": None,
        "key_id": None,
        "user": str(c.get("email") or c.get("sub") or ""),
        "org_admin": org_admin,
        "platform_admin": platform_admin,
    }


def actor_from_api_ctx(ctx: dict | None) -> dict:
    """Engine-API actor. An owner key IS the org admin; a partner key never is."""
    c = ctx or {}
    owner = bool(c.get("owner"))
    return {
        "source": "api",
        "owner": owner,
        "partner_id": c.get("partner_id"),
        "key_id": c.get("key_id"),
        "user": None,
        "org_admin": owner,
        "platform_admin": False,
    }


# ── the decision ──────────────────────────────────────────────────────────────


def _policy_on() -> bool:
    try:
        from brain.settings import settings

        return bool(int(settings.get("content_read_policy", 1) or 0))
    except (TypeError, ValueError):
        return True
    except Exception:
        return True


def content_read_allowed(actor: dict, persona: str, kind: str) -> Decision:
    """The decision. Never raises; a failure inside resolves to deny (fail closed)."""
    from brain import org_settings

    try:
        mode = org_settings.learning_mode()
    except Exception:
        mode = org_settings.UNKNOWN
    if not _policy_on():
        return Decision(True, REASON_POLICY_OFF, mode, SCOPE_ALL)
    if mode == org_settings.UNKNOWN:
        return Decision(False, REASON_MODE_UNKNOWN, mode, SCOPE_ALL)
    if not bool((actor or {}).get("org_admin")):
        return Decision(False, REASON_ORG_ADMIN, mode, SCOPE_ALL)
    if mode == "isolated":
        slug = _slug(persona)
        if not slug or not org_settings.is_home(slug):
            return Decision(False, REASON_ISOLATED, mode, SCOPE_ALL)
        return Decision(True, REASON_OK, mode, SCOPE_OWNER_LANE)
    return Decision(True, REASON_OK, mode, SCOPE_ALL)


def require_content_read(actor: dict, persona: str, kind: str, route: str) -> Decision:
    """content_read_allowed + 403 on deny (fastapi HTTPException) + audit on allow."""
    d = content_read_allowed(actor, persona, kind)
    if not d.allow:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403,
            detail={"detail": d.reason, "kind": kind, "persona": _slug(persona), "content": ""},
        )
    audit_read(actor, kind, persona, route, decision=d)
    return d


# ── persona resolution off rows / events ──────────────────────────────────────


def _slug(persona: str | None) -> str:
    from brain.persona_key import persona_slug

    return persona_slug(persona or "")


def persona_of_agent_id(agent_id: str | None) -> str:
    """'<persona>.<mandate>' → persona slug; '' when absent."""
    aid = str(agent_id or "")
    return _slug(aid.split(".", 1)[0]) if aid else ""


def persona_of_event(ev: dict | None) -> str:
    e = ev or {}
    return _slug(e.get("persona")) or persona_of_agent_id(e.get("agent_id"))


def persona_of_job(row: dict | None) -> str:
    r = row or {}
    return _slug(r.get("origin_persona") or r.get("persona")) or persona_of_agent_id(
        r.get("agent_id") or r.get("origin_agent_id")
    )


def end_user_hash(end_user_id: str | None) -> str:
    """Stable, non-reversible reference to a customer for the wire/audit log."""
    s = str(end_user_id or "")
    if not s:
        return ""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def owner_lane_row(row: dict | None) -> bool:
    """True when a row belongs to the owner/autonomous lane (no end user)."""
    return not str((row or {}).get("end_user_id") or (row or {}).get("origin_end_user_id") or "")


# ── projections (allowlists) ──────────────────────────────────────────────────


def project_turn(row: dict) -> dict:
    r = row or {}
    return {
        "agent_id": r.get("agent_id", ""),
        "persona": persona_of_event(r),
        "session_id": r.get("session_id", ""),
        "turn_id": r.get("turn_id", ""),
        "ts": r.get("ts"),
        "end_user_hash": end_user_hash(r.get("end_user_id")),
        "prompt_len": len(str(r.get("prompt") or "")),
        "response_len": len(str(r.get("response") or "")),
        "content": False,
    }


_JOB_META = (
    "job_id",
    "id",
    "state",
    "reason_code",
    "source",
    "agent_id",
    "partner_id",
    "productive_steps",
    "stories_completed",
    "stories_total",
    "cloud_usd",
    "created_at",
    "updated_at",
    "completed_at",
    "origin_persona",
)


def project_job(row: dict) -> dict:
    r = row or {}
    out = {k: r.get(k) for k in _JOB_META if k in r}
    out["persona"] = persona_of_job(r)
    out["end_user_hash"] = end_user_hash(r.get("end_user_id") or r.get("origin_end_user_id"))
    out["steps"] = len(r.get("steps_json") or r.get("steps") or [])
    out["content"] = False
    return out


def project_approval(a: dict) -> dict:
    r = a or {}
    return {
        "id": r.get("id", ""),
        "tool": r.get("tool", ""),
        "status": r.get("status", ""),
        "created_at": r.get("created_at"),
        "end_user_hash": end_user_hash(r.get("end_user_id")),
        "content": False,
    }


_EVENT_ALWAYS = ("type", "channel", "route_sid", "agent_id", "persona", "turn_id", "ts")
_EVENT_PASSTHROUGH = {
    "activation",
    "cell_activation",
    "emotion",
    "user_emotion",
    "neuromod",
    "hormonal",
    "user_prosody",
}
_TASK_META = (
    "task_id",
    "job_id",
    "state",
    "step",
    "steps_total",
    "cloud_usd",
    "productive_steps",
    "reason_code",
)
_EVENT_DROP = {"proactive_speech", "data_table", "chart", "transcript", "speaking"}


def project_event(ev: dict) -> dict | None:
    """Content-free shape of one UI event, or None when the event has no
    content-free meaning (it is dropped for that client)."""
    e = ev or {}
    etype = str(e.get("type") or "")
    if etype in _EVENT_DROP:
        return None
    if etype in _EVENT_PASSTHROUGH:
        return dict(e)
    out = {k: e[k] for k in _EVENT_ALWAYS if k in e}
    out["persona"] = persona_of_event(e)
    if e.get("end_user_id"):
        out["end_user_hash"] = end_user_hash(e.get("end_user_id"))
    if etype == "turn_start":
        out["input_len"] = len(str(e.get("user_input") or ""))
    elif etype == "turn_end":
        out["response_len"] = len(str(e.get("response") or ""))
        for k in ("elapsed_s", "llm_calls"):
            if k in e:
                out[k] = e[k]
    elif etype.startswith("task_"):
        for k in _TASK_META:
            if k in e:
                out[k] = e[k]
    elif etype == "stream_thought":
        out["type"] = "stream_thought_withheld"
        for k in ("salience", "urgency", "from_job", "proactive"):
            if k in e:
                out[k] = e[k]
    out["content"] = False
    return out


# ── audit ─────────────────────────────────────────────────────────────────────

_recent_reads: dict[tuple, float] = {}


def _audit_on() -> bool:
    try:
        from brain.settings import settings

        return bool(int(settings.get("content_read_audit", 1) or 0))
    except Exception:
        return True


def _audit_window() -> float:
    try:
        from brain.settings import settings

        return float(settings.get("content_read_audit_window_s", 300) or 0.0)
    except Exception:
        return 300.0


def audit_read(
    actor: dict,
    kind: str,
    persona: str,
    route: str,
    *,
    decision: Decision | None = None,
    end_user_id: str = "",
    rows: int = 0,
    event: str = "content_read",
) -> bool:
    """One governance line per allowed content read (never text). Identical reads
    by the same user within the window are coalesced (the console polls).
    Returns True when a line was written."""
    if not _audit_on():
        return False
    slug = _slug(persona)
    key = (
        str((actor or {}).get("user") or (actor or {}).get("key_id") or ""),
        kind,
        slug,
        route,
        event,
    )
    now = time.time()
    last = _recent_reads.get(key, 0.0)
    if now - last < _audit_window():
        return False
    _recent_reads[key] = now
    if len(_recent_reads) > 4096:
        for k in sorted(_recent_reads, key=_recent_reads.get)[:1024]:
            _recent_reads.pop(k, None)
    try:
        from brain import learning_mode

        learning_mode.audit(
            event,
            actor,
            kind=kind,
            persona=slug,
            route=route,
            mode=decision.mode if decision else "",
            scope=decision.scope if decision else "",
            reason=decision.reason if decision else "",
            end_user_hash=end_user_hash(end_user_id),
            rows=int(rows or 0),
        )
        return True
    except Exception as e:  # pragma: no cover - best effort
        logger.debug("[read_policy] audit skipped: %s", e)
        return False


def count_content_reads(persona: str, since_s: float = 30 * 86400.0, max_lines: int = 10000) -> int:
    """How many `content_read` audit lines name this persona in the window (the
    isolation snapshot's `content_reads_30d`). Reads the jsonl tail; never raises."""
    import json

    slug = _slug(persona)
    if not slug:
        return 0
    try:
        from brain.learning_mode import audit_log_path

        path = audit_log_path()
        if not path.is_file():
            return 0
        lines = path.read_text(encoding="utf-8").splitlines()[-max_lines:]
    except Exception:
        return 0
    cutoff = time.time() - float(since_s)
    n = 0
    for ln in lines:
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        if (
            rec.get("event") == "content_read"
            and rec.get("persona") == slug
            and float(rec.get("ts") or 0.0) >= cutoff
        ):
            n += 1
    return n
