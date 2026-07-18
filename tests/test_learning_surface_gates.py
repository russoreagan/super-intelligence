"""The Learning surface reads the evidence gates and Tier-2 structural growth.

The blind spot this closes is the one eligibility credit already had once: a
subsystem applies real learning, logs it, and the surface that exists to prove
learning happened cannot see it. Avoidance beliefs, evidence-gate commits, and
node recruitment were all in that state (audit 2026-07-18, finding B7).

The privacy contract is tested alongside the counts: the records carry entity
strings so the eval log stays debuggable, but the Learning surface renders in a
UI and must stay numbers and route names.
"""

import pytest

from brain.observability import learning_ledger, learning_reader


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "tenant" / "personas" / "the_companion"
    r.mkdir(parents=True)
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(r))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "the_companion")
    monkeypatch.setattr(learning_ledger, "_line_counts", {})
    return r


def _log(decision, **fields):
    learning_ledger.append(
        {"type": "decision", "decision": decision, "persona": "the_companion", **fields}
    )


def test_gate_kinds_reach_the_ledger_at_all():
    """The central hook only writes kinds it knows about — an unlisted kind is
    silently eval-log-only, which is exactly how these went invisible."""
    for kind in (
        "evidence_commit",
        "avoidance_armed",
        "avoidance_confirmed",
        "avoidance_refuted",
        "node_recruited",
    ):
        assert kind in learning_ledger.LEDGER_TYPES


def test_evidence_commits_counted_per_gate(root):
    _log("evidence_commit", gate="avoidance", level=1.7, arm_bound=1.5)
    _log("evidence_commit", gate="avoidance", level=1.6, arm_bound=1.5)
    _log("evidence_commit", gate="satiation", level=2.1, arm_bound=2.0)
    g = learning_reader._gates_view("the_companion")
    assert g["commits_total"] == 3
    assert g["commits_by_gate"] == {"avoidance": 2, "satiation": 1}


def test_avoidance_precision_reads_from_graded_beliefs(root):
    _log("avoidance_armed", entity="the layoff", confidence=0.9)
    _log("avoidance_armed", entity="his brother", confidence=0.8)
    _log("avoidance_armed", entity="the move", confidence=0.7)
    _log("avoidance_confirmed", entity="the layoff", correct=True, da=0.02, steer=1)
    _log("avoidance_confirmed", entity="his brother", correct=True, da=0.02, steer=1)
    _log("avoidance_refuted", entity="the move", correct=False, da=-0.01, steer=1)
    av = learning_reader._gates_view("the_companion")["avoidance"]
    assert av["armed"] == 3
    assert (av["confirmed"], av["refuted"], av["resolved"]) == (2, 1, 3)
    assert av["precision_pct"] == pytest.approx(66.7, abs=0.1)
    assert av["steering"] is True
    assert av["da_moved"] == pytest.approx(0.05, abs=1e-6)


def test_no_graded_beliefs_yet_is_not_a_divide_by_zero(root):
    _log("avoidance_armed", entity="x", confidence=0.9)
    av = learning_reader._gates_view("the_companion")["avoidance"]
    assert av["armed"] == 1 and av["resolved"] == 0
    assert av["precision_pct"] is None
    assert av["steering"] is False


def test_gate_view_never_surfaces_entity_text(root):
    """The whole surface's contract: numbers and route names, no conversation
    content. The underlying records DO carry the entity — this must not pass it on."""
    _log("avoidance_armed", entity="the miscarriage", confidence=0.9)
    _log("avoidance_confirmed", entity="the miscarriage", correct=True, da=0.02, steer=1)
    _log("evidence_commit", gate="avoidance", level=1.7, arm_bound=1.5)
    blob = repr(learning_reader._gates_view("the_companion"))
    assert "miscarriage" not in blob


def test_recruitment_grouped_by_trigger(root):
    _log("node_recruited", node="drafter_5", source="drafter_1",
         fragments=["s1", "s2"], trigger="proven_cluster")
    _log("node_recruited", node="drafter_6", source="drafter_2", fragments=["s3", "s4"],
         trigger="workspace_ignition", coalition="memory", ignition_score=4.2)
    s = learning_reader._structure_view("the_companion")
    assert s["recruited_total"] == 2
    assert s["by_trigger"] == {"proven_cluster": 1, "workspace_ignition": 1}
    latest = s["recent"][-1]
    assert latest["node"] == "drafter_6"
    assert latest["fragments"] == 2  # count, not the ids
    assert latest["ignition_score"] == 4.2


def test_summary_exposes_both_new_views(root):
    _log("node_recruited", node="drafter_5", source="drafter_1",
         fragments=["s1"], trigger="proven_cluster")
    out = learning_reader.summary("the_companion")
    assert out["gates"]["commits_total"] == 0
    assert out["structure"]["recruited_total"] == 1


def test_recruitment_becomes_a_story(root):
    _log("node_recruited", node="drafter_6", source="drafter_2", fragments=["s3", "s4"],
         trigger="workspace_ignition", coalition="memory", ignition_score=4.2)
    claims = [s["claim"] for s in learning_reader.stories("the_companion")["stories"]]
    assert any("drafter_6" in c and "igniting" in c for c in claims)


def test_avoidance_learning_becomes_a_story(root):
    _log("avoidance_armed", entity="the layoff", confidence=0.9)
    _log("avoidance_confirmed", entity="the layoff", correct=True, da=0.02, steer=1)
    story = next(
        s for s in learning_reader.stories("the_companion")["stories"]
        if s["subsystem"] == "gates"
    )
    assert "1 of 1" in story["claim"]
    assert "layoff" not in story["claim"]
