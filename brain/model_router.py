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
from collections import OrderedDict

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
    # OpenAI-compatible cloud (also Groq/Mistral/DeepSeek/Together via OPENAI_BASE_URL).
    "gpt": os.environ.get("OPENAI_MODEL", "gpt-5.1"),
    "gpt-mini": os.environ.get("OPENAI_MODEL_MINI", "gpt-5-mini"),
    # Vertex AI (Model Garden). Resolved ids keep the "vertex-" prefix so
    # _provider_for routes them to the Vertex client. Auth is ADC (service account
    # / workload identity / `gcloud auth application-default login`), NOT an API
    # key. Additive: nothing routes here unless cloud_provider=vertex or a cell
    # explicitly names one of these keys.
    "vertex-gemini-flash": "vertex-gemini-2.5-flash",
    "vertex-gemini-pro": "vertex-gemini-2.5-pro",
    "vertex-claude": "vertex-" + os.environ.get("VERTEX_CLAUDE_MODEL", "claude-sonnet-4-5@20250929"),
    "vertex-claude-haiku": "vertex-"
    + os.environ.get("VERTEX_CLAUDE_HAIKU_MODEL", "claude-haiku-4-5@20251001"),
}

_LOCAL_VARIANTS = frozenset(
    {
        "local",
        "local-free",
        "local-code",
        "local-general",
        "runpod",
        "runpod-free",
        "runpod-code",
        "runpod-general",
    }
)

# A 'lite'-tier brain holds no local pod, so cell config's local routes must run on
# cloud instead. This names the cheap cloud model_key they fall back to. The cloud-vs-
# local TRUTH still lives in cell config + _provider_for; this is only the per-brain
# permission gate (see ModelRouter._local_disabled).
_LITE_CLOUD_KEY = os.environ.get("BRAIN_LITE_CLOUD_MODEL_KEY", "haiku")

# A lite brain has no local pod, so the daily-USD ceiling can't "degrade to local"
# the way a full brain does — it must HARD-STOP instead. This gives every lite brain
# a finite ceiling out of the box so a misbehaving tenant/job (e.g. a runaway ingest
# loop) can never bill unbounded. Overridable via env; 0 disables the default (an
# explicit org/agent cap still applies). A full brain ignores this entirely.
_LITE_DEFAULT_DAILY_USD_CAP = float(os.environ.get("BRAIN_LITE_DAILY_USD_CAP", "25") or 0)


class CloudBudgetExceeded(RuntimeError):
    """A cloud call was blocked because the org/agent daily USD ceiling is reached AND
    there is no local pod to fall back to (lite tier). On a full brain the same
    condition degrades to local instead of raising. Raised BEFORE the cloud call is
    dispatched, so it never costs anything; the engine API maps it to HTTP 402."""


def _provider_for(model_id: str) -> str:
    """Which client a resolved model id dispatches to. Anything that isn't
    Claude/Gemini/local routes through the OpenAI-compatible client — which is
    how GPT and every base_url-compatible provider (Groq, Mistral, DeepSeek,
    Together) plug in without new client code."""
    if model_id.startswith("vertex-"):
        return "vertex"
    if model_id.startswith("claude"):
        return "anthropic"
    if model_id.startswith("gemini"):
        return "google"
    if model_id in _LOCAL_VARIANTS:
        return "local"
    return "openai"


def _remap_cloud_provider(model_id: str, cluster: str) -> str:
    """The cognition-provider lever: cloud_provider=openai reroutes Claude-bound
    calls to the configured OpenAI model (haiku-class work → the mini model).
    The motor cluster is exempt — its tool-use loop is Anthropic-shaped — and
    non-Claude routes (Gemini, local) are untouched."""
    from brain.settings import settings as _settings

    if cluster == "motor" or not model_id.startswith("claude"):
        return model_id
    _cp = str(_settings.get("cloud_provider", "anthropic")).lower()
    if _cp == "openai":
        return MODEL_MAP["gpt-mini"] if "haiku" in model_id else MODEL_MAP["gpt"]
    # Serve the reasoning path via Google-hosted Claude (Vertex Model Garden) when
    # selected AND Vertex is enabled — the SAME Claude models, billed/authed through
    # Google. Otherwise leave it on the Anthropic API.
    if _cp == "vertex" and int(_settings.get("enable_vertex", 0) or 0):
        return MODEL_MAP["vertex-claude-haiku"] if "haiku" in model_id else MODEL_MAP["vertex-claude"]
    return model_id


# Embedding dim must match EpisodicStore table schema (see brain/second_brain/store.py).
# nomic-embed-text and gemini-embedding-001 both produce 768-dim vectors.
EMBEDDING_DIM = 768
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
GOOGLE_EMBED_MODEL = os.environ.get("GOOGLE_EMBED_MODEL", "gemini-embedding-001")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
RUNPOD_HOST = os.environ.get("RUNPOD_HOST", OLLAMA_HOST)
# Dedicated embeddings host (import-time ⚠). Embeddings are the highest-volume
# model call (10-15/turn) but need no GPU — nomic-embed-text runs fine on CPU.
# On Railway the gateway runs a CPU Ollama sidecar and points every tenant here,
# so embeds stop depending on the GPU pod (or silently falling back to Google).
# Empty → fall through to OLLAMA_HOST, byte-identical to the old behavior.
OLLAMA_EMBED_HOST = os.environ.get("OLLAMA_EMBED_HOST", "").strip()
# NO runpod/local → cloud fallback. A cell whose model is local/runpod is designed to
# run on the GPU pod (or a real local Ollama); it must NEVER silently bill Claude when
# the pod is unreachable. If the pod is down, the local call fails and the calling cell
# degrades (e.g. DMN backs off) — it does not fall back to a cloud model. The reverse
# direction (a cloud cell shedding to local under timeout / budget cap) is still allowed.
RUNPOD_MODEL = os.environ.get("RUNPOD_MODEL", "qwen2.5:32b")
RUNPOD_HTTP_TIMEOUT = float(os.environ.get("RUNPOD_HTTP_TIMEOUT_SECONDS", "180"))
# Negative keep_alive ("-1m") = never unload. The pod bills per-second whether or not
# the weights sit in VRAM, so unloading saves nothing — it only let the model go
# "absent" during quiet spells, which the pod owner then mistook for an unhealthy pod
# (terminate→recreate churn). Must match brain/runpod_manager._keep_alive's default.
RUNPOD_KEEP_ALIVE = os.environ.get("RUNPOD_KEEP_ALIVE", "-1m")
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


