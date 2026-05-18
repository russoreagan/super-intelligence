"""
ModelRouter — single dispatch point for all LLM calls.
Cell config declares model: "haiku" | "flash" | "flash-lite" | "local".
This class decides the actual API call. Swap providers here, nowhere else.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

MODEL_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "flash": "gemini-2.0-flash",
    "flash-lite": "gemini-2.0-flash-lite",
    "local": "local",
}


class ModelRouter:
    def __init__(self) -> None:
        self._anthropic_client = None
        self._google_client = None
        self._call_log: list[dict] = []

    def _get_anthropic(self):
        if self._anthropic_client is None:
            import anthropic
            self._anthropic_client = anthropic.AsyncAnthropic(
                api_key=os.environ["ANTHROPIC_API_KEY"]
            )
        return self._anthropic_client

    def _get_google(self):
        if self._google_client is None:
            from google import genai
            self._google_client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        return self._google_client

    async def call(self, model_key: str, system_prompt: str, messages: list[dict]) -> str:
        model_id = MODEL_MAP.get(model_key, model_key)

        if model_id.startswith("claude"):
            return await self._call_anthropic(model_id, system_prompt, messages)
        elif model_id.startswith("gemini"):
            return await self._call_google(model_id, system_prompt, messages)
        elif model_id == "local":
            return await self._call_local(system_prompt, messages)
        else:
            raise ValueError(f"Unknown model key: {model_key}")

    async def _call_anthropic(self, model_id: str, system_prompt: str, messages: list[dict]) -> str:
        client = self._get_anthropic()
        # Convert to Anthropic message format
        anthropic_msgs = []
        for m in messages:
            anthropic_msgs.append({"role": m["role"], "content": m["content"]})

        response = await client.messages.create(
            model=model_id,
            max_tokens=1024,
            system=[{"type": "text", "text": system_prompt,
                      "cache_control": {"type": "ephemeral"}}],
            messages=anthropic_msgs,
        )
        self._log_call(model_id, messages)
        return response.content[0].text

    async def _call_google(self, model_id: str, system_prompt: str, messages: list[dict]) -> str:
        from google import genai
        from google.genai import types

        client = self._get_google()
        # Build contents from messages
        contents = []
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            contents.append(types.Content(
                role=role,
                parts=[types.Part(text=m["content"])]
            ))

        response = await client.aio.models.generate_content(
            model=model_id,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=1024,
            ),
        )
        self._log_call(model_id, messages)
        return response.text

    async def _call_local(self, system_prompt: str, messages: list[dict]) -> str:
        import httpx
        payload = {
            "model": os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"),
            "messages": [{"role": "system", "content": system_prompt}] + messages,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post("http://localhost:11434/api/chat", json=payload)
            r.raise_for_status()
            self._log_call("local", messages)
            return r.json()["message"]["content"]

    def _log_call(self, model_id: str, messages: list[dict]) -> None:
        self._call_log.append({"model": model_id, "msgs": len(messages)})

    @property
    def total_calls_this_turn(self) -> int:
        return len(self._call_log)

    def reset_turn_log(self) -> list[dict]:
        log = self._call_log[:]
        self._call_log = []
        return log
