"""Regression guard: settings read at their call sites with an inline fallback must
also be registered in DEFAULTS, or the config loader silently drops any attempt to
tune them (see settings.py's unknown-key drop logic)."""

from __future__ import annotations

from brain.settings import DEFAULTS


def test_orphan_settings_registered_at_expected_defaults():
    expected = {
        "resting_mood_consolidation_alpha": 0.3,
        "dmn_min_tick_interval": 5.0,
        "cma_budget_check_interval_s": 30.0,
        "motor_write_approval_bytes": 5_000_000,
        "job_store_max_jobs": 100,
        "job_store_max_mb": 100,
    }
    for key, value in expected.items():
        assert key in DEFAULTS, f"{key} missing from DEFAULTS"
        assert DEFAULTS[key] == value
