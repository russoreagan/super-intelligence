"""
Agent runtime tier (full | lite) — see 014_agent_tier.sql and brain/agents.py.

set_tier validates the tier value BEFORE touching the database, so the rejection
path is unit-testable without Supabase. The DB-backed read paths (set_tier success,
effective_tier "full dominates") are covered by integration against a real org.
"""

from __future__ import annotations

import pytest

from brain.agents import VALID_TIERS, set_tier
from brain.mandates import MandateError


def test_valid_tiers_are_lite_and_full():
    assert set(VALID_TIERS) == {"lite", "full"}


def test_set_tier_rejects_unknown_value_before_db():
    # An invalid tier raises immediately (validation precedes any _sb() call), so
    # this never reaches Supabase.
    with pytest.raises(MandateError):
        set_tier("the_visionary.trading_bull", "turbo")