# Cloud model pricing ($/1M tokens: input, output, cache_read).
# Used for logging/budgeting AND for metering the CMA managed-agents path (whose
# model is the org's `cma_model`, default Sonnet 4.6; Opus tiers kept for orgs that
# override). Both the dated and alias model strings are listed so a price lands
# whichever form the setting carries. Update if Anthropic changes pricing.
_CLOUD_RATES: dict[str, tuple[float, float, float]] = {
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 0.10),
    # OpenAI (budgeting estimates; unknown models fall back to the Sonnet-class
    # default below, so a wrong guess only skews the log line, never the call).
    "gpt-5.1": (1.25, 10.0, 0.125),
    "gpt-5-mini": (0.25, 2.0, 0.025),
}
# Path for the per-day cloud-spend file. Resolve under SECOND_BRAIN_PATH so each
# hosted tenant counts its own daily spend on its own volume. A repo-relative
# path would make cloud_daily_usd_budget a single global counter shared by every
# tenant (one user's spend trips fallback-to-local for everyone, concurrent
# writes race) and lose it on redeploy.
_CLOUD_USAGE_PATH = os.path.join(
    os.environ.get(
        "SECOND_BRAIN_PATH",
        os.path.join(os.path.dirname(__file__), "..", "second_brain"),
    ),
    "cloud_usage.json",
)


def _coerce_local_decision(raw_text: str) -> dict:
    """Interpret a local model's reply to the tool/answer protocol
    ({"tool":...,"args":...} | {"text":"<summary>"}) into that decision, robust to
    a small model's JSON slips.

    The failure this guards: a local model that crams its answer into a degenerate
    object — the summary used as BOTH key and value, e.g. {"<summary>":"<summary>"}
    (no "text" key). The old fallback (parsed.get("text", raw_text)) then returned
    raw_text — the JSON blob itself — which surfaced verbatim as the spoken result
    ({ "I found that…": "I found that…" }). Instead, recover the answer from the
    object's strings (longest value, else key). A non-JSON reply is plain prose and
    passes through untouched.
    """
    from brain.utils import safe_json_parse

    parsed = safe_json_parse(raw_text)
    if isinstance(parsed, dict):
        if parsed.get("tool"):
            return {"tool": str(parsed["tool"]), "args": parsed.get("args") or {}}
        if isinstance(parsed.get("text"), str):
            return {"text": parsed["text"]}
        # Degenerate object (no usable tool/text key): recover the crammed answer
        # rather than echoing the raw JSON. Prefer values, then keys.
        strings = [
            s for s in (*parsed.values(), *parsed.keys()) if isinstance(s, str) and s.strip()
        ]
        if strings:
            return {"text": max(strings, key=len)}
    return {"text": raw_text}


