"""
Integration test for gating shadow-validation in the frontal executive.

Exercises the real _run_executive() gated-skip path with a stubbed executive LLM:
when the gate fires AND shadow sampling is on, the integrator runs purely for
measurement — but the gated prediction still drives behavior (zero behavior
change), a shadow outcome is recorded, and the true label is fed back into
predictor history (self-correction).
"""

from __future__ import annotations

import asyncio

from brain.clusters.frontal import FrontalCluster
from brain.observability.timeline import TurnTrace
from brain.predictor import CompositePredictor
from brain.settings import settings

DRAFT_SCORE = 0.9


class _StubExecutive:
    """Stands in for the executive IntegratorCell. Records call count and
    returns a fixed instruction JSON differing from the gated prediction."""

    def __init__(self, response_json: str):
        self._json = response_json
        self.calls = 0

    def reset_turn(self, turn_id):  # noqa: D401
        pass

    async def call(self, messages):
        self.calls += 1
        return self._json


def _frontal_with_gate(stub, trace):
    f = FrontalCluster.__new__(FrontalCluster)
    f._exec_predictor = CompositePredictor(name="exec", cluster="frontal")
    f._executive = stub
    # Avoid the full context-builder plumbing.
    f._build_exec_context = lambda *a, **k: ""  # type: ignore[method-assign]
    f._record_trace_bypass = lambda: trace  # type: ignore[method-assign]
    return f


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _StubSynapse:
    """Stands in for the plastic synapses the drafter path fires along the way."""

    def should_fire(self, *a, **k):
        return False

    def fire(self, *a, **k):
        pass


class _StubBrainstem:
    def __init__(self):
        self.endorsed = []

    def add_draft(self, draft_id, text, score):
        pass

    def endorse(self, draft_id):
        self.endorsed.append(draft_id)

    def veto(self, draft_id):
        pass


def _frontal_with_drafters(stub, trace):
    """Extend the gate harness with the collaborators _run_drafters_and_select needs.

    Everything here is a stand-in EXCEPT the two predictors, which are real — the
    point of these tests is that a real turn drives real predictor state.
    """
    f = _frontal_with_gate(stub, trace)
    f._critic_predictor = CompositePredictor(name="critic", cluster="frontal")
    f._brainstem = _StubBrainstem()
    f._critic = _StubExecutive("{}")
    f._bus = None  # untouched while colony_features is off
    f._drafters = [None, None]
    f.last_turn_draft_scores = []  # normally reset by _run_engine at turn start
    for name in (
        "_arousal_modulator",
        "_drafter_count_selector",
        "_epistemic_mode",
        "_self_ref_mode",
        "_response_type_router",
        "_length_budget",
        "_tone_selector",
        "_low_DA_inhibits_planner",
        "_planner_trigger",
        "_template_fallback",
    ):
        setattr(f, name, _StubSynapse())

    f._build_drafter_prompt = lambda *a, **k: "prompt"
    f._build_cached_context = lambda *a, **k: ""
    f._select_drafters = lambda count, turn_id: list(range(count))
    f._downshift_indices = lambda idxs, turn_id: set()
    f._select_explore_drafters = lambda idxs, turn_id: set()

    async def _run_drafter(i, *a, **k):
        return f"draft{i}", f"draft text {i}"

    async def _score_draft(text, drafter_prompt, turn_id):
        return {
            "overall": DRAFT_SCORE,
            "coherence": DRAFT_SCORE,
            "relevance": DRAFT_SCORE,
            "tone_fit": DRAFT_SCORE,
            "craft": DRAFT_SCORE,
            "veto": False,
        }

    judge_calls: list[tuple] = []

    async def _judge_shadow_and_record(*a, **k):
        judge_calls.append((a, k))

    f._run_drafter = _run_drafter
    f._score_draft = _score_draft
    f._judge_shadow_and_record = _judge_shadow_and_record
    f.judge_producer_calls = judge_calls
    return f


