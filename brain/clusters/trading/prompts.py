"""System prompts for the LLM-driven trading features.

GOVERNANCE (non-negotiable): these ship EMPTY. No prompt content is authored
automatically and nothing is copied from any reviewed repo. Each LLM-driven
feature (reflection, the stress-test roles, mispricing) is INERT — it returns
``[blocked] prompt not configured`` — until Russ writes/approves the prompt text
here. Only Russ's own data is ever fed into these prompts at call time.

To activate a feature, fill in its constant below with your own instructions.
The comment above each constant describes what that prompt is for; it is NOT
prompt content and is never sent to the model.
"""

from __future__ import annotations

# Reflection: given the original prediction, rationale, indicators-at-open, and
# the actual outcome (return, alpha, threshold hit), produce a concise 2–4
# sentence lesson focused on what the reasoning missed + a concrete adjustment.
REFLECTION_SYSTEM: str = ""

# Bull researcher: argue the strongest upside case for the thesis.
BULL_SYSTEM: str = ""

# Bear researcher: argue what breaks the thesis — the edge case the signal hides.
BEAR_SYSTEM: str = ""

# Risk manager: position sizing, concentration, drift from stated strategy.
RISK_SYSTEM: str = ""

# Portfolio-manager synthesis: weigh bull/bear/risk → a 5-tier rating
# (Buy/Overweight/Hold/Underweight/Sell), the single biggest thing that breaks
# the thesis, and whether a hedge exists.
SYNTHESIS_SYSTEM: str = ""

# Mispricing: contrast what the data suggests vs market sentiment/consensus;
# name the divergence and what would close the gap.
MISPRICING_SYSTEM: str = ""

# ── Memory condensation (data management, not trading analysis) ──────────────
#
# CONDENSATION_SYSTEM is the exception to the "ships empty" rule: it's purely
# about how the brain compresses its own internal records (like a memory
# consolidation pass), not about trading judgment. A working default is provided
# so pruning is never blocked. You can edit the text to change tone/focus.
#
# Called with a JSON payload of resolved trade records. Expected output: a
# condensed paragraph (2-5 sentences) capturing transferable pattern-level
# insights — which conditions reliably predicted outcomes and what reasoning
# errors recurred. Written in first person as durable memory.
CONDENSATION_SYSTEM: str = """\
You are consolidating older trading journal entries into durable long-term memory. \
You will receive a JSON list of resolved trade predictions, each with a symbol, \
direction, rationale, indicators at the time, outcome metrics, and any lesson recorded. \
Write a concise 2-5 sentence summary capturing only the transferable insights: \
which indicator conditions or market regimes reliably preceded wins or misses, \
and what reasoning errors recurred across these entries. \
Be specific about thresholds and conditions — not just "the trade didn't work." \
Write in first person as a memory entry for future use. \
Output only the summary text, nothing else.\
"""

BLOCKED_MSG = "[blocked] prompt not configured"


def is_configured(prompt: str) -> bool:
    """A prompt is active only once Russ has authored non-empty text for it."""
    return bool(prompt and prompt.strip())
