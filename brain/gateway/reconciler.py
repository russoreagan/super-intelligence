"""Event-driven reconciler — level-triggered ticks, edge-triggered wake-ups.

The gateway's pool and placement ticks are idempotent desired-state functions:
they do not care WHY they run, only that they converge on the rows, the files
and the pods. That property is what lets the 60-second timer go. This loop
runs a tick when something that can change the answer happens, and otherwise
sleeps until the earliest moment the state machine itself needs one:

  wake(reason)   — an EDGE. Sources: a tenant nudge over the gateway's loopback
                   route (a placement row written, a consumer asking for its
                   pod, output arriving, pressure changing), a child process
                   exiting, the sleep sweep, a budget change.
  deadline_fn    — a TIMER. Each tick reports the earliest absolute time it
                   needs to run again: a paid_until expiry, the end of a pod's
                   grace period, a scale dwell, a churn cooldown, the UTC
                   rollover that refills a budget, an org's projected budget
                   exhaustion. The loop waits exactly that long, no longer.
  resync_s       — the SAFETY NET. A lost nudge or a missed timer self-heals at
                   the next resync (default 15 min), the way a Kubernetes
                   controller resyncs its informers. It is the only periodic
                   work left, and it is the difference between "correct when
                   every event arrives" and "correct".

Wakes within `debounce_s` of each other coalesce into one tick; the reasons are
carried into the tick so a placement wake can invalidate the registry cache
while a demand wake does not.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


def resync_interval_s() -> float:
    """BRAIN_RECONCILE_RESYNC_S — the safety-net period (default 900 s)."""
    try:
        return max(30.0, float(os.environ.get("BRAIN_RECONCILE_RESYNC_S", "900") or 900))
    except ValueError:
        return 900.0


def legacy_period_s() -> float:
    """BRAIN_POD_RECONCILE_S — the OLD fixed tick, kept as an opt-in: 0 (default)
    means events + deadlines + resync only; a positive value ALSO ticks on that
    period for a deployment that wants the previous behaviour."""
    try:
        return max(0.0, float(os.environ.get("BRAIN_POD_RECONCILE_S", "0") or 0))
    except ValueError:
        return 0.0


class Reconciler:
    def __init__(
        self,
        tick: Callable[[list[str]], Awaitable[None]],
        *,
        deadline_fn: Callable[[float], float | None] | None = None,
        resync_s: float | None = None,
        period_s: float | None = None,
        debounce_s: float = 1.0,
        name: str = "reconciler",
    ) -> None:
        self._tick = tick
        self._deadline_fn = deadline_fn or (lambda _now: None)
        self.resync_s = float(resync_s if resync_s is not None else resync_interval_s())
        self.period_s = float(period_s if period_s is not None else legacy_period_s())
        self.debounce_s = float(debounce_s)
        self.name = name
        self._event = asyncio.Event()
        self._pending: list[str] = []
        self.ticks = 0
        self.last_tick_at: float | None = None
        self.last_reasons: list[str] = []
        self.last_error: str | None = None
        self.next_deadline: float | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── edges ──

    def wake(self, reason: str) -> None:
        """Ask for a tick. Safe to call many times; wakes coalesce."""
        self._pending.append(str(reason)[:64])
        self._event.set()

    def wake_threadsafe(self, reason: str) -> None:
        """wake() from a thread (a child-exit waiter, a sync callback)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self.wake, reason)

    # ── the loop ──

    def _wait_s(self, now: float) -> float:
        """How long to sleep before a tick is due with no event: the earliest of
        the state machine's own deadline, the legacy period and the resync."""
        waits = [self.resync_s]
        if self.period_s > 0:
            waits.append(self.period_s)
        try:
            dl = self._deadline_fn(now)
        except Exception as e:
            logger.debug("[%s] deadline_fn failed: %s", self.name, e)
            dl = None
        self.next_deadline = dl
        if dl is not None:
            waits.append(max(0.0, float(dl) - now))
        return max(0.0, min(waits))

    async def run_once(self, reasons: list[str]) -> None:
        self.ticks += 1
        self.last_tick_at = time.time()
        self.last_reasons = list(reasons)
        try:
            await self._tick(list(reasons))
            self.last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.last_error = str(e)[:200]
            logger.warning("[%s] tick error (%s): %s", self.name, ",".join(reasons) or "-", e)

    async def run(self, *, initial: bool = True) -> None:
        self._loop = asyncio.get_running_loop()
        if initial:
            await self.run_once(["startup"])
        while True:
            try:
                timeout = self._wait_s(time.time())
                try:
                    await asyncio.wait_for(self._event.wait(), timeout=timeout)
                    reasons = ["wake"]
                except TimeoutError:
                    reasons = ["resync" if timeout >= self.resync_s else "deadline"]
                if self._event.is_set():
                    # Coalesce the burst: a placement write plus the demand it
                    # causes a moment later should be ONE tick, not two.
                    await asyncio.sleep(self.debounce_s)
                    self._event.clear()
                    reasons = self._pending or ["wake"]
                    self._pending = []
                await self.run_once(reasons)
            except asyncio.CancelledError:
                return

    def status(self) -> dict:
        return {
            "name": self.name,
            "ticks": self.ticks,
            "last_tick_at": self.last_tick_at,
            "last_reasons": list(self.last_reasons),
            "last_error": self.last_error,
            "next_deadline": self.next_deadline,
            "resync_s": self.resync_s,
            "period_s": self.period_s,
            "pending": list(self._pending),
        }


def next_utc_midnight(now: float) -> float:
    """The next UTC day boundary — when daily budgets refill."""
    from datetime import UTC, datetime, timedelta

    d = datetime.fromtimestamp(now, tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return (d + timedelta(days=1)).timestamp()


def earliest(*deadlines: float | None) -> float | None:
    vals = [float(d) for d in deadlines if d is not None]
    return min(vals) if vals else None


__all__ = ["Reconciler", "earliest", "legacy_period_s", "next_utc_midnight", "resync_interval_s"]
