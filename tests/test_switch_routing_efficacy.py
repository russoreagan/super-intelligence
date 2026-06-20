"""Switch-ordering Hebbian learning surface: bounded efficacy (consume) + credit (write).

Pure tests — no LLM. They prove the loop closes (a reinforced edge flips a
borderline input that wouldn't fire at baseline) and that the direction-aware
bands are the safety guarantee (a safety gate can't be learned past its allowed
direction regardless of the raw weight).
"""

from __future__ import annotations

import types

from brain.clusters.temporal import TemporalCluster
from brain.hebbian import HebbianUpdater
from brain.neuron import SwitchNeuron
from brain.wiring import Wiring
from brain.wiring_bootstrap import bootstrap


# ── consume: efficacy on the threshold (neuron) ──────────────────────────────
def test_efficacy_default_is_identity():
    s = SwitchNeuron("x", "temporal", threshold=0.5)
    assert s.effective_threshold(None) == 0.5
    assert s.effective_threshold(None, 1.0) == 0.5


def test_reinforced_edge_flips_a_borderline_input():
    """The loop-closer: an input below threshold doesn't fire at baseline but DOES
    once efficacy (a learned-strong route) lowers the threshold."""
    s = SwitchNeuron("self_reference", "temporal", threshold=0.5)
    assert s.should_fire(0.48, None) is False
    assert s.should_fire(0.48, None, efficacy=1.4) is True


def test_efficacy_respects_threshold_clamp():
    s = SwitchNeuron("x", "temporal", threshold=0.5)
    assert s.effective_threshold(None, 10.0) >= s.min_threshold  # 0.05 hard floor
    assert s.effective_threshold(None, 0.01) <= s.max_threshold  # 0.95 hard ceiling


# ── direction-aware bands (temporal._switch_efficacy) ────────────────────────
class _FakeWiring:
    def __init__(self, w):
        self._w = w

    def get_edge_weight(self, src, tgt):
        return self._w


def _eff(switch, weight, frozen=False):
    stub = types.SimpleNamespace(_wiring=_FakeWiring(weight), _wiring_frozen=frozen)
    return TemporalCluster._switch_efficacy(stub, switch)


def test_template_match_can_only_lower_readiness():
    # band [0.85, 1.0]: a high learned weight is CAPPED at 1.0 — learning can never
    # make the canned-response shortcut fire MORE eagerly (the "learn not to think" runaway).
    assert _eff("template_match", 3.0) == 1.0
    assert _eff("template_match", 0.5) == 0.85


def test_self_reference_can_only_raise_readiness():
    # band [1.0, 1.4]: a low learned weight is FLOORED at 1.0 — learning can never
    # suppress the safety block that forces understanding on self-referential input.
    assert _eff("self_reference", 0.1) == 1.0
    assert _eff("self_reference", 3.0) == 1.4


def test_exempt_switch_and_frozen_are_identity():
    assert _eff("length_bucket", 3.0) == 1.0  # no band → exempt
    assert _eff("self_reference", 3.0, frozen=True) == 1.0  # frozen → causal control intact


# ── write: credit (hebbian) ──────────────────────────────────────────────────
def _trace(fired):
    return types.SimpleNamespace(fired_path=fired, turn_id="t1")


def test_credit_reinforces_gated_switch_but_not_exempt():
    w = Wiring()
    bootstrap(w)  # ensures sensory.text→temporal.* edges exist
    upd = HebbianUpdater(w)
    fired = [
        {"name": "temporal.self_reference", "cluster": "temporal", "kind": "switch"},
        {"name": "temporal.length_bucket", "cluster": "temporal", "kind": "switch"},
    ]
    before_sr = w.get_edge_weight("sensory.text", "temporal.self_reference")
    before_lb = w.get_edge_weight("sensory.text", "temporal.length_bucket")
    n = upd._apply_switch_routing_credit(
        _trace(fired), outcome=0.6, plasticity=1.0, turn_plast=1.0, gainers=[], losers=[]
    )
    assert n == 1  # only the gated switch credited
    assert w.get_edge_weight("sensory.text", "temporal.self_reference") > before_sr
    assert w.get_edge_weight("sensory.text", "temporal.length_bucket") == before_lb  # exempt


def test_credit_sign_follows_outcome():
    w = Wiring()
    bootstrap(w)
    upd = HebbianUpdater(w)
    fired = [{"name": "temporal.epistemic_action", "cluster": "temporal", "kind": "switch"}]
    before = w.get_edge_weight("sensory.text", "temporal.epistemic_action")
    upd._apply_switch_routing_credit(
        _trace(fired), outcome=-0.6, plasticity=1.0, turn_plast=1.0, gainers=[], losers=[]
    )
    assert (
        w.get_edge_weight("sensory.text", "temporal.epistemic_action") < before
    )  # negative → weakens
