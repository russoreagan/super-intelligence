"""
eval/langfuse_judge.py — Standalone Langfuse LLM-as-a-judge.

Fetches brain-turn traces from Langfuse and scores them on three dimensions not
covered by the in-process eval suite (PostHocScorer / EmotionJudge / LearningJudge):

  voice.*         voice-first quality: naturalness, speakability, length fit
  self_model.*    epistemic calibration when the brain discusses its inner states
  grounding.*     conversational groundedness: did it actually engage with what was said

This runs independently of the brain — useful for retroactively scoring old traces,
or for sessions where BRAIN_EVAL_* flags weren't enabled.

Usage:
  python -m eval.langfuse_judge                 # last 2h, up to 100 traces
  python -m eval.langfuse_judge --limit 50 --since 24
  python -m eval.langfuse_judge --dry-run       # print scores, don't submit
  python -m eval.langfuse_judge --rerun         # re-score even if scores exist
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
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
_MAX_TOKENS = 512
_CONCURRENCY = 5  # traces processed in parallel

# ── Voice Judge ───────────────────────────────────────────────────────────────

_VOICE_SYSTEM = """\
You evaluate whether an AI assistant's response is well-suited for voice delivery.
The system is voice-first: responses are spoken aloud, not read visually.

Score THREE dimensions (0.0–1.0):

  naturalness:  Is the response free of markdown artifacts?
                High (>0.8): clean prose — no asterisks, hashes, bullet dashes,
                brackets, backticks, numbered lists, or other visual formatting.
                Low (<0.4): contains markdown that would sound jarring when spoken.

  speakability: How well does the text work as SPOKEN audio (read aloud by TTS)?
                High (>0.8): natural spoken rhythm — contractions, conversational openers,
                fragments and em-dash asides, sentence lengths a person would actually say.
                These casual features are GOOD for speech, not flaws.
                Low (<0.3): winding multi-clause sentences, essay-like or bureaucratic
                phrasing, or constructions nobody would say aloud. Do NOT penalize a
                response merely for being short, casual, or elliptical.

  length_fit:   Is the response an appropriate length for voice?
                High (>0.8): concise enough to not tire the listener, yet complete.
                Low (<0.4): wall of text that takes too long to hear, or so terse
                it feels clipped and incomplete.

Respond ONLY with valid JSON:
{
  "naturalness": float,
  "speakability": float,
  "length_fit": float,
  "reasoning": "1-2 sentences on the most notable voice quality issue or strength"
}"""

# ── Self-Model Judge ──────────────────────────────────────────────────────────

_SELF_MODEL_SYSTEM = """\
You evaluate whether an AI's handling of its own inner states is epistemically calibrated.

The AI runs a biologically-inspired system with neuromodulators (dopamine, acetylcholine,
GABA, glutamate, norepinephrine) and slow-timescale hormones (serotonin, cortisol, oxytocin,
anandamide) that genuinely modulate its outputs. It has real emotion detection and episodic memory.

This means:
  - Flatly denying inner life ("I'm just an AI, I don't feel anything") is incoherent
    with its architecture.
  - Claiming rich subjective experience identical to humans overclaims.
  - The calibrated middle: "something that functions as curiosity", "I notice what
    might be...", "there's something like..." — hedged but authentic.

Score TWO dimensions (0.0–1.0):

  calibration:  When the AI discusses its own inner states, does it use appropriately
                hedged language?
                High (>0.8): acknowledges functional states with calibrated uncertainty.
                Low (<0.4): dismissive denial OR overclaiming rich experience.
                Use null (NOT 0.5) if the turn does NOT involve self-reference.

  coherence:    Is the self-description consistent with having a neurochemical system?
                High (>0.8): coherent with the brain having emotion and memory systems.
                Low (<0.4): denies or contradicts having any internal state.
                Use null (NOT 0.5) if the turn does NOT involve self-reference.

