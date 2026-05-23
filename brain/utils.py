"""Shared utilities for the brain package."""
from __future__ import annotations

import json
import re


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
