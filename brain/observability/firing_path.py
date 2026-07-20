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

current_turn_trace: contextvars.ContextVar[TurnTrace | None] = contextvars.ContextVar(
    "current_turn_trace", default=None
)


def set_current_trace(trace: TurnTrace | None) -> contextvars.Token:
    """Bind the trace for the current async context. Returns a token to reset()."""
    return current_turn_trace.set(trace)


def reset_current_trace(token: contextvars.Token) -> None:
    current_turn_trace.reset(token)


def get_current_trace():
    """Return the TurnTrace bound to the current async context, or None.
    Lets in-cluster code (e.g. frontal drafter-prompt assembly) record
    instrumentation flags without threading the trace through call args."""
    return current_turn_trace.get()


def record_switch_fire(
    name: str,
    cluster: str,
    level: float,
    tag: str,
    polarity: str = "excitatory",
    eff_threshold: float | None = None,
    mod_delta: float | None = None,
) -> None:
    """Called from SwitchNeuron.fire(). No-op when no trace is bound."""
    trace = current_turn_trace.get()
    if trace is None:
        return
    with contextlib.suppress(Exception):
        entry: dict = {
            "name": f"{cluster}.{name}",
            "cluster": cluster,
            "kind": "switch",
            "level": round(float(level), 3),
            "tag": tag,
            "polarity": polarity,
            "ts": time.time(),
        }
        if eff_threshold is not None:
            entry["effective_threshold"] = round(float(eff_threshold), 3)
        if mod_delta is not None and abs(mod_delta) > 1e-6:
            entry["modulation_delta"] = round(float(mod_delta), 3)
        trace.fired_path.append(entry)


def record_node_active(name: str, level: float = 1.0) -> None:
    """Record that a NON-FIRING node participated this turn. No-op without a trace.

    `name` is the canonical `cluster.node` name from the wiring graph. `level` is
    how much it participated, in [0,1] — NOT merely whether it ran. The distinction
    is load-bearing twice over: a constant 1.0 on every turn carries no information
    for the sleep pass to learn from, and for the recall strategies specifically a
    "it ran" level would close a positive loop (a strategy that returns nothing gains
    weight → gets more budget → runs more → gains more).

    Repeat records for one node keep the MAX: participating strongly once is the
    honest summary of the turn.
    """
    trace = current_turn_trace.get()
    if trace is None:
        return
    with contextlib.suppress(Exception):
        lvl = max(0.0, min(1.0, float(level)))
        prior = trace.coactive.get(name)
        if prior is None or lvl > prior:
            trace.coactive[name] = lvl


def record_integrator_call(name: str, cluster: str) -> None:
    """Called from IntegratorCell.call() before LLM dispatch."""
    trace = current_turn_trace.get()
    if trace is None:
        return
    with contextlib.suppress(Exception):
        trace.fired_path.append(
            {
                "name": f"{cluster}.{name}",
                "cluster": cluster,
                "kind": "integrator",
                "level": 1.0,
                "tag": "call",
                "polarity": "excitatory",
                "ts": time.time(),
            }
        )