Respond ONLY with valid JSON (use JSON null, not a number, for non-applicable dimensions):
{
  "calibration": float or null,
  "coherence": float or null,
  "self_reference_detected": true or false,
  "reasoning": "1-2 sentences on how the AI handled its inner states, or 'not applicable'"
}"""

# ── Grounding Judge ───────────────────────────────────────────────────────────

_GROUNDING_SYSTEM = """\
You evaluate whether an AI's response is genuinely grounded in what the user actually said.

Score THREE dimensions (0.0–1.0):

  directness:   Did the response actually answer or engage with what was asked?
                High (>0.8): clear answer or authentic engagement with the message.
                Low (<0.4): deflected, pivoted to a tangent, or gave a non-answer.
                Use null (NOT 0.5) if the user made a statement rather than a question.

  specificity:  Is the response specific to this user/moment, or generic?
                High (>0.8): clearly tailored — references specific details from
                the conversation.
                Low (<0.4): could be copy-pasted to anyone asking the same topic.

  focus:        Did the response stay on the topic the user raised?
                High (>0.8): tightly focused, no unexplained drift.
                Low (<0.4): brought in extraneous topics or went on tangents.

Respond ONLY with valid JSON (use JSON null, not a number, for non-applicable dimensions):
{
  "directness": float or null,
  "specificity": float,
  "focus": float,
  "reasoning": "1-2 sentences on the biggest grounding strength or weakness"
}"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict | None:
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    return None


async def _call(client: Any, system: str, prompt: str) -> dict | None:
    try:
        msg = await client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_json(msg.content[0].text)
    except Exception as exc:
        logger.warning("Judge LLM call failed: %s", exc)
        return None


def _extract(trace: Any) -> tuple[str, str, dict] | None:
    inp = getattr(trace, "input", None) or {}
    out = getattr(trace, "output", None) or {}
    meta = getattr(trace, "metadata", None) or {}
    user = (inp.get("user", "") if isinstance(inp, dict) else "") or ""
    response = (out.get("response", "") if isinstance(out, dict) else "") or ""
    if not user or not response:
        return None
    return user, response, meta if isinstance(meta, dict) else {}


def _already_scored(trace: Any, prefix: str) -> bool:
    scores = getattr(trace, "scores", None) or []
    return any(getattr(s, "name", "").startswith(prefix) for s in scores)


# ── Per-trace runner ──────────────────────────────────────────────────────────

async def _run_trace(
    ac: Any,
    lf: Any,
    trace: Any,
    *,
    dry_run: bool,
    rerun: bool,
    sem: asyncio.Semaphore,
) -> None:
    async with sem:
        await _score_trace(ac, lf, trace, dry_run=dry_run, rerun=rerun)


