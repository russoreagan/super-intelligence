"""Daily GPU-uptime ceiling for the shared RunPod pod.

WHY: cloud spend has had a hard daily ceiling for a long time
(`cloud_daily_usd_budget`, enforced in brain/model_router). GPU spend had none. The
asymmetry was not academic — the pod ran 144 hours straight at $0.44/hr while serving
roughly 90 seconds of inference a day, and nothing in the system was empowered to say
"that's enough." Metered-but-uncapped is how you find out about a bill afterwards.

This is the missing counterpart: a per-calendar-day ceiling on how long the pod may be
held up, enforced by the gateway's pod reconciler. Demand-gating alone does not bound
cost, because the DMN wants to think whenever the user is idle — on a hosted tenant that
is nearly always true, so "something wants the GPU" is nearly always true too. Demand
decides WHETHER to wake; this decides HOW MUCH is affordable.

Scope: the gateway is the single owner of the shared pod, so a plain file + in-memory
counter is sufficient — no atomic-increment RPC of the kind partner_cloud_usage needs
for concurrent tenant processes. It is persisted so a gateway redeploy mid-day does not
hand the pod a fresh budget (which is precisely how a "capped" resource ends up
uncapped in practice).
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Lives beside the other shared pod files on the tenant volume so it survives a
# redeploy. Same directory contract as provisioner.HOST_SYNC_FILE.
_LEDGER = Path(os.environ.get("BRAIN_TENANTS_DIR", "tenants")).resolve() / ".pod_budget.json"


def _today() -> str:
    """UTC calendar day, matching how partner_cloud_usage rolls its daily counter."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")


def _read() -> tuple[str, float]:
    try:
        d = json.loads(_LEDGER.read_text(encoding="utf-8"))
        return str(d.get("date") or ""), float(d.get("seconds") or 0.0)
    except FileNotFoundError:
        return "", 0.0
    except Exception as e:
        logger.debug("[pod_budget] ledger read failed: %s", e)
        return "", 0.0


def spent_seconds() -> float:
    """Pod uptime already billed today. A stale date reads as 0 — the day rolled."""
    day, secs = _read()
    return secs if day == _today() else 0.0


def record_uptime(seconds: float) -> float:
    """Add `seconds` of pod uptime to today's total and return the new total.

    Called by the reconciler once per tick while the pod is up, with the elapsed
    wall-clock since the previous tick — so the ledger measures what RunPod actually
    bills (uptime), not what we managed to use it for."""
    if seconds <= 0:
        return spent_seconds()
    day, secs = _read()
    total = (secs if day == _today() else 0.0) + float(seconds)
    try:
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        tmp = _LEDGER.with_suffix(".tmp")
        tmp.write_text(json.dumps({"date": _today(), "seconds": round(total, 1)}), encoding="utf-8")
        os.replace(tmp, _LEDGER)
    except Exception as e:
        logger.debug("[pod_budget] ledger write failed: %s", e)
    return total


def budget_seconds() -> float:
    """Today's ceiling in seconds. 0 = uncapped.

    Read at call time (not import) so the dial is live-editable from the settings UI
    without a redeploy — the same contract as cloud_daily_usd_budget."""
    try:
        from brain.settings import settings

        return max(0.0, float(settings.get("pod_daily_minutes_budget") or 0.0)) * 60.0
    except Exception:
        return 0.0


def exhausted() -> bool:
    """True when today's GPU budget is spent and the pod must not be woken again.

    Uncapped (0) is always False — an explicit opt-out, not an accident, and it is
    logged loudly by the reconciler so an uncapped pod is never silently uncapped."""
    cap = budget_seconds()
    return cap > 0 and spent_seconds() >= cap


def should_hold_pod(
    *,
    full_tier_brains: int,
    demand_age_s: float | None,
    grace_s: float,
    over_budget: bool,
) -> bool:
    """Should the shared GPU pod be held up right now? The whole wake/sleep decision,
    as a pure function so it can be tested without a gateway, a RunPod key, or a clock.

    All three conditions must hold:
      • a FULL-tier brain is alive — a lite brain remaps every local route to cloud and
        would spin a GPU it can never use;
      • something asked for the pod within `grace_s` — demand, not mere process
        liveness, which a keepalive cron makes permanently true;
      • today's GPU budget is not spent.

    `demand_age_s is None` means nothing has ever asked, which is no-demand.
    """
    if full_tier_brains <= 0 or over_budget:
        return False
    return demand_age_s is not None and demand_age_s <= grace_s


def status() -> dict:
    """Snapshot for /health and the ops dashboard — GPU spend beside cloud spend.

    Dollars are derived from the pod's advertised hourly rate so the number on the
    dashboard is directly comparable to the cloud_usd figures next to it. GPU cost was
    invisible on every surface that showed cloud cost, which is the whole reason six
    days of idle burn went unnoticed."""
    spent = spent_seconds()
    cap = budget_seconds()
    rate = float(os.environ.get("RUNPOD_COST_PER_HR", "0.44") or 0.0)
    return {
        "minutes_used": round(spent / 60.0, 1),
        "minutes_budget": round(cap / 60.0, 1) if cap else 0,
        "usd_today": round(spent / 3600.0 * rate, 3),
        "usd_budget": round(cap / 3600.0 * rate, 2) if cap else 0,
        "exhausted": exhausted(),
        "uncapped": cap == 0,
    }
