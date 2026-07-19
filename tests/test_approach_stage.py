"""
Pre-tool approach competition — phases 1-3 of the approach-competition plan.

Covers: the sanitizer (the motor boundary's structural + lexical layers), the
anchor's admissibility, the pair ledger's count semantics and cap, the outcome
verifier's per-dimension directions and reliability weighting, and the sleep-time
stance credit (outcome-first, light loser demotion, no edge created to demote).
"""

from __future__ import annotations

import importlib

import pytest

from brain.approach_outcome import NEGATIVE_TONES, PendingApproach, verify
from brain.clusters.approach_schema import (
    APPROACH_KEYS,
    sanitize_approach,
    wants_action,
)
from brain.fragment_pool import APPROACH_ANCHOR, is_admissible
from brain.observability.timeline import TurnTrace
from brain.settings import settings
from brain.stance_pairs import pair_key, record_candidate, record_verdict, residual

TOOLS = ["read_file", "run_command", "fetch_url", "cloud_action"]


# ── sanitizer: the boundary proof ────────────────────────────────────────────


def test_tool_key_is_dropped_at_parse():
    out = sanitize_approach(
        {"stance": "Answer directly.", "tool": "run_command", "args": {"cmd": "ls"}}, TOOLS
    )
    assert out is not None
    assert "tool" not in out and "args" not in out
    assert set(out) <= APPROACH_KEYS


def test_tool_names_scrubbed_from_free_text():
    out = sanitize_approach(
        {"stance": "First run_command against the logs, then fetch_url the docs."}, TOOLS
    )
    assert "run_command" not in out["stance"]
    assert "fetch_url" not in out["stance"]
    assert "[tool]" in out["stance"]


def test_decomposition_must_be_interrogative():
    out = sanitize_approach(
        {
            "stance": "Diagnose before fixing.",
            "decomposition": [
                "What changed in the deploy?",
                "read the config file",  # an action — dropped
                "Is the premise even right?",
            ],
        },
        TOOLS,
    )
    assert out["decomposition"] == ["What changed in the deploy?", "Is the premise even right?"]


def test_external_kind_rejects_urls_paths_backticks():
    for bad in ("https://example.com", "/etc/passwd", "`ls`"):
        out = sanitize_approach({"stance": "s", "external_kind": bad}, TOOLS)
        assert "external_kind" not in out
    ok = sanitize_approach({"stance": "s", "external_kind": "current market data"}, TOOLS)
    assert ok["external_kind"] == "current market data"


def test_no_stance_disqualifies_and_defaults_are_safe():
    assert sanitize_approach({"information_need": "external"}, TOOLS) is None
    assert sanitize_approach(None, TOOLS) is None
    out = sanitize_approach({"stance": "s"}, TOOLS)
    assert out["information_need"] == "none"
    assert wants_action(out) is False


def test_wants_action_mapping_is_a_lookup():
    assert wants_action({"information_need": "external"})
    assert wants_action({"information_need": "both"})
    assert not wants_action({"information_need": "internal"})  # info ≠ tool
    assert not wants_action({"information_need": "none"})


# ── anchor admissibility ─────────────────────────────────────────────────────


def test_anchor_hosts_both_stance_axes_and_nothing_else():
    assert is_admissible("stance-verify-the-premise", APPROACH_ANCHOR)
    assert is_admissible("constraint-hardness-testing", APPROACH_ANCHOR, "method")
    # a procedural (draft_slot) fragment can never land on the anchor
    assert not is_admissible("constraint-hardness-testing", APPROACH_ANCHOR)
    assert not is_admissible("some-partner-skill", APPROACH_ANCHOR)


# ── pair ledger ──────────────────────────────────────────────────────────────


def test_pair_ledger_counts_not_rates(monkeypatch):
    led: dict = {}
    for i in range(4):
        record_candidate(led, "stance-a", "method-x", won=(i == 0), now=100.0 + i)
    row = led[pair_key("stance-a", "method-x")]
    assert row["plays"] == 4 and row["wins"] == 1
    assert row["ext_wins"] == 0 and row["confirmed"] == 0
    record_verdict(led, "stance-a", "method-x", column="confirmed", now=200.0)
    assert led[pair_key("stance-a", "method-x")]["confirmed"] == 1


def test_pair_ledger_cap_evicts_stalest(monkeypatch):
    monkeypatch.setitem(settings._data, "stance_pair_cap", 5)
    led: dict = {}
    for i in range(9):
        record_candidate(led, f"s{i}", "m", won=False, now=float(i))
    assert len(led) <= 6  # cap + the just-inserted row before eviction settles
    assert pair_key("s8", "m") in led  # freshest survives
    assert pair_key("s0", "m") not in led  # stalest evicted


def test_pair_verdict_never_resurrects(monkeypatch):
    led: dict = {}
    record_verdict(led, "gone", "m", column="confirmed")
    assert led == {}


