"""Tests for per-persona RISK POSTURE — loss aversion (λ) and uncertainty aversion (κ).

These are the asymmetry axes that are INDEPENDENT of the symmetric reward-source weights:
reward_weight scales a gain and its matching loss together; λ scales only the loss on top of
that; κ adds dread from outcome variance regardless of sign. Covers:

  - neuron.loss_aversion / uncertainty_aversion: per-persona lookup, identity fallback, the
    pinned Stoic control, display-name/slug equivalence, and the per-deployment settings dials.
  - Independence from reward_weight: a persona can value a reward source weakly yet fear losses
    the most (the_poet), or feed strongly on a source yet barely fear loss (the_visionary).
  - prediction_reward folds λ into the WRONG branch only — gains are never λ-scaled.
  - DMN conclusion penalty scales the verified-wrong sting by λ (the Sage's λ=1.1 stings deeper
    than the Stoic control's λ=1.0 at equal correctness valuation), while the affirm reward does
    not move at all under a huge λ.
"""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.neuron import loss_aversion, reward_weight, uncertainty_aversion
from brain.settings import settings

_PANEL = ("The Poet", "The Analyst", "The Visionary", "The Empath", "The Sage", "The Cynic")


@pytest.fixture(autouse=True)
def _restore_settings():
    keys = ("persona_name", "loss_aversion_scale", "uncertainty_aversion_scale")
    prev = {k: settings._data.get(k) for k in keys}
    yield
    for k, v in prev.items():
        if v is None:
            settings._data.pop(k, None)
        else:
            settings._data[k] = v


# ── loss_aversion / uncertainty_aversion lookups ────────────────────────────────


def test_loss_aversion_known_personas():
    assert loss_aversion("The Poet") == pytest.approx(2.4)  # Tortured Artist: losses loom largest
    assert loss_aversion("The Analyst") == pytest.approx(2.0)
    assert loss_aversion("The Visionary") == pytest.approx(0.6)  # reckless: underweights downside
    assert loss_aversion("The Sage") == pytest.approx(1.1)  # even-keeled
    # The Poet feels losses harder than anyone else on the panel.
    assert loss_aversion("The Poet") == max(loss_aversion(p) for p in _PANEL)


def test_uncertainty_aversion_known_personas():
    assert uncertainty_aversion("The Analyst") == pytest.approx(1.25)  # craves certainty most
    assert uncertainty_aversion("The Visionary") == pytest.approx(0.05)
    # The Analyst is the most uncertainty-averse of the panel.
    assert uncertainty_aversion("The Analyst") == max(uncertainty_aversion(p) for p in _PANEL)


def test_risk_posture_identity_fallback():
    assert loss_aversion("Nobody") == 1.0  # unknown → symmetric (no loss aversion)
    assert loss_aversion("") == 1.0
    assert uncertainty_aversion("Nobody") == 0.0  # unknown → risk-neutral
    # the_stoic is the pinned experimental control: symmetric + risk-neutral on purpose.
    assert loss_aversion("The Stoic") == 1.0
    assert uncertainty_aversion("The Stoic") == 0.0


def test_risk_posture_accepts_display_name_or_slug():
    assert loss_aversion("The Poet") == loss_aversion("the_poet")
    assert uncertainty_aversion("The Analyst") == uncertainty_aversion("the_analyst")


def test_loss_aversion_independent_of_reward_weight():
    """The whole point: loss sensitivity is a SEPARATE axis from reward sensitivity.
    The Poet draws only modest correctness reward (<1.0) yet fears losses the most (highest λ);
    the Visionary feeds strongly on novelty (>1.0) yet is the least loss-averse (λ<1)."""
    assert reward_weight("The Poet", "correctness") < 1.0 < loss_aversion("The Poet")
    assert reward_weight("The Visionary", "novelty") > 1.0
    assert loss_aversion("The Visionary") < 1.0


def test_settings_dials_scale_and_clamp():
    settings._data["persona_name"] = "ignored"
    # The per-deployment dial multiplies the innate baseline, then bounds clamp.
    settings._data["loss_aversion_scale"] = 2.0
    assert loss_aversion("The Analyst") == pytest.approx(3.0)  # 2.0×2.0=4.0 → λ ceiling 3.0
    settings._data["loss_aversion_scale"] = 0.1
    assert loss_aversion("The Analyst") == pytest.approx(0.5)  # 2.0×0.1=0.2 → λ floor 0.5
    settings._data["uncertainty_aversion_scale"] = 10.0
    assert uncertainty_aversion("The Analyst") == pytest.approx(1.5)  # → κ ceiling


