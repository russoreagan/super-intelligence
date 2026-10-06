"""Turn-latency pass (2026-10-05): the Claude 5 model update and the limits that
kept the designed pipeline from running.

Covers:
  * Sonnet 5.5 request shaping — explicit effort + thinking headroom on the 5-gen
    models, Haiku 4.5 untouched; reply text read by block type (a response can open
    with a thinking block); forced tool_choice replaced on models that 400 on it.
  * SUPERSEDED_DEFAULTS — a tenant settings.json still carrying a replaced default
    loads the new one; a deliberate value survives.
  * The critic scores EVERY draft (its per-turn cap covered only 2 of 5).
  * Critic and empathy run side by side, with veto precedence unchanged.
  * The hippocampus encoder no longer dials a container-local Ollama.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

import brain.model_router as mr
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
    _StubExecutive,
)

# ── model ids ──────────────────────────────────────────────────────────────────


def test_sonnet_key_resolves_to_sonnet_5_5():
    assert mr.MODEL_MAP["sonnet"] == "claude-sonnet-5-5"
    assert mr.MODEL_MAP["haiku"] == "claude-haiku-4-5-20251001"  # current Haiku, unchanged
    assert mr.MODEL_MAP["vertex-claude"] == "vertex-claude-sonnet-5-5"


def test_new_models_are_priced():
    assert mr._CLOUD_RATES["claude-sonnet-5-5"] == (2.0, 10.0, 0.20)
    assert mr._CLOUD_RATES["claude-opus-5-5"] == (4.0, 20.0, 0.20)


# ── request shaping ────────────────────────────────────────────────────────────


@pytest.fixture
def claude_settings(monkeypatch):
    monkeypatch.setitem(settings._data, "claude_effort", "low")
    monkeypatch.setitem(settings._data, "claude_structured_effort", "medium")
    monkeypatch.setitem(settings._data, "claude_thinking_headroom_tokens", 2048)


def test_effort_models_get_effort_and_thinking_headroom(claude_settings):
    mt, extra = mr._claude_request_shape("claude-sonnet-5-5", 512)
    assert mt == 512 + 2048
    assert extra == {"output_config": {"effort": "low"}}
    # Vertex ids carry the same shaping.
    _mt, v_extra = mr._claude_request_shape("vertex-claude-sonnet-5-5", 512)
    assert v_extra == {"output_config": {"effort": "low"}}


def test_structured_effort_key_is_separate(claude_settings):
    _mt, extra = mr._claude_request_shape(
        "claude-sonnet-5-5", 4096, effort_key="claude_structured_effort"
    )
    assert extra == {"output_config": {"effort": "medium"}}


def test_effort_setting_actually_changes_the_request(claude_settings, monkeypatch):
    monkeypatch.setitem(settings._data, "claude_effort", "high")
    _mt, extra = mr._claude_request_shape("claude-sonnet-5-5", 512)
    assert extra["output_config"]["effort"] == "high"
    monkeypatch.setitem(settings._data, "claude_thinking_headroom_tokens", 0)
    mt, _ = mr._claude_request_shape("claude-sonnet-5-5", 512)
    assert mt == 512


def test_haiku_request_is_unchanged(claude_settings):
    assert mr._claude_request_shape("claude-haiku-4-5-20251001", 768) == (768, {})
    assert mr._claude_request_shape("claude-sonnet-4-6", 512) == (512, {})


def test_forced_tool_rejection_set():
    assert mr._claude_rejects_forced_tool("claude-sonnet-5-5")
    assert mr._claude_rejects_forced_tool("vertex-claude-sonnet-5-5")
    assert not mr._claude_rejects_forced_tool("claude-haiku-4-5-20251001")


class _Block:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Resp:
    def __init__(self, content, stop_reason="end_turn", stop_details=None):
        self.content = content
        self.usage = _Usage()
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class _Recorder:
    """Fake AsyncAnthropic: records each create() kwargs and returns `resp`."""

    def __init__(self, resp):
        self.kwargs: list[dict] = []
        outer = self

        class _Messages:
            async def create(self, **kw):
                outer.kwargs.append(kw)
                return resp

        self.messages = _Messages()


def _bare_router():
    r = mr.ModelRouter.__new__(mr.ModelRouter)
    r._provider_outage = {}
    r._bg_mode = False
    r._bg_defer_reason = None
    r._local_disabled = False
    r._cloud_semaphore = None
    r._bg_cloud_semaphore = None
    return r


def test_reply_text_skips_a_leading_thinking_block(claude_settings, monkeypatch):
    r = _bare_router()
    rec = _Recorder(_Resp([_Block("thinking", thinking=""), _Block("text", text='{"ok": true}')]))
    monkeypatch.setattr(r, "_get_anthropic", lambda: rec)
    text, *_ = asyncio.run(
        r._call_anthropic("claude-sonnet-5-5", "sys", [{"role": "user", "content": "x"}], 512)
    )
    assert text == '{"ok": true}'
    kw = rec.kwargs[0]
    assert kw["output_config"] == {"effort": "low"}
    assert kw["max_tokens"] == 512 + 2048
    assert "thinking" not in kw and "temperature" not in kw  # adaptive default, no sampling


def test_haiku_call_sends_no_effort(claude_settings, monkeypatch):
    r = _bare_router()
    rec = _Recorder(_Resp([_Block("text", text="hi")]))
    monkeypatch.setattr(r, "_get_anthropic", lambda: rec)
    asyncio.run(
        r._call_anthropic(
            "claude-haiku-4-5-20251001", "sys", [{"role": "user", "content": "x"}], 768
        )
    )
    kw = rec.kwargs[0]
    assert "output_config" not in kw and kw["max_tokens"] == 768


def test_refusal_is_logged_not_silent(caplog):
    resp = _Resp([], stop_reason="refusal", stop_details=_Block("refusal", category="bio"))
    with caplog.at_level(logging.WARNING, logger="brain.model_router"):
        assert mr._claude_text(resp, "frontal/executive") == ""
    assert "refusal" in caplog.text and "bio" in caplog.text


def _structured_router(monkeypatch, rec):
    r = _bare_router()
    monkeypatch.setattr(r, "_get_anthropic", lambda: rec)
    monkeypatch.setattr(r, "_enforce_cloud_budget", lambda *a, **k: False)
    monkeypatch.setattr(r, "_charge_cloud_usd", lambda *a, **k: None)
    return r


def test_structured_call_on_sonnet_5_5_steers_instead_of_forcing(claude_settings, monkeypatch):
    rec = _Recorder(_Resp([_Block("tool_use", name="create_plan", input={"steps": [1]})]))
    r = _structured_router(monkeypatch, rec)
    out = asyncio.run(
        r.call_structured(
            "sonnet",
            "plan it",
            [{"role": "user", "content": "goal"}],
            "create_plan",
            "Return a plan.",
            {"type": "object", "properties": {"steps": {"type": "array"}}},
            cluster="motor",
            cell="strategic_planner",
        )
    )
    assert out == {"steps": [1]}
    kw = rec.kwargs[0]
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["tool_choice"] == {"type": "auto"}  # forced choice is a 400 on 5.5
    assert "create_plan" in kw["system"][0]["text"]  # ...so the prompt says to call it
    assert kw["output_config"] == {"effort": "medium"}


def test_structured_call_on_haiku_still_forces_the_tool(claude_settings, monkeypatch):
    rec = _Recorder(_Resp([_Block("tool_use", name="extract", input={"a": 1})]))
    r = _structured_router(monkeypatch, rec)
    out = asyncio.run(
        r.call_structured(
            "haiku",
            "extract",
            [{"role": "user", "content": "text"}],
            "extract",
            "Return fields.",
            {"type": "object"},
            cluster="api",
            cell="extract",
        )
    )
    assert out == {"a": 1}
    kw = rec.kwargs[0]
    assert kw["tool_choice"] == {"type": "tool", "name": "extract"}
    assert "output_config" not in kw
    assert kw["system"][0]["text"] == "extract"  # prompt untouched


# ── superseded defaults ────────────────────────────────────────────────────────


def _load_from(tmp_path, monkeypatch, on_disk: dict):
    import brain.settings as bs

    path = tmp_path / "settings.json"
    path.write_text(json.dumps(on_disk), encoding="utf-8")
    monkeypatch.setattr(bs, "SETTINGS_PATH", path)
    return bs.Settings()


def test_a_replaced_default_in_a_tenant_file_loads_the_new_default(tmp_path, monkeypatch):
    s = _load_from(
        tmp_path, monkeypatch, {"cloud_max_concurrent": 3, "cma_model": "claude-sonnet-4-6"}
    )
    assert s.get("cloud_max_concurrent") == 16
    assert s.get("cma_model") == "claude-sonnet-5-5"


def test_a_deliberate_value_survives(tmp_path, monkeypatch):
    s = _load_from(
        tmp_path, monkeypatch, {"cloud_max_concurrent": 4, "cma_model": "claude-opus-5-5"}
    )
    assert s.get("cloud_max_concurrent") == 4
    assert s.get("cma_model") == "claude-opus-5-5"


def test_new_settings_are_declared_in_both_files():
    from pathlib import Path

    import brain.settings as bs

    bundled = json.loads((Path(bs.__file__).parent / "settings.json").read_text())
    for key in (
        "claude_effort",
        "claude_structured_effort",
        "claude_thinking_headroom_tokens",
    ):
        assert key in bs.DEFAULTS and key in bundled
    assert bundled["cloud_max_concurrent"] == bs.DEFAULTS["cloud_max_concurrent"] == 16
    assert bundled["cma_model"] == bs.DEFAULTS["cma_model"] == "claude-sonnet-5-5"


# ── critic covers every draft; critic ∥ empathy ────────────────────────────────


def _make_full_frontal(router):
    from brain.brainstem import Brainstem
    from brain.bus import Bus
    from brain.clusters.frontal import FrontalCluster

    bus = Bus()
    return FrontalCluster(bus, Brainstem(bus, router), router)


def test_critic_cap_covers_the_whole_drafter_pool():
    class _Router:
        async def call(self, *a, **kw):
            return '{"overall": 0.9, "veto": false}'

        def supports(self, *a, **kw):
            return True

    f = _make_full_frontal(_Router())
    assert f._critic.max_calls_per_turn >= len(f._drafters) >= 5
    f._critic.reset_turn("t1")

    async def _all():
        return await asyncio.gather(
            *[f._critic.call([{"role": "user", "content": f"d{i}"}]) for i in range(5)]
        )

    # Before: calls 3-5 returned "" (Per-turn call limit hit) → a default 0.5 score.
    assert all(out for out in asyncio.run(_all()))


def _scoring_frontal(score_fn, empathy_fn):
    from brain.observability.timeline import TurnTrace

    f = _frontal_with_drafters(_StubExecutive("{}"), TurnTrace("t", "s", "hi"))
    f._drafters = [None] * 5
    f._score_draft = score_fn
    f._run_empathy_check = empathy_fn
    f._stamp_judge_floor_inputs = lambda *a, **k: None
    return f


def _drive_emotional(f, turn_id):
    instruction = dict(_INSTRUCTION, drafter_count=5)
    features = dict(_FEATURES, user_emotion="sadness")  # turns the empathy check on
    return _run(
        f._run_drafters_and_select(
            _NM, _CHEM, _EXEC_SIG, instruction, features, _AFFECT, {}, "", turn_id
        )
    )


def test_critic_and_empathy_run_side_by_side():
    settings.update({"colony_features": 0})
    events: list[str] = []

    async def score(text, prompt, turn_id):
        events.append("critic_start")
        await asyncio.sleep(0.05)
        events.append("critic_end")
        return {"overall": 0.8, "veto": False}

    async def empathy(text, emotion, turn_id):
        events.append("empathy_start")
        await asyncio.sleep(0.05)
        events.append("empathy_end")
        return {"empathy_score": 0.6, "veto": False}

    try:
        f = _scoring_frontal(score, empathy)
        assert _drive_emotional(f, "t-par").startswith("draft text")
    finally:
        settings.update({"colony_features": 1})
    # Every empathy check started before any critic finished — concurrent, not serial.
    first_critic_end = events.index("critic_end")
    assert events.count("empathy_start") == 5
    assert all(i < first_critic_end for i, e in enumerate(events) if e == "empathy_start")
    # Scores still blend 0.7 critic + 0.3 empathy on every draft.
    scored = [e for e in f.last_turn_draft_scores if not e["vetoed"]]
    assert len(scored) == 5
    assert all(e["overall"] == pytest.approx(0.8 * 0.7 + 0.6 * 0.3) for e in scored)


def test_critic_veto_still_wins_over_an_empathy_pass():
    settings.update({"colony_features": 0})

    async def score(text, prompt, turn_id):
        return {"overall": 0.2, "veto": text.endswith("0"), "veto_reason": "unsafe"}

    async def empathy(text, emotion, turn_id):
        return {"empathy_score": 0.9, "veto": False}

    try:
        f = _scoring_frontal(score, empathy)
        _drive_emotional(f, "t-veto")
    finally:
        settings.update({"colony_features": 1})
    vetoed = [e for e in f.last_turn_draft_scores if e["vetoed"]]
    assert [e["draft_id"] for e in vetoed] == ["draft0"]
    assert vetoed[0]["empathy_score"] is None  # the critic veto drops the empathy verdict


def test_empathy_veto_still_vetoes():
    settings.update({"colony_features": 0})

    async def score(text, prompt, turn_id):
        return {"overall": 0.9, "veto": False}

    async def empathy(text, emotion, turn_id):
        return {"empathy_score": 0.1, "veto": text.endswith("1"), "veto_reason": "cold"}

    try:
        f = _scoring_frontal(score, empathy)
        _drive_emotional(f, "t-eveto")
    finally:
        settings.update({"colony_features": 1})
    assert [e["draft_id"] for e in f.last_turn_draft_scores if e["vetoed"]] == ["draft1"]


# ── hippocampus encoder ────────────────────────────────────────────────────────


def test_encoder_never_dials_a_container_local_ollama():
    from brain.bus import Bus
    from brain.clusters.hippocampus import HippocampusCluster

    class _Router:
        async def call(self, *a, **kw):
            return "{}"

    h = HippocampusCluster(Bus(), _Router())
    # "local" = localhost:11434, which does not exist in a hosted tenant container.
    assert h._encoder.model == "runpod"
    assert h._encoder.locality == "local"  # still the local-provider tier, never cloud
