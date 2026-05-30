"""
eval/langfuse_audit.py — Audit Langfuse evaluator scores for flaws.

Pulls all scores over a recent window and, per evaluator, reports the score
distribution plus a set of "flaw" flags, and — critically — labels each
evaluator as LOCAL (submitted by our own code via the SDK / CLI, score
source=API) vs LANGFUSE-NATIVE (a UI-configured LLM-as-a-judge evaluator,
source=EVAL) vs HUMAN (source=ANNOTATION). Native-evaluator scores also carry
a config_id; local ones do not.

This answers two questions directly:
  1. Which evaluators are producing bad data (stuck, constant, half-filled,
     out of range, or barely covering traces)?
  2. For each, is it a LOCAL evaluator (fix in this repo) or a LANGFUSE-NATIVE
     one (fix in the Langfuse UI)?

Requires LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY (and optional LANGFUSE_HOST).

Usage:
  python -m eval.langfuse_audit                    # last 4 days
  python -m eval.langfuse_audit --since-days 7
  python -m eval.langfuse_audit --name voice.naturalness
  python -m eval.langfuse_audit --json out.json    # also dump raw aggregates
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass


_SOURCE_LABEL = {
    "API": "LOCAL (our code / CLI via SDK)",
    "EVAL": "LANGFUSE-NATIVE (UI evaluator)",
    "ANNOTATION": "HUMAN (manual annotation)",
}


def _fetch_scores(lf, since: datetime, hard_cap: int) -> list:
    """Page through scores.get_many over the window."""
    out: list = []
    page = 1
    while len(out) < hard_cap:
        resp = lf.api.scores.get_many(page=page, limit=100, from_timestamp=since)
        data = getattr(resp, "data", None) or []
        if not data:
            break
        out.extend(data)
        meta = getattr(resp, "meta", None)
        total_pages = getattr(meta, "total_pages", None) if meta else None
        if total_pages is not None and page >= total_pages:
            break
        page += 1
    return out[:hard_cap]


def _flaw_flags(values: list[float], n_total: int, n_null: int) -> list[str]:
    flags = []
    nums = [v for v in values if isinstance(v, (int, float))]
    if n_null and n_null / max(1, n_total) > 0.2:
        flags.append(f"NULL/NON-NUMERIC {n_null}/{n_total}")
    if not nums:
        if not flags:
            flags.append("NO-NUMERIC-VALUES")
        return flags
    distinct = set(round(v, 4) for v in nums)
    std = statistics.pstdev(nums) if len(nums) > 1 else 0.0
    half = sum(1 for v in nums if abs(v - 0.5) < 1e-6)
    oor = sum(1 for v in nums if v < 0.0 or v > 1.0)
    if len(nums) > 5 and std < 1e-6:
        flags.append(f"CONSTANT (all = {nums[0]:.3f})")
    if len(nums) > 10 and len(distinct) <= 2:
        flags.append(f"DEGENERATE ({len(distinct)} distinct values)")
    if half / len(nums) > 0.5:
        flags.append(f"STUCK-AT-0.5 ({half}/{len(nums)} = not-applicable sentinel)")
    if oor:
        flags.append(f"OUT-OF-RANGE ({oor} values outside 0..1)")
    return flags


def _run(since_days: float, name_filter: str | None, hard_cap: int, json_out: str | None) -> None:
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not pk or not sk:
        print("Error: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY", file=sys.stderr)
        sys.exit(1)

    from langfuse import Langfuse

    lf = Langfuse(
        public_key=pk,
        secret_key=sk,
        host=os.environ.get("LANGFUSE_HOST")
        or os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"),
    )

    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    print(f"Fetching scores since {since:%Y-%m-%d %H:%M UTC} (cap {hard_cap})...")
    scores = _fetch_scores(lf, since, hard_cap)
    print(f"Pulled {len(scores)} score records.\n")
    if not scores:
        return

    # Group by (name, source)
    groups: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"values": [], "n_null": 0, "config_ids": set(), "comments": [], "envs": set()}
    )
    for s in scores:
        nm = getattr(s, "name", "?") or "?"
        if name_filter and nm != name_filter:
            continue
        src = getattr(s, "source", "?") or "?"
        g = groups[(nm, src)]
        val = getattr(s, "value", None)
        if isinstance(val, (int, float)):
            g["values"].append(float(val))
        else:
            g["n_null"] += 1
        cid = getattr(s, "config_id", None)
        if cid:
            g["config_ids"].add(cid)
        if getattr(s, "environment", None):
            g["envs"].add(s.environment)
        c = getattr(s, "comment", None)
        if c and len(g["comments"]) < 3:
            g["comments"].append(c[:100])

    report = []
    for (nm, src), g in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        vals = g["values"]
        n_total = len(vals) + g["n_null"]
        flags = _flaw_flags(vals, n_total, g["n_null"])
        nums = vals
        row = {
            "name": nm,
            "source": src,
            "source_label": _SOURCE_LABEL.get(src, src),
            "n": n_total,
            "mean": round(statistics.mean(nums), 3) if nums else None,
            "std": round(statistics.pstdev(nums), 3) if len(nums) > 1 else 0.0,
            "min": round(min(nums), 3) if nums else None,
            "max": round(max(nums), 3) if nums else None,
            "n_distinct": len(set(round(v, 4) for v in nums)),
            "config_ids": sorted(g["config_ids"]),
            "environments": sorted(g["envs"]),
            "flaws": flags,
            "sample_comments": g["comments"],
        }
        report.append(row)

    # Print: flagged evaluators first
    report.sort(key=lambda r: (not r["flaws"], r["name"]))
    print("=" * 78)
    print(f"{'EVALUATOR':<28} {'SOURCE':<28} {'N':>5} {'MEAN':>6} {'STD':>6} {'DIST':>5}")
    print("=" * 78)
    for r in report:
        print(
            f"{r['name']:<28} {r['source_label']:<28} {r['n']:>5} "
            f"{str(r['mean']):>6} {str(r['std']):>6} {r['n_distinct']:>5}"
        )
        if r["flaws"]:
            for f in r["flaws"]:
                print(f"    ⚠  {f}")
    print("=" * 78)

    flagged = [r for r in report if r["flaws"]]
    print(f"\n{len(flagged)} of {len(report)} evaluator/source groups flagged.")
    if flagged:
        print("\nLikely-flawed evaluators (and where to fix them):")
        for r in flagged:
            where = "Langfuse UI" if r["source"] == "EVAL" else (
                "this repo" if r["source"] == "API" else "n/a (human)"
            )
            print(f"  • {r['name']} [{r['source_label']}] → fix in {where}: {'; '.join(r['flaws'])}")

    if json_out:
        with open(json_out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nWrote raw aggregates to {json_out}")


# ── Offline mode: audit a Langfuse observations export (.jsonl / .json) ─────────

_SCORE_PREFIXES = (
    "voice.", "grounding.", "self_model.", "critic.", "thought.", "emotion.",
    "judge.", "pipeline.", "novelty.", "faithfulness.", "safety.",
)


def _load_export(path: str) -> list[dict]:
    if path.endswith(".jsonl"):
        out = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return out
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("data", [])


def _audit_file(path: str, json_out: str | None) -> None:
    """Audit a Langfuse observations export where scores are flattened as dotted
    columns whose values are lists (a trace may carry >1 score under one name)."""
    recs = _load_export(path)
    print(f"Loaded {len(recs)} records from {path}.")
    cols = set()
    for r in recs:
        for k in r:
            if any(k.startswith(p) for p in _SCORE_PREFIXES):
                cols.add(k)

    report, empty = [], []
    for name in sorted(cols):
        vals, recs_with, multi, maxlen = [], 0, 0, 0
        for r in recs:
            v = r.get(name)
            if v in (None, ""):
                continue
            if not isinstance(v, list):
                v = [v]
            nums = [float(x) for x in v if isinstance(x, (int, float))]
            if nums:
                recs_with += 1
                maxlen = max(maxlen, len(nums))
                if len(nums) > 1:
                    multi += 1
                vals.extend(nums)
        if not vals:
            empty.append(name)
            continue
        n = len(vals)
        std = statistics.pstdev(vals) if n > 1 else 0.0
        mean = statistics.mean(vals)
        distinct = len(set(round(v, 3) for v in vals))
        half = sum(1 for v in vals if abs(v - 0.5) < 1e-6) / n
        flags = []
        if std < 1e-6:
            flags.append(f"CONSTANT={vals[0]:.2f}")
        if half > 0.4:
            flags.append(f"STUCK-AT-0.5={100 * half:.0f}%")
        if mean < 0.35:
            flags.append("LOW-MEAN")
        if distinct <= 3 and n > 20:
            flags.append(f"LOW-VARIANCE({distinct} distinct)")
        if any(v < 0 or v > 1 for v in vals):
            flags.append("OUT-OF-RANGE")
        if multi:
            flags.append(f"MULTI-SCORED({multi} traces — local+native overlap?)")
        report.append({
            "name": name, "recs": recs_with, "n_values": n, "mean": round(mean, 3),
            "std": round(std, 3), "min": round(min(vals), 2), "max": round(max(vals), 2),
            "distinct": distinct, "pct_half": round(100 * half, 1), "flaws": flags,
        })

    report.sort(key=lambda r: (not r["flaws"], r["name"]))
    print("=" * 86)
    print(f"{'EVALUATOR':<26}{'recs':>5}{'mean':>7}{'std':>7}{'min':>6}{'max':>6}{'dist':>5}{'%0.5':>6}")
    print("=" * 86)
    for r in report:
        print(f"{r['name']:<26}{r['recs']:>5}{r['mean']:>7.3f}{r['std']:>7.3f}"
              f"{r['min']:>6.2f}{r['max']:>6.2f}{r['distinct']:>5}{r['pct_half']:>5.0f}%")
        for fl in r["flaws"]:
            print(f"    ⚠  {fl}")
    print("=" * 86)
    flagged = [r for r in report if r["flaws"]]
    print(f"\n{len(flagged)} of {len(report)} populated evaluators flagged.")
    if empty:
        print(f"\nEvaluators present in the schema but PRODUCED NO SCORES (didn't run / "
              f"not submitting): {', '.join(empty)}")
    print(
        "\nNote: this export flattens scores as columns and does not carry the score "
        "`source`, so local-vs-native can't be read directly. MULTI-SCORED traces (>1 "
        "value under one name) indicate the same trace was scored more than once — the "
        "local CLI/SDK and a native UI evaluator overlapping. Use the live `--since-days` "
        "mode for an authoritative source split."
    )
    if json_out:
        with open(json_out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nWrote raw aggregates to {json_out}")


def main() -> None:
    p = argparse.ArgumentParser(description="Audit Langfuse evaluator scores for flaws.")
    p.add_argument("--from-file", dest="from_file", default=None,
                   help="Audit a Langfuse observations export (.jsonl/.json) instead of the live API")
    p.add_argument("--since-days", type=float, default=4.0, help="Look back N days (default 4)")
    p.add_argument("--name", default=None, help="Only audit this score name")
    p.add_argument("--cap", type=int, default=5000, help="Max score records to pull")
    p.add_argument("--json", dest="json_out", default=None, help="Dump raw aggregates to this path")
    args = p.parse_args()
    if args.from_file:
        _audit_file(args.from_file, args.json_out)
    else:
        _run(args.since_days, args.name, args.cap, args.json_out)


if __name__ == "__main__":
    main()