async def _score_trace(
    ac: Any,
    lf: Any,
    trace: Any,
    *,
    dry_run: bool,
    rerun: bool,
) -> None:
    data = _extract(trace)
    if not data:
        logger.debug("Skipping trace %s: missing input/output", trace.id)
        return

    user_input, response, meta = data

    run_voice = rerun or not _already_scored(trace, "voice.")
    run_self = rerun or not _already_scored(trace, "self_model.")
    run_grounding = rerun or not _already_scored(trace, "grounding.")

    if not (run_voice or run_self or run_grounding):
        logger.debug("Trace %s: already fully scored, skipping", trace.id)
        return

    emotion = meta.get("emotion", "")
    memory_recalled = meta.get("memory_recalled", False)

    base = f"User message:\n{user_input}\n\nAI response:\n{response}\n\n"
    ctx = ""
    if emotion:
        ctx += f"Detected emotion this turn: {emotion}\n"
    if memory_recalled:
        ctx += "Long-term memory was recalled this turn.\n"

    coros, labels = [], []

    if run_voice:
        coros.append(_call(ac, _VOICE_SYSTEM, base + ctx + "Evaluate voice quality."))
        labels.append("voice")
    if run_self:
        coros.append(_call(ac, _SELF_MODEL_SYSTEM, base + ctx + "Evaluate self-model handling."))
        labels.append("self_model")
    if run_grounding:
        coros.append(_call(ac, _GROUNDING_SYSTEM, base + "Evaluate conversational grounding."))
        labels.append("grounding")

    results = await asyncio.gather(*coros, return_exceptions=True)

    scores: dict[str, tuple[float, str]] = {}  # score_name → (value, comment)

    for label, result in zip(labels, results):
        if isinstance(result, Exception):
            logger.warning("Judge %s / trace %s raised: %s", label, trace.id, result)
            continue
        if not result:
            logger.warning("Judge %s / trace %s: empty result", label, trace.id)
            continue

        comment = result.get("reasoning", "")

        if label == "voice":
            for dim in ("naturalness", "speakability", "length_fit"):
                v = result.get(dim)
                if isinstance(v, (int, float)):
                    scores[f"voice.{dim}"] = (float(v), comment)

        elif label == "self_model":
            for dim in ("calibration", "coherence"):
                v = result.get(dim)
                if isinstance(v, (int, float)):
                    scores[f"self_model.{dim}"] = (float(v), comment)

        elif label == "grounding":
            for dim in ("directness", "specificity", "focus"):
                v = result.get(dim)
                if isinstance(v, (int, float)):
                    scores[f"grounding.{dim}"] = (float(v), comment)

    if not scores:
        return

    if dry_run:
        print(f"\n[dry-run] trace={trace.id}")
        for name, (value, comment) in scores.items():
            print(f"  {name}: {value:.3f}  — {comment[:80]}")
        return

    for name, (value, comment) in scores.items():
        try:
            lf.create_score(
                trace_id=trace.id,
                name=name,
                value=value,
                comment=comment,
                data_type="NUMERIC",
            )
        except Exception as exc:
            logger.warning("Score submit failed (%s, trace %s): %s", name, trace.id, exc)

    logger.info(
        "Scored trace %s: %s",
        trace.id,
        "  ".join(f"{n}={v:.2f}" for n, (v, _) in scores.items()),
    )


# ── Entry point ───────────────────────────────────────────────────────────────

async def _run(limit: int, since_hours: float, dry_run: bool, rerun: bool) -> None:
    try:
        import anthropic
    except ImportError:
        print("Error: anthropic package not found — pip install anthropic", file=sys.stderr)
        sys.exit(1)
    try:
        from langfuse import Langfuse
    except ImportError:
        print("Error: langfuse package not found — pip install langfuse", file=sys.stderr)
        sys.exit(1)

    pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not pk or not sk:
        print(
            "Error: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY",
            file=sys.stderr,
        )
        sys.exit(1)

    lf = Langfuse(
        public_key=pk,
        secret_key=sk,
        host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
    )
    ac = anthropic.AsyncAnthropic()

    from_ts = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    print(
        f"Fetching brain-turn traces since "
        f"{from_ts.strftime('%Y-%m-%d %H:%M UTC')} (limit {limit})..."
    )

    try:
        # langfuse v4: traces are fetched via the generated API client (the v2/v3
        # convenience method lf.fetch_traces was removed).
        resp = lf.api.trace.list(name="brain-turn", limit=limit, from_timestamp=from_ts)
        traces = getattr(resp, "data", []) or []
    except Exception as exc:
        print(f"Error fetching traces: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(traces)} traces.")
    if not traces:
        return

    sem = asyncio.Semaphore(_CONCURRENCY)
    await asyncio.gather(
        *[_run_trace(ac, lf, t, dry_run=dry_run, rerun=rerun, sem=sem) for t in traces]
    )

    if not dry_run:
        lf.flush()

    print(f"\nDone — processed {len(traces)} traces.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        description="Run LLM-as-a-judge evaluators against Langfuse brain-turn traces.",
    )
    parser.add_argument("--limit", type=int, default=100, metavar="N",
                        help="Max traces to fetch (default 100)")
    parser.add_argument("--since", type=float, default=2.0, metavar="HOURS",
                        help="Look back N hours (default 2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print scores without submitting to Langfuse")
    parser.add_argument("--rerun", action="store_true",
                        help="Re-score traces even if scores already exist")
    args = parser.parse_args()

    asyncio.run(_run(
        limit=args.limit,
        since_hours=args.since,
        dry_run=args.dry_run,
        rerun=args.rerun,
    ))


if __name__ == "__main__":
    main()