# Neutral emotion keeps run_empathy False; DA at the configured baseline keeps
# critic_force False, so nothing forces the critic outside the paths under test.
_NM = {"DA": 0.62, "GABA": 0.0, "Glu": 0.8}
_CHEM = {"DA": 0.62, "NE": 0.1}
_FEATURES = {
    "intent": "chitchat",
    "register": "casual",
    "requires_memory": False,
    "user_emotion": "neutral",
}
_AFFECT = {"emotion": "neutral", "neuromod": {"DA": 0.62, "GABA": 0.0}}
_EXEC_SIG = ("chitchat", "casual", False, "mid", "low")
_INSTRUCTION = {
    "response_type": "chitchat",
    "target_length": "brief",
    "tone": "warm",
    "key_points": [],
    "drafter_count": 2,
}


def _drive_drafters(f, turn_id):
    """Run one real drafter+critic pass, returning the committed text."""
    return _run(
        f._run_drafters_and_select(
            _NM, _CHEM, _EXEC_SIG, _INSTRUCTION, _FEATURES, _AFFECT, {}, "", turn_id
        )
    )


def test_completed_turn_populates_exec_outcome():
    """A turn that actually ran the critic must feed the EXECUTIVE predictor's
    outcome history, not just the critic predictor's.

    Regression guard: _exec_predictor.record_outcome() had no production caller, so
    avg_recent_outcome() returned None forever and the executive skip in
    _run_executive was unreachable dead code. Unlike the tests above, nothing here
    seeds record_outcome by hand — the score has to come from the turn itself.
    """
    settings.update({"colony_features": 0})
    try:
        trace = TurnTrace(turn_id="t3", session_id="s1", user_input="hi")
        f = _frontal_with_drafters(_StubExecutive("{}"), trace)

        assert f._exec_predictor.avg_recent_outcome(_EXEC_SIG) is None  # nothing yet

        text = _drive_drafters(f, "t3")

        assert text.startswith("draft text")  # a draft really was committed
        assert f._exec_predictor.avg_recent_outcome(_EXEC_SIG) == DRAFT_SCORE
        # The critic predictor keeps its own signature-keyed history, unaffected.
        assert (
            f._critic_predictor.avg_recent_outcome(_EXEC_SIG + ("chitchat", "warm")) == DRAFT_SCORE
        )
    finally:
        settings.update({"colony_features": 1})


def test_exec_gate_fires_from_naturally_recorded_outcomes():
    """End-to-end: one real turn is enough to arm the executive skip on a repeat
    signature, and the second turn skips the sonnet integrator entirely.

    This is the assertion that fails without the record_outcome wiring — exec_avg
    stays None, the gate condition is False, and the executive LLM runs every turn.
    """
    settings.update({"colony_features": 0, "gating_shadow_sample_rate": 0.0})
    try:
        trace = TurnTrace(turn_id="t4", session_id="s1", user_input="hi")
        stub = _StubExecutive(
            '{"response_type": "chitchat", "target_length": "brief", "tone": "warm"}'
        )
        f = _frontal_with_drafters(stub, trace)

        # Turn 1: no history → the integrator runs and records its label.
        _run(f._run_executive(_NM, _CHEM, _EXEC_SIG, _FEATURES, _AFFECT, {}, "", "t4a"))
        assert stub.calls == 1
        assert trace.llm_calls_saved == 0  # nothing skipped yet
        # ...and the drafter/critic pass records the quality outcome for that signature.
        _drive_drafters(f, "t4a")

        # Turn 2: same signature, now with both a confident label AND a quality
        # score above the floor → the gate fires and the integrator is skipped.
        instruction = _run(
            f._run_executive(_NM, _CHEM, _EXEC_SIG, _FEATURES, _AFFECT, {}, "", "t4b")
        )

        assert stub.calls == 1  # no second executive LLM call
        assert trace.llm_calls_saved == 1
        assert instruction["response_type"] == "chitchat"
        assert instruction["target_length"] == "brief"
        assert instruction["tone"] == "warm"
    finally:
        settings.update({"colony_features": 1, "gating_shadow_sample_rate": 0.30})


