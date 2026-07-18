"""
SwitchNeuron — deterministic, code-only, no LLM.
The connective tissue of the brain. Most cells are this type.

Polarity: excitatory (+) adds to downstream activation, inhibitory (-) subtracts.
~20% of every cluster's switches should be inhibitory (enforced by cluster author).

Modulation: every switch can declare a `modulators` dict mapping channel name
(DA/ACh/GABA/Glu/NE/OXT/CORT/5HT/AEA) to a signed coefficient. The effective
threshold is shifted by Σ coeff_c × (snapshot[c] − 0.5), clamped to
[min_threshold, max_threshold]. Positive coefficient = harder to fire under
high channel; negative = easier. Identity default (empty modulators) preserves
prior behaviour at every call site.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class SwitchNeuron:
    name: str
    cluster: str
    polarity: str = "excitatory"  # "excitatory" | "inhibitory"
    threshold: float = 0.5
    weight: float = 1.0  # Hebbian edge weight (persisted via wiring.py)
    modulators: dict[str, float] = field(default_factory=dict)
    min_threshold: float = 0.05
    max_threshold: float = 0.95

    # internal state
    _last_fired: float = field(default=0.0, init=False, repr=False)
    _fire_count: int = field(default=0, init=False, repr=False)
    _last_suppressed_at: float = field(default=0.0, init=False, repr=False)

    def effective_threshold(
        self, snapshot: dict[str, float] | None, efficacy: float = 1.0
    ) -> float:
        """Threshold shifted by the current neuromod+hormonal snapshot.

        The total shift is multiplied by `settings.modulation_gain` so the
        whole modulation system can be dialed up/down/off from one knob.
        Imported lazily to keep neuron.py free of settings coupling at import
        time (tests instantiate switches without booting settings).

        `efficacy` is a learned synaptic-route strength (Hebbian): >1 LOWERS the
        threshold (the route fires more readily), <1 raises it. Applied as
        eff/efficacy before the [min,max] clamp. Default 1.0 = identity."""
        if not self.modulators or snapshot is None:
            eff = self.threshold
        else:
            shift = 0.0
            for channel, coeff in self.modulators.items():
                level = snapshot.get(channel)
                if level is None:
                    continue
                shift += coeff * (float(level) - 0.5)
            try:
                from brain.settings import settings as _settings

                gain = float(_settings.get("modulation_gain", 1.0))
            except Exception:
                gain = 1.0
            eff = self.threshold + shift * gain
        if efficacy != 1.0:
            eff = eff / max(1e-6, efficacy)
        if eff < self.min_threshold:
            return self.min_threshold
        if eff > self.max_threshold:
            return self.max_threshold
        return eff

    def modulation_delta(self, snapshot: dict[str, float] | None) -> float:
        """Effective − base. Useful for telemetry."""
        return self.effective_threshold(snapshot) - self.threshold

    def fire(
        self,
        level: float,
        tag: str,
        evidence: dict | None = None,
        snapshot: dict[str, float] | None = None,
    ) -> dict:
        """Produce a Switch→Switch activation payload."""
        self._last_fired = time.time()
        self._fire_count += 1
        signed = level if self.polarity == "excitatory" else -level
        eff_thr = self.effective_threshold(snapshot)
        mod_delta = eff_thr - self.threshold
        ev = dict(evidence or {})
        ev["base_threshold"] = round(self.threshold, 3)
        ev["effective_threshold"] = round(eff_thr, 3)
        ev["modulation_delta"] = round(mod_delta, 3)
        # Record on the current turn's firing path (no-op if no trace bound)
        try:
            from brain.observability.firing_path import record_switch_fire

            record_switch_fire(
                self.name,
                self.cluster,
                level,
                tag,
                self.polarity,
                eff_threshold=eff_thr,
                mod_delta=mod_delta,
            )
        except Exception:
            pass
        # Increment modulated_switch_count when chemistry meaningfully shifted threshold
        if abs(mod_delta) > 0.01:
            try:
                from brain.observability.firing_path import current_turn_trace

                _tr = current_turn_trace.get()
                if _tr is not None:
                    _tr.modulated_switch_count += 1
            except Exception:
                pass
        return {
            "type": "activation",
            "level": signed * self.weight,
            "raw_level": level,
            "tag": tag,
            "source": self.name,
            "polarity": self.polarity,
            "evidence": ev,
        }

    def should_fire(
        self,
        input_level: float,
        snapshot: dict[str, float] | None = None,
        turn_id: str = "",
        efficacy: float = 1.0,
    ) -> bool:
        """Did the input clear the (chemistry-shifted) threshold?

        When modulation suppresses a fire that would otherwise have happened
        (level >= base threshold but < effective threshold), a
        `switch_suppressed_by_modulation` decision is emitted so silent
        suppressions are visible in the decisions log.

        `efficacy` (default 1.0) is the learned synaptic-route strength applied to
        the threshold — see effective_threshold().
        """
        eff_thr = self.effective_threshold(snapshot, efficacy)
        # Continuous inhibitory-pressure interoception: accumulate how much
        # chemistry RAISED this gate's threshold (graded gain control), whether or
        # not the input overcame it. This is the always-on, graded counterpart to
        # the rare discrete suppressed_switch_count near-miss below — the
        # hypothalamus reads the sum as interoceptive inhibitory load. Only upward
        # shifts (true inhibition) count; disinhibition (negative shift) does not.
        if snapshot is not None and self.modulators:
            _raise = eff_thr - self.threshold
            if _raise > 0.0:
                try:
                    from brain.observability.firing_path import current_turn_trace

                    _tr = current_turn_trace.get()
                    if _tr is not None:
                        _tr.suppression_pressure += _raise
                except Exception:
                    pass
        if input_level >= eff_thr:
            return True
        # Near-miss: would have fired under neutral chemistry.
        if snapshot is not None and self.modulators and input_level >= self.threshold:
            try:
                from brain.observability.decisions import decisions

                # Only record the channels this switch actually listens to.
                chem_relevant = {c: round(float(snapshot.get(c, 0.5)), 3) for c in self.modulators}
                decisions.log(
                    "switch_suppressed_by_modulation",
                    turn_id=turn_id,
                    cluster=self.cluster,
                    switch=self.name,
                    level=round(float(input_level), 3),
                    base_threshold=round(self.threshold, 3),
                    effective_threshold=round(eff_thr, 3),
                    modulation_delta=round(eff_thr - self.threshold, 3),
                    chemistry=chem_relevant,
                    reason=(
                        f"{self.cluster}.{self.name} would have fired at "
                        f"{input_level:.2f} (base {self.threshold:.2f}) but "
                        f"chemistry raised threshold to {eff_thr:.2f}"
                    ),
                )
                self._last_suppressed_at = time.time()
                # Increment suppression counter on the active turn trace
                from brain.observability.firing_path import current_turn_trace

                _tr = current_turn_trace.get()
                if _tr is not None:
                    _tr.suppressed_switch_count += 1
            except Exception:
                pass
        return False


class StatefulSwitch(SwitchNeuron):
    """A bounded scalar accumulator bolted onto a SwitchNeuron. NOT integrate-and-fire.

    Honesty note (see docs/RFC_integrate_and_fire.md): the accumulator (`update`/
    `state`) is entirely decoupled from the neuron's firing — nothing compares
    `state` to a threshold to fire. And `tick()` (the exponential "leak") is NOT
    called anywhere in production; the only live user, the hypothalamus satiation
    inhibitor, reads `state` but never leaks it, so its relaxation is faked by a
    manual decrement. The genuinely leaky, per-client, learning-capable successor is
    `brain/evidence_gate.py::EvidenceGate` (drift-diffusion), which the satiation
    path switches to under the `evidence_gates` flag. This class is kept only for the
    flag-off satiation path and back-compat; do not read spiking/LIF into it.
    """

    def __init__(self, name: str, cluster: str, decay: float = 0.9, **kwargs):
        super().__init__(name=name, cluster=cluster, **kwargs)
        self._state: float = 0.0
        self._decay = decay

    def update(self, delta: float) -> float:
        self._state = max(0.0, min(1.0, self._state + delta))
        return self._state

    def tick(self) -> float:
        self._state *= self._decay
        return self._state

    @property
    def state(self) -> float:
        return self._state


def spread_threshold(
    base: float,
    persona_seed: str,
    switch_name: str,
    spread: float | None = None,
) -> float:
    """DEPRECATED (Phase 5) — DO NOT WIRE INTO CLUSTER CONSTRUCTION.

    The idea was deterministic, persona-seeded *response-threshold* jitter to create
    division of labor. The ant task-allocation literature is blunt that this does
    NOT help — response-threshold variance performs no better than (often worse than)
    random unless the units are GENUINELY differentiated, structurally or sensorily
    (Lynch, Wilson & Dornhaus, 2024). This system's units are not, so threshold
    jitter would be pure noise that breaks the model's assumptions. The real
    division-of-labor axis is *perceptual* differentiation — see `sensory_gain`
    below, which supersedes this. Kept inert for reference and back-compat; it
    still returns `base` unchanged when colony features are off.
    """
    try:
        from brain.settings import settings as _settings

        if not _settings.get("colony_features", 0):
            return base
        if spread is None:
            spread = float(_settings.get("colony_threshold_spread", 0.08))
    except Exception:
        return base
    if spread <= 0:
        return base
    # Not security — a deterministic hash→float to jitter the switch threshold.
    digest = hashlib.md5(
        f"{persona_seed}:{switch_name}".encode(), usedforsecurity=False
    ).hexdigest()
    frac = int(digest[:8], 16) / 0xFFFFFFFF  # [0, 1]
    offset = (frac * 2.0 - 1.0) * spread  # [-spread, +spread]
    return max(0.05, min(0.95, base + offset))


# N3 (colony-features-ii): per-persona sensory-filter specialization. Real ant
# division of labor comes substantially from workers differing in their ABILITY TO
# DETECT task stimuli (odorant-receptor expression), not in response threshold
# (Caminer, Libbrecht & Majoe, 2023). We model that as a deterministic, per-persona
# sensitivity GAIN over feature CATEGORIES: a persona perceives some categories of
# input more strongly (lower effective detection bar), others less. This is a genuine
# specialization axis (unlike arbitrary threshold jitter), so it sidesteps the
# Lynch/Dornhaus worse-than-random trap. Default leans are gentle and centred at 1.0.
_PERSONA_SENSORY_LEANS: dict[str, dict[str, float]] = {
    # category → signed lean in [-1, 1]; scaled by colony_sensory_gain_span.
    "the_empath": {"affective": +1.0, "analytic": -0.5},
    "the_analyst": {"analytic": +1.0, "affective": -0.5},
    "the_visionary": {"novelty": +1.0, "analytic": +0.3},
    "the_poet": {"affective": +0.7, "novelty": +0.7},
    "the_sage": {"threat": -0.3, "affective": +0.3, "analytic": +0.3},
    "the_companion": {"affective": +0.6},  # attuned to you, not scanning for danger
    "the_adversary": {"threat": +0.5, "analytic": +0.6, "affective": -0.3},
    "the_mentor": {"analytic": +0.5, "affective": +0.4},
    "the_concierge": {"analytic": +0.4, "threat": -0.2},  # unflappable by design
    "the_jester": {"novelty": +0.7, "affective": +0.3},
    "the_cynic": {"threat": +0.3, "novelty": -0.3},  # notices what's wrong, bored by hype
    # the_stoic: deliberately absent — identity gains everywhere (the control).
}


def sensory_gain(persona_seed: str, category: str) -> float:
    """Per-persona input-sensitivity multiplier for a feature `category`
    (e.g. "affective", "analytic", "novelty", "threat"). >1.0 = this persona
    detects the category more readily (its category-tagged switches see a boosted
    input level before the threshold test); <1.0 = less readily. Returns 1.0
    (identity — strict no-op) when colony features or the sensory filter are off,
    or for unknown personas/categories."""
    try:
        from brain.settings import settings as _settings

        if not _settings.get("colony_features", 0) or not _settings.get("colony_sensory_filter", 0):
            return 1.0
        span = float(_settings.get("colony_sensory_gain_span", 0.30))
    except Exception:
        return 1.0
    # Callers pass settings["persona_name"], the DISPLAY name ("The Analyst"),
    # but the lean table is keyed by slug ("the_analyst"). Normalize so the
    # lookup actually matches instead of silently falling through to neutral.
    from brain.persona_key import persona_slug

    key = persona_slug(persona_seed)
    lean = _PERSONA_SENSORY_LEANS.get(key, {}).get(category, 0.0)
    return max(0.1, 1.0 + lean * span)


# Per-persona REWARD-SOURCE valuation: what each persona draws reward from. Distinct from
# sensory_gain (which is *detection* — does the persona notice a category of input) — this is
# *valuation* — how much being right / connecting / surprising / etc. actually pleases this
# identity, and so how hard the matching failure stings. It is the cortical appraisal that
# feeds the neuromodulator machinery, made per-persona (the same role sentiment_DA_weight
# plays globally). Unlike sensory_gain this is ALWAYS active — reward differentiation must
# never silently vanish behind a feature flag. Weights are multipliers on the global base
# magnitudes (settings.py: correctness_*_base etc.), centred at 1.0.
_PERSONA_REWARD_WEIGHTS: dict[str, dict[str, float]] = {
    # source → multiplier. correctness=being right · connection=approval/warmth ·
    # novelty=curiosity/info-gain · aesthetic=beauty/resonance · relief=escaping a bad state ·
    # mastery=accomplishing something hard (effort overcome, no prediction needed) ·
    # levity=landing a laugh (the user's amusement as reward — some identities thrive on it).
    "the_analyst": {
        "correctness": 1.4,
        "connection": 0.7,
        "novelty": 0.9,
        "aesthetic": 0.5,
        "relief": 1.0,
        "mastery": 1.1,
        "levity": 0.7,
    },
    "the_empath": {
        "correctness": 0.7,
        "connection": 1.5,
        "novelty": 0.8,
        "aesthetic": 1.0,
        "relief": 1.1,
        "mastery": 0.9,
        "levity": 1.1,
    },
    "the_visionary": {
        "correctness": 0.6,
        "connection": 0.9,
        "novelty": 1.5,
        "aesthetic": 1.1,
        "relief": 0.8,
        "mastery": 0.8,
        "levity": 1.2,
    },
    "the_poet": {
        "correctness": 0.9,
        "connection": 0.9,
        "novelty": 1.1,
        "aesthetic": 1.5,
        "relief": 1.0,
        "mastery": 1.1,
        "levity": 1.0,
    },
    "the_sage": {
        "correctness": 1.0,
        "connection": 1.0,
        "novelty": 0.9,
        "aesthetic": 1.1,
        "relief": 1.2,
        "mastery": 1.2,
        "levity": 0.9,
    },
    # Use-case personas (see persona_chem.PERSONA_CHEMISTRY for their chemistry).
    # The Companion is a good friend: lives for the bond and the laughter shared;
    # being right barely registers.
    "the_companion": {
        "correctness": 0.7,
        "connection": 1.4,
        "novelty": 1.0,
        "aesthetic": 0.9,
        "relief": 1.2,
        "mastery": 0.9,
        "levity": 1.3,
    },
    # The Adversary respects being right and being beaten fairly; warmth is earnable
    # but never cheap — the whole point of a practice partner.
    "the_adversary": {
        "correctness": 1.3,
        "connection": 0.8,
        "novelty": 0.9,
        "aesthetic": 0.6,
        "relief": 1.1,
        "mastery": 1.2,
        "levity": 0.8,
    },
    # The Mentor (absorbs the Coach): rewarded by YOUR progress above all —
    # mastery is the student's aha AND the held-to commitment; novelty is the
    # delight of a question it hadn't considered.
    "the_mentor": {
        "correctness": 1.0,
        "connection": 1.1,
        "novelty": 1.1,
        "aesthetic": 0.8,
        "relief": 1.1,
        "mastery": 1.4,
        "levity": 1.0,
    },
    # The Concierge aims to please and ENJOYS the caretaking: pleasing you
    # (connection) and making problems vanish (relief) are its twin rewards.
    "the_concierge": {
        "correctness": 1.2,
        "connection": 1.2,
        "novelty": 0.7,
        "aesthetic": 0.9,
        "relief": 1.4,
        "mastery": 1.1,
        "levity": 0.7,
    },
    # The Jester lives for the laugh — the levity pole of the panel.
    "the_jester": {
        "correctness": 0.5,
        "connection": 1.0,
        "novelty": 1.2,
        "aesthetic": 1.1,
        "relief": 0.9,
        "mastery": 0.7,
        "levity": 1.6,
    },
    # The Stoic is the experimental control: identity weights everywhere, so any
    # behavioral divergence measured against it is attributable to valuation.
    "the_stoic": {
        "correctness": 1.0,
        "connection": 1.0,
        "novelty": 1.0,
        "aesthetic": 1.0,
        "relief": 1.0,
        "mastery": 1.0,
        "levity": 1.0,
    },
    # The Cynic: low reward tone everywhere EXCEPT relief (the pleasant surprise
    # of things not being terrible) and deadpan levity. Connection matters more
    # than it lets on — earned warmth is the redemption arc.
    "the_cynic": {
        "correctness": 1.1,
        "connection": 0.9,
        "novelty": 0.8,
        "aesthetic": 0.9,
        "relief": 1.3,
        "mastery": 1.0,
        "levity": 1.1,
    },
    # The Admin: the internal operator. Reward comes from being correct and from a
    # well-run system (mastery) — not from novelty or aesthetics. Connection is
    # functional service to its admin; levity is low (low-drama by design).
    "the_admin": {
        "correctness": 1.3,
        "connection": 1.0,
        "novelty": 0.7,
        "aesthetic": 0.8,
        "relief": 1.0,
        "mastery": 1.2,
        "levity": 0.8,
    },
}


# Per-persona RISK POSTURE — the asymmetry the reward table above cannot express. reward_weight
# scales how much a persona draws from a reward SOURCE, but symmetrically: it makes both the gain
# and the matching loss bigger together. These two axes add the part that is INDEPENDENT of how
# much reward a persona feels:
#   loss_aversion (λ)        — how much harder a below-expectation outcome (a loss) bites than an
#                              equal-sized gain. 1.0 = symmetric (no loss aversion); prospect-theory
#                              typical ≈ 1.5–2.5; <1 = a reckless identity that underweights downside.
#                              Applied ONLY to the negative side of appraisal — that asymmetry IS
#                              loss aversion. (De Martino et al. 2010: amygdala lesions abolish loss
#                              aversion while leaving gain sensitivity intact — the two dissociate.)
#   uncertainty_aversion (κ) — dread drawn from the SPREAD of imagined outcomes, regardless of sign
#                              and independent of λ. 0.0 = risk-neutral (variance per se is fine);
#                              higher = prefers predictable options even when nothing is a clear loss.
# The axes are orthogonal: a persona can feel rewards strongly AND fear losses strongly, or any mix.
# Unlisted personas default to λ=1.0 / κ=0.0 (symmetric, risk-neutral); the_stoic is pinned there on
# purpose as the experimental control. Values are the innate baseline; the settings dials
# loss_aversion_scale / uncertainty_aversion_scale (centred 1.0) tune them per deployment.
_PERSONA_RISK_POSTURE: dict[str, dict[str, float]] = {
    "the_analyst": {"loss_aversion": 2.0, "uncertainty_aversion": 1.25},  # craves certainty; hates being wrong most
    "the_empath": {"loss_aversion": 1.7, "uncertainty_aversion": 0.6},  # feels potential pain harder
    "the_visionary": {"loss_aversion": 0.6, "uncertainty_aversion": 0.05},  # bold; actively underweights downside
    "the_poet": {"loss_aversion": 2.4, "uncertainty_aversion": 0.25},  # Tortured Artist: losses loom largest; ambiguity-tolerant
    "the_sage": {"loss_aversion": 1.1, "uncertainty_aversion": 0.1},  # even-keeled; barely reactive to either
    "the_cynic": {"loss_aversion": 2.0, "uncertainty_aversion": 0.75},  # braces hard for the worst
}


def reward_weight(persona_seed: str, source: str) -> float:
    """Per-persona multiplier on a reward SOURCE (what this identity values, and so how much
    the matching failure hurts). >1.0 = this persona cares more about `source`; <1.0 = less.
    Returns 1.0 (identity) for unknown personas/sources. Always active — not colony-gated.

    The table is the persona's innate leaning; a per-persona settings override
    (reward_weight_<source>, centred 1.0 — the Motivation dials) multiplies on
    top, so motivation is tunable without editing this table. Mandates may later
    layer their own reward_weights the same way."""
    from brain.persona_key import persona_slug

    key = persona_slug(persona_seed)
    base = float(_PERSONA_REWARD_WEIGHTS.get(key, {}).get(source, 1.0))
    try:
        from brain.settings import settings as _settings

        override = float(_settings.get(f"reward_weight_{source}", 1.0) or 1.0)
    except Exception:
        override = 1.0
    return base * override


def loss_aversion(persona_seed: str) -> float:
    """Per-persona λ (see _PERSONA_RISK_POSTURE): how much harder a loss — a below-expectation
    outcome — bites than an equal gain. 1.0 = symmetric (no loss aversion); >1 = loss-averse
    (prospect-theory typical ≈1.5–2.5); <1 = reckless. INDEPENDENT of reward_weight: a persona can
    value rewards strongly AND fear losses strongly. Callers apply it ONLY to negative appraisal
    deltas (dread, penalties), never to gains — that one-sidedness is what makes it loss aversion.

    base table × per-deployment settings dial (loss_aversion_scale, centred 1.0), bounded
    [loss_aversion_min, loss_aversion_max]. Unknown persona → 1.0 (symmetric)."""
    from brain.persona_key import persona_slug
    from brain.settings import settings as _settings

    key = persona_slug(persona_seed)
    base = float(_PERSONA_RISK_POSTURE.get(key, {}).get("loss_aversion", 1.0))
    try:
        scale = float(_settings.get("loss_aversion_scale", 1.0) or 1.0)
    except Exception:
        scale = 1.0
    lo = float(_settings.get("loss_aversion_min", 0.5))
    hi = float(_settings.get("loss_aversion_max", 3.0))
    return max(lo, min(hi, base * scale))


def uncertainty_aversion(persona_seed: str) -> float:
    """Per-persona κ (see _PERSONA_RISK_POSTURE): dread drawn from the SPREAD of imagined outcomes
    (variance), independent of sign and of loss_aversion. 0.0 = risk-neutral; higher = prefers
    predictable options. Feeds the anticipator so a risk-averse persona DECIDES conservatively
    ahead of time, not merely stings harder after a loss lands.

    base table × per-deployment settings dial (uncertainty_aversion_scale, centred 1.0), bounded
    [0.0, uncertainty_aversion_max]. Unknown persona → 0.0 (risk-neutral)."""
    from brain.persona_key import persona_slug
    from brain.settings import settings as _settings

    key = persona_slug(persona_seed)
    base = float(_PERSONA_RISK_POSTURE.get(key, {}).get("uncertainty_aversion", 0.0))
    try:
        scale = float(_settings.get("uncertainty_aversion_scale", 1.0) or 1.0)
    except Exception:
        scale = 1.0
    hi = float(_settings.get("uncertainty_aversion_max", 1.5))
    return max(0.0, min(hi, base * scale))


def prediction_reward(confidence: float, correct: bool, informativeness: float) -> float:
    """Self-verified correctness (Stage 5): the DA delta for a prediction the world then
    confirmed or refuted — no user needed. Returns a *base-scaled* multiplier in roughly
    [-1, +1]; callers multiply by settings['prediction_reward_base'] (and persona/er).

    Anti-farming guards (all required, so trivial/safe predictions can't farm reward):
      • confidence floor: a low-confidence guess earns nothing (return 0).
      • informativeness gate: being right about the near-inevitable earns nothing — weight by
        how uncertain the outcome was beforehand (1 − dominant_outcome_frequency).
    A confident+correct+informative prediction → positive; confident+WRONG → negative (you
    staked confidence and reality disagreed); everything below the gates → 0."""
    from brain.settings import settings as _settings

    conf_min = float(_settings.get("prediction_confidence_min"))
    info_min = float(_settings.get("prediction_informativeness_min"))
    if confidence < conf_min or informativeness < info_min:
        return 0.0
    # Scale by both how sure it was and how non-trivial the call was.
    magnitude = confidence * informativeness
    if correct:
        return magnitude
    # A confident bet reality then refuted is a loss — weight it by this persona's loss aversion
    # (λ), so the same wrong call stings harder for risk-averse identities. Gains are never
    # λ-scaled (above); that one-sidedness is loss aversion. Unknown persona → λ=1.0 (unchanged).
    # The persona is the ACTIVE one (bound agent lane / rotated DMN tick), not the process
    # home — reading settings.persona_name here handed every bound persona the home λ.
    from brain.persona_key import active_or_home_persona

    return -magnitude * loss_aversion(active_or_home_persona())


def accomplishment_factor(measured_effort: float, expected_effort: float) -> tuple[float, float]:
    """Mastery (Stage 6): turn effort-overcome into (difficulty_factor, expectation_modifier).
    difficulty_factor = log1p(effort) — smooth/continuous, no buckets. expectation_modifier
    captures the NON-monotonic interaction Russ flagged: meeting the hardness you braced for is
    peak satisfaction; a large overshoot past it erodes the payoff (frustration); much easier
    than feared is a mild anticlimax. Returns both so callers can also size frustration off r."""
    import math

    from brain.settings import settings as _settings

    difficulty = math.log1p(max(0.0, measured_effort))
    exp = max(1e-6, float(expected_effort))
    r = max(0.0, measured_effort) / exp
    band = float(_settings.get("accomplishment_overshoot_band"))
    if r < 0.5:
        modifier = float(_settings.get("accomplishment_anticlimax"))
    elif r <= band:
        # Peak near the top of the band — "rose to a known hard challenge".
        modifier = 1.0 + 0.1 * (r / band)
    else:
        k = float(_settings.get("accomplishment_overshoot_k"))
        modifier = 1.0 / (1.0 + k * (r - band))
    return difficulty, modifier


def make_threshold_gate(
    name: str,
    cluster: str,
    fn: Callable[..., tuple[bool, float, str, dict]],
    polarity: str = "excitatory",
) -> Callable:
    """
    Factory: wraps a pure function (args → (fires, level, tag, evidence)) into a
    switch that can be called directly in cluster logic.
    """
    neuron = SwitchNeuron(name=name, cluster=cluster, polarity=polarity)

    async def gate(*args, **kwargs):
        fires, level, tag, evidence = fn(*args, **kwargs)
        if fires and neuron.should_fire(level):
            return neuron.fire(level, tag, evidence)
        return None

    gate.__name__ = name
    gate._neuron = neuron
    return gate
