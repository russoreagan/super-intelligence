"""
ModelRouter — single dispatch point for all LLM calls.
Cell config declares model: "haiku" | "flash" | "flash-lite" | "local".
This class decides the actual API call. Swap providers here, nowhere else.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

MODEL_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "flash": "gemini-2.5-flash",
    "flash-lite": "gemini-2.5-flash-lite",
    "local": "local",
}

# Embedding dim must match EpisodicStore table schema (see brain/second_brain/store.py).
# nomic-embed-text and gemini-embedding-001 both produce 768-dim vectors.
EMBEDDING_DIM = 768
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
GOOGLE_EMBED_MODEL = os.environ.get("GOOGLE_EMBED_MODEL", "gemini-embedding-001")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


class ModelRouter:
    def __init__(self, obs=None) -> None:
        self._anthropic_client = None
        self._google_client = None
        self._http_client = None   # persistent httpx client; reused across Ollama calls
        self._call_log: list[dict] = []
        self._obs = obs
        # Local-first embeddings; flip to "google" if Ollama is unreachable.
        self._embed_backend = "ollama"

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

    def _get_http(self):
        """Lazily-created persistent httpx client; avoids a new TCP connection per Ollama call."""
        if self._http_client is None:
            import httpx
            self._http_client = httpx.AsyncClient()
        return self._http_client

    async def call(self, model_key: str, system_prompt: str, messages: list[dict],
                   *, cluster: str = "", cell: str = "", turn_id: str = "",
                   locality: str = "either", max_tokens: int = 1024,
                   skills: list[str] | None = None) -> str:
        model_id = MODEL_MAP.get(model_key, model_key)

        # Locality enforcement: local cells must never dispatch to cloud APIs
        _is_cloud = model_id.startswith("claude") or model_id.startswith("gemini")
        if locality == "local" and _is_cloud:
            logger.warning(
                "[Security] Memory cell %s/%s is restricted to local-only inference but model '%s' "
                "routes to a cloud API — redirecting to Ollama. If Ollama isn't running this call "
                "will fail. Fix: run 'ollama serve' and 'ollama pull qwen2.5:7b'.",
                cluster, cell, model_id,
            )
            model_key = "local"
            model_id = "local"

        start = time.time()
        if model_id.startswith("claude"):
            text, in_tok, out_tok = await self._call_anthropic(model_id, system_prompt, messages, max_tokens)
        elif model_id.startswith("gemini"):
            text, in_tok, out_tok = await self._call_google(model_id, system_prompt, messages, max_tokens)
        elif model_id == "local":
            if skills:
                from brain.skill_loader import SkillLoader
                skill_block = SkillLoader.load_many(skills)
                if skill_block:
                    system_prompt = f"{system_prompt}\n\n{skill_block}"
            text, in_tok, out_tok = await self._call_local(system_prompt, messages, max_tokens)
        else:
            raise ValueError(f"Unknown model key: {model_key}")

        latency = time.time() - start
        self._log_call(model_id, messages, in_tok, out_tok, latency, cluster=cluster or "", cell=cell or "")
        if self._obs and turn_id:
            try:
                self._obs.record_llm_call(
                    turn_id=turn_id, cluster=cluster or "unknown",
                    cell=cell or "unknown", model=model_id,
                    prompt_tokens=in_tok, completion_tokens=out_tok,
                    latency_s=latency,
                )
            except Exception as e:
                logger.debug("obs.record_llm_call failed: %s", e)
        return text

    async def _call_anthropic(self, model_id: str, system_prompt: str,
                              messages: list[dict], max_tokens: int = 1024) -> tuple[str, int, int]:
        client = self._get_anthropic()
        anthropic_msgs = [{"role": m["role"], "content": m["content"]} for m in messages]

        response = await client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system_prompt,
                      "cache_control": {"type": "ephemeral"}}],
            messages=anthropic_msgs,
        )
        usage = getattr(response, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) if usage else 0
        out_tok = getattr(usage, "output_tokens", 0) if usage else 0
        return response.content[0].text, in_tok, out_tok

    async def _call_google(self, model_id: str, system_prompt: str,
                           messages: list[dict], max_tokens: int = 1024) -> tuple[str, int, int]:
        from google import genai
        from google.genai import types

        client = self._get_google()
        contents = []
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            raw = m["content"]
            # Multimodal: content may be a list of parts like
            #   [{"type": "text", "text": "..."}, {"type": "image", "data": bytes, "mime": "image/jpeg"}, ...]
            if isinstance(raw, list):
                parts = []
                for part in raw:
                    if part.get("type") == "text":
                        parts.append(types.Part(text=part["text"]))
                    elif part.get("type") == "image":
                        parts.append(types.Part(
                            inline_data=types.Blob(mime_type=part["mime"], data=part["data"])
                        ))
                contents.append(types.Content(role=role, parts=parts))
            else:
                contents.append(types.Content(
                    role=role,
                    parts=[types.Part(text=raw)]
                ))

        response = await client.aio.models.generate_content(
            model=model_id,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=max_tokens,
            ),
        )
        usage = getattr(response, "usage_metadata", None)
        in_tok = getattr(usage, "prompt_token_count", 0) if usage else 0
        out_tok = getattr(usage, "candidates_token_count", 0) if usage else 0
        return response.text, in_tok, out_tok

    async def _call_local(self, system_prompt: str,
                          messages: list[dict], max_tokens: int = 1024) -> tuple[str, int, int]:
        payload = {
            "model": os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"),
            "messages": [{"role": "system", "content": system_prompt}] + messages,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        r = await self._get_http().post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        in_tok = int(data.get("prompt_eval_count", 0))
        out_tok = int(data.get("eval_count", 0))
        return data["message"]["content"], in_tok, out_tok

    async def embed(self, text: str) -> list[float] | None:
        """
        Generate an embedding vector. Tries Ollama first (local, free),
        falls back to Google text-embedding-004 if Ollama unreachable.
        Returns None on total failure so callers can skip vector storage.
        Output dim: EMBEDDING_DIM (768).
        """
        if not text:
            return None
        text = text[:8192]  # safety cap

        if self._embed_backend == "ollama":
            vec = await self._embed_ollama(text)
            if vec is not None:
                return vec
            # Permanent flip to google for remainder of session.
            logger.info(
                "Ollama embedding service unreachable — switching to Google embeddings for this session. "
                "Memory search will still work. To restore local embeddings: run 'ollama serve' and "
                "'ollama pull nomic-embed-text'."
            )
            self._embed_backend = "google"

        return await self._embed_google(text)

    async def _embed_ollama(self, text: str) -> list[float] | None:
        try:
            r = await self._get_http().post(
                f"{OLLAMA_HOST}/api/embeddings",
                json={"model": OLLAMA_EMBED_MODEL, "prompt": text},
                timeout=10,
            )
            r.raise_for_status()
            vec = r.json().get("embedding")
            if vec and len(vec) == EMBEDDING_DIM:
                return vec
            if vec:
                logger.warning(
                    "Ollama returned %d-dimensional embeddings but %d were expected — "
                    "wrong model pulled? Check OLLAMA_EMBED_MODEL in .env (should be 'nomic-embed-text').",
                    len(vec), EMBEDDING_DIM,
                )
            return None
        except Exception as e:
            logger.debug("Ollama embed failed: %s", e)
            return None

    async def _embed_google(self, text: str) -> list[float] | None:
        try:
            client = self._get_google()
            r = await client.aio.models.embed_content(
                model=GOOGLE_EMBED_MODEL,
                contents=text,
                config={"output_dimensionality": EMBEDDING_DIM},
            )
            # google-genai returns ContentEmbedding objects with `.values`
            if r.embeddings and r.embeddings[0].values:
                vec = list(r.embeddings[0].values)
                if len(vec) == EMBEDDING_DIM:
                    return vec
                logger.warning(
                    "Google returned %d-dimensional embeddings despite output_dimensionality=%d — "
                    "check GOOGLE_EMBED_MODEL in .env.",
                    len(vec), EMBEDDING_DIM,
                )
            return None
        except Exception as e:
            logger.warning("Google embedding API failed — memory search may be degraded this turn: %s", e)
            return None

    def _log_call(self, model_id: str, messages: list[dict],
                   in_tok: int = 0, out_tok: int = 0, latency_s: float = 0.0,
                   cluster: str = "", cell: str = "") -> None:
        self._call_log.append({
            "model": model_id, "msgs": len(messages),
            "in": in_tok, "out": out_tok, "latency_s": latency_s,
            "cluster": cluster, "cell": cell,
        })

    @property
    def total_calls_this_turn(self) -> int:
        return len(self._call_log)

    def reset_turn_log(self) -> list[dict]:
        log = self._call_log[:]
        self._call_log = []
        return log
