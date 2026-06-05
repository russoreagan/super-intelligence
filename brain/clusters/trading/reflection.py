"""Reflection loop — turn a resolved prediction into a lesson, then persist it.

Mirrors the TradingAgents reflection idea (compute the outcome, then have an LLM
write a short lesson that gets re-injected into future analysis) but uses only
Russ's own data and a prompt Russ authored in prompts.py.

INERT until REFLECTION_SYSTEM is configured: if the prompt is empty, the lesson
is left blank and the decision is still resolved (the numbers are recorded; only
the LLM commentary is skipped).
"""

from __future__ import annotations

import json
import logging

from . import journal, prompts

logger = logging.getLogger(__name__)

_LESSON_SCHEMA = {
    "type": "object",
    "required": ["lesson"],
    "properties": {
        "lesson": {"type": "string", "description": "2-4 sentence lesson"},
        "missed_signal": {"type": "string", "description": "the single signal most missed"},
    },
}


async def reflect_and_resolve(
    decision_id: str,
    *,
    price_at_resolve: float,
    benchmark_at_resolve: float | None = None,
    router=None,
    model_key: str = "local",
    note: str = "",
    hippocampus=None,
    on_conclude_thread=None,
) -> dict:
    """Resolve a decision, generating an LLM lesson if the prompt is configured."""
    records = journal.get_records()
    target = next((r for r in records if r.get("id") == decision_id), None)
    if target is None:
        return {"error": f"decision not found: {decision_id}"}

    metrics = journal.compute_metrics(target, price_at_resolve, benchmark_at_resolve)

    lesson = ""
    missed = ""
    if router is not None and prompts.is_configured(prompts.REFLECTION_SYSTEM):
        try:
            payload = {
                "prediction": target.get("prediction"),
                "rationale": target.get("rationale"),
                "indicators_at_open": target.get("indicators_at_open"),
                "direction": target.get("direction"),
                "outcome": metrics,
            }
            result = await router.call_structured(
                model_key=model_key,
                system_prompt=prompts.REFLECTION_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
                tool_name="record_lesson",
                tool_description="Record the lesson learned from this trade outcome.",
                tool_schema=_LESSON_SCHEMA,
                cluster="trading",
                cell="reflection",
            )
            lesson = (result or {}).get("lesson", "")
            missed = (result or {}).get("missed_signal", "")
        except Exception as e:  # pragma: no cover - model path
            logger.warning("[reflection] lesson generation failed: %s", e)

    return journal.resolve_decision(
        decision_id,
        price_at_resolve=price_at_resolve,
        benchmark_at_resolve=benchmark_at_resolve,
        lesson=lesson,
        missed_signal=missed,
        note=note,
        hippocampus=hippocampus,
        on_conclude_thread=on_conclude_thread,
    )
