"""DMN dormancy: an org nobody has talked to for days stops thinking.

Idle thinking is meant to run while people are away, so the limit is days, not
minutes. The clock is the org's last HUMAN turn on ANY agent (owner UI or engine
API), persisted so a respawn does not read as fresh engagement — one abandoned
org had been minting self-tasks and holding the shared GPU pod up for a month,
and every respawn restarted the run.
"""

from __future__ import annotations

import math
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


# ── Per-persona stamp: which personas of an ISOLATED org sit on the idle roster ──


def test_persona_stamp_round_trip_throttle_and_path(monkeypatch, tmp_path):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "sb" / "personas" / "home_p"))
    monkeypatch.setattr(human_activity, "_last_write_ts", 0.0)
    monkeypatch.setattr(human_activity, "_persona_last_write_ts", {})
    assert human_activity.persona_last_turn_ts("luna") is None
    assert human_activity.stamp_persona("Luna", 1000.0) is True  # display name → slug
    assert human_activity.persona_last_turn_ts("luna") == 1000.0
    assert human_activity.stamp_persona("luna", 1030.0) is False  # throttled per slug
    assert human_activity.persona_last_turn_ts("luna") == 1000.0
    assert human_activity.stamp_persona("luna", 1030.0, force=True) is True
    assert human_activity.persona_last_turn_ts("luna") == 1030.0
    # Throttle is keyed per slug: another persona's first stamp is not suppressed.
    assert human_activity.stamp_persona("ahab", 1031.0) is True
    assert human_activity.persona_last_turn_ts("ahab") == 1031.0
    # Empty persona is a no-op (callers resolve home themselves).
    assert human_activity.stamp_persona("", 2000.0) is False
    assert human_activity.persona_last_turn_ts("") is None
    # Files sit under the ORG root, one per persona; the org stamp is untouched.
    assert (tmp_path / "sb" / "personas" / "luna" / human_activity.FILENAME).exists()
    assert (tmp_path / "sb" / "personas" / "ahab" / human_activity.FILENAME).exists()
    assert human_activity.last_turn_ts() is None


def test_persona_active_window(monkeypatch, tmp_path):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "sb"))
    monkeypatch.setattr(human_activity, "_persona_last_write_ts", {})
    now = 1_000_000.0
    assert human_activity.persona_active("never", 7, now) is False  # unknown = inactive
    assert human_activity.persona_active("never", 0, now) is True  # 0 = everyone
    human_activity.stamp_persona("fresh", now - 3 * 86400.0, force=True)
    human_activity.stamp_persona("stale", now - 8 * 86400.0, force=True)
    assert human_activity.persona_active("fresh", 7, now) is True
    assert human_activity.persona_active("stale", 7, now) is False
    assert human_activity.persona_active("stale", 30, now) is True


def test_isolated_roster_policy_reads_settings(monkeypatch):
    assert human_activity.isolated_roster_mode() == "active"  # the default
    assert human_activity.active_roster_days() == 7.0
    monkeypatch.setitem(settings._data, "dmn_isolated_roster", "ALL ")
    assert human_activity.isolated_roster_mode() == "all"
    monkeypatch.setitem(settings._data, "dmn_isolated_roster", "bogus")
    assert human_activity.isolated_roster_mode() == "home"  # fail closed
    monkeypatch.setitem(settings._data, "dmn_active_roster_days", -3)
    assert human_activity.active_roster_days() == 0.0


# ── Boot seeding: org stamp → newest persona stamp → "no human turn" = dormant ──


def _reset_stamps(monkeypatch, tmp_path):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "sb" / "personas" / "home_p"))
    monkeypatch.setattr(human_activity, "_last_write_ts", 0.0)
    monkeypatch.setattr(human_activity, "_persona_last_write_ts", {})


def test_boot_seed_prefers_the_org_stamp(monkeypatch, tmp_path):
    _reset_stamps(monkeypatch, tmp_path)
    now = 2_000_000.0
    human_activity.stamp_persona("luna", now - 100.0, force=True)  # newer, but per-persona
    human_activity.stamp(now - 500_000.0, force=True)
    assert human_activity.boot_seed(now) == (now - 500_000.0, "org")
    human_activity.stamp(now + 10_000.0, force=True)  # skew → clamped
    assert human_activity.boot_seed(now) == (now, "org")


def test_boot_seed_falls_back_to_the_newest_persona_stamp(monkeypatch, tmp_path):
    _reset_stamps(monkeypatch, tmp_path)
    now = 2_000_000.0
    human_activity.stamp_persona("ahab", now - 9 * 86400.0, force=True)
    human_activity.stamp_persona("luna", now - 2 * 86400.0, force=True)
    assert human_activity.newest_persona_turn_ts() == (now - 2 * 86400.0, "luna")
    assert human_activity.boot_seed(now) == (now - 2 * 86400.0, "persona:luna")


def test_boot_seed_with_no_stamp_starts_the_clock_and_persists_it(monkeypatch, tmp_path):
    """First deploy after the stamps landed: every existing org had no stamp and
    booted dormant — an over-correction. No stamp now STARTS the three-day clock
    (stamp written at `now`), and the next boot reads that stamp back, so a
    redeploy never restarts it."""
    _reset_stamps(monkeypatch, tmp_path)
    assert human_activity.newest_persona_turn_ts() is None
    # seed_clock keeps its "no stamp → default" contract for its other callers.
    assert human_activity.seed_clock(123.0) == 123.0
    assert human_activity.boot_seed(2_000_000.0) == (2_000_000.0, "grace")
    assert human_activity.last_turn_ts() == 2_000_000.0, "persisted"
    assert human_activity.boot_seed(2_000_500.0) == (2_000_000.0, "org"), "a redeploy keeps it"


