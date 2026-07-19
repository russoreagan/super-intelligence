"""
Relevance-ranked fragment exploration (Phase A of the approach-competition plan).

_explore_candidate used to pick an exploring drafter's fragment by a blind
deterministic hash over the whole pool — relevance to the input played no part.
With `fragment_explore_relevance_ranked` on, the pool is cosine-ranked against
the turn's query embedding, the top K survive, and the per-turn hash rolls among
those. The hash path remains byte-identical as both the rollback (flag off) and
the fallback (no query vector this turn).
"""

from __future__ import annotations

import hashlib

import pytest

from brain.clusters.frontal import FrontalCluster
from brain.clusters.skill_selector import SkillSelector
from brain.settings import settings

# ── helpers ──────────────────────────────────────────────────────────────────


def _selector_with(entries: dict[str, list[float]]) -> SkillSelector:
    """A SkillSelector skeleton whose index serves the given {sid: embedding}."""
    sel = SkillSelector.__new__(SkillSelector)

    class _FakeIndex:
        @staticmethod
        def get(name):
            emb = entries.get(name)
            return None if emb is None else {"name": name, "embedding": emb}

    sel._index = _FakeIndex()
    return sel


def _frontal_with(pool: list[str], entries: dict[str, list[float]], query_vec):
    """A FrontalCluster skeleton with just what _explore_candidate touches."""
    f = FrontalCluster.__new__(FrontalCluster)

    class _Wiring:
        @staticmethod
        def attached_fragments(host):
            return []  # no promising sub-threshold attachments → pool path

    sel = _selector_with(entries)
    sel.attachable_fragment_ids = lambda: list(pool)
    f._wiring = _Wiring()
    f._skill_selector = sel
    f._current_skill_bundle = None
    f._current_query_vec = query_vec
    return f


def _legacy_pick(pool: list[str], turn_id: str, seed_idx: int) -> str:
    seed = int.from_bytes(hashlib.sha1(f"{turn_id}:{seed_idx}".encode()).digest()[:8], "big")
    return sorted(pool)[seed % len(pool)]


@pytest.fixture(autouse=True)
def _admissible_everything(monkeypatch):
    """These tests cover selection, not admissibility (test_fragment_wiring owns that)."""
    import brain.fragment_pool as fp

    monkeypatch.setattr(fp, "is_admissible", lambda sid, host: True)


HOST = "frontal.drafter_A"

# Orthogonal embeddings: alpha ⟂ beta, gamma near alpha.
ENTRIES = {
    "alpha": [1.0, 0.0],
    "beta": [0.0, 1.0],
    "gamma": [0.9, 0.1],
}
POOL = list(ENTRIES)


# ── rank helper ──────────────────────────────────────────────────────────────


def test_rank_by_relevance_orders_and_omits_missing():
    sel = _selector_with({**ENTRIES, "unembedded": []})
    ranked = sel.rank_fragments_by_relevance([*POOL, "unembedded", "unknown"], [1.0, 0.0])
    names = [sid for sid, _ in ranked]
    assert names == ["alpha", "gamma", "beta"]  # cosine order to [1,0]
    assert "unembedded" not in names and "unknown" not in names


def test_rank_ties_break_by_name():
    sel = _selector_with({"zeta": [1.0, 0.0], "acme": [1.0, 0.0]})
    ranked = sel.rank_fragments_by_relevance(["zeta", "acme"], [1.0, 0.0])
    assert [sid for sid, _ in ranked] == ["acme", "zeta"]


# ── _explore_candidate behavior ──────────────────────────────────────────────


def test_different_inputs_pick_different_candidates_same_turn(monkeypatch):
    """The Phase A acceptance check: same turn_id, different inputs → different
    explore candidates. Under the legacy hash they would be identical."""
    monkeypatch.setitem(settings._data, "fragment_explore_relevance_ranked", 1)
    monkeypatch.setitem(settings._data, "fragment_explore_top_k", 1)
    pick_a = _frontal_with(POOL, ENTRIES, [1.0, 0.0])._explore_candidate(HOST, [], "t1", 0)
    pick_b = _frontal_with(POOL, ENTRIES, [0.0, 1.0])._explore_candidate(HOST, [], "t1", 0)
    assert pick_a == "alpha"
    assert pick_b == "beta"
    assert pick_a != pick_b


def test_roll_stays_within_top_k(monkeypatch):
    """With top_k=2 and a query near alpha, beta (the off-topic fragment) can
    never be explored, whatever the turn hash says."""
    monkeypatch.setitem(settings._data, "fragment_explore_relevance_ranked", 1)
    monkeypatch.setitem(settings._data, "fragment_explore_top_k", 2)
    for turn in ("t1", "t2", "t3", "t4", "t5"):
        f = _frontal_with(POOL, ENTRIES, [1.0, 0.0])
        assert f._explore_candidate(HOST, [], turn, 0) in ("alpha", "gamma")


def test_no_query_vec_falls_back_to_legacy_hash(monkeypatch):
    monkeypatch.setitem(settings._data, "fragment_explore_relevance_ranked", 1)
    f = _frontal_with(POOL, ENTRIES, None)
    for turn in ("t1", "t2", "t3"):
        assert f._explore_candidate(HOST, [], turn, 0) == _legacy_pick(POOL, turn, 0)


def test_flag_off_is_byte_identical_to_legacy(monkeypatch):
    monkeypatch.setitem(settings._data, "fragment_explore_relevance_ranked", 0)
    f = _frontal_with(POOL, ENTRIES, [1.0, 0.0])  # vec present but flag off
    for turn in ("t1", "t2", "t3"):
        for seed_idx in (0, 1, 2):
            assert f._explore_candidate(HOST, [], turn, seed_idx) == _legacy_pick(
                POOL, turn, seed_idx
            )


def test_rank_failure_falls_back_to_legacy_hash(monkeypatch):
    """A selector error must degrade to the blind roll, never propagate."""
    monkeypatch.setitem(settings._data, "fragment_explore_relevance_ranked", 1)
    f = _frontal_with(POOL, ENTRIES, [1.0, 0.0])

    def _boom(*a, **kw):
        raise RuntimeError("index unavailable")

    f._skill_selector.rank_fragments_by_relevance = _boom
    assert f._explore_candidate(HOST, [], "t1", 0) == _legacy_pick(POOL, "t1", 0)


def test_exclude_and_baseline_still_respected(monkeypatch):
    """Relevance ranking runs AFTER the baseline/exclude filtering — an excluded
    fragment can't sneak back in by being the most relevant."""
    monkeypatch.setitem(settings._data, "fragment_explore_relevance_ranked", 1)
    monkeypatch.setitem(settings._data, "fragment_explore_top_k", 3)
    f = _frontal_with(POOL, ENTRIES, [1.0, 0.0])
    pick = f._explore_candidate(HOST, ["alpha"], "t1", 0)
    assert pick in ("beta", "gamma")  # alpha excluded despite top cosine


def test_settings_registered():
    """Unknown keys are silently dropped by settings load — pin registration."""
    from brain.settings import DEFAULTS

    assert DEFAULTS["fragment_explore_relevance_ranked"] == 1
    assert DEFAULTS["fragment_explore_top_k"] == 3
