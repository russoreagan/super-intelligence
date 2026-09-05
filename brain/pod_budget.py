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
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Lives beside the other shared pod files on the tenant volume so it survives a
# redeploy. Same directory contract as provisioner.HOST_SYNC_FILE.
_LEDGER = Path(os.environ.get("BRAIN_TENANTS_DIR", "tenants")).resolve() / ".pod_budget.json"


def _today() -> str:
    """UTC calendar day, matching how partner_cloud_usage rolls its daily counter."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")


def _read_state() -> dict:
    """Whole persisted state. A corrupt or missing file reads as empty — the reconciler
    must keep running, and the worst case is one day billed from zero."""
    try:
        d = json.loads(_LEDGER.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.debug("[pod_budget] ledger read failed: %s", e)
        return {}


def _write_state(st: dict) -> None:
    try:
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        tmp = _LEDGER.with_suffix(".tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, _LEDGER)
    except Exception as e:
        logger.debug("[pod_budget] ledger write failed: %s", e)


def spent_seconds() -> float:
    """Pod uptime already billed today. A stale date reads as 0 — the day rolled."""
    st = _read_state()
    return float(st.get("seconds") or 0.0) if str(st.get("date") or "") == _today() else 0.0


def record_uptime(seconds: float) -> float:
    """Add `seconds` of pod uptime to today's total and return the new total.

    Called by the reconciler once per tick while the pod is up, with the elapsed
    wall-clock since the previous tick — so the ledger measures what RunPod actually
    bills (uptime), not what we managed to use it for. The cooldown state rides along in
    the same file and is deliberately NOT reset by the day rollover: an unproductive pod
    at 23:59 is still unproductive at 00:01."""
    if seconds <= 0:
        return spent_seconds()
    st = _read_state()
    prior = float(st.get("seconds") or 0.0) if str(st.get("date") or "") == _today() else 0.0
    total = prior + float(seconds)
    st["date"] = _today()
    st["seconds"] = round(total, 1)
    _write_state(st)
    return total


# Fallback $/hr when the live pod's rate isn't known yet. Deliberately the PESSIMISTIC
# end — RunPodManager._PRICE_CEILING, the most it will ever pay for a card — because
# this converts a dollar ceiling into an uptime allowance. Guessing low (say $0.44 when
# the pod actually costs $0.50) would silently overshoot the dollar target by 14%; a cost
# ceiling must never round in favour of spending more.
_FALLBACK_RATE_PER_HR = 0.50
_rate_per_hr: float | None = None


def set_rate_per_hr(rate: float | None) -> None:
    """Tell the ledger what the live pod actually costs, so the dollar budget converts
    to seconds using the real rate rather than a guess. The reconciler calls this each
    tick from the held pod's cost_per_hr; None or 0 leaves the previous value alone.

    This matters because the pod's price is not fixed: the manager ranks GPUs and takes
    the best one under its price ceiling, so the same uptime can cost meaningfully
    different amounts on different days."""
    global _rate_per_hr
    if rate and rate > 0:
        _rate_per_hr = float(rate)


def rate_per_hr() -> float:
    """$/hr for the pod: the live rate when known, else the env override, else the
    pessimistic ceiling."""
    if _rate_per_hr:
        return _rate_per_hr
    try:
        env = float(os.environ.get("RUNPOD_COST_PER_HR", "") or 0.0)
    except ValueError:
        env = 0.0
    return env if env > 0 else _FALLBACK_RATE_PER_HR


def budget_usd() -> float:
    """Today's ceiling in dollars. 0 = uncapped.

    Read at call time (not import) so the dial is live-editable from the settings UI
    without a redeploy — the same contract as cloud_daily_usd_budget."""
    try:
        from brain.settings import settings

        return max(0.0, float(settings.get("pod_daily_usd_budget") or 0.0))
    except Exception:
        return 0.0


def budget_seconds() -> float:
    """Today's ceiling as an uptime allowance in seconds. 0 = uncapped.

    Denominated in DOLLARS and converted here, not stored as minutes. The ceiling exists
    to bound money, and a minutes dial does not: at the manager's price ceiling the same
    1363 minutes is $10.00 on a $0.44/hr card and $11.36 on a $0.50/hr one. Converting at
    the live rate keeps the number the operator set the number they actually pay."""
    usd = budget_usd()
    return (usd / rate_per_hr()) * 3600.0 if usd > 0 else 0.0


def exhausted() -> bool:
    """True when today's GPU budget is spent and the pod must not be woken again.

    Uncapped (0) is always False — an explicit opt-out, not an accident, and it is
    logged loudly by the reconciler so an uncapped pod is never silently uncapped."""
    cap = budget_seconds()
    return cap > 0 and spent_seconds() >= cap


# Cooldown after a pod ran a full grace period without producing anything: doubling
# from 15 minutes, capped at 4 hours. Bounds the wake→nothing→sleep→wake churn without
# blocking recovery for a whole day if the cause was transient.
_COOLDOWN_BASE_S = 900.0
_COOLDOWN_MAX_S = 4 * 3600.0


def record_sleep(produced: bool) -> None:
    """Called when the pod is put to sleep. `produced` says whether it did any real work
    during the session that just ended; an unproductive one arms an escalating cooldown
    so the next wake request cannot immediately restart the cycle."""
    st = _read_state()
    if produced:
        st["unproductive_streak"] = 0
        st["cooldown_until"] = 0.0
    else:
        streak = int(st.get("unproductive_streak") or 0) + 1
        st["unproductive_streak"] = streak
        st["cooldown_until"] = time.time() + min(
            _COOLDOWN_MAX_S, _COOLDOWN_BASE_S * (2 ** (streak - 1))
        )
    _write_state(st)


def cooldown_remaining_s() -> float:
    """Seconds until an unproductive-pod cooldown lifts. 0 = no cooldown."""
    return max(0.0, float(_read_state().get("cooldown_until") or 0.0) - time.time())


def should_hold_pod(
    *,
    full_tier_brains: int,
    demand_age_s: float | None,
    grace_s: float,
    over_budget: bool,
    pod_is_up: bool = False,
    use_age_s: float | None = None,
    up_for_s: float | None = None,
    cooldown_active: bool = False,
) -> bool:
    """Should the shared GPU pod be held up right now? The whole wake/sleep decision,
    as a pure function so it can be tested without a gateway, a RunPod key, or a clock.

    Two preconditions always apply:
      • a FULL-tier brain is alive — a lite brain remaps every local route to cloud and
        would spin a GPU it can never use;
      • today's GPU budget is not spent.

    Then the signal depends on which way the pod is moving, because "should I start
    paying?" and "should I keep paying?" are different questions:

      • WAKING (pod down) keys off DEMAND — something asked. It has to: a sleeping pod
        can serve nothing, so productive use is impossible until it is up, and gating
        the wake on use would make a slept pod permanently unwakeable.

      • HOLDING (pod up) keys off USE — something actually got output back. Demand is
        the wrong signal here: the DMN asks on every idle tick regardless of what it
        receives, so a pod producing nothing looks exactly like a pod doing real work
        and stays up all day at $0.50/hr. Falling back to demand once the pod is up
        would reintroduce the original bug wearing a different hat.

    A pod that is up and has produced nothing within `grace_s` therefore goes back to
    sleep even while requests keep arriving — which is the whole point: run as long as
    it is being used, idle down when it is not.
    """
    if full_tier_brains <= 0 or over_budget:
        return False
    if not pod_is_up:
        # A cooldown blocks only the WAKE. It is set after a pod ran a full grace period
        # producing nothing, and without it this design churns: wake → produce nothing →
        # sleep → demand is still fresh → wake again, forever. Under a network volume
        # each of those cycles is a pod CREATE and TERMINATE, so the failure mode is
        # worse than the leak it replaced.
        if cooldown_active:
            return False
        return demand_age_s is not None and demand_age_s <= grace_s
    # Up: keep it only while it is earning its keep...
    if use_age_s is not None and use_age_s <= grace_s:
        return True
    # ...but a freshly woken pod has not had a chance to produce anything yet (and any
    # use_age_s it does have is stale, from a previous session). Grace it from the wake,
    # or it gets killed mid-boot and can never reach the state that would justify it.
    return up_for_s is not None and up_for_s <= grace_s


def status() -> dict:
    """Snapshot for /health and the ops dashboard — GPU spend beside cloud spend.

    Dollars are derived from the pod's advertised hourly rate so the number on the
    dashboard is directly comparable to the cloud_usd figures next to it. GPU cost was
    invisible on every surface that showed cloud cost, which is the whole reason six
    days of idle burn went unnoticed."""
    spent = spent_seconds()
    cap = budget_seconds()
    rate = rate_per_hr()
    return {
        "minutes_used": round(spent / 60.0, 1),
        "minutes_budget": round(cap / 60.0, 1) if cap else 0,
        "usd_today": round(spent / 3600.0 * rate, 3),
        "usd_budget": round(budget_usd(), 2),
        "rate_per_hr": rate,
        "exhausted": exhausted(),
        "uncapped": cap == 0,
    }
