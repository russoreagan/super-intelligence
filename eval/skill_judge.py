"""
eval/skill_judge.py — Efficacy evaluator for the dynamic reasoning-framework selector.

The skill router injects a reasoning / emotional-intelligence framework into the drafters
on substantive turns. Nothing currently scores whether that selection helps. This tool,
run offline against eval/turns.jsonl + its decision records, answers two questions:

  1. LLM judge (per turn where a skill was chosen): was the framework an appropriate fit,
     and is its influence visible and beneficial in the response (without being announced)?

  2. Deterministic comparison: mean selected-draft quality on turns WHERE a skill was chosen
     vs. turns the selector GATED OUT — a cheap signal for whether selection tracks quality.

No Langfuse or brain process required.

Usage:
  python -m eval.skill_judge                 # judge up to 30 skill turns
  python -m eval.skill_judge --limit 50
  python -m eval.skill_judge --dry-run       # deterministic comparison only, no LLM
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM = """\
You evaluate whether a reasoning or emotional-intelligence "thinking framework" that was
chosen for an AI turn was a good fit, and whether its influence is visible and beneficial
in the response. The framework is meant to be used as a habit of thought, NOT announced —
so do not reward the response for naming it.

You are given: the user's message, the framework that was selected, and the AI's response.

Score (0.0–1.0):
  fit:      Was this framework appropriate for what the turn actually called for? High
            (>0.8) = a clearly suitable lens; Low (<0.3) = irrelevant or a poor match.
  benefit:  Is the response better for having reasoned through this lens — more rigorous,
            structured, balanced, or attuned — than a generic answer would be? High = the
            framework's influence is visible and helps; Low = no discernible benefit.

Respond ONLY with valid JSON:
{
  "fit": float,
  "benefit": float,
  "reasoning": "1 sentence on whether the framework fit and helped"
}"""


def _load(path: str) -> tuple[dict, list]:
    turns: dict[str, dict] = {}
    decisions: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") == "turn":
                turns[r.get("turn_id", "")] = r
            elif r.get("type") == "decision":
                decisions.append(r)
    return turns, decisions


def _selected_overall(turn: dict) -> float | None:
    """Mean 'overall' of the selected draft(s), if internal critic scores exist."""
    ds = turn.get("draft_scores") or []
    sel = [d.get("overall") for d in ds if d.get("selected") and isinstance(d.get("overall"), (int, float))]
    if not sel:
        sel = [d.get("overall") for d in ds if isinstance(d.get("overall"), (int, float))]
    return statistics.mean(sel) if sel else None


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


def _run(path: str, limit: int, dry_run: bool) -> None:
    turns, decisions = _load(path)

    # turn_id → chosen skill (conversational picks / active reuse with a real choice)
    picked: dict[str, str] = {}
    gated: set[str] = set()
    for d in decisions:
        kind = d.get("decision")
        tid = d.get("turn_id", "")
        if kind in ("skill_selector_pick", "skill_active_reused"):
            chosen = d.get("chosen") or []
            if chosen and tid:
                picked[tid] = chosen[0]
        elif kind == "skill_selector_gated_out" and tid:
            gated.add(tid)

    print(f"Skill selector: {len(picked)} turns with a chosen framework, "
          f"{len(gated)} gated out (in {path}).\n")

    # ── Deterministic: quality on skill turns vs gated turns ──────────────────
    skill_q = [q for tid in picked if (q := _selected_overall(turns.get(tid, {}))) is not None]
    gated_q = [q for tid in gated if (q := _selected_overall(turns.get(tid, {}))) is not None]
    if skill_q and gated_q:
        print("  DETERMINISTIC (selected-draft 'overall' critic score):")
        print(f"    skill-selected turns: mean {statistics.mean(skill_q):.3f}  (n={len(skill_q)})")
        print(f"    gated-out turns:      mean {statistics.mean(gated_q):.3f}  (n={len(gated_q)})")
        print(f"    difference:           {statistics.mean(skill_q) - statistics.mean(gated_q):+.3f}\n")
    else:
        print("  DETERMINISTIC: not enough turns with internal critic scores to compare.\n")

    if dry_run or not picked:
        if not picked:
            print("No skill-selected turns to LLM-judge.")
        return

    try:
        import anthropic
    except ImportError:
        print("Error: anthropic not installed — pip install anthropic", file=sys.stderr)
        sys.exit(1)
    client = anthropic.Anthropic()

    fits, benefits = [], []
    judged = 0
    for tid, skill in list(picked.items())[:limit]:
        turn = turns.get(tid)
        if not turn or not turn.get("response"):
            continue
        prompt = (
            f"User message:\n{turn.get('user_input', '')}\n\n"
            f"Selected framework: {skill}\n\n"
            f"AI response:\n{turn.get('response', '')}\n\n"
            "Evaluate fit and benefit."
        )
        try:
            msg = client.messages.create(
                model=_MODEL, max_tokens=200, system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            sc = _parse_json(msg.content[0].text)
        except Exception as e:
            print(f"  judge failed for {tid[:8]}: {e}", file=sys.stderr)
            continue
        if not sc:
            continue
        judged += 1
        if isinstance(sc.get("fit"), (int, float)):
            fits.append(sc["fit"])
        if isinstance(sc.get("benefit"), (int, float)):
            benefits.append(sc["benefit"])

    print(f"  LLM JUDGE (n={judged} skill turns):")
    if fits:
        print(f"    fit:     mean {statistics.mean(fits):.3f}")
    if benefits:
        print(f"    benefit: mean {statistics.mean(benefits):.3f}")
    low = sum(1 for b in benefits if b < 0.4)
    if benefits:
        print(f"    low-benefit selections (<0.4): {low}/{len(benefits)} "
              f"— candidates for tuning the selector")


def main() -> None:
    p = argparse.ArgumentParser(description="Skill-selection efficacy judge.")
    p.add_argument("--log", default=os.environ.get("BRAIN_EVAL_LOG") or "eval/turns.jsonl")
    p.add_argument("--limit", type=int, default=30, help="Max skill turns to LLM-judge")
    p.add_argument("--dry-run", action="store_true", help="Deterministic comparison only")
    args = p.parse_args()
    _run(args.log, args.limit, args.dry_run)


if __name__ == "__main__":
    main()
