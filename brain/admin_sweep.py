"""The Admin's status sweep — watch the org, and fix what is safely fixable.

The Admin used to "monitor" by thinking idly and turning its ideas into motor jobs.
Those jobs ran under a lockdown (no shell, no network, no writes), so a typical one
was "run a performance audit" that failed at its first step, deferred, and spawned
the next idea. This replaces that with a cheap deterministic loop:

  sweep   — every `admin_sweep_interval_s`, read the org's live signals (provider
            breaker, job ledger, project rows, connectors). No model call.
  issue   — anything failing or wedged. A healthy sweep ends here.
  fix     — the one bounded remedy each issue type has, when its precondition holds.
            Four remedies exist, and nothing else is ever changed:
              reset_breaker    clear a provider hold, then probe; restore it if the
                               probe fails (a rejected key needs a human)
              kill_job         stop a job running with no update for 30 min
              release_project  free a project row whose claimed task is gone
              reload_connectors re-read connector config, clearing connector breakers
            Jobs awaiting approval and projects blocked on a person are reported,
            never touched: they are waiting on a human by design.
  explain — one cheap model call in The Admin's voice over the evidence, only when
            a sweep found something new. A per-issue cooldown keeps a lingering
            problem from re-reporting every sweep.

Everything here is pure (no brain, no database, no clock) so every rule is
unit-testable; session_loops._admin_sweep_loop gathers the inputs and applies the
fixes. Evidence is CONTENT-FREE like admin_briefing: states, counts, reason codes,
provider and connector names. Never a job goal, never conversation text.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from brain import fleet_alerts

logger = logging.getLogger(__name__)

LOG_FILENAME = "admin_sweeps.jsonl"
LOG_KEEP = 200
# A project row claimed this long ago whose task is no longer queued has lost its
# worker (same lease as agent_projects_store.clear_in_flight).
PROJECT_LEASE_S = 30 * 60.0
# A project blocked on a person for this long is worth mentioning (not fixing).
PROJECT_BLOCKED_REPORT_S = 24 * 3600.0
# Failed jobs are counted over this window (the sweep interval is usually shorter,
# the cooldown stops repeats).
FAILED_WINDOW_S = 2 * 3600.0
FAILED_MIN = 2
# Already-settled bookkeeping, not failures of the work itself.
_SETTLED_REASONS = frozenset({"stale_running", "approval_expired"})
# Providers the breaker fix can probe with a cheap router model.
PROBE_MODELS = {"anthropic": "haiku"}

FIXES = ("reset_breaker", "kill_job", "release_project", "reload_connectors")

_log_lock = threading.Lock()


def _issue(code: str, subject: str, severity: str, detail: str, fix: str = "", **evidence) -> dict:
    return {
        "key": f"{code}:{subject}" if subject else code,
        "code": code,
        "subject": subject,
        "severity": severity,
        "detail": detail,
        "fix": fix,
        "evidence": evidence,
    }


def find_issues(
    *,
    signals: dict | None,
    jobs: list[dict] | None,
    projects: list[dict] | None = None,
    live_task_ids: set[str] | frozenset[str] = frozenset(),
    connectors: list[dict] | None = None,
    connector_health: dict | None = None,
    now: float | None = None,
) -> list[dict]:
    """Everything failing or wedged right now, each with the fix it qualifies for
    ("" = report only). Pure function of its inputs."""
    ref = float(now if now is not None else time.time())
    s = signals or {}
    out: list[dict] = []

    for provider, o in sorted((s.get("breaker") or {}).items()):
        o = o or {}
        held = float(o.get("until") or 0.0) > ref
        out.append(
            _issue(
                "breaker_open",
                provider,
                "crit",
                f"{provider} is rejecting this org's key ({o.get('kind') or 'unknown'})",
                "reset_breaker" if held and provider in PROBE_MODELS else "",
                kind=o.get("kind") or "",
                strikes=int(o.get("strikes") or 0),
                held_for_s=round(max(0.0, ref - float(o.get("since") or ref))),
            )
        )

    for st in fleet_alerts.stuck_jobs(jobs or [], now=ref):
        jid = str(st.get("job_id") or "")
        if st.get("state") == "running":
            out.append(
                _issue(
                    "stuck_job",
                    jid,
                    "warn",
                    "a job has been running with no progress for over 30 min",
                    "kill_job",
                    age_s=st.get("age_s"),
                )
            )
        else:
            out.append(
                _issue(
                    "waiting_on_human",
                    jid,
                    "warn",
                    f"a job has been {st.get('state')} for over a day",
                    "",
                    state=st.get("state"),
                    age_s=st.get("age_s"),
                )
            )

    by_reason: dict[str, int] = {}
    for j in jobs or []:
        if str(j.get("state") or "") != "failed":
            continue
        ts = fleet_alerts._epoch(j.get("updated_at") or j.get("completed_at"))  # noqa: SLF001
        if ts is None or ref - ts > FAILED_WINDOW_S:
            continue
        reason = str(j.get("reason_code") or "unknown")
        if reason in _SETTLED_REASONS:
            continue
        by_reason[reason] = by_reason.get(reason, 0) + 1
    for reason, n in sorted(by_reason.items()):
        if n >= FAILED_MIN:
            out.append(
                _issue(
                    "jobs_failing",
                    reason,
                    "warn",
                    f"{n} jobs failed in the last {int(FAILED_WINDOW_S // 3600)} h ({reason})",
                    "",
                    count=n,
                    reason_code=reason,
                    reason_human=next(
                        (
                            str(j.get("reason_human") or "")[:200]
                            for j in jobs or []
                            if str(j.get("reason_code") or "unknown") == reason
                            and j.get("reason_human")
                        ),
                        "",
                    ),
                )
            )

    for p in projects or []:
        state = str(p.get("state") or "")
        pid = str(p.get("id") or "")
        if state == "running":
            started = float(p.get("last_started_at") or 0.0)
            task = str(p.get("in_flight_task_id") or "")
            if started and ref - started > PROJECT_LEASE_S and task not in live_task_ids:
                out.append(
                    _issue(
                        "stuck_project",
                        pid,
                        "warn",
                        "a project is marked running but its task is no longer queued",
                        "release_project",
                        age_s=round(ref - started),
                    )
                )
        elif state == "blocked":
            since = float(p.get("updated_at") or p.get("last_finished_at") or 0.0)
            if since and ref - since > PROJECT_BLOCKED_REPORT_S:
                out.append(
                    _issue(
                        "project_waiting",
                        pid,
                        "warn",
                        "a project has been blocked on a person for over a day",
                        "",
                        age_s=round(ref - since),
                    )
                )
        elif state == "failed":
            out.append(
                _issue(
                    "project_failed",
                    pid,
                    "warn",
                    "a project failed three times in a row and is quarantined",
                    "",
                    failures=int(p.get("consecutive_failures") or 0),
                )
            )

    broken = sorted(
        {str(c.get("name")) for c in connectors or [] if str(c.get("status") or "") == "error"}
        | {str(n) for n, h in (connector_health or {}).items() if (h or {}).get("disabled")}
    )
    if broken:
        out.append(
            _issue(
                "connector_error",
                ",".join(broken),
                "warn",
                f"connector{'s' if len(broken) > 1 else ''} in error: {', '.join(broken)}",
                "reload_connectors",
                connectors=broken,
            )
        )

    if (s.get("pod_budget") or {}).get("exhausted"):
        out.append(
            _issue("pod_budget_exhausted", "", "warn", "today's GPU pod budget is spent", "")
        )
    return out


class Cooldown:
    """Per-issue memory so a lingering problem is reported (and fixed) once per
    `cooldown_s`, not every sweep. In-memory: a restarted brain re-checks once."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}

    def fresh(self, issues: list[dict], cooldown_s: float, now: float | None = None) -> list[dict]:
        ref = float(now if now is not None else time.time())
        out = []
        for i in issues:
            last = self._seen.get(i["key"])
            if last is None or ref - last >= cooldown_s:
                self._seen[i["key"]] = ref
                out.append(i)
        # An issue that cleared can be reported again the moment it recurs.
        live = {i["key"] for i in issues}
        for k in [k for k in self._seen if k not in live]:
            self._seen.pop(k, None)
        return out


