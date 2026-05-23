"""Shared utilities for the brain package."""
from __future__ import annotations

import json
import re
import subprocess


def get_idle_seconds() -> float:
    """Return seconds since the last keyboard/mouse activity on this machine.

    Uses the macOS IOKit HIDIdleTime counter (nanosecond resolution).
    Returns 0.0 on any error or non-macOS platform so callers always
    get a safe value that won't suppress proactive speech by accident.
    """
    try:
        out = subprocess.check_output(
            ["ioreg", "-c", "IOHIDSystem"],
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        ).decode()
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        if m:
            return int(m.group(1)) / 1_000_000_000  # nanoseconds → seconds
    except Exception:
        pass
    return 0.0


def safe_json_parse(raw: str) -> dict | None:
    """Try to parse JSON from an LLM response, with regex fallback for wrapped output."""
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return None
