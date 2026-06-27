"""Regression test for the rumination-trigger bug (Stage 7 Part 0).

Before the fix, `_rumination_drive` only counted chemistry elevated above rest and subtracted
5HT, so at resting chemistry the drive was ~0 and never crossed the 0.45 threshold — rumination
NEVER fired during deep idle (the two preconditions idle≥60s AND drive≥0.45 were mutually
exclusive). The fix adds a TONIC idle drive (boredom + unfinished business, persona-scaled) and a
conversation-idle fallback for hosts where OS HID idle is unavailable.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import brain.open_threads as ot
from brain.dmn import DefaultModeNetwork
from brain.persona_chem import PERSONA_CHEMISTRY
from brain.settings import settings


def _make_dmn(persona: str, *, idle_for_s: float, advances: int):
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._consecutive_ruminations = 0
    dmn._last_user_activity_ts = time.time() - idle_for_s
    threads = []
    if advances > 0:
        threads, t = ot.open_thread([], "a lingering question")
        for _ in range(advances):
            threads, t = ot.advance_thread(threads, t.id, "deepened")
    dmn._open_threads = threads
    # Mirror what _tick does: snapshot the engagement idle + phase for this "tick" so the
    # idle-gated decision reads the same single source of truth it does in production.
    dmn._tick_idle_s = dmn._effective_idle_seconds()
    dmn._tick_idle_phase = dmn._idle_phase(dmn._tick_idle_s)
    settings._data["persona_name"] = persona
    return dmn


def teardown_function():
    settings._data.pop("persona_name", None)


def test_resting_chemistry_drive_is_near_zero():
    """The original bug: phasic drive alone is ~0 at rest, so it never crossed threshold."""
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    for chem in PERSONA_CHEMISTRY.values():
        drive, _ = dmn._rumination_drive(chem)
        assert drive < 0.1  # confirms the phasic-only drive cannot trigger at rest


def test_fires_after_sustained_idle_with_open_thread():
    """The fix: tonic idle drive carries a ruminative persona over threshold during deep idle."""
    dmn = _make_dmn("The Poet", idle_for_s=600, advances=3)
    # Force the probability gate to always pass so the test is deterministic.
    with patch("brain.dmn.random.random", return_value=0.0):
        mode, _flavor, drive = dmn._rumination_decision(dict(PERSONA_CHEMISTRY["The Poet"]))
    assert drive >= settings.get("dmn_rumination_drive_threshold")
    assert mode == "ruminate"


def test_does_not_fire_when_freshly_active():
    """Just engaged the agent (idle≈0) → no rumination, regardless of chemistry."""
    dmn = _make_dmn("The Poet", idle_for_s=0.0, advances=3)
    mode, _flavor, _drive = dmn._rumination_decision(dict(PERSONA_CHEMISTRY["The Poet"]))
    assert mode == "normal"


def test_effective_idle_is_engagement_based():
    """Idle is seconds since the user last engaged the AGENT (not device HID), so working in
    another app still accrues idle and lets rumination fire."""
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._last_user_activity_ts = time.time() - 500.0
    assert dmn._effective_idle_seconds() >= 480.0


def test_idle_phase_thresholds():
    """The single idle definition: named phases map to the expected second-thresholds, so
    every gate that keys off a phase crosses at the same moment."""
    from brain.dmn import IdlePhase

    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    assert dmn._idle_phase(0.0) == IdlePhase.ENGAGED
    assert dmn._idle_phase(45.0) == IdlePhase.COOLING  # >=30, <60
    assert dmn._idle_phase(75.0) == IdlePhase.WANDERING  # >=60, <90
    assert dmn._idle_phase(120.0) == IdlePhase.DEEP  # >=90


def test_persona_divergence_in_tonic_drive():
    """The Poet (ruminative) reaches a higher idle drive than the Sage (disengages)."""
    chem_poet = dict(PERSONA_CHEMISTRY["The Poet"])
    chem_sage = dict(PERSONA_CHEMISTRY["The Sage"])
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._open_threads = []
    poet = dmn._tonic_idle_drive(chem_poet, idle=600, idle_threshold=60)
    sage = dmn._tonic_idle_drive(chem_sage, idle=600, idle_threshold=60)
    assert poet > sage