def test_residual_gated_on_min_plays(monkeypatch):
    monkeypatch.setitem(settings._data, "stance_pair_min_plays", 3)
    led: dict = {}
    record_candidate(led, "a", "m", won=True, now=1.0)
    assert residual(led, "a", "m", 0.5) is None  # 1 play < 3
    record_candidate(led, "a", "m", won=True, now=2.0)
    record_candidate(led, "a", "m", won=False, now=3.0)
    r = residual(led, "a", "m", 0.5)
    assert r == pytest.approx(2 / 3 - 0.5)


# ── outcome verifier ─────────────────────────────────────────────────────────


def _pending(**kw) -> PendingApproach:
    base = {
        "turn_id": "t1",
        "information_need": "none",
        "info_id": "stance-answer-from-known",
        "method_id": "logic-check",
        "override": "",
        "query_vec": [1.0, 0.0],
        "topic": "databases",
        "ts": 1000.0,
    }
    base.update(kw)
    return PendingApproach(**base)


def _features(**kw) -> dict:
    base = {
        "raw_text": "ok thanks",
        "user_tone_toward_ai": "neutral",
        "user_emotion": "neutral",
        "intent": "chitchat",
        "topic_summary": "databases",
        "sentiment": 0.2,
        "switch_only": False,
    }
    base.update(kw)
    return base


def test_tool_failure_refutes_external(monkeypatch):
    p = _pending(information_need="external", tool_success=False)
    v = verify(p, _features(), None, now=1010.0)
    assert v["info"] < 0 and "tool_failed" in v["signals"] and not v["confirmed"]


def test_empty_tool_output_is_weak_negative(monkeypatch):
    p = _pending(information_need="external", tool_success=True)
    p.tool_output_len = 5
    v = verify(p, _features(), None, now=1010.0)
    assert -0.5 < v["info"] < 0 and "tool_empty" in v["signals"]


def test_post_suppression_tool_request_refutes(monkeypatch):
    p = _pending(override="suppressed_action")
    v = verify(p, _features(raw_text="please fetch https://example.com"), None, now=1010.0)
    assert v["info"] <= -1.0 + 1e-9 or v["info"] < 0
    assert "post_suppression_tool_request" in v["signals"]


def test_reask_refutes_both_axes_but_not_on_topic_change(monkeypatch):
    same = verify(_pending(), _features(), [1.0, 0.0], now=1010.0)
    assert "re_ask" in same["signals"] and same["info"] < 0 and same["method"] < 0
    moved = verify(_pending(), _features(topic_summary="cooking"), [1.0, 0.0], now=1010.0)
    assert "re_ask" not in moved["signals"]


def test_confusion_indicts_method_not_info(monkeypatch):
    v = verify(_pending(), _features(user_emotion="confused"), None, now=1010.0)
    assert v["method"] < 0 and v["info"] == 0.0 and "confusion" in v["signals"]


def test_tone_toward_ai_is_the_channel_not_user_emotion(monkeypatch):
    """Frustrated at the PROBLEM but warm toward the AI must not read as failure."""
    v = verify(
        _pending(),
        _features(user_emotion="frustrated", user_tone_toward_ai="warm", sentiment=-0.4),
        None,
        now=1010.0,
    )
    assert not any(s.startswith("tone_") for s in v["signals"])
    for tone in NEGATIVE_TONES:
        v2 = verify(_pending(), _features(user_tone_toward_ai=tone), None, now=1010.0)
        assert f"tone_{tone}" in v2["signals"] and v2["info"] < 0


def test_switch_only_downweights_tone_but_not_cosine(monkeypatch):
    full = verify(_pending(), _features(user_tone_toward_ai="impatient"), None, now=1010.0)
    half = verify(
        _pending(),
        _features(user_tone_toward_ai="impatient", switch_only=True),
        None,
        now=1010.0,
    )
    assert half["info"] == pytest.approx(full["info"] / 2)
    # cosine keeps full weight on a switch_only turn (degenerate topic can't veto)
    re = verify(
        _pending(),
        _features(switch_only=True, topic_summary="greeting"),
        [1.0, 0.0],
        now=1010.0,
    )
    assert "re_ask" in re["signals"]


def test_quiet_turn_is_weakest_and_never_alone_a_confirmation_signal(monkeypatch):
    v = verify(_pending(), _features(), None, now=1010.0)
    assert v["signals"] == ["quiet_ok"]
    assert 0 < v["info"] <= 0.2  # never the weight of a real confirmation


def test_stash_expires(monkeypatch):
    assert verify(_pending(ts=0.0), _features(), None, now=100000.0) is None


# ── sleep-time stance credit ─────────────────────────────────────────────────


