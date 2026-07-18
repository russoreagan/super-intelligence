"""ignition_tally — content-free per-persona Global-Workspace ignition pressure.

The conftest autouse fixture isolates SECOND_BRAIN_PATH to tmp_path, so every
test writes its tally under a throwaway root. The flag ships ON (kill switch);
tests that need it off set it explicitly.
"""

from __future__ import annotations

import json

import pytest

from brain import ignition_tally
from brain.settings import settings


@pytest.fixture(autouse=True)
def _isolated_pending():
    """record() accumulates in a module-global pending list; keep tests hermetic."""
    ignition_tally._pending.clear()
    yield
    ignition_tally._pending.clear()


def _tally_path():
    from brain.persona_key import persona_state_root

    return persona_state_root("") / "ignition_tally.json"


def test_flag_off_is_dark(monkeypatch):
    monkeypatch.setitem(settings._data, "node_recruit_from_ignition", 0)
    ignition_tally.record("threat")
    assert not _tally_path().exists()
    assert ignition_tally.pressure() == (0.0, "")


def test_record_accumulates_and_reports_dominant():
    for _ in range(4):
        ignition_tally.record("threat")
    ignition_tally.record("salience")
    total, dominant = ignition_tally.pressure()
    # Decay between record and read is microseconds — total is fractionally under 5.
    assert 4.9 < total <= 5.0
    assert dominant == "threat"


def test_decay_half_life(monkeypatch):
    t0 = 1_000_000.0
    monkeypatch.setattr(ignition_tally, "_now", lambda: t0)
    for _ in range(4):
        ignition_tally.record("memory")
    hl_s = float(settings.get("ignition_tally_half_life_h", 72.0)) * 3600.0
    monkeypatch.setattr(ignition_tally, "_now", lambda: t0 + hl_s)
    total, _ = ignition_tally.pressure()
    assert abs(total - 2.0) < 1e-6
    monkeypatch.setattr(ignition_tally, "_now", lambda: t0 + 3 * hl_s)
    total3, _ = ignition_tally.pressure()
    assert total3 < 1.0


def test_consume_resets():
    for _ in range(4):
        ignition_tally.record("threat")
    assert ignition_tally.pressure()[0] > 3.0
    ignition_tally.consume()
    assert ignition_tally.pressure() == (0.0, "")


def test_record_is_memory_only_until_flush():
    """The turn path does zero disk I/O: record() only accumulates in memory, and
    flush() (called by pressure() in the sleep pass) makes it durable."""
    ignition_tally.record("threat")
    ignition_tally.record("threat")
    assert not _tally_path().exists()  # nothing touched disk yet
    ignition_tally.flush()
    data = json.loads(_tally_path().read_text(encoding="utf-8"))
    assert 1.9 < data["threat"]["score"] <= 2.0
    total, dominant = ignition_tally.pressure()
    assert 1.9 < total <= 2.0 and dominant == "threat"


def test_file_is_content_free():
    ignition_tally.record("threat")
    ignition_tally.record("vision")
    ignition_tally.record("the user mentioned their divorce")  # would-be content
    ignition_tally.flush()
    data = json.loads(_tally_path().read_text(encoding="utf-8"))
    assert set(data) <= {"threat", "salience", "memory", "vision", "other"}
    for entry in data.values():
        assert set(entry) == {"score", "last_ts"}
        assert isinstance(entry["score"], float)
        assert isinstance(entry["last_ts"], float)


def test_unknown_coalition_clamped_to_other():
    ignition_tally.record("user said something")
    ignition_tally.flush()
    data = json.loads(_tally_path().read_text(encoding="utf-8"))
    assert "other" in data
    assert "user said something" not in data
