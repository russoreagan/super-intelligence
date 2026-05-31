"""
RelationshipMonitor — deterministic relationship-system metrics from turns.jsonl.

No LLM. Turns the per-turn relationship instrumentation (added to TurnTrace) into
the aggregate answers the §2.4 / §6.7 research bets actually need. Right now those
counters are write-only; this reads them.

CLI:
    python -m eval.relationship_monitor [--log PATH] [--session SID] [--tail N] [--json]

Metrics computed (all deterministic):
  - disclosure_fire_rate          fraction of turns that injected a disclosure opportunity
  - disclosure_reciprocation_rate of fired disclosures, fraction the user's sentiment rose after
  - style_note_rate               fraction of turns that injected a style-synchrony note
  - style_register_variety        distinct register labels seen (detector actually varying?)
  - oxt_connected_rate            fraction of turns OXT cleared the "connected" threshold
  - reunion_boost_turns           count of turns a reunion boost was applied (>1.0)
  - bond_trajectory               first/last/min/max bond across the window
  - affection_trajectory          first/last/min/max affection across the window
  - warmth_by_stage               mean entity warmth proxy grouped by familiarity tier
  - empathy_by_stage              mean selected-draft empathy score grouped by tier
  - reciprocation_lift            mean user-sentiment change after disclosure vs. baseline turns

These let you answer: does disclosure fire where it should? does it actually produce
reciprocation? does bond accumulate and recover? is warmth calibrated to relationship
depth? — without any LLM scoring.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from eval.report import DEFAULT_LOG, load_turns


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def compute_relationship_metrics(turns: list[dict]) -> dict:
    """Compute deterministic relationship metrics over a list of merged turns."""
    n = len(turns)
    if n == 0:
        return {"turns": 0}

    disclosure_fired = [t for t in turns if t.get("disclosure_fired")]
    style_noted = [t for t in turns if t.get("style_note_emitted")]
    oxt_connected = [t for t in turns if t.get("oxt_connected_reached")]
    reunion_turns = [
        t for t in turns if float(t.get("reunion_boost_applied", 1.0) or 1.0) != 1.0
    ]

    # Reciprocation: of fired disclosures with a resolved proxy, how many rose?
    resolved = [
        t for t in disclosure_fired if t.get("disclosure_reciprocated") is not None
    ]
    reciprocated = [t for t in resolved if t.get("disclosure_reciprocated")]

    # Register variety — is the detector actually producing different labels?
    registers = {t.get("style_register") for t in style_noted if t.get("style_register")}

    # Bond / affection trajectories
    bonds = [float(t["bond"]) for t in turns if t.get("bond") is not None]
    affections = [int(t["affection"]) for t in turns if t.get("affection") is not None]

    def _traj(xs):
        if not xs:
            return None
        return {"first": xs[0], "last": xs[-1], "min": min(xs), "max": max(xs)}

    # Warmth / empathy grouped by familiarity tier. Warmth proxy = user_sentiment
    # is the USER's signal; for the ENTITY's warmth we use selected_empathy_score
    # (how empathetic the chosen draft was) as the available proxy.
    warmth_by_stage: dict[str, list[float]] = defaultdict(list)
    empathy_by_stage: dict[str, list[float]] = defaultdict(list)
    for t in turns:
        tier = t.get("familiarity_tier") or "unknown"
        emp = t.get("selected_empathy_score")
        if emp is not None:
            empathy_by_stage[tier].append(float(emp))
        # affection_label is the entity's calibrated warmth band; map to ordinal
        lbl = t.get("affection_label")
        if lbl:
            warmth_by_stage[tier].append(_AFFECTION_ORDINAL.get(lbl, 0))

    # Reciprocation lift: mean user-sentiment delta on the turn AFTER a disclosure
    # vs. the mean turn-to-turn sentiment delta overall (baseline).
    sentiments = [float(t.get("user_sentiment", 0.0) or 0.0) for t in turns]
    overall_deltas = [sentiments[i] - sentiments[i - 1] for i in range(1, n)]
    post_disclosure_deltas = []
    for i in range(1, n):
        if turns[i - 1].get("disclosure_fired"):
            post_disclosure_deltas.append(sentiments[i] - sentiments[i - 1])

    return {
        "turns": n,
        "disclosure_fire_rate": round(len(disclosure_fired) / n, 4),
        "disclosure_fired_count": len(disclosure_fired),
        "disclosure_reciprocation_rate": (
            round(len(reciprocated) / len(resolved), 4) if resolved else None
        ),
        "disclosure_resolved_count": len(resolved),
        "style_note_rate": round(len(style_noted) / n, 4),
        "style_register_variety": sorted(registers),
        "oxt_connected_rate": round(len(oxt_connected) / n, 4),
        "reunion_boost_turns": len(reunion_turns),
        "bond_trajectory": _traj(bonds),
        "affection_trajectory": _traj(affections),
        "warmth_by_stage": {
            k: round(_mean(v), 3) for k, v in warmth_by_stage.items() if v
        },
        "empathy_by_stage": {
            k: round(_mean(v), 3) for k, v in empathy_by_stage.items() if v
        },
        "reciprocation_lift": {
            "post_disclosure_mean_delta": (
                round(_mean(post_disclosure_deltas), 4) if post_disclosure_deltas else None
            ),
            "baseline_mean_delta": round(_mean(overall_deltas), 4) if overall_deltas else None,
        },
    }


# Affection band → ordinal, for a coarse "is warmth calibrated to stage?" read.
_AFFECTION_ORDINAL = {
    "guarded": 0,
    "cool": 1,
    "neutral": 2,
    "friendly": 3,
    "warm": 4,
    "close": 5,
}


def _print_report(m: dict) -> None:
    if m.get("turns", 0) == 0:
        print("No turns found.")
        return
    print(f"\n=== Relationship metrics ({m['turns']} turns) ===\n")
    print(f"  disclosure fire rate      : {m['disclosure_fire_rate']:.1%} "
          f"({m['disclosure_fired_count']} turns)")
    rr = m["disclosure_reciprocation_rate"]
    print(f"  reciprocation rate        : "
          f"{rr:.1%}" if rr is not None else "  reciprocation rate        : n/a (no resolved)")
    print(f"  style-note rate           : {m['style_note_rate']:.1%}")
    print(f"  style register variety    : {m['style_register_variety'] or '(none)'}")
    print(f"  OXT 'connected' rate      : {m['oxt_connected_rate']:.1%}")
    print(f"  reunion-boost turns       : {m['reunion_boost_turns']}")
    print(f"  bond trajectory           : {m['bond_trajectory']}")
    print(f"  affection trajectory      : {m['affection_trajectory']}")
    print(f"  warmth band by tier       : {m['warmth_by_stage']}")
    print(f"  empathy by tier           : {m['empathy_by_stage']}")
    lift = m["reciprocation_lift"]
    print(f"  reciprocation lift        : post-disclosure Δ={lift['post_disclosure_mean_delta']} "
          f"vs baseline Δ={lift['baseline_mean_delta']}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Deterministic relationship-system metrics.")
    ap.add_argument("--log", type=str, default=None, help="Path to turns.jsonl")
    ap.add_argument("--session", type=str, default=None, help="Filter to one session id")
    ap.add_argument("--tail", type=int, default=None, help="Only the last N turns")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a report")
    args = ap.parse_args()

    log_path = Path(args.log) if args.log else DEFAULT_LOG
    turns = load_turns(log_path, session_id=args.session, tail=args.tail)
    metrics = compute_relationship_metrics(turns)
    if args.json:
        print(json.dumps(metrics, indent=2))
    else:
        _print_report(metrics)


if __name__ == "__main__":
    main()
