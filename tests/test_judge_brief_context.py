"""The frontal judges read the drafters' brief (docs/JUDGE_CONTEXT_DESIGN.md).

On (judge_brief_context=1, the default): the critic and the empathy critic run with
JUDGE_PREFIX + the drafters' cached block as one shared cached prefix, the rubric in
the message, and no cache write on the per-draft message. Off: every judge call is
byte-for-byte what it was before.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from brain.cell import IntegratorCell
from brain.clusters.frontal import FrontalCluster
from brain.clusters.frontal_prompts import CRITIC_RUBRIC, EMPATHY_RUBRIC, JUDGE_PREFIX
from brain.settings import settings


class _Cell:
    """Records each call's message and keyword arguments; returns a fixed verdict."""

    def __init__(self, verdict: dict):
        self.verdict = verdict
        self.calls: list[dict] = []
        self.model = "haiku"
        self.locality = "cloud"

    def reset_turn(self, turn_id):
        pass

    async def call(self, messages, **kw):
        self.calls.append({"content": messages[0]["content"], "kw": kw})
        return json.dumps(self.verdict)


def _frontal():
    f = FrontalCluster.__new__(FrontalCluster)
    f._critic = _Cell({"overall": 0.8, "brief_fit": 0.7, "veto": False})
    f._empathy_critic = _Cell({"empathy_score": 0.6, "user_need": "be heard", "veto": False})
    f._wiring = None
    f._skill_selector = None
    f._turn_user_emotion = "sad"
    f._turn_hostility = 0.0
    return f


FEATURES = {
    "raw_text": "what's up with you this week?",
    "user_emotion": "curious",
    "input_modality": "voice",
    "user_register": "casual",
}
PARIETAL = "User: hi\nBrain: Hey, I'm Farrah."
CACHED = "Entity self-model:\n<data>I am Farrah.</data>\nAssignments: [haven_friend] ..."


def _stamp(f, monkeypatch, on: bool):
    monkeypatch.setitem(settings._data, "judge_brief_context", 1 if on else 0)
    f._stamp_judge_brief(CACHED, FEATURES, {"vocal_tone": "energetic"}, PARIETAL)


def test_flag_off_is_the_old_prompt_exactly(monkeypatch):
    f = _frontal()
    _stamp(f, monkeypatch, on=False)
    assert f._judge_brief is None
    asyncio.run(f._score_draft("a draft", "the drafter prompt", "t1"))
    asyncio.run(f._run_empathy_check("a draft", "sad", "t1"))
    (c,) = f._critic.calls
    (e,) = f._empathy_critic.calls
    assert c["content"] == FrontalCluster._critic_prompt("a draft", "the drafter prompt")
    assert e["content"] == FrontalCluster._empathy_prompt("a draft", "sad")
    assert c["kw"] == {} and e["kw"] == {}


def test_flag_on_critic_reads_the_brief(monkeypatch):
    f = _frontal()
    _stamp(f, monkeypatch, on=True)
    verdict = asyncio.run(f._score_draft("a draft", "the drafter prompt", "t1"))
    (c,) = f._critic.calls
    assert c["content"].startswith(CRITIC_RUBRIC)
    assert "the drafter prompt" in c["content"] and "a draft" in c["content"]
    assert c["kw"] == {
        "system_override": JUDGE_PREFIX,
        "cached_context": CACHED,
        "cache_last_message": False,
    }
    assert verdict["brief_fit"] == 0.7


def test_flag_on_empathy_reads_the_conversation_and_shares_the_prefix(monkeypatch):
    f = _frontal()
    _stamp(f, monkeypatch, on=True)
    asyncio.run(f._score_draft("a draft", "the drafter prompt", "t1"))
    verdict = asyncio.run(f._run_empathy_check("a draft", "curious", "t1"))
    (e,) = f._empathy_critic.calls
    assert e["content"].startswith(EMPATHY_RUBRIC)
    for piece in ("Hey, I'm Farrah.", "what's up with you this week?", "curious", "by voice"):
        assert piece in e["content"]
    assert "the drafter prompt" not in e["content"]  # its own lean context, not the drafters'
    # One cache entry serves both judges.
    assert e["kw"] == f._critic.calls[0]["kw"]
    assert verdict["user_need"] == "be heard"


def test_judge_prefix_carries_the_house_rules():
    from brain.clusters.frontal_prompts import _DRAFTER_IDENTITY

    assert _DRAFTER_IDENTITY in JUDGE_PREFIX
    assert "Precedence" in JUDGE_PREFIX


def test_turn_context_without_voice_or_history():
    ctx = FrontalCluster._judge_turn_context({"raw_text": "hello"}, {}, "")
    assert "Recent conversation" not in ctx and "by voice" not in ctx
    assert "hello" in ctx and "neutral" in ctx


class _Router:
    def __init__(self):
        self.calls: list[tuple] = []

    async def call(self, model, system, messages, **kw):
        self.calls.append((model, system, kw))
        return ""


