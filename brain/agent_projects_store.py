"""agent_projects — the agent-scoped standing work queue (migration 034).

One row per PROJECT (a standing backlog item that the DMN advances across many
runs), not per run — runs are PersistentTaskQueue.Task and agent_jobs. Each row is
linked to an AGENT (persona × mandate), which is what lets the job that advances it
run under that agent's permissions instead of the org ceiling.

Why a table and not the markdown section it replaces: the section was regex-parsed
out of a file that rode in every turn's prompt, its status was free text that
substring-matching kept eligible forever, and a status write resolved its target
filename at COMPLETION time so it could land in a different mandate's ledger than it
was read from. Markdown survives only as a human INPUT surface, imported one way.

Backends:
  - supabase (hosted): the agent_projects table. Every chain carries `.eq("org_id")`
    or an `on_conflict` naming org_id — tests/security/test_org_scoping.py walks the
    AST and refuses anything else. A failed call logs and returns []/False/None and
    NEVER falls back to the local file: for an authorization list, "select nothing
    this tick" is the correct direction to fail, and a hosted brain quietly writing
    project state into a local file nobody reconciles is the 2026-07-17 split-brain.
  - local (dev/tests): second_brain/agent_projects.json, atomic temp→rename. This is
    a real backend, not a no-op — once the markdown is no longer authoritative there
    is no other local source of truth.

Records are dicts with epoch-float time fields in BOTH backends (ISO in the
database, converted here), so the pure scheduler sees one shape.

Reads are cached for CACHE_TTL_S and invalidated by every write from this process:
the task worker polls every 3s, and an unguarded table read there is the same shape
as the DMN roster N+1 that was once 60% of all Supabase traffic.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

TABLE = "agent_projects"
CACHE_TTL_S = 60.0
MAX_FAILURES = 3
DEFAULT_BACKOFF_S = 300.0
LOCAL_FILENAME = "agent_projects.json"

READY, RUNNING, PENDING, BLOCKED, DONE, FAILED, CANCELLED = (
    "ready",
    "running",
    "pending",
    "blocked",
    "done",
    "failed",
    "cancelled",
)
CLAIMABLE = (READY, PENDING)

TIME_FIELDS = (
    "ready_at",
    "deferred_until",
    "deadline_at",
    "appraised_at",
    "last_started_at",
    "last_finished_at",
    "created_at",
    "updated_at",
)
LIST_FIELDS = ("unblocks", "bears_on")
# Content the importer / user may refresh. Lifecycle fields are never in this set.
CONTENT_FIELDS = ("title", "task", "priority", "max_runs", "deadline_at", "unblocks")

_local_lock = threading.Lock()
_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, list[dict]]] = {}
_spend_cache: tuple[float, dict[str, float]] | None = None


# ── Backend plumbing ─────────────────────────────────────────────────────────


def _backend() -> str:
    return os.environ.get("BRAIN_STORAGE_BACKEND", "local").lower()


def _sb():
    """(client, org_id) on the Supabase backend, else None. Callers decide what a
    None means: on the local backend it is expected; on the hosted backend it is a
    failure and they fail CLOSED."""
    try:
        from brain.second_brain import supabase_client

        if not supabase_client.is_enabled():
            return None
        return supabase_client.get_client(), supabase_client.get_org_id()
    except Exception:
        return None


def _local_path() -> Path:
    root = os.environ.get("SECOND_BRAIN_PATH", "").strip()
    base = Path(root) if root else Path(__file__).resolve().parent.parent / "second_brain"
    return base / LOCAL_FILENAME


def _local_read() -> dict[str, dict]:
    p = _local_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): dict(v) for k, v in (data or {}).items()}
    except (OSError, ValueError):
        return {}


def _local_write(rows: dict[str, dict]) -> None:
    p = _local_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def _now() -> float:
    return time.time()


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(float(ts), _dt.UTC).isoformat()


def _epoch(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).replace("Z", "+00:00")
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.UTC)
        return d.timestamp()
    except ValueError:
        return None


def _to_db(rec: dict) -> dict:
    out = dict(rec)
    for f in TIME_FIELDS:
        if f in out:
            out[f] = _iso(out[f])
    for f in LIST_FIELDS:
        out[f] = list(out.get(f) or [])
    return out


def _from_db(row: dict) -> dict:
    out = dict(row)
    for f in TIME_FIELDS:
        if f in out:
            out[f] = _epoch(out[f])
    for f in LIST_FIELDS:
        out[f] = list(out.get(f) or [])
    return out


def invalidate_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ── Ids and records ──────────────────────────────────────────────────────────


def project_id(persona: str, mandate_id: str, title: str) -> str:
    """Deterministic: the markdown importer is idempotent without a uniqueness
    constraint, and the PK (id, org_id) puts org_id in every on_conflict."""
    key = f"{str(persona).strip()}|{str(mandate_id).strip()}|{str(title).strip().lower()}"
    return "p-" + hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def new_record(
    persona: str,
    mandate_id: str,
    title: str,
    task: str,
    *,
    priority: int = 2,
    user_waiting: bool = False,
    source: str = "manual",
    max_runs: int = 1,
    state: str = READY,
    unblocks: list[str] | tuple[str, ...] = (),
    deadline_at: float | None = None,
    status_note: str = "",
) -> dict:
    now = _now()
    return {
        "id": project_id(persona, mandate_id, title),
        "persona": str(persona).strip(),
        "mandate_id": str(mandate_id or "").strip(),
        "title": str(title).strip()[:200],
        "task": str(task).strip()[:4000],
        "priority": int(priority),
        "user_waiting": bool(user_waiting),
        "unblocks": list(unblocks),
        "deadline_at": deadline_at,
        "urgency_score": None,
        "est_cost_usd": None,
        "bears_on": [],
        "appraised_at": None,
        "state": state if state in (READY, PENDING, BLOCKED, DONE, FAILED, CANCELLED) else READY,
        "status_note": str(status_note)[:400],
        "max_runs": int(max_runs),
        "ready_at": now,
        "deferred_until": None,
        "blocked_reason": "",
        "runs": 0,
        "consecutive_failures": 0,
        "in_flight_task_id": "",
        "last_started_at": None,
        "last_finished_at": None,
        "last_job_id": "",
        "source": str(source)[:40],
        "created_at": now,
        "updated_at": now,
    }


# ── Reads ────────────────────────────────────────────────────────────────────


def list_for_personas(personas: list[str] | tuple[str, ...]) -> list[dict]:
    """Every project (any state, ALL mandates) for these personas. Cached."""
    key = tuple(sorted({str(p) for p in personas if p}))
    if not key:
        return []
    now = _now()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_S:
            return [dict(r) for r in hit[1]]
    rows: list[dict]
    if _backend() == "supabase":
        sb = _sb()
        if sb is None:
            logger.warning("[agent_projects] list skipped — Supabase client unavailable")
            return []
        client, org = sb
        try:
            res = (
                client.table(TABLE)
                .select("*")
                .eq("org_id", org)
                .in_("persona", list(key))
                .execute()
            )
            rows = [_from_db(r) for r in (res.data or [])]
        except Exception as e:
            logger.warning("[agent_projects] list FAILED: %s", e)
            return []
    else:
        with _local_lock:
            rows = [dict(r) for r in _local_read().values() if r.get("persona") in key]
    with _cache_lock:
        _cache[key] = (now, [dict(r) for r in rows])
    return rows


def list_for_agent(persona: str, mandate_id: str) -> list[dict]:
    m = str(mandate_id or "").strip()
    return [r for r in list_for_personas([persona]) if str(r.get("mandate_id") or "") == m]


def get(pid: str) -> dict | None:
    if not pid:
        return None
    if _backend() == "supabase":
        sb = _sb()
        if sb is None:
            return None
        client, org = sb
        try:
            res = client.table(TABLE).select("*").eq("org_id", org).eq("id", pid).limit(1).execute()
            rows = res.data or []
            return _from_db(rows[0]) if rows else None
        except Exception as e:
            logger.debug("[agent_projects] get skipped: %s", e)
            return None
    with _local_lock:
        r = _local_read().get(pid)
        return dict(r) if r else None


# ── Writes ───────────────────────────────────────────────────────────────────


def _local_update(pid: str, patch: dict, *, only_if_state: tuple[str, ...] | None = None) -> bool:
    with _local_lock:
        rows = _local_read()
        r = rows.get(pid)
        if not r:
            return False
        if only_if_state is not None and r.get("state") not in only_if_state:
            return False
        r.update(patch)
        rows[pid] = r
        _local_write(rows)
    invalidate_cache()
    return True


def _sb_update(pid: str, patch: dict, *, only_if_state: tuple[str, ...] | None = None) -> bool:
    sb = _sb()
    if sb is None:
        logger.warning("[agent_projects] update skipped — Supabase client unavailable")
        return False
    client, org = sb
    try:
        q = client.table(TABLE).update(_to_db(patch)).eq("org_id", org).eq("id", pid)
        if only_if_state is not None:
            q = q.in_("state", list(only_if_state))
        res = q.execute()
        ok = bool(res.data)
    except Exception as e:
        logger.warning("[agent_projects] update FAILED for %s: %s", pid, e)
        return False
    if ok:
        invalidate_cache()
    return ok


def _update(pid: str, patch: dict, *, only_if_state: tuple[str, ...] | None = None) -> bool:
    patch = {**patch, "updated_at": _now()}
    if _backend() == "supabase":
        return _sb_update(pid, patch, only_if_state=only_if_state)
    return _local_update(pid, patch, only_if_state=only_if_state)


def _insert(rec: dict) -> bool:
    if _backend() == "supabase":
        sb = _sb()
        if sb is None:
            logger.warning("[agent_projects] insert skipped — Supabase client unavailable")
            return False
        client, org = sb
        try:
            client.table(TABLE).upsert(
                {**_to_db(rec), "org_id": org}, on_conflict="id,org_id"
            ).execute()
        except Exception as e:
            logger.warning("[agent_projects] insert FAILED for %s: %s", rec.get("id"), e)
            return False
        invalidate_cache()
        return True
    with _local_lock:
        rows = _local_read()
        rows[rec["id"]] = dict(rec)
        _local_write(rows)
    invalidate_cache()
    return True


def add(
    persona: str,
    mandate_id: str,
    title: str,
    task: str,
    *,
    priority: int = 2,
    user_waiting: bool = False,
    source: str = "manual",
    max_runs: int = 1,
    state: str = READY,
    unblocks: list[str] | tuple[str, ...] = (),
    deadline_at: float | None = None,
    status_note: str = "",
) -> str:
    """Insert if missing. Returns the project id ("" on failure). An existing row is
    left exactly as it is — its lifecycle belongs to the scheduler now."""
    rec = new_record(
        persona,
        mandate_id,
        title,
        task,
        priority=priority,
        user_waiting=user_waiting,
        source=source,
        max_runs=max_runs,
        state=state,
        unblocks=unblocks,
        deadline_at=deadline_at,
        status_note=status_note,
    )
    if get(rec["id"]) is not None:
        return rec["id"]
    return rec["id"] if _insert(rec) else ""


def upsert_content(rec: dict) -> bool:
    """Importer write: a missing project is inserted whole (initial state included);
    an existing one gets only its CONTENT fields refreshed — never its lifecycle,
    so a re-import cannot resurrect a project the scheduler has finished."""
    pid = str(rec.get("id") or "")
    if not pid:
        return False
    if get(pid) is None:
        return _insert(rec)
    patch = {k: rec[k] for k in CONTENT_FIELDS if k in rec}
    return _update(pid, patch) if patch else True


def claim(pid: str, task_id: str = "") -> bool:
    """Compare-and-set: READY/PENDING → RUNNING. False means another process got
    there first (elastic placement runs more than one brain per org)."""
    now = _now()
    return _update(
        pid,
        {"state": RUNNING, "in_flight_task_id": task_id or "claimed", "last_started_at": now},
        only_if_state=CLAIMABLE,
    )


def note_task(pid: str, task_id: str) -> bool:
    return _update(pid, {"in_flight_task_id": task_id}, only_if_state=(RUNNING,))


def release(pid: str) -> bool:
    """Undo a claim whose enqueue was deduplicated — back to READY, no run counted."""
    return _update(pid, {"state": READY, "in_flight_task_id": ""}, only_if_state=(RUNNING,))


def finish(
    pid: str,
    *,
    success: bool,
    note: str = "",
    job_id: str = "",
    backoff_s: float = 0.0,
) -> str | None:
    """The run ended. Returns the new state, or None if the row is unknown.

      success and runs ≥ max_runs (max_runs > 0) → DONE
      success otherwise                          → READY, ready_at = now (back of the queue)
      failure, failures < MAX_FAILURES           → PENDING with backoff
      failure otherwise                          → FAILED (quarantine)
    A delivered result clears user_waiting."""
    rec = get(pid)
    if rec is None:
        return None
    now = _now()
    patch: dict = {
        "in_flight_task_id": "",
        "last_finished_at": now,
        "last_job_id": str(job_id or "")[:120],
        "status_note": str(note or "")[:400],
    }
    if success:
        runs = int(rec.get("runs") or 0) + 1
        max_runs = int(rec.get("max_runs") or 0)
        patch.update({"runs": runs, "consecutive_failures": 0, "user_waiting": False})
        if max_runs > 0 and runs >= max_runs:
            state = DONE
        else:
            state = READY
            patch["ready_at"] = now
    else:
        failures = int(rec.get("consecutive_failures") or 0) + 1
        patch["consecutive_failures"] = failures
        if failures >= MAX_FAILURES:
            state = FAILED
        else:
            state = PENDING
            wait = max(float(backoff_s or 0.0), DEFAULT_BACKOFF_S * (2 ** (failures - 1)))
            patch["deferred_until"] = now + wait
    patch["state"] = state
    return state if _update(pid, patch) else None


def block(pid: str, reason: str = "") -> bool:
    return _update(
        pid,
        {"state": BLOCKED, "blocked_reason": str(reason or "")[:400], "in_flight_task_id": ""},
    )


def unblock(pid: str) -> bool:
    """The user answered: ready again, at the BACK of the aging queue, and they are
    now waiting on the follow-through."""
    return _update(
        pid,
        {"state": READY, "ready_at": _now(), "blocked_reason": "", "user_waiting": True},
        only_if_state=(BLOCKED,),
    )


def cancel(pid: str) -> bool:
    return _update(pid, {"state": CANCELLED, "in_flight_task_id": ""})


def set_state(pid: str, state: str, note: str = "") -> bool:
    """Operator override (UI / scripts). Anything → the given state."""
    if state not in (READY, PENDING, BLOCKED, DONE, FAILED, CANCELLED):
        return False
    patch: dict = {"state": state, "in_flight_task_id": ""}
    if state == READY:
        patch["ready_at"] = _now()
    if note:
        patch["status_note"] = str(note)[:400]
    return _update(pid, patch)


def clear_in_flight(personas: list[str] | tuple[str, ...] | None = None) -> int:
    """Boot repair: a pod that died mid-step leaves rows RUNNING forever. Back to
    READY; returns the number repaired."""
    if _backend() == "supabase":
        sb = _sb()
        if sb is None:
            return 0
        client, org = sb
        try:
            q = (
                client.table(TABLE)
                .update(_to_db({"state": READY, "in_flight_task_id": "", "ready_at": _now()}))
                .eq("org_id", org)
                .eq("state", RUNNING)
            )
            if personas:
                q = q.in_("persona", [str(p) for p in personas])
            res = q.execute()
            n = len(res.data or [])
        except Exception as e:
            logger.warning("[agent_projects] clear_in_flight FAILED: %s", e)
            return 0
        if n:
            invalidate_cache()
        return n
    n = 0
    with _local_lock:
        rows = _local_read()
        for r in rows.values():
            if r.get("state") == RUNNING and (not personas or r.get("persona") in set(personas)):
                r.update({"state": READY, "in_flight_task_id": "", "ready_at": _now()})
                n += 1
        if n:
            _local_write(rows)
    if n:
        invalidate_cache()
    return n


# ── Spend (for the soft fairness term and the daily-cap gate) ────────────────


def agent_spend_today() -> dict[str, float]:
    """agent_id → cloud USD spent since UTC midnight, from agent_usage_totals
    (migration 016). {} on the local backend or any error; cached CACHE_TTL_S."""
    global _spend_cache
    now = _now()
    if _spend_cache and now - _spend_cache[0] < CACHE_TTL_S:
        return dict(_spend_cache[1])
    out: dict[str, float] = {}
    if _backend() == "supabase":
        sb = _sb()
        if sb is not None:
            client, org = sb
            since = _dt.datetime.now(_dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            try:
                res = client.rpc(
                    "agent_usage_totals",
                    {"p_org_id": org, "p_since": since.isoformat(), "p_until": None},
                ).execute()
                for r in res.data or []:
                    aid = str(r.get("agent_id") or "")
                    if aid and aid != "owner":
                        out[aid] = float(r.get("cloud_usd") or 0.0)
            except Exception as e:
                logger.debug("[agent_projects] spend lookup skipped: %s", e)
    _spend_cache = (now, dict(out))
    return out
