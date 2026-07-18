"""Unit tests for the shared bounded-ledger primitive (brain/bounded_ledger.py) —
the one place cap / wall-clock age-out / half-life decay math lives for
open_threads, ignition_tally, the avoidance slices, and the parietal entity map.
"""

from __future__ import annotations

import pytest

from brain.bounded_ledger import aged_out, cap_evict, decay


# ── decay ──────────────────────────────────────────────────────────────────────


def test_decay_halves_per_half_life():
    assert decay(4.0, 0.0, 100.0, 100.0) == pytest.approx(2.0)
    assert decay(4.0, 0.0, 200.0, 100.0) == pytest.approx(1.0)


def test_decay_no_time_elapsed_or_clock_skew():
    assert decay(3.0, 50.0, 50.0, 100.0) == pytest.approx(3.0)
    # a timestamp from the future never amplifies (clamped to zero elapsed)
    assert decay(3.0, 500.0, 50.0, 100.0) == pytest.approx(3.0)


def test_decay_disabled_without_half_life_or_timestamp():
    assert decay(3.0, 0.0, 1e9, 0.0) == 3.0
    assert decay(3.0, 0.0, 1e9, -1.0) == 3.0
    assert decay(3.0, None, 1e9, 100.0) == 3.0


# ── aged_out ───────────────────────────────────────────────────────────────────


def test_aged_out_at_and_past_horizon():
    assert not aged_out(0.0, 99.0, 100.0)
    assert aged_out(0.0, 100.0, 100.0)  # inclusive at the horizon
    assert aged_out(0.0, 101.0, 100.0)


# ── cap_evict ──────────────────────────────────────────────────────────────────


def test_cap_evict_under_cap_is_noop():
    assert cap_evict([("a", 1), ("b", 2)], 2, staleness=lambda kv: kv[1]) == []


def test_cap_evict_removes_stalest_first_down_to_cap():
    items = [("new", 9), ("old", 1), ("mid", 5), ("older", 0)]
    victims = cap_evict(items, 2, staleness=lambda kv: kv[1])
    assert victims == [("older", 0), ("old", 1)]


def test_cap_evict_respects_evictable_even_if_still_over_cap():
    items = [("pinned", 0), ("also-pinned", 1), ("free", 2)]
    victims = cap_evict(
        items, 1, staleness=lambda kv: kv[1], evictable=lambda kv: kv[0] == "free"
    )
    assert victims == [("free", 2)]  # never selects a non-evictable item


def test_cap_evict_stable_on_ties():
    items = [("first", 1), ("second", 1), ("third", 1)]
    assert cap_evict(items, 2, staleness=lambda kv: kv[1]) == [("first", 1)]
