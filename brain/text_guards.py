"""Shared guards for raw model output that must never reach a user or a partner.

These live in one place so every surface that speaks or forwards text — the UI
emitter, the partner webhook, the drafter follow-through, the background-job
reporter — applies the *same* check and can't drift apart over time.
"""

from __future__ import annotations

import re

__all__ = ["looks_like_json_blob"]


def looks_like_json_blob(text: str | None) -> bool:
    """True when ``text`` is a raw JSON object/array rather than spoken prose.

    Background-job cells (the result reporter, the planner) run on the local model
    and are instructed to emit plain spoken text. A degenerate local model ignores
    that and echoes the tool transcript or confabulates a response schema — e.g.
    ``{"has_signal": ...}`` or ``{"speech": ...}`` — which must never be spoken to a
    user or forwarded to a partner webhook. Spoken summaries never start with a
    brace/bracket, so treat a leading ``{``/``[`` (after stripping any code fence) as
    a non-answer.
    """
    if not text:
        return False
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    return t.startswith("{") or t.startswith("[")