class ModelRouter:
    def __init__(self, obs=None) -> None:
        self._anthropic_client = None
        self._google_client = None
        self._vertex_gemini_client = None  # google-genai in Vertex mode (ADC auth)
        self._vertex_anthropic_client = None  # AnthropicVertex (Claude on Vertex)
        self._http_client = None  # persistent httpx client; reused across Ollama calls
        self._call_log: list[dict] = []
        self._obs = obs
        # Local-first embeddings; flip to "google" if Ollama is unreachable.
        self._embed_backend = "ollama"
        # Small LRU over recent embeddings — the same texts recur within a session
        # (DMN predictions re-checked per turn, dedup backfill, repeated recall
        # queries) and re-embedding them is pure waste.
        self._embed_cache: OrderedDict[str, list[float]] = OrderedDict()
        # Egress pseudonymization gateway (injected from session after creation).
        self._egress = None

        # ── Resource policy ───────────────────────────────────────────────────
        # Background mode: set True while running autonomous/self-initiated work.
        # Cloud calls in this mode are budgeted and capped to prevent bill creep.
        self._bg_mode: bool = False
        # Tier gate: a 'lite' brain has no local pod, so ALL local routing is disabled
        # and falls back to cloud. Set from the per-brain tier injected at spawn
        # (BRAIN_TIER). This is the single per-brain enforcement of local-permission;
        # the cloud-vs-local truth itself still lives in cell config + _provider_for.
        self._local_disabled: bool = os.environ.get("BRAIN_TIER", "full").strip().lower() == "lite"
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
        # Autonomous-only slice of today's spend (charged when _bg_mode). A separate
        # pool so a busy interactive day never trips the autonomous soft/hard caps and
        # vice-versa; persisted alongside `usd` in cloud_usage.json (see AutonomousBudget).
        self._cloud_usd_autonomous_today: float = 0.0
        # UTC date on which the owner cleared the autonomous soft-pause ("keep spending
        # today"); soft pause is lifted while this == today, still bounded by the hard cap.
        self._autonomous_soft_cleared_date: str = ""
        # One-shot signal set when an autonomous (bg) cloud call could not proceed
        # (rate bucket empty / budget / cloud unreachable) instead of degrading to a
        # (non-existent) local backend. The motor consumes it via take_bg_defer() to
        # turn the empty planner result into a clean DEFERRED job rather than a failure.
        self._bg_defer_reason = None
        # Injected SpendRiskGate (session setup) so the router can feed it cloud-health
        # timeouts/successes. Optional — never assume it's set.
        self._spend_gate = None
        # Per-agent token + cost meter for the Agents dashboard: which engine-API
        # agent drove the model, how much. Keyed by agent_id (turn_ctx lane);
        # owner/idle turns bucket under "owner" and are hidden from the dashboard.
        # In-memory + process-session scoped (see agent_usage()).
        self._agent_usage: dict[str, dict] = {}
        # High-water snapshot of _agent_usage at the last durable flush, so
        # flush_usage() can persist only the delta since then (migration 016).
        self._usage_flushed: dict[str, dict] = {}

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

    def _load_cloud_usage(self) -> dict:
        """Load today's (UTC) persisted usage record ({usd, usd_autonomous, soft_cleared}).
        Empty dict if the file is missing or from a previous UTC day."""
        import datetime as _dt
        import json

        today = _dt.date.today().isoformat()
        try:
            path = os.path.realpath(_CLOUD_USAGE_PATH)
            with open(path) as f:
                data = json.load(f)
            if data.get("date") == today:
                return data
        except (FileNotFoundError, Exception):
            pass
        return {}

    def _load_cloud_usd_today(self) -> float:
        """Back-compat: today's total cloud USD (interactive + autonomous)."""
        return float(self._load_cloud_usage().get("usd", 0.0))

    def _persist_cloud_usd(self) -> None:
        """Persist today's cloud USD spend to disk (best-effort). Carries the autonomous
        slice and the soft-pause 'cleared' date so both survive a restart within the day."""
        import json

        try:
            path = os.path.realpath(_CLOUD_USAGE_PATH)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(
                    {
                        "date": self._cloud_usd_date,
                        "usd": self._cloud_usd_today,
                        "usd_autonomous": getattr(self, "_cloud_usd_autonomous_today", 0.0),
                        "soft_cleared": getattr(self, "_autonomous_soft_cleared_date", ""),
                    },
                    f,
                )
        except Exception as e:
            logger.debug("[ModelRouter] cloud_usage persist failed: %s", e)

    @staticmethod
    def _price_usd(model_id: str, in_tok: int, out_tok: int, cache_read: int = 0) -> float:
        """USD for a cloud call's token counts (unknown models → Sonnet-class rate)."""
        ri, ro, rc = _CLOUD_RATES.get(model_id, (3.0, 15.0, 0.30))
        return (in_tok * ri + out_tok * ro + cache_read * rc) / 1_000_000

    def _charge_cloud_usd(self, model_id: str, in_tok: int, out_tok: int, cache_read: int) -> float:
        """Compute and accumulate USD for a completed cloud call. Returns call cost.
        When in background mode, the spend also accrues to the autonomous-only pool."""
        usd = self._price_usd(model_id, in_tok, out_tok, cache_read)
        if usd > 0:
            self._cloud_usd_today = getattr(self, "_cloud_usd_today", 0.0) + usd
            # Monotonic per-process total (no day rollover) — per-job cloud_usd is
            # metered as a start/end delta of this counter.
            self._cloud_usd_process_total = getattr(self, "_cloud_usd_process_total", 0.0) + usd
            if self._bg_mode:
                self._cloud_usd_autonomous_today = (
                    getattr(self, "_cloud_usd_autonomous_today", 0.0) + usd
                )
            self._persist_cloud_usd()
        return usd

    def _refresh_cloud_usd_today(self) -> None:
        """Roll today's accumulated cloud spend over at the UTC date boundary."""
        import datetime as _dt

        today_str = _dt.date.today().isoformat()
        if getattr(self, "_cloud_usd_date", "") != today_str:
            self._cloud_usd_date = today_str
            data = self._load_cloud_usage()
            self._cloud_usd_today = float(data.get("usd", 0.0))
            self._cloud_usd_autonomous_today = float(data.get("usd_autonomous", 0.0))
            self._autonomous_soft_cleared_date = data.get("soft_cleared", "") or ""

    def _effective_daily_usd_cap(self) -> float:
        """Today's USD ceiling: the org `cloud_daily_usd_budget` setting folded with any
        tighter per-agent cap (the lower wins). On a lite brain, fall back to
        BRAIN_LITE_DAILY_USD_CAP when neither is set, so a lite tenant is never
        unbounded. 0 = no cap."""
        from brain.settings import settings as _settings

        cap = float(_settings.get("cloud_daily_usd_budget") or 0.0)
        # A bound agent may carry a tighter spend cap WITHIN the org ceiling
        # (e.g. org $100k, this agent $20k) — the lower of the two wins.
        try:
            from brain.agent_ctx import current_agent

            _a = current_agent()
            _ac = (_a or {}).get("permissions", {}).get("cloud_daily_usd_budget") if _a else None
            if _ac not in (None, ""):
                _acf = float(_ac)
                cap = _acf if cap <= 0 else min(cap, _acf)
        except Exception:
            pass
        # Lite has no local pod to shed onto, so default it to a finite ceiling.
        if cap <= 0 and getattr(self, "_local_disabled", False):
            cap = _LITE_DEFAULT_DAILY_USD_CAP
        return cap

    def _enforce_cloud_budget(self, cluster: str, cell: str) -> bool:
        """Gate a cloud call on the daily USD ceiling. Returns True when the caller
        should REDIRECT the call to local (a full brain over cap). Raises
        CloudBudgetExceeded on a lite brain over cap (no local fallback). Returns
        False to proceed on cloud. Checked BEFORE dispatch, so a block costs nothing."""
        self._refresh_cloud_usd_today()
        cap = self._effective_daily_usd_cap()
        if cap <= 0 or getattr(self, "_cloud_usd_today", 0.0) < cap:
            return False
        if getattr(self, "_local_disabled", False):
            raise CloudBudgetExceeded(
                f"daily cloud budget reached (${self._cloud_usd_today:.4f} ≥ ${cap:.2f}); "
                f"lite brain has no local fallback — blocking {cluster}/{cell}"
            )
        logger.warning(
            "[Resource] Daily cloud USD cap reached ($%.4f / $%.2f) "
            "— routing %s/%s to local for the rest of today.",
            self._cloud_usd_today,
            cap,
            cluster,
            cell,
        )
        return True

    def _meter_agent(
        self,
        model_id: str,
        in_tok: int,
        out_tok: int,
        *,
        is_cloud: bool,
        latency: float = 0.0,
        cache_read: int = 0,
    ) -> None:
        """Attribute one completed model call to the agent that drove this turn, so
        the Agents dashboard can show which agent is calling the model and how much
        it burns. Reads the agent_id from the turn-routing lane (turn_ctx); owner/
        idle turns fall under "owner" and are dropped by the dashboard.

        Local (GPU-pod) calls accrue ``pod_s`` (their wall-clock latency) so the
        dashboard can split the shared pod's $/hr across agents by real compute time;
        cloud calls accrue metered ``cloud_usd``. Best-effort — never raises."""
        try:
            from brain import turn_ctx

            aid = (turn_ctx.current_turn() or {}).get("agent_id") or "owner"
        except Exception:
            aid = "owner"
        # Lazily ensure the per-agent ledger exists. Tests (and any caller that
        # builds ModelRouter via __new__) bypass __init__, so this dashboard-only
        # metering must not assume the attribute was set — honor the "never raises"
        # contract above rather than break the model call it's meant to observe.
        usage = getattr(self, "_agent_usage", None)
        if usage is None:
            usage = self._agent_usage = {}
        u = usage.get(aid)
        if u is None:
            u = usage[aid] = {
                "calls": 0, "cloud_calls": 0, "in_tok": 0, "out_tok": 0,
                "cloud_usd": 0.0, "pod_s": 0.0, "last_ts": 0.0,
            }
        u["calls"] += 1
        u["in_tok"] += int(in_tok or 0)
        u["out_tok"] += int(out_tok or 0)
        if is_cloud:
            u["cloud_calls"] += 1
            u["cloud_usd"] += self._price_usd(model_id, in_tok, out_tok, cache_read)
        else:
            u["pod_s"] += max(0.0, float(latency or 0.0))
        u["last_ts"] = time.time()

    def flush_usage(self) -> int:
        """Persist each agent's usage accumulated since the last flush to the durable
        ledger (migration 016) so the dashboard can sum it across restarts. Writes one
        additive delta row per agent that had activity. Returns rows written. Blocking
        Supabase I/O — call from a thread. Best-effort / no-op when Supabase is off."""
        rows = []
        for aid, cur in self._agent_usage.items():
            if not aid or aid == "owner":
                continue
            prev = self._usage_flushed.get(aid, {})
            delta = {k: cur.get(k, 0) - prev.get(k, 0) for k in
                     ("calls", "cloud_calls", "in_tok", "out_tok", "cloud_usd", "pod_s")}
            if all(v <= 0 for v in delta.values()):
                continue
            rows.append({"agent_id": aid, "persona": aid.split(".", 1)[0], **delta})
        if not rows:
            return 0
        try:
            from brain import agent_usage_store

            ok = agent_usage_store.record_deltas(rows)
        except Exception as e:
            logger.debug("[ModelRouter] usage flush failed: %s", e)
            ok = False
        if ok:
            # Advance the high-water mark to the current cumulative for every agent.
            for aid, cur in self._agent_usage.items():
                self._usage_flushed[aid] = dict(cur)
        return len(rows) if ok else 0

    def agent_usage(self) -> dict:
        """Per-agent token + cloud-$ tallies for this process session. Keyed by
        agent_id; excludes the "owner" bucket (interactive UI + idle inner life).

        Scope caveat: in-memory and per-process — covers agents whose turns ran in
        THIS brain process (one (org, persona) process binds many agents). It resets
        on restart and does not aggregate a separate agent-worker process. Durable
        cross-process metering would persist these like agent_turns (migration 015)."""
        return {k: dict(v) for k, v in self._agent_usage.items() if k and k != "owner"}

    # ── External cloud usage (calls that bypass call(), e.g. the CMA executor) ──

    def cloud_budget_exhausted(self) -> bool:
        """True when today's cloud spend is at/over the effective daily USD cap.

        A pre-dispatch gate for cloud consumers that DON'T go through call() — the
        CMA managed-agents executor runs inference on the Anthropic API directly
        (billing the tenant's key), so the in-call _enforce_cloud_budget() never sees
        it. This lets that path honor the same cap. Best-effort; never raises. A cap
        of 0 (no ceiling) always returns False."""
        try:
            self._refresh_cloud_usd_today()
            cap = self._effective_daily_usd_cap()
            return cap > 0 and getattr(self, "_cloud_usd_today", 0.0) >= cap
        except Exception:
            return False

    def record_cloud_usage(
        self, model_id: str, in_tok: int, out_tok: int, cache_read: int = 0
    ) -> float:
        """Account for a completed cloud call that did NOT route through call().

        Charges the daily USD tally (→ the lite cap binds next time) AND attributes
        the tokens to the current agent lane (→ the Agents dashboard sees them). The
        seam for CMA managed-agent inference, which bills the API key directly and is
        otherwise invisible to every meter. Returns the call's USD. Never raises."""
        try:
            in_t, out_t, cr = int(in_tok or 0), int(out_tok or 0), int(cache_read or 0)
            usd = self._charge_cloud_usd(model_id, in_t, out_t, cr)
            self._meter_agent(model_id, in_t, out_t, is_cloud=True, cache_read=cr)
            return usd
        except Exception as e:
            logger.debug("[ModelRouter] record_cloud_usage failed: %s", e)
            return 0.0

    def note_unmetered_spend_suspected(self) -> None:
        """Count a failed attempt to meter out-of-band cloud spend (e.g. a CMA
        session whose usage read errored). Each tick means real dollars may have
        billed the key without landing in any tally — the exact failure class behind
        the invisible-$200 incident — so consumers surface it instead of trusting a
        clean-looking meter."""
        self._unmetered_spend_suspected = self.unmetered_spend_suspected + 1

    @property
    def unmetered_spend_suspected(self) -> int:
        """How many times this process failed to meter out-of-band cloud spend."""
        return int(getattr(self, "_unmetered_spend_suspected", 0))

    # ── Autonomy: separate spend pool + defer signalling (see brain.autonomy) ───

    def set_spend_gate(self, gate) -> None:
        """Inject the SpendRiskGate so the router can report cloud-health (bg call
        timeouts/successes) that feed the gate's CLOUD_UNREACHABLE cooldown."""
        self._spend_gate = gate

    def autonomous_usd_today(self) -> float:
        """Today's (UTC) autonomous-only cloud spend — the pool the soft/hard caps bind."""
        self._refresh_cloud_usd_today()
        return getattr(self, "_cloud_usd_autonomous_today", 0.0)

    @property
    def cloud_usd_process_total(self) -> float:
        """Monotonic cloud spend since process start (no day rollover). Job records
        meter their own cost as a start/end delta of this counter."""
        return getattr(self, "_cloud_usd_process_total", 0.0)

    def autonomous_soft_cleared(self) -> bool:
        """True if the owner cleared the soft pause for the current UTC day."""
        self._refresh_cloud_usd_today()
        import datetime as _dt

        return getattr(self, "_autonomous_soft_cleared_date", "") == _dt.date.today().isoformat()

    def clear_autonomous_soft_pause(self) -> None:
        """Lift the autonomous soft pause for the rest of today (owner approved 'continue')."""
        import datetime as _dt

        self._refresh_cloud_usd_today()
        self._autonomous_soft_cleared_date = _dt.date.today().isoformat()
        self._persist_cloud_usd()

    def bg_bucket_empty(self) -> bool:
        """True when the background cloud token bucket is exhausted (rate-limited)."""
        self._refill_bg_bucket()
        return self._bg_cloud_bucket <= 0

    def take_bg_defer(self):
        """Consume (and clear) the one-shot bg-defer signal. Returns a DeferReason or None.
        The motor calls this right after an autonomous cloud call that came back empty, to
        tell a genuine planner failure apart from 'cloud was unavailable → defer the job'."""
        r = getattr(self, "_bg_defer_reason", None)
        self._bg_defer_reason = None
        return r

    def _bg_precheck(self, cluster: str, cell: str):
        """Up-front autonomous-call gate: returns a DeferReason if a bg cloud call must
        NOT proceed (rate bucket empty, or the global daily USD ceiling is exhausted),
        else None. The autonomous soft/hard caps themselves are enforced by the gate
        before the job plans; this is the per-call backstop that replaces degrade-to-local."""
        from brain.autonomy.reasons import DeferReason

        self._refill_bg_bucket()
        if self._bg_cloud_bucket <= 0:
            return DeferReason.RATE_BUCKET_EMPTY
        if self.cloud_budget_exhausted():
            return DeferReason.BUDGET_SOFT_PAUSE
        return None

    def _note_cloud_timeout(self) -> None:
        g = getattr(self, "_spend_gate", None)
        if g is not None:
            try:
                g.note_cloud_timeout()
            except Exception:
                pass

    def _notify_cloud_ok(self) -> None:
        g = getattr(self, "_spend_gate", None)
        if g is not None:
            try:
                g.note_cloud_success()
            except Exception:
                pass

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
            if host == "off":  # pod declared off by the gateway — nothing to warm
                return False
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

    def _resolve_model_id(self, model_key: str, cluster: str) -> tuple[str, str]:
        """Resolve a cell's model_key to (model_key, model_id) — the single place all
        dispatch paths route through. Applies the cloud-provider remap AND the
        per-brain lite gate: a 'lite' brain has no local pod, so any local route falls
        back to cloud here. The cloud-vs-local TRUTH still lives in cell config +
        _provider_for; this only enforces the per-brain local-permission, uniformly."""
        model_id = _remap_cloud_provider(MODEL_MAP.get(model_key, model_key), cluster)
        if self._local_disabled and _provider_for(model_id) == "local":
            model_key = _LITE_CLOUD_KEY
            model_id = _remap_cloud_provider(MODEL_MAP.get(model_key, model_key), cluster)
        return model_key, model_id

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
        self._bg_defer_reason = None  # cleared each call; set only if this call defers

        model_key, model_id = self._resolve_model_id(model_key, cluster)
        # A lite brain can't honor a local-only cell (no pod) — relax locality so the
        # enforcement below won't force it back to a pod that doesn't exist.
        if self._local_disabled:
            locality = "either"

        # Locality enforcement: local cells must never dispatch to cloud APIs
        _is_cloud = _provider_for(model_id) != "local"
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

        # Background mode (autonomous): CLOUD-ONLY. Instead of degrading to a local
        # backend that doesn't exist on hosted, an autonomous cloud call that can't
        # proceed (rate bucket empty / daily USD ceiling) sets the one-shot defer signal
        # and returns empty — the motor turns that into a cleanly DEFERRED (requeued) job.
        if self._bg_mode and _is_cloud:
            _defer = self._bg_precheck(cluster, cell)
            if _defer is not None:
                self._bg_defer_reason = _defer
                return ""
            # Cap output tokens for background calls.
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

        # Daily cloud USD budget — hard ceiling across all cloud providers. INTERACTIVE
        # only: a full brain degrades to local at the cap; a lite brain (no pod) raises
        # CloudBudgetExceeded. Autonomous (bg) budget is handled by _bg_precheck above.
        if not self._bg_mode and _is_cloud and self._enforce_cloud_budget(cluster, cell):
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

        # Inject skill text into the system prompt. Two classes, two rules:
        #  • Humanity/native frameworks → LOCAL/RunPod ONLY. Cloud models (Claude,
        #    Gemini) already have these reasoning/skill frameworks natively, so local
        #    copies are redundant prompt bloat. Gating on the resolved route (_is_cloud)
        #    adapts automatically as cells move between local and cloud models.
        #  • App-provided (partner) skills → ALWAYS injected, cloud or local. A partner
        #    skill is domain knowledge the model does NOT have (the embedding app's own
        #    data/tools/house style), and it's untrusted, so it's injected fenced behind
        #    a precedence framing (load_partner_block) on every route.
        if skills:
            from brain.skill_loader import SkillLoader

            partner_names = [s for s in skills if SkillLoader.is_partner(s)]
            other_names = [s for s in skills if not SkillLoader.is_partner(s)]
            blocks: list[str] = []
            if partner_names:
                blocks.append(SkillLoader.load_partner_block(partner_names))
            if other_names and not _is_cloud:
                blocks.append(SkillLoader.load_many(other_names))
            skill_block = "\n\n".join(b for b in blocks if b)
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
                if self._bg_mode:
                    from brain.autonomy.reasons import DeferReason

                    logger.warning(
                        "[Resource] Background cloud call %s/%s timed out after %.0fs — deferring "
                        "(cloud-only; no local fallback for autonomous work).",
                        cluster, cell, bg_timeout,
                    )
                    self._note_cloud_timeout()
                    self._bg_defer_reason = DeferReason.CLOUD_UNREACHABLE
                    return ""
                logger.warning(
                    "[Resource] Cloud call %s/%s timed out after %.0fs — falling back to local.",
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
        elif model_id.startswith("vertex-"):
            try:
                async with self._get_cloud_semaphore():
                    # Pass system + context separately so Claude-on-Vertex caches the
                    # stable block (Gemini-on-Vertex folds them back together itself).
                    coro = self._call_vertex(
                        model_id, system_prompt, messages, max_tokens, cached_context=cached_context
                    )
                    if bg_timeout:
                        text, in_tok, out_tok = await asyncio.wait_for(coro, timeout=bg_timeout)
                    else:
                        text, in_tok, out_tok = await coro
            except TimeoutError:
                if self._bg_mode:
                    from brain.autonomy.reasons import DeferReason

                    logger.warning(
                        "[Resource] Background cloud call %s/%s timed out after %.0fs — deferring "
                        "(cloud-only; no local fallback for autonomous work).",
                        cluster, cell, bg_timeout,
                    )
                    self._note_cloud_timeout()
                    self._bg_defer_reason = DeferReason.CLOUD_UNREACHABLE
                    return ""
                logger.warning(
                    "[Resource] Cloud call %s/%s timed out after %.0fs — falling back to local.",
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
        elif model_id.startswith("gemini"):
            try:
                coro = self._call_google(model_id, system_with_context, messages, max_tokens)
                if bg_timeout:
                    text, in_tok, out_tok = await asyncio.wait_for(coro, timeout=bg_timeout)
                else:
                    text, in_tok, out_tok = await coro
            except TimeoutError:
                if self._bg_mode:
                    from brain.autonomy.reasons import DeferReason

                    logger.warning(
                        "[Resource] Background cloud call %s/%s timed out after %.0fs — deferring "
                        "(cloud-only; no local fallback for autonomous work).",
                        cluster, cell, bg_timeout,
                    )
                    self._note_cloud_timeout()
                    self._bg_defer_reason = DeferReason.CLOUD_UNREACHABLE
                    return ""
                logger.warning(
                    "[Resource] Cloud call %s/%s timed out after %.0fs — falling back to local.",
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
        elif model_id in _LOCAL_VARIANTS:
            text, in_tok, out_tok = await self._call_local(
                system_with_context,
                messages,
                max_tokens,
                local_variant=model_id,
                temperature=temperature,
            )
        else:
            # OpenAI-compatible cloud (GPT, or any base_url provider).
            try:
                async with self._get_cloud_semaphore():
                    coro = self._call_openai(
                        model_id, system_with_context, messages, max_tokens, temperature
                    )
                    if bg_timeout:
                        text, in_tok, out_tok = await asyncio.wait_for(coro, timeout=bg_timeout)
                    else:
                        text, in_tok, out_tok = await coro
            except TimeoutError:
                if self._bg_mode:
                    from brain.autonomy.reasons import DeferReason

                    logger.warning(
                        "[Resource] Background cloud call %s/%s timed out after %.0fs — deferring "
                        "(cloud-only; no local fallback for autonomous work).",
                        cluster, cell, bg_timeout,
                    )
                    self._note_cloud_timeout()
                    self._bg_defer_reason = DeferReason.CLOUD_UNREACHABLE
                    return ""
                logger.warning(
                    "[Resource] Cloud call %s/%s timed out after %.0fs — falling back to local.",
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
            self._charge_cloud_usd(model_id, in_tok, out_tok, 0)

        # ── Depseudonymize cloud response (restores ⟨type_n⟩ → real values) ──────
        if _egress_active:
            from brain.security import EGRESS_MODE as _egress_mode2

            if _egress_mode2 not in ("redact", "block"):
                text = self._egress.depseudonymize(text)

        latency = time.time() - start
        # A completed autonomous cloud call resets the gate's cloud-health streak.
        if _is_cloud and self._bg_mode:
            self._notify_cloud_ok()
        self._meter_agent(
            model_id, in_tok, out_tok, is_cloud=_is_cloud, latency=latency, cache_read=cache_read
        )
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

    async def call_structured_any(
        self,
        model_key: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict],
        *,
        cluster: str = "",
        cell: str = "",
        turn_id: str = "",
        max_tokens: int = 1024,
    ) -> dict:
        """Agentic tool-choice (NOT forced): the model picks one of `tools` or
        none. Provider-agnostic — dispatches by _provider_for like call(). Each
        tool is {name, description, input_schema}. Returns
        {"tool": <name>, "args": {...}} on a tool call, or {"text": <str>} when
        the model answered without calling a tool (the loop's stop signal).
        Powers GenericExecutor; budget/bg-mode caps mirror call_structured."""
        import json as _json

        model_key, model_id = self._resolve_model_id(model_key, cluster)
        provider = _provider_for(model_id)
        self._bg_defer_reason = None
        # Daily USD ceiling. INTERACTIVE: a lite brain over cap raises CloudBudgetExceeded
        # (→ HTTP 402); a full brain ends the agentic loop cleanly with a no-tool answer.
        # AUTONOMOUS (bg): rate bucket / budget set the defer signal + stop the loop.
        if provider != "local" and self._bg_mode:
            _defer = self._bg_precheck(cluster, cell)
            if _defer is not None:
                self._bg_defer_reason = _defer
                return {"text": ""}
        elif provider != "local" and self._enforce_cloud_budget(cluster, cell):
            return {"text": ""}
        try:
            if provider == "anthropic":
                client = self._get_anthropic()
                resp = await client.messages.create(
                    model=model_id,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[{"role": m["role"], "content": m["content"]} for m in messages],
                    tools=[
                        {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "input_schema": t["input_schema"],
                        }
                        for t in tools
                    ],
                )
                for block in resp.content:
                    if getattr(block, "type", None) == "tool_use":
                        return {"tool": block.name, "args": block.input or {}}
                txt = "".join(
                    getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
                )
                return {"text": txt}
            if provider in ("openai",):
                client = self._get_openai()
                oai_msgs = [{"role": "system", "content": system_prompt}]
                for m in messages:
                    c = m["content"]
                    if isinstance(c, list):
                        c = "\n".join(str(b.get("text", "")) for b in c if isinstance(b, dict))
                    oai_msgs.append({"role": m["role"], "content": c})
                resp = await client.chat.completions.create(
                    model=model_id,
                    max_completion_tokens=max_tokens,
                    messages=oai_msgs,
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": t["name"],
                                "description": t.get("description", ""),
                                "parameters": t["input_schema"],
                            },
                        }
                        for t in tools
                    ],
                    tool_choice="auto",
                )
                msg = resp.choices[0].message if resp.choices else None
                calls = getattr(msg, "tool_calls", None) if msg else None
                if calls:
                    return {
                        "tool": calls[0].function.name,
                        "args": _json.loads(calls[0].function.arguments or "{}"),
                    }
                return {"text": (msg.content or "") if msg else ""}
            # Local / RunPod (Ollama): no native tool calling — ask for a JSON
            # decision and parse it. Looser, but enough to drive the loop on a
            # local model. The tool menu is rendered into the prompt.
            menu = "\n".join(
                f"- {t['name']}({', '.join((t['input_schema'].get('properties') or {}).keys())}): "
                f"{t.get('description', '')}"
                for t in tools
            )
            sys2 = (
                f"{system_prompt}\n\nAvailable tools:\n{menu}\n\n"
                'Reply with ONE JSON object: {"tool":"<name>","args":{...}} to call a tool, '
                'or {"text":"<one-line summary>"} when the task is done — put the summary as '
                'the value of "text", never as a key. JSON only.'
            )
            text, _i, _o = await self._call_local(
                sys2, messages, max_tokens, local_variant=model_id
            )
            return _coerce_local_decision(text)
        except Exception as e:
            logger.warning("[ModelRouter] call_structured_any %s/%s failed: %s", cluster, cell, e)
            return {"text": f"[error] {e}"}

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
        model_key, model_id = self._resolve_model_id(model_key, cluster)
        _is_cloud = _provider_for(model_id) != "local"
        self._bg_defer_reason = None  # cleared each call; set only if this call defers

        # Daily USD ceiling. INTERACTIVE: a structured call has no local tool-use path
        # to degrade to, so a full brain over cap fails soft to {} and a lite brain
        # raises CloudBudgetExceeded (→ HTTP 402). AUTONOMOUS (bg): rate bucket / budget
        # set the defer signal and return {} so the strategic planner requeues the job
        # instead of planning on an empty result (the direct caller sees this, not a cell).
        if self._bg_mode and _is_cloud:
            _defer = self._bg_precheck(cluster, cell)
            if _defer is not None:
                self._bg_defer_reason = _defer
                return {}
        elif _is_cloud and self._enforce_cloud_budget(cluster, cell):
            return {}

        if _provider_for(model_id) == "openai":
            return await self._call_structured_openai(
                model_id,
                system_prompt,
                messages,
                tool_name,
                tool_description,
                tool_schema,
                cluster=cluster,
                cell=cell,
                max_tokens=max_tokens,
            )

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
            # Bound the semaphore ACQUIRE too (not just the call) so a saturated cloud
            # pool can't pin a caller indefinitely — the acquire sits outside the
            # per-call wait_for, so without this a wedged holder blocks forever.
            _acq_to = float(_s("semaphore_acquire_timeout_s") or 30.0)
            _sem = self._get_cloud_semaphore()
            await asyncio.wait_for(_sem.acquire(), timeout=_acq_to)
            try:
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
            finally:
                _sem.release()
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
            if self._bg_mode:
                self._notify_cloud_ok()
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
        except TimeoutError:
            # Autonomous: a wedged structured call defers (cloud-health streak) instead
            # of silently returning {} → single mega-story. Interactive: soft {} as before.
            if self._bg_mode:
                from brain.autonomy.reasons import DeferReason

                self._note_cloud_timeout()
                self._bg_defer_reason = DeferReason.CLOUD_UNREACHABLE
            logger.warning("[ModelRouter] call_structured %s/%s timed out", cluster, cell)
            return {}
        except Exception as e:
            logger.warning("[ModelRouter] call_structured %s/%s failed: %s", cluster, cell, e)
            return {}

    async def _call_structured_openai(
        self,
        model_id: str,
        system_prompt: str,
        messages: list[dict],
        tool_name: str,
        tool_description: str,
        tool_schema: dict,
        *,
        cluster: str = "",
        cell: str = "",
        max_tokens: int = 4096,
    ) -> dict:
        """Structured output via forced function-calling — the OpenAI-compatible
        analog of the Anthropic tool_use path. Returns the parsed arguments dict,
        {} on any failure (mirrors call_structured's contract)."""
        import json as _json

        from brain.settings import settings as _settings

        try:
            client = self._get_openai()
            oai_msgs = [{"role": "system", "content": system_prompt}]
            for m in messages:
                content = m["content"]
                if isinstance(content, list):
                    content = "\n".join(
                        str(b.get("text", "")) for b in content if isinstance(b, dict)
                    )
                oai_msgs.append({"role": m["role"], "content": content})
            _struct_to = float(_settings.get("structured_call_timeout_s") or 150.0)
            async with self._get_cloud_semaphore():
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model_id,
                        max_completion_tokens=max_tokens,
                        messages=oai_msgs,
                        tools=[
                            {
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "description": tool_description,
                                    "parameters": tool_schema,
                                },
                            }
                        ],
                        tool_choice={"type": "function", "function": {"name": tool_name}},
                    ),
                    timeout=_struct_to,
                )
            usage = getattr(response, "usage", None)
            in_tok = getattr(usage, "prompt_tokens", 0) or 0
            out_tok = getattr(usage, "completion_tokens", 0) or 0
            if self._bg_mode:
                spent = in_tok + out_tok
                self._bg_cloud_tokens_used += spent
                self._bg_cloud_bucket -= spent
            self._charge_cloud_usd(model_id, in_tok, out_tok, 0)
            calls = response.choices[0].message.tool_calls if response.choices else None
            if calls:
                return _json.loads(calls[0].function.arguments or "{}")
            logger.warning(
                "[ModelRouter] call_structured(openai) %s/%s: no tool call in response",
                cluster,
                cell,
            )
            return {}
        except Exception as e:
            logger.warning(
                "[ModelRouter] call_structured(openai) %s/%s failed: %s", cluster, cell, e
            )
            return {}

    def _get_openai(self):
        """OpenAI-compatible client. OPENAI_API_KEY + optional OPENAI_BASE_URL
        come from env (the SDK reads both natively), so pointing the whole
        cognition layer at Groq/Mistral/DeepSeek is two env vars."""
        if getattr(self, "_openai_client", None) is None:
            import httpx
            import openai

            from brain.settings import settings as _settings

            _read_to = float(_settings.get("anthropic_timeout_s") or 120.0)
            _connect_to = float(_settings.get("anthropic_connect_timeout_s") or 10.0)
            self._openai_client = openai.AsyncOpenAI(
                timeout=httpx.Timeout(_read_to, connect=_connect_to),
                max_retries=int(_settings.get("anthropic_max_retries") or 2),
            )
        return self._openai_client

    async def _call_openai(
        self,
        model_id: str,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int = 1024,
        temperature: float | None = None,
    ) -> tuple[str, int, int]:
        """Call an OpenAI-compatible chat API. Returns (text, in_tok, out_tok).

        The cached_context block is folded into system_prompt by the caller
        (system_with_context) — OpenAI prefix-caches stable prompts automatically,
        so the per-session block still gets discounted without explicit markers.
        chat.completions (not the Responses API) for base_url compatibility."""
        client = self._get_openai()
        oai_msgs = [{"role": "system", "content": system_prompt}]
        for m in messages:
            content = m["content"]
            if isinstance(content, list):  # flatten Anthropic-style blocks
                content = "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
            oai_msgs.append({"role": m["role"], "content": content})
        kwargs: dict = {
            "model": model_id,
            "messages": oai_msgs,
            "max_completion_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        response = await client.chat.completions.create(**kwargs)
        text = (response.choices[0].message.content or "") if response.choices else ""
        usage = getattr(response, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", 0) or 0
        out_tok = getattr(usage, "completion_tokens", 0) or 0
        return text, in_tok, out_tok

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

    def _vertex_cfg(self) -> tuple[str, str]:
        """Resolve (project, location) for Vertex. Setting → env → default."""
        from brain.settings import settings as _s

        project = str(_s.get("vertex_project") or "") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        location = (
            str(_s.get("vertex_location") or "")
            or os.environ.get("GOOGLE_CLOUD_LOCATION", "")
            or "us-central1"
        )
        if not project:
            raise RuntimeError("vertex_project not set — configure Vertex in Settings → Providers")
        return project, location

    def _get_vertex_gemini(self):
        if self._vertex_gemini_client is None:
            project, location = self._vertex_cfg()
            from google import genai

            self._vertex_gemini_client = genai.Client(
                vertexai=True, project=project, location=location
            )
        return self._vertex_gemini_client

    def _get_vertex_anthropic(self):
        if self._vertex_anthropic_client is None:
            from brain.settings import settings as _s

            project, location = self._vertex_cfg()
            # Claude on Vertex lives in specific regions (e.g. us-east5); allow an
            # override separate from the Gemini location.
            region = str(_s.get("vertex_claude_location") or "") or location
            from anthropic import AsyncAnthropicVertex

            self._vertex_anthropic_client = AsyncAnthropicVertex(project_id=project, region=region)
        return self._vertex_anthropic_client

    async def _call_vertex(
        self,
        model_id: str,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int = 1024,
        cached_context: str = "",
    ) -> tuple[str, int, int]:
        """Dispatch a 'vertex-' model to Gemini-on-Vertex or Claude-on-Vertex.

        Gemini has no prompt caching, so the per-session context is folded into the
        system instruction (same as _call_google). Claude-on-Vertex DOES cache, so
        the context is passed through as a dedicated cached block (see
        _call_vertex_claude), matching the Anthropic API path."""
        real = model_id[len("vertex-") :]
        if real.startswith("gemini"):
            sys_full = f"{system_prompt}\n\n{cached_context}" if cached_context else system_prompt
            return await self._gemini_generate(
                self._get_vertex_gemini(), real, sys_full, messages, max_tokens
            )
        return await self._call_vertex_claude(
            real, system_prompt, messages, max_tokens, cached_context=cached_context
        )

    async def _call_vertex_claude(
        self,
        model_id: str,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int = 1024,
        cached_context: str = "",
    ) -> tuple[str, int, int]:
        """Claude on Vertex with prompt caching — mirrors _call_anthropic so the
        stable system + per-session context are a cache READ on every turn after the
        first. Cache-control on the last message lets intra-turn calls hit the cache
        too. Without this, Vertex would re-bill the full context every turn."""
        client = self._get_vertex_anthropic()

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
        if cache_read:
            logger.debug("[Vertex] Claude cache read: %d tokens", cache_read)
        return response.content[0].text, in_tok, out_tok

    async def _call_google(
        self, model_id: str, system_prompt: str, messages: list[dict], max_tokens: int = 1024
    ) -> tuple[str, int, int]:
        # Gemini Developer API client; the generate body is shared with Vertex.
        return await self._gemini_generate(
            self._get_google(), model_id, system_prompt, messages, max_tokens
        )

    async def _gemini_generate(
        self, client, model_id: str, system_prompt: str, messages: list[dict], max_tokens: int = 1024
    ) -> tuple[str, int, int]:
        from google.genai import types

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
            if host == "off":
                # The gateway declared the shared pod OFF (terminated, no replacement
                # yet). Same contract as an unreachable pod — runpod cells have no
                # cloud fallback — but failing here skips the full HTTP timeout the
                # dead pod's proxy host would otherwise cost every call.
                logger.debug("[RunPod] pod is off — skipping local call")
                return "", 0, 0
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
                        _raw_joined = "".join(text_parts)
                        _stripped = _strip_chatml(_raw_joined)
                        # DIAG: the model answered (200, done) but the content stripped
                        # to nothing — pure ChatML/markup. Suspected source of empty
                        # monologue ticks. Log the raw form so we can see what it emits.
                        if _raw_joined and not _stripped.strip():
                            logger.warning(
                                "[RunPod] response stripped to empty "
                                "(out_tok=%d, raw_len=%d) raw=%r",
                                out_tok,
                                len(_raw_joined),
                                _raw_joined[:300],
                            )
                        return _stripped, in_tok, out_tok
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

        cached = self._embed_cache.get(text)
        if cached is not None:
            self._embed_cache.move_to_end(text)
            return list(cached)

        vec: list[float] | None = None
        if self._embed_backend == "ollama":
            vec = await self._embed_ollama(text)
            if vec is None:
                # Permanent flip to google for remainder of session.
                logger.info(
                    "Ollama embedding service unreachable — switching to Google embeddings for this session. "
                    "Memory search will still work. To restore local embeddings: run 'ollama serve' and "
                    "'ollama pull nomic-embed-text'."
                )
                self._embed_backend = "google"
        if vec is None:
            vec = await self._embed_google(text)
        if vec is not None:
            self._embed_cache[text] = list(vec)
            while len(self._embed_cache) > 256:
                self._embed_cache.popitem(last=False)
        return vec

    @staticmethod
    def _embed_hosts() -> list[str]:
        """Ordered Ollama hosts to try for embeddings: the dedicated CPU embed
        host first (when configured), then the general Ollama host (pod/local).
        Deduped so the unconfigured case is exactly the old single-host path."""
        hosts = []
        if OLLAMA_EMBED_HOST:
            hosts.append(OLLAMA_EMBED_HOST)
        if OLLAMA_HOST not in hosts:
            hosts.append(OLLAMA_HOST)
        return hosts

    async def _embed_ollama(self, text: str) -> list[float] | None:
        for host in self._embed_hosts():
            try:
                r = await self._get_http().post(
                    f"{host}/api/embeddings",
                    json={"model": OLLAMA_EMBED_MODEL, "prompt": text},
                    timeout=10,
                )
                r.raise_for_status()
                vec = r.json().get("embedding")
                if vec and len(vec) == EMBEDDING_DIM:
                    return vec
                if vec:
                    logger.warning(
                        "Ollama (%s) returned %d-dimensional embeddings but %d were expected — "
                        "wrong model pulled? Check OLLAMA_EMBED_MODEL in .env (should be 'nomic-embed-text').",
                        host,
                        len(vec),
                        EMBEDDING_DIM,
                    )
            except Exception as e:
                logger.debug("Ollama embed failed on %s: %s", host, e)
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
