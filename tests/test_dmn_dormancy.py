"""DMN dormancy: an org nobody has talked to for days stops thinking.

Idle thinking is meant to run while people are away, so the limit is days, not
minutes. The clock is the org's last HUMAN turn on ANY agent (owner UI or engine
API), persisted so a respawn does not read as fresh engagement — one abandoned
org had been minting self-tasks and holding the shared GPU pod up for a month,
and every respawn restarted the run.
"""

from __future__ import annotations

import time

from brain import human_activity
from brain.settings import settings


def test_org_root_is_one_level_above_a_persona_namespace(monkeypatch, tmp_path):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "second_brain" / "personas" / "luna"))
    assert human_activity.org_state_root() == tmp_path / "second_brain"
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "second_brain"))
    assert human_activity.org_state_root() == tmp_path / "second_brain"


def test_stamp_round_trip_and_throttle(monkeypatch, tmp_path):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "sb" / "personas" / "p"))
    monkeypatch.setattr(human_activity, "_last_write_ts", 0.0)
    assert human_activity.last_turn_ts() is None
    assert human_activity.stamp(1000.0) is True
    assert human_activity.last_turn_ts() == 1000.0
    assert human_activity.stamp(1030.0) is False  # throttled
    assert human_activity.last_turn_ts() == 1000.0
    assert human_activity.stamp(1030.0, force=True) is True
    assert human_activity.last_turn_ts() == 1030.0
    # The file sits at the ORG root, shared by every persona of the org.
    assert (tmp_path / "sb" / human_activity.FILENAME).exists()


def test_seed_clock_prefers_the_persisted_stamp(monkeypatch, tmp_path):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "sb"))
    monkeypatch.setattr(human_activity, "_last_write_ts", 0.0)
    now = time.time()
    assert human_activity.seed_clock(now) == now  # no stamp → default
    human_activity.stamp(now - 500_000.0, force=True)
    assert abs(human_activity.seed_clock(now) - (now - 500_000.0)) < 0.01
    human_activity.stamp(now + 10_000.0, force=True)  # skew → clamped
    assert human_activity.seed_clock(now) <= time.time()


class _DMNStub:
    """Just enough of DefaultModeNetwork for the dormancy property."""

    from brain.dmn import DefaultModeNetwork as _DMN

    dormant = _DMN.dormant
    _log_dormancy_edge = _DMN._log_dormancy_edge
    _effective_idle_seconds = _DMN._effective_idle_seconds

    def __init__(self, last_ts: float):
        self._last_user_activity_ts = last_ts
        self._dormant_logged_at = 0.0
        self._was_dormant = False


def test_dormant_after_the_limit_and_never_when_disabled(monkeypatch):
    monkeypatch.setitem(settings._data, "dmn_pause_after_idle_s", 259200.0)
    fresh = _DMNStub(time.time() - 3600.0)
    assert fresh.dormant is False
    old = _DMNStub(time.time() - 4 * 86400.0)
    assert old.dormant is True
    monkeypatch.setitem(settings._data, "dmn_pause_after_idle_s", 0)
    assert old.dormant is False


def test_dormancy_edge_logs_once_per_hour(monkeypatch, caplog):
    monkeypatch.setitem(settings._data, "dmn_pause_after_idle_s", 10.0)
    d = _DMNStub(time.time() - 100.0)
    with caplog.at_level("INFO", logger="brain.dmn"):
        d._log_dormancy_edge(True)
        d._log_dormancy_edge(True)
        d._log_dormancy_edge(True)
    assert sum("Dormant" in r.getMessage() for r in caplog.records) == 1
    with caplog.at_level("INFO", logger="brain.dmn"):
        d._log_dormancy_edge(False)
    assert any("Awake again" in r.getMessage() for r in caplog.records)
