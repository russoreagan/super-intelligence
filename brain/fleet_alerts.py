"""Fleet health rules — content-free alerts for the org admin's Fleet console.

`evaluate(signals)` is a pure function of a signals dict (assembled by
session_loops.fleet_signals + the console route) so every rule is unit-testable
without a brain, a database or a clock. Each alert: {code, severity ok|warn|crit,
subject, count, hint}. Nothing here carries a buyer's words — only states,
counts, timestamps and costs.
"""

from __future__ import annotations

import time

STUCK_RUNNING_S = 30 * 60.0
STUCK_APPROVAL_S = 24 * 3600.0


def _alert(code: str, severity: str, subject: str = "", count: int = 0, hint: str = "") -> dict:
    return {"code": code, "severity": severity, "subject": subject, "count": count, "hint": hint}


def _epoch(v) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        import datetime as _dt

        return _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def stuck_jobs(jobs: list[dict], now: float | None = None) -> list[dict]:
    """Running jobs not updated for 30 min, or awaiting approval for over a day."""
    ref = float(now if now is not None else time.time())
    out = []
    for j in jobs or []:
        state = str(j.get("state") or "")
        ts = _epoch(j.get("updated_at") or j.get("created_at"))
        if ts is None:
            continue
        age = ref - ts
        if (state == "running" and age > STUCK_RUNNING_S) or (
            state in ("awaiting_approval", "blocked") and age > STUCK_APPROVAL_S
        ):
            out.append(
                {"job_id": j.get("job_id") or j.get("id"), "state": state, "age_s": round(age)}
            )
    return out


def evaluate(signals: dict, now: float | None = None) -> list[dict]:
    s = signals or {}
    out: list[dict] = []
    breaker = s.get("breaker") or {}
    if breaker:
        out.append(
            _alert(
                "breaker_open",
                "crit",
                ", ".join(sorted(breaker)),
                len(breaker),
                "a cloud provider is rejecting this org's key — fix it in Settings → Providers; "
                "self-tasks and projects are paused meanwhile",
            )
        )
    dmn = s.get("dmn") or {}
    if dmn.get("dormant"):
        out.append(
            _alert(
                "org_dormant",
                "warn",
                "",
                0,
                "no human turn on any agent for dmn_pause_after_idle_s — idle thinking, "
                "self-tasks and projects are paused until someone talks",
            )
        )
    roster = dmn.get("roster") or {}
    cadence = float(roster.get("cadence_s") or 0.0)
    warn_at = float(s.get("roster_cadence_warn_s") or 600.0)
    if cadence > warn_at:
        out.append(
            _alert(
                "roster_cadence_high",
                "warn",
                f"{int(roster.get('size') or 0)} personas",
                int(roster.get("size") or 0),
                "each persona thinks idle less than once per "
                f"{int(cadence // 60)} min — set dmn_isolated_roster=active or lower "
                "dmn_active_roster_days",
            )
        )
    stuck = s.get("stuck_jobs") or []
    if stuck:
        out.append(
            _alert(
                "stuck_jobs",
                "warn",
                "",
                len(stuck),
                "running with no update for 30 min, or awaiting approval for over a day",
            )
        )
    pod = s.get("pod_budget") or {}
    if pod.get("exhausted"):
        out.append(
            _alert(
                "pod_budget_exhausted",
                "warn",
                "",
                0,
                "today's GPU pod budget is spent — local cells route to cloud or wait",
            )
        )
    cap = s.get("capacity") or {}
    n, mx = int(cap.get("personas") or 0), int(cap.get("max_personas") or 0)
    if mx and n >= 0.9 * mx:
        out.append(
            _alert(
                "clone_cap_near",
                "warn" if n < mx else "crit",
                f"{n}/{mx}",
                n,
                "raise BRAIN_MAX_PERSONAS or purge never-touched clones",
            )
        )
    for p in s.get("partners") or []:
        if p.get("over_budget"):
            out.append(
                _alert(
                    "partner_over_budget",
                    "warn",
                    str(p.get("partner_id") or ""),
                    0,
                    "partner is at 90% of partner_cloud_daily_usd_budget",
                )
            )
    if int(s.get("multi_owner") or 0):
        out.append(
            _alert(
                "multi_owner",
                "warn",
                "",
                int(s["multi_owner"]),
                "personas with more than one end user cannot be bound to an owner",
            )
        )
    if int(s.get("unmetered_spend") or 0):
        out.append(
            _alert(
                "unmetered_spend",
                "warn",
                "",
                int(s["unmetered_spend"]),
                "cloud calls without an agent attribution",
            )
        )
    return out


def worst(alerts: list[dict]) -> str:
    sev = {a.get("severity") for a in alerts or []}
    return "crit" if "crit" in sev else ("warn" if "warn" in sev else "ok")