def test_boot_seed_reports_unknown_only_when_the_stamp_cannot_be_written(monkeypatch, tmp_path):
    _reset_stamps(monkeypatch, tmp_path)
    monkeypatch.setattr(human_activity, "stamp", lambda *a, **k: False)
    assert human_activity.boot_seed(2_000_000.0) == (None, "none")


class _UnknownStub(_DMNStub):
    """A DMN that booted with no stamp anywhere: no human turn recorded."""

    from brain.dmn import DefaultModeNetwork as _DMN

    pause = _DMN.pause

    def __init__(self):
        super().__init__(0.0)
        self._no_human_turn_recorded = True
        self._skip_next_tick = False
        self.__dict__["_home"] = "home_p"

    def _active_persona_name(self):
        return "home_p"


def test_no_human_turn_recorded_boots_dormant_and_a_turn_wakes_it(monkeypatch, tmp_path, caplog):
    from brain.dmn import IDLE_UNKNOWN_S

    _reset_stamps(monkeypatch, tmp_path)
    monkeypatch.setitem(settings._data, "dmn_pause_after_idle_s", 259200.0)
    d = _UnknownStub()
    # The 3-day limit is untouched; "unknown" is simply beyond it, and finite
    # (fleet_signals rounds/serialises the value; other readers int() it).
    assert d._effective_idle_seconds() == IDLE_UNKNOWN_S
    assert IDLE_UNKNOWN_S > 259200.0 and math.isfinite(IDLE_UNKNOWN_S)
    assert int(IDLE_UNKNOWN_S) == IDLE_UNKNOWN_S
    assert d.dormant is True
    with caplog.at_level("INFO", logger="brain.dmn"):
        d._log_dormancy_edge(True)
    assert any("no human turn recorded" in r.getMessage() for r in caplog.records)
    assert not any("87600" in r.getMessage() for r in caplog.records)
    # A real turn stamps the clock and clears the flag → engaged again, and the
    # stamps now exist on disk for the next boot.
    d.pause(stamp_activity=True)
    assert d._no_human_turn_recorded is False
    assert d._effective_idle_seconds() < 5.0
    assert d.dormant is False
    ts, src = human_activity.boot_seed()
    assert src == "org" and ts is not None and time.time() - ts < 5.0
    assert (tmp_path / "sb" / "personas" / "home_p" / human_activity.FILENAME).exists()


def test_an_unset_clock_without_the_flag_still_reads_engaged():
    # Pre-existing contract for stubs that never set the flag (0.0 → 0.0, not unknown).
    d = _DMNStub(0.0)
    assert d._effective_idle_seconds() == 0.0


def test_real_dmn_starts_the_clock_without_a_stamp_and_seeds_from_one(
    monkeypatch, tmp_path, caplog
):
    from unittest.mock import MagicMock

    from brain.bus import Bus
    from brain.dmn import DefaultModeNetwork

    _reset_stamps(monkeypatch, tmp_path)
    monkeypatch.setitem(settings._data, "dmn_pause_after_idle_s", 259200.0)
    with caplog.at_level("INFO", logger="brain.dmn"):
        dmn = DefaultModeNetwork(Bus(), router=MagicMock(), hippocampus=None, parietal=None)
    # No stamp anywhere: the clock STARTS now (engaged), and the stamp is persisted.
    assert dmn._no_human_turn_recorded is False
    assert dmn.dormant is False
    assert dmn._effective_idle_seconds() < 5.0
    assert human_activity.last_turn_ts() is not None
    assert any("dormancy clock" in r.getMessage() for r in caplog.records)
    caplog.clear()
    # The stamp cannot be written (read-only volume): unknown → dormant, not engaged.
    monkeypatch.setattr(human_activity, "stamp", lambda *a, **k: False)
    monkeypatch.setattr(human_activity, "last_turn_ts", lambda: None)
    monkeypatch.setattr(human_activity, "newest_persona_turn_ts", lambda: None)
    with caplog.at_level("INFO", logger="brain.dmn"):
        dmn0 = DefaultModeNetwork(Bus(), router=MagicMock(), hippocampus=None, parietal=None)
    assert dmn0._no_human_turn_recorded is True and dmn0.dormant is True
    assert any("No human turn recorded" in r.getMessage() for r in caplog.records)
    monkeypatch.undo()
    _reset_stamps(monkeypatch, tmp_path)
    monkeypatch.setitem(settings._data, "dmn_pause_after_idle_s", 259200.0)
    caplog.clear()
    human_activity.stamp(time.time() - 3600.0, force=True)
    with caplog.at_level("INFO", logger="brain.dmn"):
        dmn2 = DefaultModeNetwork(Bus(), router=MagicMock(), hippocampus=None, parietal=None)
    assert dmn2._no_human_turn_recorded is False
    assert dmn2.dormant is False
    assert abs(dmn2._effective_idle_seconds() - 3600.0) < 5.0
    assert any("seeded from the org stamp" in r.getMessage() for r in caplog.records)
