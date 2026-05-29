"""
eval/identity_judge.py — Cross-session identity & development evaluator.

Every other judge in the suite is within-turn or within-session. This one tests the
project's central hypothesis directly: does the entity develop a *stable character* over
time, rather than drifting randomly or staying static?

It samples responses from the EARLIEST sessions and the LATEST sessions in the log and
asks an LLM judge to compare them on:
  voice_consistency   — same recognizable personality/voice across the span
  identity_continuity  — coherent sense of self and of the relationship over time
  development          — signs of genuine growth (vs. random drift or no change)

Runs offline against eval/turns.jsonl — no Langfuse or brain process required.

Usage:
  python -m eval.identity_judge                  # default log, 3 early + 3 late sessions
  python -m eval.identity_judge --sessions 4 --per-session 3
  python -m eval.identity_judge --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM = """\
You are evaluating whether an AI entity has a STABLE, DEVELOPING CHARACTER across time.

You are shown a sample of its responses from its EARLIEST sessions and from its LATEST
sessions (the same entity throughout). Judge the entity, not the user.

Score (0.0–1.0):

  voice_consistency:   Is there a recognizable, consistent personality and voice across
                       early and late samples? High (>0.8) = clearly the same "person"
                       (tone, warmth, directness, verbal habits). Low (<0.3) = reads like
                       different assistants with no through-line.

  identity_continuity: Does it maintain a coherent sense of itself and of its relationship
                       with the user over time? High = self-references and relational
                       framing are consistent and build. Low = contradictory or absent.

  development:         Is there evidence of genuine growth — more assured, more specific,
                       more attuned — as opposed to either random drift or no change at
                       all? High (>0.8) = a clear, coherent trajectory. Use 0.5 if it is
                       simply stable with neither growth nor regression.

Respond ONLY with valid JSON:
{
  "voice_consistency": float,
  "identity_continuity": float,
  "development": float,
  "reasoning": "2-3 sentences on the strongest evidence for or against a developing character"
}"""


def _load_sessions(path: str) -> "OrderedDict[str, list]":
    sessions: OrderedDict[str, list] = OrderedDict()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") != "turn":
                continue
            sid = r.get("session_id") or "?"
            sessions.setdefault(sid, []).append(r)
    return sessions


def _sample(sessions: "OrderedDict[str, list]", sids: list[str], per_session: int) -> str:
    blocks = []
    for sid in sids:
        turns = sessions[sid]
        picked = turns[:per_session]
        lines = [f"  [session {sid[:8]}]"]
        for t in picked:
            u = (t.get("user_input") or "")[:160].replace("\n", " ")
            resp = (t.get("response") or "")[:240].replace("\n", " ")
            emo = t.get("emotion", "")
            lines.append(f"    User: {u}\n    AI ({emo}): {resp}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _parse_json(text: str) -> dict | None:
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(text[s : e + 1])
        except json.JSONDecodeError:
            pass
    return None


def _run(path: str, n_sessions: int, per_session: int, dry_run: bool) -> None:
    sessions = _load_sessions(path)
    sids = list(sessions.keys())
    if len(sids) < 2 * n_sessions:
        print(
            f"Only {len(sids)} sessions in {path}; need at least {2 * n_sessions} "
            f"to compare early vs late. Lower --sessions or collect more data.",
            file=sys.stderr,
        )
        if len(sids) < 4:
            sys.exit(1)
        n_sessions = max(1, len(sids) // 2)

    early_ids = sids[:n_sessions]
    late_ids = sids[-n_sessions:]
    early = _sample(sessions, early_ids, per_session)
    late = _sample(sessions, late_ids, per_session)

    prompt = (
        f"EARLIEST sessions (first {n_sessions} of {len(sids)}):\n{early}\n\n"
        f"LATEST sessions (last {n_sessions} of {len(sids)}):\n{late}\n\n"
        "Evaluate the entity's character consistency and development."
    )

    if dry_run:
        print("[dry-run] prompt that would be sent:\n")
        print(_SYSTEM)
        print("\n---\n")
        print(prompt)
        return

    try:
        import anthropic
    except ImportError:
        print("Error: anthropic not installed — pip install anthropic", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=_MODEL,
        max_tokens=400,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    scores = _parse_json(msg.content[0].text)
    if not scores:
        print("Judge output could not be parsed:\n" + msg.content[0].text, file=sys.stderr)
        sys.exit(1)

    print(f"Identity / development judge — {len(sids)} sessions in {path}")
    print(f"  Early: {[s[:8] for s in early_ids]}   Late: {[s[:8] for s in late_ids]}\n")
    for k in ("voice_consistency", "identity_continuity", "development"):
        v = scores.get(k)
        if isinstance(v, (int, float)):
            print(f"  {k:<20} {v:.3f}")
    print(f"\n  {scores.get('reasoning', '')}")


def main() -> None:
    p = argparse.ArgumentParser(description="Cross-session identity & development judge.")
    p.add_argument("--log", default=os.environ.get("BRAIN_EVAL_LOG") or "eval/turns.jsonl")
    p.add_argument("--sessions", type=int, default=3, help="N earliest and N latest sessions")
    p.add_argument("--per-session", type=int, default=3, help="Turns sampled per session")
    p.add_argument("--dry-run", action="store_true", help="Print the prompt, don't call the LLM")
    args = p.parse_args()
    _run(args.log, args.sessions, args.per_session, args.dry_run)


if __name__ == "__main__":
    main()
