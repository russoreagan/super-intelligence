"""
CMAExecutor — runs cloud-connected actions on Anthropic Managed Agents (CMA)
instead of a local Claude CLI subprocess. This is the hosted-native equivalent
of CloudExecutor: the agent loop and its sandboxed toolset (bash/read/write/
edit/glob/grep/web_search/web_fetch) run server-side, with remote MCP connectors
authenticated via OAuth vaults.

It presents the EXACT same public surface as CloudExecutor (execute_read /
execute_pending / pending-state / confirmation detection / available /
connectors_summary), so motor_cortex, the session_turn confirmation gate, and
the test contract are untouched. Selection between the two is done in
session_setup via BRAIN_EXECUTOR / the `brain_executor` setting.

The three CloudExecutor guardrails are preserved:
  1. Minimal context  — only task + operational facts reach the agent
  2. Result fencing   — output is screened + fenced before entering the brain
  3. Confirmation gate — write actions are gated by the brain's own
                         set_pending → user-confirm → execute_pending handshake
                         BEFORE write tools are ever enabled (so CMA's own
                         always_ask permission policy is not needed).

Read vs write is enforced with two agents (a read agent with write/edit/bash
disabled, a write agent with the full toolset) because the SDK has no
per-session tool override.

Cost note: model inference inside a session bills against the tenant's own
ANTHROPIC_API_KEY at standard rates. There is no managed-agents task budget in
this SDK version, so per-task cost control is the wall-clock timeout
(cma_task_timeout_s) plus account-level spend limits.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets as _secrets_mod
import threading
import time
from collections.abc import Callable
from pathlib import Path

from brain.bus import Bus
from brain.clusters._executor_common import ExecutorCommon
from brain.settings import settings

logger = logging.getLogger(__name__)

CLUSTER = "cma_executor"

_SECOND_BRAIN_ROOT = Path(
    os.environ.get("SECOND_BRAIN_PATH", str(Path(__file__).parent.parent.parent / "second_brain"))
)
_STATE_PATH = _SECOND_BRAIN_ROOT / "cma_state.json"
_MCP_CONFIG_PATH = _SECOND_BRAIN_ROOT / "cma_mcp.json"

# ── Module-level connector registry ───────────────────────────────────────────
# The registry is ORG-LEVEL, not persona-level, so it must NOT live in the
# persona-namespaced second_brain volume. Two backends:
#   • Supabase (hosted, BRAIN_STORAGE_BACKEND=supabase): table public.mcp_connectors
#     with the secret in Supabase Vault, scoped by org via RLS. Survives persona
#     switches and is shared across all of an org's agents.
#   • Local file fallback (cma_mcp.json) when Supabase is off.
# When BRAIN_CMA_MCP_SERVERS is set, the registry is read-only (env-managed) and
# registration/removal are rejected.

_CONNECTOR_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_connector_file_lock = threading.Lock()  # guards read-modify-write of cma_mcp.json (local fallback)


def _supabase_enabled() -> bool:
    try:
        from brain.second_brain import supabase_client

        return supabase_client.is_enabled()
    except Exception:
        return False


# Per-end-user identity token. Mirrors the app-side verifier in lib/mcp/identity.ts
# EXACTLY (do not change the encoding without updating both):
#   token = "mcpu_" + base64url(JSON({eu, exp})) + "." + base64url(HMAC_SHA256(body, secret))
#   eu  = the engine session's end_user_id
#   exp = epoch MILLISECONDS expiry (Date.now() + ttl*1000 on the JS side)
# The connector verifies the HMAC with its shared secret, so only the brain (which
# holds the secret) can mint a given end-user's identity — the agent cannot forge it.
_MCPU_PREFIX = "mcpu_"
# Refresh the minted token this many seconds BEFORE expiry, so an active end-user's
# in-place credential is rotated well within the 1-hour TTL.
_MCPU_REFRESH_BUFFER_S = 600


def _b64url_nopad(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mint_end_user_token(
    end_user_id: str, secret: str, *, now_ms: int, ttl_s: int = 3600
) -> tuple[str, int]:
    """Return (token, exp_ms) for an end-user, HMAC-signed with the connector secret."""
    import hashlib
    import hmac

    exp_ms = now_ms + ttl_s * 1000
    body = _b64url_nopad(
        json.dumps({"eu": end_user_id, "exp": exp_ms}, separators=(",", ":")).encode("utf-8")
    )
    sig = _b64url_nopad(
        hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{_MCPU_PREFIX}{body}.{sig}", exp_ms


def is_env_managed() -> bool:
    """True when THIS brain's connector registry is pinned via BRAIN_CMA_MCP_SERVERS
    (read-only).

    Env-pinned connectors are process-global, so in multi-tenant hosting they would
    otherwise be inherited by EVERY tenant brain — a cross-org leak (one org's
    `trading` connector showing up in another org's tool menu). They are therefore
    gated to the owning org: when BRAIN_CMA_MCP_OWNER_ORG is set and does not match
    this brain's org (BRAIN_ORG_ID / BRAIN_USER_ID), the env list does NOT apply and
    the brain falls through to its own org-scoped Supabase registry. With no owner
    pin (single-tenant / dev) the env applies as before."""
    if not os.environ.get("BRAIN_CMA_MCP_SERVERS", "").strip():
        return False
    owner = os.environ.get("BRAIN_CMA_MCP_OWNER_ORG", "").strip()
    this_org = (
        os.environ.get("BRAIN_ORG_ID", "").strip()
        or os.environ.get("BRAIN_USER_ID", "").strip()
    )
    return not (owner and this_org and owner != this_org)


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("url is required")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise ValueError("url must start with http:// or https://")
    return url


def _read_mcp_config() -> dict:
    try:
        return (
            json.loads(_MCP_CONFIG_PATH.read_text(encoding="utf-8"))
            if _MCP_CONFIG_PATH.exists()
            else {"servers": []}
        )
    except Exception:
        return {"servers": []}


def _write_mcp_config(cfg: dict) -> None:
    _MCP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MCP_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def register_connector(name: str, url: str, display_name: str = "") -> str:
    """Generate a shared secret, register the connector (Supabase or file), return it once."""
    if is_env_managed():
        raise ValueError(
            "connectors are pinned via BRAIN_CMA_MCP_SERVERS and cannot be edited here"
        )
    name = name.strip().lower()
    if not _CONNECTOR_NAME_RE.match(name):
        raise ValueError(
            "name must be lowercase letters/digits/underscore/hyphen, starting with a letter or digit"
        )
    url = _normalize_url(url)
    display_name = (display_name or "").strip()
    secret = _secrets_mod.token_hex(32)

    if _supabase_enabled():
        from brain.second_brain import supabase_client

        try:
            supabase_client.get_client().rpc(
                "register_mcp_connector",
                {
                    "p_name": name,
                    "p_url": url,
                    "p_secret": secret,
                    "p_display_name": display_name or None,
                },
            ).execute()
        except Exception as e:
            # The RPC raises on duplicate name; surface a clean message.
            msg = str(e)
            if "already exists" in msg:
                raise ValueError(f"connector '{name}' already exists") from e
            raise
        return secret

    with _connector_file_lock:
        cfg = _read_mcp_config()
        servers = cfg.setdefault("servers", [])
        if any(s.get("name") == name for s in servers):
            raise ValueError(f"connector '{name}' already exists")
        entry: dict = {"name": name, "url": url, "access_token": secret}
        if display_name:
            entry["display_name"] = display_name
        servers.append(entry)
        _write_mcp_config(cfg)
    return secret


def remove_connector(name: str) -> bool:
    """Remove a connector by name. Returns True if it existed."""
    if is_env_managed():
        raise ValueError(
            "connectors are pinned via BRAIN_CMA_MCP_SERVERS and cannot be edited here"
        )
    name = name.strip().lower()
    if _supabase_enabled():
        from brain.second_brain import supabase_client

        try:
            resp = (
                supabase_client.get_client().rpc("delete_mcp_connector", {"p_name": name}).execute()
            )
            return bool(resp.data)
        except Exception as e:
            logger.warning("[CMAExecutor] delete_mcp_connector failed for %s: %s", name, e)
            return False

    with _connector_file_lock:
        cfg = _read_mcp_config()
        before = len(cfg.get("servers", []))
        cfg["servers"] = [s for s in cfg.get("servers", []) if s.get("name") != name]
        if len(cfg["servers"]) == before:
            return False
        _write_mcp_config(cfg)
    return True


def _load_connectors_from_supabase() -> list[dict]:
    """Return [{name, url, access_token, display_name}] from Supabase, or []."""
    from brain.second_brain import supabase_client

    try:
        resp = supabase_client.get_client().rpc("get_mcp_connectors", {}).execute()
        rows = resp.data if isinstance(resp.data, list) else []
    except Exception as e:
        logger.warning("[CMAExecutor] could not load connectors from Supabase: %s", e)
        return []
    out: list[dict] = []
    for r in rows:
        name = (r or {}).get("name")
        url = (r or {}).get("url")
        if not name or not url:
            continue
        # Registry connectors carry OUR shared secret, so they are always
        # identity-aware: per-end-user turns get a minted HMAC bearer instead of
        # the static secret (single-user fallback is used only when no end-user).
        srv: dict = {"name": name, "url": url, "identity": True}
        if r.get("token"):
            srv["access_token"] = r["token"]
        if r.get("display_name"):
            srv["display_name"] = r["display_name"]
        out.append(srv)
    return out


def list_connector_details() -> list[dict]:
    """Return [{name, url, display_name}] without secrets — for the UI."""
    if _supabase_enabled() and not is_env_managed():
        servers = _load_connectors_from_supabase()
    else:
        servers = _read_mcp_config().get("servers", [])
    return [
        {
            "name": s["name"],
            "url": s.get("url", ""),
            "display_name": s.get("display_name") or s["name"],
        }
        for s in servers
        if s.get("name")
    ]


_AGENT_TOOLSET = "agent_toolset_20260401"
# Tools withheld from the read agent (the write agent gets the full toolset).
# Note: this toolset has no `notebook` member — disabling write/edit/bash is the
# complete read-scoping set.
_READ_DISABLED_TOOLS = ("write", "edit", "bash")


# ── Action approval policy ─────────────────────────────────────────────────────
# Reads and small data-saving writes run unattended; destructive / code-changing /
# communication actions need explicit user approval; money movement is blocked
# outright. The classifier is conservative: anything it can't confidently call
# "safe" falls through to "ask", never "allow".
_READ_TOOLS = {"read", "glob", "grep", "web_search", "web_fetch", "view", "ls", "list_dir"}
# Read-only verb prefixes (covers MCP connector reads: get_quote, scan_watchlist,
# review_journal, check_contradictions, find_mispricing, …).
_READ_PREFIXES = (
    "get_", "list_", "read_", "search_", "scan_", "review_", "find_", "check_", "fetch_", "view_",
)
_CODE_EXTS = (
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".c", ".cc", ".cpp",
    ".h", ".hpp", ".cs", ".rb", ".php", ".sh", ".bash", ".zsh", ".sql", ".html", ".css",
    ".scss", ".vue", ".swift", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".env",
)
# Token-based matching (robust to snake_case / camelCase) rather than regex word
# boundaries, which miss compound names like `buy_stock` or `placeOrder`.
_MONEY_WORDS = {
    "buy", "sell", "order", "trade", "transfer", "withdraw", "deposit", "wire",
    "liquidate", "purchase", "sweep", "remit",
}
_COMMS_WORDS = {
    "send", "email", "mail", "message", "dm", "sms", "post", "publish", "tweet",
    "slack", "notify", "broadcast", "reply", "share", "comment",
}
_DESTRUCTIVE_WORDS = {
    "delete", "remove", "destroy", "drop", "truncate", "wipe", "purge", "reset",
    "revoke", "cancel", "clear",
}


def _name_tokens(name: str) -> set[str]:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name or "")
    return {t for t in re.split(r"[^a-zA-Z0-9]+", s.lower()) if t}


def _write_approval_bytes() -> int:
    """Size above which even a plain data write needs sign-off. Default 5 MB so
    images and other media save without a prompt; only very large writes pause.
    Override per-tenant via the `motor_write_approval_bytes` setting."""
    try:
        return int(settings.get("motor_write_approval_bytes") or 5_000_000)
    except Exception:
        return 5_000_000


def _classify_action(tool: str, args, write_allowed: bool) -> tuple[str, str]:
    """Classify a pending tool call. Returns (decision, reason) where decision is
    'allow' (run unattended) | 'ask' (needs user approval) | 'block' (never run).
    Conservative: anything not confidently safe falls through to 'ask'."""
    name = (tool or "").strip().lower()
    args = args if isinstance(args, dict) else {}
    toks = _name_tokens(tool)  # original casing → camelCase split works
    # 1) Reads first — never mis-block a data pull.
    if name in _READ_TOOLS or name.startswith(_READ_PREFIXES):
        return ("allow", "")
    # 2) Money movement is blocked outright (also covered by the platform trade ban).
    if toks & _MONEY_WORDS:
        return ("block", f"{tool} moves money or places an order")
    # 3) Communication out.
    if toks & _COMMS_WORDS:
        return ("ask", f"{tool} would send communication")
    # 4) Destructive mutation.
    if toks & _DESTRUCTIVE_WORDS:
        return ("ask", f"{tool} is destructive")
    # 5) Arbitrary shell / edits to existing content (often code).
    if name == "bash":
        return ("ask", "runs a shell command")
    if name == "edit":
        return ("ask", "edits existing content")
    # 6) Writes: code/config files always ask; data files ask only when very large.
    if name in ("write", "write_file", "append_file") or name.startswith(
        ("write_", "save_", "log_", "append_", "store_", "record_")
    ):
        path = str(args.get("path") or args.get("file_path") or args.get("filename") or "")
        content = args.get("content") or args.get("text") or args.get("body") or ""
        size = len(content if isinstance(content, (str, bytes)) else str(content))
        if path.lower().endswith(_CODE_EXTS):
            return ("ask", f"writes a code/config file ({path})")
        if size > _write_approval_bytes():
            return ("ask", f"large write (~{size} bytes)")
        return ("allow", "")
    # Anything unrecognized is treated as sensitive.
    return ("ask", f"{tool} needs review")

# Standing guidance — the per-call task + facts go in the user message; this is
# the equivalent of CloudExecutor._build_prompt's standing instructions.
_SYSTEM_GUIDANCE = (
    "You are the action-execution arm of an AI brain. Carry out the user's task "
    "using your tools and connected services, then report back concisely.\n"
    "If your response will be lengthy (more than ~400 words), write the full "
    "findings to a file under /mnt/session/outputs/ and return only a concise "
    "summary referencing the file.\n"
    "When reading files: get the text content; if the file is HTML, strip all "
    "markup and work only with the readable text. Never return raw file contents "
    "— always respond with your own understanding or summary of what the file "
    "contains."
)


class CMAExecutor(ExecutorCommon):
    """Managed-Agents-backed executor. Drop-in for CloudExecutor."""

    def __init__(self, bus: Bus, schema_store=None) -> None:
        self._bus = bus
        self._schema = schema_store
        self._pending: dict | None = None

        # Lazy/async-provisioned state (no network in __init__).
        self._client = None
        self._ready = False
        self._ready_lock = asyncio.Lock()
        self._read_agent_id: str | None = None
        self._write_agent_id: str | None = None
        self._env_id: str | None = None
        self._vault_id: str | None = None
        self._session_id: str | None = None
        self._session_agent: str | None = None

        # Per-end-user vault + session cache (in-memory; survives the process lifetime).
        # Keyed by end_user_id; values are {vault_id, mcpu_exp_ms, cred_ids:{url:id}}.
        self._user_vault_cache: dict[str, dict] = {}
        # Per-user CMA sessions keyed by f"{agent_id}:{vault_id}" — kept separate
        # from the org-level warm session so users get credential-isolated sessions.
        self._user_sessions: dict[str, str] = {}
        self._current_end_user_id: str | None = None

        self._user_id = os.environ.get("BRAIN_USER_ID", "").strip()
        self._model = str(settings.get("cma_model") or "claude-opus-4-6")
        self._state = self._load_state()
        self._mcp_servers = self._load_mcp_config()
        # Optional connector allowlist (lowercased names) set per-dispatch by the
        # motor cortex — None = all configured connectors. Lets self-directed
        # work run with a narrower connector set than user-commanded work.
        self._connector_filter: set[str] | None = None

        # Approval hook for actions the classifier flags 'ask'. Signature:
        #   approval_fn(action: dict) -> "allow" | "deny"  (sync or async)
        # where action = {tool, input, reason, turn_id}. None → 'ask' actions are
        # denied (skipped) so nothing sensitive runs unattended.
        self._approval_fn: Callable[[dict], object] | None = None
        self._current_turn_id: str = ""

        logger.info(
            "[CMAExecutor] initialized (user=%s, model=%s, connectors=%s)",
            self._user_id or "local",
            self._model,
            self.connectors_summary(),
        )

    # ── Availability + summary (cheap, no network) ─────────────────────────────

    @property
    def available(self) -> bool:
        # The executor is only constructed when the CMA flag is selected
        # (session_setup), so the meaningful runtime check is that a key exists.
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def connectors_summary(self) -> str:
        if self._mcp_servers:
            return ", ".join(sorted(s["name"] for s in self._mcp_servers))
        return "no MCP connectors configured"

    # ── State + config persistence ─────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _STATE_PATH.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("[CMAExecutor] could not persist state: %s", e)

    def _load_mcp_config(self) -> list[dict]:
        """Load remote MCP server list (+ optional OAuth creds) for v1 seeding.

        Sources, in order: BRAIN_CMA_MCP_SERVERS (JSON) → Supabase mcp_connectors
        (hosted, org-scoped) → cma_mcp.json (local fallback). Connectors are an
        ORG-level concept, so the Supabase registry is the source of truth on
        hosted — never the persona-namespaced volume file.
        Each server: {"name","url"} with optional "access_token"/"expires_at"/
        "refresh"; tokens may also come from env BRAIN_CMA_MCP_<NAME>_TOKEN /
        _REFRESH_TOKEN / _CLIENT_ID / _TOKEN_ENDPOINT. This is the seam where the
        future interactive "Connect" (OAuth) flow plugs in — it just needs to
        make creds available here.
        """
        data = None
        # Env-pinned connectors apply only to the owning org (is_env_managed gates
        # this) — otherwise every tenant brain would inherit them. A non-owner org
        # skips the env list and resolves its own org-scoped registry below.
        if is_env_managed():
            raw = os.environ.get("BRAIN_CMA_MCP_SERVERS", "").strip()
            try:
                data = json.loads(raw)
            except Exception as e:
                logger.warning("[CMAExecutor] bad BRAIN_CMA_MCP_SERVERS JSON: %s", e)
        if data is None and _supabase_enabled():
            sb = _load_connectors_from_supabase()
            if sb:
                data = {"servers": sb}
        if data is None and _MCP_CONFIG_PATH.exists():
            try:
                data = json.loads(_MCP_CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("[CMAExecutor] bad cma_mcp.json: %s", e)
        if not data:
            return []
        items = data.get("servers") if isinstance(data, dict) else data
        servers: list[dict] = []
        for it in items or []:
            name = (it or {}).get("name")
            url = (it or {}).get("url")
            if not name or not url:
                continue
            srv: dict = {"name": name, "url": url}
            # Identity-aware = brain mints a per-end-user HMAC bearer from our shared
            # secret. Supabase rows and locally-registered file entries carry our
            # secret (default on); env-pinned connectors may carry a real OAuth bearer
            # instead, so they default OFF unless the JSON sets "identity": true.
            srv["identity"] = bool(it.get("identity", not is_env_managed()))
            env_key = name.upper().replace("-", "_")
            token = it.get("access_token") or os.environ.get(f"BRAIN_CMA_MCP_{env_key}_TOKEN")
            if token:
                srv["access_token"] = token
            if it.get("expires_at"):
                srv["expires_at"] = it["expires_at"]
            refresh = it.get("refresh")
            if not refresh:
                rt = os.environ.get(f"BRAIN_CMA_MCP_{env_key}_REFRESH_TOKEN")
                if rt:
                    refresh = {"refresh_token": rt}
                    ci = os.environ.get(f"BRAIN_CMA_MCP_{env_key}_CLIENT_ID")
                    te = os.environ.get(f"BRAIN_CMA_MCP_{env_key}_TOKEN_ENDPOINT")
                    if ci:
                        refresh["client_id"] = ci
                    if te:
                        refresh["token_endpoint"] = te
            if refresh:
                srv["refresh"] = refresh
            servers.append(srv)
        return servers

    # ── Client + provisioning ──────────────────────────────────────────────────

    def _get_client(self):
        import anthropic
        import httpx

        read_to = float(settings.get("anthropic_timeout_s") or 120.0)
        connect_to = float(settings.get("anthropic_connect_timeout_s") or 10.0)
        retries = int(settings.get("anthropic_max_retries") or 2)
        return anthropic.AsyncAnthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            timeout=httpx.Timeout(read_to, connect=connect_to),
            max_retries=retries,
        )

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        async with self._ready_lock:
            if self._ready:
                return
            self._client = self._get_client()
            await self._ensure_environment()
            await self._ensure_vault()
            await self._ensure_agents()
            self._ready = True

    async def _ensure_environment(self) -> None:
        env_id = self._state.get("env_id")
        if env_id:
            try:
                await self._client.beta.environments.retrieve(env_id)
                self._env_id = env_id
                return
            except Exception:
                pass
        networking = str(settings.get("cma_networking") or "unrestricted")
        env = await self._client.beta.environments.create(
            name=f"brain-cma-{self._user_id or 'local'}",
            config={"type": "cloud", "networking": {"type": networking}},
        )
        self._env_id = env.id
        self._state["env_id"] = env.id
        self._save_state()

    async def _ensure_vault(self) -> None:
        if not self._mcp_servers:
            self._vault_id = None
            return
        vault_id = self._state.get("vault_id")
        if vault_id:
            try:
                await self._client.beta.vaults.retrieve(vault_id)
                self._vault_id = vault_id
            except Exception:
                self._vault_id = None
        if not self._vault_id:
            v = await self._client.beta.vaults.create(
                display_name=f"brain-cma-{self._user_id or 'local'}"
            )
            self._vault_id = v.id
            self._state["vault_id"] = v.id
            self._state["seeded_mcp"] = []
            self._save_state()

        seeded = set(self._state.get("seeded_mcp") or [])
        for srv in self._mcp_servers:
            if not srv.get("access_token") or srv["url"] in seeded:
                continue
            auth: dict = {
                "type": "mcp_oauth",
                "mcp_server_url": srv["url"],
                "access_token": srv["access_token"],
            }
            if srv.get("expires_at"):
                auth["expires_at"] = srv["expires_at"]
            if srv.get("refresh"):
                auth["refresh"] = srv["refresh"]
            try:
                await self._client.beta.vaults.credentials.create(
                    self._vault_id, auth=auth, display_name=srv["name"]
                )
                seeded.add(srv["url"])
            except Exception as e:
                logger.warning("[CMAExecutor] seeding credential for %s failed: %s", srv["name"], e)
        self._state["seeded_mcp"] = sorted(seeded)
        self._save_state()

    # ── Per-end-user vault provisioning ───────────────────────────────────────

    async def _fetch_end_user_tokens(self, end_user_id: str) -> list[dict]:
        """Return [{server_name, server_url, token, expires_at}] from Supabase,
        or [] when the backend is disabled or no tokens are stored."""
        try:
            from brain.second_brain import supabase_client

            if not supabase_client.is_enabled():
                return []
            resp = (
                supabase_client.get_client()
                .rpc("get_end_user_mcp_tokens", {"p_end_user_id": end_user_id})
                .execute()
            )
            data = resp.data
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(
                "[CMAExecutor] could not fetch end-user tokens for %s: %s", end_user_id, e
            )
            return []

    def _identity_connectors(self) -> list[dict]:
        """Configured connectors that take a brain-minted per-end-user HMAC bearer."""
        return [s for s in self._mcp_servers if s.get("identity") and s.get("access_token")]

    async def _ensure_user_vault(self, end_user_id: str) -> str | None:
        """Return the Anthropic Vault ID for this end-user, provisioning lazily.

        The per-user vault holds two credential kinds:
          • partner-stored OAuth tokens (end_user_mcp_tokens) → mcp_oauth creds
          • brain-minted HMAC identity tokens for identity-aware connectors →
            static_bearer creds (refreshed in place before the 1-hour TTL lapses)
        Returns None when neither applies (caller falls back to the org vault)."""
        now_ms = int(time.time() * 1000)
        cached = self._user_vault_cache.get(end_user_id)
        if cached:
            # Refresh minted identity tokens in place if they're near expiry — no
            # new vault, just an update to each static_bearer credential.
            if cached.get("mcpu_exp_ms", 0) - now_ms > _MCPU_REFRESH_BUFFER_S * 1000:
                return cached["vault_id"]
            await self._refresh_user_identity_tokens(end_user_id, cached, now_ms)
            return cached["vault_id"]

        oauth_tokens = await self._fetch_end_user_tokens(end_user_id)
        identity_conns = self._identity_connectors()
        if not oauth_tokens and not identity_conns:
            return None

        vault_name = f"brain-cma-{self._user_id or 'local'}-{end_user_id}"
        try:
            v = await self._client.beta.vaults.create(display_name=vault_name)
            vault_id = v.id
        except Exception as e:
            logger.warning("[CMAExecutor] user vault creation failed for %s: %s", end_user_id, e)
            return None

        for tok in oauth_tokens:
            auth: dict = {
                "type": "mcp_oauth",
                "mcp_server_url": tok["server_url"],
                "access_token": tok["token"],
            }
            if tok.get("expires_at"):
                auth["expires_at"] = tok["expires_at"]
            try:
                await self._client.beta.vaults.credentials.create(
                    vault_id, auth=auth, display_name=tok["server_name"]
                )
            except Exception as e:
                logger.warning(
                    "[CMAExecutor] seeding user OAuth credential %s/%s: %s",
                    end_user_id,
                    tok["server_name"],
                    e,
                )

        cred_ids: dict[str, str] = {}  # connector url -> static_bearer credential id
        min_exp_ms = now_ms + 3600 * 1000
        for srv in identity_conns:
            token, exp_ms = mint_end_user_token(end_user_id, srv["access_token"], now_ms=now_ms)
            min_exp_ms = min(min_exp_ms, exp_ms)
            try:
                cred = await self._client.beta.vaults.credentials.create(
                    vault_id,
                    auth={"type": "static_bearer", "mcp_server_url": srv["url"], "token": token},
                    display_name=srv["name"],
                )
                cred_ids[srv["url"]] = cred.id
            except Exception as e:
                logger.warning(
                    "[CMAExecutor] seeding user identity credential %s/%s: %s",
                    end_user_id,
                    srv["name"],
                    e,
                )

        self._user_vault_cache[end_user_id] = {
            "vault_id": vault_id,
            "mcpu_exp_ms": min_exp_ms,
            "cred_ids": cred_ids,
        }
        logger.info("[CMAExecutor] provisioned per-user vault %s for %s", vault_id, end_user_id)
        return vault_id

    async def _refresh_user_identity_tokens(
        self, end_user_id: str, cached: dict, now_ms: int
    ) -> None:
        """Re-mint and update-in-place each identity connector's static_bearer cred."""
        vault_id = cached["vault_id"]
        cred_ids: dict = cached.get("cred_ids", {})
        min_exp_ms = now_ms + 3600 * 1000
        for srv in self._identity_connectors():
            cid = cred_ids.get(srv["url"])
            if not cid:
                continue
            token, exp_ms = mint_end_user_token(end_user_id, srv["access_token"], now_ms=now_ms)
            min_exp_ms = min(min_exp_ms, exp_ms)
            try:
                await self._client.beta.vaults.credentials.update(
                    cid,
                    vault_id=vault_id,
                    auth={"type": "static_bearer", "mcp_server_url": srv["url"], "token": token},
                )
            except Exception as e:
                logger.warning(
                    "[CMAExecutor] refreshing user identity credential %s/%s: %s",
                    end_user_id,
                    srv["name"],
                    e,
                )
        cached["mcpu_exp_ms"] = min_exp_ms

    def connector_names(self) -> list[str]:
        """All configured connector names (unfiltered) — for the settings UI."""
        return sorted({s["name"] for s in self._mcp_servers})

    def reload_mcp_config(self) -> None:
        """Reload the connector registry into memory. Call after register/remove."""
        self._mcp_servers = self._load_mcp_config()
        # Force agent re-creation on next task (config hash will differ).
        self._ready = False
        # Drop per-end-user vault + session caches so a newly registered connector
        # is seeded into already-provisioned per-user vaults on their next turn
        # (otherwise the cached vault id is reused and the new credential is never
        # added until process restart).
        self._user_vault_cache.clear()
        self._user_sessions.clear()
        logger.info("[CMAExecutor] MCP config reloaded (%d connectors)", len(self._mcp_servers))

    def set_approval_fn(self, fn: Callable[[dict], object] | None) -> None:
        """Wire the human-approval hook for 'ask' actions (see __init__)."""
        self._approval_fn = fn

    def set_connector_filter(self, names: set[str] | None) -> None:
        """Restrict which MCP connectors the NEXT agent session may use.
        None = all. Filter participates in the config hash, so a warm session
        built with broader connectors is never reused for a narrower policy."""
        self._connector_filter = {n.strip().lower() for n in names} if names else None

    def _active_mcp_servers(self) -> list[dict]:
        if self._connector_filter is None:
            return self._mcp_servers
        return [s for s in self._mcp_servers if s["name"].strip().lower() in self._connector_filter]

    def _agent_tools(self, write_allowed: bool) -> list[dict]:
        # Reads run server-side with no prompt (always_allow). Mutating built-in
        # tools (write/edit/bash) pause for review (always_ask) so the executor can
        # classify each call: small data writes auto-approve, while large /
        # destructive / code-changing actions get routed to the user for approval.
        toolset: dict = {
            "type": _AGENT_TOOLSET,
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        }
        if not write_allowed:
            # Read agent: mutating tools are removed outright — only reads remain.
            toolset["configs"] = [{"name": n, "enabled": False} for n in _READ_DISABLED_TOOLS]
        else:
            toolset["configs"] = [
                {"name": n, "permission_policy": {"type": "always_ask"}}
                for n in _READ_DISABLED_TOOLS
            ]
        tools: list[dict] = [toolset]
        for srv in self._active_mcp_servers():
            entry: dict = {"type": "mcp_toolset", "mcp_server_name": srv["name"]}
            # On the write path, connector calls also pause for review; the executor
            # auto-approves read-only connector tools at decision time, so only
            # connector mutations actually reach the approval hook.
            if write_allowed:
                entry["default_config"] = {"permission_policy": {"type": "always_ask"}}
            tools.append(entry)
        return tools

    def _mcp_server_decls(self) -> list[dict]:
        return [
            {"name": s["name"], "type": "url", "url": s["url"]} for s in self._active_mcp_servers()
        ]

    def _config_hash(self) -> str:
        blob = json.dumps(
            {
                "model": self._model,
                "system": _SYSTEM_GUIDANCE,
                "mcp": self._mcp_server_decls(),
                "toolset": _AGENT_TOOLSET,
            },
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    async def _ensure_agents(self) -> None:
        want_hash = self._config_hash()
        hash_ok = self._state.get("agent_config_hash") == want_hash
        for kind, write_allowed, state_key in (
            ("read", False, "read_agent_id"),
            ("write", True, "write_agent_id"),
        ):
            aid = self._state.get(state_key)
            valid = False
            if aid and hash_ok:
                try:
                    await self._client.beta.agents.retrieve(aid)
                    valid = True
                except Exception:
                    valid = False
            if valid:
                setattr(self, f"_{kind}_agent_id", aid)
                continue
            # (Re)create. On config change we create a fresh agent rather than
            # version-bumping — simpler and robust; old agents are harmless.
            a = await self._client.beta.agents.create(
                model=self._model,
                name=f"brain-{kind}",
                system=_SYSTEM_GUIDANCE,
                tools=self._agent_tools(write_allowed),
                mcp_servers=self._mcp_server_decls(),
            )
            setattr(self, f"_{kind}_agent_id", a.id)
            self._state[state_key] = a.id
        self._state["agent_config_hash"] = want_hash
        self._save_state()

    # ── Public execution paths (CloudExecutor-compatible) ──────────────────────

    async def execute_read(
        self, task: str, context_facts: list[str], turn_id: str = "", end_user_id: str | None = None
    ) -> dict:
        self._current_end_user_id = end_user_id
        return await self._run(
            task, context_facts, turn_id=turn_id, write_allowed=False, end_user_id=end_user_id
        )

    async def execute_pending(self, turn_id: str = "") -> dict | None:
        if not self._pending:
            return None
        action = self._pending
        self._pending = None
        return await self._run(
            action["task"],
            action.get("context_facts", []),
            turn_id=turn_id,
            write_allowed=True,
            end_user_id=self._current_end_user_id,
        )

    async def _run(
        self,
        task: str,
        context_facts: list[str],
        turn_id: str = "",
        write_allowed: bool = False,
        end_user_id: str | None = None,
    ) -> dict:
        if not self.available:
            return {
                "tool": "cloud_action",
                "output": "[error] CMA executor not available (no ANTHROPIC_API_KEY).",
                "success": False,
            }
        self._current_turn_id = turn_id or ""
        start = time.time()
        try:
            await self._ensure_ready()
            # Resolve per-end-user vault (if any tokens are stored for this user).
            # Falls back to None → org-level vault used in _ensure_session.
            user_vault_id: str | None = None
            if end_user_id:
                user_vault_id = await self._ensure_user_vault(end_user_id)
            timeout = float(settings.get("cma_task_timeout_s") or 120.0)
            raw = await asyncio.wait_for(
                self._drive_task(task, context_facts, write_allowed, user_vault_id=user_vault_id),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning("[CMAExecutor] task timed out after %.1fs", time.time() - start)
            raw = "[error] CMA task timed out."
        except Exception as e:
            logger.error("[CMAExecutor] task failed: %s", e)
            raw = f"[error] {e}"

        output = self._screen_result(raw)
        # success from the RAW text — fenced clean output never starts with [error]
        success = not raw.startswith("[error]") and not output.startswith("[error]")
        logger.info(
            "[CMAExecutor] Completed in %.1fs (success=%s, %d chars)",
            time.time() - start,
            success,
            len(output),
        )
        await self._append_tool_log(task, output, success)
        return {"tool": "cloud_action", "output": output, "success": success}

    # ── Task composition + session drive loop ──────────────────────────────────

    def _compose_task(self, task: str, context_facts: list[str]) -> str:
        parts = [task]
        if context_facts:
            facts_str = "; ".join(f.strip() for f in context_facts if f.strip())
            if facts_str:
                parts.append(f"Context: {facts_str}")
        return "\n".join(parts)

    async def _ensure_session(self, write_allowed: bool, user_vault_id: str | None = None) -> str:
        want_agent = self._write_agent_id if write_allowed else self._read_agent_id

        if user_vault_id:
            # Per-end-user session — keyed in memory by (agent, vault) so each
            # end-user's credentials stay isolated.
            key = f"{want_agent}:{user_vault_id}"
            sid = self._user_sessions.get(key)
            if sid:
                try:
                    s = await self._client.beta.sessions.retrieve(sid)
                    if getattr(s, "status", None) != "terminated":
                        return sid
                except Exception:
                    pass
            kwargs: dict = {
                "agent": want_agent,
                "environment_id": self._env_id,
                "title": "brain-user",
            }
            kwargs["vault_ids"] = [user_vault_id]
            s = await self._client.beta.sessions.create(**kwargs)
            self._user_sessions[key] = s.id
            return s.id

        # Org-level warm session (existing behaviour, unchanged).
        reuse = bool(int(settings.get("cma_session_warm_reuse") or 1))
        if reuse and self._session_id and self._session_agent == want_agent:
            try:
                s = await self._client.beta.sessions.retrieve(self._session_id)
                if getattr(s, "status", None) != "terminated":
                    return self._session_id
            except Exception:
                pass
        kwargs = {"agent": want_agent, "environment_id": self._env_id, "title": "brain-warm"}
        if self._vault_id:
            kwargs["vault_ids"] = [self._vault_id]
        s = await self._client.beta.sessions.create(**kwargs)
        self._session_id = s.id
        self._session_agent = want_agent
        return s.id

    async def _drive_task(
        self,
        task: str,
        context_facts: list[str],
        write_allowed: bool,
        user_vault_id: str | None = None,
    ) -> str:
        sid = await self._ensure_session(write_allowed, user_vault_id=user_vault_id)
        text = await self._consume(sid, self._compose_task(task, context_facts), write_allowed)
        return text or "(no output)"

    async def _consume(self, sid: str, message: str, write_allowed: bool = False) -> str:
        """Stream-first event loop with bounded reconnect-and-replay."""
        seen: set[str] = set()
        buf: dict = {}  # event_id (or index) -> text, insertion-ordered
        tools: dict = {}  # tool-call event id -> {name, input} (for approval routing)
        max_reconnects = int(settings.get("cma_max_reconnects") or 3)
        sent = False
        attempts = 0

        while True:
            try:
                stream = await self._client.beta.sessions.events.stream(sid)
                try:
                    if not sent:
                        await self._client.beta.sessions.events.send(
                            sid,
                            events=[
                                {
                                    "type": "user.message",
                                    "content": [{"type": "text", "text": message}],
                                }
                            ],
                        )
                        sent = True
                    async for ev in stream:
                        done, err, pending = self._handle_event(ev, seen, buf, tools)
                        if err is not None:
                            return err
                        if pending:
                            # Agent paused on tool calls that need our go-ahead.
                            # Classify each: reads + small data writes auto-approve,
                            # money is blocked, and anything sensitive is routed to
                            # the approval hook — keep the session running either way.
                            await self._resolve_pending(sid, pending, tools, write_allowed)
                            continue
                        if done:
                            return self._join(buf)
                finally:
                    await self._close_stream(stream)
            except Exception as e:
                attempts += 1
                logger.warning("[CMAExecutor] stream error (attempt %d): %s", attempts, e)
                if attempts > max_reconnects:
                    return self._join(buf) or f"[error] CMA stream failed: {e}"
                # fall through to history replay below

            # Stream ended (or dropped) without a terminal event — replay history
            # to catch anything missed, then either finish or reconnect.
            attempts += 1
            try:
                if await self._replay_history(sid, seen, buf):
                    return self._join(buf)
            except Exception as e:
                logger.warning("[CMAExecutor] history replay failed: %s", e)
            if attempts > max_reconnects:
                return self._join(buf) or "(no output)"

    def _handle_event(
        self, ev, seen: set, buf: dict, tools: dict | None = None
    ) -> tuple[bool, str | None, list | None]:
        """Return (done, error, pending). done=True is terminal; error!=None
        short-circuits; pending is a non-empty list of tool-call event ids the
        caller must approve before the session will continue."""
        etype = getattr(ev, "type", None)
        eid = getattr(ev, "id", None)
        if etype in ("agent.tool_use", "agent.mcp_tool_use"):
            # Record name/input so a later requires_action can classify this call.
            if tools is not None and eid:
                tools[eid] = {
                    "name": getattr(ev, "name", None) or getattr(ev, "tool_name", "") or "",
                    "input": getattr(ev, "input", None) or getattr(ev, "args", None) or {},
                }
            return (False, None, None)
        if etype == "agent.message":
            key = eid or f"_idx{len(buf)}"
            if key in seen:
                return (False, None, None)
            seen.add(key)
            buf[key] = self._extract_text(ev)
            return (False, None, None)
        if etype == "session.error":
            msg = getattr(getattr(ev, "error", None), "message", None) or "session error"
            return (False, f"[error] {msg}", None)
        if etype == "session.status_terminated":
            return (True, None, None)
        if etype == "session.status_idle":
            sr = getattr(ev, "stop_reason", None)
            if getattr(sr, "type", None) == "requires_action":
                # Agent is blocked on us to approve its tool calls — NOT terminal.
                # Surface the pending event ids so the caller can auto-confirm and
                # keep the session running.
                ids = [i for i in (getattr(sr, "event_ids", None) or []) if i]
                if ids:
                    return (False, None, ids)
                logger.warning("[CMAExecutor] requires_action with no event ids — ending")
            return (True, None, None)
        return (False, None, None)

    @staticmethod
    def _extract_text(ev) -> str:
        parts = []
        for b in getattr(ev, "content", None) or []:
            if getattr(b, "type", None) == "text":
                parts.append(getattr(b, "text", "") or "")
            else:
                t = getattr(b, "text", None)
                if t:
                    parts.append(t)
        return "".join(parts)

    @staticmethod
    def _join(buf: dict) -> str:
        return "".join(v for v in buf.values() if v).strip()

    @staticmethod
    async def _maybe_await(value):
        if asyncio.iscoroutine(value):
            return await value
        return value

    async def _resolve_pending(
        self, sid: str, pending: list, tools: dict, write_allowed: bool
    ) -> None:
        """Classify each paused tool call and answer it: allow safe ones, deny
        money outright, and route sensitive ones to the approval hook (default:
        deny/skip). Always responds so the session never strands on a 400."""
        events: list[dict] = []
        for eid in pending:
            call = tools.get(eid) or {}
            name = call.get("name", "")
            decision, reason = _classify_action(name, call.get("input"), write_allowed)
            if decision == "ask":
                verdict = "deny"
                if self._approval_fn is not None:
                    try:
                        verdict = await self._maybe_await(
                            self._approval_fn(
                                {
                                    "tool": name,
                                    "input": call.get("input"),
                                    "reason": reason,
                                    "turn_id": self._current_turn_id,
                                    "end_user_id": self._current_end_user_id or "",
                                }
                            )
                        )
                    except Exception as _ae:
                        logger.warning("[CMAExecutor] approval hook failed for %s: %s", name, _ae)
                        verdict = "deny"
                decision = "allow" if verdict == "allow" else "deny"
            if decision == "allow":
                events.append(
                    {"type": "user.tool_confirmation", "tool_use_id": eid, "result": "allow"}
                )
            else:
                events.append(
                    {
                        "type": "user.tool_confirmation",
                        "tool_use_id": eid,
                        "result": "deny",
                        "deny_message": (
                            f"Not run — needs the user's approval ({reason or 'sensitive action'}). "
                            "Continue with what you can do without it."
                        ),
                    }
                )
                logger.info("[CMAExecutor] action gated (%s): %s", reason or "sensitive", name)
        if events:
            await self._client.beta.sessions.events.send(sid, events=events)

    async def _replay_history(self, sid: str, seen: set, buf: dict) -> bool:
        """Page the full event list, dedupe by id, fold in. Return True if a
        terminal event is present."""
        terminal = False
        async for ev in self._client.beta.sessions.events.list(sid, order="asc"):
            done, _err, _pending = self._handle_event(ev, seen, buf)
            if done:
                terminal = True
        return terminal

    @staticmethod
    async def _close_stream(stream) -> None:
        try:
            close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
            if close:
                r = close()
                if asyncio.iscoroutine(r):
                    await r
        except Exception:
            pass
