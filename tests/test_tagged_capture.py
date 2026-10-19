"""Tagged consolidation: a strong grounded outcome tags what sleep keeps.

Synaptic tagging and capture + prioritized replay (brain/hebbian.py
_apply_tagged_capture). The audit found no organically learned skill ever
reached the 1.3 injection bar: uniform per-turn credit decayed faster than it
accrued. A turn whose GROUNDED outcome was strong (a tool or job result, an
external grade — never self-graded draft quality) now gets extra credit on its
path and winning attachments, capped per edge per UTC day, so only repetition
across distinct days carries a skill into use.
"""

from __future__ import annotations

import time

import pytest

from brain.settings import settings
from tests.test_fragment_wiring import _isolated_wiring, _sc, _win_trace

SUCCESS = {
    "delta": 0.2,
    "signal_type": "self_graded",
    "reward_source": "mastery",
    "reason": "tool_success",
}
FAILURE = {
    "delta": -0.2,
    "signal_type": "self_graded",
    "reward_source": "mastery",
    "reason": "tool_failure",
}
SELF = {
    "delta": 0.2,
    "signal_type": "self_graded",
    "reward_source": "correctness",
    "reason": "draft_quality_high",
}


@pytest.fixture
def day(monkeypatch):
    """Pin the capture ledger's UTC day; tests advance it explicitly."""
    import brain.hebbian as hb

    clock = {"t": 1_760_000_000.0}
    real = time.gmtime
    monkeypatch.setattr(hb.time, "gmtime", lambda *a: real(clock["t"]) if not a else real(*a))
    return clock


def _weight(w, sid="alpha", host="frontal.drafter_C"):
    return dict(w.attached_fragments(host)).get(sid, 1.0)


def _run(monkeypatch, tmp_path, emissions, *, tagged=1, passes=1):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setitem(settings._data, "tagged_consolidation", tagged)
    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = _sc(monkeypatch, tmp_path, w)
    for i in range(passes):
        t = _win_trace(f"t{i}", winner_idx=2, drafter_fragments={"frontal.drafter_C": ["alpha"]})
        t.reward_emissions = [dict(e) for e in emissions]
        sc._run_hebbian_pass("sess", [t])
    return w


def test_strong_grounded_success_tags_and_captures(monkeypatch, tmp_path, day):
    plain = _weight(_run(monkeypatch, tmp_path / "a", [SUCCESS], tagged=0))
    tagged = _weight(_run(monkeypatch, tmp_path / "b", [SUCCESS], tagged=1))
    assert tagged > plain + 0.05


def test_self_graded_reward_never_tags(monkeypatch, tmp_path, day):
    plain = _weight(_run(monkeypatch, tmp_path / "a", [SELF], tagged=0))
    tagged = _weight(_run(monkeypatch, tmp_path / "b", [SELF], tagged=1))
    assert tagged == pytest.approx(plain)


def test_capture_is_capped_per_edge_per_day(monkeypatch, tmp_path, day):
    plain = _weight(_run(monkeypatch, tmp_path / "a", [SUCCESS], tagged=0, passes=7))
    tagged = _weight(_run(monkeypatch, tmp_path / "b", [SUCCESS], tagged=1, passes=7))
    cap = float(settings.get("tag_capture_daily_cap"))
    assert tagged - plain <= cap + 1e-6  # seven passes in one day: one day's capture


def test_distinct_days_carry_a_skill_past_injection(monkeypatch, tmp_path, day):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    w = _isolated_wiring(monkeypatch, tmp_path)
    sc = _sc(monkeypatch, tmp_path, w)
    inject = float(settings.get("fragment_inject_threshold", 1.3))
    crossed_on = None
    for d in range(10):
        day["t"] = 1_760_000_000.0 + d * 86400.0
        t = _win_trace(f"d{d}", winner_idx=2, drafter_fragments={"frontal.drafter_C": ["alpha"]})
        t.reward_emissions = [dict(SUCCESS)]
        sc._run_hebbian_pass(f"s{d}", [t])
        if crossed_on is None and _weight(w) >= inject:
            crossed_on = d
    assert crossed_on is not None and 1 <= crossed_on <= 6


def test_grounded_failure_weakens_an_existing_attachment(monkeypatch, tmp_path, day):
    plain = _weight(_run(monkeypatch, tmp_path / "a", [FAILURE], tagged=0, passes=2))
    tagged = _weight(_run(monkeypatch, tmp_path / "b", [FAILURE], tagged=1, passes=2))
    assert tagged <= plain


def test_wiring_frozen_still_halts_everything(monkeypatch, tmp_path, day):
    monkeypatch.setenv("BRAIN_WIRING_FROZEN", "true")
    w = _run(monkeypatch, tmp_path, [SUCCESS], tagged=1)
    assert w.attached_fragments("frontal.drafter_C") == []
