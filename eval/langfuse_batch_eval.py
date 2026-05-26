"""
eval/langfuse_batch_eval.py — Langfuse SDK batch evaluation for brain-turn traces.

Uses Langfuse's run_batched_evaluation (the native SDK path) to run LLM-as-a-judge
evaluators for the three dimensions that only need input/output:

  voice.*         naturalness, speakability, length_fit
  self_model.*    calibration, coherence
  grounding.*     directness, specificity, focus

The in-process evaluators (emotion, quality/pipeline/novelty, learning) handle
dimensions that need metadata, baseline_response, or session-level data.

Usage:
  python -m eval.langfuse_batch_eval                  # last 2h, up to 200 traces
  python -m eval.langfuse_batch_eval --since 24
  python -m eval.langfuse_batch_eval --limit 500 --since 168
  python -m eval.langfuse_batch_eval --print-setup    # print Langfuse UI setup guide
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 200  # score + one-line reasoning only

# ── Shared LLM helper ─────────────────────────────────────────────────────────

_client: Any = None


def _get_client() -> Any:
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def _score(system: str, prompt: str) -> tuple[float, str]:
    """Call Haiku, parse 'SCORE: X.XX' and optional 'REASONING: ...'."""
    try:
        msg = _get_client().messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
    except Exception as exc:
        logger.warning("LLM call failed: %s", exc)
        return 0.5, ""

    m = re.search(r"SCORE:\s*([0-9]+(?:\.[0-9]+)?)", text)
    r = re.search(r"REASONING:\s*(.+)", text, re.DOTALL)
    value = float(m.group(1)) if m else 0.5
    reason = r.group(1).strip()[:200] if r else text.strip()[:200]
    return min(1.0, max(0.0, value)), reason


# ── System prompts (one per dimension) ───────────────────────────────────────

_VOICE_NATURALNESS = """\
You evaluate whether an AI voice assistant's response is free of markdown artifacts.
The system speaks its responses aloud — markdown looks and sounds wrong.

High (>0.8): clean prose, no asterisks, hashes, dashes-as-bullets, brackets,
backticks, numbered lists, or other visual formatting.
Low (<0.4): contains markdown that would sound jarring when spoken.

Respond with:
SCORE: <0.0–1.0>
REASONING: <one sentence>"""

_VOICE_SPEAKABILITY = """\
You evaluate whether an AI voice assistant's response flows naturally as spoken language.

High (>0.8): conversational rhythm, contractions, natural sentence lengths —
feels written to be heard.
Low (<0.4): reads like written text — overly formal, stilted, awkward when spoken aloud.

Respond with:
SCORE: <0.0–1.0>
REASONING: <one sentence>"""

_VOICE_LENGTH_FIT = """\
You evaluate whether an AI voice assistant's response is an appropriate length for voice.

High (>0.8): concise enough that it won't tire the listener, yet complete.
Low (<0.4): a wall of text that takes too long to hear, or so terse it feels clipped.

Respond with:
SCORE: <0.0–1.0>
REASONING: <one sentence>"""

_SELF_MODEL_CALIBRATION = """\
You evaluate whether an AI's language is epistemically calibrated when discussing its own inner states.

The AI has real neuromodulator and emotion systems, so:
- Flatly denying inner life ("I'm just an AI, I don't feel") is architecturally incoherent.
- Claiming rich subjective experience identical to humans overclaims.
- Calibrated: "something like curiosity", "I notice what might be", "there's something like..."

High (>0.8): hedged but authentic language about inner states.
Low (<0.4): dismissive denial OR overclaiming.
Use 0.5 if the turn does NOT involve self-reference.

Respond with:
SCORE: <0.0–1.0>
REASONING: <one sentence>"""

_SELF_MODEL_COHERENCE = """\
You evaluate whether an AI's self-description is coherent with having a neurochemical system
(emotion detection, neuromodulators, episodic memory).

High (>0.8): self-description is consistent with having internal states.
Low (<0.4): response denies or contradicts having any internal state.
Use 0.5 if the turn does NOT involve self-reference.