def _isolated_wiring(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_WIRING_PATH", str(tmp_path / "wiring.json"))
    monkeypatch.setenv("BRAIN_WIRING_HISTORY_DIR", str(tmp_path / "history"))
    import brain.wiring as w_mod

    importlib.reload(w_mod)
    return w_mod.Wiring()


def _approach_trace(outcome: dict | None = None) -> TurnTrace:
    t = TurnTrace(turn_id="ap_turn", session_id="s", user_input="x")
    t.approach_scores = [
        {
            "cell": "frontal.approach_A",
            "info_id": "stance-verify-the-premise",
            "method_id": "logic-check",
            "overall": 0.8,
            "selected": True,
            "vetoed": False,
            "critic_ran": True,
        },
        {
            "cell": "frontal.approach_B",
            "info_id": "stance-do-and-report",
            "method_id": "decision-premortem-analysis",
            "overall": 0.4,
            "selected": False,
            "vetoed": False,
            "critic_ran": True,
        },
    ]
    if outcome is not None:
        t.approach_outcome = outcome
    return t


def test_winner_stances_earn_anchor_edges(monkeypatch, tmp_path):
    from brain.hebbian import HebbianUpdater

    w = _isolated_wiring(monkeypatch, tmp_path)
    h = HebbianUpdater(w)
    n = h._apply_approach_stance_credit(_approach_trace(), 1.0, [], [])
    assert n > 0
    assert w.has("fragment.stance-verify-the-premise", APPROACH_ANCHOR)
    assert w.has("fragment.logic-check", APPROACH_ANCHOR)
    # both winner edges above rest (small self-graded step)
    assert w.get_edge_weight("fragment.stance-verify-the-premise", APPROACH_ANCHOR) > 1.0


def test_verified_outcome_outweighs_critic_preference(monkeypatch, tmp_path):
    """A refuted winner LOSES weight even though the critic preferred it — pins
    outcome-first so a tuning pass can't silently invert it back to self-graded."""
    from brain.hebbian import HebbianUpdater

    w = _isolated_wiring(monkeypatch, tmp_path)
    h = HebbianUpdater(w)
    h._apply_approach_stance_credit(
        _approach_trace(outcome={"info": -1.0, "method": -0.8, "confirmed": False}),
        1.0,
        [],
        [],
    )
    assert w.get_edge_weight("fragment.stance-verify-the-premise", APPROACH_ANCHOR) < 1.0


def test_loser_never_gets_an_edge_created_to_demote(monkeypatch, tmp_path):
    from brain.hebbian import HebbianUpdater

    w = _isolated_wiring(monkeypatch, tmp_path)
    h = HebbianUpdater(w)
    h._apply_approach_stance_credit(_approach_trace(), 1.0, [], [])
    assert not w.has("fragment.stance-do-and-report", APPROACH_ANCHOR)
    assert not w.has("fragment.decision-premortem-analysis", APPROACH_ANCHOR)


def test_credit_flag_kills_it(monkeypatch, tmp_path):
    from brain.hebbian import HebbianUpdater

    monkeypatch.setitem(settings._data, "approach_competition_credit", 0)
    w = _isolated_wiring(monkeypatch, tmp_path)
    h = HebbianUpdater(w)
    assert h._apply_approach_stance_credit(_approach_trace(), 1.0, [], []) == 0


def test_competitions_are_independent(monkeypatch, tmp_path):
    """One trace with BOTH draft_scores and approach_scores: both edge sets move,
    neither leaks into the other's namespace."""
    from brain.hebbian import HebbianUpdater

    w = _isolated_wiring(monkeypatch, tmp_path)
    w.add("frontal.executive", "frontal.drafter_A", weight=1.0)
    w.add("frontal.executive", "frontal.drafter_B", weight=1.0)
    h = HebbianUpdater(w)
    t = _approach_trace()
    t.draft_scores = [
        {"draft_id": "draft_0_ap_turn", "overall": 0.9, "selected": True, "critic_ran": True},
        {"draft_id": "draft_1_ap_turn", "overall": 0.4, "selected": False, "critic_ran": True},
    ]
    h._apply_drafter_competition(t, 0.5, 1.0, [], [])
    h._apply_approach_stance_credit(t, 1.0, [], [])
    assert w.get_edge_weight("frontal.executive", "frontal.drafter_A") > 1.0
    assert w.has("fragment.stance-verify-the-premise", APPROACH_ANCHOR)
    # no cross-contamination: no fragment edges on drafters, no exec→approach edges
    assert not w.has("fragment.stance-verify-the-premise", "frontal.drafter_A")
    assert not w.has("frontal.executive", "frontal.approach_A")


def test_settings_registered():
    from brain.settings import DEFAULTS

    for key in (
        "approach_competition",
        "approach_authority",
        "approach_authority_confidence_floor",
        "approach_candidate_base",
        "approach_predictor_bypass",
        "approach_competition_credit",
        "stance_inject_winner_body",
        "stance_pair_ledger",
        "stance_pair_cap",
        "stance_pair_min_plays",
    ):
        assert key in DEFAULTS, key
