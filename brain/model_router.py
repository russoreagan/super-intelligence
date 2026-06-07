"""
ModelRouter — single dispatch point for all LLM calls.
Cell config declares model: "haiku" | "flash" | "flash-lite" | "local".
This class decides the actual API call. Swap providers here, nowhere else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

MODEL_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "flash": "gemini-2.5-flash",
    "flash-lite": "gemini-2.5-flash-lite",
    "local": "local",
    "local-free": "local-free",  # same model as local, plain-text output (no JSON grammar)
    "local-code": "local-code",  # routes to OLLAMA_CODE_MODEL (defaults to qwen2.5:14b — the hot model)
    "local-general": "local-general",  # routes to OLLAMA_GENERAL_MODEL (qwen2.5:14b)
    "runpod": "runpod",  # RunPod remote Ollama — same options as local
    "runpod-free": "runpod-free",  # RunPod plain-text output (no JSON grammar)
    "runpod-code": "runpod-code",  # RunPod code/JSON settings (temp=0.1, ctx=8192)
    "runpod-general": "runpod-general",  # RunPod general settings (temp=0.3, ctx=8192)
}

# Embedding dim must match EpisodicStore table schema (see brain/second_brain/store.py).
# nomic-embed-text and gemini-embedding-001 both produce 768-dim vectors.
EMBEDDING_DIM = 768
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
GOOGLE_EMBED_MODEL = os.environ.get("GOOGLE_EMBED_MODEL", "gemini-embedding-001")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
RUNPOD_HOST = os.environ.get("RUNPOD_HOST", OLLAMA_HOST)
RUNPOD_MODEL = os.environ.get("RUNPOD_MODEL", "qwen2.5:32b")
RUNPOD_HTTP_TIMEOUT = float(os.environ.get("RUNPOD_HTTP_TIMEOUT_SECONDS", "180"))
RUNPOD_KEEP_ALIVE = os.environ.get("RUNPOD_KEEP_ALIVE", "30m")
# The motor planner ("local-code") defaults to the SAME model as the rest of the
# brain (local → qwen2.5:14b). Reason: every other cell (DMN, hippocampus,
# skill_selector, sleep) keeps qwen2.5:14b hot. If the planner used a distinct
# model (e.g. qwen2.5-coder:14b), every tool attempt would force Ollama to
# cold-load a second ~9GB model under memory contention — which exceeds the
# call timeout and makes EVERY tool use fail with "[planner failed]". Sharing the
# hot model eliminates the cold-load entirely. Override via OLLAMA_CODE_MODEL only
# if you have the VRAM headroom to keep a second model resident.
OLLAMA_CODE_MODEL = os.environ.get("OLLAMA_CODE_MODEL", "qwen2.5:14b")
OLLAMA_GENERAL_MODEL = os.environ.get("OLLAMA_GENERAL_MODEL", "qwen2.5:14b")
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

# ChatML special tokens. Qwen2.5 (the default local/RunPod model) emits <|im_end|>
# to close a message. Without it set as a stop sequence, a degraded or
# memory-pressured model can run past end-of-turn and emit <|im_start|> over and
# over until num_predict is hit — corrupting planner JSON and spoken summaries
# (observed in failed job records). We set these as stop tokens on the /api/chat
# payload AND strip them defensively from any returned text.
_CHATML_STOP = ["<|im_end|>", "<|endoftext|>"]
_CHATML_TOKEN_RE = re.compile(r"<\|(?:im_start|im_end|im_sep|endoftext)\|>")


def _strip_chatml(text: str) -> str:
    """Remove leaked ChatML/control tokens and trailing tool-call scaffolding.

    Defense in depth (A2): even with stop tokens set, a degraded model can leak
    special tokens; we never want those persisted into job records or memory.
    """
    if not text:
        return text
    cleaned = _CHATML_TOKEN_RE.sub("", text)
    # Some templates emit a dangling <tool_call> block after the answer when the
    # model fails to close it — drop everything from the first such marker on.
    cleaned = re.sub(r"<tool_call>.*$", "", cleaned, flags=re.DOTALL)
    return cleaned.strip()


# Sonnet 4.6 and Haiku 4.5 pricing ($/1M tokens: input, output, cache_read).
# Used only for logging/budgeting; update if Anthropic changes pricing.
_CLOUD_RATES: dict[str, tuple[float, float, float]] = {
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 0.10),
}
# Path for the per-day cloud-spend file.
_CLOUD_USAGE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "second_brain", "cloud_usage.json"
)


class ModelRouter:
    def __init__(self, obs=None) -> None:
        self._anthropic_client = None
        self._google_client = None
        self._http_client = None  # persistent httpx client; reused across Ollama calls
        self._call_log: list[dict] = []
        self._obs = obs
        # Local-first embeddings; flip to "google" if Ollama is unreachable.
        self._embed_backend = "ollama"
        # Egress pseudonymization gateway (injected from session after creation).
        self._egress = None

        # ── Resource policy ───────────────────────────────────────────────────
        # Background mode: set True while running autonomous/self-initiated work.
        # Cloud calls in this mode are budgeted and capped to prevent bill creep.
        self._bg_mode: bool = False
        # Session-level token counter for background cloud usage (in + out combined).
        # Kept for diagnostics/logging; the rate gate uses the token bucket below.
        self._bg_cloud_tokens_used: int = 0
        # Token bucket for per-hour rate limiting.  Starts full (one hour's allowance).
        # Refills at bg_cloud_token_rate tokens/hour; can go negative (borrows from
        # the next hour).  A call is blocked only when the bucket is at or below 0.
        self._bg_cloud_bucket: float = 100_000.0
        self._bg_cloud_bucket_ts: float = time.monotonic()
        # Lazily-created semaphore; limits concurrent Ollama calls to protect device.
        self._local_semaphore: asyncio.Semaphore | None = None
        # Interactive-turn semaphore: limits concurrent Anthropic calls from live turns.
        self._cloud_semaphore: asyncio.Semaphore | None = None
        # Background-task semaphore: separate pool so DMN/metacognition/motor background
        # calls can never starve interactive-turn calls waiting for a slot.
        self._bg_cloud_semaphore: asyncio.Semaphore | None = None
        # Daily cloud USD tracking — in-memory; loaded from disk lazily.
        self._cloud_usd_today: float = 0.0
        self._cloud_usd_date: str = ""  # "YYYY-MM-DD" (UTC)

    # ── Egress pseudonymization ───────────────────────────────────────────────

    def set_egress(self, gateway) -> None:
        """Inject the session's PseudonymizationGateway.

        Must be called once during session setup (after the gateway is created).
        All subsequent cloud calls will be pseudonymized before dispatch and
        de-pseudonymized on return.
        """
        self._egress = gateway

    def _pseudonymize_messages(self, messages: list[dict]) -> list[dict]:
        """Pseudonymize string message contents; leave structured/multimodal content unchanged."""
        if self._egress is None:
            return messages
        result = []
        for m in messages:
            content = m["content"]
            if isinstance(content, str):
                ps, _ = self._egress.pseudonymize(content)
                result.append({**m, "content": ps})
            else:
                result.append(m)  # list of parts (images, etc.) — don't modify
        return result

    # ── Daily cloud USD budget ────────────────────────────────────────────────

    def _load_cloud_usd_today(self) -> float:
        """Load today's (UTC) accumulated cloud USD from the persistent usage file."""
        import datetime as _dt
        import json

        today = _dt.date.today().isoformat()
        try:
            path = os.path.realpath(_CLOUD_USAGE_PATH)
            with open(path) as f:
                data = json.load(f)
            if data.get("date") == today:
                return float(data.get("usd", 0.0))
        except (FileNotFoundError, Exception):
            pass
        return 0.0

    def _persist_cloud_usd(self) -> None:
        """Persist today's cloud USD spend to disk (best-effort)."""
        import json

        try:
            path = os.path.realpath(_CLOUD_USAGE_PATH)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump({"date": self._cloud_usd_date, "usd": self._cloud_usd_today}, f)
        except Exception as e:
            logger.debug("[ModelRouter] cloud_usage persist failed: %s", e)

    def _charge_cloud_usd(self, model_id: str, in_tok: int, out_tok: int, cache_read: int) -> float:
        """Compute and accumulate USD for a completed cloud call. Returns call cost."""
        ri, ro, rc = _CLOUD_RATES.get(model_id, (3.0, 15.0, 0.30))
        usd = (in_tok * ri + out_tok * ro + cache_read * rc) / 1_000_000
        if usd > 0:
            self._cloud_usd_today = getattr(self, "_cloud_usd_today", 0.0) + usd
            self._persist_cloud_usd()
        return usd

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
        """Current token-bucket level (can be negative when borrowed from next hour)."""
        self._refill_bg_bucket()
        return int(self._bg_cloud_bucket)

    def _refill_bg_bucket(self) -> None:
        """Drip tokens into the bucket based on elapsed wall-clock time."""
        from brain.settings import settings as _settings

        now = time.monotonic()
        elapsed = now - self._bg_cloud_bucket_ts
        self._bg_cloud_bucket_ts = now
        rate_per_hr = float(_settings.get("bg_cloud_token_rate") or 100_000)
        refill = elapsed * rate_per_hr / 3600.0
        # Cap at one full hour's worth so idle time doesn't accumulate indefinitely.
        self._bg_cloud_bucket = min(rate_per_hr, self._bg_cloud_bucket + refill)

    def _get_local_semaphore(self) -> asyncio.Semaphore:
        """Lazily-created concurrency limiter for Ollama calls."""
        if self._local_semaphore is None:
            from brain.settings import settings as _settings

            _s = _settings.get
            limit = int(_s("local_max_concurrent") or 3)
            self._local_semaphore = asyncio.Semaphore(limit)
        return self._local_semaphore

    def _get_cloud_semaphore(self) -> asyncio.Semaphore:
        """Concurrency limiter for interactive-turn Anthropic cloud calls.

        Returns the background-specific semaphore when in bg_mode so DMN /
        metacognition / motor background tasks draw from their own pool and
        can never starve live-turn cells waiting for a slot.
        """
        if self._bg_mode:
            if self._bg_cloud_semaphore is None:
                from brain.settings import settings as _settings

                limit = int(_settings.get("bg_cloud_max_concurrent") or 2)
                self._bg_cloud_semaphore = asyncio.Semaphore(limit)
            return self._bg_cloud_semaphore
        if self._cloud_semaphore is None:
            from brain.settings import settings as _settings

            limit = int(_settings.get("cloud_max_concurrent") or 3)
            self._cloud_semaphore = asyncio.Semaphore(limit)
        return self._cloud_semaphore

    def _get_anthropic(self):
        if self._anthropic_client is None:
            import anthropic
            import httpx

            from brain.settings import settings as _settings

            # Explicit timeout + bounded retries so NO cloud call can hang
            # indefinitely. The SDK's default (600s) let a stalled connection
            # freeze whole motor-cortex jobs at the strategic-plan call. A short
            # connect timeout catches dead sockets fast; the read timeout bounds
            # long generations. Tunable via settings.
            _read_to = float(_settings.get("anthropic_timeout_s") or 120.0)
            _connect_to = float(_settings.get("anthropic_connect_timeout_s") or 10.0)
            _retries = int(_settings.get("anthropic_max_retries") or 2)
            self._anthropic_client = anthropic.AsyncAnthropic(
                api_key=os.environ["ANTHROPIC_API_KEY"],
                timeout=httpx.Timeout(_read_to, connect=_connect_to),
                max_retries=_retries,
            )
        return self._anthropic_client

    def _get_google(self):
        if self._google_client is None:
            key = os.environ.get("GOOGLE_API_KEY")
            if not key:
                # Clean, catchable error instead of a bare KeyError — lets callers
                # (and the occipital vision guard) degrade gracefully when no key.
                raise RuntimeError("GOOGLE_API_KEY not set — Gemini/vision unavailable")
            from google import genai

            self._google_client = genai.Client(api_key=key)
        return self._google_client

    def _get_http(self):
        """Lazily-created persistent httpx client; avoids a new TCP connection per Ollama call.

        Configured with a SHORT keepalive expiry: a RunPod pod that restarts keeps the
        same proxy URL, so without this the pool would reuse dead keep-alive sockets to
        a restarted backend and inference would stop reconnecting. A short expiry drops
        idle sockets quickly; _reset_http() force-rebuilds the pool after a hard failure."""
        if self._http_client is None:
            import httpx

            self._http_client = httpx.AsyncClient(
                limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=15.0),
            )
        return self._http_client

    async def _reset_http(self) -> None:
        """Drop and close the pooled httpx client so the next call builds fresh
        connections. Called after a connection-class failure — e.g. a RunPod pod
        restart leaving stale keep-alive sockets to the same proxy URL."""
        client = self._http_client
        self._http_client = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    @staticmethod
    def _resolve_local_model(model_key: str) -> str | None:
        """Map a local/runpod model_key to the concrete Ollama model name, or None if not local."""
        if model_key in ("runpod", "runpod-free", "runpod-code", "runpod-general"):
            return RUNPOD_MODEL
        if model_key == "local-code":
            return OLLAMA_CODE_MODEL
        if model_key == "local-general":
            return OLLAMA_GENERAL_MODEL
        if model_key in ("local", "local-free"):
            return os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")
        return None

    async def warmup_local(
        self, model_key: str = "local-code", timeout: float | None = None
    ) -> bool:
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
        to = timeout if timeout is not None else OLLAMA_MODEL_LOAD_TIMEOUT
        is_runpod = model_key.startswith("runpod")
        if is_runpod:
            from brain.settings import settings as _s

            host = str(_s.get("runpod_host") or "") or RUNPOD_HOST
            keep_alive = RUNPOD_KEEP_ALIVE
        else:
            host = OLLAMA_HOST
            keep_alive = OLLAMA_KEEP_ALIVE
        try:
            # POST /api/generate with no prompt loads the model and returns immediately
            # once it's resident (Ollama's documented preload mechanism).
            async with self._get_local_semaphore():
                r = await self._get_http().post(
                    f"{host}/api/generate",
                    json={"model": model_name, "keep_alive": keep_alive},
                    timeout=to,
                )
            r.raise_for_status()
            logger.info("[ModelRouter] Warmed up local model %s", model_name)
            return True
        except Exception as e:
            logger.warning(
                "[ModelRouter] Warmup of %s failed (continuing anyway): %s", model_name, e
            )
            return False

    async def call(
        self,
        model_key: str,
        system_prompt: str,
        messages: list[dict],
        *,
        cluster: str = "",
        cell: str = "",
        turn_id: str = "",
        locality: str = "either",
        max_tokens: int = 1024,
        skills: list[str] | None = None,
        temperature: float | None = None,
        cached_context: str = "",
    ) -> str:
        from brain.settings import settings as _settings

        _s = _settings.get

        model_id = MODEL_MAP.get(model_key, model_key)

        # Locality enforcement: local cells must never dispatch to cloud APIs
        _is_cloud = model_id.startswith("claude") or model_id.startswith("gemini")
        if locality == "local" and _is_cloud:
            logger.warning(
                "[Security] Memory cell %s/%s is restricted to local-only inference but model '%s' "
                "routes to a cloud API — redirecting to Ollama. If Ollama isn't running this call "
                "will fail. Fix: run 'ollama serve' and 'ollama pull qwen2.5:14b'.",
                cluster,
                cell,
                model_id,
            )
            model_key = (
                model_key
                if model_key in ("local", "local-free", "local-code", "local-general")
                else "local"
            )
            model_id = model_key
            _is_cloud = False

        # Background mode: apply per-hour rate limit + per-call caps to protect
        # against runaway spend on autonomous work.
        if self._bg_mode and _is_cloud:
            self._refill_bg_bucket()
            rate_per_hr = float(_s("bg_cloud_token_rate") or 100_000)
            if self._bg_cloud_bucket <= 0:
                logger.warning(
                    "[Resource] Background rate-limited (bucket: %d tokens, "
                    "refilling at %.0f/hr, session total: %d) "
                    "— routing %s/%s to local for this call.",
                    int(self._bg_cloud_bucket),
                    rate_per_hr,
                    self._bg_cloud_tokens_used,
                    cluster,
                    cell,
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
                        cluster,
                        cell,
                        max_tokens,
                        call_cap,
                    )
                    max_tokens = call_cap

        # Daily cloud USD budget — hard ceiling across all cloud providers.
        if _is_cloud:
            import datetime as _dt

            today_str = _dt.date.today().isoformat()
            # Guard with getattr: tests may construct ModelRouter via __new__,
            # bypassing __init__; these attributes might not exist in that case.
            if getattr(self, "_cloud_usd_date", "") != today_str:
                self._cloud_usd_date = today_str
                self._cloud_usd_today = self._load_cloud_usd_today()
            daily_cap = float(_s("cloud_daily_usd_budget") or 0.0)
            if daily_cap > 0 and self._cloud_usd_today >= daily_cap:
                logger.warning(
                    "[Resource] Daily cloud USD cap reached ($%.4f / $%.2f) "
                    "— routing %s/%s to local for the rest of today.",
                    self._cloud_usd_today,
                    daily_cap,
                    cluster,
                    cell,
                )
                model_key = "local"
                model_id = "local"
                _is_cloud = False

        # ── R1 egress pseudonymization (gateway backstop before every cloud call) ──
        # Idempotent: already-tokenized content (⟨type_n⟩) doesn't match PII patterns.
        # The interactive turn path in session_turn.py also pseudonymizes before calling
        # here; this catches all OTHER paths (motor, DMN, metacognition) that bypass it.
        _egress_active = False
        if _is_cloud and getattr(self, "_egress", None) is not None:
            from brain.security import EGRESS_MODE as _egress_mode

            if _egress_mode != "off":
                _egress_active = True
                system_prompt, _n = self._egress.pseudonymize(system_prompt)
                messages = self._pseudonymize_messages(messages)
                if cached_context:
                    # Pseudonymization is deterministic (same PII → same token), so the
                    # tokenized context stays byte-stable across turns and still caches.
                    cached_context, _ = self._egress.pseudonymize(cached_context)
                if _n > 0:
                    logger.debug(
                        "[Egress] %s/%s: %d PII replacements in system_prompt", cluster, cell, _n
                    )

        start = time.time()
        bg_timeout = (
            float(_s("bg_cloud_timeout_s") or 20.0) if (self._bg_mode and _is_cloud) else None
        )

        # Inject skill text into the system prompt — LOCAL/RunPod backends ONLY.
        # Cloud models (Claude, Gemini) already have these reasoning/skill frameworks
        # natively, so injecting local skill copies is redundant prompt bloat (and cost).
        # Gating on the resolved route (_is_cloud) means this adapts automatically as
        # cells are moved between local and cloud models — the DMN and other Ollama/
        # RunPod cells keep their skills; anything routed to Claude does not.
        if skills and not _is_cloud:
            from brain.skill_loader import SkillLoader

            skill_block = SkillLoader.load_many(skills)
            if skill_block:
                system_prompt = f"{system_prompt}\n\n{skill_block}"

        # Backends without prompt caching (Gemini, local) can't use a separate cached
        # block — fold the per-session context into the system prompt so its content is
        # never lost. Only the Anthropic path passes it as a dedicated cached block.
        system_with_context = (
            f"{system_prompt}\n\n{cached_context}" if cached_context else system_prompt
        )

        cache_read = 0
        if model_id.startswith("claude"):
            try:
                async with self._get_cloud_semaphore():
                    coro = self._call_anthropic(
                        model_id, system_prompt, messages, max_tokens, cached_context=cached_context
                    )
                    if bg_timeout:
                        text, in_tok, out_tok, cache_read, cache_write = await asyncio.wait_for(
                            coro, timeout=bg_timeout
                        )
                    else:
                        text, in_tok, out_tok, cache_read, cache_write = await coro
            except TimeoutError:
                logger.warning(
                    "[Resource] Background cloud call %s/%s timed out after %.0fs — falling back to local.",
                    cluster,
                    cell,
                    bg_timeout,
                )
                text, in_tok, out_tok = await self._call_local(
                    system_with_context, messages, max_tokens
                )
                cache_read = 0
            if self._bg_mode:
                spent = in_tok + out_tok
                self._bg_cloud_tokens_used += spent
                self._bg_cloud_bucket -= spent
                logger.debug(
                    "[Resource] BG cloud bucket: %d tokens remaining (this call: %d+%d, session: %d)",
                    int(self._bg_cloud_bucket),
                    in_tok,
                    out_tok,
                    self._bg_cloud_tokens_used,
                )
            # USD tracking + logging
            usd = self._charge_cloud_usd(model_id, in_tok, out_tok, cache_read)
            if cache_read > 0:
                logger.info(
                    "[Cache] %s/%s: %d cache-read tokens (%.4f¢ saved vs uncached)",
                    cluster,
                    cell,
                    cache_read,
                    cache_read * 0.27 / 10_000,
                )
            logger.debug(
                "[Cloud] %s/%s: %d in + %d out + %d cached → $%.5f (day total $%.4f)",
                cluster,
                cell,
                in_tok,
                out_tok,
                cache_read,
                usd,
                self._cloud_usd_today,
            )
        elif model_id.startswith("gemini"):
            try:
                coro = self._call_google(model_id, system_with_context, messages, max_tokens)
                if bg_timeout:
                    text, in_tok, out_tok = await asyncio.wait_for(coro, timeout=bg_timeout)
                else:
                    text, in_tok, out_tok = await coro
            except TimeoutError:
                logger.warning(
                    "[Resource] Background cloud call %s/%s timed out after %.0fs — falling back to local.",
                    cluster,
                    cell,
                    bg_timeout,
                )
                text, in_tok, out_tok = await self._call_local(
                    system_with_context, messages, max_tokens
                )
            if self._bg_mode:
                spent = in_tok + out_tok
                self._bg_cloud_tokens_used += spent
                self._bg_cloud_bucket -= spent
                logger.debug(
                    "[Resource] BG cloud bucket: %d tokens remaining (this call: %d+%d, session: %d)",
                    int(self._bg_cloud_bucket),
                    in_tok,
                    out_tok,
                    self._bg_cloud_tokens_used,
                )
        elif model_id in (
            "local",
            "local-free",
            "local-code",
            "local-general",
            "runpod",
            "runpod-free",
            "runpod-code",
            "runpod-general",
        ):
            text, in_tok, out_tok = await self._call_local(
                system_with_context,
                messages,
                max_tokens,
                local_variant=model_id,
                temperature=temperature,
            )
        else:
            raise ValueError(f"Unknown model key: {model_key}")

        # ── Depseudonymize cloud response (restores ⟨type_n⟩ → real values) ──────
        if _egress_active:
            from brain.security import EGRESS_MODE as _egress_mode2

            if _egress_mode2 not in ("redact", "block"):
                text = self._egress.depseudonymize(text)

        latency = time.time() - start
        self._log_call(
            model_id,
            messages,
            in_tok,
            out_tok,
            latency,
            cluster=cluster or "",
            cell=cell or "",
            skills=skills or [],
        )
        if self._obs and turn_id:
            try:
                self._obs.record_llm_call(
                    turn_id=turn_id,
                    cluster=cluster or "unknown",
                    cell=cell or "unknown",
                    model=model_id,
                    prompt_tokens=in_tok,
                    completion_tokens=out_tok,
                    latency_s=latency,
                    skills=skills or [],
                )
            except Exception as e:
                logger.debug("obs.record_llm_call failed: %s", e)
        return text

    async def call_structured(
        self,
        model_key: str,
        system_prompt: str,
        messages: list[dict],
        tool_name: str,
        tool_description: str,
        tool_schema: dict,
        *,
        cluster: str = "",
        cell: str = "",
        turn_id: str = "",
        max_tokens: int = 4096,
    ) -> dict:
        """Force Claude to return structured output via native tool_use.

        Defines a single tool with the given schema and tool_choice forcing Claude
        to call it. Returns the tool input dict directly — no JSON parsing needed.
        Falls back to empty dict on any error.
        """
        from brain.settings import settings as _settings

        _s = _settings.get
        model_id = MODEL_MAP.get(model_key, model_key)
        _is_cloud = model_id.startswith("claude") or model_id.startswith("gemini")

        # Background mode: apply per-hour rate limit — if exhausted, fall back gracefully.
        if self._bg_mode and _is_cloud:
            self._refill_bg_bucket()
            rate_per_hr = float(_s("bg_cloud_token_rate") or 100_000)
            if self._bg_cloud_bucket <= 0:
                logger.warning(
                    "[Resource] Background rate-limited (bucket: %d tokens, "
                    "refilling at %.0f/hr, session total: %d) "
                    "— call_structured %s/%s falling back to empty dict.",
                    int(self._bg_cloud_bucket),
                    rate_per_hr,
                    self._bg_cloud_tokens_used,
                    cluster,
                    cell,
                )
                return {}

        try:
            client = self._get_anthropic()
            anthropic_msgs = [{"role": m["role"], "content": m["content"]} for m in messages]
            tools = [
                {
                    "name": tool_name,
                    "description": tool_description,
                    "input_schema": tool_schema,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            # Hard wait_for bound on top of the client timeout so a structured
            # planning call can never outlive its budget AND never holds the
            # cloud semaphore indefinitely (which would starve other cloud cells).
            _struct_to = float(_s("structured_call_timeout_s") or 150.0)
            async with self._get_cloud_semaphore():
                response = await asyncio.wait_for(
                    client.messages.create(
                        model=model_id,
                        max_tokens=max_tokens,
                        system=[
                            {
                                "type": "text",
                                "text": system_prompt,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                        messages=anthropic_msgs,
                        tools=tools,
                        tool_choice={"type": "tool", "name": tool_name},
                    ),
                    timeout=_struct_to,
                )
            usage = getattr(response, "usage", None)
            in_tok = getattr(usage, "input_tokens", 0) if usage else 0
            out_tok = getattr(usage, "output_tokens", 0) if usage else 0
            cache_read = getattr(usage, "cache_read_input_tokens", 0) if usage else 0

            if self._bg_mode:
                spent = in_tok + out_tok
                self._bg_cloud_tokens_used += spent
                self._bg_cloud_bucket -= spent
                logger.debug(
                    "[Resource] BG cloud bucket: %d tokens remaining (call_structured %s/%s: %d+%d, session: %d)",
                    int(self._bg_cloud_bucket),
                    cluster,
                    cell,
                    in_tok,
                    out_tok,
                    self._bg_cloud_tokens_used,
                )
            self._charge_cloud_usd(model_id, in_tok, out_tok, cache_read)
            if cache_read > 0:
                logger.info(
                    "[Cache] %s/%s: %d cache-read tokens (%.4f¢ saved vs uncached)",
                    cluster,
                    cell,
                    cache_read,
                    cache_read * 0.27 / 10_000,
                )

            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    return block.input or {}
            logger.warning(
                "[ModelRouter] call_structured %s/%s: no tool_use block in response", cluster, cell
            )
            return {}
        except Exception as e:
            logger.warning("[ModelRouter] call_structured %s/%s failed: %s", cluster, cell, e)
            return {}

    async def _call_anthropic(
        self,
        model_id: str,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int = 1024,
        cached_context: str = "",
    ) -> tuple[str, int, int, int, int]:
        """Call Anthropic Claude. Returns (text, in_tok, out_tok, cache_read, cache_write).

        Prompt-cache layout (up to 4 breakpoints; we use 2–3):
          1. system_prompt — global static identity, cached, shared across all users.
          2. cached_context — per-session-stable context (full self-model + user-model,
             capabilities), cached, no truncation. Stable within a session, so it's a
             cache READ on every turn after the first. Omitted when empty.
          3. last message — volatile turn content; cached so intra-turn drafter calls
             (all 3 drafters in one turn) read after the first write.
        The live affection score must NOT go in cached_context — it ticks each turn and
        would bust the per-session cache. It stays in the volatile message tail.
        """
        client = self._get_anthropic()

        # Build messages, marking the last message for caching so intra-turn calls
        # (e.g. all 3 drafters within one turn) hit the cache after the first write.
        anthropic_msgs = []
        for i, m in enumerate(messages):
            content = m["content"]
            if i == len(messages) - 1:
                if isinstance(content, str):
                    content = [
                        {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                    ]
                elif isinstance(content, list) and content:
                    content = list(content)
                    last = dict(content[-1])
                    last["cache_control"] = {"type": "ephemeral"}
                    content[-1] = last
            anthropic_msgs.append({"role": m["role"], "content": content})

        system_blocks = [
            {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
        ]
        if cached_context:
            system_blocks.append(
                {"type": "text", "text": cached_context, "cache_control": {"type": "ephemeral"}}
            )

        response = await client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=anthropic_msgs,
        )
        usage = getattr(response, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) if usage else 0
        out_tok = getattr(usage, "output_tokens", 0) if usage else 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) if usage else 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) if usage else 0
        return response.content[0].text, in_tok, out_tok, cache_read, cache_write

    async def _call_google(
        self, model_id: str, system_prompt: str, messages: list[dict], max_tokens: int = 1024
    ) -> tuple[str, int, int]:
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
                        parts.append(
                            types.Part(
                                inline_data=types.Blob(mime_type=part["mime"], data=part["data"])
                            )
                        )
                contents.append(types.Content(role=role, parts=parts))
            else:
                contents.append(types.Content(role=role, parts=[types.Part(text=raw)]))

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
            return " ".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        return content or ""

    async def _call_local(
        self,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int = 1024,
        local_variant: str = "local",
        temperature: float | None = None,
    ) -> tuple[str, int, int]:
        flat_messages = [
            {"role": m["role"], "content": self._flatten_content(m["content"])} for m in messages
        ]
        is_runpod = local_variant.startswith("runpod")
        # For runpod variants, normalise to the equivalent local variant for options lookup
        options_variant = local_variant.replace("runpod", "local") if is_runpod else local_variant
        if is_runpod:
            from brain.settings import settings as _s

            host = str(_s.get("runpod_host") or "") or RUNPOD_HOST
            http_timeout = RUNPOD_HTTP_TIMEOUT
            keep_alive = RUNPOD_KEEP_ALIVE
        else:
            host = OLLAMA_HOST
            http_timeout = OLLAMA_HTTP_TIMEOUT
            keep_alive = OLLAMA_KEEP_ALIVE
        base_model = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")
        if is_runpod:
            from brain.settings import settings as _s

            model_name = str(_s.get("runpod_model") or "") or RUNPOD_MODEL
        elif options_variant == "local-code":
            model_name = OLLAMA_CODE_MODEL
        elif options_variant == "local-general":
            model_name = OLLAMA_GENERAL_MODEL
        else:
            # local and local-free both use the base model
            model_name = base_model

        options: dict = {"num_predict": max_tokens}
        use_json_format = False

        if options_variant == "local-code":
            # Tool planner — deterministic; large context for system prompt + skill blocks
            options["temperature"] = 0.1
            options["num_ctx"] = 8192
            use_json_format = True
        elif options_variant == "local-general":
            # Sleep consolidation (all three cells return JSON)
            options["temperature"] = 0.3
            options["num_ctx"] = 8192
            use_json_format = True
        elif options_variant == "local-free":
            # Plain-text output only (speak_bridge rewriter) — needs creative latitude
            options["temperature"] = 0.7
            options["num_ctx"] = 2048
        else:
            # local — hippocampus + all DMN JSON cells; format:json ensures valid structure
            # while temp=0.3 keeps content focused without killing variety in thought fields
            options["temperature"] = 0.3
            # RunPod: cap at 8192 — prefill for 16k context on 32B exceeds cell timeouts
            options["num_ctx"] = 8192 if is_runpod else 16384
            use_json_format = True

        # Per-cell override (e.g. the DMN monologue runs hot for divergent ideation).
        if temperature is not None:
            options["temperature"] = float(temperature)

        # Stop on the ChatML end-of-message token so the model halts at end-of-turn
        # instead of degenerating into repeated <|im_start|> until num_predict.
        options["stop"] = _CHATML_STOP
        payload: dict = {
            "model": model_name,
            "messages": [{"role": "system", "content": system_prompt}] + flat_messages,
            "stream": is_runpod,  # stream=true for RunPod — keeps proxy alive during long prefill
            "options": options,
            "keep_alive": keep_alive,
        }
        if use_json_format:
            payload["format"] = "json"
        if is_runpod:
            import json as _json

            # Bounded retry-with-reconnect. After a pod restart the first attempt
            # typically fails on a stale keep-alive socket; we drop the pooled client
            # (_reset_http) and retry on a fresh connection, then fall back to a single
            # non-streaming POST. This is what makes inference RECONNECT after a restart
            # instead of silently returning "" forever.
            attempts = int(_s.get("runpod_stream_retries", 2)) + 1
            for attempt in range(attempts):
                text_parts: list[str] = []
                in_tok = out_tok = 0
                got_done = False
                try:
                    async with self._get_http().stream(
                        "POST", f"{host}/api/chat", json=payload, timeout=http_timeout
                    ) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            try:
                                chunk = _json.loads(line)
                                delta = (chunk.get("message") or {}).get("content", "")
                                if delta:
                                    text_parts.append(delta)
                                if chunk.get("done"):
                                    got_done = True
                                    in_tok = int(chunk.get("prompt_eval_count", 0))
                                    out_tok = int(chunk.get("eval_count", 0))
                            except Exception:
                                pass
                    if text_parts or got_done:
                        return _strip_chatml("".join(text_parts)), in_tok, out_tok
                    # Connected but produced nothing — treat as a soft failure and retry.
                    logger.warning(
                        "[RunPod] stream produced no content (attempt %d/%d)", attempt + 1, attempts
                    )
                except Exception as e:
                    logger.warning(
                        "[RunPod] stream error (attempt %d/%d): %s", attempt + 1, attempts, e
                    )
                    await self._reset_http()  # drop stale sockets before retrying
                if attempt < attempts - 1:
                    await asyncio.sleep(min(2.0, 0.5 * (attempt + 1)))

            # All stream attempts failed — last-resort non-streaming POST on a fresh
            # connection. Degrades gracefully rather than returning empty.
            try:
                await self._reset_http()
                async with self._get_local_semaphore():
                    r = await self._get_http().post(
                        f"{host}/api/chat", json={**payload, "stream": False}, timeout=http_timeout
                    )
                r.raise_for_status()
                data = r.json()
                return (
                    _strip_chatml(data["message"]["content"]),
                    int(data.get("prompt_eval_count", 0)),
                    int(data.get("eval_count", 0)),
                )
            except Exception as e:
                logger.warning(
                    "[RunPod] post fallback failed after %d stream attempts: %s", attempts, e
                )
                return "", 0, 0
        else:
            async with self._get_local_semaphore():
                r = await self._get_http().post(
                    f"{host}/api/chat", json=payload, timeout=http_timeout
                )
            r.raise_for_status()
            data = r.json()
            in_tok = int(data.get("prompt_eval_count", 0))
            out_tok = int(data.get("eval_count", 0))
            return _strip_chatml(data["message"]["content"]), in_tok, out_tok

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
                    len(vec),
                    EMBEDDING_DIM,
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
                    len(vec),
                    EMBEDDING_DIM,
                )
            return None
        except Exception as e:
            logger.warning(
                "Google embedding API failed — memory search may be degraded this turn: %s", e
            )
            return None

    def _log_call(
        self,
        model_id: str,
        messages: list[dict],
        in_tok: int = 0,
        out_tok: int = 0,
        latency_s: float = 0.0,
        cluster: str = "",
        cell: str = "",
        skills: list[str] | None = None,
    ) -> None:
        self._call_log.append(
            {
                "model": model_id,
                "msgs": len(messages),
                "in": in_tok,
                "out": out_tok,
                "latency_s": latency_s,
                "cluster": cluster,
                "cell": cell,
                "skills": skills or [],
            }
        )

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