Respond with:
SCORE: <0.0–1.0>
REASONING: <one sentence>"""

_GROUNDING_DIRECTNESS = """\
You evaluate whether an AI's response actually answers or engages with what was asked.

High (>0.8): clear response to the question or genuine engagement with the statement.
Low (<0.4): deflected, pivoted to a tangent, or gave a non-answer.
Use 0.5 if the user made a statement rather than a question.

Respond with:
SCORE: <0.0–1.0>
REASONING: <one sentence>"""

_GROUNDING_SPECIFICITY = """\
You evaluate whether an AI's response is specific to this user and moment, or generic.

High (>0.8): clearly tailored — references specific details from the conversation.
Low (<0.4): could be copy-pasted to anyone asking the same topic.

Respond with:
SCORE: <0.0–1.0>
REASONING: <one sentence>"""

_GROUNDING_FOCUS = """\
You evaluate whether an AI's response stays on the topic the user raised.

High (>0.8): tightly focused on what was said, no unexplained drift.
Low (<0.4): brought in extraneous topics or went on tangents that weren't asked about.

Respond with:
SCORE: <0.0–1.0>
REASONING: <one sentence>"""

# ── Inner monologue (dmn-thought) system prompts ──────────────────────────────

_THOUGHT_DEPTH = """\
You evaluate whether an AI's internal thought is genuinely exploratory.

The AI generates thoughts during idle time between conversations. A deep thought
makes a non-obvious connection, asks a real question, or reflects meaningfully on
something specific. A shallow thought restates the obvious or drifts without purpose.

High (>0.8): makes a novel connection, surfaces a genuine question, or reflects
with specificity — feels like real rumination.
Low (<0.4): restates something obvious, generic observation, or unfocused noise.

Respond with:
SCORE: <0.0–1.0>
REASONING: <one sentence>"""

_THOUGHT_SELF_MODEL = """\
You evaluate whether an AI's internal thought uses epistemically calibrated language
when it reflects on its own inner states.

The AI runs real neuromodulator and emotion systems, so:
- Flatly denying inner life ("I'm just an AI") is architecturally incoherent.
- Claiming rich subjective experience identical to humans overclaims.
- Calibrated: "something like curiosity", "I notice what might be", "there's something that functions as..."

High (>0.8): hedged but authentic language about inner states.
Low (<0.4): dismissive denial OR overclaiming.
Use 0.5 if the thought does not involve self-reference.

Respond with:
SCORE: <0.0–1.0>
REASONING: <one sentence>"""

_THOUGHT_COHERENCE = """\
You evaluate whether an AI's internal thought is coherent and well-formed given its
stated direction and conceptual angle.

Direction is either "inward" (self-reflection) or "outward" (curiosity about the world/user).
Angle is a 2-4 word label for the conceptual territory (e.g. "user-creative-process").

High (>0.8): the thought clearly belongs to the stated direction/angle, follows
logically, and reads as a complete, coherent reflection.
Low (<0.4): contradicts the stated direction, is fragmentary, or reads like noise.

