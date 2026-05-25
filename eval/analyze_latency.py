#!/usr/bin/env python3
"""Quick latency analysis from eval/turns.jsonl.

Usage:
  python eval/analyze_latency.py           # full report
  python eval/analyze_latency.py --last N  # only last N turns
  python eval/analyze_latency.py --since SESSION_ID

Compares brain latency to single-LLM baseline if BaselineRunner has run.
Enable that via the "Eval" button in the UI header or
BRAIN_EVAL_BASELINE=true BRAIN_EVAL_INTENSIVE=true at start time.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
LOG = ROOT / "eval" / "turns.jsonl"


def load_turns(last: int | None = None, since_session: str | None = None) -> list[dict]:
    turns = []
    with open(LOG) as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "turn":
                continue
            if since_session and d.get("session_id") != since_session:
                continue
            turns.append(d)
    if last:
        turns = turns[-last:]
    return turns


def stats(label: str, vals: list[float]) -> None:
    if not vals:
        print(f"  {label:24} (no data)")
        return
    n = len(vals)
    s = sorted(vals)
    avg = statistics.mean(vals)
    med = statistics.median(vals)
    p90 = s[int(0.9 * (n - 1))] if n > 1 else s[0]
    print(f"  {label:24} n={n:4}  avg={avg:6.2f}s  med={med:6.2f}s  p90={p90:6.2f}s  min={min(vals):5.2f}  max={max(vals):6.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--last", type=int, default=None, help="only analyse the last N turns")
    ap.add_argument("--since", type=str, default=None, help="only turns from this session_id")
    args = ap.parse_args()

    turns = load_turns(last=args.last, since_session=args.since)
    if not turns:
        print("No turn rows in eval/turns.jsonl.")
        sys.exit(1)

    label = f"last {args.last}" if args.last else f"all {len(turns)}"
    if args.since:
        label += f" / session {args.since}"

    print(f"=== {label.upper()} TURNS ===")
    stats("brain elapsed_s", [t.get("elapsed_s", 0) for t in turns if t.get("elapsed_s", 0) > 0])
    stats("brain llm_calls", [t.get("llm_calls", 0) for t in turns if t.get("llm_calls", 0) > 0])

    # Baseline-paired turns (BaselineRunner has filled baseline_latency_s)
    paired = [(t.get("elapsed_s", 0), t.get("baseline_latency_s", 0)) for t in turns
              if t.get("baseline_latency_s", 0) > 0]
    print(f"\n=== BASELINE COMPARISON ({len(paired)} paired turns) ===")
    if not paired:
        print("  No baseline data. Click the 'Eval' button in the UI header,")
        print("  or set BRAIN_EVAL_BASELINE=true BRAIN_EVAL_INTENSIVE=true and restart.")
    else:
        stats("brain elapsed_s",    [b for b, _ in paired])
        stats("baseline elapsed_s", [s for _, s in paired])
        ratios = [b / s for b, s in paired if s > 0.1]
        if ratios:
            r = sorted(ratios)
            avg = statistics.mean(ratios)
            med = statistics.median(ratios)
            p90 = r[int(0.9 * (len(r) - 1))] if len(r) > 1 else r[0]
            print(f"\n  brain/baseline ratio   avg={avg:.2f}x  med={med:.2f}x  p90={p90:.2f}x")
            print("  (>1 = brain is slower; <1 = brain is faster)")

    print("\n=== LLM CALLS DISTRIBUTION ===")
    cc = Counter([t.get("llm_calls", 0) for t in turns])
    for k in sorted(cc):
        bar = "█" * min(cc[k], 60)
        print(f"  {k} calls: {cc[k]:4} {bar}")


if __name__ == "__main__":
    main()
