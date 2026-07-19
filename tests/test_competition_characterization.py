"""
Characterization tests for the drafter-competition reward math (Phase 0).

These pin the EXACT current behavior — post-weights, gainers/losers, decision
events, and no-op conditions — so the extraction of a generic competition core
is provably behavior-preserving rather than argued-to-be. Written and run green
against the pre-refactor code first; any refactor that moves these numbers is
a change to a REWARD RULE, and drift in reward is silent (nothing crashes, the
brain just learns something slightly different).

Constants derive from: hebbian_outcome_delta=0.02, plasticity=1.0 →
  winner bonus  = margin(0.5) × 0.02 × 0.5  = 0.005
  loser penalty = shortfall(0.5) × 0.02 × 0.25 = 0.0025
"""

from __future__ import annotations

import importlib

import pytest

import brain.observability.decisions as dmod
from brain.observability.timeline import TurnTrace


def _isolated_wiring(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_WIRING_PATH", str(tmp_path / "wiring.json"))
    monkeypatch.setenv("BRAIN_WIRING_HISTORY_DIR", str(tmp_path / "history"))
    import brain.wiring as w_mod

    importlib.reload(w_mod)
    return w_mod.Wiring()


def _make_trace(
    winner_idx=0, winner_score=0.9, loser_score=0.4, n=2, no_selected=False, one_scored=False
):
    t = TurnTrace(turn_id="comp_turn", session_id="s", user_input="x")
    t.draft_scores = []
    for i in range(n):
        t.draft_scores.append(
            {
                "draft_id": f"draft_{i}_comp_turn",
                "overall": winner_score if i == winner_idx else loser_score,
                "selected": (i == winner_idx) and not no_selected,
                "vetoed": False,
                "critic_ran": (i == 0) if one_scored else True,
            }
        )
    return t


@pytest.fixture()
def rig(monkeypatch, tmp_path):
    w = _isolated_wiring(monkeypatch, tmp_path)
    for d in "ABC":
        w.add("frontal.executive", f"frontal.drafter_{d}", weight=1.0)
    from brain.hebbian import HebbianUpdater

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(dmod.decisions, "log", lambda kind, **kw: events.append((kind, kw)))
    return w, HebbianUpdater(w), events


def test_two_drafter_exact_post_weights_and_events(rig):
    w, h, events = rig
    gainers, losers = [], []
    h._apply_drafter_competition(_make_trace(), 0.5, 1.0, gainers, losers)

    assert w.get_edge_weight("frontal.executive", "frontal.drafter_A") == pytest.approx(
        1.005, abs=1e-12
    )
    assert w.get_edge_weight("frontal.executive", "frontal.drafter_B") == pytest.approx(
        0.9975, abs=1e-12
    )
    assert [(lbl, pytest.approx(d, abs=1e-9)) for lbl, d in gainers] == [
        ("frontal.executive→frontal.drafter_A", pytest.approx(0.005, abs=1e-9))
    ]
    assert [(lbl, pytest.approx(d, abs=1e-9)) for lbl, d in losers] == [
        ("frontal.executive→frontal.drafter_B", pytest.approx(-0.0025, abs=1e-9))
    ]
    assert [k for k, _ in events] == ["drafter_competition_applied"] * 2
    win_ev = events[0][1]
    lose_ev = events[1][1]
    assert win_ev == {
        "turn_id": "comp_turn",
        "drafter": "frontal.drafter_A",
        "won": True,
        "from_weight": 1.0,
        "to_weight": 1.005,
        "delta": 0.005,
        "winner_score": 0.9,
    }
    assert lose_ev == {
        "turn_id": "comp_turn",
        "drafter": "frontal.drafter_B",
        "won": False,
        "from_weight": 1.0,
        "to_weight": 0.9975,
        "delta": -0.0025,
        "winner_score": 0.9,
    }


def test_three_drafter_winner_mid_slate_order_preserved(rig):
    w, h, events = rig
    h._apply_drafter_competition(
        _make_trace(winner_idx=1, winner_score=0.8, loser_score=0.3, n=3), 0.5, 1.0, [], []
    )
    assert w.get_edge_weight("frontal.executive", "frontal.drafter_A") == pytest.approx(0.9975)
    assert w.get_edge_weight("frontal.executive", "frontal.drafter_B") == pytest.approx(1.005)
    assert w.get_edge_weight("frontal.executive", "frontal.drafter_C") == pytest.approx(0.9975)
    # events emitted in draft_scores iteration order, winner mid-slate
    assert [(e["drafter"], e["won"]) for _, e in events] == [
        ("frontal.drafter_A", False),
        ("frontal.drafter_B", True),
        ("frontal.drafter_C", False),
    ]
    assert all(e["winner_score"] == 0.8 for _, e in events)


def test_no_selected_entry_is_a_complete_noop(rig):
    w, h, events = rig
    gainers, losers = [], []
    h._apply_drafter_competition(_make_trace(no_selected=True), 0.5, 1.0, gainers, losers)
    assert w.get_edge_weight("frontal.executive", "frontal.drafter_A") == 1.0
    assert w.get_edge_weight("frontal.executive", "frontal.drafter_B") == 1.0
    assert gainers == [] and losers == [] and events == []


def test_fewer_than_two_critic_ran_is_a_complete_noop(rig):
    w, h, events = rig
    h._apply_drafter_competition(_make_trace(one_scored=True), 0.5, 1.0, [], [])
    assert w.get_edge_weight("frontal.executive", "frontal.drafter_A") == 1.0
    assert events == []


def test_unwired_and_malformed_ids_are_skipped(rig):
    """Non-drafter producers ("switch_draft", "subsystem_x_t") and out-of-graph
    indices fall out silently — existing behavior, deliberately preserved."""
    w, h, events = rig
    t = _make_trace()
    t.draft_scores.append(
        {"draft_id": "switch_draft", "overall": 0.7, "selected": False, "critic_ran": True}
    )
    t.draft_scores.append(
        {"draft_id": "draft_9_comp_turn", "overall": 0.7, "selected": False, "critic_ran": True}
    )
    h._apply_drafter_competition(t, 0.5, 1.0, [], [])
    # winner/loser still move exactly as in the 2-drafter case; extras contribute nothing
    assert w.get_edge_weight("frontal.executive", "frontal.drafter_A") > 1.0
    assert {e["drafter"] for _, e in events} == {"frontal.drafter_A", "frontal.drafter_B"}
