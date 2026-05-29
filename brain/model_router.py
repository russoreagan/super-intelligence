"""
ModelRouter — single dispatch point for all LLM calls.
Cell config declares model: "haiku" | "flash" | "flash-lite" | "local".
This class decides the actual API call. Swap providers here, nowhere else.
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

MODEL_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "flash": "gemini-2.5-flash",
    "flash-lite": "gemini-2.5-flash-lite",
    "local": "local",
    "local-free": "local-free",       # same model as local, plain-text output (no JSON grammar)
    "local-code": "local-code",       # routes to OLLAMA_CODE_MODEL (defaults to qwen2.5:14b — the hot model)
    "local-general": "local-general", # routes to OLLAMA_GENERAL_MODEL (qwen2.5:32b)
}

# Embedding dim must match EpisodicStore table schema (see brain/second_brain/store.py).
# nomic-embed-text and gemini-embedding-001 both produce 768-dim vectors.
EMBEDDING_DIM = 768
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
GOOGLE_EMBED_MODEL = os.environ.get("GOOGLE_EMBED_MODEL", "gemini-embedding-001")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# The motor planner ("local-code") defaults to the SAME model as the rest of the
# brain (local → qwen2.5:14b). Reason: every other cell (DMN, hippocampus,
# skill_selector, sleep) keeps qwen2.5:14b hot. If the planner used a distinct
# model (e.g. qwen2.5-coder:14b), every tool attempt would force Ollama to
# cold-load a second ~9GB model under memory contention — which exceeds the
# call timeout and makes EVERY tool use fail with "[planner failed]". Sharing the
# hot model eliminates the cold-load entirely. Override via OLLAMA_CODE_MODEL only
# if you have the VRAM headroom to keep a second model resident.
OLLAMA_CODE_MODEL = os.environ.get("OLLAMA_CODE_MODEL", "qwen2.5:14b")
OLLAMA_GENERAL_MODEL = os.environ.get("OLLAMA_GENERAL_MODEL", "qwen2.5:32b")
# A cold model load from disk takes ~50s on a 14B model. The per-request HTTP
# timeout must comfortably exceed that, or the FIRST call after the model is
# evicted always fails. Override via OLLAMA_HTTP_TIMEOUT_SECONDS.
OLLAMA_HTTP_TIMEOUT = float(os.environ.get("OLLAMA_HTTP_TIMEOUT_SECONDS", "120"))
# How long Ollama keeps a model resident after a call. Longer = fewer cold loads
# (the dominant cause of tool-call timeouts). Override via OLLAMA_KEEP_ALIVE.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")
# Timeout for an explicit model-preload (warmup) request. A cold 14B load can take
# up to ~3 min under memory pressure; this is deliberately generous because warmup
# is a one-time cost that makes every subsequent planner call fast.
OLLAMA_MODEL_LOAD_TIMEOUT = float(os.environ.get("OLLAMA_MODEL_LOAD_TIMEOUT_SECONDS", "240"))


class ModelRouter:
    def __init__(self, obs=None) -> None:
        self._anthropic_client = None
        self._google_client = None
        self._http_client = None   # persistent httpx client; reused across Ollama calls
        self._call_log: list[dict] = []
        self._obs = obs
        # Local-first embeddings; flip to "google" if Ollama is unreachable.
        self._embed_backend = "ollama"

        # ── Resource policy ───────────────────────────────────────────────────
        # Background mode: set True while running autonomous/self-initiated work.
        # Cloud calls in this mode are budgeted and capped to prevent bill creep.
        self._bg_mode: bool = False
        # Session-level token counter for background cloud usage (in + out combined).
        self._bg_cloud_tokens_used: int = 0
        # Lazily-created semaphore; limits concurrent Ollama calls to protect device.
        self._local_semaphore: "asyncio.Semaphore | None" = None

    # ── Background mode controls ──────────────────────────────────────────────

    def enter_background_mode(self) -> None:
        """Mark subsequent calls as background/autonomous. Cloud calls will be
        budgeted and capped. Always pair with exit_background_mode() in a
        try/finally block."""
        self._bg_mode = True

    def exit_background_mode(self) -> None:
        """Return to interactive mode. Call in a finally block."""
        self._bg_mode = False

    @property
    def _bg_mode(self) -> bool:  # type: ignore[override]
        return getattr(self, "_bg_mode_val", False)

    @_bg_mode.setter
    def _bg_mode(self, v: bool) -> None:
        self._bg_mode_val = v

    @property
    def bg_cloud_tokens_used(self) -> int:
        """Total input+output tokens spent on background cloud calls this session."""
        return self._bg_cloud_tokens_used

    @property
    def bg_cloud_budget_remaining(self) -> int:
        """How many background cloud tokens are left before fallback to local."""
        from brain.settings import settings as _settings; _s = _settings.get
        budget = int(_s("bg_cloud_token_budget") or 50_000)
        return max(0, budget - self._bg_cloud_tokens_used)

    def _get_local_semaphore(self) -> "asyncio.Semaphore":
        """Lazily-created concurrency limiter for Ollama calls."""
        if self._local_semaphore is None:
            import asyncio
            from brain.settings import settings as _settings; _s = _settings.get
            limit = int(_s("local_max_concurrent") or 3)
            self._local_semaphore = asyncio.Semaphore(limit)
        return self._local_semaphore

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

    @staticmethod
    def _resolve_local_model(model_key: str) -> str | None:
        """Map a local model_key to the concrete Ollama model name, or None if not local."""
        if model_key == "local-code":
            return OLLAMA_CODE_MODEL
        if model_key == "local-general":
            return OLLAMA_GENERAL_MODEL
        if model_key in ("local", "local-free"):
            return os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")
        return None

    async def warmup_local(self, model_key: str = "local-code",
                           timeout: float | None = None) -> bool:
        """Preload a local Ollama model into memory as an explicit, separately-timed
        step so the cold-load latency (~50s, up to ~3min under memory pressure) is
        NOT charged against — and does not trip — the planner's per-call timeout.

        Best-effort: returns True if the model is resident afterward, False otherwise.
        A failed warmup is non-fatal — the caller proceeds and the normal call path
        (with retries) still runs; warmup just makes the common case fast.
        """
        model_name = self._resolve_local_model(model_key)
        if model_name is None:
            return False  # cloud models don't need warming
        import asyncio
        to = timeout if timeout is not None else OLLAMA_MODEL_LOAD_TIMEOUT
        try:
            # POST /api/generate with no prompt loads the model and returns immediately
            # once it's resident (Ollama's documented preload mechanism).
            async with self._get_local_semaphore():
                r = await self._get_http().post(
                    f"{OLLAMA_HOST}/api/generate",
                    json={"model": model_name, "keep_alive": OLLAMA_KEEP_ALIVE},
                    timeout=to,
                )
            r.raise_for_status()
            logger.info("[ModelRouter] Warmed up local model %s", model_name)
            return True
        except Exception as e:
            logger.warning("[ModelRouter] Warmup of %s failed (continuing anyway): %s",
                           model_name, e)
            return False

    async def call(self, model_key: str, system_prompt: str, messages: list[dict],
                   *, cluster: str = "", cell: str = "", turn_id: str = "",
                   locality: str = "either", max_tokens: int = 1024,
                   skills: list[str] | None = None,
                   temperature: float | None = None) -> str:
        import asyncio
        from brain.settings import settings as _settings; _s = _settings.get

        model_id = MODEL_MAP.get(model_key, model_key)

        # Locality enforcement: local cells must never dispatch to cloud APIs
        _is_cloud = model_id.startswith("claude") or model_id.startswith("gemini")
        if locality == "local" and _is_cloud:
            logger.warning(
                "[Security] Memory cell %s/%s is restricted to local-only inference but model '%s' "
                "routes to a cloud API — redirecting to Ollama. If Ollama isn't running this call "
                "will fail. Fix: run 'ollama serve' and 'ollama pull qwen2.5:14b'.",
                cluster, cell, model_id,
            )
            model_key = model_key if model_key in ("local", "local-free", "local-code", "local-general") else "local"
            model_id = model_key
            _is_cloud = False

        # Background mode: apply cloud budget + per-call caps to protect against
        # runaway spend on autonomous work.
        if self._bg_mode and _is_cloud:
            budget = int(_s("bg_cloud_token_budget") or 50_000)
            if self._bg_cloud_tokens_used >= budget:
                logger.warning(
                    "[Resource] Background cloud budget exhausted (%d/%d tokens used) "
                    "— routing %s/%s to local for this call.",
                    self._bg_cloud_tokens_used, budget, cluster, cell,
                )
                model_key = "local"
                model_id = "local"
                _is_cloud = False
            else:
                # Cap output tokens for background calls
                call_cap = int(_s("bg_cloud_max_tokens_per_call") or 512)
                if max_tokens > call_cap:
                    logger.debug(
                        "[Resource] Background call %s/%s: capping max_tokens %d→%d",
                        cluster, cell, max_tokens, call_cap,
                    )
                    max_tokens = call_cap

        start = time.time()
        bg_timeout = float(_s("bg_cloud_timeout_s") or 20.0) if (self._bg_mode and _is_cloud) else None

        # Inject skill text into system prompt before dispatch — applies to all backends
        # (Haiku, Gemini, local). Previously this was scoped to local-only, which silently
        # dropped skills passed to cloud cells like frontal drafters.
        if skills:
            from brain.skill_loader import SkillLoader
            skill_block = SkillLoader.load_many(skills)
            if skill_block:
                system_prompt = f"{system_prompt}\n\n{skill_block}"

        if model_id.startswith("claude"):
            try:
                coro = self._call_anthropic(model_id, system_prompt, messages, max_tokens)
                if bg_timeout:
                    text, in_tok, out_tok = await asyncio.wait_for(coro, timeout=bg_timeout)
                else:
                    text, in_tok, out_tok = await coro
            except asyncio.TimeoutError:
                logger.warning(
                    "[Resource] Background cloud call %s/%s timed out after %.0fs — falling back to local.",
                    cluster, cell, bg_timeout,
                )
                text, in_tok, out_tok = await self._call_local(system_prompt, messages, max_tokens)
            if self._bg_mode:
                self._bg_cloud_tokens_used += in_tok + out_tok
                logger.debug("[Resource] BG cloud tokens used: %d/%d (this call: %d+%d)",
                             self._bg_cloud_tokens_used,
                             int(_s("bg_cloud_token_budget") or 50_000), in_tok, out_tok)
        elif model_id.startswith("gemini"):
            try:
                coro = self._call_google(model_id, system_prompt, messages, max_tokens)
                if bg_timeout:
                    text, in_tok, out_tok = await asyncio.wait_for(coro, timeout=bg_timeout)
                else:
                    text, in_tok, out_tok = await coro
            except asyncio.TimeoutError:
                logger.warning(
                    "[Resource] Background cloud call %s/%s timed out after %.0fs — falling back to local.",
                    cluster, cell, bg_timeout,
                )
                text, in_tok, out_tok = await self._call_local(system_prompt, messages, max_tokens)
            if self._bg_mode:
                self._bg_cloud_tokens_used += in_tok + out_tok
                logger.debug("[Resource] BG cloud tokens used: %d/%d (this call: %d+%d)",
                             self._bg_cloud_tokens_used,
                             int(_s("bg_cloud_token_budget") or 50_000), in_tok, out_tok)
        elif model_id in ("local", "local-free", "local-code", "local-general"):
            text, in_tok, out_tok = await self._call_local(
                system_prompt, messages, max_tokens, local_variant=model_id,
                temperature=temperature,
            )
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
            cache_control={"type": "ephemeral"},
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

    @staticmethod
    def _flatten_content(content) -> str:
        if isinstance(content, list):
            return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        return content or ""

    async def _call_local(self, system_prompt: str,
                          messages: list[dict], max_tokens: int = 1024,
                          local_variant: str = "local",
                          temperature: float | None = None) -> tuple[str, int, int]:
        flat_messages = [{"role": m["role"], "content": self._flatten_content(m["content"])} for m in messages]
        base_model = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")
        if local_variant == "local-code":
            model_name = OLLAMA_CODE_MODEL
        elif local_variant == "local-general":
            model_name = OLLAMA_GENERAL_MODEL
        else:
            # local and local-free both use the base model
            model_name = base_model

        options: dict = {"num_predict": max_tokens}
        use_json_format = False

        if local_variant == "local-code":
            # Tool planner — deterministic; large context for system prompt + skill blocks
            options["temperature"] = 0.1
            options["num_ctx"] = 8192
            use_json_format = True
        elif local_variant == "local-general":
            # Sleep consolidation (all three cells return JSON)
            options["temperature"] = 0.3
            options["num_ctx"] = 8192
            use_json_format = True
        elif local_variant == "local-free":
            # Plain-text output only (speak_bridge rewriter) — needs creative latitude
            options["temperature"] = 0.7
            options["num_ctx"] = 2048
        else:
            # local — hippocampus + all DMN JSON cells; format:json ensures valid structure
            # while temp=0.3 keeps content focused without killing variety in thought fields
            options["temperature"] = 0.3
            options["num_ctx"] = 16384
            use_json_format = True

        # Per-cell override (e.g. the DMN monologue runs hot for divergent ideation).
        if temperature is not None:
            options["temperature"] = float(temperature)

        payload: dict = {
            "model": model_name,
            "messages": [{"role": "system", "content": system_prompt}] + flat_messages,
            "stream": False,
            "options": options,
            "keep_alive": OLLAMA_KEEP_ALIVE,  # keep model hot; fewer cold loads
        }
        if use_json_format:
            payload["format"] = "json"
        async with self._get_local_semaphore():
            r = await self._get_http().post(f"{OLLAMA_HOST}/api/chat", json=payload,
                                            timeout=OLLAMA_HTTP_TIMEOUT)
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

    def turn_calls_excluding_background(self) -> int:
        """Count of LLM calls this turn that should count against the turn's
        budget. DMN-cluster calls happen continuously between *and during*
        turns and aren't logically part of the current turn's work, so they
        are excluded here. Used by brainstem.end_turn and run.py telemetry.
        """
        return sum(1 for c in self._call_log if c.get("cluster") != "dmn")

    def reset_turn_log(self) -> list[dict]:
        log = self._call_log[:]
        self._call_log = []
        return log
