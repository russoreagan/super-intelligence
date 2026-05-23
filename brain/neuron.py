"""
SwitchNeuron — deterministic, code-only, no LLM.
The connective tissue of the brain. Most cells are this type.

Polarity: excitatory (+) adds to downstream activation, inhibitory (-) subtracts.
~20% of every cluster's switches should be inhibitory (enforced by cluster author).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class SwitchNeuron:
    name: str
    cluster: str
    polarity: str = "excitatory"   # "excitatory" | "inhibitory"
    threshold: float = 0.5
    weight: float = 1.0            # Hebbian edge weight (persisted via wiring.py)

    # internal state
    _last_fired: float = field(default=0.0, init=False, repr=False)
    _fire_count: int = field(default=0, init=False, repr=False)

    def fire(self, level: float, tag: str, evidence: dict | None = None) -> dict:
        """Produce a Switch→Switch activation payload."""
        self._last_fired = time.time()
        self._fire_count += 1
        signed = level if self.polarity == "excitatory" else -level
        # Record on the current turn's firing path (no-op if no trace bound)
        try:
            from brain.observability.firing_path import record_switch_fire
            record_switch_fire(self.name, self.cluster, level, tag, self.polarity)
        except Exception:
            pass
        return {
            "type": "activation",
            "level": signed * self.weight,
            "raw_level": level,
            "tag": tag,
            "source": self.name,
            "polarity": self.polarity,
            "evidence": evidence or {},
        }

    def should_fire(self, input_level: float) -> bool:
        return input_level >= self.threshold


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
