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
import contextlib
import glob
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from brain.bus import Bus
from brain.security import fence, screen_input

logger = logging.getLogger(__name__)

CLUSTER = "cloud_executor"

# Paths for dynamic connector discovery
_CLAUDE_SUPPORT = Path(os.path.expanduser("~/Library/Application Support/Claude"))
_EXTENSIONS_SETTINGS_DIR = _CLAUDE_SUPPORT / "Claude Extensions Settings"
_EXTENSIONS_INSTALLS_FILE = _CLAUDE_SUPPORT / "extensions-installations.json"
_EXTENSIONS_DIR = _CLAUDE_SUPPORT / "Claude Extensions"

# Words that indicate user confirmation of a pending write action
_CONFIRM_WORDS = frozenset(
    [
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "ok",
        "okay",
        "go ahead",
        "do it",
        "confirm",
        "proceed",
        "send it",
        "go for it",
        "affirmative",
    ]
)

_DENY_WORDS = frozenset(
    [
        "no",
        "nope",
        "cancel",
        "stop",
        "don't",
        "abort",
        "never mind",
        "nevermind",
        "skip",
        "forget it",
        "hold on",
    ]
)

SUBPROCESS_TIMEOUT = 120  # seconds — cloud ops can be slow

RESEARCH_DIR = Path("second_brain/research")
_RESEARCH_MAX_AGE_DAYS = 2


