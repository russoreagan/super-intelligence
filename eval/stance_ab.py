"""
eval/stance_ab.py — Do stance directives change a drafter's APPROACH, or only its tone?

The go/no-go gate for the approach-competition plan's Phase 1 (pre-tool stage): a
one-line stance directive (~15 tokens) injected into a drafter's context must make
the SAME drafter attack the SAME input differently — different information posture,
different structure of attack — not merely warmer or terser phrasing. If directives
only move register, the stance library is decorative and the pre-tool competition
should not be built on top of it.

Deliberately a CONTROLLED probe, not a full pipeline run: one fixed drafter cell
(same system prompt, same model, same minimal context), with ONLY the stance block
varying between conditions — exactly the block _fragment_block_for_host renders.
Full-pipeline runs add recall/executive variance that would smear the one variable
this instrument exists to isolate.

Protocol:
  1. For each probe input × condition (control = no stance, plus K stances), call
     the drafter once. Same turn seed per probe so nothing else varies.
  2. A sonnet judge classifies each pair of outputs for the same probe:
     identical | tone_only | approach_differs — with one line of evidence.
  3. Gate: fraction of STANCE-vs-STANCE pairs judged approach_differs must clear
     --gate (default 0.5). Control-vs-stance pairs are reported separately as the
     weaker "does the directive do anything at all" signal.

Two tiers (--tier): "directive" (name + first sentence — the competition-tier form)
and "body" (directive + full on-disk body — the winner-tier form). Comparing the
two runs answers whether the body earns its ~775 tokens (plan: two-tier check).

Non-destructive: no session, no memory, no wiring; Langfuse/RunPod disabled.
Requires ANTHROPIC key in .env. Embeddings are NOT needed (directives and bodies
are read from disk/index; embed failures during warm are tolerated by design).

Usage:
  python -m eval.stance_ab --smoke          # 2 probes × (control + 2 stances)
  python -m eval.stance_ab                  # full: 4 probes × (control + 4 stances)
  python -m eval.stance_ab --tier body      # winner-tier injection
  python -m eval.stance_ab --axis method    # method stances instead of info
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

# Inputs chosen so stances SHOULD diverge: premise-laden, volatile, underspecified,
# and task-shaped asks — the turn types the info axis exists to disambiguate.
PROBES = [
    "Should I switch my project from Postgres to Mongo? We keep hitting slow queries.",
    "What's the best laptop to buy right now?",
    "Clean up my notes file when you get a chance.",
    "Why did my deploy break the login flow?",
]

INFO_STANCES = [
    "stance-answer-from-known",
    "stance-ask-dont-guess",
    "stance-verify-the-premise",
    "stance-do-and-report",
]
METHOD_STANCES = [
    "constraint-hardness-testing",
    "creativity-assumption-excavator",
    "decision-premortem-analysis",
    "investigation-counter-hypothesis",
]

JUDGE_SYSTEM = """You compare two assistant replies to the SAME user message.
Classify how they differ:
- "identical": same content and structure, trivial wording variation.
- "tone_only": same approach and substance; only register/warmth/length differs.
- "approach_differs": they ATTACK the problem differently — different information
  posture (one answers directly, one asks a clarifying question, one questions the
  premise, one proposes an action), different structure of reasoning, or different
  judgment about what the user actually needs.
