"""Daily GPU-uptime ceiling for RunPod pods.

WHY: cloud spend has had a hard daily ceiling for a long time
(`cloud_daily_usd_budget`, enforced in brain/model_router). GPU spend had none. The
asymmetry was not academic — the pod ran 144 hours straight at $0.44/hr while serving
roughly 90 seconds of inference a day, and nothing in the system was empowered to say
"that's enough." Metered-but-uncapped is how you find out about a bill afterwards.

This is the missing counterpart: a per-calendar-day ceiling on how long pods may be
held up, enforced by the gateway's pod reconciler. Demand-gating alone does not bound
cost, because the DMN wants to think whenever the user is idle — on a hosted tenant that
is nearly always true, so "something wants the GPU" is nearly always true too. Demand
decides WHETHER to wake; this decides HOW MUCH is affordable.

Two layers:

  PodLedger(path)   — one ledger file: uptime seconds billed today, the unproductive-
                      session cooldown, and the rate it converts dollars at. The
                      platform pool uses one (tenants/.pod_budget.json); the premium
                      tier (plan §10.4) gives each org its own
                      (tenants/<org>/.gpu_budget.json) with the same class.
  module functions  — the platform ledger, kept as the flat API the reconciler and
                      /health already call. `record_uptime` is additive, so calling it
                      once per HELD POD per tick makes `pod_daily_usd_budget` the
                      ceiling for the whole pool, not for one pod.

Where the ceiling lives: `pod_daily_usd_budget` is a PLATFORM value, not a per-tenant
one — the gateway enforces it for every org at once — yet the gateway runs with no
BRAIN_SETTINGS_PATH, so `brain.settings` hands it the repo-bundled default and nothing
an admin can reach ever changed it. The runtime store fixes that: a small JSON file
beside the ledger (tenants/.pod_budget_config.json) written by the superadmin route
(PUT /__fleet/pod_budget, surfaced on the Fleet page) and read here with the bundled
setting as the fallback. Precedence is runtime file > bundled settings. The file is
re-read on an mtime check, so a change is live on the reconciler's next tick without
a redeploy, and every process on the host (gateway and brains) sees the same number.

Scope: the gateway is the single owner of the pool, so a plain file + in-memory
counter is sufficient — no atomic-increment RPC of the kind partner_cloud_usage needs
for concurrent tenant processes. It is persisted so a gateway redeploy mid-day does not
hand the pool a fresh budget (which is precisely how a "capped" resource ends up
uncapped in practice).
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import os
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Lives beside the other shared pod files on the tenant volume so it survives a
# redeploy. Same directory contract as provisioner.HOST_SYNC_FILE.
_LEDGER = Path(os.environ.get("BRAIN_TENANTS_DIR", "tenants")).resolve() / ".pod_budget.json"

# The platform budget's runtime store and its audit trail, derived from the ledger's
# directory at call time so relocating `_LEDGER` (tests, BRAIN_TENANTS_DIR) moves all
# three files together. See `runtime_budget_path()`.
_RUNTIME_BUDGET_NAME = ".pod_budget_config.json"
_RUNTIME_AUDIT_NAME = ".pod_budget_audit.jsonl"
# Sanity ceiling on what the route will accept: a daily GPU cap above this is a typo,
# not a decision (the pool's price ceiling × max pods × 24h is well under it).
MAX_BUDGET_USD = 10_000.0
# (path, mtime_ns, size) → parsed runtime value, so a tick costs one stat(), not a read.
_runtime_cache: tuple[tuple[str, int, int], float | None] | None = None

# Fallback $/hr when the live pod's rate isn't known yet. Deliberately the PESSIMISTIC
# end — RunPodManager._PRICE_CEILING, the most it will ever pay for a card — because
# this converts a dollar ceiling into an uptime allowance. Guessing low (say $0.44 when
# the pod actually costs $0.50) would silently overshoot the dollar target by 14%; a cost
# ceiling must never round in favour of spending more.
_FALLBACK_RATE_PER_HR = 0.50
_rate_per_hr: float | None = None

# Cooldown after a pod ran a full grace period without producing anything: doubling
# from 15 minutes, capped at 4 hours. Bounds the wake→nothing→sleep→wake churn without
# blocking recovery for a whole day if the cause was transient.
_COOLDOWN_BASE_S = 900.0
_COOLDOWN_MAX_S = 4 * 3600.0


def _today() -> str:
    """UTC calendar day, matching how partner_cloud_usage rolls its daily counter."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")