class CloudExecutor:
    def __init__(self, bus: Bus, schema_store=None) -> None:
        self._bus = bus
        self._schema = schema_store
        self._claude_bin = self._find_claude_binary()
        self._connectors = self._discover_connectors()
        self._trusted_dirs = self._discover_trusted_dirs()
        self._pending: dict | None = None  # write action awaiting confirmation

        self._ensure_research_dir()

        if self._claude_bin:
            logger.info("[CloudExecutor] Claude binary: %s", self._claude_bin)
            if self._connectors:
                logger.info(
                    "[CloudExecutor] Available connectors: %s", ", ".join(self._connectors.values())
                )
            else:
                logger.info(
                    "[CloudExecutor] No MCP extensions detected — Claude will use its base capabilities"
                )
            if self._trusted_dirs:
                logger.info(
                    "[CloudExecutor] Trusted project dirs: %s", ", ".join(self._trusted_dirs)
                )
        else:
            logger.warning(
                "[CloudExecutor] Could not find Claude CLI binary. "
                "Cloud actions will be unavailable until Claude Code is installed."
            )

    # ── Research directory ─────────────────────────────────────────────────────

    def _ensure_research_dir(self) -> None:
        """Create research dir and sweep files older than _RESEARCH_MAX_AGE_DAYS."""
        RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - _RESEARCH_MAX_AGE_DAYS * 86400
        for f in RESEARCH_DIR.glob("*.md"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass

    # ── Binary + connector discovery ───────────────────────────────────────────

    def _find_claude_binary(self) -> str | None:
        """Find the Claude Code CLI binary, trying all known locations.

        Searches multiple patterns so it survives app updates, path structure
        changes, and the macOS app-bundle vs bare-binary layouts. Returns the
        newest match across all patterns, or None if Claude isn't installed.
        """
        import shutil

        # Fastest path: on $PATH (e.g. symlinked by the installer)
        if shutil.which("claude"):
            return shutil.which("claude")

        # All known layouts under Application Support, newest version last
        patterns = [
            # macOS app bundle (current default)
            "~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude",
            # Bare binary (VM / alternate install)
            "~/Library/Application Support/Claude/claude-code-vm/*/claude",
            # Future-proofing: bare binary alongside the app bundle
            "~/Library/Application Support/Claude/claude-code/*/claude",
        ]
        candidates = []
        for pattern in patterns:
            candidates.extend(glob.glob(os.path.expanduser(pattern)))

        if not candidates:
            return None
        # Sort so the highest version number wins across all layouts
        return sorted(candidates)[-1]

    def _discover_connectors(self) -> dict[str, str]:
        """Return {extension_id: display_name} for all enabled extensions.

        Display names are read from extensions-installations.json (the richest
        source — has display_name from each extension's manifest) with a fallback
        to the individual manifest.json in the extension directory. No hard-coded
        list — any extension added to Claude is automatically picked up.
        """
        if not _EXTENSIONS_SETTINGS_DIR.exists():
            return {}

        # Build display-name index from the installations manifest.
        # Structure: {"extensions": {"<id>": {"manifest": {"display_name": "..."}}}}
        install_names: dict[str, str] = {}
        try:
            data = json.loads(_EXTENSIONS_INSTALLS_FILE.read_text())
            for ext_id, ext_data in data.get("extensions", {}).items():
                manifest = ext_data.get("manifest", {})
                name = manifest.get("display_name") or manifest.get("name")
                if name:
                    install_names[ext_id] = name
        except Exception:
            pass

        enabled: dict[str, str] = {}
        for json_file in _EXTENSIONS_SETTINGS_DIR.glob("*.json"):
            ext_id = json_file.stem
            try:
                if not json.loads(json_file.read_text()).get("isEnabled"):
                    continue
            except Exception:
                continue

            # Priority: installations manifest > individual manifest.json > ID tail
            if ext_id in install_names:
                display = install_names[ext_id]
            else:
                display = None
                manifest_path = _EXTENSIONS_DIR / ext_id / "manifest.json"
                if manifest_path.exists():
                    try:
                        m = json.loads(manifest_path.read_text())
                        display = m.get("display_name") or m.get("name")
                    except Exception:
                        pass
                if not display:
                    # Fall back to the last human-readable component of the ID
                    parts = [p for p in ext_id.split(".") if not p.startswith("ant")]
                    display = parts[-1] if parts else ext_id

            enabled[ext_id] = display

        # Also pick up MCP servers added via the Claude CLI (~/.claude.json).
        # Recursively scan so any nesting structure (top-level, per-project, future layouts)
        # is caught automatically without needing to know the schema in advance.
        cli_config = Path(os.path.expanduser("~/.claude.json"))
        try:
            cli_data = json.loads(cli_config.read_text())

            def _collect_mcp_servers(obj: object) -> None:
                if not isinstance(obj, dict):
                    return
                for name in obj.get("mcpServers", {}):
                    if name not in enabled:
                        enabled[name] = name
                for v in obj.values():
                    _collect_mcp_servers(v)

            _collect_mcp_servers(cli_data)
        except Exception:
            pass

        return enabled

    def _mcp_allow_patterns(self) -> list[str]:
        """Server-level --allowedTools grants for connected MCP servers.

        Returns ["mcp__<server>", ...] — each covers all of that server's tools.
        A server can surface under two naming conventions, so we grant both:
          - CLI-config name (e.g. "scite" in ~/.claude.json -> mcp__scite__*)
          - Claude-connected form (mcp__claude_ai_<Name>__*)
        Read access by intent; writes are gated upstream by the motor cortex's
        is_write confirmation path before cloud_action ever dispatches.
        """
        patterns: set[str] = set()
        try:
            data = json.loads(Path(os.path.expanduser("~/.claude.json")).read_text())

            def _collect(obj: object) -> None:
                if not isinstance(obj, dict):
                    return
                for name in obj.get("mcpServers", {}):
                    patterns.add(f"mcp__{name}")
                    patterns.add(f"mcp__claude_ai_{name[:1].upper()}{name[1:]}")
                for v in obj.values():
                    _collect(v)

            _collect(data)
        except Exception:
            pass
        return sorted(patterns)

    def _discover_trusted_dirs(self) -> list[str]:
        """Read project directories the user has granted Claude access to.

        Reads localAgentModeTrustedFolders from the Claude Desktop config —
        that's where the user configures which of their projects Claude Code
        can read and write.
        """
        config_path = Path(
            os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")
        )
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

    async def execute_read(self, task: str, context_facts: list[str], turn_id: str = "") -> dict:
        """Execute immediately — read-only, no confirmation needed."""
        return await self._run(task, context_facts, turn_id=turn_id)

    async def execute_pending(self, turn_id: str = "") -> dict | None:
        """Execute the stored write action after user has confirmed."""
        if not self._pending:
            return None
        action = self._pending
        self._pending = None
        return await self._run(action["task"], action.get("context_facts", []), turn_id=turn_id)

    # ── Subprocess call ────────────────────────────────────────────────────────

    async def _run(self, task: str, context_facts: list[str], turn_id: str = "") -> dict:
        if not self._claude_bin:
            return {
                "tool": "cloud_action",
                "output": "[error] Claude CLI not found.",
                "success": False,
            }

        prompt = self._build_prompt(task, context_facts)
        start = time.time()

        # Build --add-dir flags for every project directory the user has
        # granted Claude access to in Claude Desktop settings.
        add_dir_args: list[str] = []
        for d in self._trusted_dirs:
            add_dir_args.extend(["--add-dir", d])

        # Build the allowed-tools list. Standard built-ins, plus an explicit
        # server-level grant for each connected MCP server so the entity has
        # read access to its tools (scite, etc.) without an interactive prompt
        # it can't answer headlessly.
        #
        # NOTE: the old ",mcp__*" was a no-op — --allowedTools does NOT treat
        # "mcp__*" as a wildcard, so MCP tool calls sat pending approval forever.
        # We grant each server at "mcp__<server>" level (covers all its tools).
        # This is READ access by intent; write actions are independently gated
        # upstream by the motor cortex's is_write confirmation path before any
        # cloud_action dispatches here.
        allowed_tools = "WebSearch,WebFetch,Bash,Read,Write,Edit,LS"
        mcp_grants = self._mcp_allow_patterns()
        if mcp_grants:
            allowed_tools += "," + ",".join(mcp_grants)

        try:
            proc = await asyncio.create_subprocess_exec(
                self._claude_bin,
                "--print",  # non-interactive, single-turn
                "--output-format",
                "text",
                "--allowedTools",
                allowed_tools,
                *add_dir_args,
                "--",  # separator so prompt isn't parsed as a flag
                prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SUBPROCESS_TIMEOUT)
        except TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            elapsed = time.time() - start
            logger.warning("[CloudExecutor] Subprocess timed out after %.1fs", elapsed)
            return {
                "tool": "cloud_action",
                "output": "[error] Claude subprocess timed out.",
                "success": False,
            }
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
        logger.info(
            "[CloudExecutor] Completed in %.1fs (success=%s, %d chars)",
            elapsed,
            success,
            len(output),
        )

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
        research_dir = RESEARCH_DIR.resolve()
        parts.append(
            f"If your response will be lengthy (more than ~400 words), write the full "
            f"findings to {research_dir}/<YYYYMMDD-HHmmss>-result.md (use actual "
            f"timestamp) and return only a concise summary with the file path."
        )
        parts.append(
            "When reading files: use the Read tool to get the text content. "
            "If the file is HTML, strip all markup and work only with the readable text. "
            "Never return raw file contents — always respond with your own understanding or summary of what the file contains. "
            "Never use Bash to open files in a browser or app (no `open` command)."
        )
        return "\n".join(parts)

    # ── Result security screening ──────────────────────────────────────────────

    def _screen_result(self, raw: str) -> str:
        """
        Guardrail 2: Treat Claude's output as untrusted (it may include email
        contents or web data that contains adversarial text). Screen and fence.
        """
        if not raw:
            return "(no output)"

        # Truncate first so the injection screen sees the same content the
        # downstream cells will see — avoids blocking legitimately long output.
        truncated = raw[:8000]
        result = screen_input(truncated)
        if result.flagged:
            logger.warning(
                "[CloudExecutor] Output failed injection screen (reason=%s) — "
                "returning sanitised placeholder instead of raw output",
                result.reason,
            )
            return "[output blocked: potential injection pattern detected in tool result]"

        # Wrap in fence tag so downstream cells treat it as data, not instructions
        return fence("cloud_result", truncated)

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
