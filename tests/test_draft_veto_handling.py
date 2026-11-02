"""
Regression tests for two veto-handling bugs in reply-draft selection (frontal).

  * A long veto_reason ran the critic past its token cap; the truncated JSON failed
    to parse, the default `{"overall": 0.5, "veto": False}` replaced it, and the
    vetoed draft competed at 0.5. The verdict's scalars are now recovered from the
    truncated reply, and a verdict that is truly missing is NO OPINION, not 0.5.
  * When every draft was vetoed, the single-draft path endorsed drafts[0] (vetoed)
    at a placeholder 0.8 and overwrote the veto records. A fresh fallback reply is
    now written instead, and the veto records survive.
"""

from __future__ import annotations

from brain.clusters.frontal import _parse_judge_verdict
from brain.settings import settings
from tests.test_gating_shadow import (
    _AFFECT,
    _CHEM,
    _EXEC_SIG,
    _FEATURES,
    _INSTRUCTION,
    _NM,
    _frontal_with_drafters,
    _run,
    _StubBrainstem,
    _StubExecutive,
)

# A real-shaped critic veto, cut off mid-explanation by the token cap.
_TRUNCATED_VETO = (
    '{"coherence": 0.3, "relevance": 0.2, "tone_fit": 0.4, "craft": 0.3, '
    '"overall": 0.25, "veto": true, "veto_reason": "The draft tells the user they '
    "said something they never said and then builds the whole reply on that"
)

# ── parsing ────────────────────────────────────────────────────────────────────


def test_truncated_veto_keeps_the_veto_and_the_scores():
    v = _parse_judge_verdict(_TRUNCATED_VETO)
    assert v["veto"] is True
    assert v["overall"] == 0.25 and v["coherence"] == 0.3
    assert v["truncated"] is True


def test_a_number_quoted_in_a_later_string_cannot_override_a_score():
    raw = '{"overall": 0.2, "veto": true, "veto_reason": "claims \\"overall\\": 0.9 a'
    assert _parse_judge_verdict(raw)["overall"] == 0.2


def test_complete_verdicts_parse_unchanged_and_garbage_is_none():
    v = _parse_judge_verdict('{"overall": 0.9, "veto": false, "veto_reason": ""}')
    assert v == {"overall": 0.9, "veto": False, "veto_reason": ""}
    assert _parse_judge_verdict("") is None
    assert _parse_judge_verdict("no json here") is None
    assert _parse_judge_verdict('{"veto_reason": "cut') is None


def test_critic_prompt_bounds_the_veto_reason():
    from brain.clusters.frontal_prompts import CRITIC_SYSTEM

    assert '"" unless veto' in CRITIC_SYSTEM and "12 words" in CRITIC_SYSTEM


# ── _score_draft ───────────────────────────────────────────────────────────────


def _critic_frontal(critic_reply: str):
    from brain.observability.timeline import TurnTrace

    f = _frontal_with_drafters(_StubExecutive("{}"), TurnTrace("t", "s", "hi"))
    del f._score_draft  # the harness stubs it; these tests drive the real one
    f._critic = _StubExecutive(critic_reply)
    f._critic_prompt = lambda draft, context: "prompt"
    f._inject_host_fragments = lambda prompt, host, turn_id: prompt
    f._apply_judge_gates = lambda host, verdict, field: verdict
    return f


def test_score_draft_keeps_a_truncated_veto():
    f = _critic_frontal(_TRUNCATED_VETO)
    # Before: {"overall": 0.5, "veto": False} — the veto was lost.
    v = _run(f._score_draft("draft", "ctx", "t1"))
    assert v["veto"] is True and v["overall"] == 0.25
    assert v["veto_reason"] == "verdict_truncated"


def test_score_draft_with_no_verdict_is_no_opinion():
    f = _critic_frontal("")
    v = _run(f._score_draft("draft", "ctx", "t1"))
    assert v["overall"] is None and v["veto"] is False and v["unavailable"] is True


def test_shadow_pair_reads_a_truncated_vetoing_arm(monkeypatch):
    """_judge_shadow_pair parses like the live critic: a candidate arm that vetoes
    at length is still a comparison, not a discarded turn."""
    import brain.clusters.frontal as frontal_mod

    f = _critic_frontal("")
    replies = iter(['{"overall": 0.7, "veto": false, "veto_reason": ""}', _TRUNCATED_VETO])

    class _Cell:
        def reset_turn(self, turn_id):
            pass

        async def call(self, messages, **kw):
            return next(replies)

    f._critic = _Cell()
    f._local_available = lambda: True
    f._fragment_block_for_ids = lambda ids: "<fenced>"
    monkeypatch.setattr(frontal_mod.shadow_tasks, "shadow_cell", lambda cell: cell)
    pair = _run(f._judge_shadow_pair("frontal.critic", "d", "sad", "t1", "sid", "overall"))
    assert pair is not None
    base, cand, veto = pair
    assert veto is True and cand <= 0.25 < base


# ── selection ──────────────────────────────────────────────────────────────────


class _VetoBrainstem(_StubBrainstem):
    def __init__(self):
        super().__init__()
        self.vetoed = set()

    def veto(self, draft_id):
        self.vetoed.add(draft_id)


class _FallbackDrafter:
    def __init__(self, reply: str):
        self.reply = reply
        self.prompts: list[str] = []

    def reset_turn(self, turn_id):
        pass

    async def call(self, messages, **kw):
        self.prompts.append(messages[-1]["content"])
        return self.reply


