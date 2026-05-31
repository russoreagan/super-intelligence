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


class TestCallLocalSetsStopTokens:
    @pytest.mark.asyncio
    async def test_payload_includes_chatml_stop(self):
        router = ModelRouter()

        captured: dict = {}

        class _FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "message": {"content": "ok"},
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                }

        class _FakeHTTP:
            async def post(self, url, json=None, timeout=None):  # noqa: A002
                captured["payload"] = json
                return _FakeResp()

        router._get_http = MagicMock(return_value=_FakeHTTP())

        out, _in, _o = await router._call_local(
            "system", [{"role": "user", "content": "hi"}], local_variant="runpod"
        )
        assert out == "ok"
        assert captured["payload"]["options"]["stop"] == _CHATML_STOP
