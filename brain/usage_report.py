"""GET /v1/usage — the org's bill, per UTC day per persona.

Two ledgers, one report. `agent_usage` (016) holds model calls, cloud dollars and
POOL inference seconds (`pod_s`) per persona; `gpu_usage` (038) holds the uptime
of standalone / org pods the org's dedicated instances were given. The report
joins them per (day, persona):

  calls, cloud_calls, cloud_usd     from agent_usage
  pod_hours_shared  = Σ pod_s / 3600          the pool share (basic tier)
  pod_usd_shared    = pod_hours_shared × rate  pricing wording, not a meter
  pod_hours_dedicated = Σ gpu_usage.seconds / 3600   standalone / org wall-clock
  gpu_usd           = Σ gpu_usage.usd          what those pods actually cost

plus the budgets the numbers are measured against. Pure over the two readers so
it can be tested without a database; the route runs the readers in a thread.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

MAX_WINDOW_DAYS = 92
DEFAULT_WINDOW_DAYS = 7

_FIELDS = (
    "calls",
    "cloud_calls",
    "cloud_usd",
    "pod_hours_shared",
    "pod_usd_shared",
    "pod_hours_dedicated",
    "gpu_usd",
)


class UsageWindowError(ValueError):
    """A since/until pair the report cannot serve (400)."""


def parse_day(value: str | None, field: str) -> datetime | None:
    """'YYYY-MM-DD' or ISO-8601 → aware UTC datetime; None when unset."""
    if value is None or str(value).strip() == "":
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        if len(s) == 10:
            d = date.fromisoformat(s)
            return datetime(d.year, d.month, d.day, tzinfo=UTC)
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise UsageWindowError(f"{field} must be YYYY-MM-DD or ISO-8601") from e
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def window(
    since: str | None, until: str | None, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """[since, until) as UTC datetimes. Default: the last DEFAULT_WINDOW_DAYS UTC
    days including today (until = tomorrow 00:00Z, exclusive). Capped at
    MAX_WINDOW_DAYS; until must be after since."""
    now = now or datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    u = parse_day(until, "until") or (today + timedelta(days=1))
    s = parse_day(since, "since") or (u - timedelta(days=DEFAULT_WINDOW_DAYS))
    if u <= s:
        raise UsageWindowError("until must be after since")
    if (u - s) > timedelta(days=MAX_WINDOW_DAYS):
        raise UsageWindowError(f"window is capped at {MAX_WINDOW_DAYS} days")
    return s, u


def _zero() -> dict:
    return dict.fromkeys(_FIELDS, 0)


def _add(dst: dict, src: dict) -> None:
    for k in _FIELDS:
        dst[k] = dst.get(k, 0) + src.get(k, 0)


def _round(d: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


def build(
    agent_rows: list[dict],
    gpu_rows: list[dict],
    *,
    since: datetime,
    until: datetime,
    rate_per_hr: float,
    budgets: dict,
    home_persona: str = "",
) -> dict:
    """Join the two per-day readers into the report shape."""
    days: dict[str, dict[str, dict]] = {}

    def cell(day: str, persona: str) -> dict:
        p = persona or home_persona or "(org)"
        return days.setdefault(day, {}).setdefault(p, _zero())

    for r in agent_rows:
        c = cell(r["day"], r.get("persona") or "")
        hrs = float(r.get("pod_s") or 0.0) / 3600.0
        _add(
            c,
            {
                "calls": int(r.get("calls") or 0),
                "cloud_calls": int(r.get("cloud_calls") or 0),
                "cloud_usd": float(r.get("cloud_usd") or 0.0),
                "pod_hours_shared": hrs,
                "pod_usd_shared": hrs * float(rate_per_hr or 0.0),
            },
        )
    for r in gpu_rows:
        c = cell(r["day"], r.get("persona") or "")
        _add(
            c,
            {
                "pod_hours_dedicated": float(r.get("seconds") or 0.0) / 3600.0,
                "gpu_usd": float(r.get("usd") or 0.0),
            },
        )

    out_days: list[dict] = []
    totals = _zero()
    persona_totals: dict[str, dict] = {}
    for day in sorted(days):
        personas = {p: _round(v) for p, v in sorted(days[day].items())}
        day_total = _zero()
        for p, v in days[day].items():
            _add(day_total, v)
            _add(persona_totals.setdefault(p, _zero()), v)
        _add(totals, day_total)
        out_days.append({"day": day, "personas": personas, "totals": _round(day_total)})
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "rate_per_hr": round(float(rate_per_hr or 0.0), 4),
        "days": out_days,
        "personas": {p: _round(v) for p, v in sorted(persona_totals.items())},
        "totals": _round(totals),
        "budgets": {
            "cloud_daily_usd_budget": float(budgets.get("cloud_daily_usd_budget") or 0.0),
            "partner_cloud_daily_usd_budget": float(
                budgets.get("partner_cloud_daily_usd_budget") or 0.0
            ),
            "gpu_daily_usd_budget": float(budgets.get("gpu_daily_usd_budget") or 0.0),
            "gpu_usd_today": round(float(budgets.get("gpu_usd_today") or 0.0), 4),
        },
    }


def gather(since: str | None, until: str | None) -> dict:
    """Blocking: read both ledgers and the budgets, then build(). The route runs
    this in a thread."""
    from brain import agent_usage_store, gpu_usage_store, org_settings
    from brain.settings import settings

    s, u = window(since, until)
    agent_rows = agent_usage_store.by_day(s.isoformat(), u.isoformat())
    gpu_rows = gpu_usage_store.by_day(s.isoformat(), u.isoformat())
    budgets = {
        "cloud_daily_usd_budget": settings.get("cloud_daily_usd_budget", 0.0),
        "partner_cloud_daily_usd_budget": settings.get("partner_cloud_daily_usd_budget", 0.0),
        "gpu_daily_usd_budget": org_settings.gpu_daily_usd_budget(),
        "gpu_usd_today": gpu_usage_store.usd_today(),
    }
    return build(
        agent_rows,
        gpu_rows,
        since=s,
        until=u,
        rate_per_hr=gpu_usage_store.rate_per_hr(),
        budgets=budgets,
        home_persona=org_settings.home_persona(),
    )
