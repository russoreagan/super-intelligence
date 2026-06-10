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
import time
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

_AGENT_TOOLSET = "agent_toolset_20260401"
# Tools withheld from the read agent (the write agent gets the full toolset).
# Note: this toolset has no `notebook` member — disabling write/edit/bash is the
# complete read-scoping set.
_READ_DISABLED_TOOLS = ("write", "edit", "bash")

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

        self._user_id = os.environ.get("BRAIN_USER_ID", "").strip()
        self._model = str(settings.get("cma_model") or "claude-opus-4-6")
        self._state = self._load_state()
        self._mcp_servers = self._load_mcp_config()
        # Optional connector allowlist (lowercased names) set per-dispatch by the
        # motor cortex — None = all configured connectors. Lets self-directed
        # work run with a narrower connector set than user-commanded work.
        self._connector_filter: set[str] | None = None

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

        Sources, in order: BRAIN_CMA_MCP_SERVERS (JSON), then cma_mcp.json.
        Each server: {"name","url"} with optional "access_token"/"expires_at"/
        "refresh"; tokens may also come from env BRAIN_CMA_MCP_<NAME>_TOKEN /
        _REFRESH_TOKEN / _CLIENT_ID / _TOKEN_ENDPOINT. This is the seam where the
        future interactive "Connect" (OAuth) flow plugs in — it just needs to
        make creds available here.
        """
        data = None
        raw = os.environ.get("BRAIN_CMA_MCP_SERVERS", "").strip()
        if raw:
            try:
                data = json.loads(raw)
            except Exception as e:
                logger.warning("[CMAExecutor] bad BRAIN_CMA_MCP_SERVERS JSON: %s", e)
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
        toolset: dict = {"type": _AGENT_TOOLSET, "default_config": {"enabled": True}}
        if not write_allowed:
            toolset["configs"] = [{"name": n, "enabled": False} for n in _READ_DISABLED_TOOLS]
        tools: list[dict] = [toolset]
        for srv in self._active_mcp_servers():
            tools.append({"type": "mcp_toolset", "mcp_server_name": srv["name"]})
        return tools

    def _mcp_server_decls(self) -> list[dict]:
        return [
            {"name": s["name"], "type": "url", "url": s["url"]}
            for s in self._active_mcp_servers()
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

    async def execute_read(self, task: str, context_facts: list[str], turn_id: str = "") -> dict:
        return await self._run(task, context_facts, turn_id=turn_id, write_allowed=False)

    async def execute_pending(self, turn_id: str = "") -> dict | None:
        if not self._pending:
            return None
        action = self._pending
        self._pending = None
        return await self._run(
            action["task"], action.get("context_facts", []), turn_id=turn_id, write_allowed=True
        )

    async def _run(
        self, task: str, context_facts: list[str], turn_id: str = "", write_allowed: bool = False
    ) -> dict:
        if not self.available:
            return {
                "tool": "cloud_action",
                "output": "[error] CMA executor not available (no ANTHROPIC_API_KEY).",
                "success": False,
            }
        start = time.time()
        try:
            await self._ensure_ready()
            timeout = float(settings.get("cma_task_timeout_s") or 120.0)
            raw = await asyncio.wait_for(
                self._drive_task(task, context_facts, write_allowed), timeout=timeout
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

    async def _ensure_session(self, write_allowed: bool) -> str:
        want_agent = self._write_agent_id if write_allowed else self._read_agent_id
        reuse = bool(int(settings.get("cma_session_warm_reuse") or 1))
        if reuse and self._session_id and self._session_agent == want_agent:
            try:
                s = await self._client.beta.sessions.retrieve(self._session_id)
                if getattr(s, "status", None) != "terminated":
                    return self._session_id
            except Exception:
                pass
        kwargs: dict = {"agent": want_agent, "environment_id": self._env_id, "title": "brain-warm"}
        if self._vault_id:
            kwargs["vault_ids"] = [self._vault_id]
        s = await self._client.beta.sessions.create(**kwargs)
        self._session_id = s.id
        self._session_agent = want_agent
        return s.id

    async def _drive_task(self, task: str, context_facts: list[str], write_allowed: bool) -> str:
        sid = await self._ensure_session(write_allowed)
        text = await self._consume(sid, self._compose_task(task, context_facts))
        return text or "(no output)"

    async def _consume(self, sid: str, message: str) -> str:
        """Stream-first event loop with bounded reconnect-and-replay."""
        seen: set[str] = set()
        buf: dict = {}  # event_id (or index) -> text, insertion-ordered
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
                                {"type": "user.message", "content": [{"type": "text", "text": message}]}
                            ],
                        )
                        sent = True
                    async for ev in stream:
                        done, err = self._handle_event(ev, seen, buf)
                        if err is not None:
                            return err
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

    def _handle_event(self, ev, seen: set, buf: dict) -> tuple[bool, str | None]:
        """Return (done, error). done=True means terminal; error!=None short-circuits."""
        etype = getattr(ev, "type", None)
        eid = getattr(ev, "id", None)
        if etype == "agent.message":
            key = eid or f"_idx{len(buf)}"
            if key in seen:
                return (False, None)
            seen.add(key)
            buf[key] = self._extract_text(ev)
            return (False, None)
        if etype == "session.error":
            msg = getattr(getattr(ev, "error", None), "message", None) or "session error"
            return (False, f"[error] {msg}")
        if etype == "session.status_terminated":
            return (True, None)
        if etype == "session.status_idle":
            sr = getattr(ev, "stop_reason", None)
            if getattr(sr, "type", None) == "requires_action":
                # v1 does not handle mid-task tool confirmations (writes are
                # pre-confirmed upstream). Don't hang — return what we have.
                logger.warning(
                    "[CMAExecutor] session idle requires_action — returning partial (v1)"
                )
            return (True, None)
        return (False, None)

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

    async def _replay_history(self, sid: str, seen: set, buf: dict) -> bool:
        """Page the full event list, dedupe by id, fold in. Return True if a
        terminal event is present."""
        terminal = False
        async for ev in self._client.beta.sessions.events.list(sid, order="asc"):
            done, _err = self._handle_event(ev, seen, buf)
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
