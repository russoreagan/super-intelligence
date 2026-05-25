"""
Firing-path context var. Set per-turn from run.py; switches and integrators
append entries as they fire. Used by sleep consolidation to apply Hebbian
updates along the path the turn actually traversed.
"""
from __future__ import annotations

import contextlib
import contextvars
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain.observability.timeline import TurnTrace

current_turn_trace: contextvars.ContextVar[TurnTrace | None] = (
    contextvars.ContextVar("current_turn_trace", default=None)
)


def set_current_trace(trace: TurnTrace | None) -> contextvars.Token:
    """Bind the trace for the current async context. Returns a token to reset()."""
    return current_turn_trace.set(trace)


def reset_current_trace(token: contextvars.Token) -> None:
    current_turn_trace.reset(token)


def record_switch_fire(name: str, cluster: str, level: float, tag: str,
                        polarity: str = "excitatory") -> None:
    """Called from SwitchNeuron.fire(). No-op when no trace is bound."""
    trace = current_turn_trace.get()
    if trace is None:
        return
    with contextlib.suppress(Exception):
        trace.fired_path.append({
            "name": f"{cluster}.{name}",
            "cluster": cluster,
            "kind": "switch",
            "level": round(float(level), 3),
            "tag": tag,
            "polarity": polarity,
            "ts": time.time(),
        })


def record_integrator_call(name: str, cluster: str) -> None:
    """Called from IntegratorCell.call() before LLM dispatch."""
    trace = current_turn_trace.get()
    if trace is None:
        return
    with contextlib.suppress(Exception):
        trace.fired_path.append({
            "name": f"{cluster}.{name}",
            "cluster": cluster,
            "kind": "integrator",
            "level": 1.0,
            "tag": "call",
            "polarity": "excitatory",
            "ts": time.time(),
        })
