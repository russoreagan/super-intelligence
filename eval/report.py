"""
Eval report CLI — reads turns.jsonl and prints a quality summary.

Usage:
    uv run python -m eval.report [--tail N] [--session SESSION_ID] [--log PATH]

Merges eval_patch records into turn records by turn_id on read.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from pathlib import Path

DEFAULT_LOG = Path("eval/turns.jsonl")


def load_turns(log_path: Path, session_id: str | None = None,
               tail: int | None = None, since_ts: float | None = None) -> list[dict]:
    """Read JSONL, merge patches into turns, return list of merged turns."""
    if not log_path.exists():
        return []

    raw_turns: dict[str, dict] = {}
    order: list[str] = []

    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if rec.get("type") == "turn":
                tid = rec.get("turn_id", "")
                if session_id and rec.get("session_id") != session_id:
                    continue
                if since_ts and rec.get("ts", 0) < since_ts:
                    continue
                raw_turns[tid] = rec
                order.append(tid)
            elif rec.get("type") == "eval_patch":
                tid = rec.get("turn_id", "")
                if tid in raw_turns:
                    raw_turns[tid].update({k: v for k, v in rec.items()
                                           if k not in ("type", "turn_id")})

    turns = [raw_turns[tid] for tid in order if tid in raw_turns]
    if tail:
        turns = turns[-tail:]
    return turns


def fmt_bar(value: float, width: int = 20) -> str:
    filled = int(round(value * width))
    return "█" * filled + "░" * (width - filled)


def fmt_score(v: float | None) -> str:
    if v is None:
        return "  —  "
    return f"{v:.2f}"


def report(turns: list[dict]) -> None:
    if not turns:
        print("No turns found in log.")
        return

    total = len(turns)
    scored = [t for t in turns if t.get("judge_scores")]
    baseline_turns = [t for t in turns if t.get("baseline_response")]
    memory_turns = [t for t in turns if t.get("memory_recalled")]

    print()
    print("═" * 60)
    print("  BRAIN EVAL REPORT")
    print("═" * 60)
    print(f"  Total turns:        {total}")
    print(f"  Scored turns:       {len(scored)}")
    print(f"  Baseline turns:     {len(baseline_turns)}")
    print(f"  Memory turns:       {len(memory_turns)}")
    print()

    # ── Quality summary ──────────────────────────────────────────────────
    if scored:
        # Only count numeric scores — non-applicable dimensions are now null and
        # must be excluded from aggregates rather than counted as 0 or 0.5.
        def _vals(key: str, *, only_recall: bool = False) -> list[float]:
            out = []
            for t in scored:
                if only_recall and not t.get("memory_recalled"):
                    continue
                v = t["judge_scores"].get(key)
                if isinstance(v, (int, float)):
                    out.append(v)
            return out

        brain_scores = _vals("brain_overall")
        base_scores = _vals("baseline_overall")
        deltas = _vals("delta")

        if brain_scores and base_scores and deltas:
            brain_avg = sum(brain_scores) / len(brain_scores)
            base_avg = sum(base_scores) / len(base_scores)
            delta_avg = sum(deltas) / len(deltas)

            wins = sum(1 for d in deltas if d > 0.1)
            losses = sum(1 for d in deltas if d < -0.1)

            blinded = sum(1 for t in scored if t["judge_scores"].get("judge_blinded"))
            tag = " (blinded)" if blinded == len(scored) else ""

            print(f"  QUALITY SCORES (judge-evaluated turns){tag}")
            print(f"  Brain avg:          {brain_avg:.3f}  {fmt_bar(brain_avg)}")
            print(f"  Baseline avg:       {base_avg:.3f}  {fmt_bar(base_avg)}")
            print(f"  Avg delta:          {delta_avg:+.3f}")
            print(f"  Brain wins  (>0.1): {wins}/{len(deltas)}")
            print(f"  Brain losses(<0.1): {losses}/{len(deltas)}")
            print()

        # Memory utilization (numeric only, on turns with recall)
        mem_scores = _vals("memory_utilization", only_recall=True)
        if mem_scores:
            print(f"  Memory utilization: {sum(mem_scores)/len(mem_scores):.3f} "
                  f"(avg on {len(mem_scores)} turns with recall)")

        # Personality consistency (numeric only)
        pers_scores = _vals("personality_consistency")
        if pers_scores:
            print(f"  Personality cons.:  {sum(pers_scores)/len(pers_scores):.3f}")
        print()

    # ── Draft scores (internal critic — always available) ────────────────
    draft_turns = [t for t in turns if t.get("draft_scores")]
    if draft_turns:
        coherences = [d.get("coherence", 0) for t in draft_turns
                      for d in t["draft_scores"] if d.get("selected")]
        tones = [d.get("tone_fit", 0) for t in draft_turns
                 for d in t["draft_scores"] if d.get("selected")]
        overalls = [d.get("overall", 0) for t in draft_turns
                    for d in t["draft_scores"] if d.get("selected")]
        if coherences:
            print("  INTERNAL CRITIC SCORES (selected drafts)")
            print(f"  Coherence avg:      {sum(coherences)/len(coherences):.3f}")
            print(f"  Tone fit avg:       {sum(tones)/len(tones):.3f}")
            print(f"  Overall avg:        {sum(overalls)/len(overalls):.3f}")
            print()

    # ── Per-cluster token usage ──────────────────────────────────────────
    cluster_totals: dict[str, dict] = defaultdict(lambda: {"in": 0, "out": 0, "calls": 0})
    for t in turns:
        for cluster, usage in (t.get("cluster_tokens") or {}).items():
            cluster_totals[cluster]["in"] += usage.get("in", 0)
            cluster_totals[cluster]["out"] += usage.get("out", 0)
            cluster_totals[cluster]["calls"] += usage.get("calls", 0)

    if cluster_totals:
        print("  CLUSTER TOKEN USAGE (all turns)")
        max_calls = max(v["calls"] for v in cluster_totals.values()) or 1
        for cluster in sorted(cluster_totals):
            u = cluster_totals[cluster]
            bar = fmt_bar(u["calls"] / max_calls, 15)
            print(f"  {cluster:<14} calls={u['calls']:>4}  in={u['in']:>7}  out={u['out']:>6}  {bar}")
        print()

    # ── Notable turns ────────────────────────────────────────────────────
    notable = [t for t in scored if abs(t["judge_scores"].get("delta", 0)) > 0.25]
    notable.sort(key=lambda t: abs(t["judge_scores"].get("delta", 0)), reverse=True)

    if notable:
        print("  NOTABLE TURNS  (|delta| > 0.25, top 5)")
        print()
        for t in notable[:5]:
            js = t["judge_scores"]
            delta = js.get("delta", 0)
            sign = "✓ brain wins" if delta > 0 else "✗ baseline wins"
            print(f"  [{sign}  delta={delta:+.2f}]")
            print(f"  Q: {t.get('user_input', '')[:80]}")
            print(f"  Brain:    {t.get('response', '')[:120]}")
            print(f"  Baseline: {t.get('baseline_response', '')[:120]}")
            reasoning = js.get("reasoning", "")
            if reasoning:
                print(f"  Judge:    {reasoning}")
            print()

    print("═" * 60)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Brain eval report")
    parser.add_argument("--tail", type=int, default=None, metavar="N",
                        help="Only show the last N turns")
    parser.add_argument("--session", type=str, default=None,
                        help="Filter by session_id")
    parser.add_argument("--since", type=float, default=None, metavar="DAYS",
                        help="Only show turns from the last N days (e.g. --since 7)")
    parser.add_argument("--log", type=str, default=None,
                        help="Path to turns.jsonl (default: eval/turns.jsonl)")
    args = parser.parse_args()

    since_ts = (time.time() - args.since * 86400) if args.since else None
    log_path = Path(args.log) if args.log else Path(
        os.environ.get("BRAIN_EVAL_LOG", str(DEFAULT_LOG))
    )
    turns = load_turns(log_path, session_id=args.session, tail=args.tail, since_ts=since_ts)
    report(turns)


if __name__ == "__main__":
    main()
