"""Pure-stats helpers for the Hebbian divergence experiment (no LLM, no I/O)."""

from __future__ import annotations

from eval.wiring_divergence_ab import (
    _divergence,
    _jaccard,
    _kendall_tau,
    _permutation_p,
    _tv_distance,
)


def test_kendall_tau_bounds():
    assert _kendall_tau(["a", "b", "c"], ["a", "b", "c"]) == 0.0
    assert _kendall_tau(["a", "b", "c"], ["c", "b", "a"]) == 1.0
    assert abs(_kendall_tau(["a", "b", "c", "d"], ["a", "c", "b", "d"]) - 1 / 6) < 1e-9
    assert _kendall_tau(["a"], ["a"]) is None  # <2 common → undefined


def test_jaccard():
    assert _jaccard(["a", "b"], ["a", "b"]) == 0.0
    assert _jaccard(["a", "b"], ["b", "c"]) == 1 - 1 / 3
    assert _jaccard([], []) == 0.0


def test_tv_distance():
    a = [{"d": ["x", "y"]}]
    b = [{"d": ["z"]}]
    assert _tv_distance(a, b, "d") == 1.0  # disjoint
    assert _tv_distance(a, a, "d") == 0.0


def test_divergence_within_vs_across():
    same = [{"probe": "p", "rep": 0, "o": ["x", "y", "z"]},
            {"probe": "p", "rep": 1, "o": ["x", "y", "z"]}]
    rev = [{"probe": "p", "rep": 0, "o": ["z", "y", "x"]},
           {"probe": "p", "rep": 1, "o": ["z", "y", "x"]}]
    assert _divergence(same, same, "o", _kendall_tau, within=True) == 0.0
    assert _divergence(same, rev, "o", _kendall_tau, within=False) == 1.0


def test_permutation_p_detects_separation():
    same = [{"probe": "p", "rep": i, "o": ["x", "y", "z"]} for i in range(4)]
    rev = [{"probe": "p", "rep": i, "o": ["z", "y", "x"]} for i in range(4, 8)]
    obs = _divergence(same, rev, "o", _kendall_tau, within=False)
    p = _permutation_p(same, rev, "o", _kendall_tau, obs, n_perm=200)
    assert p < 0.1  # clearly separated groups → low p