class PodLedger:
    """One daily GPU ledger backed by one JSON file.

    `budget_usd_fn` supplies today's dollar ceiling (0 = uncapped) and `rate_fn` the
    $/hr the ceiling converts at; both are looked up at call time so a settings change
    (or a per-org budget row) is live without a restart. The defaults are the platform's
    (module-level) functions, which is what makes the module API below a thin delegate."""

    def __init__(
        self,
        path: Path,
        *,
        budget_usd_fn: Callable[[], float] | None = None,
        rate_fn: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(path)
        self._budget_usd_fn = budget_usd_fn
        self._rate_fn = rate_fn
        self._rate: float | None = None

    # ── persistence ──

    def _read_state(self) -> dict:
        """Whole persisted state. A corrupt or missing file reads as empty — the
        reconciler must keep running, and the worst case is one day billed from zero."""
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.debug("[pod_budget] ledger read failed (%s): %s", self.path.name, e)
            return {}

    def _write_state(self, st: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(st), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as e:
            logger.debug("[pod_budget] ledger write failed (%s): %s", self.path.name, e)

    # ── uptime ──

    def spent_seconds(self) -> float:
        """Uptime already billed today. A stale date reads as 0 — the day rolled."""
        st = self._read_state()
        return float(st.get("seconds") or 0.0) if str(st.get("date") or "") == _today() else 0.0

    def record_uptime(self, seconds: float) -> float:
        """Add `seconds` of pod uptime to today's total and return the new total.

        Called by the reconciler once per tick PER HELD POD, with the elapsed wall-clock
        since the previous tick — so the ledger measures what RunPod actually bills
        (uptime, summed over pods), not what we managed to use it for. The cooldown state
        rides along in the same file and is deliberately NOT reset by the day rollover:
        an unproductive pod at 23:59 is still unproductive at 00:01."""
        if seconds <= 0:
            return self.spent_seconds()
        st = self._read_state()
        prior = float(st.get("seconds") or 0.0) if str(st.get("date") or "") == _today() else 0.0
        total = prior + float(seconds)
        st["date"] = _today()
        st["seconds"] = round(total, 1)
        self._write_state(st)
        return total

    # ── rate and budget ──

    def set_rate_per_hr(self, rate: float | None) -> None:
        """What the held pod(s) actually cost, so the dollar budget converts to seconds
        at the real rate rather than a guess. None or 0 leaves the previous value alone.
        With several pods at different rates the reconciler passes the HIGHEST — a cost
        ceiling must never round in favour of spending more."""
        if rate and rate > 0:
            self._rate = float(rate)

    def rate_per_hr(self) -> float:
        """$/hr: the live rate when known, else the env override, else the ceiling."""
        if self._rate_fn is not None:
            return self._rate_fn()
        if self._rate:
            return self._rate
        return _env_rate_or_fallback()

    def budget_usd(self) -> float:
        """Today's ceiling in dollars. 0 = uncapped."""
        if self._budget_usd_fn is not None:
            return max(0.0, float(self._budget_usd_fn() or 0.0))
        return _settings_budget_usd()

    def budget_seconds(self) -> float:
        """Today's ceiling as an uptime allowance in seconds. 0 = uncapped.

        Denominated in DOLLARS and converted here, not stored as minutes. The ceiling
        exists to bound money, and a minutes dial does not: at the manager's price ceiling
        the same 1363 minutes is $10.00 on a $0.44/hr card and $11.36 on a $0.50/hr one."""
        usd = self.budget_usd()
        return (usd / self.rate_per_hr()) * 3600.0 if usd > 0 else 0.0

    def exhausted(self) -> bool:
        """True when today's GPU budget is spent and no pod may be woken (or added).
        Uncapped (0) is always False — an explicit opt-out, logged loudly by the
        reconciler so an uncapped GPU is never silently uncapped."""
        cap = self.budget_seconds()
        return cap > 0 and self.spent_seconds() >= cap

    # ── churn guard ──

    def record_sleep(self, produced: bool) -> None:
        """Called when a pod is put to sleep. `produced` says whether it did any real
        work during the session that just ended; an unproductive one arms an escalating
        cooldown so the next wake request cannot immediately restart the cycle."""
        st = self._read_state()
        if produced:
            st["unproductive_streak"] = 0
            st["cooldown_until"] = 0.0
        else:
            streak = int(st.get("unproductive_streak") or 0) + 1
            st["unproductive_streak"] = streak
            st["cooldown_until"] = time.time() + min(
                _COOLDOWN_MAX_S, _COOLDOWN_BASE_S * (2 ** (streak - 1))
            )
        self._write_state(st)

    def cooldown_remaining_s(self) -> float:
        """Seconds until an unproductive-pod cooldown lifts. 0 = no cooldown."""
        return max(0.0, float(self._read_state().get("cooldown_until") or 0.0) - time.time())

    def status(self) -> dict:
        """Snapshot for /health and the ops dashboard — GPU spend beside cloud spend.
        Dollars are derived from the hourly rate so the number is directly comparable
        to the cloud_usd figures next to it."""
        spent = self.spent_seconds()
        cap = self.budget_seconds()
        rate = self.rate_per_hr()
        return {
            "minutes_used": round(spent / 60.0, 1),
            "minutes_budget": round(cap / 60.0, 1) if cap else 0,
            "usd_today": round(spent / 3600.0 * rate, 3),
            "usd_budget": round(self.budget_usd(), 2),
            "rate_per_hr": rate,
            "exhausted": self.exhausted(),
            "uncapped": cap == 0,
        }


# ── the platform ledger (module-level API the reconciler and /health call) ──────


def _env_rate_or_fallback() -> float:
    try:
        env = float(os.environ.get("RUNPOD_COST_PER_HR", "") or 0.0)
    except ValueError:
        env = 0.0
    return env if env > 0 else _FALLBACK_RATE_PER_HR


def _bundled_budget_usd() -> float:
    """The `pod_daily_usd_budget` this process's settings resolve to. On the gateway
    that is the repo-bundled brain/settings.json (it runs with no BRAIN_SETTINGS_PATH);
    on a tenant brain it is the tenant's own file. Either way it is the FALLBACK, never
    the platform's editable value — see `_settings_budget_usd`."""
    try:
        from brain.settings import settings

        return max(0.0, float(settings.get("pod_daily_usd_budget") or 0.0))
    except Exception:
        return 0.0


def runtime_budget_path() -> Path:
    """The platform budget's runtime store: `<BRAIN_TENANTS_DIR>/.pod_budget_config.json`,
    beside the ledger on the volume so it survives a redeploy and is one file for every
    process on the host."""
    return _LEDGER.with_name(_RUNTIME_BUDGET_NAME)


def runtime_audit_path() -> Path:
    return _LEDGER.with_name(_RUNTIME_AUDIT_NAME)


def validate_budget_usd(value) -> tuple[float | None, str | None]:
    """(usd, None) for an acceptable daily ceiling, (None, reason) otherwise. Accepts a
    number or a numeric string; refuses bools, NaN/inf, negatives and typo-sized values.
    0 is valid and means uncapped — the caller must surface that as a warning."""
    if isinstance(value, bool) or value is None:
        return None, "usd must be a number ≥ 0 (0 = uncapped)"
    try:
        usd = float(value)
    except (TypeError, ValueError):
        return None, "usd must be a number ≥ 0 (0 = uncapped)"
    if not math.isfinite(usd):
        return None, "usd must be a finite number"
    if usd < 0:
        return None, "usd must be ≥ 0 (0 = uncapped)"
    if usd > MAX_BUDGET_USD:
        return None, f"usd must be ≤ {MAX_BUDGET_USD:.0f}"
    return round(usd, 2), None


def _read_runtime_record() -> dict | None:
    """The whole runtime file as a dict, or None when absent/unreadable. Uncached —
    the cached path is `runtime_budget_usd()`; this is for the admin view."""
    try:
        d = json.loads(runtime_budget_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug("[pod_budget] runtime budget read failed: %s", e)
        return None


def runtime_budget_usd() -> float | None:
    """The platform's runtime-set daily ceiling, or None when nobody has set one (or
    the file is unreadable, which must degrade to the bundled default — never to 0,
    because 0 means UNCAPPED and a corrupt file must not silently remove the ceiling).

    Cheap per tick: one stat(); the file is parsed again only when its mtime or size
    changes, so the reconciler picks up an edit on its next tick without a restart."""
    global _runtime_cache
    path = runtime_budget_path()
    try:
        st = path.stat()
    except FileNotFoundError:
        _runtime_cache = None
        return None
    except Exception as e:
        logger.debug("[pod_budget] runtime budget stat failed: %s", e)
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _runtime_cache is not None and _runtime_cache[0] == key:
        return _runtime_cache[1]
    value: float | None = None
    rec = _read_runtime_record()
    if rec is not None and "usd" in rec:
        usd, problem = validate_budget_usd(rec.get("usd"))
        if problem is None:
            value = usd
        else:
            logger.warning(
                "[pod_budget] ignoring runtime budget %r in %s (%s) — using the bundled "
                "setting instead",
                rec.get("usd"),
                path.name,
                problem,
            )
    _runtime_cache = (key, value)
    return value


def budget_source() -> str:
    """Which layer today's ceiling comes from: "runtime" (the superadmin-set file) or
    "settings" (the bundled/tenant settings default)."""
    return "runtime" if runtime_budget_usd() is not None else "settings"


def _settings_budget_usd() -> float:
    """Today's platform ceiling, resolved at call time (not import) with the precedence
    runtime file > bundled settings:

      1. `<BRAIN_TENANTS_DIR>/.pod_budget_config.json` — written by the superadmin
         route (PUT /__fleet/pod_budget, the Fleet page's inline edit). Live on the
         reconciler's next tick; survives a redeploy.
      2. `pod_daily_usd_budget` from `brain.settings` — on the gateway that is the
         repo-bundled brain/settings.json, i.e. a deploy-time constant.

    It is NOT editable from a tenant's settings UI: the gateway enforces one ceiling
    for the whole pool and never reads a tenant's settings file."""
    rt = runtime_budget_usd()
    return rt if rt is not None else _bundled_budget_usd()


def _audit_runtime_change(rec: dict) -> None:
    """Append one line to the audit trail beside the runtime file and mirror it to the
    process log. Never raises — an audit failure must not block the budget change,
    but it is logged at warning so it is not silent either."""
    logger.warning(
        "[pod_budget] platform GPU budget %s → %s by %s",
        rec.get("previous_usd"),
        rec.get("usd"),
        (rec.get("updated_by") or {}).get("email") or (rec.get("updated_by") or {}).get("user"),
    )
    try:
        path = runtime_audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:
        logger.warning("[pod_budget] audit append failed: %s", e)


def set_runtime_budget_usd(usd: float, actor: dict | None = None) -> dict:
    """Write the platform's daily ceiling to the runtime store (atomic replace) and
    audit it. `usd` must already have passed `validate_budget_usd`. Returns the record
    written. Raises on a write failure — the route must report that, not pretend."""
    checked, problem = validate_budget_usd(usd)
    if problem is not None:
        raise ValueError(problem)
    previous = runtime_budget_usd()
    prev_source = "runtime" if previous is not None else "settings"
    now = time.time()
    rec = {
        "usd": checked,
        "updated_at": datetime.datetime.fromtimestamp(now, datetime.UTC).isoformat(),
        "updated_by": {
            "user": (actor or {}).get("user"),
            "email": (actor or {}).get("email"),
            "source": str((actor or {}).get("source") or "gateway"),
        },
    }
    path = runtime_budget_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    os.replace(tmp, path)
    _audit_runtime_change(
        {
            "ts": now,
            "event": "pod_budget_set",
            "usd": checked,
            "previous_usd": previous if previous is not None else _bundled_budget_usd(),
            "previous_source": prev_source,
            "updated_by": rec["updated_by"],
        }
    )
    return rec


def platform_budget_view() -> dict:
    """What the superadmin route returns: the effective ceiling and where it comes from,
    the bundled fallback, the runtime record (if any) and today's ledger snapshot."""
    rec = _read_runtime_record()
    rt = runtime_budget_usd()
    return {
        "usd_budget": round(budget_usd(), 2),
        "source": "runtime" if rt is not None else "settings",
        "bundled_default": round(_bundled_budget_usd(), 2),
        "runtime": (
            {
                "usd": rt,
                "updated_at": rec.get("updated_at"),
                "updated_by": rec.get("updated_by"),
            }
            if rt is not None and rec is not None
            else None
        ),
        "path": str(runtime_budget_path()),
        "status": status(),
    }


def _platform() -> PodLedger:
    """The platform pool's ledger at `_LEDGER`, wired to the module-level rate and
    budget functions so that patching those (settings UI, tests) still governs it.
    Constructed per call — it is a path plus two callables, and reading `_LEDGER` late
    keeps the file relocatable."""
    return PodLedger(_LEDGER, budget_usd_fn=budget_usd, rate_fn=rate_per_hr)


def spent_seconds() -> float:
    return _platform().spent_seconds()


def record_uptime(seconds: float) -> float:
    return _platform().record_uptime(seconds)


def set_rate_per_hr(rate: float | None) -> None:
    """Tell the platform ledger what the live pod(s) cost — the reconciler passes the
    highest rate among held pods each tick. None or 0 leaves the previous value alone."""
    global _rate_per_hr
    if rate and rate > 0:
        _rate_per_hr = float(rate)


def rate_per_hr() -> float:
    if _rate_per_hr:
        return _rate_per_hr
    return _env_rate_or_fallback()


def budget_usd() -> float:
    return _settings_budget_usd()


def budget_seconds() -> float:
    usd = budget_usd()
    return (usd / rate_per_hr()) * 3600.0 if usd > 0 else 0.0


def exhausted() -> bool:
    cap = budget_seconds()
    return cap > 0 and spent_seconds() >= cap


def record_sleep(produced: bool) -> None:
    _platform().record_sleep(produced)


def cooldown_remaining_s() -> float:
    return _platform().cooldown_remaining_s()


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
    """Should the shared GPU pod (pool pod 0) be held up right now? The whole wake/sleep
    decision, as a pure function so it can be tested without a gateway, a RunPod key, or
    a clock. Pods above 0 are pod_pool.decide_scale's business; this is 0↔1.

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
    """Snapshot for /health and the ops dashboard — GPU spend beside cloud spend. GPU
    cost was invisible on every surface that showed cloud cost, which is the whole
    reason six days of idle burn went unnoticed."""
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
        # "runtime" = the superadmin-set file; "settings" = the bundled default.
        "source": budget_source(),
    }
