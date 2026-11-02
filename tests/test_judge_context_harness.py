"""Offline checks for eval/judge_context: the fixtures are well formed, the `current`
arm is the engine's own judge prompt (so a before/after run measures the engine, not a
copy of it), and the scorer counts gold pairs correctly. No network."""

from __future__ import annotations

from brain.clusters.frontal import FrontalCluster
from brain.clusters.frontal_prompts import CRITIC_SYSTEM, EMPATHY_CRITIC_SYSTEM
from eval.judge_context.fixtures import CASES
from eval.judge_context.harness import (
    ARMS,
    NO_EMPATHY,
    build_requests,
    engine_inputs,
    summarize,
)


def test_fixture_gold_pairs_name_real_drafts():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))
    for c in CASES:
        drafts = set(c["drafts"])
        assert len(drafts) >= 3, c["id"]
        assert c["gold"], c["id"]
        for better, worse, why in c["gold"]:
            assert better in drafts and worse in drafts and better != worse, (
                c["id"],
                better,
                worse,
            )
            assert why.strip()
        # Every case has at least one draft that never loses a gold pair.
        losers = {w for _b, w, _ in c["gold"]}
        assert drafts - losers, c["id"]


def test_fixtures_cover_every_agent_shape():
    assert {c["agent_type"] for c in CASES} >= {
        "task",
        "analyst",
        "companion",
        "listener",
        "story",
        "default",
    }


def test_current_arm_is_the_engine_prompt():
    for c in CASES:
        inputs = engine_inputs(c)
        jobs = build_requests("current", c, inputs)
        critic = [j for j in jobs if j["judge"] == "critic"]
        empathy = [j for j in jobs if j["judge"] == "empathy"]
        assert len(critic) == len(c["drafts"])
        assert len(empathy) == (0 if c["user_emotion"] in NO_EMPATHY else len(c["drafts"]))
        for j in critic:
            did = j["draft_ids"][0]
            req = j["request"]
            assert [b["text"] for b in req["system"]] == [CRITIC_SYSTEM]
            assert req["messages"][0]["content"][0]["text"] == FrontalCluster._critic_prompt(
                c["drafts"][did], inputs["drafter_prompt"]
            )
        for j in empathy:
            did = j["draft_ids"][0]
            req = j["request"]
            assert [b["text"] for b in req["system"]] == [EMPATHY_CRITIC_SYSTEM]
            assert req["messages"][0]["content"][0]["text"] == FrontalCluster._empathy_prompt(
                c["drafts"][did], c["user_emotion"]
            )


def test_engine_inputs_carry_the_mandate_where_drafters_see_it():
    c = next(c for c in CASES if c["id"] == "companion_about_you")
    inputs = engine_inputs(c)
    # The drafters get the mandate text in the cached block and only its id per turn.
    # The critic is scored against the per-turn prompt alone, so it never sees the
    # length rule: the gap the design closes for the judges.
    rule = "one to three sentences"
    assert rule in inputs["cached_context"]
    assert rule not in inputs["drafter_prompt"]
    assert f"[{c['mandate_id']}]" in inputs["drafter_prompt"]


def test_proposed_judges_share_one_cached_prefix():
    for c in CASES:
        inputs = engine_inputs(c)
        jobs = build_requests("proposed", c, inputs)
        prefixes = {tuple(b["text"] for b in j["request"]["system"]) for j in jobs}
        assert len(prefixes) == 1, c["id"]  # critic + empathy read the same cache entry
        (prefix,) = prefixes
        assert inputs["cached_context"] in prefix


def test_every_arm_builds_for_every_case():
    for c in CASES:
        inputs = engine_inputs(c)
        for arm in ARMS:
            assert build_requests(arm, c, inputs), (arm, c["id"])


def test_summarize_counts_pairs_and_flips():
    case = {
        "id": "t",
        "agent_type": "task",
        "drafts": {"a": "short.", "b": "longer one?", "c": "longest of the three drafts."},
        "gold": [("a", "b", "x"), ("a", "c", "x")],
    }

    def round_(cs, es):
        return {
            "critic": {d: {"score": s, "veto": False} for d, s in cs.items()},
            "empathy": {d: {"score": s, "veto": False} for d, s in es.items()},
            "latency": 1.0,
            "cost": 0.01,
            "calls": 6,
        }

    # The critic prefers a; the empathy judge prefers b strongly enough to flip it.
    res = [
        {
            "case": "t",
            "agent_type": "task",
            "rounds": [round_({"a": 0.9, "b": 0.85, "c": 0.5}, {"a": 0.5, "b": 1.0, "c": 0.5})],
        }
    ]
    s = summarize(res, [case])
    assert s["critic_pairwise"] == 1.0
    assert s["pairwise"] == 0.5  # blended: b (0.895) beats a (0.78); a still beats c
    assert s["top1"] == 0.0
    assert s["empathy_flips"].startswith("1/1") and "away 1" in s["empathy_flips"]
