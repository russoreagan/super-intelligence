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


def test_effective_tiers_bulk_matches_per_persona_semantics():
    # Pure computation over rows already fetched — no Supabase. Full dominates,
    # disabled rows are ignored, a missing tier defaults to lite, blank personas drop.
    from brain.agents import effective_tiers

    rows = [
        {"persona": "a", "enabled": True, "tier": "lite"},
        {"persona": "a", "enabled": True, "tier": "full"},
        {"persona": "b", "enabled": True, "tier": None},
        {"persona": "b", "enabled": False, "tier": "full"},
        {"persona": "", "enabled": True, "tier": "full"},
        {"persona": "c", "enabled": False, "tier": "full"},
    ]
    assert effective_tiers(rows) == {"a": "full", "b": "lite"}


def test_effective_tiers_of_no_rows_is_empty():
    from brain.agents import effective_tiers

    assert effective_tiers([]) == {}
