"""Recall fan-out Hebbian learning surface (PAPER.md §4.7's third surface): credit.

Pure tests — no LLM. The consume side is already live (hippocampus._allocate_recall_
budget reads mem.recall→hippocampus.<strategy> weights to set the schema-vs-episode
split). These prove the WRITE side closes the loop: on a good-outcome turn the side
that actually surfaced memories is reinforced, by contribution share, so the learned
split drifts toward the productive pathway — while an idle pathway is left to decay.
"""

from __future__ import annotations

import types

from brain.hebbian import HebbianUpdater
from brain.wiring import Wiring
from brain.wiring_bootstrap import bootstrap


def _trace(contrib):
    return types.SimpleNamespace(recall_contrib=contrib, turn_id="t1")


def _w():
    w = Wiring()
    bootstrap(w)  # ensures mem.recall→hippocampus.<strategy> edges exist at 1.0
    return w


def test_episode_side_credited_when_episodes_drove_recall():
    """Episodes contributed, schema didn't → episode pathway rises, schema untouched."""
    w = _w()
    upd = HebbianUpdater(w)
    before_cos = w.get_edge_weight("mem.recall", "hippocampus.cosine_recall")
    before_grep = w.get_edge_weight("mem.recall", "hippocampus.schema_grep")
    n = upd._apply_recall_credit(
        _trace({"schema": 0, "episode": 4}),
        outcome=0.6, plasticity=1.0, turn_plast=1.0, gainers=[], losers=[],
    )
    assert n == 2  # cosine_recall + time_filter (episode side)
    assert w.get_edge_weight("mem.recall", "hippocampus.cosine_recall") > before_cos
    assert w.get_edge_weight("mem.recall", "hippocampus.time_filter") > before_cos
    assert w.get_edge_weight("mem.recall", "hippocampus.schema_grep") == before_grep  # 0 share


def test_split_shifts_toward_the_bigger_contributor():
    """Both sides contributed but episode more → episode rises more than schema, so the
    learned schema-vs-episode ratio moves toward episodes (the actual learning)."""
    w = _w()
    upd = HebbianUpdater(w)
    upd._apply_recall_credit(
        _trace({"schema": 1, "episode": 3}),
        outcome=0.8, plasticity=1.0, turn_plast=1.0, gainers=[], losers=[],
    )
    episode_gain = w.get_edge_weight("mem.recall", "hippocampus.cosine_recall") - 1.0
    schema_gain = w.get_edge_weight("mem.recall", "hippocampus.schema_grep") - 1.0
    assert episode_gain > schema_gain > 0


def test_credit_sign_follows_outcome():
    """A bad-outcome turn that leaned on a pathway nudges it DOWN."""
    w = _w()
    upd = HebbianUpdater(w)
    before = w.get_edge_weight("mem.recall", "hippocampus.cosine_recall")
    upd._apply_recall_credit(
        _trace({"schema": 0, "episode": 4}),
        outcome=-0.6, plasticity=1.0, turn_plast=1.0, gainers=[], losers=[],
    )
    assert w.get_edge_weight("mem.recall", "hippocampus.cosine_recall") < before


def test_no_recall_no_credit():
    """No recall ran (empty contrib) → no edges touched."""
    w = _w()
    upd = HebbianUpdater(w)
    n = upd._apply_recall_credit(
        _trace({}), outcome=0.9, plasticity=1.0, turn_plast=1.0, gainers=[], losers=[]
    )
    assert n == 0


def test_flag_off_disables_credit():
    from brain.settings import settings

    w = _w()
    upd = HebbianUpdater(w)
    settings.update({"recall_routing_credit": 0})
    try:
        n = upd._apply_recall_credit(
            _trace({"schema": 2, "episode": 2}),
            outcome=0.6, plasticity=1.0, turn_plast=1.0, gainers=[], losers=[],
        )
        assert n == 0
    finally:
        settings.update({"recall_routing_credit": 1})
