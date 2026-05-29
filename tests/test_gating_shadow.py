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

        # Build history so the gate fires confidently (conf 1.0 >= 0.70 skip threshold).
        gated_label = ("chitchat", "brief", "warm")
        for _ in range(3):
            f._exec_predictor.record(exec_sig, gated_label)

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
        settings.update({"gating_shadow_sample_rate": 0.15})  # restore default


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

        instruction = _run(
            f._run_executive({"DA": 0.5, "GABA": 0.0}, {}, exec_sig, features, affect, {}, "", "t2")
        )

        assert instruction["target_length"] == "brief"  # gated prediction used
        assert stub.calls == 0  # no LLM call at all
        assert trace.llm_calls_saved == 1
        assert not [o for o in trace.predictor_outcomes if o.get("shadow")]
    finally:
        settings.update({"gating_shadow_sample_rate": 0.15})
