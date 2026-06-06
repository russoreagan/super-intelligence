"""
Tests for the ChatML stop-token + sanitizer fix (Part A1/A2).

Failed self-jobs were caused by the local/RunPod Qwen model degenerating into
repeated <|im_start|> tokens — no stop sequence was set on the Ollama /api/chat
payload, so generation ran to num_predict and the garbage was persisted into job
records (planner JSON and spoken summaries). These tests lock in:
  A1 — _call_local sets options["stop"] to the ChatML end tokens.
  A2 — _strip_chatml removes any leaked control tokens defensively.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import brain.model_router as mr_mod
from brain.model_router import (
    _CHATML_STOP,
    ModelRouter,
    _strip_chatml,
)


class TestStripChatml:
    def test_strips_runaway_im_start_spam(self):
        # The exact corruption shape observed in second_brain/jobs/*.json.
        corrupt = '{"Done.":") }<tool_call>' + "<|im_start|>" * 30
        cleaned = _strip_chatml(corrupt)
        assert "<|im_start|>" not in cleaned
        assert "<tool_call>" not in cleaned

    def test_strips_im_end_and_endoftext(self):
        assert _strip_chatml("hello<|im_end|>") == "hello"
        assert _strip_chatml("hi<|endoftext|>") == "hi"

    def test_passes_clean_text_through(self):
        assert _strip_chatml("All good here.") == "All good here."

    def test_valid_json_survives(self):
        # JSON-format responses must remain parseable after sanitizing.
        assert json.loads(_strip_chatml('{"a": 1}<|im_end|>')) == {"a": 1}

    def test_empty_and_none_safe(self):
        assert _strip_chatml("") == ""
        assert _strip_chatml(None) is None


def _fake_http(captured: dict):
    """A fake Ollama HTTP client supporting BOTH transports _call_local uses:
    streaming (RunPod variants, `async with client.stream(...)`) and plain POST
    (local variants). Both capture the request payload so tests can assert on it."""

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "ok"}, "prompt_eval_count": 1, "eval_count": 1}

    class _FakeStreamCtx:
        def __init__(self, payload):
            captured["payload"] = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            # NDJSON, mirroring Ollama's /api/chat stream=true output.
            yield json.dumps({"message": {"content": "ok"}})
            yield json.dumps({"done": True, "prompt_eval_count": 1, "eval_count": 1})

    class _FakeHTTP:
        def stream(self, method, url, json=None, timeout=None):  # noqa: A002
            return _FakeStreamCtx(json)

        async def post(self, url, json=None, timeout=None):  # noqa: A002
            captured["payload"] = json
            return _FakeResp()

    return _FakeHTTP()


class TestCallLocalSetsStopTokens:
    @pytest.mark.asyncio
    async def test_payload_includes_chatml_stop_runpod(self):
        """RunPod variants stream; the streamed payload must still carry the stop tokens."""
        router = ModelRouter()
        captured: dict = {}
        router._get_http = MagicMock(return_value=_fake_http(captured))

        out, _in, _o = await router._call_local(
            "system", [{"role": "user", "content": "hi"}], local_variant="runpod"
        )
        assert out == "ok"  # streamed content assembled (regression: was "" on stream error)
        assert captured["payload"]["stream"] is True  # RunPod uses streaming transport
        assert captured["payload"]["options"]["stop"] == _CHATML_STOP

    @pytest.mark.asyncio
    async def test_payload_includes_chatml_stop_local(self):
        """Local (non-RunPod) variants POST; that payload must carry the stop tokens too."""
        router = ModelRouter()
        captured: dict = {}
        router._get_http = MagicMock(return_value=_fake_http(captured))

        out, _in, _o = await router._call_local(
            "system", [{"role": "user", "content": "hi"}], local_variant="local"
        )
        assert out == "ok"
        assert captured["payload"]["stream"] is False  # local is non-streaming
        assert captured["payload"]["options"]["stop"] == _CHATML_STOP


class TestRunpodReconnect:
    """RunPod stream path must reconnect after a restart (stale-socket) failure
    rather than silently returning empty."""

    @pytest.mark.asyncio
    async def test_stream_failure_resets_client_and_retries(self, monkeypatch):
        monkeypatch.setitem(
            __import__("brain.settings", fromlist=["settings"]).settings._data,
            "runpod_stream_retries",
            2,
        )
        monkeypatch.setattr(mr_mod.asyncio, "sleep", AsyncMock())  # no real backoff delay
        router = ModelRouter()
        router._reset_http = AsyncMock()

        class _OkStreamCtx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield json.dumps({"message": {"content": "recovered"}})
                yield json.dumps({"done": True, "prompt_eval_count": 1, "eval_count": 1})

        calls = {"n": 0}

        class _FlakyHTTP:
            def stream(self, method, url, json=None, timeout=None):  # noqa: A002
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ConnectionError("stale keep-alive socket (pod restarted)")
                return _OkStreamCtx()

        router._get_http = MagicMock(return_value=_FlakyHTTP())

        out, _i, _o = await router._call_local(
            "system", [{"role": "user", "content": "hi"}], local_variant="runpod"
        )
        assert out == "recovered"  # reconnected on the retry
        assert calls["n"] == 2  # first failed, second succeeded
        router._reset_http.assert_awaited()  # stale client was dropped before retry

    @pytest.mark.asyncio
    async def test_all_streams_fail_falls_back_to_post(self, monkeypatch):
        monkeypatch.setitem(
            __import__("brain.settings", fromlist=["settings"]).settings._data,
            "runpod_stream_retries",
            1,
        )
        monkeypatch.setattr(mr_mod.asyncio, "sleep", AsyncMock())
        router = ModelRouter()
        router._reset_http = AsyncMock()
        captured: dict = {}

        class _FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"message": {"content": "via-post"}, "prompt_eval_count": 2, "eval_count": 3}

        class _PostOnlyHTTP:
            def stream(self, method, url, json=None, timeout=None):  # noqa: A002
                raise ConnectionError("stream down")

            async def post(self, url, json=None, timeout=None):  # noqa: A002
                captured["payload"] = json
                return _FakeResp()

        router._get_http = MagicMock(return_value=_PostOnlyHTTP())

        out, _i, _o = await router._call_local(
            "system", [{"role": "user", "content": "hi"}], local_variant="runpod"
        )
        assert out == "via-post"  # graceful degrade to non-streaming POST
        assert captured["payload"]["stream"] is False  # fallback forces non-streaming
        assert captured["payload"]["options"]["stop"] == _CHATML_STOP  # stop tokens preserved
