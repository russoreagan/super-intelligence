"""
AutonomousBudget — a soft/hard spend pool for autonomous work, distinct from
interactive spend.

Autonomous cloud spend is metered into its own counter (`usd_autonomous` in
`second_brain/cloud_usage.json`, written by the router when `_bg_mode`), so a busy
interactive day never trips the autonomous caps and vice-versa. Two thresholds:

  soft (default $30) — pause new autonomous jobs; the owner approves 'continue' once
  hard (default $50) — stop autonomous cloud work for the rest of the UTC day

All per-day persistence (spend + the soft-pause 'cleared' flag) lives in the router's
usage file and resets at the UTC boundary; this class is a thin policy layer over it.
Every router call is defensive so a minimally-constructed router (tests) can't break it.
"""

from __future__ import annotations

import contextlib
from enum import Enum

from brain.settings import settings as _settings


class BudgetTier(str, Enum):
    UNDER_SOFT = "under_soft"
    SOFT_EXCEEDED = "soft_exceeded"
    HARD_EXCEEDED = "hard_exceeded"


class AutonomousBudget:
    def __init__(self, router) -> None:
        self._router = router

    def soft_cap(self) -> float:
        return float(_settings.get("autonomous_soft_usd") or 0.0)

    def hard_cap(self) -> float:
        return float(_settings.get("autonomous_hard_usd") or 0.0)

    def spent_today(self) -> float:
        fn = getattr(self._router, "autonomous_usd_today", None)
        if fn is None:
            return 0.0
        try:
            return float(fn())
        except Exception:
            return 0.0

    def tier(self) -> BudgetTier:
        spent = self.spent_today()
        hard = self.hard_cap()
        soft = self.soft_cap()
        if hard > 0 and spent >= hard:
            return BudgetTier.HARD_EXCEEDED
        if soft > 0 and spent >= soft:
            return BudgetTier.SOFT_EXCEEDED
        return BudgetTier.UNDER_SOFT

    def soft_pause_active(self) -> bool:
        """True when the soft cap is breached AND the owner has not (yet, today)
        approved 'continue spending'. Once cleared, work runs up to the hard cap."""
        if self.tier() is not BudgetTier.SOFT_EXCEEDED:
            return False
        cleared = getattr(self._router, "autonomous_soft_cleared", None)
        if cleared is None:
            return True
        try:
            return not bool(cleared())
        except Exception:
            return True

    def clear_soft_pause(self) -> None:
        """Called when the owner approves the continue-spending sentinel: lift the
        soft pause for the rest of the UTC day (still bounded by the hard cap)."""
        fn = getattr(self._router, "clear_autonomous_soft_pause", None)
        if fn is None:
            return
        with contextlib.suppress(Exception):
            fn()
