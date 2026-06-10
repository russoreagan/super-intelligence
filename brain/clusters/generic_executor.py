"""
GenericExecutor — a provider-agnostic cloud-action executor for the motor cortex.

Where CloudExecutor delegates whole jobs to a local Claude CLI subprocess and
CMAExecutor delegates them to Anthropic Managed Agents (both Anthropic-bound),
this executor runs the agent loop HERE, in-process, over the brain's own local
toolset (brain.clusters.motor_dispatcher.ToolDispatcher). The LLM only chooses
the next tool call; the brain executes it. That makes the whole tier
provider-agnostic:

  - any OpenAI-compatible chat model with function calling (GPT, and via
    OPENAI_BASE_URL: Groq / Mistral / DeepSeek / Together), or
  - the local / RunPod Ollama models the brain already runs (qwen),

driven through brain.model_router.ModelRouter.call_structured, which already
dispatches by provider. A weaker model picks worse actions but can do nothing
the allowlist forbids — the safety posture is the dispatcher's, not the
provider's.

It presents the EXACT CloudExecutor/CMAExecutor public surface (execute_read /
execute_pending / pending-state / confirmation detection / available /
connectors_summary) via ExecutorCommon, so motor_cortex, the session_turn
confirmation gate, and the executor test contract are untouched. Selected in
session_setup via BRAIN_EXECUTOR=generic / brain_executor="generic".

Guardrails preserved:
  1. Minimal context  — only task + operational facts reach the loop.
  2. Result fencing   — final output is screened + fenced (ExecutorCommon).
  3. Confirmation gate — write tools are withheld until execute_pending()
                         (i.e. AFTER the brain's set_pending → user-confirm
                         handshake), so a write is impossible pre-confirmation.
  4. Step ceiling     — ralph_max_total_attempts bounds tool calls per task so
                        a weak model can't thrash.

No MCP connectors (that is CMA's domain): connectors_summary reports none.
"""

from __future__ import annotations

import json
import logging
import os
import time

from brain.bus import Bus
from brain.clusters._executor_common import ExecutorCommon
from brain.clusters.motor_dispatcher import ToolDispatcher
from brain.settings import settings

logger = logging.getLogger(__name__)

CLUSTER = "generic_executor"

_SYSTEM_GUIDANCE = (
    "You are the action executor for a digital mind. You accomplish a single "
    "concrete task by calling the provided tools, then stop. Rules: take the "
    "fewest steps that finish the task; read before you write; never invent file "
    "paths — list a directory if unsure; if a tool returns text starting with "
    "[blocked] or [error], adapt or stop rather than retrying blindly. When the "
    "task is done, reply with a one-line summary of what you did and DO NOT call "
    "another tool."
)

# Read-only tools (always available) vs write tools (withheld until the brain's
# confirmation handshake authorizes a write run). Mirrors the read/write agent
# split in CloudExecutor/CMAExecutor.
_READ_TOOLS = ("fs_read", "fs_list", "fs_search", "run_command", "fetch_url")
_WRITE_TOOLS = ("fs_write", "fs_append")