def test_exec_gate_quality_floor_is_a_kill_switch():
    """Raising exec_gate_quality_floor above any achievable score disables the gate."""
    settings.update(
        {"colony_features": 0, "gating_shadow_sample_rate": 0.0, "exec_gate_quality_floor": 1.1}
    )
    try:
        trace = TurnTrace(turn_id="t5", session_id="s1", user_input="hi")
        stub = _StubExecutive(
            '{"response_type": "chitchat", "target_length": "brief", "tone": "warm"}'
        )
        f = _frontal_with_drafters(stub, trace)

        _run(f._run_executive(_NM, _CHEM, _EXEC_SIG, _FEATURES, _AFFECT, {}, "", "t5a"))
        _drive_drafters(f, "t5a")
        # Outcome quality is recorded and would clear the 0.7 default...
        assert f._exec_predictor.avg_recent_outcome(_EXEC_SIG) == DRAFT_SCORE

        _run(f._run_executive(_NM, _CHEM, _EXEC_SIG, _FEATURES, _AFFECT, {}, "", "t5b"))

        # ...but the raised floor keeps the integrator awake.
        assert stub.calls == 2
        assert trace.llm_calls_saved == 0
    finally:
        settings.update(
            {
                "colony_features": 1,
                "gating_shadow_sample_rate": 0.30,
                "exec_gate_quality_floor": 0.7,
            }
        )


def test_scored_turn_feeds_the_critic_judge_producer():
    """A scored turn must hand the judge-attachment producer the CRITIC's claim.

    Regression guard: JUDGE_HOSTS lists frontal.critic, but the only producer call
    site was empathy-gated (run_empathy + empathy_score is not None), so on the
    critic's own scored path no prediction was ever recorded — the judge could
    carry a clamp yet never accumulate the evidence needed to earn an attachment.
    """
    settings.update({"colony_features": 0, "gating_shadow_sample_rate": 0.0})
    try:
        trace = TurnTrace(turn_id="t6", session_id="s1", user_input="hi")
        f = _frontal_with_drafters(_StubExecutive("{}"), trace)

        _drive_drafters(f, "t6")

        hosts = [c[0][0] for c in f.judge_producer_calls]
        assert hosts == ["frontal.critic"]  # neutral turn: the empathy check never ran
        args, kwargs = f.judge_producer_calls[0]
        assert args[4] == {"overall": DRAFT_SCORE, "veto": False}
        assert args[5] == "overall"
        # The drafter prompt rides along so the critic's shadow arm can rebuild the
        # exact scoring prompt the live critic saw.
        assert kwargs["context"] == "prompt"
    finally:
        settings.update({"colony_features": 1, "gating_shadow_sample_rate": 0.30})


def test_emotional_turn_feeds_both_judge_producers_with_their_own_claims():
    """With the empathy check live, BOTH judges record — and each is handed its OWN
    claim: the empathy critic its raw empathy_score (byte-identical to the old
    behavior), the critic its pre-blend overall. Grading the 0.7/0.3 blend would
    hold each judge accountable for the other judge's read."""
    settings.update({"colony_features": 0, "gating_shadow_sample_rate": 0.0})
    try:
        trace = TurnTrace(turn_id="t7", session_id="s1", user_input="hi")
        f = _frontal_with_drafters(_StubExecutive("{}"), trace)

        async def _run_empathy_check(text, user_emotion, turn_id):
            return {"empathy_score": 0.8, "veto": False}

        f._run_empathy_check = _run_empathy_check
        features = dict(_FEATURES, user_emotion="sad")
        _run(
            f._run_drafters_and_select(
                _NM, _CHEM, _EXEC_SIG, _INSTRUCTION, features, _AFFECT, {}, "", "t7"
            )
        )

        by_host = {c[0][0]: c for c in f.judge_producer_calls}
        assert set(by_host) == {"frontal.empathy_critic", "frontal.critic"}
        emp_args, _emp_kwargs = by_host["frontal.empathy_critic"]
        assert emp_args[4] == {"empathy_score": 0.8, "veto": False}
        assert emp_args[5] == "empathy_score"
        crit_args, _crit_kwargs = by_host["frontal.critic"]
        # The critic's own 0.9 — NOT the blended overall (0.9*0.7 + 0.8*0.3 = 0.87).
        assert crit_args[4] == {"overall": DRAFT_SCORE, "veto": False}
    finally:
        settings.update({"colony_features": 1, "gating_shadow_sample_rate": 0.30})


