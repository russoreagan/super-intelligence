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

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class SwitchNeuron:
    name: str
    cluster: str
    polarity: str = "excitatory"   # "excitatory" | "inhibitory"
    threshold: float = 0.5
    weight: float = 1.0            # Hebbian edge weight (persisted via wiring.py)
    modulators: dict[str, float] = field(default_factory=dict)
    min_threshold: float = 0.05
    max_threshold: float = 0.95

    # internal state
    _last_fired: float = field(default=0.0, init=False, repr=False)
    _fire_count: int = field(default=0, init=False, repr=False)
    _last_suppressed_at: float = field(default=0.0, init=False, repr=False)

    def effective_threshold(self, snapshot: dict[str, float] | None) -> float:
        """Threshold shifted by the current neuromod+hormonal snapshot.

        The total shift is multiplied by `settings.modulation_gain` so the
        whole modulation system can be dialed up/down/off from one knob.
        Imported lazily to keep neuron.py free of settings coupling at import
        time (tests instantiate switches without booting settings)."""
        if not self.modulators or snapshot is None:
            return self.threshold
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
        if eff < self.min_threshold:
            return self.min_threshold
        if eff > self.max_threshold:
            return self.max_threshold
        return eff

    def modulation_delta(self, snapshot: dict[str, float] | None) -> float:
        """Effective − base. Useful for telemetry."""
        return self.effective_threshold(snapshot) - self.threshold

    def fire(self, level: float, tag: str, evidence: dict | None = None,
             snapshot: dict[str, float] | None = None) -> dict:
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
            record_switch_fire(self.name, self.cluster, level, tag, self.polarity,
                               eff_threshold=eff_thr, mod_delta=mod_delta)
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

    def should_fire(self, input_level: float,
                    snapshot: dict[str, float] | None = None,
                    turn_id: str = "") -> bool:
        """Did the input clear the (chemistry-shifted) threshold?

        When modulation suppresses a fire that would otherwise have happened
        (level >= base threshold but < effective threshold), a
        `switch_suppressed_by_modulation` decision is emitted so silent
        suppressions are visible in the decisions log.
        """
        eff_thr = self.effective_threshold(snapshot)
        if input_level >= eff_thr:
            return True
        # Near-miss: would have fired under neutral chemistry.
        if snapshot is not None and self.modulators and input_level >= self.threshold:
            try:
                from brain.observability.decisions import decisions
                # Only record the channels this switch actually listens to.
                chem_relevant = {c: round(float(snapshot.get(c, 0.5)), 3)
                                 for c in self.modulators}
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
    """
    A SwitchNeuron that holds persistent scalar state with exponential decay.
    Used for neuromod updates, ring buffers, running counters.
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


def make_threshold_gate(name: str, cluster: str, fn: Callable[..., tuple[bool, float, str, dict]],
                        polarity: str = "excitatory") -> Callable:
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