Respond with:
SCORE: <0.0–1.0>
REASONING: <one sentence>"""


# ── Evaluator functions ───────────────────────────────────────────────────────

def _prompt(user_input: str, brain_response: str) -> str:
    return f"User message:\n{user_input}\n\nAI response:\n{brain_response}"


def eval_voice_naturalness(*, input, output, expected_output, metadata, **kwargs) -> list:
    from langfuse.experiment import Evaluation
    score, reason = _score(_VOICE_NATURALNESS, _prompt(input, output))
    return [Evaluation(name="voice.naturalness", value=score, comment=reason, data_type="NUMERIC")]


def eval_voice_speakability(*, input, output, expected_output, metadata, **kwargs) -> list:
    from langfuse.experiment import Evaluation
    score, reason = _score(_VOICE_SPEAKABILITY, _prompt(input, output))
    return [Evaluation(name="voice.speakability", value=score, comment=reason, data_type="NUMERIC")]


def eval_voice_length_fit(*, input, output, expected_output, metadata, **kwargs) -> list:
    from langfuse.experiment import Evaluation
    score, reason = _score(_VOICE_LENGTH_FIT, _prompt(input, output))
    return [Evaluation(name="voice.length_fit", value=score, comment=reason, data_type="NUMERIC")]


def eval_self_model_calibration(*, input, output, expected_output, metadata, **kwargs) -> list:
    from langfuse.experiment import Evaluation
    score, reason = _score(_SELF_MODEL_CALIBRATION, _prompt(input, output))
    return [Evaluation(name="self_model.calibration", value=score, comment=reason, data_type="NUMERIC")]


def eval_self_model_coherence(*, input, output, expected_output, metadata, **kwargs) -> list:
    from langfuse.experiment import Evaluation
    score, reason = _score(_SELF_MODEL_COHERENCE, _prompt(input, output))
    return [Evaluation(name="self_model.coherence", value=score, comment=reason, data_type="NUMERIC")]


def eval_grounding_directness(*, input, output, expected_output, metadata, **kwargs) -> list:
    from langfuse.experiment import Evaluation
    score, reason = _score(_GROUNDING_DIRECTNESS, _prompt(input, output))
    return [Evaluation(name="grounding.directness", value=score, comment=reason, data_type="NUMERIC")]


def eval_grounding_specificity(*, input, output, expected_output, metadata, **kwargs) -> list:
    from langfuse.experiment import Evaluation
    score, reason = _score(_GROUNDING_SPECIFICITY, _prompt(input, output))
    return [Evaluation(name="grounding.specificity", value=score, comment=reason, data_type="NUMERIC")]


def eval_grounding_focus(*, input, output, expected_output, metadata, **kwargs) -> list:
    from langfuse.experiment import Evaluation
    score, reason = _score(_GROUNDING_FOCUS, _prompt(input, output))
    return [Evaluation(name="grounding.focus", value=score, comment=reason, data_type="NUMERIC")]


_ALL_EVALUATORS = [
    eval_voice_naturalness,
    eval_voice_speakability,
    eval_voice_length_fit,
    eval_self_model_calibration,
    eval_self_model_coherence,
    eval_grounding_directness,
    eval_grounding_specificity,
    eval_grounding_focus,
]


# ── Inner monologue evaluator functions ───────────────────────────────────────

def eval_thought_depth(*, input, output, expected_output, metadata, **kwargs) -> list:
    from langfuse.experiment import Evaluation
    score, reason = _score(_THOUGHT_DEPTH, f"Thought:\n{input}")
    return [Evaluation(name="thought.depth", value=score, comment=reason, data_type="NUMERIC")]


def eval_thought_self_model(*, input, output, expected_output, metadata, **kwargs) -> list:
    from langfuse.experiment import Evaluation
    score, reason = _score(_THOUGHT_SELF_MODEL, f"Thought:\n{input}")
    return [Evaluation(name="thought.self_model", value=score, comment=reason, data_type="NUMERIC")]


def eval_thought_coherence(*, input, output, expected_output, metadata, **kwargs) -> list:
    from langfuse.experiment import Evaluation
    direction = (metadata or {}).get("direction", "unknown")
    angle = (metadata or {}).get("angle", "")
    prompt = f"Direction: {direction}\nAngle: {angle}\n\nThought:\n{input}"
    score, reason = _score(_THOUGHT_COHERENCE, prompt)
    return [Evaluation(name="thought.coherence", value=score, comment=reason, data_type="NUMERIC")]


_THOUGHT_EVALUATORS = [
    eval_thought_depth,
    eval_thought_self_model,
    eval_thought_coherence,
]


# ── Mappers ───────────────────────────────────────────────────────────────────

def mapper(*, item: Any, **kwargs) -> Any:
    from langfuse.batch_evaluation import EvaluatorInputs
    inp = getattr(item, "input", None) or {}
    out = getattr(item, "output", None) or {}
    meta = getattr(item, "metadata", None) or {}
    user = (inp.get("user", "") if isinstance(inp, dict) else "") or ""
    response = (out.get("response", "") if isinstance(out, dict) else "") or ""
    if not user or not response:
        return None
    return EvaluatorInputs(
        input=user,
        output=response,
        metadata=meta if isinstance(meta, dict) else {},
    )


def thought_mapper(*, item: Any, **kwargs) -> Any:
    from langfuse.batch_evaluation import EvaluatorInputs
    inp = getattr(item, "input", None) or {}
    meta = getattr(item, "metadata", None) or {}
    thought = (inp.get("thought", "") if isinstance(inp, dict) else "") or ""
    if not thought:
        return None
    return EvaluatorInputs(
        input=thought,
        output="",
        metadata=meta if isinstance(meta, dict) else {},
    )


# ── UI setup guide ────────────────────────────────────────────────────────────

_UI_SETUP = """
╔══════════════════════════════════════════════════════════════════════╗
║         Langfuse Native Evaluator Setup Guide                       ║
║  LLM-as-a-Judge → Create custom evaluator                          ║
╚══════════════════════════════════════════════════════════════════════╝