def test_single_draft_turn_records_no_judge_prediction():
    """The single-draft path carries a hardcoded 0.8 and critic_ran=False — no
    judge made a claim, so the producer must stay silent. A judge graded on a
    score it never emitted would poison the accuracy signal."""
    settings.update({"colony_features": 0, "gating_shadow_sample_rate": 0.0})
    try:
        trace = TurnTrace(turn_id="t8", session_id="s1", user_input="hi")
        f = _frontal_with_drafters(_StubExecutive("{}"), trace)
        instruction = dict(_INSTRUCTION, drafter_count=1)
        _run(
            f._run_drafters_and_select(
                _NM, _CHEM, _EXEC_SIG, instruction, _FEATURES, _AFFECT, {}, "", "t8"
            )
        )
        assert f.judge_producer_calls == []
        assert f.last_turn_draft_scores[0]["critic_ran"] is False
    finally:
        settings.update({"colony_features": 1, "gating_shadow_sample_rate": 0.30})


def test_gated_skip_with_shadow_validation():
    settings.update({"gating_shadow_sample_rate": 1.0})  # force shadow every gated skip
    try:
        trace = TurnTrace(turn_id="t1", session_id="s1", user_input="hi")
        # Stubbed executive returns a DIFFERENT tuple than the gated prediction,
        # so we can assert behavior used the prediction (not the shadow result).
        stub = _StubExecutive(
            '{"response_type": "explainer", "target_length": "long", "tone": "formal"}'
        )
        f = _frontal_with_gate(stub, trace)

        features = {"intent": "chitchat", "register": "casual", "requires_memory": False}
        affect = {"emotion": "neutral", "neuromod": {"DA": 0.5, "GABA": 0.0}}
        exec_sig = ("chitchat", "casual", False, "mid", "low")

        # Build history so the gate fires confidently (conf 1.0 >= 0.70 skip
        # threshold) AND recent outcome quality clears the avg_recent_outcome > 0.7
        # bar the gate also requires before skipping the integrator.
        gated_label = ("chitchat", "brief", "warm")
        for _ in range(3):
            f._exec_predictor.record(exec_sig, gated_label)
            f._exec_predictor.record_outcome(exec_sig, 0.9)

        instruction = _run(
            f._run_executive({"DA": 0.5, "GABA": 0.0}, {}, exec_sig, features, affect, {}, "", "t1")
        )

        # 1. Behavior used the GATED prediction, not the shadow LLM output.
        assert instruction["response_type"] == "chitchat"
        assert instruction["target_length"] == "brief"
        assert instruction["tone"] == "warm"

        # 2. The shadow run actually invoked the executive exactly once (measurement).
        assert stub.calls == 1

        # 3. A shadow outcome was recorded with actual populated and correct set.
        shadow_rows = [o for o in trace.predictor_outcomes if o.get("shadow")]
        assert len(shadow_rows) == 1
        row = shadow_rows[0]
        assert row["integrator_woken"] is False
        assert row["actual"] == ["explainer", "long", "formal"]
        assert row["correct"] is False  # prediction != shadow actual
        assert row["match_frac"] == 0.0

        # 4. Self-correction: the true label was fed back into predictor history.
        assert (exec_sig, ("explainer", "long", "formal")) in list(f._exec_predictor._history)
    finally:
        settings.update({"gating_shadow_sample_rate": 0.30})  # restore default


def test_gated_skip_without_shadow_does_not_run_llm():
    settings.update({"gating_shadow_sample_rate": 0.0})  # shadow off
    try:
        trace = TurnTrace(turn_id="t2", session_id="s1", user_input="hi")
        stub = _StubExecutive('{"response_type": "explainer"}')
        f = _frontal_with_gate(stub, trace)

        features = {"intent": "chitchat", "register": "casual", "requires_memory": False}
        affect = {"emotion": "neutral", "neuromod": {"DA": 0.5, "GABA": 0.0}}
        exec_sig = ("chitchat", "casual", False, "mid", "low")
        for _ in range(3):
            f._exec_predictor.record(exec_sig, ("chitchat", "brief", "warm"))
            f._exec_predictor.record_outcome(exec_sig, 0.9)

        instruction = _run(
            f._run_executive({"DA": 0.5, "GABA": 0.0}, {}, exec_sig, features, affect, {}, "", "t2")
        )

        assert instruction["target_length"] == "brief"  # gated prediction used
        assert stub.calls == 0  # no LLM call at all
        assert trace.llm_calls_saved == 1
        assert not [o for o in trace.predictor_outcomes if o.get("shadow")]
    finally:
        settings.update({"gating_shadow_sample_rate": 0.30})
