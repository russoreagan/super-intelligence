"""Weighted drafter sampling: learned edge weights shape the selected mix.

_weighted_sample uses only its args + the settings temperature (not instance
state), so we can call it unbound with self=None.
"""

from __future__ import annotations

import random
from collections import Counter

from brain.clusters.frontal import FrontalCluster

_sample = FrontalCluster._weighted_sample


def test_picks_requested_count_distinct():
    random.seed(0)
    picked = _sample(None, [0, 1, 2, 3, 4], [1.0, 1.2, 0.8, 1.0, 0.9], 3)
    assert len(picked) == 3
    assert len(set(picked)) == 3
    assert all(0 <= i < 5 for i in picked)


def test_count_clamped_to_pool():
    picked = _sample(None, [0, 1], [1.0, 1.0], 5)
    assert sorted(picked) == [0, 1]


def test_higher_weight_selected_more_often():
    random.seed(1)
    weights = [1.0, 1.5, 0.7]  # index 1 strongly favored, 2 disfavored
    c = Counter()
    for _ in range(1500):
        for i in _sample(None, [0, 1, 2], weights, 1):
            c[i] += 1
    assert c[1] > c[0] > c[2]


def test_learned_ranking_flips_the_mix():
    """The whole point: a weight difference (B>A vs A>B) changes selection frequency
    even at count that would saturate a hard top-N."""
    random.seed(2)

    def freq(weights, count, n=1500):
        c = Counter()
        for _ in range(n):
            for i in _sample(None, [0, 1, 2, 3, 4], weights, count):
                c[i] += 1
        return {i: c[i] / n for i in range(5)}

    warm = [1.08, 1.17, 0.84, 1.0, 1.0]  # B>A
    ana = [1.15, 1.09, 0.84, 1.0, 1.0]  # A>B
    fw, fa = freq(warm, 3), freq(ana, 3)
    assert fw[1] > fa[1]  # warm picks drafter_B (idx1) more than analytical does
    assert fa[0] > fw[0]  # analytical picks drafter_A (idx0) more than warm does