def test_prediction_reward_folds_loss_aversion_into_misses_only():
    from brain.neuron import prediction_reward

    conf, info = 0.9, 0.9
    # A correct prediction (a gain) is identical regardless of persona — never λ-scaled.
    settings._data["persona_name"] = "The Poet"
    gain_poet = prediction_reward(conf, True, info)
    settings._data["persona_name"] = "The Stoic"
    gain_stoic = prediction_reward(conf, True, info)
    assert gain_poet > 0
    assert gain_poet == pytest.approx(gain_stoic)

    # A confident MISS stings in proportion to λ.
    settings._data["persona_name"] = "The Poet"  # λ = 2.4
    miss_poet = prediction_reward(conf, False, info)
    settings._data["persona_name"] = "The Stoic"  # λ = 1.0
    miss_stoic = prediction_reward(conf, False, info)
    assert miss_poet < miss_stoic < 0
    assert miss_poet == pytest.approx(miss_stoic * 2.4)


def test_prediction_reward_uses_bound_persona_lambda_not_home():
    """Agent-lane turns and rotated DMN ticks run under bind_persona(); the miss sting
    must scale by the BOUND persona's λ, not the process home's (the residual site from
    the 2026-07 chemistry audit — every other reward site already resolves the active
    persona)."""
    from brain.neuron import prediction_reward
    from brain.second_brain.store import bind_persona

    conf, info = 0.9, 0.9
    settings._data["persona_name"] = "The Poet"  # home λ = 2.4
    home_miss = prediction_reward(conf, False, info)
    with bind_persona("The Visionary"):  # bound λ = 0.6
        bound_miss = prediction_reward(conf, False, info)
        bound_gain = prediction_reward(conf, True, info)
    assert home_miss == pytest.approx(-conf * info * 2.4)
    assert bound_miss == pytest.approx(home_miss / 2.4 * 0.6)  # agent-lane λ, not home
    assert bound_gain == pytest.approx(conf * info)  # gains never λ-scaled


# ── DMN: λ scales the verified-wrong sting, one-sidedly ──────────────────────────


class _RecordingNeuromod:
    def __init__(self):
        self.deltas: dict[str, float] = {}

    def add(self, channel, delta, source="intrinsic", **attribution):
        self.deltas[channel] = self.deltas.get(channel, 0.0) + delta


def _make_dmn_with_bus(persona: str):
    import brain.open_threads as ot
    from brain.dmn import DefaultModeNetwork

    settings._data["persona_name"] = persona
    dmn = DefaultModeNetwork.__new__(DefaultModeNetwork)
    dmn._router = MagicMock()
    dmn._router.embed = AsyncMock(return_value=None)
    hip = MagicMock()
    hip.encode_conclusion = AsyncMock()
    dmn._hippocampus = hip
    dmn._session_id = "test"
    dmn._open_threads = []
    dmn._recent_conclusions = deque(maxlen=5)
    dmn._save_threads = AsyncMock()
    nm = _RecordingNeuromod()
    dmn._bus = MagicMock()
    dmn._bus.neuromod = nm
    return dmn, nm, ot


@pytest.mark.asyncio
async def test_reject_penalty_scales_with_loss_aversion():
    """Equal correctness valuation (Sage & Stoic both reward_weight 1.0), but the Sage's λ=1.1
    makes the verified-wrong sting deeper than the Stoic control's λ=1.0 — isolating loss
    aversion from reward sensitivity."""
    assert (
        reward_weight("The Sage", "correctness") == reward_weight("The Stoic", "correctness") == 1.0
    )

    dmn_s, nm_s, ot = _make_dmn_with_bus("The Sage")
    dmn_s._open_threads, ts = ot.open_thread([], "q?")
    dmn_s._open_threads, ts = ot.mark_pending(dmn_s._open_threads, ts.id)
    await dmn_s._resolve_pending_conclusion(ts, "reject", "no")

    dmn_c, nm_c, ot = _make_dmn_with_bus("The Stoic")
    dmn_c._open_threads, tc = ot.open_thread([], "q?")
    dmn_c._open_threads, tc = ot.mark_pending(dmn_c._open_threads, tc.id)
    await dmn_c._resolve_pending_conclusion(tc, "reject", "no")

    assert nm_s.deltas["DA"] < nm_c.deltas["DA"] < 0  # Sage stings deeper
    assert nm_s.deltas["DA"] == pytest.approx(nm_c.deltas["DA"] * 1.1)
    assert nm_s.deltas["5HT"] == pytest.approx(nm_c.deltas["5HT"] * 1.1)


@pytest.mark.asyncio
async def test_affirm_reward_unaffected_by_loss_aversion():
    """λ is one-sided: the Poet's huge λ (2.4) must NOT change the affirm (gain) reward."""
    dmn_p, nm_p, ot = _make_dmn_with_bus("The Poet")
    dmn_p._open_threads, tp = ot.open_thread([], "q?")
    dmn_p._open_threads, tp = ot.mark_pending(dmn_p._open_threads, tp.id)
    tp.pending_conclusion = "c"
    await dmn_p._resolve_pending_conclusion(tp, "affirm", "yes")

    expected = (
        float(settings.get("correctness_reward_base"))
        * reward_weight("The Poet", "correctness")
        * float(settings.get("emotional_reactivity_scale"))
    )
    assert nm_p.deltas["DA"] == pytest.approx(expected)  # gain path: no λ
