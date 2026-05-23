"""
eval/compare.py — compare LLM-call cost and quality with predict-and-surprise
gating ON vs OFF.

Reads recent `turn` records from eval/turns.jsonl, segregates them by whether
BRAIN_DISABLE_PREDICT_GATING was set during the run (heuristic: presence of
`llm_calls_saved > 0` indicates gating was ON), and reports:
  - Total LLM calls per group
  - Avg LLM calls per turn
  - Avg selected-draft critic overall (quality proxy)
  - LLM calls saved (cumulative)

Usage:
    uv run python -m eval.compare
    uv run python -m eval.compare --log eval/turns.jsonl
    uv run python -m eval.compare --gating on,off  # filter to specific modes
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


def load_turns(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") == "turn":
            out.append(rec)
    return out


def selected_overall(turn: dict) -> float | None:
    for d in turn.get("draft_scores", []) or []:
        if d.get("selected"):
            try:
                return float(d.get("overall", 0.5))
            except Exception:
                return 0.5
    return None


def bucket_by_gating(turns: list[dict]) -> dict[str, list[dict]]:
    """Group turns by gating mode using llm_calls_saved as proxy."""
    on, off = [], []
    for t in turns:
        saved = int(t.get("llm_calls_saved", 0) or 0)
        bypass = int(t.get("gating_bypassed_count", 0) or 0)
        # Gating was active if anything was saved or bypass logic ran
        if saved > 0 or bypass > 0:
            on.append(t)
        else:
            off.append(t)
    return {"on": on, "off": off}


def summarize(label: str, turns: list[dict]) -> dict:
    if not turns:
        return {"label": label, "count": 0}
    calls = [int(t.get("llm_calls", 0) or 0) for t in turns]
    saved = [int(t.get("llm_calls_saved", 0) or 0) for t in turns]
    quality = [s for s in (selected_overall(t) for t in turns) if s is not None]
    return {
        "label": label,
        "count": len(turns),
        "total_llm_calls": sum(calls),
        "avg_llm_calls": round(statistics.mean(calls), 2) if calls else 0.0,
        "total_llm_calls_saved": sum(saved),
        "avg_quality": round(statistics.mean(quality), 3) if quality else None,
        "quality_n": len(quality),
    }


def render(report: dict) -> str:
    lines = [
        f"\n=== Predict-and-surprise gating comparison ===",
        f"Source: {report['source']}",
        f"Total turns: {report['total_turns']}",
        "",
    ]
    for grp in report["groups"]:
        if not grp.get("count"):
            lines.append(f"[{grp['label']:>3}] (no turns)")
            continue
        lines.append(f"[{grp['label']:>3}] {grp['count']:>4} turns | "
                     f"total_calls={grp['total_llm_calls']:>4} | "
                     f"avg/turn={grp['avg_llm_calls']:>5} | "
                     f"saved={grp['total_llm_calls_saved']:>3} | "
                     f"quality={grp['avg_quality']} (n={grp['quality_n']})")

    # Comparison verdict
    on = next((g for g in report["groups"] if g["label"] == "on"), None)
    off = next((g for g in report["groups"] if g["label"] == "off"), None)
    if on and off and on.get("count") and off.get("count"):
        call_reduction = (off["avg_llm_calls"] - on["avg_llm_calls"]) / off["avg_llm_calls"]
        lines.append("")
        lines.append(f"Avg call reduction: {call_reduction * 100:.1f}%")
        if on.get("avg_quality") is not None and off.get("avg_quality") is not None:
            q_delta = on["avg_quality"] - off["avg_quality"]
            verdict = "no quality drop" if q_delta >= -0.05 else f"quality dropped {q_delta:+.2f}"
            lines.append(f"Quality delta: {q_delta:+.3f} ({verdict})")
        pass_cri = call_reduction >= 0.25 and (
            on.get("avg_quality") is None
            or off.get("avg_quality") is None
            or (on["avg_quality"] - off["avg_quality"]) >= -0.05
        )
        lines.append(f"Pass criterion (≥25% reduction, ≤0.05 quality drop): "
                     f"{'PASS' if pass_cri else 'FAIL'}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare predict-and-surprise gating on vs off")
    parser.add_argument("--log", default="eval/turns.jsonl", help="Path to JSONL turn log")
    parser.add_argument("--gating", default="on,off",
                        help="Comma-separated subset of {on, off} to report")
    args = parser.parse_args()

    path = Path(args.log)
    turns = load_turns(path)
    if not turns:
        print(f"No turn records found in {path}. Run a session first.")
        return 1

    requested = {g.strip() for g in args.gating.split(",") if g.strip()}
    buckets = bucket_by_gating(turns)
    groups = [summarize(label, ts) for label, ts in buckets.items()
              if label in requested]

    report = {
        "source": str(path),
        "total_turns": len(turns),
        "groups": groups,
    }
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
