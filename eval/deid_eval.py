"""
De-identification gate — adversarial corpus eval.

The gate's CONTROL logic (fail-closed, all-three-required, parsing) is proven by
tests/test_deid_gate.py with a scripted router. This harness measures the other
half: does a REAL model actually classify correctly? It runs brain.deid_gate.DeidGate
over eval/deid_corpus.jsonl and reports the metric that matters most for privacy —
REJECT RECALL: of the cases that must be rejected, how many were? A miss there is a
leak. Grow the corpus over time; treat any drop in reject-recall as a regression.

Run:  python -m eval.deid_eval            # uses the configured ModelRouter
The scoring core (run_corpus) is model-agnostic — pass any object with the
ModelRouter.call interface, so it's unit-testable with a fake (see
tests/test_deid_eval.py).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from brain.deid_gate import DeidGate

_CORPUS = Path(__file__).resolve().parent / "deid_corpus.jsonl"


def load_corpus(path: Path = _CORPUS) -> list[dict]:
    cases = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


async def run_corpus(gate: DeidGate, cases: list[dict]) -> dict:
    """Run every case through the gate and score it. Returns a report dict with the
    confusion matrix, reject-recall / admit-recall / accuracy, and the list of
    misses (with which were leaks — an expected-reject that got admitted)."""
    tp = fp = tn = fn = 0  # 'positive' = reject (the privacy-protective action)
    misses = []
    for c in cases:
        res = await gate.filter(c["input"], source_id=c.get("id", ""), source_context=c.get("source_context"))
        predicted = "admit" if res.admitted else "reject"
        expected = c["expect"]
        if expected == "reject" and predicted == "reject":
            tn += 1  # correctly withheld
        elif expected == "reject" and predicted == "admit":
            fn += 1  # LEAK — expected reject, got admit
            misses.append({**c, "predicted": predicted, "leak": True, "stage": res.stage})
        elif expected == "admit" and predicted == "admit":
            tp += 1
        else:  # expected admit, got reject
            fp += 1  # over-rejection (safe but loses a good insight)
            misses.append({**c, "predicted": predicted, "leak": False, "stage": res.stage})

    n_reject = tn + fn
    n_admit = tp + fp
    return {
        "total": len(cases),
        "reject_recall": (tn / n_reject) if n_reject else None,  # ↑ = fewer leaks
        "admit_recall": (tp / n_admit) if n_admit else None,
        "accuracy": (tp + tn) / len(cases) if cases else None,
        "leaks": fn,  # the number that matters most — any > 0 is a privacy failure
        "over_rejections": fp,
        "misses": misses,
    }


def _format(report: dict) -> str:
    lines = [
        f"cases: {report['total']}",
        f"reject-recall: {report['reject_recall']:.0%}  (leaks: {report['leaks']})"
        if report["reject_recall"] is not None
        else "reject-recall: n/a",
        f"admit-recall:  {report['admit_recall']:.0%}  (over-rejections: {report['over_rejections']})"
        if report["admit_recall"] is not None
        else "admit-recall: n/a",
        f"accuracy:      {report['accuracy']:.0%}" if report["accuracy"] is not None else "",
    ]
    for m in report["misses"]:
        tag = "LEAK" if m["leak"] else "over-reject"
        lines.append(f"  [{tag}] {m['id']}: expected {m['expect']}, got {m['predicted']} @ {m['stage']}")
    return "\n".join(filter(None, lines))


async def _main() -> None:
    try:
        from brain.model_router import ModelRouter

        router = ModelRouter()
    except Exception as exc:  # no model configured / import failure
        print(f"[deid_eval] could not build ModelRouter ({exc}).")
        print("Configure a model and rerun: python -m eval.deid_eval")
        return
    report = await run_corpus(DeidGate(router), load_corpus())
    print(_format(report))


if __name__ == "__main__":
    asyncio.run(_main())
