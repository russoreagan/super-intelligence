"""
approach_schema — the structural half of the motor boundary.

An approach candidate expresses STRATEGY (does this turn need the outside world,
what does a good answer look like, what angle of attack) and must be structurally
unable to express STEPS (which tool, what args, in what order). Motor cortex keeps
detailed planning; what it must never receive from this stage is a step list.

Three enforcement layers (the plan's motor-boundary section); this module is the
first two:
  1. STRUCTURAL — sanitize_approach() is a key whitelist. There is no tool/args/
     steps field for a plan to survive in; a helpful {"tool": "run_command"} loses
     the key at parse.
  2. LEXICAL — whole-word scrub of live tool names (passed in from the registry —
     never a hardcoded list, so it can't drift when a tool is added) from the
     candidate's own generated free text; decomposition items must be QUESTIONS
     (end in "?"), which makes "1. read the file 2. grep for X" grammatically
     unrepresentable; external_kind rejects URLs, backticks, absolute paths.
  3. POSITIONAL — lives in motor_cortex._build_plan_prompt: the approach is
     rendered as read-only prose; no motor code branches on it.

The scrub applies to MODEL-GENERATED approach text only. Library skill bodies are
pre-vetted content in a known trust class and reach the planner intact under their
existing native/partner framing.
"""

from __future__ import annotations

import re

APPROACH_KEYS: frozenset[str] = frozenset(
    {
        "stance",
        "information_need",
        "external_kind",
        "success_criteria",
        "framing",
        "decomposition",
        "risk",
        "confidence",
    }
)

INFORMATION_NEEDS: tuple[str, ...] = ("none", "internal", "external", "both")

_URL_RE = re.compile(r"https?://", re.IGNORECASE)

_LIMITS = {"stance": 200, "external_kind": 80, "framing": 120, "risk": 120}


def _scrub_tools(text: str, tool_names: list[str]) -> str:
    """Replace whole-word occurrences of live tool names with [tool]."""
    out = text
    for name in tool_names:
        if not name:
            continue
        out = re.sub(rf"\b{re.escape(name)}\b", "[tool]", out, flags=re.IGNORECASE)
    return out


def sanitize_approach(raw: dict | None, tool_names: list[str]) -> dict | None:
    """Whitelist + scrub a generator's candidate. Returns None when the candidate
    is disqualified (no usable stance survives). Never raises on malformed input."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    for key in APPROACH_KEYS:
        if key not in raw:
            continue
        val = raw[key]
        if key == "information_need":
            v = str(val or "").strip().lower()
            if v in INFORMATION_NEEDS:
                out[key] = v
        elif key == "confidence":
            try:
                out[key] = max(0.0, min(1.0, float(val)))
            except (TypeError, ValueError):
                continue
        elif key == "success_criteria":
            items = [
                _scrub_tools(str(x), tool_names).strip()
                for x in (val if isinstance(val, list) else [])
            ]
            out[key] = [x for x in items if x][:3]
        elif key == "decomposition":
            # Interrogative-only: a decomposition into QUESTIONS is strategy; a
            # decomposition into actions is planning, and it is dropped here.
            items = [
                _scrub_tools(str(x), tool_names).strip()
                for x in (val if isinstance(val, list) else [])
            ]
            out[key] = [x for x in items if x.endswith("?")][:4]
        elif key == "external_kind":
            v = _scrub_tools(str(val or ""), tool_names).strip()
            if _URL_RE.search(v) or "`" in v or v.startswith("/"):
                continue
            out[key] = v[: _LIMITS[key]]
        else:  # stance / framing / risk — bounded scrubbed prose
            v = _scrub_tools(str(val or ""), tool_names).strip()
            out[key] = v[: _LIMITS[key]]
    if not out.get("stance"):
        return None
    out.setdefault("information_need", "none")
    out.setdefault("confidence", 0.5)
    return out


def wants_action(approach: dict) -> bool:
    """The information_need → requires_action mapping: a lookup, not a judgement.
    `internal` needs information but no tool — the case that most often produces a
    wrong requires_action."""
    return approach.get("information_need") in ("external", "both")