# ── Explanation ────────────────────────────────────────────────────────────────


def fallback_text(issues: list[dict], results: dict[str, dict]) -> str:
    """The report without a model: one line per issue, plus what was done."""
    lines = []
    for i in issues:
        r = results.get(i["key"]) or {}
        tail = ""
        if r.get("applied"):
            tail = f" — fixed: {r.get('note') or i['fix']}"
        elif r.get("note"):
            tail = f" — not fixed: {r['note']}"
        lines.append(f"- {i['detail']}{tail}")
    return "\n".join(lines)


def system_prompt(self_md: str) -> str:
    return (
        (self_md.strip() + "\n\n" if self_md else "")
        + "You are The Admin writing a short incident note for the org's administrator. "
        "You get the issues a status sweep found and the fixes already applied. For each "
        "issue, say in one or two plain sentences what is wrong, the most likely reason "
        "given the evidence, and whether it is fixed or what a person needs to do. Do "
        "not invent facts beyond the evidence. No headings, no preamble."
    )


def user_prompt(issues: list[dict], results: dict[str, dict]) -> str:
    payload = [
        {
            "issue": i["code"],
            "subject": i["subject"],
            "detail": i["detail"],
            "evidence": i["evidence"],
            "fix": results.get(i["key"]) or {"applied": False, "note": "no automatic fix"},
        }
        for i in issues
    ]
    return "Sweep findings:\n" + json.dumps(payload, indent=1, default=str)


# ── Sweep log (tenant-local JSONL) ─────────────────────────────────────────────


def _log_path() -> Path | None:
    try:
        from brain.security import tenant_root

        root = tenant_root()
    except Exception:
        root = None
    if root is None:
        sb = os.environ.get("SECOND_BRAIN_PATH", "").strip()
        root = Path(sb) if sb else None
    return (Path(root) / LOG_FILENAME) if root else None


def record(entry: dict) -> None:
    """Append one sweep record, keeping the newest LOG_KEEP. Never raises."""
    path = _log_path()
    if path is None:
        return
    try:
        with _log_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = path.read_text().splitlines() if path.exists() else []
            lines.append(json.dumps(entry, default=str))
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines[-LOG_KEEP:]) + "\n")
            tmp.replace(path)
    except Exception as e:
        logger.debug("[AdminSweep] could not write the sweep log: %s", e)


def recent(limit: int = 20) -> list[dict]:
    """Newest-first sweep records. [] when there is no log yet."""
    path = _log_path()
    if path is None or not path.exists():
        return []
    out = []
    try:
        for line in reversed(path.read_text().splitlines()):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
            if len(out) >= max(1, int(limit)):
                break
    except Exception:
        return []
    return out
