"""
Durable autonomous-job outcome store (migration 021_agent_jobs).

Job results used to live only in second_brain/jobs/{id}.json (JobStore) — no DB
table, so they weren't pollable and didn't survive volume loss. This mirrors
brain/agent_usage_store.py exactly: a best-effort, Supabase-backed writer that
upserts a job's outcome by job_id and is a silent no-op in companion/local mode
(no Supabase) — the JSON JobStore stays as the local fallback.

The upsert is called INCREMENTALLY (once per completed chunk, state='running',
results-so-far) and again at the terminal state, always keyed on job_id, so
partial results are durable even if the whole job later fails or is killed.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _sb():
    try:
        from brain.second_brain import supabase_client

        if not supabase_client.is_enabled():
            return None
        return supabase_client.get_client(), supabase_client.get_org_id()
    except Exception:
        return None


def _agent_id() -> str:
    try:
        from brain import turn_ctx

        return str((turn_ctx.current_turn() or {}).get("agent_id") or "")
    except Exception:
        return ""


def _row(org: str, record: dict) -> dict:
    """Project a JobOutcome/JobStore record dict onto the agent_jobs columns."""
    import datetime as _dt

    state = str(record.get("state") or ("completed" if record.get("success") else "failed"))
    now = _dt.datetime.now(_dt.UTC).isoformat()
    return {
        "job_id": str(record.get("job_id") or ""),
        "org_id": org,
        "agent_id": str(record.get("agent_id") or _agent_id()),
        "source": str(record.get("source") or "self"),
        "goal": str(record.get("goal") or "")[:4000],
        "state": state,
        "reason_code": str(record.get("reason_code") or "")[:200],
        "reason_human": str(record.get("reason_human") or "")[:2000],
        "summary": str(record.get("summary") or record.get("spoken_summary") or "")[:4000],
        "productive_steps": int(record.get("productive_steps") or 0),
        "stories_completed": int(record.get("stories_completed") or 0),
        "stories_total": int(record.get("stories_total") or record.get("steps_planned_count") or 0),
        "steps_json": record.get("steps") or [],
        "results_json": record.get("results") or [],
        "source_links": record.get("source_links") or [],
        "written_files": record.get("written_files") or [],
        "cloud_usd": float(record.get("cloud_usd") or 0.0),
        "updated_at": now,
        "completed_at": (now if state not in ("running",) else None),
    }


def upsert(record: dict) -> bool:
    """Upsert a job outcome by job_id. Best-effort; returns True if written. No-op
    (False) in local/companion mode or on any error — never raises into the caller."""
    if not record or not record.get("job_id"):
        return False
    sb = _sb()
    if sb is None:
        return False
    client, org = sb
    try:
        client.table("agent_jobs").upsert(_row(org, record), on_conflict="job_id").execute()
        return True
    except Exception as e:
        # Warning, not debug: a failed upsert means a real job outcome is now
        # invisible in the durable table (boot reconcile() is the repair path).
        logger.warning("[agent_jobs] upsert FAILED for job %s: %s", record.get("job_id"), e)
        return False


def reconcile(job_store, limit: int = 50) -> int:
    """Boot-time repair for the mirror split-brain: the local JSON JobStore and this
    table are written independently and best-effort, so a network blip can leave a
    job that completed locally missing (or stuck at state='running') in the durable
    table. Compare the most recent local records against the table and re-upsert any
    that are missing or stale. Returns the number of rows repaired. Best-effort —
    returns 0 on any read failure and never raises into boot."""
    sb = _sb()
    if sb is None:
        return 0
    try:
        local = job_store.list_recent(limit=limit) or []
    except Exception:
        return 0
    ids = [str(r.get("job_id")) for r in local if r.get("job_id")]
    if not ids:
        return 0
    client, org = sb
    try:
        rows = (
            client.table("agent_jobs")
            .select("job_id,state")
            .eq("org_id", org)
            .in_("job_id", ids)
            .execute()
            .data
            or []
        )
        remote = {str(r.get("job_id")): str(r.get("state") or "") for r in rows}
    except Exception as e:
        logger.warning("[agent_jobs] reconcile read failed: %s", e)
        return 0
    fixed = 0
    for meta in local:
        jid = str(meta.get("job_id") or "")
        if not jid:
            continue
        local_state = str(
            meta.get("state") or ("completed" if meta.get("success") else "failed")
        )
        remote_state = remote.get(jid)
        if remote_state is not None and not (
            remote_state == "running" and local_state != "running"
        ):
            continue  # present and not stale — nothing to repair
        try:
            record = job_store.get(jid)  # full record (steps/results), not the digest
        except Exception:
            record = None
        if record and upsert(record):
            fixed += 1
    if fixed:
        logger.info(
            "[agent_jobs] reconciled %d job outcome(s) that were missing/stale in the durable table",
            fixed,
        )
    return fixed


def list_recent(limit: int = 20, state: str | None = None) -> list[dict]:
    """Most-recent job outcomes for this org (newest first), optionally filtered by
    state. Empty list on any error or local mode (caller falls back to JobStore)."""
    sb = _sb()
    if sb is None:
        return []
    client, org = sb
    try:
        q = (
            client.table("agent_jobs")
            .select(
                "job_id,goal,state,reason_code,reason_human,summary,source,agent_id,"
                "productive_steps,stories_completed,stories_total,cloud_usd,"
                "source_links,written_files,created_at,updated_at,completed_at"
            )
            .eq("org_id", org)
            .order("updated_at", desc=True)
            .limit(max(1, min(int(limit or 20), 200)))
        )
        if state:
            q = q.eq("state", state)
        return q.execute().data or []
    except Exception as e:
        logger.debug("[agent_jobs] list_recent skipped: %s", e)
        return []


def get(job_id: str) -> dict | None:
    """Full job row by id (org-scoped). None on error / local mode / not found."""
    if not job_id:
        return None
    sb = _sb()
    if sb is None:
        return None
    client, org = sb
    try:
        rows = (
            client.table("agent_jobs")
            .select("*")
            .eq("org_id", org)
            .eq("job_id", job_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else None
    except Exception as e:
        logger.debug("[agent_jobs] get skipped: %s", e)
        return None
