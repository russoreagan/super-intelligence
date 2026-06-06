"""
Tests for the relationship-system audit fixes:
  - F1: style note describes the USER's actual register (not the entity target)
        and the labels actually vary with the user's style
  - F2: self-disclosure intent set matches the real temporal intent vocabulary
  - F5/F9: bond-based affection decay, reunion recovery, bond-driven familiarity
  - OXT accrual gate (F4)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from brain.clusters.parietal import (
    ParietalCluster,
    _measure_formality,
    _measure_verbosity,
)

# ── F1: style note reflects the user, not the clamped entity target ───────────


def _fresh_parietal():
    return ParietalCluster(MagicMock())


def test_style_note_reflects_formal_verbose_user():
    """A user who writes formally and expansively must be described that way —
    not flattened to 'casually/concisely' by the entity-target clamp (F1)."""
    p = _fresh_parietal()
    formal_verbose = (
        "I would be most grateful if you could elaborate further. "
        "Furthermore, regarding the architecture, I believe a thorough "
        "and systematic analysis would be appropriate. Consequently, "
        "please provide a comprehensive and detailed explanation accordingly, "
        "as I wish to understand the complete picture in its entirety."
    )
    for _ in range(4):
        p.update_user_style(formal_verbose, "text", sentiment=0.2, alpha=0.5)
    note = p.user_style_note("text")
    assert note, "style note should emit after >=3 tracked turns"
    # Must NOT describe a formal/verbose user as casual/terse
    assert "casually" not in note
    assert "tersely" not in note
    # Should pick up expansiveness at minimum
    assert "expansively" in note or "formally" in note


def test_style_note_reflects_terse_casual_user():
    p = _fresh_parietal()
    for _ in range(4):
        p.update_user_style("yeah ok lol", "text", sentiment=0.1, alpha=0.5)
    note = p.user_style_note("text")
    assert note
    assert "tersely" in note
    # text brevity disclaimer present for low-verbosity text
    assert "does not indicate coldness" in note


def test_style_note_varies_between_users():
    """The whole point: two different users produce different notes."""
    p1 = _fresh_parietal()
    p2 = _fresh_parietal()
    for _ in range(4):
        p1.update_user_style("yeah sure", "text", 0.1, 0.5)
        p2.update_user_style(
            "I would appreciate a thorough and comprehensive explanation, "
            "furthermore I would like to understand the complete rationale "
            "behind each architectural decision in considerable detail please.",
            "text",
            0.2,
            0.5,
        )
    assert p1.user_style_note("text") != p2.user_style_note("text")


def test_style_note_empty_before_min_turns():
    p = _fresh_parietal()
    p.update_user_style("hello there", "text", 0.1, 0.5)
    assert p.user_style_note("text") == ""


def test_measure_helpers_monotonic():
    assert _measure_verbosity("hi") < _measure_verbosity(" ".join(["word"] * 100))
    assert _measure_formality("yeah gonna lol idk") < _measure_formality(
        "Furthermore, I believe this is appropriate. Consequently, we proceed."
    )


# ── F2: self-disclosure intent set matches real temporal vocabulary ───────────

from brain.clusters.frontal import FrontalCluster  # noqa: E402

# Affection >= 20 (text floor); user model string the helper parses
_WARM_USER = "## Affection score\n- Score: 30\n"
_AFFECT_ENGAGED = {"emotion": "engaged", "affect_dims": {"arousal": 0.5}}


def _disc(intent, modality="text", user_emo="curious", tone="warm", affect=None, user=_WARM_USER):
    features = {
        "intent": intent,
        "input_modality": modality,
        "user_emotion": user_emo,
        "user_tone_toward_ai": tone,
    }
    return FrontalCluster._disclosure_ready(features, affect or _AFFECT_ENGAGED, user)


def test_disclosure_fires_on_greeting_and_question():
    # The whole point of the fix: greeting and question are warm conversational
    # turns that MUST be able to trigger disclosure (they were excluded before).
    assert _disc("greeting") is True
    assert _disc("question") is True
    assert _disc("chitchat") is True
    assert _disc("other") is True


def test_disclosure_silent_on_task_and_hostile():
    assert _disc("task") is False
    assert _disc("memory_recall") is False
    assert _disc("hostile") is False
    assert _disc("epistemic_action") is False


def test_disclosure_blocked_when_user_hostile():
    assert _disc("question", user_emo="frustrated") is False
    assert _disc("question", tone="dismissive") is False


def test_disclosure_blocked_below_affection_floor():
    # Text floor is 20; a score of 10 should not qualify
    assert _disc("question", user="- Score: 10") is False


def test_disclosure_text_floor_higher_than_voice():
    # Score 10 fails for text (floor 20) but passes for voice (floor 5)
    cold_user = "- Score: 10"
    assert _disc("question", modality="text", user=cold_user) is False
    assert _disc("question", modality="voice", user=cold_user) is True


def test_disclosure_needs_something_to_share():
    flat = {"emotion": "neutral", "affect_dims": {"arousal": 0.1}}
    assert _disc("question", affect=flat) is False


# ── P5: instrumentation fields exist with safe defaults ───────────────────────


def test_turntrace_has_relationship_instrumentation():
    from brain.observability.timeline import TurnTrace

    t = TurnTrace(turn_id="t", session_id="s", user_input="hi")
    assert t.disclosure_fired is False
    assert t.style_note_emitted is False
    assert t.oxt_connected_reached is False
    assert t.bond == 0.0
    assert t.reunion_boost_applied == 1.0
    assert t.disclosure_reciprocated is None


def test_mark_trace_flag_sets_current_trace():
    from brain.clusters.frontal import _mark_trace_flag
    from brain.observability.firing_path import reset_current_trace, set_current_trace
    from brain.observability.timeline import TurnTrace

    t = TurnTrace(turn_id="t", session_id="s", user_input="hi")
    tok = set_current_trace(t)
    try:
        _mark_trace_flag("disclosure_fired", True)
        assert t.disclosure_fired is True
    finally:
        reset_current_trace(tok)


def test_mark_trace_flag_noop_without_trace():
    from brain.clusters.frontal import _mark_trace_flag

    # Must not raise when no trace is bound (e.g. unit-test context)
    _mark_trace_flag("disclosure_fired", True)


# ── F5: bond model (decay / recovery / familiarity) ───────────────────────────

from brain.relationship import (  # noqa: E402
    apply_absence,
    decay_affection,
    decay_bond,
    familiarity_from_bond,
    reunion_boost,
)

# Default constants (mirror settings.py defaults)
AFF_BASE = 25.0
BOND_BASE = 90.0
SCALE = 23.0
CLOSE_BOND = 35.0
ACQ_BOND = 12.0


def test_close_friend_long_gap_small_decline():
    """Close friend (bond 60), 90-day gap → affection declines only modestly,
    bond barely moves, familiarity stays close."""
    aff, bond = apply_absence(60, 60, 90, aff_base=AFF_BASE, bond_base=BOND_BASE, scale=SCALE)
    assert 45 <= aff <= 56, f"expected modest decline, got {aff}"
    assert bond >= 55, f"bond should barely move, got {bond}"
    assert familiarity_from_bond(bond, CLOSE_BOND, ACQ_BOND) == "close"


def test_acquaintance_long_gap_fades_to_nothing():
    """Acquaintance (bond 10), 90-day gap → affection mostly gone, familiarity
    drops to new."""
    aff, bond = apply_absence(10, 10, 90, aff_base=AFF_BASE, bond_base=BOND_BASE, scale=SCALE)
    assert aff <= 3, f"acquaintance affection should mostly fade, got {aff}"
    assert familiarity_from_bond(bond, CLOSE_BOND, ACQ_BOND) == "new"


def test_decline_proportional_to_closeness():
    """The closer the prior relationship, the SMALLER the proportional decline
    over the same gap."""
    close_aff, _ = apply_absence(60, 60, 90, aff_base=AFF_BASE, bond_base=BOND_BASE, scale=SCALE)
    acq_aff, _ = apply_absence(10, 10, 90, aff_base=AFF_BASE, bond_base=BOND_BASE, scale=SCALE)
    close_retained = close_aff / 60
    acq_retained = acq_aff / 10
    assert close_retained > acq_retained


def test_reunion_boost_scales_with_gap():
    """A former-close friend (affection decayed below bond) recovers fast;
    boost tapers to 1.0 as affection approaches bond."""
    # Big gap between affection and bond → strong boost
    assert reunion_boost(20, 60, gain=8.0) > 3.0
    # No gap → no boost
    assert reunion_boost(60, 60, gain=8.0) == 1.0
    # affection above bond (shouldn't happen, but safe) → no boost
    assert reunion_boost(70, 60, gain=8.0) == 1.0


def test_reunion_recovery_is_fast():
    """Simulate: close friend decayed 60→50 over a gap; a few +2 warm turns with
    reunion boost should recover toward bond (60) much faster than from scratch."""
    bond = 60.0
    aff = 50.0
    turns = 0
    while aff < 59 and turns < 10:
        boost = reunion_boost(aff, bond, gain=8.0)
        aff = min(bond, aff + 2 * boost)
        turns += 1
    assert turns <= 6, f"recovery should take few turns, took {turns}"


def test_fight_no_gap_keeps_familiarity():
    """A hostile session drops affection but NOT bond → familiarity stays close.
    (Bond only decays on absence, not on low affection.)"""
    # No elapsed time: bond unchanged regardless of affection
    aff, bond = apply_absence(-10, 60, 0, aff_base=AFF_BASE, bond_base=BOND_BASE, scale=SCALE)
    assert bond == 60
    assert familiarity_from_bond(bond, CLOSE_BOND, ACQ_BOND) == "close"


def test_bond_high_water_never_below_affection():
    """apply_absence keeps bond >= decayed affection."""
    aff, bond = apply_absence(80, 80, 5, aff_base=AFF_BASE, bond_base=BOND_BASE, scale=SCALE)
    assert bond >= aff


def test_familiarity_from_bond_thresholds():
    assert familiarity_from_bond(40, CLOSE_BOND, ACQ_BOND) == "close"
    assert familiarity_from_bond(20, CLOSE_BOND, ACQ_BOND) == "acquainted"
    assert familiarity_from_bond(5, CLOSE_BOND, ACQ_BOND) == "new"


def test_decay_monotonic_in_time():
    a1 = decay_affection(50, 30, 10, AFF_BASE, SCALE)
    a2 = decay_affection(50, 30, 30, AFF_BASE, SCALE)
    assert a2 < a1 < 50
    b1 = decay_bond(50, 30, BOND_BASE, SCALE)
    b2 = decay_bond(50, 90, BOND_BASE, SCALE)
    assert b2 < b1 < 50


# ── F3: style persistence round-trips across sessions ─────────────────────────


def test_style_persistence_round_trip():
    """Save the style vector to a fake schema, build a fresh parietal, reload —
    the reloaded vector must carry forward (turns_tracked > 0) so the user
    resumes warm instead of cold-starting."""
    import asyncio

    class _FakeSchema:
        def __init__(self):
            self.store = {}

        def primary_user_name(self):
            return ""

        def speaker_filename(self, name):
            return "user.md"

        def ensure_speaker_schema(self, name):
            return "user.md"

        def read(self, fn):
            return self.store.get(fn, "")

        async def upsert_section(self, fn, section, body):
            self.store[fn] = self.store.get(fn, "") + f"\n## {section}\n{body}\n"

    schema = _FakeSchema()
    p1 = _fresh_parietal()
    for _ in range(5):
        p1.update_user_style(
            "I would appreciate a thorough and comprehensive answer please, "
            "furthermore I would like the complete rationale in considerable detail.",
            "text",
            0.2,
            0.5,
        )
    saved_turns = p1.get_user_style("text").turns_tracked
    assert saved_turns >= 5

    asyncio.run(p1.save_style_to_schema(schema, ""))

    # Fresh session — cold parietal
    p2 = _fresh_parietal()
    assert p2.get_user_style("text").turns_tracked == 0
    p2.load_style_from_schema(schema, "")
    assert p2.get_user_style("text").turns_tracked == saved_turns
    # And the note emits immediately (no 3-turn re-warm needed)
    assert p2.user_style_note("text") != ""


def test_style_persistence_skips_empty():
    """A parietal that tracked nothing should not write a section."""
    import asyncio

    class _FakeSchema:
        def __init__(self):
            self.calls = 0

        def ensure_speaker_schema(self, name):
            return "user.md"

        async def upsert_section(self, fn, section, body):
            self.calls += 1

    schema = _FakeSchema()
    p = _fresh_parietal()
    asyncio.run(p.save_style_to_schema(schema, ""))
    assert schema.calls == 0


# ── F6: affection tier labels are consistent across all subsystems ────────────


def test_affection_tier_labels_consistent_across_clusters():
    """metacognition.affection_to_label() must agree with hippocampus._AFFECTION_TIERS
    for EVERY score in range — they are meant to be one source of truth (F6)."""
    from brain.clusters.hippocampus import HippocampusCluster
    from brain.metacognition import affection_to_label

    # hippocampus tiers are (lower_bound, description); first match where score>=bound.
    # Map its descriptions to the canonical short labels by position.
    hippo = HippocampusCluster._AFFECTION_TIERS
    # description → label by the known ordering close/warm/friendly/neutral/cool/guarded
    order = ["close", "warm", "friendly", "neutral", "cool", "guarded"]

    def hippo_label(score):
        for (bound, _desc), label in zip(hippo, order, strict=False):
            if score >= bound:
                return label
        return "guarded"

    for score in range(-50, 101):
        assert affection_to_label(score) == hippo_label(score), (
            f"mismatch at score={score}: "
            f"meta={affection_to_label(score)} hippo={hippo_label(score)}"
        )


def test_affection_label_boundary_minus_11_is_cool():
    """The specific off-by-one the audit caught: -11 must be 'cool', not 'neutral'."""
    from brain.metacognition import affection_to_label

    assert affection_to_label(-11) == "cool"
    assert affection_to_label(-10) == "neutral"


# ── F7: relationship_stage_from_content parses everything incl. bond ──────────


def test_relationship_stage_from_content():
    from brain.metacognition import relationship_stage_from_content

    content = (
        "## Relationship\n- Familiarity: close (interactions: 40)\n"
        "## Affection score\n- Score: 30\n- Bond: 55.0\n- Interactions: 40\n"
    )
    stage = relationship_stage_from_content(content)
    assert stage.affection == 30
    assert stage.tier == "close"
    assert stage.affection_label == "warm"  # 30 → warm band
    assert stage.session_count == 40
    assert stage.bond == 55.0


def test_relationship_stage_empty_content():
    from brain.metacognition import relationship_stage_from_content

    stage = relationship_stage_from_content("")
    assert stage.tier == "new"
    assert stage.affection == 0
    assert stage.bond == 0.0


# ── Eval: deterministic relationship_monitor ──────────────────────────────────


def _turn(**kw):
    base = {
        "type": "turn",
        "user_sentiment": 0.0,
        "familiarity_tier": "friendly",
        "affection": 10,
        "affection_label": "friendly",
    }
    base.update(kw)
    return base


def test_relationship_monitor_basic_rates():
    from eval.relationship_monitor import compute_relationship_metrics

    turns = [
        _turn(disclosure_fired=True, disclosure_reciprocated=True, user_sentiment=0.2),
        _turn(user_sentiment=0.5),  # the turn after disclosure (sentiment rose)
        _turn(style_note_emitted=True, style_register="casually/tersely"),
        _turn(oxt_connected_reached=True, bond=55.0),
        _turn(disclosure_fired=True, disclosure_reciprocated=False),
    ]
    m = compute_relationship_metrics(turns)
    assert m["turns"] == 5
    assert m["disclosure_fired_count"] == 2
    assert m["disclosure_fire_rate"] == 0.4
    # two resolved, one reciprocated → 0.5
    assert m["disclosure_reciprocation_rate"] == 0.5
    assert m["style_note_rate"] == 0.2
    assert m["style_register_variety"] == ["casually/tersely"]
    assert m["oxt_connected_rate"] == 0.2


def test_relationship_monitor_trajectories():
    from eval.relationship_monitor import compute_relationship_metrics

    turns = [
        _turn(bond=10.0, affection=10),
        _turn(bond=30.0, affection=25),
        _turn(bond=55.0, affection=40),
    ]
    m = compute_relationship_metrics(turns)
    assert m["bond_trajectory"] == {"first": 10.0, "last": 55.0, "min": 10.0, "max": 55.0}
    assert m["affection_trajectory"]["last"] == 40


def test_relationship_monitor_empty():
    from eval.relationship_monitor import compute_relationship_metrics

    assert compute_relationship_metrics([])["turns"] == 0


def test_relationship_monitor_reunion_count():
    from eval.relationship_monitor import compute_relationship_metrics

    turns = [
        _turn(reunion_boost_applied=1.0),
        _turn(reunion_boost_applied=3.2),
        _turn(reunion_boost_applied=1.5),
    ]
    m = compute_relationship_metrics(turns)
    assert m["reunion_boost_turns"] == 2


# ── Eval: relationship_judge is built but OFF by default ──────────────────────


def test_relationship_judge_disabled_by_default(monkeypatch):
    """The judge must be wired but inert unless BRAIN_EVAL_RELATIONSHIP is set."""
    from unittest.mock import MagicMock

    monkeypatch.delenv("BRAIN_EVAL_RELATIONSHIP", raising=False)
    from eval.relationship_judge import RelationshipJudge

    judge = RelationshipJudge(MagicMock())
    assert judge._enabled is False
    # fire() must be a no-op when disabled (no task scheduled, no raise)
    judge.fire(MagicMock())


def test_relationship_judge_enabled_with_env(monkeypatch):
    monkeypatch.setenv("BRAIN_EVAL_RELATIONSHIP", "true")
    from unittest.mock import MagicMock

    from eval.relationship_judge import RelationshipJudge

    judge = RelationshipJudge(MagicMock())
    assert judge._enabled is True


# ── Style register accessor ───────────────────────────────────────────────────


def test_user_style_register_string():
    p = _fresh_parietal()
    for _ in range(4):
        p.update_user_style("yeah ok sure", "text", 0.1, 0.5)
    reg = p.user_style_register("text")
    assert "/" in reg
    assert "tersely" in reg


def test_user_style_register_empty_before_min_turns():
    p = _fresh_parietal()
    p.update_user_style("hello", "text", 0.1, 0.5)
    assert p.user_style_register("text") == ""


# ── Performed-emotion gate (relationship + mood) ──────────────────────────────


def _user_model(score: int, tier: str) -> str:
    return f"## Affection score\n- Score: {score}\n- Familiarity: {tier}\n"


def _perf(score, tier, user_emo="neutral", tone="neutral"):
    from brain.clusters.frontal import FrontalCluster

    features = {"user_emotion": user_emo, "user_tone_toward_ai": tone, "intent": "chitchat"}
    return FrontalCluster._performed_emotion_gate(features, {}, _user_model(score, tier))


def test_performed_blocked_for_unfamiliar_cool():
    # The explicit "definitely not": unfamiliar + cool/guarded.
    assert _perf(-20, "new")[0] is False  # guarded
    assert _perf(-15, "new")[0] is False  # cool
    # New but not warm enough (below new floor 15) → blocked even at neutral.
    assert _perf(5, "new")[0] is False


def test_performed_blocked_for_cool_even_if_familiar():
    # A cool/guarded relationship blocks performance regardless of familiarity.
    assert _perf(-15, "close")[0] is False
    assert _perf(-30, "acquainted")[0] is False


def test_performed_playful_when_user_positive():
    ok, flavor = _perf(30, "close", user_emo="happy")
    assert ok is True and flavor == "playful"
    ok, flavor = _perf(20, "acquainted", tone="joking")
    assert ok is True and flavor == "playful"


def test_performed_cheerup_requires_established_relationship():
    # Down user + close warm relationship → cheer_up allowed.
    ok, flavor = _perf(30, "close", user_emo="sad")
    assert ok is True and flavor == "cheer_up"
    # Down user + new/low relationship → NOT allowed (won't land).
    assert _perf(18, "new", user_emo="sad")[0] is False
    assert _perf(10, "acquainted", user_emo="sad")[0] is False  # below cheerup floor 20


def test_performed_tension_break_on_friction():
    ok, flavor = _perf(25, "close", user_emo="frustrated")
    assert ok is True and flavor == "tension_break"
    ok, flavor = _perf(25, "acquainted", tone="impatient")
    assert ok is True and flavor == "tension_break"


def test_performed_neutral_mood_needs_warmth():
    # Neutral mood, warm enough → playful.
    assert _perf(15, "acquainted")[0] is True
    assert _perf(12, "new")[0] is False  # new + only mild warmth (below new floor)
    # Neutral mood, close familiarity even at modest affection → allowed.
    assert _perf(5, "close")[0] is True


def test_performed_gate_disabled_returns_always_on(monkeypatch):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "enable_performed_emotion_gate", 0)
    try:
        ok, flavor = _perf(-50, "new", user_emo="angry")
        assert ok is True  # feature off → preserve old always-offered behaviour
    finally:
        monkeypatch.setitem(settings._data, "enable_performed_emotion_gate", 1)


def test_performed_emotion_monitor_metric():
    from eval.relationship_monitor import compute_relationship_metrics

    turns = [
        _turn(performed_emotion_offered="playful"),
        _turn(performed_emotion_offered="cheer_up"),
        _turn(),
        _turn(performed_emotion_offered="playful"),
    ]
    m = compute_relationship_metrics(turns)
    assert m["performed_emotion_rate"] == 0.75
    assert m["performed_emotion_flavors"] == {"playful": 2, "cheer_up": 1}