def _warm_frontal(monkeypatch, idle=240.0):
    f = _frontal()
    f._router = _Router()
    monkeypatch.setitem(settings._data, "judge_cache_warm_idle_s", idle)
    _stamp(f, monkeypatch, on=True)
    return f


def test_cache_warm_fires_once_per_idle_window(monkeypatch):
    f = _warm_frontal(monkeypatch)

    async def turn():
        f._maybe_warm_judge_cache("t1")
        f._maybe_warm_judge_cache("t2")  # same window: no second warm
        await f._judge_warm_task

    asyncio.run(turn())
    assert len(f._router.calls) == 1
    model, system, kw = f._router.calls[0]
    assert system == JUDGE_PREFIX
    assert kw["cached_context"] == CACHED and kw["max_tokens"] == 1
    assert kw["cell"] == "judge_cache_warm"  # its cost is attributed apart from the critic


@pytest.mark.parametrize("idle", [0.0])
def test_cache_warm_off(monkeypatch, idle):
    f = _warm_frontal(monkeypatch, idle=idle)

    async def turn():
        f._maybe_warm_judge_cache("t1")

    asyncio.run(turn())
    assert f._router.calls == []


def test_cache_warm_skipped_when_brief_off(monkeypatch):
    f = _warm_frontal(monkeypatch)
    _stamp(f, monkeypatch, on=False)

    async def turn():
        f._maybe_warm_judge_cache("t1")

    asyncio.run(turn())
    assert f._router.calls == []


class _ShadowCell(_Cell):
    """A dataclass-free stand-in: shadow_tasks.shadow_cell returns it as-is."""


def _shadow_frontal(monkeypatch, cached: str):
    f = _frontal()
    f._critic = _ShadowCell({"overall": 0.8})
    f._local_available = lambda: True
    f._fragment_block_for_ids = lambda ids: "<<candidate>>"
    monkeypatch.setitem(settings._data, "judge_brief_context", 1)
    f._stamp_judge_brief(cached, FEATURES, {}, PARIETAL)
    return f


def test_shadow_pair_uses_the_brief(monkeypatch):
    f = _shadow_frontal(monkeypatch, CACHED)
    pair = asyncio.run(
        f._judge_shadow_pair(
            "frontal.critic", "a draft", "sad", "t1", "sid", "overall", "ctx", brief=f._judge_brief
        )
    )
    assert pair is not None
    base, cand = f._critic.calls
    assert base["content"].startswith(CRITIC_RUBRIC)
    assert cand["content"].endswith("<<candidate>>")
    assert base["kw"]["system_override"] == JUDGE_PREFIX
    assert base["kw"]["locality_override"] == "local"


def test_shadow_pair_skips_a_brief_longer_than_the_pod_window(monkeypatch):
    monkeypatch.setitem(settings._data, "runpod_num_ctx", 4096)
    f = _shadow_frontal(monkeypatch, "x" * 40_000)
    pair = asyncio.run(
        f._judge_shadow_pair(
            "frontal.critic", "a draft", "sad", "t1", "sid", "overall", "ctx", brief=f._judge_brief
        )
    )
    assert pair is None and f._critic.calls == []


def test_cell_passes_overrides_only_when_set():
    seen: list[tuple] = []

    class R:
        async def call(self, model, system, messages, **kw):
            seen.append((system, kw))
            return "ok"

    cell = IntegratorCell(
        name="t", cluster="frontal", model="haiku", system_prompt="SYS", topics=[]
    )
    cell.set_router(R())
    asyncio.run(cell.call([{"role": "user", "content": "x"}]))
    cell.reset_turn("t2")
    asyncio.run(
        cell.call(
            [{"role": "user", "content": "x"}], system_override="OTHER", cache_last_message=False
        )
    )
    (s1, kw1), (s2, kw2) = seen
    assert s1 == "SYS" and "cache_last_message" not in kw1
    assert s2 == "OTHER" and kw2["cache_last_message"] is False


def test_anthropic_request_skips_last_message_cache_when_asked():
    from brain.model_router import ModelRouter

    sent: list[dict] = []

    class _Msgs:
        async def create(self, **kw):
            sent.append(kw)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="{}")],
                stop_reason="end_turn",
                usage=SimpleNamespace(
                    input_tokens=1,
                    output_tokens=1,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            )

    r = ModelRouter.__new__(ModelRouter)
    r._get_anthropic = lambda: SimpleNamespace(messages=_Msgs())
    r.note_provider_error = lambda *a, **k: None
    r.note_provider_success = lambda *a, **k: None
    msgs = [{"role": "user", "content": "draft"}]
    asyncio.run(r._call_anthropic("claude-haiku-4-5-20251001", "S", msgs, 10, cached_context="C"))
    asyncio.run(
        r._call_anthropic(
            "claude-haiku-4-5-20251001", "S", msgs, 10, cached_context="C", cache_last_message=False
        )
    )
    cached, uncached = sent
    assert cached["messages"][0]["content"][0].get("cache_control")
    assert uncached["messages"][0]["content"] == "draft"
    # The system blocks still carry their breakpoints either way.
    assert all(b.get("cache_control") for b in uncached["system"])
