"""Filesystem and shell tool dispatch for the motor cortex."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_COMMANDS = {
    "ls", "find", "grep", "cat", "head", "tail", "wc",
    "npm", "npx", "node", "python", "python3", "uv",
    "git", "curl", "echo", "mkdir", "cp", "mv", "rm",
    "sed", "awk", "sort", "uniq", "diff",
}


class ToolDispatcher:
    """Validates and executes filesystem/shell tool calls on behalf of MotorCortexCluster."""

    def __init__(
        self,
        allowed_paths: list[str] | None = None,
        allowed_commands: set[str] | None = None,
    ) -> None:
        self._allowed_paths: list[str] = []
        for p in (allowed_paths or []):
            try:
                self._allowed_paths.append(str(Path(p).resolve()))
            except Exception:
                logger.warning("[ToolDispatcher] Ignoring invalid allowed path: %s", p)
        self._allowed_commands: set[str] = allowed_commands or set(DEFAULT_COMMANDS)

    # ── Path / command safety ──────────────────────────────────────────────────

    def _validate_path(self, path: str) -> tuple[bool, str]:
        """Returns (is_safe, resolved_path_or_error_message)."""
        if not self._allowed_paths:
            return False, "No paths configured. Set BRAIN_MOTOR_PATHS env var."
        if not path:
            return False, "Empty path."
        try:
            resolved = str(Path(path).resolve())
        except Exception as e:
            return False, f"Invalid path '{path}': {e}"
        for allowed in self._allowed_paths:
            if resolved == allowed or resolved.startswith(allowed + os.sep):
                return True, resolved
        return False, (f"Path '{path}' (resolved: {resolved}) is outside allowed roots: "
                       f"{self._allowed_paths}")

    def _validate_command(self, cmd: str) -> tuple[bool, str]:
        """Returns (is_safe, error_message_or_empty)."""
        try:
            parts = shlex.split(cmd)
        except ValueError as e:
            return False, f"Invalid command syntax: {e}"
        if not parts:
            return False, "Empty command."
        base = os.path.basename(parts[0])
        if base not in self._allowed_commands:
            return False, (f"Command '{base}' is not in the allowed list. "
                           f"Allowed: {sorted(self._allowed_commands)}")
        return True, ""

    # ── Tool implementations ───────────────────────────────────────────────────

    def _read_file(self, path: str) -> str:
        safe, resolved = self._validate_path(path)
        if not safe:
            return f"[blocked] {resolved}"
        try:
            content = Path(resolved).read_text(errors="replace")
            if len(content) > 4000:
                content = content[:4000] + "\n[... truncated at 4000 chars ...]"
            return content
        except FileNotFoundError:
            return f"[error] File not found: {resolved}"
        except PermissionError:
            return f"[error] Permission denied: {resolved}"

    def _write_file(self, path: str, content: str) -> str:
        safe, resolved = self._validate_path(path)
        if not safe:
            return f"[blocked] {resolved}"
        try:
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
            Path(resolved).write_text(content)
            return f"Written {len(content)} bytes to {resolved}"
        except PermissionError:
            return f"[error] Permission denied: {resolved}"

    def _append_file(self, path: str, content: str) -> str:
        safe, resolved = self._validate_path(path)
        if not safe:
            return f"[blocked] {resolved}"
        try:
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
            with open(resolved, "a") as f:
                f.write(content)
            return f"Appended {len(content)} bytes to {resolved}"
        except PermissionError:
            return f"[error] Permission denied: {resolved}"

    def _list_files(self, path: str, pattern: str = "*", recursive: bool = False) -> str:
        safe, resolved = self._validate_path(path)
        if not safe:
            return f"[blocked] {resolved}"
        try:
            p = Path(resolved)
            if not p.is_dir():
                return f"[error] Not a directory: {resolved}"
            matches = list(p.rglob(pattern)) if recursive else list(p.glob(pattern))
            if not matches:
                return "(no files matched)"
            lines = [str(m.relative_to(p)) for m in sorted(matches)[:200]]
            result = "\n".join(lines)
            if len(matches) > 200:
                result += f"\n[... {len(matches) - 200} more files not shown ...]"
            return result
        except PermissionError:
            return f"[error] Permission denied: {resolved}"

    async def _run_command(self, cmd: str, cwd: str = "") -> str:
        safe_cmd, err = self._validate_command(cmd)
        if not safe_cmd:
            return f"[blocked] {err}"

        cwd_resolved = None
        if cwd:
            safe_cwd, resolved_cwd = self._validate_path(cwd)
            if not safe_cwd:
                return f"[blocked] cwd: {resolved_cwd}"
            cwd_resolved = resolved_cwd
        elif self._allowed_paths:
            cwd_resolved = self._allowed_paths[0]

        try:
            parts = shlex.split(cmd)
            proc = await asyncio.create_subprocess_exec(
                *parts,
                cwd=cwd_resolved,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=os.environ.copy(),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            output = stdout.decode(errors="replace")
            if len(output) > 3000:
                output = output[:3000] + "\n[... truncated ...]"
            return output or "(command produced no output)"
        except TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            return "[error] Command timed out after 30s."
        except FileNotFoundError:
            return f"[error] Command not found: {shlex.split(cmd)[0]}"
        except Exception as e:
            return f"[error] {e}"

    def _search_files(self, path: str, query: str, file_pattern: str = "*") -> str:
        safe, resolved = self._validate_path(path)
        if not safe:
            return f"[blocked] {resolved}"
        if not query:
            return "[error] Empty search query."
        try:
            p = Path(resolved)
            matches: list[str] = []
            for fpath in p.rglob(file_pattern):
                if not fpath.is_file():
                    continue
                try:
                    text = fpath.read_text(errors="replace")
                    for i, line in enumerate(text.splitlines(), 1):
                        if query.lower() in line.lower():
                            rel = fpath.relative_to(p)
                            matches.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(matches) >= 100:
                                break
                except Exception:
                    continue
                if len(matches) >= 100:
                    break
            if not matches:
                return f"(no matches for '{query}' in {resolved})"
            result = "\n".join(matches)
            if len(matches) == 100:
                result += "\n[... search limited to 100 results ...]"
            return result
        except PermissionError:
            return f"[error] Permission denied: {resolved}"

    # ── Path management ────────────────────────────────────────────────────────

    def build_path_hint(self) -> str:
        if not self._allowed_paths:
            return "Filesystem access: none configured (BRAIN_MOTOR_PATHS unset)."
        primary = self._allowed_paths[0]
        roots = "\n  ".join(self._allowed_paths)
        return (
            f"Filesystem access:\n"
            f"  Working directory (CWD): {primary}\n"
            f"  Allowed roots:\n  {roots}\n"
            f"Always use paths relative to CWD (e.g. 'second_brain/schema/self.md') "
            f"or absolute paths under the allowed roots. Never guess paths outside these roots."
        )

    @property
    def allowed_paths(self) -> list[str]:
        return list(self._allowed_paths)

    def add_allowed_path(self, path: str) -> None:
        try:
            resolved = str(Path(path).resolve())
            if resolved not in self._allowed_paths:
                self._allowed_paths.append(resolved)
                logger.info("[ToolDispatcher] Added allowed path: %s", resolved)
        except Exception as e:
            logger.warning("[ToolDispatcher] Could not add path %s: %s", path, e)
