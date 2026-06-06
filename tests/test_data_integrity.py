"""Data integrity guard.

Catches any large tracked file that has been silently reduced to near-nothing
in the working copy — the most common symptom of an accidental destructive
operation (running a rebuild script with empty input, a bad `>` redirect, an
automated process clearing files it shouldn't touch, etc.).

Rule: if a tracked file was >= MIN_BYTES_IN_HEAD in the last commit, and the
working copy is now < SHRINK_THRESHOLD of that size, the test fails immediately
with the exact git command to restore it.

Exclusions:
- Binary files (images, lock files, compiled assets)
- eval/ directory: these are append-only logs that may legitimately be trimmed
- uv.lock / *.lock: managed by package tools, size fluctuates normally
- Files explicitly listed in EXCLUDED_PATHS

This test is intentionally broad — it protects the skill index, persona schemas,
sequence weights, DMN routing weights, trading journal, and anything else that
took computation or accumulated user data to produce.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# A file must be at least this large in HEAD to be watched.
MIN_BYTES_IN_HEAD = 1024  # 1 KB

# If the working copy is smaller than this fraction of the HEAD size, fail.
SHRINK_THRESHOLD = 0.10  # < 10% of original = suspect

# File extensions that are binary / not text — skip size comparison.
BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".mp3",
    ".wav",
    ".ogg",
    ".flac",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".lock",
    ".bin",
    ".pyc",
    ".so",
    ".dylib",
    ".lance",  # LanceDB binary format
}

# Paths (relative to repo root) to exclude entirely — these legitimately shrink.
EXCLUDED_PATHS = {
    "eval/turns.jsonl",  # append-only eval log, may be rotated
    "eval/turns_archive.jsonl",
}

# Directory prefixes to exclude.
EXCLUDED_DIRS = {
    "eval/",
    ".venv/",
    "__pycache__/",
}


def _repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        capture_output=True,
        text=True,
    )
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def _head_size(rel_path: str) -> int:
    """Byte size of the file at HEAD, or 0 if not in HEAD."""
    result = subprocess.run(
        ["git", "show", f"HEAD:{rel_path}"],
        capture_output=True,
    )
    if result.returncode != 0:
        return 0
    return len(result.stdout)


def test_no_large_file_silently_wiped():
    """No tracked file >= 1KB in HEAD should shrink to < 10% of its HEAD size.

    If this fails, a data file was likely accidentally cleared. Each failure
    line includes the exact git command to restore the file.
    """
    root = _repo_root()
    violations: list[str] = []

    for rel_path in _tracked_files():
        # Skip excluded dirs
        if any(rel_path.startswith(d) for d in EXCLUDED_DIRS):
            continue
        # Skip excluded paths
        if rel_path in EXCLUDED_PATHS:
            continue
        # Skip binary extensions
        if Path(rel_path).suffix.lower() in BINARY_EXTENSIONS:
            continue

        head_bytes = _head_size(rel_path)
        if head_bytes < MIN_BYTES_IN_HEAD:
            continue  # too small to be worth watching

        working_path = root / rel_path
        if not working_path.exists():
            # File deleted entirely — always a violation for large tracked files.
            violations.append(
                f"{rel_path}: DELETED in working copy (was {head_bytes:,} bytes in HEAD)\n"
                f"    restore: git checkout HEAD -- {rel_path}"
            )
            continue

        working_bytes = working_path.stat().st_size
        if working_bytes < head_bytes * SHRINK_THRESHOLD:
            violations.append(
                f"{rel_path}: {working_bytes:,} bytes (was {head_bytes:,} in HEAD, "
                f"{working_bytes / head_bytes:.0%} remaining)\n"
                f"    restore: git checkout HEAD -- {rel_path}"
            )

    assert not violations, (
        f"\n{len(violations)} file(s) appear to have been accidentally wiped "
        f"or dramatically reduced:\n\n" + "\n\n".join(f"  • {v}" for v in violations)
    )
