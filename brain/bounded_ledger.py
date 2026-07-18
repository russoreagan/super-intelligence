"""bounded_ledger — the shared math under the brain's per-persona/entity ledgers.

Several systems keep a small keyed ledger that must never grow without bound and
must be able to forget without user action: the DMN's open-threads ledger
(brain/open_threads.py), the per-persona ignition tally (brain/ignition_tally.py),
the avoidance gate's per-entity evidence slices (brain/avoidance_gate.py), and the
parietal entity tracker (brain/clusters/parietal.py). Each needs some mix of the
same three guarantees — exponential half-life decay, wall-clock age-out, and a
size cap with deterministic stalest-first eviction — and each used to hand-roll
them. This module is the one place that math lives: pure functions, no I/O, no
state, so every substrate keeps its own storage and serialization.
"""

from __future__ import annotations

from typing import Callable, Iterable, TypeVar

T = TypeVar("T")


def decay(value: float, last_ts: float | None, now: float, half_life_s: float) -> float:
    """`value` exponentially leaked from `last_ts` to `now` (half-life in seconds).
    No decay when the half-life is non-positive or the timestamp is missing."""
    if half_life_s <= 0 or last_ts is None:
        return value
    return value * (0.5 ** (max(0.0, now - float(last_ts)) / float(half_life_s)))


def aged_out(ts: float, now: float, max_age_s: float) -> bool:
    """True when the wall-clock age of `ts` has reached the age-out horizon."""
    return (now - float(ts)) >= float(max_age_s)


def cap_evict(
    items: Iterable[T],
    cap: int,
    *,
    staleness: Callable[[T], object],
    evictable: Callable[[T], bool] | None = None,
) -> list[T]:
    """The victims to remove so the ledger shrinks back to `cap`: the stalest
    evictable items first (stable on ties). Never selects a non-evictable item,
    even if that leaves the ledger over cap. Pure — the caller does the deleting."""
    pool = list(items)
    excess = len(pool) - int(cap)
    if excess <= 0:
        return []
    candidates = sorted(
        (x for x in pool if evictable is None or evictable(x)), key=staleness
    )
    return candidates[:excess]
