"""
CloudExecutor — calls Claude CLI as a subprocess for cloud-connected actions.
Handles Gmail, Calendar, iMessages, Drive, and any other MCP connector
the user has enabled in their Claude account.

Three guardrails are always active:
  1. Minimal context  — only operational facts reach Claude, never memory dumps
  2. Result fencing   — all output is screened through security.py before
                        returning to the brain
  3. Confirmation gate — write/destructive actions require explicit user sign-off
                         before execution; read actions execute immediately

Writes an audit trail to second_brain/schema/tool_log.md after every call.
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from brain.bus import Bus
from brain.security import screen_input, fence

logger = logging.getLogger(__name__)

CLUSTER = "cloud_executor"

# Extension ID → human-readable connector name (for planner context string)
_EXTENSION_NAMES: dict[str, str] = {
    "ant.dir.ant.anthropic.imessage":        "iMessages",
    "ant.dir.ant.anthropic.chrome-control":  "Chrome browser",
    "ant.dir.gh.elevenlabs.agents-mcp-app":  "ElevenLabs",
    "ant.dir.gh.ableton.ableton-knowledge":  "Ableton",
    "ant.dir.gh.adobe.react-aria":           "Adobe/React Aria",
    "ant.dir.gh.elevenlabs.elevenlabs-player": "ElevenLabs Player",
}

# Words that indicate user confirmation of a pending write action
_CONFIRM_WORDS = frozenset([
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "go ahead",
    "do it", "confirm", "proceed", "send it", "go for it", "affirmative",
])

_DENY_WORDS = frozenset([
    "no", "nope", "cancel", "stop", "don't", "abort", "never mind",
    "nevermind", "skip", "forget it", "hold on",
])

SUBPROCESS_TIMEOUT = 120  # seconds — cloud ops can be slow


class CloudExecutor:
    def __init__(self, bus: Bus, schema_store=None) -> None:
        self._bus = bus
        self._schema = schema_store
        self._claude_bin = self._find_claude_binary()
        self._connectors = self._discover_connectors()
        self._trusted_dirs = self._discover_trusted_dirs()
        self._pending: dict | None = None  # write action awaiting confirmation

        if self._claude_bin:
            logger.info("[CloudExecutor] Claude binary: %s", self._claude_bin)
            if self._connectors:
                logger.info("[CloudExecutor] Available connectors: %s",
                            ", ".join(self._connectors.values()))
            else:
                logger.info("[CloudExecutor] No MCP extensions detected — Claude will use its base capabilities")
            if self._trusted_dirs:
                logger.info("[CloudExecutor] Trusted project dirs: %s",
                            ", ".join(self._trusted_dirs))
        else:
            logger.warning(
                "[CloudExecutor] Could not find Claude CLI binary. "
                "Cloud actions will be unavailable until Claude Code is installed."
            )

    # ── Binary + connector discovery ───────────────────────────────────────────

    def _find_claude_binary(self) -> str | None:
        """Find the newest installed Claude Code CLI binary."""
        pattern = os.path.expanduser(
            "~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude"
        )
        candidates = glob.glob(pattern)
        if not candidates:
            return None
        # Sort by version string — newest last (semver-ish comparison)
        return sorted(candidates)[-1]

    def _discover_connectors(self) -> dict[str, str]:
        """Return {extension_id: display_name} for enabled extensions."""
        settings_dir = Path(
            os.path.expanduser("~/Library/Application Support/Claude/Claude Extensions Settings")
        )
        if not settings_dir.exists():
            return {}
        enabled: dict[str, str] = {}
        for json_file in settings_dir.glob("*.json"):
            ext_id = json_file.stem  # filename without .json
            try:
                data = json.loads(json_file.read_text())
                if data.get("isEnabled"):
                    display = _EXTENSION_NAMES.get(ext_id, ext_id.split(".")[-1])
                    enabled[ext_id] = display
            except Exception:
                continue
        return enabled

    def _discover_trusted_dirs(self) -> list[str]:
        """Read project directories the user has granted Claude access to.

        Reads localAgentModeTrustedFolders from the Claude Desktop config —
        that's where the user configures which of their projects Claude Code
        can read and write.
        """
        config_path = Path(os.path.expanduser(
            "~/Library/Application Support/Claude/claude_desktop_config.json"
        ))
        try:
            data = json.loads(config_path.read_text())
            dirs = data.get("preferences", {}).get("localAgentModeTrustedFolders", [])
            return [str(Path(d).resolve()) for d in dirs if d]
        except Exception:
            return []

    def connectors_summary(self) -> str:
        """Human-readable list of active connectors and accessible project dirs."""
        parts = []
        if self._connectors:
            parts.append(", ".join(sorted(self._connectors.values())))
        if self._trusted_dirs:
            names = [Path(d).name for d in self._trusted_dirs]
            parts.append("projects: " + ", ".join(names))
        return "; ".join(parts) if parts else "no MCP extensions enabled"

    # ── Pending confirmation state ─────────────────────────────────────────────

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    def set_pending(self, action: dict) -> None:
        self._pending = action

    def clear_pending(self) -> None:
        self._pending = None

    def get_pending(self) -> dict | None:
        return self._pending

    def is_user_confirming(self, text: str) -> bool:
        t = text.strip().lower()
        return any(w in t for w in _CONFIRM_WORDS)

    def is_user_denying(self, text: str) -> bool:
        t = text.strip().lower()
        return any(w in t for w in _DENY_WORDS)

    # ── Main execution paths ───────────────────────────────────────────────────

    async def execute_read(self, task: str, context_facts: list[str],
                           turn_id: str = "") -> dict:
        """Execute immediately — read-only, no confirmation needed."""
        return await self._run(task, context_facts, turn_id=turn_id)

    async def execute_pending(self, turn_id: str = "") -> dict | None:
        """Execute the stored write action after user has confirmed."""
        if not self._pending:
            return None
        action = self._pending
        self._pending = None
        return await self._run(action["task"], action.get("context_facts", []),
                               turn_id=turn_id)

    # ── Subprocess call ────────────────────────────────────────────────────────

    async def _run(self, task: str, context_facts: list[str],
                   turn_id: str = "") -> dict:
        if not self._claude_bin:
            return {"tool": "cloud_action", "output": "[error] Claude CLI not found.",
                    "success": False}

        prompt = self._build_prompt(task, context_facts)
        start = time.time()

        # Build --add-dir flags for every project directory the user has
        # granted Claude access to in Claude Desktop settings.
        add_dir_args: list[str] = []
        for d in self._trusted_dirs:
            add_dir_args.extend(["--add-dir", d])

        try:
            proc = await asyncio.create_subprocess_exec(
                self._claude_bin,
                "--print",          # non-interactive, single-turn
                "--output-format", "text",
                "--allowedTools", "WebSearch,WebFetch,Bash,Read,Write,Edit,LS",
                *add_dir_args,
                "--",               # separator so prompt isn't parsed as a flag
                prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=SUBPROCESS_TIMEOUT
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            elapsed = time.time() - start
            logger.warning("[CloudExecutor] Subprocess timed out after %.1fs", elapsed)
            return {"tool": "cloud_action", "output": "[error] Claude subprocess timed out.",
                    "success": False}
        except Exception as e:
            logger.error("[CloudExecutor] Subprocess failed: %s", e)
            return {"tool": "cloud_action", "output": f"[error] {e}", "success": False}

        elapsed = time.time() - start
        raw = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()

        if not raw and err:
            raw = f"[stderr] {err[:500]}"

        # Guardrail 2: screen + fence the result before it enters the brain
        output = self._screen_result(raw)

        success = not output.startswith("[error]") and proc.returncode == 0
        logger.info("[CloudExecutor] Completed in %.1fs (success=%s, %d chars)",
                    elapsed, success, len(output))

        await self._append_tool_log(task, output, success)
        return {"tool": "cloud_action", "output": output, "success": success}

    # ── Prompt construction (minimal context) ─────────────────────────────────

    def _build_prompt(self, task: str, context_facts: list[str]) -> str:
        """
        Guardrail 1: Minimal context — only the task and essential operational
        facts go to Claude. No memory dumps, no schema content, no episode history.
        """
        parts = [task]
        if context_facts:
            facts_str = "; ".join(f.strip() for f in context_facts if f.strip())
            if facts_str:
                parts.append(f"Context: {facts_str}")
        return "\n".join(parts)

    # ── Result security screening ──────────────────────────────────────────────

    def _screen_result(self, raw: str) -> str:
        """
        Guardrail 2: Treat Claude's output as untrusted (it may include email
        contents or web data that contains adversarial text). Screen and fence.
        """
        if not raw:
            return "(no output)"

        result = screen_input(raw)
        if result.flagged:
            logger.warning(
                "[CloudExecutor] Output failed injection screen (reason=%s) — "
                "returning sanitised placeholder instead of raw output",
                result.reason,
            )
            return "[output blocked: potential injection pattern detected in tool result]"

        # Wrap in fence tag so downstream cells treat it as data, not instructions
        return fence("cloud_result", raw[:4000])

    # ── Audit trail ───────────────────────────────────────────────────────────

    async def _append_tool_log(self, task: str, output: str, success: bool) -> None:
        """Append one entry to second_brain/schema/tool_log.md."""
        log_path = Path("second_brain/schema/tool_log.md")
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            status = "✓" if success else "✗"
            # Truncate output for the log — keep it readable
            preview = output[:200].replace("\n", " ").strip()
            if len(output) > 200:
                preview += "..."
            entry = f"\n## {ts} {status}\n**Task:** {task}\n**Result:** {preview}\n"
            async with asyncio.Lock():
                with open(log_path, "a") as f:
                    f.write(entry)
        except Exception as e:
            logger.debug("[CloudExecutor] Could not write tool log: %s", e)

    @property
    def available(self) -> bool:
        return self._claude_bin is not None