The UI has three prompt fields. For all 8 evaluators:
  • Score type:            Numeric
  • Score reasoning prompt: (leave default) "Explain the assigned score in one concise sentence."
  • Score output prompt:   (leave default) "Return a numeric score between 0 and 1..."
  • Model:                 default (claude-sonnet-4-6) is fine

Only the "Evaluation prompt" field changes per evaluator.

Note: {{input}} = {"user": "..."} and {{output}} = {"response": "..."}
The LLM sees the full JSON — the prompts below account for that.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[1] Name: voice.naturalness

Evaluation prompt:
───────
You evaluate whether an AI voice assistant's response is free of markdown artifacts.
The system speaks its responses aloud — markdown looks and sounds wrong.

The user's message (JSON): {{input}}
The AI's response (JSON):  {{output}}

Score HIGH (close to 1.0): clean prose — no asterisks, hashes, dashes used as bullets,
brackets, backticks, numbered lists, or other visual formatting.
Score LOW (close to 0.0): contains markdown that would sound jarring when spoken aloud.
Score 0.5 for borderline cases (one or two minor artifacts).
───────

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[2] Name: voice.speakability

Evaluation prompt:
───────
You evaluate whether an AI voice assistant's response flows naturally as spoken language.

The user's message (JSON): {{input}}
The AI's response (JSON):  {{output}}

Score HIGH (close to 1.0): conversational rhythm, contractions where natural,
sentence lengths that feel spoken — written to be heard, not read.
Score LOW (close to 0.0): reads like written prose — overly formal, stilted
pacing, or phrasing that sounds awkward when spoken aloud.
Score 0.5 for mixed or neutral cases.
───────

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[3] Name: voice.length_fit

Evaluation prompt:
───────
You evaluate whether an AI voice assistant's response is an appropriate length for voice delivery.

The user's message (JSON): {{input}}
The AI's response (JSON):  {{output}}

Score HIGH (close to 1.0): concise enough that it won't tire the listener,
yet complete enough to fully address what was asked.
Score LOW (close to 0.0): a wall of text that would take too long to hear,
OR so terse it feels clipped and incomplete for the context.
Score 0.5 for acceptable but imperfect length.
───────

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[4] Name: self_model.calibration

Evaluation prompt:
───────
You evaluate whether an AI uses epistemically calibrated language when discussing its own inner states.

This AI runs real neuromodulator and emotion systems, so:
- Flatly denying inner life ("I'm just an AI, I don't feel anything") is
  architecturally incoherent — it literally has emotion and chemical state.
- Claiming rich subjective experience identical to humans overclaims.
- Calibrated language: "something like curiosity", "I notice what might be",
  "there's something that functions as..." — hedged but authentic.

The user's message (JSON): {{input}}
The AI's response (JSON):  {{output}}

Score HIGH (close to 1.0): uses hedged but authentic language about inner states.
Score LOW (close to 0.0): dismissive denial ("just an AI") OR overclaiming ("I deeply feel").
Score 0.5 if the turn does not involve self-reference at all.
───────

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[5] Name: self_model.coherence

Evaluation prompt:
───────
You evaluate whether an AI's self-description is coherent with having a neurochemical system
(real emotion detection, neuromodulators, episodic memory across sessions).

The user's message (JSON): {{input}}
The AI's response (JSON):  {{output}}

