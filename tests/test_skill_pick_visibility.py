"""Skill picks: whole-word keyword pre-pass + per-turn visibility.

2026-10-11 (Haven, org 0f3c064f): the direct-name pre-pass split every skill id on
"-" and substring-tested the tokens, so "myself" loaded haven-self-compassion,
"actually" loaded haven-act, and the bare word "Haven" loaded whichever haven-*
skill came first. Nothing about the pick reached stdout or the /v1 response, so the
misfires were invisible from Railway and from the tenant app.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from brain.api._affect import turn_stats
from brain.clusters.skill_selector import SkillBundle, SkillSelector, _Index


def _entry(name, keywords=(), *, partner=False, all_agents=True, agents=(), kind=None):
    e = {
        "name": name,
        "description": "",
        "category": "partner" if partner else "native",
        "tier": 2,
        "is_router": False,
        "keywords": list(keywords),
        "embedding": [],
        "_native": True,
    }
    if partner:
        e.update(_partner=True, _all_agents=all_agents, _agents=set(agents))
    if kind:
        e["kind"] = kind
    return e


def _index(*entries) -> _Index:
    idx = _Index.__new__(_Index)
    idx.skills = list(entries)
    idx._by_name = {e["name"]: e for e in entries}
    idx.tier1_names = []
    return idx


HAVEN = [
    _entry("haven-reflective-listening", partner=True),
    _entry("haven-self-compassion", partner=True),
    _entry("haven-act", partner=True),
]


# ── keyword pre-pass ──────────────────────────────────────────────────────────


def test_id_tokens_never_substring_match():
    idx = _index(*HAVEN, _entry("supabase-postgres-best-practices"))
    for text in (
        "I keep being hard on myself",
        "I actually felt better today",
        "Haven has been helping",
        "that was the best week in ages",
        "my practices at work",
    ):
        assert idx.keyword_match(text) is None, text


def test_full_id_phrase_matches_with_any_separator():
    idx = _index(*HAVEN, _entry("haven_self_compassion", partner=True))
    assert idx.keyword_match("can we do the haven self compassion one")["name"] in (
        "haven-self-compassion",
        "haven_self_compassion",
    )
    assert idx.keyword_match("try haven-act please")["name"] == "haven-act"


def test_keywords_are_whole_word_with_plural():
    idx = _index(_entry("trading-analyst", ["journal", "stock", "watchlist"]))
    assert idx.keyword_match("add it to my watchlist")["name"] == "trading-analyst"
    assert idx.keyword_match("check my journals")["name"] == "trading-analyst"
    assert idx.keyword_match("I've been journaling every night") is None
    assert idx.keyword_match("we're stocking up") is None


def test_single_word_native_name_still_matches():
    idx = _index(_entry("supabase"))
    assert idx.keyword_match("is supabase down?")["name"] == "supabase"


def test_longest_phrase_wins():
    idx = _index(_entry("a", ["break"]), _entry("b", ["self compassion break"]))
    assert idx.keyword_match("let's do a self compassion break")["name"] == "b"


def test_stances_never_keyword_match():
    idx = _index(_entry("stance-ask", ["ask first"], kind="stance"))
    assert idx.keyword_match("ask first") is None


def test_out_of_scope_partner_cannot_shadow_an_in_scope_match():
    idx = _index(
        _entry("other-app-grief-protocol", ["grief protocol"], partner=True, all_agents=False),
        _entry("grief", ["grief"], partner=True),
    )
    hit = idx.keyword_match("the grief protocol", allowed=lambda e: e["name"] == "grief")
    assert hit["name"] == "grief"


async def test_select_conversational_reports_direct_match_path():
    sel = SkillSelector.__new__(SkillSelector)
    sel._index = _index(*HAVEN, _entry("grounding", ["5-4-3-2-1"], partner=True))
    sel._router = SimpleNamespace(embed=AsyncMock(return_value=None))
    bundle, _, extras = await sel.select_conversational(
        "can we do the 5-4-3-2-1 thing", {}, "", [], None
    )
    assert bundle.chosen == ["grounding"]
    assert extras["pick_path"] == "direct_name_match"
    # "myself" no longer short-circuits. A chitchat turn with no emotion would gate
    # out, but this agent has org skills, so it goes to selection over those instead
    # (here the embedder is down, so nothing is picked) rather than direct-matching.
    bundle, _, extras = await sel.select_conversational(
        "I keep being hard on myself", {"response_type": "chitchat"}, "", [], None
    )
    assert extras.get("pick_path") != "direct_name_match"
    assert extras.get("gate_bypass") == "org_skills" and extras.get("embed_failed") is True


# ── per-turn visibility ───────────────────────────────────────────────────────


def _frontal(selector_result=None, exec_pick=False):
    from brain.clusters.frontal import FrontalCluster

    f = FrontalCluster.__new__(FrontalCluster)
    f._current_skill_bundle = None
    f._current_query_vec = None
    f.last_turn_skills = {}
    f._router = SimpleNamespace(embed=AsyncMock(return_value=None))
    f._parietal = SimpleNamespace(
        active_skill_context=None,
        recent_turns=lambda n: [],
        set_active_skill_context=lambda a: None,
    )
    sel = SimpleNamespace(
        tier1_names=[],
        get_skill=lambda n: {"name": n} if exec_pick else None,
        allowed_for_current_agent=lambda n: True,
        select_conversational=AsyncMock(return_value=selector_result),
    )
    f._skill_selector = sel
    return f


async def test_frontal_records_pick_path_and_chosen():
    f = _frontal(
        (
            SkillBundle(tier1=[], chosen=["haven-grounding"], pick_source="embed_winner"),
            None,
            {"pick_path": "embed_winner"},
        )
    )
    await f._select_skills_for_turn({"raw_text": "x"}, {}, "t1")
    assert f.last_turn_skills == {
        "turn_id": "t1",
        "path": "embed_winner",
        "chosen": ["haven-grounding"],
        "gated": False,
    }


async def test_frontal_records_gated_turn():
    f = _frontal((None, None, {"gated": True}))
    await f._select_skills_for_turn({"raw_text": "hi"}, {}, "t2")
    assert f.last_turn_skills["path"] == "gated"
    assert f.last_turn_skills["gated"] is True
    assert f.last_turn_skills["chosen"] == []


async def test_frontal_records_executive_pick():
    f = _frontal(exec_pick=True)
    await f._select_skills_for_turn({"raw_text": "x"}, {"skill": "haven-cbt"}, "t3")
    assert f.last_turn_skills["path"] == "executive_pick"
    assert f.last_turn_skills["chosen"] == ["haven-cbt"]


def test_pinned_skills_join_the_summary(monkeypatch):
    f = _frontal(exec_pick=True)
    f.last_turn_skills = {"turn_id": "t4", "path": "gated", "chosen": [], "gated": True}
    monkeypatch.setattr("brain.turn_ctx.current_turn", lambda: {"pinned_skills": ["haven-safety"]})
    f._apply_pinned_skills("t4")
    assert f.last_turn_skills["chosen"] == ["haven-safety"]
    assert f.last_turn_skills["path"] == "pinned"


def test_session_turn_summary_ignores_a_stale_turn():
    from brain.session_turn import _TurnMixin

    s = _TurnMixin.__new__(_TurnMixin)
    s.frontal = SimpleNamespace(
        last_turn_skills={"turn_id": "old", "path": "llm_pick", "chosen": ["x"], "gated": False}
    )
    assert s._turn_skill_summary("new") == {"path": "none", "chosen": [], "gated": False}
    assert s._turn_skill_summary("old")["chosen"] == ["x"]


def test_turn_stats_carries_skills_ids_only():
    out = turn_stats({"elapsed_s": 1.5, "llm_calls": 4, "skills": ["haven-act", ""]})
    assert out == {"elapsed_s": 1.5, "llm_calls": 4, "skills": ["haven-act"]}
    assert "skills" not in turn_stats({"elapsed_s": 1.0})