def _selecting_frontal(score_fn, fallback_reply="A careful fallback."):
    from brain.observability.timeline import TurnTrace

    f = _frontal_with_drafters(_StubExecutive("{}"), TurnTrace("t", "s", "hi"))
    f._brainstem = _VetoBrainstem()
    fallback = _FallbackDrafter(fallback_reply)
    f._drafters = [fallback, None, None]
    f._score_draft = score_fn
    f._stamp_judge_floor_inputs = lambda *a, **k: None
    return f, fallback


def _drive(f, turn_id, n=3):
    return _run(
        f._run_drafters_and_select(
            _NM,
            _CHEM,
            _EXEC_SIG,
            dict(_INSTRUCTION, drafter_count=n),
            _FEATURES,
            _AFFECT,
            {},
            "",
            turn_id,
        )
    )


def _with_colony_off(fn):
    settings.update({"colony_features": 0})
    try:
        return fn()
    finally:
        settings.update({"colony_features": 1})


def test_when_every_draft_is_vetoed_a_vetoed_draft_is_never_spoken():
    async def score(text, prompt, turn_id):
        return {"overall": 0.2, "veto": True, "veto_reason": "invents a fact"}

    f, fallback = _selecting_frontal(score)
    text = _with_colony_off(lambda: _drive(f, "t-allveto"))

    assert text == "A careful fallback."
    assert not text.startswith("draft text")
    # Only the fallback was endorsed; every draft stayed vetoed.
    assert f._brainstem.endorsed == ["veto_fallback_t-allveto"]
    assert f._brainstem.vetoed == {"draft0", "draft1", "draft2"}
    # The veto records survive, plus one selected fallback entry no learner trusts.
    scores = f.last_turn_draft_scores
    assert [e["draft_id"] for e in scores if e["vetoed"]] == ["draft0", "draft1", "draft2"]
    (selected,) = [e for e in scores if e["selected"]]
    assert selected["draft_id"] == "veto_fallback_t-allveto"
    assert selected["critic_ran"] is False and selected["vetoed"] is False
    assert selected["overall"] == 0.0
    # The fallback drafter was told why the drafts were held back.
    assert "invents a fact" in fallback.prompts[0]
    # Nothing measured a score for this turn's reply, so no predictor learned from it.
    assert f._exec_predictor.avg_recent_outcome(_EXEC_SIG) is None


def test_all_vetoed_with_an_empty_fallback_draft_uses_the_template_line():
    async def score(text, prompt, turn_id):
        return {"overall": 0.2, "veto": True, "veto_reason": "harmful"}

    f, _ = _selecting_frontal(score, fallback_reply="")
    text = _with_colony_off(lambda: _drive(f, "t-empty"))
    assert text == "I'm not sure how to respond to that."
    assert f._brainstem.endorsed == ["veto_fallback_t-empty"]


def test_a_truncated_veto_no_longer_competes_at_half():
    """End to end through the real _score_draft: before, draft0's truncated veto
    parsed as 0.5 and beat draft1's honest 0.4."""
    f, _ = _selecting_frontal(None)
    replies = {
        "draft text 0": _TRUNCATED_VETO,
        "draft text 1": '{"overall": 0.4, "veto": false, "veto_reason": ""}',
    }

    class _Critic:
        def reset_turn(self, turn_id):
            pass

        async def call(self, messages, **kw):
            return replies[messages[-1]["content"]]

    del f._score_draft  # use the real method
    f._critic = _Critic()
    f._critic_prompt = lambda draft, context: draft
    f._inject_host_fragments = lambda prompt, host, turn_id: prompt
    f._apply_judge_gates = lambda host, verdict, field: verdict

    text = _with_colony_off(lambda: _drive(f, "t-trunc", n=2))
    assert text == "draft text 1"
    assert f._brainstem.vetoed == {"draft0"}


def test_an_unrated_draft_ranks_below_every_rated_one():
    async def score(text, prompt, turn_id):
        if text.endswith("0"):
            return {"overall": None, "veto": False, "unavailable": True}
        return {"overall": 0.3, "veto": False}

    f, _ = _selecting_frontal(score)
    text = _with_colony_off(lambda: _drive(f, "t-unrated"))
    assert text in ("draft text 1", "draft text 2")
    (unrated,) = [e for e in f.last_turn_draft_scores if e["draft_id"] == "draft0"]
    assert unrated["critic_ran"] is False and unrated["selected"] is False


def test_with_no_rated_draft_the_first_unvetoed_one_is_spoken_and_records_kept():
    async def score(text, prompt, turn_id):
        if text.endswith("0"):
            return {"overall": 0.2, "veto": True, "veto_reason": "rude"}
        return {"overall": None, "veto": False, "unavailable": True}

    f, fallback = _selecting_frontal(score)
    text = _with_colony_off(lambda: _drive(f, "t-nocritic"))
    assert text == "draft text 1"
    assert fallback.prompts == []  # a speakable draft existed — no fallback written
    scores = {e["draft_id"]: e for e in f.last_turn_draft_scores}
    assert scores["draft0"]["vetoed"] is True
    assert scores["draft1"]["selected"] is True and scores["draft1"]["critic_ran"] is False
    assert f._exec_predictor.avg_recent_outcome(_EXEC_SIG) is None