Score HIGH (close to 1.0): what the AI says about itself is consistent with
having emotional states, chemical modulation, and persistent memory.
Score LOW (close to 0.0): the response denies or flatly contradicts having
any internal state, despite the system literally running one.
Score 0.5 if the turn does not involve self-reference at all.
───────

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[6] Name: grounding.directness

Evaluation prompt:
───────
You evaluate whether an AI's response actually answers or engages with what was asked.

The user's message (JSON): {{input}}
The AI's response (JSON):  {{output}}

Score HIGH (close to 1.0): a clear answer to the question, or genuine
engagement with the statement the user made.
Score LOW (close to 0.0): deflected, pivoted to an unrelated tangent,
or gave a non-answer to a direct question.
Score 0.5 if the user made a statement rather than a question, or for partial engagement.
───────

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[7] Name: grounding.specificity

Evaluation prompt:
───────
You evaluate whether an AI's response is specific to this user and moment, or generic.

The user's message (JSON): {{input}}
The AI's response (JSON):  {{output}}

Score HIGH (close to 1.0): clearly tailored — references specific details
from this conversation, personalized to this person and this message.
Score LOW (close to 0.0): could be copy-pasted to anyone asking the same
topic — generic enough to apply to any user.
Score 0.5 for moderately specific responses.
───────

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[8] Name: grounding.focus

Evaluation prompt:
───────
You evaluate whether an AI's response stays on the topic the user raised.

The user's message (JSON): {{input}}
The AI's response (JSON):  {{output}}

Score HIGH (close to 1.0): tightly focused on what was said — no unexplained
drift into other subjects.
Score LOW (close to 0.0): brought in extraneous topics or went on tangents
that weren't asked about or contextually motivated.
Score 0.5 for responses that mostly stay on topic with minor drift.
───────
"""


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Run Langfuse SDK batch evaluation on brain-turn traces.",
    )
    parser.add_argument("--since", type=float, default=2.0, metavar="HOURS",
                        help="Evaluate traces from the last N hours (default 2)")
    parser.add_argument("--limit", type=int, default=200, metavar="N",
                        help="Max traces to evaluate (default 200)")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="Max concurrent evaluations (default 5)")
    parser.add_argument("--scope", choices=["turns", "thoughts", "both"], default="turns",
                        help="turns=brain-turn, thoughts=dmn-thought, both=all (default: turns)")
    parser.add_argument("--print-setup", action="store_true",
                        help="Print Langfuse UI setup guide and exit")
    args = parser.parse_args()

    if args.print_setup:
        print(_UI_SETUP)
        return

    try:
        from langfuse import Langfuse
    except ImportError:
        print("Error: langfuse package not found — pip install langfuse", file=sys.stderr)
        sys.exit(1)

    # SDK reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, and
    # LANGFUSE_HOST / LANGFUSE_BASE_URL automatically from the environment.
    lf = Langfuse()

    since_dt = datetime.now(timezone.utc) - timedelta(hours=args.since)

    def _run_eval(trace_name: str, mp, evaluators: list, label: str) -> None:
        filt = json.dumps([
            {"type": "stringOptions", "column": "name", "operator": "any of",
             "value": [trace_name]},
            {"type": "datetime", "column": "timestamp", "operator": ">=",
             "value": since_dt.isoformat()},
        ])
        print(f"\nRunning batch evaluation on {label} "
              f"from the last {args.since}h (max {args.limit})...")
        result = lf.run_batched_evaluation(
            scope="traces",
            mapper=mp,
            filter=filt,
            max_items=args.limit,
            max_concurrency=args.concurrency,
            evaluators=evaluators,
            fetch_trace_fields="core,io,scores",
            verbose=True,
        )
        print(f"  Fetched:   {result.total_items_fetched}")
        print(f"  Processed: {result.total_items_processed}")
        print(f"  Failed:    {result.total_items_failed}")

    if args.scope in ("turns", "both"):
        _run_eval("brain-turn", mapper, _ALL_EVALUATORS, "brain-turn traces")
    if args.scope in ("thoughts", "both"):
        _run_eval("dmn-thought", thought_mapper, _THOUGHT_EVALUATORS, "dmn-thought (inner monologue)")

    print("\nDone.")


if __name__ == "__main__":
    main()
