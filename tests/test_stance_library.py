"""
Stance library — Phase B of the approach-competition plan.

Covers: the 9 stance-*.md files themselves, containment (a stance can never be the
conversational active skill or receive the operational framing), the two pools and
their axis separation, receptor classes and budgets, the chemistry-modulated draw
(bias with a floor — never a gate), and the drafter injection in directive form.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from brain.budget import chem_budget, chem_effort
from brain.clusters.frontal import FrontalCluster
from brain.clusters.skill_selector import SkillSelector
from brain.fragment_pool import (
    DRAFT_SLOT,
    INFO_SLOT,
    METHOD_SLOT,
    fragment_receptor,
    is_admissible,
)
from brain.settings import settings
from brain.stance_affinity import (
    KNOWN_CHANNELS,
    affinity_score,
    complexity_congruence,
    floored_softmax_pick,
    turn_seed,
)

SKILLS_DIR = pathlib.Path(__file__).resolve().parents[1] / "brain" / "skills"
STANCE_FILES = sorted(SKILLS_DIR.glob("stance-*.md"))

EXPECTED_STANCES = {
    "stance-answer-from-known",
    "stance-recall-before-search",
    "stance-verify-the-premise",
    "stance-freshness-check",
    "stance-ask-dont-guess",
    "stance-proportion-effort",
    "stance-smallest-reversible-probe",
    "stance-propose-before-acting",
    "stance-do-and-report",
}


def _frontmatter(path: pathlib.Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---")
    return yaml.safe_load(raw[3 : raw.index("---", 3)])


async def _warmed_selector(monkeypatch) -> SkillSelector:
    """A real SkillSelector warmed over brain/skills/*.md with the humanity index
    sync neutralized — .claude/skills exists in this repo, and letting the sync run
    could REWRITE the real index JSON from inside a test."""
    import brain.skills._import_humanity as ih
    from tests.conftest import FakeRouter

    monkeypatch.setattr(ih, "SOURCE_DIR", pathlib.Path("/nonexistent-for-test"))
    sel = SkillSelector(FakeRouter())
    await sel.warm_native_skills()
    return sel


# ── the files themselves ─────────────────────────────────────────────────────


def test_all_nine_stance_files_present_and_wellformed():
    names = {_frontmatter(p)["name"] for p in STANCE_FILES}
    assert names == EXPECTED_STANCES
    for p in STANCE_FILES:
        fm = _frontmatter(p)
        assert fm["kind"] == "stance", p.name
        assert not fm.get("is_router"), p.name
        assert 0.0 <= float(fm["complexity"]) <= 1.0, p.name
        # description's FIRST SENTENCE is the injected directive — it must exist
        # and be crisp enough to fit the ~15-token budget.
        first = str(fm["description"]).split(". ")[0]
        assert 20 < len(first) < 160, p.name
        # affinity maps may only reference known chemistry channels
        for ch in fm.get("affinity") or {}:
            assert ch in KNOWN_CHANNELS, f"{p.name}: unknown channel {ch}"
        # body substantial enough to serve as the winner-tier injection later
        body = p.read_text(encoding="utf-8")
        assert len(body) > 1500, p.name


def test_affinity_directions_match_the_design():
    """The plan's chemistry→posture table, pinned as data."""
    fm = {p.name.removesuffix(".md"): _frontmatter(p) for p in STANCE_FILES}
    aff = {k: v.get("affinity") or {} for k, v in fm.items()}
    assert aff["stance-propose-before-acting"].get("CORT", 0) > 0
    assert aff["stance-smallest-reversible-probe"].get("CORT", 0) > 0
    assert aff["stance-do-and-report"].get("DA", 0) > 0
    assert aff["stance-verify-the-premise"].get("NE", 0) > 0
    assert aff["stance-freshness-check"].get("NE", 0) > 0
    assert aff["stance-ask-dont-guess"].get("OXT", 0) > 0
    assert aff["stance-answer-from-known"].get("DA", 0) < 0


# ── containment: stances never enter skill selection ─────────────────────────


async def test_stances_warm_into_index_with_kind(monkeypatch):
    sel = await _warmed_selector(monkeypatch)
    entry = sel.get_skill("stance-ask-dont-guess")
    assert entry is not None
    assert entry["kind"] == "stance"
    assert entry.get("affinity", {}).get("OXT", 0) > 0
    assert 0.0 <= entry["complexity"] <= 1.0


async def test_stance_never_ranked_or_keyword_matched(monkeypatch):
    sel = await _warmed_selector(monkeypatch)
    ranked = sel._index.rank([0.0] * 768, include_tier_1=True)
    assert all(e["kind"] != "stance" for e, _ in ranked if "kind" in e)
    assert all(not e["name"].startswith("stance-") for e, _ in ranked)
    # keyword short-circuit: "ask" appears in stance-ask-dont-guess keywords
    hit = sel._index.keyword_match("just ask first before you guess anything")
    assert hit is None or hit["name"] not in EXPECTED_STANCES


async def test_stance_body_empty_on_conversational_path(monkeypatch):
    """Even if a stance were somehow chosen as the active skill, the operational
    'tools are REAL' injection path gets "" — while stance_body() still reads it."""
    sel = await _warmed_selector(monkeypatch)
    assert sel.native_skill_body("stance-do-and-report") == ""
    assert len(sel.stance_body("stance-do-and-report")) > 1500


async def test_stance_excluded_from_manifest_and_fragment_pool(monkeypatch):
    sel = await _warmed_selector(monkeypatch)
    assert "stance-" not in sel.capability_manifest()
    assert not any(s.startswith("stance-") for s in sel.attachable_fragment_ids())


# ── pools and axis separation ────────────────────────────────────────────────


async def test_info_pool_is_exactly_the_nine(monkeypatch):
    sel = await _warmed_selector(monkeypatch)
    assert {e["name"] for e in sel.info_pool()} == EXPECTED_STANCES


async def test_method_pool_is_strategy_leaves_only(monkeypatch):
    sel = await _warmed_selector(monkeypatch)
    pool = sel.method_pool()
    names = {e["name"] for e in pool}
    assert len(pool) > 30  # ~56 strategy-shaped leaves
    assert not any(n.startswith(("writing-", "dnd", "supabase")) for n in names)
    assert not any(n in EXPECTED_STANCES for n in names)
    assert all(not e["is_router"] for e in pool)
    assert all(e["tier"] != 1 for e in pool)
    assert all(e["category"] in SkillSelector.METHOD_CATEGORIES for e in pool)
    # derived complexity: in range, deterministic, and not degenerate
    cplx = [e["complexity"] for e in pool]
    assert all(0.1 <= c <= 0.95 for c in cplx)
    assert len(set(cplx)) > 1  # varies with body structure, not a constant


async def test_axes_never_cross(monkeypatch):
    """A method skill can never occupy the info slot or vice versa — this is what
    keeps candidates comparable, so it is a test rather than a convention."""
    sel = await _warmed_selector(monkeypatch)
    assert sel.stance_kind("stance-verify-the-premise") == "info"
    assert sel.stance_kind("constraint-hardness-testing") == "method"
    assert sel.stance_kind("writing-line-editing") is None  # not strategy-shaped
    info_names = {e["name"] for e in sel.info_pool()}
    method_names = {e["name"] for e in sel.method_pool()}
    assert not info_names & method_names


async def test_stance_directive_form(monkeypatch):
    sel = await _warmed_selector(monkeypatch)
    d = sel.stance_directive("stance-answer-from-known")
    assert d.startswith("stance-answer-from-known — ")
    assert d.endswith(".")
    assert len(d) < 200  # directive tier stays ~15 tokens, not a body


# ── receptor classes ─────────────────────────────────────────────────────────


def test_receptor_classes_and_admissibility():
    assert fragment_receptor("stance-freshness-check") == INFO_SLOT
    assert fragment_receptor("investigation-source-trace") == DRAFT_SLOT
    assert fragment_receptor("investigation-source-trace", kind="method") == METHOD_SLOT
    # info stances: drafters only — never a judge or the executive
    assert is_admissible("stance-freshness-check", "frontal.drafter_C")
    for host in ("frontal.critic", "frontal.empathy_critic", "frontal.executive"):
        assert not is_admissible("stance-freshness-check", host)
    # safety denylist unaffected
    assert not is_admissible("stance-freshness-check", "motor_cortex.tool_planner")


# ── chemistry math ───────────────────────────────────────────────────────────


def test_chem_effort_matches_chem_budget_curve():
    """The float sibling agrees with the canonical curve at rest and both clamps."""
    assert chem_effort(None) == 0.5
    assert chem_effort({"DA": 0.5, "CORT": 0.5}) == 0.5
    assert chem_effort({"DA": 1.0, "CORT": 0.0}) == 1.0
    assert chem_effort({"DA": 0.0, "CORT": 1.0}) == 0.0
    # direction agrees with chem_budget on the same inputs
    hi = {"DA": 0.9, "CORT": 0.2}
    lo = {"DA": 0.2, "CORT": 0.9}
    assert chem_effort(hi) > 0.5 > chem_effort(lo)
    assert chem_budget(hi, base=3, gain=2.0, lo=1, hi=5) >= 3
    assert chem_budget(lo, base=3, gain=2.0, lo=1, hi=5) <= 3


def test_affinity_score_directions():
    aff = {"CORT": 0.7}
    assert affinity_score({"CORT": 0.95}, aff) > 0
    assert affinity_score({"CORT": 0.1}, aff) < 0
    assert affinity_score({"CORT": 0.5}, aff) == 0.0
    # negative coefficient reads as favored-when-low
    assert affinity_score({"DA": 0.1}, {"DA": -0.6}) > 0
    # unknown channels ignored, never an error
    assert affinity_score({"CORT": 0.9}, {"BOGUS": 5.0}) == 0.0
    assert affinity_score(None, aff) == 0.0


def test_complexity_congruence_peaks_at_match():
    assert complexity_congruence(0.7, 0.7) == 0.0
    assert complexity_congruence(0.9, 0.1) == pytest.approx(-0.8)
    assert complexity_congruence(0.2, 0.7) < complexity_congruence(0.6, 0.7)


def test_floored_softmax_floor_and_determinism():
    ids = ["a", "b", "c"]
    logits = [10.0, 0.0, 0.0]  # heavily biased toward "a"
    picks = {floored_softmax_pick(ids, logits, floor=0.05, seed=s) for s in range(4000)}
    assert picks == {"a", "b", "c"}  # the floor keeps b and c reachable
    # deterministic given the seed
    s = turn_seed("turn-1", 3)
    assert all(
        floored_softmax_pick(ids, logits, floor=0.05, seed=s) == "a" for _ in range(5)
    ) or all(
        floored_softmax_pick(ids, logits, floor=0.05, seed=s)
        == floored_softmax_pick(ids, logits, floor=0.05, seed=s)
        for _ in range(5)
    )


# ── the draw, end to end on a frontal skeleton ───────────────────────────────


class _RestWiring:
    """Wiring stub: no attachments, every edge at rest."""

    @staticmethod
    def attached_fragments(host):
        return []

    @staticmethod
    def get_edge_weight(a, b):
        from brain.wiring import WEIGHT_REST

        return WEIGHT_REST


async def _skeleton(monkeypatch, chem: dict):
    f = FrontalCluster.__new__(FrontalCluster)
    f._wiring = _RestWiring()
    f._wiring_frozen = False
    f._skill_selector = await _warmed_selector(monkeypatch)
    f._current_skill_bundle = None
    f._current_query_vec = None  # zero-embed FakeRouter → relevance is moot anyway
    f._chem_snapshot = lambda: chem
    return f


async def test_high_cort_pulls_cautious_postures(monkeypatch):
    """Direction, not exact picks — 'chemistry influences, not defines'."""
    stressed = await _skeleton(monkeypatch, {"CORT": 0.95, "DA": 0.2})
    resting = await _skeleton(monkeypatch, {"CORT": 0.5, "DA": 0.5})
    cautious = {"stance-propose-before-acting", "stance-smallest-reversible-probe"}
    n = 400

    def count(f):
        return sum(
            1
            for i in range(n)
            if f._draw_explore_stance("info", "frontal.drafter_A", [], f"t{i}", 0) in cautious
        )

    assert count(stressed) > count(resting)


async def test_stress_lowers_drawn_method_complexity(monkeypatch):
    """Cognitive economy: a stressed brain draws shallower methods than a calm,
    motivated one — on average, over many turns."""
    stressed = await _skeleton(monkeypatch, {"CORT": 0.95, "DA": 0.1})
    driven = await _skeleton(monkeypatch, {"CORT": 0.1, "DA": 0.95})
    n = 400

    def mean_complexity(f):
        sel = f._skill_selector
        total = 0.0
        for i in range(n):
            sid = f._draw_explore_stance("method", "frontal.drafter_A", [], f"t{i}", 1)
            total += float(sel.get_skill(sid)["complexity"])
        return total / n

    assert mean_complexity(stressed) < mean_complexity(driven)


async def test_chemistry_never_gates(monkeypatch):
    """Under maximal stress, every info stance — including freshness-check, whose
    absence would silently suppress real tool use — remains reachable."""
    f = await _skeleton(monkeypatch, {"CORT": 1.0, "DA": 0.0})
    seen = {
        f._draw_explore_stance("info", "frontal.drafter_A", [], f"t{i}", 0) for i in range(4000)
    }
    assert seen == EXPECTED_STANCES


async def test_chem_toggles_kill_the_bias(monkeypatch):
    monkeypatch.setitem(settings._data, "stance_chem_affinity", 0)
    stressed = await _skeleton(monkeypatch, {"CORT": 0.95, "DA": 0.2})
    resting = await _skeleton(monkeypatch, {"CORT": 0.5, "DA": 0.5})
    # with the affinity term off, both states draw identically per turn seed
    for i in range(50):
        a = stressed._draw_explore_stance("info", "frontal.drafter_A", [], f"t{i}", 0)
        b = resting._draw_explore_stance("info", "frontal.drafter_A", [], f"t{i}", 0)
        assert a == b


# ── drafter injection ────────────────────────────────────────────────────────


class _AttachedWiring(_RestWiring):
    def __init__(self, attached):
        self._attached = attached

    def attached_fragments(self, host):
        return list(self._attached)


async def test_established_stance_renders_as_directive_not_operational(monkeypatch):
    f = await _skeleton(monkeypatch, {"CORT": 0.5, "DA": 0.5})
    f._wiring = _AttachedWiring([("stance-verify-the-premise", 1.5)])
    block, injected = f._fragment_block_for_host(
        "frontal.drafter_B", explore=False, turn_id="t", seed_idx=1
    )
    assert "stance-verify-the-premise" in block
    assert "Approach stance" in block
    assert "angle of attack" in block
    assert "tools it names are REAL" not in block  # never the operational framing
    assert injected == ["stance-verify-the-premise"]


async def test_stances_and_procedural_skills_hold_separate_budgets(monkeypatch):
    """A host carrying 2 procedural skills AND stances keeps all of them — stances
    never evict a procedural fragment or vice versa."""
    f = await _skeleton(monkeypatch, {"CORT": 0.5, "DA": 0.5})
    sel = f._skill_selector
    # two fake partner (procedural) skills with live bodies
    for pid in ("acme-alpha", "acme-beta"):
        sel._index.inject_partner(
            {
                "name": pid,
                "description": "partner tool",
                "category": "partner",
                "tier": 2,
                "is_router": False,
                "keywords": [],
                "embedding": [0.0] * 768,
                "_native": True,
                "_partner": True,
            }
        )
        sel._native_body_cache[pid] = "Operational partner guide body."
    f._wiring = _AttachedWiring(
        [
            ("acme-alpha", 1.6),
            ("acme-beta", 1.5),
            ("stance-do-and-report", 1.5),
            ("constraint-hardness-testing", 1.4),
        ]
    )
    block, injected = f._fragment_block_for_host(
        "frontal.drafter_B", explore=False, turn_id="t", seed_idx=1
    )
    assert set(injected) == {
        "acme-alpha",
        "acme-beta",
        "stance-do-and-report",
        "constraint-hardness-testing",
    }
    # 2 procedural (cap) + 1 info + 1 method — nobody evicted anybody
    assert "Approach stance" in block


async def test_exploring_drafter_draws_a_stance(monkeypatch):
    f = await _skeleton(monkeypatch, {"CORT": 0.5, "DA": 0.5})
    block, injected = f._fragment_block_for_host(
        "frontal.drafter_A",
        explore=True,
        turn_id="t9",
        seed_idx=0,  # even → info axis
    )
    stances = [sid for sid in injected if sid.startswith("stance-")]
    assert len(stances) == 1
    assert "Approach stance" in block


async def test_stance_library_off_injects_nothing(monkeypatch):
    monkeypatch.setitem(settings._data, "stance_library", 0)
    f = await _skeleton(monkeypatch, {"CORT": 0.5, "DA": 0.5})
    f._wiring = _AttachedWiring([("stance-verify-the-premise", 1.5)])
    block, injected = f._fragment_block_for_host(
        "frontal.drafter_B", explore=True, turn_id="t", seed_idx=1
    )
    assert injected == []
    assert "Approach stance" not in block


def test_settings_registered():
    from brain.settings import DEFAULTS

    for key, expected in {
        "stance_library": 1,
        "stance_info_max_per_host": 1,
        "stance_method_max_per_host": 1,
        "stance_chem_affinity": 1,
        "stance_chem_complexity": 1,
        "stance_draw_w_relevance": 1.0,
        "stance_draw_w_learned": 0.6,
        "stance_draw_w_affinity": 0.4,
        "stance_draw_w_complexity": 0.35,
        "stance_draw_floor": 0.02,
    }.items():
        assert DEFAULTS[key] == expected, key