Be strict: politeness, hedging, or formatting changes are tone_only. A different
first move, a different assumption challenged, or a different deliverable is
approach_differs.
Return ONLY JSON: {"verdict": "identical"|"tone_only"|"approach_differs",
"evidence": "<one sentence naming the concrete difference>"}"""


def _isolate_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env", override=True)
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "RUNPOD_API_KEY"):
        os.environ.pop(k, None)


async def _selector(router):
    """Warmed SkillSelector with the .claude/skills index-sync neutralized (it can
    rewrite the real index JSON; this instrument must stay read-only)."""
    import brain.skills._import_humanity as ih
    from brain.clusters.skill_selector import SkillSelector

    ih.SOURCE_DIR = Path("/nonexistent-for-eval")
    sel = SkillSelector(router)
    await sel.warm_native_skills()
    return sel


def _stance_block(sel, sid: str, tier: str) -> str:
    """Exactly the injection the drafter path renders (frontal._fragment_block_for_host
    directive framing), plus the winner-tier body when tier="body"."""
    directive = sel.stance_directive(sid)
    block = (
        "Approach stance — adopt this angle of attack for this reply "
        f"(a thinking posture, not a tool): {directive}"
    )
    if tier == "body":
        body = sel.stance_body(sid)
        if body:
            block += f"\n\n{body[:6000]}"
    return block


def _drafter_content(probe: str, stance_block: str | None) -> str:
    """Minimal realistic drafting context: instruction JSON + optional stance block +
    the user message. Everything identical across conditions except the block."""
    parts = [
        'Drafting instruction: {"response_type": "informative", "target_length": '
        '"medium", "tone": "neutral", "key_points": [], "drafter_count": 1}',
    ]
    if stance_block:
        parts.append(stance_block)
    parts.append(f"User: {probe}")
    return "\n\n".join(parts)


async def _draft(router, probe: str, stance_block: str | None, turn_tag: str) -> str:
    from brain.clusters.frontal_prompts import DRAFTER_SYSTEMS

    return await router.call(
        "haiku",
        DRAFTER_SYSTEMS[0],  # drafter_A: the same cell for every condition
        [{"role": "user", "content": _drafter_content(probe, stance_block)}],
        cluster="frontal",
        cell="drafter_A",
        turn_id=f"stance_ab_{turn_tag}",
        max_tokens=768,
    )


async def _judge(router, probe: str, a: str, b: str, tag: str) -> dict:
    from brain.utils import safe_json_parse

    raw = await router.call(
        "sonnet",
        JUDGE_SYSTEM,
        [
            {
                "role": "user",
                "content": (
                    f"User message:\n{probe}\n\nReply A:\n{a}\n\nReply B:\n{b}\n\n"
                    "Classify the difference."
                ),
            }
        ],
        cluster="frontal",
        cell="stance_ab_judge",
        turn_id=f"stance_ab_judge_{tag}",
        max_tokens=200,
    )
    verdict = safe_json_parse(raw) or {}
    if verdict.get("verdict") not in ("identical", "tone_only", "approach_differs"):
        verdict = {"verdict": "unparseable", "evidence": raw[:160]}
    return verdict


async def run(args) -> int:
    _isolate_env()
    from brain.model_router import ModelRouter

    router = ModelRouter()
    sel = await _selector(router)

    stances = INFO_STANCES if args.axis == "info" else METHOD_STANCES
    probes = PROBES[:2] if args.smoke else PROBES
    stances = stances[:2] if args.smoke else stances
    conditions: list[tuple[str, str | None]] = [("control", None)] + [
        (sid, _stance_block(sel, sid, args.tier)) for sid in stances
    ]
    missing = [sid for sid, blk in conditions[1:] if not blk or "—" not in blk]
    if missing:
        print(f"FATAL: no directive resolved for {missing} — is the index warm?")
        return 2

    # 1. Draft every probe × condition concurrently.
    keys, tasks = [], []
    for pi, probe in enumerate(probes):
        for cond, block in conditions:
            keys.append((pi, cond))
            tasks.append(_draft(router, probe, block, f"p{pi}_{cond}"))
    outs = await asyncio.gather(*tasks, return_exceptions=True)
    drafts: dict[tuple[int, str], str] = {}
    for k, o in zip(keys, outs, strict=True):
        if isinstance(o, Exception):
            print(f"FATAL: draft failed for {k}: {o}")
            return 2
        drafts[k] = str(o)

    # 2. Judge pairs per probe: control-vs-stance (weak signal) + stance-vs-stance (the gate).
    pair_rows: list[dict] = []
    jkeys, jtasks = [], []
    for pi, probe in enumerate(probes):
        names = [c for c, _ in conditions]
        for a, b in itertools.combinations(names, 2):
            jkeys.append((pi, a, b))
            jtasks.append(_judge(router, probe, drafts[(pi, a)], drafts[(pi, b)], f"p{pi}_{a}_{b}"))
    jouts = await asyncio.gather(*jtasks, return_exceptions=True)
    for (pi, a, b), v in zip(jkeys, jouts, strict=True):
        if isinstance(v, Exception):
            v = {"verdict": "error", "evidence": str(v)[:160]}
        pair_rows.append(
            {
                "probe": probes[pi],
                "a": a,
                "b": b,
                "kind": "control" if "control" in (a, b) else "stance",
                **v,
            }
        )

    # 3. Score the gate.
    stance_pairs = [r for r in pair_rows if r["kind"] == "stance"]
    control_pairs = [r for r in pair_rows if r["kind"] == "control"]

    def _frac(rows, verdict):
        return sum(1 for r in rows if r["verdict"] == verdict) / len(rows) if rows else 0.0

    gate_frac = _frac(stance_pairs, "approach_differs")
    result = {
        "axis": args.axis,
        "tier": args.tier,
        "probes": len(probes),
        "stances": [c for c, _ in conditions[1:]],
        "stance_pairs": len(stance_pairs),
        "approach_differs_frac": round(gate_frac, 3),
        "tone_only_frac": round(_frac(stance_pairs, "tone_only"), 3),
        "control_approach_differs_frac": round(_frac(control_pairs, "approach_differs"), 3),
        "gate": args.gate,
        "verdict": "PASS" if gate_frac >= args.gate else "FAIL",
        "pairs": pair_rows,
        "drafts": {f"p{pi}:{cond}": text for (pi, cond), text in drafts.items()},
    }
    out_path = _REPO / "eval" / f"stance_ab_results_{args.axis}_{args.tier}.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    print(f"\n=== stance_ab — axis={args.axis} tier={args.tier} ===")
    print(f"stance-vs-stance pairs: {len(stance_pairs)}")
    print(f"  approach_differs: {result['approach_differs_frac']:.0%}")
    print(f"  tone_only:        {result['tone_only_frac']:.0%}")
    print(f"control-vs-stance approach_differs: {result['control_approach_differs_frac']:.0%}")
    for r in stance_pairs:
        mark = "✓" if r["verdict"] == "approach_differs" else " "
        print(f"  [{mark}] {r['a']} vs {r['b']} — {r['verdict']}: {r['evidence'][:100]}")
    print(f"\nGATE ({args.gate:.0%} approach_differs on stance pairs): {result['verdict']}")
    print(f"Full results: {out_path}")
    return 0 if result["verdict"] == "PASS" else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--smoke", action="store_true", help="2 probes × 2 stances")
    ap.add_argument("--axis", choices=("info", "method"), default="info")
    ap.add_argument("--tier", choices=("directive", "body"), default="directive")
    ap.add_argument("--gate", type=float, default=0.5)
    sys.exit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
