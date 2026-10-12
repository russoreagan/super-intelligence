"""Fixes from the Haven listener session of 2026-10-12 (org 0f3c064f, agent
the_empath.therapy_listener):

1. The conversational skill gate dropped half the therapy turns: it passed only a
   literal list of emotion labels, so "struggling", "tired", "worried" read as
   chitchat. It now gates by valence, and an agent with its org's own skills goes
   to selection over those skills on a turn the gate would drop.
2. A locked skill stayed through "so what can I do?": a change in what the person
   is asking for (telling → asking) now re-opens selection; gated and
   direct-match turns age the lock.
3. A restarted process restores an engine conversation's recent turns from the
   agent's verbatim turn log, retrying a failed read.
4. Mandate reply rules: the active assignment reaches the executive and the
   critic, the critic scores `fit`, the framing makes the assignment's reply rules
   binding, and markdown emphasis is stripped from the reply.
5. `skills_org_only` limits an agent to its org's own skills.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brain import agents
from brain.clusters import parietal as parietal_mod
from brain.clusters.parietal import ParietalCluster, make_lane_recall
from brain.clusters.skill_selector import (
    STICKY_TURN_BUDGET,
    ActiveSkillContext,
    SkillBundle,
    SkillSelector,
    _Index,
    ask_mode,
    bind_turn_agent,
)
from brain.turn_ctx import bind_turn
from brain.user_emotion import is_negative_state, user_emotion_valence

EMPATH = "the_empath.therapy_listener"


def _entry(name, vec, *, partner=False, native=None, tier=2, router=False, keywords=()):
    e = {
        "name": name,
        "description": f"{name} description",
        "category": "partner" if partner else name.split("-")[0],
        "tier": tier,
        "is_router": router,
        "keywords": list(keywords),
        "embedding": list(vec),
    }
    if native or partner:
        e["_native"] = True
    if partner:
        e.update(_partner=True, _all_agents=True, _agents=set())
    return e


def _selector(*entries, embed=None) -> SkillSelector:
    sel = SkillSelector.__new__(SkillSelector)
    idx = _Index.__new__(_Index)
    idx.skills = list(entries)
    idx._by_name = {e["name"]: e for e in entries}
    idx.tier1_names = []
    sel._index = idx
    sel._native_body_cache = {}
    sel._router = SimpleNamespace(embed=AsyncMock(return_value=embed))
    sel._conversational_cell = SimpleNamespace(
        reset_turn=lambda t: None, call=AsyncMock(return_value='{"skill": null}')
    )
    return sel


@pytest.fixture(autouse=True)
def _no_agent_store(monkeypatch):
    """No Supabase: agent skill policy comes from this table."""
    policy: dict[str, tuple[frozenset, bool]] = {}
    monkeypatch.setattr(
        agents, "disabled_skills", lambda aid: policy.get(aid, (frozenset(), False))[0]
    )
    monkeypatch.setattr(
        agents, "org_skills_only", lambda aid: policy.get(aid, (frozenset(), False))[1]
    )
    return policy


# ── 1. the gate ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "label", ["struggling", "tired", "disappointed", "worried", "stuck", "exhausted", "sad"]
)
def test_gate_passes_every_negative_label(label):
    sel = _selector()
    assert sel.gate_conversational(response_type="chitchat", user_emotion=label)


@pytest.mark.parametrize("label", ["neutral", "curious", "happy", "surprised", "", "xyzzy"])
def test_gate_drops_neutral_and_positive_labels(label):
    sel = _selector()
    assert not sel.gate_conversational(response_type="chitchat", user_emotion=label)


def test_gate_reads_negative_sentiment_when_the_label_is_neutral():
    sel = _selector()
    assert sel.gate_conversational(response_type="chitchat", user_emotion="neutral", sentiment=-0.5)
    assert not sel.gate_conversational(
        response_type="chitchat", user_emotion="neutral", sentiment=0.2
    )
    assert is_negative_state("", "-0.4") and not is_negative_state("", "junk")


def test_valence_table_is_shared_with_sleep():
    from brain.sleep import SleepConsolidation

    assert SleepConsolidation._emotion_valence("struggling") == user_emotion_valence("struggling")
    assert user_emotion_valence("WORRIED") < 0


async def test_gated_turn_selects_over_org_skills_only():
    # A framework scores higher than the org skill, but on a turn the gate drops
    # only the org's own skills are candidates.
    sel = _selector(
        _entry("decision-premortem", [1.0, 0.0]),
        _entry("haven_reflective_listening", [0.8, 0.6], partner=True),
        embed=[1.0, 0.0],
    )
    with bind_turn_agent(EMPATH):
        bundle, _, extras = await sel.select_conversational(
            "Exactly.", {"response_type": "chitchat"}, "neutral", [], None
        )
    assert extras["gate_bypass"] == "org_skills"
    assert [n for n, _ in extras["candidates"]] == ["haven_reflective_listening"]
    assert bundle.chosen == ["haven_reflective_listening"]


async def test_gated_turn_without_org_skills_still_gates():
    sel = _selector(_entry("decision-premortem", [1.0, 0.0]), embed=[1.0, 0.0])
    with bind_turn_agent(EMPATH):
        bundle, _, extras = await sel.select_conversational(
            "Exactly.", {"response_type": "chitchat"}, "neutral", [], None
        )
    assert bundle is None and extras["gated"] is True


async def test_switched_off_org_skills_do_not_open_the_gate(_no_agent_store):
    _no_agent_store[EMPATH] = (frozenset({"haven_reflective_listening"}), False)
    sel = _selector(_entry("haven_reflective_listening", [1.0, 0.0], partner=True))
    with bind_turn_agent(EMPATH):
        bundle, _, extras = await sel.select_conversational(
            "Exactly.", {"response_type": "chitchat"}, "neutral", [], None
        )
    assert bundle is None and extras["gated"] is True


# ── 2. a change in the ask re-opens selection ────────────────────────────────


def test_ask_mode_reads_intent_then_response_type():
    assert ask_mode("introspective", "question") == "seek"
    assert ask_mode("informative", "chitchat") == "share"
    assert ask_mode("task", "follow_up") == ""  # a continuation keeps the lock
    assert ask_mode("informative", "") == "seek"
    assert ask_mode("introspective", "") == "share"
    assert ask_mode("", "") == ""


def _locked(leaf="haven_reflective_listening", ask="share", turns=1):
    return ActiveSkillContext(
        category="partner",
        current_leaf=leaf,
        anchor_topic_embedding=[1.0, 0.0],
        turns_active=turns,
        anchor_ask=ask,
    )


def _haven_pair():
    return _selector(
        _entry("haven_reflective_listening", [1.0, 0.0], partner=True),
        _entry("haven_problem_solving", [0.9, 0.44], partner=True),
        embed=[1.0, 0.0],
    )


async def test_asking_for_help_reopens_a_listening_lock():
    # "so what can i do?" — same topic (drift 1.0), but now a question.
    sel = _haven_pair()
    sel._conversational_cell.call = AsyncMock(return_value='{"skill": "haven_problem_solving"}')
    with bind_turn_agent(EMPATH):
        bundle, active, extras = await sel.select_conversational(
            "so what can i do?",
            {"response_type": "informative"},
            "struggling",
            [],
            _locked(),
            intent="question",
        )
    assert extras["sticky_action"] == "ask_shifted" and extras["ask"] == "share->seek"
    assert active is None
    assert bundle.chosen == ["haven_problem_solving"]


async def test_same_ask_keeps_the_lock():
    sel = _haven_pair()
    with bind_turn_agent(EMPATH):
        bundle, active, extras = await sel.select_conversational(
            "and it keeps happening",
            {"response_type": "introspective"},
            "sad",
            [],
            _locked(),
            intent="chitchat",
        )
    assert extras["sticky_action"] == "reused"
    assert bundle.pick_source == "active_reuse" and active.turns_active == 2


async def test_continuation_keeps_the_lock_and_lock_adopts_first_read():
    sel = _haven_pair()
    lock = _locked(ask="")
    with bind_turn_agent(EMPATH):
        bundle, active, _ = await sel.select_conversational(
            "go on", {"response_type": "task"}, "sad", [], lock, intent="follow_up"
        )
        assert bundle.pick_source == "active_reuse" and active.anchor_ask == ""
        bundle, active, _ = await sel.select_conversational(
            "it hurts", {"response_type": "introspective"}, "sad", [], lock, intent="chitchat"
        )
    assert bundle.pick_source == "active_reuse" and active.anchor_ask == "share"


async def test_gated_and_direct_match_turns_age_the_lock():
    sel = _selector(
        _entry("trading-analyst", [0.0, 1.0], native=True, keywords=["stocks"]),
        embed=[1.0, 0.0],
    )
    lock = _locked(leaf="decision-premortem")
    await sel.select_conversational("ok", {"response_type": "chitchat"}, "neutral", [], lock)
    await sel.select_conversational("stocks", {}, "", [], lock)
    assert lock.turns_active == 3


async def test_lock_expires_after_the_budget_in_conversation_turns():
    sel = _haven_pair()
    lock = _locked(turns=STICKY_TURN_BUDGET)
    with bind_turn_agent(EMPATH):
        _, _, extras = await sel.select_conversational(
            "more", {"response_type": "introspective"}, "sad", [], lock, intent="chitchat"
        )
    assert extras["sticky_action"] == "drifted_or_expired"


def test_build_active_context_records_the_ask():
    sel = _haven_pair()
    bundle = SkillBundle(tier1=[], chosen=["haven_reflective_listening"])
    ctx = sel.build_active_context(bundle, [1.0, 0.0], ask="share")
    assert ctx.anchor_ask == "share"


def _frontal_with(sel, parietal):
    from brain.clusters.frontal import FrontalCluster

    f = FrontalCluster.__new__(FrontalCluster)
    f._current_skill_bundle = None
    f._current_query_vec = None
    f.last_turn_skills = {}
    f._router = SimpleNamespace(embed=AsyncMock(return_value=None))
    f._parietal = parietal
    f._skill_selector = sel
    return f


def test_frontal_clears_a_released_lock_and_notes_why():
    sel = _haven_pair()
    sel.select_conversational = AsyncMock(
        return_value=(
            SkillBundle(tier1=[], chosen=[], pick_source="llm_null"),
            None,
            {"pick_path": "llm_null", "sticky_action": "ask_shifted", "ask": "share->seek"},
        )
    )
    store = {"ctx": _locked()}
    parietal = SimpleNamespace(
        active_skill_context=store["ctx"],
        recent_turns=lambda n: [],
        set_active_skill_context=lambda a: store.__setitem__("ctx", a),
    )
    f = _frontal_with(sel, parietal)
    with bind_turn_agent(EMPATH):
        asyncio.run(
            f._select_skills_for_turn(
                {"raw_text": "so what can i do?", "intent": "question", "sentiment": -0.4},
                {"response_type": "informative"},
                "t1",
            )
        )
    assert store["ctx"] is None
    assert f.last_turn_skills["note"] == "sticky_action=ask_shifted ask=share->seek"
    kwargs = sel.select_conversational.await_args.kwargs
    assert kwargs["intent"] == "question" and kwargs["sentiment"] == -0.4


def test_skills_log_line_carries_the_note():
    from brain.session_turn import _TurnMixin

    s = SimpleNamespace(
        frontal=SimpleNamespace(
            last_turn_skills={"turn_id": "t", "path": "llm_pick", "chosen": ["x"], "note": "n=1"}
        )
    )
    assert _TurnMixin._turn_skill_summary(s, "t")["note"] == "n=1"


# ── executive manifest ────────────────────────────────────────────────────────


def _haven_ten():
    return [_entry(f"haven_{i}", [1.0, 0.0], partner=True) for i in range(10)]


def test_manifest_lists_every_org_skill_first_and_hides_switched_off(_no_agent_store):
    _no_agent_store[EMPATH] = (frozenset({"supabase"}), False)
    sel = _selector(
        _entry("supabase", [0.0, 1.0], native=True),
        _entry("trading-analyst", [0.0, 1.0], native=True),
        _entry("supabase-postgres-best-practices", [0.0, 1.0], native=True),
        *_haven_ten(),
        _entry("decision", [0.0, 1.0], router=True),
    )
    with bind_turn_agent(EMPATH):
        m = sel.capability_manifest()
    lines = [ln.strip().split(":")[0] for ln in m.splitlines()]
    assert lines[1:11] == [f"haven_{i}" for i in range(10)]
    assert "supabase" not in lines and "trading-analyst" in lines and "decision" in lines


def test_manifest_and_selection_honour_org_only(_no_agent_store):
    _no_agent_store[EMPATH] = (frozenset(), True)
    sel = _selector(
        _entry("trading-analyst", [0.0, 1.0], native=True, keywords=["stocks"]),
        _entry("decision", [0.0, 1.0], router=True),
        *_haven_ten(),
    )
    with bind_turn_agent(EMPATH):
        m = sel.capability_manifest()
        assert "trading-analyst" not in m and "decision" not in m and "haven_9" in m
        assert not sel.allowed_for_current_agent("trading-analyst")
        assert not sel.allowed_for_current_agent("decision")
        assert sel.allowed_for_current_agent("haven_0")
        assert sel._index.keyword_match("trading stocks", allowed=sel._agent_allowed) is None
    # Other agents and the owner's own chat keep the built-ins.
    assert sel.allowed_for_current_agent("trading-analyst")


def test_frontal_builds_the_manifest_per_turn_for_the_turn_agent(_no_agent_store):
    _no_agent_store[EMPATH] = (frozenset({"trading-analyst"}), False)
    sel = _selector(_entry("trading-analyst", [0.0, 1.0], native=True), *_haven_ten())
    f = _frontal_with(sel, None)
    assert "trading-analyst" not in f._turn_skill_manifest({"agent_id": EMPATH})
    assert "trading-analyst" in f._turn_skill_manifest({})


# ── 5. skills_org_only storage ───────────────────────────────────────────────


def test_skill_policy_reads_org_only(monkeypatch):
    monkeypatch.undo()  # the real readers, over a fake permissions store
    monkeypatch.setattr(
        agents, "permissions", lambda aid: {"skills_disabled": "a", "skills_org_only": "true"}
    )
    agents._skills_disabled_cache.clear()
    assert agents.disabled_skills(EMPATH) == {"a"} and agents.org_skills_only(EMPATH)
    assert "skills_org_only" in agents.PERMISSION_KEYS
    agents._skills_disabled_cache.clear()


# ── 3. restoring a conversation after a restart ───────────────────────────────


class _Rows:
    def __init__(self, rows=None, boom=None):
        self.rows, self.boom, self.calls = rows or [], boom, []

    def table(self, name):
        self.calls.append(("table", name))
        return self

    def select(self, *a):
        return self

    def eq(self, k, v):
        self.calls.append((k, v))
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def execute(self):
        if self.boom:
            raise self.boom
        return SimpleNamespace(data=self.rows)


def _lane(**kw):
    return bind_turn(
        "agent",
        session_id="s1",
        agent_id=kw.get("agent", EMPATH),
        end_user_id=kw.get("end_user", "u1"),
        partner_id="p1",
    )


def test_recent_for_lane_reads_the_agent_and_end_user(monkeypatch):
    from brain import agent_log

    db = _Rows([{"prompt": "hi", "response": "hello", "ts": "2026-10-11T19:33:43+00:00"}])
    monkeypatch.setattr(agent_log, "_sb", lambda: (db, "org"))
    rows = agent_log.recent_for_lane(EMPATH, "u1")
    assert rows == [
        {"user_input": "hi", "entity_response": "hello", "ts": "2026-10-11T19:33:43+00:00"}
    ]
    assert ("agent_id", EMPATH) in db.calls and ("end_user_id", "u1") in db.calls
    assert agent_log.recent_for_lane("", "u1") is None


def test_lane_recall_prefers_the_turn_log_then_episodes(monkeypatch):
    from brain import agent_log

    episodic = SimpleNamespace(recall_recent=lambda limit, end_user_id: [{"user_input": "ep"}])
    recall = make_lane_recall(episodic)
    monkeypatch.setattr(agent_log, "_sb", lambda: (_Rows([{"prompt": "log"}]), "org"))
    with _lane():
        assert recall("u1")[0]["user_input"] == "log"
    monkeypatch.setattr(agent_log, "_sb", lambda: (_Rows([]), "org"))
    with _lane():
        assert recall("u1") == [{"user_input": "ep"}]
    # A store error with no fallback history raises, so the lane retries.
    monkeypatch.setattr(agent_log, "_sb", lambda: (_Rows(boom=RuntimeError("down")), "org"))
    empty = make_lane_recall(SimpleNamespace(recall_recent=lambda limit, end_user_id: []))
    with _lane(), pytest.raises(RuntimeError):
        empty("u1")


def test_restart_restores_the_last_question_so_the_answer_lands():
    # The 2026-10-12 shape: the listener's last reply asked a question, the process
    # restarted, and the person's next message answers it.
    p = ParietalCluster(bus=None)
    history = [
        {"user_input": "I can't walk away", "entity_response": "Is it that things will get worse?"},
        {"user_input": "they keep arguing", "entity_response": "What do you need then?"},
    ]
    with _lane():
        asyncio.run(p.ensure_lane_seeded(lambda eu: history))
        text = p.recent_turns_text()
    assert text.index("they keep arguing") < text.index("I can't walk away")
    assert text.endswith("Brain: Is it that things will get worse?")


def test_failed_seed_retries_and_goes_in_front_of_newer_turns():
    p = ParietalCluster(bus=None)
    calls = {"n": 0}

    def flaky(eu):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("store down")
        return [{"user_input": "older", "entity_response": "old reply"}]

    with _lane():
        asyncio.run(p.ensure_lane_seeded(flaky))
        p.update({"intent": "chitchat"}, "newer", "new reply")
        asyncio.run(p.ensure_lane_seeded(flaky))
        asyncio.run(p.ensure_lane_seeded(flaky))  # seeded now: no third read
        users = [t["user"] for t in p.recent_turns(6)]
    assert users == ["older", "newer"] and calls["n"] == 2


def test_seed_merge_skips_turns_already_held_and_keeps_ring_size():
    p = ParietalCluster(bus=None)
    with _lane():
        for i in range(parietal_mod.RING_SIZE):
            p.update({}, f"u{i}", f"r{i}")
        st = p._state()
        p._seed_into(st, [{"user_input": "u0", "entity_response": "r0"}], merge=True)
        assert [t["user"] for t in p.recent_turns(6)] == [f"u{i}" for i in range(6)]
        p._seed_into(st, [{"user_input": "x", "entity_response": "y"}], merge=True)
        assert [t["user"] for t in p.recent_turns(6)] == [f"u{i}" for i in range(6)]


def test_ws_turns_reach_the_turn_log():
    import inspect

    from brain.api import ws

    src = inspect.getsource(ws.WsSession._run_turn_locked)
    assert "agent_log.record" in src


# ── 4. mandate reply rules ────────────────────────────────────────────────────


def _fence(label, content, nonce):
    return f"<{label}:{nonce}>{content}</{label}:{nonce}>"


_CAT = {
    "therapy_listener": {"text": "Two to four sentences, under 80 words.", "conduct": None},
    "other": {"text": "Long answers.", "conduct": None},
}


def test_active_assignment_block_holds_only_the_active_one():
    from brain.persona_context import active_assignment_block

    block = active_assignment_block("therapy_listener", _CAT, _fence, "n1")
    assert "<assignment_therapy_listener:n1>Two to four sentences" in block
    assert "Long answers" not in block and "bind every reply" in block
    assert active_assignment_block("missing", _CAT, _fence, "n1") == ""
    assert active_assignment_block(None, _CAT, _fence, "n1") == ""


def test_catalog_framing_makes_reply_rules_binding_over_target_length():
    from brain.persona_context import mandate_catalog_block

    block = mandate_catalog_block(_CAT, _fence, "n1").lower()
    assert "binding" in block and "wins over any target_length" in block


def test_prompts_put_the_assignment_limit_above_target_length():
    from brain.clusters.frontal_prompts import CRITIC_SYSTEM, DRAFTER_SYSTEMS, EXECUTIVE_SYSTEM

    assert '"fit": float' in CRITIC_SYSTEM and "Count the sentences and words" in CRITIC_SYSTEM
    assert "never detailed" in EXECUTIVE_SYSTEM
    assert all("wins over\ntarget_length" in d for d in DRAFTER_SYSTEMS)


def _frontal_for_cells(monkeypatch):
    from brain import mandates
    from brain.clusters.frontal import FrontalCluster

    monkeypatch.setattr(mandates, "catalog", lambda: _CAT)
    f = FrontalCluster.__new__(FrontalCluster)
    f._wiring = None
    f._skill_selector = None
    f._skill_manifest = ""
    return f


def test_executive_gets_the_active_assignment_as_cached_context(monkeypatch):
    f = _frontal_for_cells(monkeypatch)
    cell = SimpleNamespace(reset_turn=lambda t: None, call=AsyncMock(return_value="{}"))
    monkeypatch.setattr(f, "_build_exec_context", lambda *a: "ctx")
    monkeypatch.setattr(f, "_inject_host_fragments", lambda c, h, t: c)
    asyncio.run(
        f._run_executive_llm({"mandate_id": "therapy_listener"}, {}, {}, "", {}, "t1", cell=cell)
    )
    assert "Two to four sentences" in cell.call.await_args.kwargs["cached_context"]


def test_critic_gets_the_active_assignment_as_cached_context(monkeypatch):
    f = _frontal_for_cells(monkeypatch)
    f._critic = SimpleNamespace(call=AsyncMock(return_value='{"overall": 0.4, "fit": 0.2}'))
    monkeypatch.setattr(f, "_inject_host_fragments", lambda c, h, t: c)
    monkeypatch.setattr(f, "_apply_judge_gates", lambda h, v, k: v)
    ctx = f._active_assignment_context({"mandate_id": "therapy_listener"})
    verdict = asyncio.run(f._score_draft("draft", "context", "t1", ctx))
    assert verdict["fit"] == 0.2
    assert f._critic.call.await_args.kwargs["cached_context"] == ctx
    # Byte-stable across calls, so the prompt cache holds.
    assert f._active_assignment_context({"mandate_id": "therapy_listener"}) == ctx
    assert f._active_assignment_context({}) == ""


@pytest.mark.parametrize(
    "raw,clean",
    [
        ("you can't *talk* to him", "you can't talk to him"),
        ("**this** matters, *really*.", "this matters, really."),
        ("2 * 3 * 4", "2 * 3 * 4"),
        ("a lone * here", "a lone * here"),
        ("snake_case and __init__", "snake_case and __init__"),
        ("[mood:warm]*hey*[/mood]", "[mood:warm]hey[/mood]"),
    ],
)
def test_reply_emphasis_is_stripped(raw, clean):
    from brain.session_turn import _strip_emphasis

    assert _strip_emphasis(raw) == clean
