"""The external-verdict reward channel — end to end.

This is the ONE reward signal grounded outside the agent's own appraisal (a
thumbs press, a rating, an automated grade normalized to [-1, +1]) in a system
the premise audit measured at ~80% self-graded. These tests prove the channel
is genuinely LIVE, not just reachable:

  1. Submitting an external grade moves DA via the "external_grader" source.
  2. The DA-provenance tally records that write as EXTERNAL, not self_graded —
     so the self-graded ratio the system tracks becomes honest.
  3. The Hebbian composite learning outcome includes the external term.

A test here FAILS if external_grade_da_nudge regresses to 0 (the branch goes
dead again) or if the composite drops the external term. That regression guard
is the point: the channel was dead code for most of the project's life.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from brain.settings import DEFAULTS


# ── The shipped default is a meaningful, calibrated, non-zero float ───────────
def test_nudge_default_is_meaningful_nonzero_float():
    """The DA nudge default (source of truth, immune to settings.json / singleton
    mutation) must be a POSITIVE FLOAT. If this regresses to 0 the whole channel
    goes dead — the api_grade_turn branch becomes unreachable again. If it becomes
    an int, a fractional nudge silently truncates to 0 on load."""
    nudge = DEFAULTS["external_grade_da_nudge"]
    assert isinstance(nudge, float), "an int default truncates fractional nudges to 0 on load"
    assert nudge > 0.0, "0 = dead channel: the external verdict cannot move chemistry"


def test_nudge_default_is_calibrated_against_reference_payouts():
    """Calibration guard. The audit measured two reference DA payouts: inferred
    praise pays ~0.10, finishing a job pays ~0.34. An explicit external verdict
    should land ABOVE inferred praise (it's stronger evidence) but BELOW the
    intrinsic accomplishment signal (so it grounds rather than dominates)."""
    nudge = DEFAULTS["external_grade_da_nudge"]
    assert nudge > 0.10, "should outweigh merely-inferred praise (~0.10 DA)"
    assert nudge < 0.34, "must not dominate the intrinsic accomplishment signal (~0.34 DA)"


# ── 1 + 2: a grade moves DA via external_grader, tallied as external ──────────
def _loops_stub():
    """Minimal object carrying just what api_grade_turn touches: a real Bus, a
    live trace list, and no eval logger. api_grade_turn is a plain _LoopsMixin
    method, so binding it to this stub exercises the real write path."""
    from brain.bus import Bus
    from brain.observability.timeline import TurnTrace

    trace = TurnTrace(turn_id="t_ext_1", session_id="s", user_input="hi")
    stub = SimpleNamespace(bus=Bus(), _session_traces_full=[trace], _eval_logger=None)
    return stub, trace


@pytest.fixture
def _nudge_at_default(monkeypatch):
    """Bind the effective nudge to the SHIPPED default so the wiring test is
    deterministic AND still tied to the production value: if the default is ever
    set back to 0, the DA-moves assertions below fail rather than silently pass."""
    from brain.settings import settings

    monkeypatch.setitem(
        settings._data, "external_grade_da_nudge", DEFAULTS["external_grade_da_nudge"]
    )
    return DEFAULTS["external_grade_da_nudge"]


def test_thumbs_up_moves_da_via_external_grader(_nudge_at_default):
    from brain.session_loops import _LoopsMixin

    stub, trace = _loops_stub()
    nudge = _nudge_at_default
    da_before = stub.bus.neuromod.get("DA")

    result = _LoopsMixin.api_grade_turn(stub, "t_ext_1", 1)

    assert result["ok"] is True
    assert result["grade"] == 1.0
    assert result["applied_live"] is True
    # The grade landed on the live trace (consumed by the Hebbian composite at sleep).
    assert trace.external_grade == 1.0
    assert trace.external_grade_source == "user_thumbs"
    # DA actually moved, by the calibrated nudge (grade = +1 → nudge * 1). Strict
    # inequality so a regression of the default to 0 fails here, not silently passes.
    assert stub.bus.neuromod.get("DA") > da_before
    assert stub.bus.neuromod.get("DA") - da_before == pytest.approx(nudge)


def test_thumbs_down_moves_da_negative(_nudge_at_default):
    from brain.session_loops import _LoopsMixin

    stub, trace = _loops_stub()
    nudge = _nudge_at_default
    da_before = stub.bus.neuromod.get("DA")

    result = _LoopsMixin.api_grade_turn(stub, "t_ext_1", -1)

    assert result["grade"] == -1.0
    assert trace.external_grade == -1.0
    # A negative verdict pushes DA DOWN (grade = -1 → nudge * -1). Strict so a
    # regression of the default to 0 fails here rather than silently passing.
    assert stub.bus.neuromod.get("DA") < da_before
    assert stub.bus.neuromod.get("DA") - da_before == pytest.approx(-nudge)


def test_grade_tally_records_external_not_self_graded(_nudge_at_default):
    """The DA-provenance tally must classify the grade's write as EXTERNAL. With
    the nudge OFF (the old dead-code state) this write never happened, so the
    tally could only ever be intrinsic — the self-graded ratio was structurally
    incapable of improving. Now it can."""
    from brain.session_loops import _LoopsMixin

    stub, _ = _loops_stub()
    _LoopsMixin.api_grade_turn(stub, "t_ext_1", 1)

    tally = stub.bus.neuromod.da_source_tally()
    assert tally["external"] == pytest.approx(_nudge_at_default)
    assert tally["intrinsic"] == 0.0  # nothing self-administered on this bus


def test_grade_emission_signal_type_is_external_grader(monkeypatch, _nudge_at_default):
    """At the reward-emission chokepoint the write is stamped signal_type=
    'external_grader' — NOT the 'self_graded' default. This is what makes the
    self-graded-vs-external mix an honest, queryable fact."""
    from brain.observability.decisions import decisions
    from brain.session_loops import _LoopsMixin

    captured: list[dict] = []
    orig_log = decisions.log

    def _spy(decision, **fields):
        captured.append({"decision": decision, **fields})
        return orig_log(decision, **fields)

    monkeypatch.setattr(decisions, "log", _spy)

    stub, _ = _loops_stub()
    _LoopsMixin.api_grade_turn(stub, "t_ext_1", 1)

    emissions = [c for c in captured if c["decision"] == "reward_emission"]
    assert emissions, "the DA write must flow through the reward-emission chokepoint"
    assert emissions[0]["source"] == "external_grader"
    assert emissions[0]["signal_type"] == "external_grader"
    assert emissions[0]["signal_type"] != "self_graded"


def test_zero_nudge_records_grade_but_moves_no_chemistry(monkeypatch):
    """When the nudge is 0 (a tenant that explicitly opts out), the grade is still
    RECORDED on the trace and in the log — observability survives — but chemistry
    is untouched. This pins the exact boundary the default crossed."""
    from brain.session_loops import _LoopsMixin
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "external_grade_da_nudge", 0.0)
    stub, trace = _loops_stub()
    da_before = stub.bus.neuromod.get("DA")

    result = _LoopsMixin.api_grade_turn(stub, "t_ext_1", 1)

    assert result["ok"] is True
    assert trace.external_grade == 1.0  # still recorded
    assert stub.bus.neuromod.get("DA") == da_before  # but no nudge
    assert stub.bus.neuromod.da_source_tally()["external"] == 0.0


# ── 3: the Hebbian composite learning outcome includes the external term ──────
def _graded_trace(external_grade, *, da=0.5, prior_da=0.5, user_emotion=""):
    """A trace with every intrinsic signal zeroed (no DA delta, no critic run,
    neutral user emotion) so the ONLY thing moving the composite is the external
    grade — isolating its contribution to exactly w_external * grade."""
    from brain.observability.timeline import TurnTrace

    t = TurnTrace(turn_id="t_comp", session_id="s", user_input="x")
    t.neuromod = {"DA": da, "GABA": 0.0, "ACh": 0.3, "Glu": 0.3}
    t.prior_neuromod = {"DA": prior_da, "GABA": 0.0, "ACh": 0.3, "Glu": 0.3}
    t.draft_scores = []  # no critic_ran → critic term is exactly 0
    t.user_emotion = user_emotion  # "" → valence 0
    if external_grade is not None:
        t.external_grade = external_grade
    return t


def test_composite_outcome_includes_external_term():
    """With all intrinsic signals zeroed, the graded composite equals exactly
    w_external * grade — proving the external verdict flows into the learning
    outcome at its configured weight."""
    from brain.hebbian import HebbianUpdater
    from brain.settings import settings

    hu = HebbianUpdater(None)  # _composite_outcome never touches wiring
    w_ext = float(settings.get("hebbian_w_external", 0.2))

    outcome_pos, breakdown = hu._composite_outcome(_graded_trace(1.0))
    assert outcome_pos == pytest.approx(w_ext)
    assert breakdown["external"] == pytest.approx(1.0)

    outcome_neg, _ = hu._composite_outcome(_graded_trace(-1.0))
    assert outcome_neg == pytest.approx(-w_ext)


def test_external_grade_shifts_outcome_vs_ungraded():
    """A positive external grade must lift the learning outcome above the same
    turn with no grade; a negative grade must drop it below. Directional proof the
    channel reaches learning, independent of the exact weights."""
    from brain.hebbian import HebbianUpdater

    hu = HebbianUpdater(None)
    ungraded, ungraded_bd = hu._composite_outcome(_graded_trace(None))
    up, _ = hu._composite_outcome(_graded_trace(1.0))
    down, _ = hu._composite_outcome(_graded_trace(-1.0))

    assert "external" not in ungraded_bd  # legacy path carries no external term
    assert up > ungraded
    assert down < ungraded


# ── Engine path: a partner grade lands on the END-USER's bound chemistry ──────
def _engine_stub(persona="test"):
    """A _LoopsMixin-bound stub wired like an engine brain: a real Bus and a real
    per-customer ClientChemRegistry (in-memory store). api_grade_turn_engine must
    bind the customer's pair so the DA nudge lands there, not on the resting/process
    pair the reverted bus would resolve to out of band."""
    import types

    from brain.bus import Bus
    from brain.client_chem import ClientChemRegistry, InMemoryChemStore
    from brain.observability.timeline import TurnTrace
    from brain.session_loops import _LoopsMixin

    bus = Bus()
    registry = ClientChemRegistry(bus, InMemoryChemStore(), persona=persona)
    trace = TurnTrace(turn_id="t_eng_1", session_id="s", user_input="hi")
    stub = SimpleNamespace(bus=bus, _session_traces_full=[trace], _eval_logger=None)
    stub._client_chem_registry = lambda: registry
    # Path B pair factory (persona, end_user) → dedicated pair, cached like the real one.
    _persona_pairs: dict = {}
    stub._persona_pairs = _persona_pairs
    stub._persona_chem_pair = lambda p, e: _persona_pairs.setdefault(
        f"{p}:{e}", bus.new_chem_for(None, None)
    )
    stub.api_grade_turn = types.MethodType(_LoopsMixin.api_grade_turn, stub)
    return stub, registry, trace, bus


def test_engine_grade_nudges_bound_customer_not_resting_bus(_nudge_at_default):
    """The whole reason api_grade_turn_engine exists: an out-of-band grade must move
    THE GRADED CUSTOMER's dopamine, never the reverted process/resting pair and never
    another customer's mood."""
    from brain.session_loops import _LoopsMixin

    stub, registry, trace, bus = _engine_stub()
    nudge = _nudge_at_default

    pair_a = registry.get_or_create("cust-A")
    pair_b = registry.get_or_create("cust-B")  # a second, uninvolved customer
    resting = bus.resting_chem
    a_before = pair_a.neuromod.get("DA")
    b_before = pair_b.neuromod.get("DA")
    r_before = resting.neuromod.get("DA")

    result = _LoopsMixin.api_grade_turn_engine(stub, "t_eng_1", 1, end_user_id="cust-A")

    assert result["ok"] is True
    assert trace.external_grade == 1.0
    # The grade moved cust-A's mood, by the calibrated nudge.
    assert pair_a.neuromod.get("DA") - a_before == pytest.approx(nudge)
    # ...and left the other customer and the resting/process pair untouched.
    assert pair_b.neuromod.get("DA") == b_before
    assert resting.neuromod.get("DA") == r_before
    # Provenance is external, tallied on the customer's OWN bus (not the resting one).
    assert pair_a.neuromod.da_source_tally()["external"] == pytest.approx(nudge)
    assert resting.neuromod.da_source_tally()["external"] == 0.0


def test_engine_grade_with_no_end_user_falls_back_to_resting(_nudge_at_default):
    """Degenerate case (no end_user_id, e.g. an owner-scoped session): no client pair
    to bind, so it nudges the resting pair — same as the owner path. Must not error."""
    from brain.session_loops import _LoopsMixin

    stub, _registry, trace, bus = _engine_stub()
    resting = bus.resting_chem
    r_before = resting.neuromod.get("DA")

    result = _LoopsMixin.api_grade_turn_engine(stub, "t_eng_1", 1, end_user_id="")

    assert result["ok"] is True
    assert trace.external_grade == 1.0
    assert resting.neuromod.get("DA") - r_before == pytest.approx(_nudge_at_default)


# ── Round-2 hardening: isolation, binding provenance, idempotency, no-ghost-DA ─
def test_engine_grade_denies_turn_from_another_session(_nudge_at_default):
    """A1: the trace buffer is process-wide across partners. A caller who owns
    session A must NOT be able to grade a turn stamped with session B — the write
    is refused outright: no trace mutation, no DA anywhere."""
    from brain.session_loops import _LoopsMixin

    stub, registry, trace, bus = _engine_stub()
    trace.api_session_id = "sess_B"  # partner B's turn
    trace.end_user_id = "cust-B"
    pair_b = registry.get_or_create("cust-B")
    pair_a = registry.get_or_create("cust-A")
    resting = bus.resting_chem
    befores = [p.neuromod.get("DA") for p in (pair_a, pair_b, resting)]

    result = _LoopsMixin.api_grade_turn_engine(
        stub, "t_eng_1", 1, end_user_id="cust-A", api_session_id="sess_A"
    )

    assert result["ok"] is False
    assert result["denied"] is True
    assert result["applied_live"] is False
    assert trace.external_grade is None  # B's trace untouched — no learning re-weight
    afters = [p.neuromod.get("DA") for p in (pair_a, pair_b, resting)]
    assert afters == befores  # zero chemistry moved, on any pair


def test_engine_grade_same_session_is_allowed(_nudge_at_default):
    """The isolation check must not break the legitimate path: the session that
    ran the turn grades it and the stamped customer's DA moves."""
    from brain.session_loops import _LoopsMixin

    stub, registry, trace, bus = _engine_stub()
    trace.api_session_id = "sess_A"
    trace.end_user_id = "cust-A"
    pair_a = registry.get_or_create("cust-A")
    a_before = pair_a.neuromod.get("DA")

    result = _LoopsMixin.api_grade_turn_engine(
        stub, "t_eng_1", 1, end_user_id="cust-A", api_session_id="sess_A"
    )

    assert result["ok"] is True and result["applied_live"] is True
    assert trace.external_grade == 1.0
    assert pair_a.neuromod.get("DA") - a_before == pytest.approx(_nudge_at_default)


def test_engine_grade_binds_from_trace_stamps_not_caller_claim(_nudge_at_default):
    """A1 persona/end-user misattribution: the DA binding derives from the TRACE's
    own stamps (what the turn actually bound), so a stale or wrong caller-supplied
    end_user cannot steer the nudge onto a different customer's mood."""
    from brain.session_loops import _LoopsMixin

    stub, registry, trace, bus = _engine_stub()
    trace.api_session_id = "sess_A"
    trace.end_user_id = "cust-REAL"
    pair_real = registry.get_or_create("cust-REAL")
    pair_claimed = registry.get_or_create("cust-CLAIMED")
    real_before = pair_real.neuromod.get("DA")
    claimed_before = pair_claimed.neuromod.get("DA")

    result = _LoopsMixin.api_grade_turn_engine(
        stub, "t_eng_1", 1, end_user_id="cust-CLAIMED", api_session_id="sess_A"
    )

    assert result["ok"] is True
    assert pair_real.neuromod.get("DA") - real_before == pytest.approx(_nudge_at_default)
    assert pair_claimed.neuromod.get("DA") == claimed_before


def test_engine_grade_binds_trace_stamped_persona_pair(_nudge_at_default):
    """Path B: a trace stamped with a persona binds THAT persona's (persona,
    end_user) pair — the grade lands where the turn's mood lived."""
    from brain.session_loops import _LoopsMixin

    stub, registry, trace, bus = _engine_stub()
    trace.api_session_id = "sess_A"
    trace.api_persona = "the_visionary"
    trace.end_user_id = "cust-A"
    resting = bus.resting_chem
    r_before = resting.neuromod.get("DA")

    result = _LoopsMixin.api_grade_turn_engine(
        stub, "t_eng_1", 1, end_user_id="cust-A", api_session_id="sess_A"
    )

    assert result["ok"] is True
    pair = stub._persona_pairs["the_visionary:cust-A"]
    assert pair.neuromod.da_source_tally()["external"] == pytest.approx(_nudge_at_default)
    assert resting.neuromod.get("DA") == r_before


def test_repeated_identical_grades_move_da_once(_nudge_at_default):
    """A2: no dedup used to mean ~7 repeated grade:1 posts saturated DA at 1.0.
    Now chemistry moves at most once per turn_id — a repeat of the same grade is
    a recorded no-op for chemistry."""
    from brain.session_loops import _LoopsMixin

    stub, trace = _loops_stub()
    da_start = stub.bus.neuromod.get("DA")

    for _ in range(7):
        result = _LoopsMixin.api_grade_turn(stub, "t_ext_1", 1)
        assert result["ok"] is True

    assert trace.external_grade == 1.0
    # Exactly ONE nudge total, not seven.
    assert stub.bus.neuromod.get("DA") - da_start == pytest.approx(_nudge_at_default)
    assert stub.bus.neuromod.da_source_tally()["external"] == pytest.approx(_nudge_at_default)


def test_regrade_applies_bounded_difference_not_fresh_nudge(_nudge_at_default):
    """A2: changing a verdict (thumbs up → thumbs down) moves DA by the bounded
    difference, so the record follows the newest grade while any sequence of
    posts telescopes instead of accumulating."""
    from brain.session_loops import _LoopsMixin

    stub, trace = _loops_stub()
    nudge = _nudge_at_default
    da_start = stub.bus.neuromod.get("DA")

    _LoopsMixin.api_grade_turn(stub, "t_ext_1", 1)  # +nudge
    assert stub.bus.neuromod.get("DA") - da_start == pytest.approx(nudge)

    _LoopsMixin.api_grade_turn(stub, "t_ext_1", -1)  # difference −2, clamped to −nudge
    assert trace.external_grade == -1.0  # record follows the newest verdict
    assert stub.bus.neuromod.get("DA") - da_start == pytest.approx(0.0)

    # Flapping up/down forever stays bounded — never walks toward saturation.
    for _ in range(5):
        _LoopsMixin.api_grade_turn(stub, "t_ext_1", 1)
        _LoopsMixin.api_grade_turn(stub, "t_ext_1", -1)
    assert abs(stub.bus.neuromod.get("DA") - da_start) <= nudge + 1e-9


def test_unknown_turn_id_moves_zero_chemistry(_nudge_at_default):
    """A3: the nudge is gated on a successfully resolved live trace. A fabricated
    turn_id must be a pure no-op for chemistry — no free dopamine pump."""
    from brain.session_loops import _LoopsMixin

    stub, _trace = _loops_stub()
    da_before = stub.bus.neuromod.get("DA")

    result = _LoopsMixin.api_grade_turn(stub, "t_fabricated_999", 1)

    assert result["ok"] is True  # recorded for audit (eval log path)
    assert result["applied_live"] is False
    assert stub.bus.neuromod.get("DA") == da_before
    assert stub.bus.neuromod.da_source_tally()["external"] == 0.0


def test_late_grade_returns_turn_not_live_reason(_nudge_at_default):
    """A4: a grade landing after the turn left the live buffer (consolidation or
    restart) silently lost its learning half — now the response says so, so a
    partner's async grader can detect it missed the window. A live grade carries
    no such marker."""
    from brain.session_loops import _LoopsMixin

    stub, _trace = _loops_stub()

    late = _LoopsMixin.api_grade_turn(stub, "t_gone_after_sleep", 1)
    assert late["applied_live"] is False
    assert late["reason"] == "turn_not_live"

    live = _LoopsMixin.api_grade_turn(stub, "t_ext_1", 1)
    assert live["applied_live"] is True
    assert "reason" not in live


def test_engine_grade_unknown_turn_reports_not_live_and_no_da(_nudge_at_default):
    """A3+A4 through the engine path: unknown turn_id → not denied (it may simply
    have consolidated), but applied_live=false with the reason, and zero DA on
    every pair including resting."""
    from brain.session_loops import _LoopsMixin

    stub, registry, _trace, bus = _engine_stub()
    pair_a = registry.get_or_create("cust-A")
    resting = bus.resting_chem
    a_before = pair_a.neuromod.get("DA")
    r_before = resting.neuromod.get("DA")

    result = _LoopsMixin.api_grade_turn_engine(
        stub, "t_never_existed", 1, end_user_id="cust-A", api_session_id="sess_A"
    )

    assert result["ok"] is True
    assert result["applied_live"] is False
    assert result["reason"] == "turn_not_live"
    assert pair_a.neuromod.get("DA") == a_before
    assert resting.neuromod.get("DA") == r_before


def test_composite_weights_sum_to_one():
    """The graded mix is a convex blend — the four weights must sum to 1.0, or the
    outcome is no longer a normalized [-1, +1] signal."""
    from brain.settings import settings

    total = sum(
        float(settings.get(k, d))
        for k, d in (
            ("hebbian_w_da_ext", 0.4),
            ("hebbian_w_critic_ext", 0.2),
            ("hebbian_w_user_ext", 0.2),
            ("hebbian_w_external", 0.2),
        )
    )
    assert total == pytest.approx(1.0)
