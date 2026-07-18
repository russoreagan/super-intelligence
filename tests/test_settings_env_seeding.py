"""Registering a setting must not silently kill its pre-existing env var.

The failure mode (found 2026-07-18): a module reads `settings.get(k) or ENV_CONST`.
That works while `k` is unregistered, because get() returns None and the env
constant takes over. Register `k` in DEFAULTS and get() always returns a truthy
default, so the `or` never fires again and BRAIN_* becomes dead config that still
looks live in the deploy. Settings seeds registered defaults from the env at load
to keep the lever working.

Layering under test: built-in default < environment < explicit settings.json.
"""

import json

import pytest

from brain import settings as settings_mod


def _fresh(monkeypatch, tmp_path, env: dict, on_disk: dict | None = None):
    """A Settings built with a given env and settings.json, as at process boot."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    path = tmp_path / "settings.json"
    if on_disk is not None:
        path.write_text(json.dumps(on_disk), encoding="utf-8")
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", path)
    return settings_mod.Settings()


def test_env_var_overrides_the_registered_default(monkeypatch, tmp_path):
    s = _fresh(monkeypatch, tmp_path, {"BRAIN_DMN_MIN_TICK_INTERVAL": "12.5"})
    assert s.get("dmn_min_tick_interval") == pytest.approx(12.5)


def test_unset_env_leaves_the_registered_default_untouched(monkeypatch, tmp_path):
    monkeypatch.delenv("BRAIN_DMN_MIN_TICK_INTERVAL", raising=False)
    s = _fresh(monkeypatch, tmp_path, {})
    assert s.get("dmn_min_tick_interval") == settings_mod.DEFAULTS["dmn_min_tick_interval"]


def test_settings_file_still_wins_over_env(monkeypatch, tmp_path):
    """The per-tenant tuning surface stays the last word — the env is a floor
    below it, not a lock above it."""
    s = _fresh(
        monkeypatch,
        tmp_path,
        {"BRAIN_DMN_MIN_TICK_INTERVAL": "12.5"},
        on_disk={"dmn_min_tick_interval": 3.0},
    )
    assert s.get("dmn_min_tick_interval") == pytest.approx(3.0)


def test_malformed_env_value_falls_back_rather_than_breaking_boot(monkeypatch, tmp_path):
    s = _fresh(monkeypatch, tmp_path, {"BRAIN_DMN_MIN_TICK_INTERVAL": "not-a-number"})
    assert s.get("dmn_min_tick_interval") == settings_mod.DEFAULTS["dmn_min_tick_interval"]


def test_env_value_is_coerced_to_the_registered_type(monkeypatch, tmp_path):
    """A str from the environment must land as the DEFAULTS type, or every
    downstream float()/int() call site inherits the string."""
    s = _fresh(monkeypatch, tmp_path, {"BRAIN_DMN_INTERVAL": "20"})
    assert isinstance(s.get("dmn_interval"), float)
    assert s.get("dmn_interval") == pytest.approx(20.0)


def test_every_env_seeded_key_is_actually_registered():
    """A typo'd key here would seed nothing and fail silently — the same class of
    bug the table exists to fix."""
    for key in settings_mod.ENV_SEEDED:
        assert key in settings_mod.DEFAULTS, f"{key} is env-seeded but not registered"


def test_the_dmn_reads_the_env_seeded_floor(monkeypatch, tmp_path):
    """End-to-end on the call site that was broken: the floor the DMN clamps its
    tick interval to must reflect BRAIN_DMN_MIN_TICK_INTERVAL."""
    from brain.dmn import DefaultModeNetwork, IdlePhase

    s = _fresh(monkeypatch, tmp_path, {"BRAIN_DMN_MIN_TICK_INTERVAL": "30"})
    monkeypatch.setattr("brain.dmn.settings", s)

    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._backoff_mult = 1.0
    monkeypatch.setattr(DefaultModeNetwork, "_roster", lambda self: ["a", "b", "c", "d"])
    monkeypatch.setattr(DefaultModeNetwork, "_idle_phase", lambda self: IdlePhase.ENGAGED)

    # base 8s over a 4-persona roster = 2s, which the 30s env floor must override.
    assert dmn._current_interval() == pytest.approx(30.0)
