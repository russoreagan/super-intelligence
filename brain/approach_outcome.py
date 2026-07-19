"""
approach_outcome — grounded, per-dimension verification of a committed approach.

The approach critic's selection is self-graded preference; ground truth arrives a
turn late: did the tool actually deliver, did the user immediately ask again, ask
for clarification, ask for more, sound frustrated AT THE ASSISTANT. Each signal
indicts a DIFFERENT part of the approach, so verdicts are per-dimension — that
attribution is what makes the signal learnable rather than one undifferentiated
blob.

Modeled on avoidance_gate.observe_turn (grade a prior turn's belief against
next-turn behavior) and _verify_world_prediction (stash a claim, verify at the
start of the next turn).

Signal reliability is NOT uniform, and the weights say so:
  tool failure / post-suppression tool-request  → near-unambiguous       (±1.0)
  re-ask (input cosine + topic continuity)      → strong                 (±0.8)
  confusion / follow-up intent                  → moderate               (±0.5)
  tone toward the AI (impatient/dismissive)     → weak                   (±0.3)
  quiet non-negative next turn                  → weakest, positive only (+0.2)

Attribution channel: `user_tone_toward_ai`, NOT `user_emotion` — someone debugging
a hard problem reads `frustrated` while the answer was excellent; tone-toward-AI
is the attributable read. (judge_attachment._landed grades on user_emotion and
carries exactly this topic-frustration noise; do not inherit it.)

Fast-path turns (`switch_only`) synthesize tone heuristically and a degenerate
topic_summary — their tone/topic signals are DOWN-WEIGHTED (×0.5); the re-ask
cosine keeps full weight (it doesn't depend on the LLM parse).

Chemistry: this module writes NONE. Ledgers and wiring only — a new reward source
landing right after the reward-integrity hardening would be its own incident.

Stash is in-memory and keyed per session (engine tenants interleave sessions;
cross-grading would poison the ledger). Restart loss is accepted — same posture
as dmn.predicted_next.
"""

from __future__ import annotations

import math
import time as _time
from dataclasses import dataclass, field

NEGATIVE_TONES = frozenset({"impatient", "dismissive", "insulting"})
_REASK_COSINE = 0.86  # consecutive user inputs this similar = a restatement
_STASH_MAX_AGE_S = 900.0  # a pending verification older than this simply expires


@dataclass
class PendingApproach:
    turn_id: str
    information_need: str
    info_id: str
    method_id: str
    override: str  # "" | "added_action" | "suppressed_action" | "advisory"
    query_vec: list = field(default_factory=list)
    topic: str = ""
    tool_success: bool | None = None  # stamped by the motor path before next turn
    tool_output_len: int = 0
    ts: float = field(default_factory=_time.time)


def _cos(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return num / (na * nb) if na and nb else 0.0


def _looks_like_tool_request(text: str) -> bool:
    """Cross-turn reuse of temporal's within-turn detector — the single most direct
    ground truth for this stage's authority field."""
    try:
        from brain.clusters.temporal import _looks_like_tool_request as _f

        return bool(_f(text))
    except Exception:
        return False


def verify(
    pending: PendingApproach,
    next_features: dict,
    next_query_vec: list | None,
    *,
    now: float | None = None,
) -> dict | None:
    """Grade the prior turn's approach against the NEXT turn's read. Returns
    {"info": float, "method": float, "confirmed": bool, "signals": [...]} with
    per-axis credit deltas in [-1, 1], or None when verification can't run
    (stash expired, no features)."""
    now = _time.time() if now is None else now
    if not next_features or (now - pending.ts) > _STASH_MAX_AGE_S:
        return None

    switch_only = bool(next_features.get("switch_only"))
    quality = 0.5 if switch_only else 1.0  # heuristic-grade reads are worth half
    text = str(next_features.get("raw_text", "") or "")
    tone = str(next_features.get("user_tone_toward_ai", "") or "").lower()
    emotion = str(next_features.get("user_emotion", "") or "").lower()
    intent = str(next_features.get("intent", "") or "").lower()
    topic = str(next_features.get("topic_summary", "") or "")

    info = 0.0
    method = 0.0
    signals: list[str] = []

    # 1. Tool outcome (stamped last turn, near-unambiguous). Empty-but-successful
    #    output is a WEAK negative — `success` alone can't distinguish rich from
    #    hollow.
    if pending.information_need in ("external", "both"):
        if pending.tool_success is False:
            info -= 1.0
            signals.append("tool_failed")
        elif pending.tool_success is True and pending.tool_output_len < 40:
            info -= 0.3
            signals.append("tool_empty")
        elif pending.tool_success is True:
            info += 0.6
            signals.append("tool_delivered")

    # 2. Post-suppression tool request — we said no action; the user's very next
    #    message is tool-request-shaped. Near-unambiguous refutation.
    if pending.override == "suppressed_action" and _looks_like_tool_request(text):
        info -= 1.0
        signals.append("post_suppression_tool_request")

    # 3. Re-ask: high cosine between consecutive inputs AND topic continuity
    #    (a similar sentence about a NEW topic is a new question, not a repair).
    #    Degenerate fast-path topics can't veto the cosine — they just don't help.
    if next_query_vec and pending.query_vec:
        sim = _cos(pending.query_vec, next_query_vec)
        topics_comparable = bool(topic and pending.topic) and not switch_only and len(topic) > 4
        topic_same = (topic.lower() == pending.topic.lower()) if topics_comparable else True
        if sim >= _REASK_COSINE and topic_same:
            info -= 0.8
            method -= 0.8
            signals.append("re_ask")

    # 4. Confusion / clarification → the FRAMING missed; follow-up-for-more → depth.
    if emotion == "confused" or intent == "clarification_request":
        method -= 0.5 * quality
        signals.append("confusion")
    if intent == "follow_up":
        method -= 0.3 * quality
        signals.append("asked_for_more")

    # 5. Tone toward the AI — weak, both-axis.
    if tone in NEGATIVE_TONES:
        info -= 0.3 * quality
        method -= 0.3 * quality
        signals.append(f"tone_{tone}")

    # 6. Quiet non-negative next turn — the weakest signal here, positive only,
    #    and it must never carry the weight of a real confirmation on its own.
    if not signals and float(next_features.get("sentiment", 0.0) or 0.0) >= -0.1:
        info += 0.2
        method += 0.2
        signals.append("quiet_ok")

    confirmed = info > 0 and "re_ask" not in signals
    return {
        "info": max(-1.0, min(1.0, info)),
        "method": max(-1.0, min(1.0, method)),
        "confirmed": confirmed,
        "signals": signals,
    }