def _tool_specs(write_allowed: bool) -> list[dict]:
    """JSON-schema tool definitions for call_structured-style function calling.
    run_command is read-tier because the command allowlist (the dispatcher's
    DEFAULT_COMMANDS) already excludes mutating commands; genuine file mutation
    is the fs_write/fs_append pair gated by write_allowed."""
    specs: dict[str, dict] = {
        "fs_read": {
            "description": "Read a text file. Returns its content (truncated at 4000 chars).",
            "properties": {"path": {"type": "string", "description": "Absolute path."}},
            "required": ["path"],
        },
        "fs_list": {
            "description": "List files in a directory.",
            "properties": {
                "path": {"type": "string"},
                "pattern": {"type": "string", "description": "glob, default '*'"},
                "recursive": {"type": "boolean"},
            },
            "required": ["path"],
        },
        "fs_search": {
            "description": "Search files under a directory for a query string.",
            "properties": {
                "path": {"type": "string"},
                "query": {"type": "string"},
                "file_pattern": {"type": "string", "description": "glob, default '*'"},
            },
            "required": ["path", "query"],
        },
        "run_command": {
            "description": "Run an allowlisted shell command (read-only set). 30s timeout.",
            "properties": {
                "cmd": {"type": "string"},
                "cwd": {"type": "string", "description": "Optional working dir (must be allowed)."},
            },
            "required": ["cmd"],
        },
        "fetch_url": {
            "description": "Fetch a URL and return extracted text.",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer"},
            },
            "required": ["url"],
        },
        "fs_write": {
            "description": "Overwrite a file with content.",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        "fs_append": {
            "description": "Append content to a file.",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    }
    names = list(_READ_TOOLS) + (list(_WRITE_TOOLS) if write_allowed else [])
    return [
        {
            "name": n,
            "description": specs[n]["description"],
            "input_schema": {
                "type": "object",
                "properties": specs[n]["properties"],
                "required": specs[n].get("required", []),
            },
        }
        for n in names
    ]


class GenericExecutor(ExecutorCommon):
    """In-process, provider-agnostic agent loop over the local toolset."""

    def __init__(
        self,
        bus: Bus,
        schema_store=None,
        *,
        router=None,
        allowed_paths: list[str] | None = None,
        allowed_commands: set[str] | None = None,
    ) -> None:
        self._bus = bus
        self._schema_store = schema_store
        self._pending: dict | None = None
        self._dispatcher = ToolDispatcher(allowed_paths, allowed_commands)
        # The router resolves the configured cognition provider; motor_model lets
        # a deployment pin a cheaper/local model for actions specifically.
        self._router = router
        self._model_key = str(settings.get("motor_model") or "gpt")
        self._trusted_dirs: list[str] | None = None  # no Claude-Desktop inheritance

    # ── Surface required by motor_cortex / session_setup ──────────────────────

    @property
    def available(self) -> bool:
        """A local-model motor_model needs no cloud key; a cloud one needs the
        matching provider key. Be permissive — a failed call degrades to an
        [error] result, which motor already handles."""
        if self._model_key.startswith(("local", "runpod")):
            return True
        return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))

    def connectors_summary(self) -> str:
        return "No remote connectors (generic executor runs the brain's local toolset)."

    def set_allowed_paths(self, paths: list[str]) -> None:
        """session_setup configures motor paths after construction; rebuild the
        dispatcher so the executor and the in-brain motor share one allowlist."""
        self._dispatcher = ToolDispatcher(paths, self._dispatcher._allowed_commands)

    def attach_router(self, router) -> None:
        self._router = router

    # ── Public execution paths (CloudExecutor-compatible) ─────────────────────

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

    # ── Agent loop ────────────────────────────────────────────────────────────

    async def _run(
        self, task: str, context_facts: list[str], turn_id: str = "", write_allowed: bool = False
    ) -> dict:
        if self._router is None:
            return {
                "tool": "cloud_action",
                "output": "[error] Generic executor has no model router attached.",
                "success": False,
            }
        start = time.time()
        try:
            timeout = float(settings.get("cma_task_timeout_s") or 120.0)
            import asyncio as _asyncio

            raw = await _asyncio.wait_for(
                self._drive(task, context_facts, write_allowed, turn_id),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning("[GenericExecutor] task timed out after %.1fs", time.time() - start)
            raw = "[error] Action task timed out."
        except Exception as e:
            logger.error("[GenericExecutor] task failed: %s", e)
            raw = f"[error] {e}"

        output = self._screen_result(raw)
        success = not raw.startswith("[error]") and not output.startswith("[error]")
        logger.info(
            "[GenericExecutor] Completed in %.1fs (success=%s, %d chars, model=%s)",
            time.time() - start,
            success,
            len(output),
            self._model_key,
        )
        await self._append_tool_log(task, output, success)
        return {"tool": "cloud_action", "output": output, "success": success}

    async def _drive(
        self, task: str, context_facts: list[str], write_allowed: bool, turn_id: str
    ) -> str:
        facts = "; ".join(f.strip() for f in (context_facts or []) if f.strip())
        user = task if not facts else f"{task}\n\nContext: {facts}"
        system = _SYSTEM_GUIDANCE + "\n\n" + self._dispatcher.build_path_hint()
        specs = _tool_specs(write_allowed)
        max_steps = max(1, int(settings.get("ralph_max_total_attempts") or 12))

        transcript: list[str] = []
        for step in range(max_steps):
            call = await self._router.call_structured_any(
                self._model_key,
                system,
                [{"role": "user", "content": user}],
                tools=specs,
                cluster=CLUSTER,
                cell="action",
                turn_id=turn_id,
            )
            tool = (call or {}).get("tool", "")
            args = (call or {}).get("args", {}) or {}
            if not tool or tool == "__done__":
                # Model chose to finish (or returned nothing actionable).
                final = (call or {}).get("text", "") or (transcript[-1] if transcript else "(done)")
                return final
            result = await self._dispatch(tool, args, write_allowed)
            transcript.append(f"{tool} → {result[:200]}")
            # Feed the result back as the running context for the next step.
            user = (
                f"{task}\n\nProgress so far:\n"
                + "\n".join(transcript[-6:])
                + "\n\nContinue, or give your one-line summary if the task is done."
            )
        return transcript[-1] if transcript else "[error] No action taken within the step budget."

    async def _dispatch(self, tool: str, args: dict, write_allowed: bool) -> str:
        """Route one tool call to the local dispatcher. Write tools are refused
        unless the confirmation handshake authorized this run (defense in depth —
        they're also absent from the tool specs in a read run)."""
        import asyncio as _asyncio

        d = self._dispatcher
        loop = _asyncio.get_running_loop()
        try:
            if tool == "fs_read":
                return await loop.run_in_executor(None, d._read_file, args.get("path", ""))
            if tool == "fs_list":
                return await loop.run_in_executor(
                    None,
                    d._list_files,
                    args.get("path", ""),
                    args.get("pattern", "*"),
                    bool(args.get("recursive", False)),
                )
            if tool == "fs_search":
                return await loop.run_in_executor(
                    None,
                    d._search_files,
                    args.get("path", ""),
                    args.get("query", ""),
                    args.get("file_pattern", "*"),
                )
            if tool == "run_command":
                return await d._run_command(args.get("cmd", ""), args.get("cwd", ""))
            if tool == "fetch_url":
                return await d._fetch_url(args.get("url", ""), int(args.get("max_chars", 8000)))
            if tool in _WRITE_TOOLS:
                if not write_allowed:
                    return "[blocked] write tools require user confirmation first"
                if tool == "fs_write":
                    return await loop.run_in_executor(
                        None, d._write_file, args.get("path", ""), args.get("content", "")
                    )
                return await loop.run_in_executor(
                    None, d._append_file, args.get("path", ""), args.get("content", "")
                )
            return f"[error] unknown tool '{tool}'"
        except Exception as e:
            return f"[error] {tool} failed: {e}"
