#!/usr/bin/env python3
"""
Invalidate stale muscle-memory procedures (one-off maintenance).

Muscle memory (brain/clusters/motor_memory.py) replays a stored procedure
verbatim — planner bypassed — once a goal matches at sim >= 0.90 with
use_count >= 2. When a procedure goes stale (e.g. the 2026-07-03 debate plans
whose step 1 re-fetched live quotes on every "Round N: audit their claims"
prompt), the only runtime demotion is a prediction divergence. This script is
the operator's lever: list procedures whose goal matches a regex, then reset
their use_count to 0 (drops them below the open-loop threshold until they
re-earn it) or delete them outright.

Reads the same LanceDB the brain writes: <root>/episodes, table "procedures",
where <root> is one persona's second-brain directory (what SECOND_BRAIN_PATH
points at for that persona — per-persona under the tenant volume in hosted
mode). Pass the episodes dir itself or its parent; both work.

Usage (dry-run lists matches, mutating needs an explicit flag):
  python scripts/invalidate_procedures.py --pattern "audit their claims" /data/tenants/<org>/personas/<persona>
  python scripts/invalidate_procedures.py --pattern "(?i)round \\d|AAPL" --reset ROOT [ROOT ...]
  python scripts/invalidate_procedures.py --pattern "..." --delete ROOT

Run on Railway as a one-off against the prod volume. Stop the brain first if
deleting — LanceDB handles concurrent readers, but a mid-run save can resurrect
a just-deleted row's twin.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def _episodes_dir(root: str) -> Path:
    p = Path(root).expanduser()
    return p if p.name == "episodes" else p / "episodes"


def _open_table(root: str):
    import lancedb

    d = _episodes_dir(root)
    if not d.exists():
        print(f"  !! no episodes dir at {d} — skipping")
        return None
    db = lancedb.connect(str(d))
    if "procedures" not in db.table_names():
        print(f"  !! no procedures table in {d} — skipping")
        return None
    return db.open_table("procedures")


def _first_tool(steps_json: str) -> str:
    try:
        steps = json.loads(steps_json or "[]")
        return steps[0].get("tool", "?") if steps else "-"
    except Exception:
        return "?"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("roots", nargs="+", help="persona second-brain dir(s) (or episodes dir)")
    ap.add_argument("--pattern", required=True, help="regex matched against procedure goals")
    ap.add_argument(
        "--reset",
        action="store_true",
        help="reset matches' use_count to 0 (below the open-loop threshold)",
    )
    ap.add_argument("--delete", action="store_true", help="delete matching procedures")
    args = ap.parse_args()
    if args.reset and args.delete:
        ap.error("--reset and --delete are mutually exclusive")
    try:
        rx = re.compile(args.pattern, re.IGNORECASE)
    except re.error as e:
        ap.error(f"bad --pattern: {e}")

    total_matched = 0
    for root in args.roots:
        print(f"\n== {root}")
        table = _open_table(root)
        if table is None:
            continue
        rows = table.search().limit(10_000).to_list()
        matched = [r for r in rows if rx.search(str(r.get("goal", "")))]
        print(f"   {len(rows)} procedure(s), {len(matched)} matching {args.pattern!r}")
        for r in matched:
            print(
                f"   [{r.get('id')}] uses={r.get('use_count', 0)} "
                f"recorded={str(r.get('recorded_at', ''))[:19]} "
                f"step1={_first_tool(r.get('steps'))} :: {str(r.get('goal', ''))[:90]}"
            )
        total_matched += len(matched)
        if not matched:
            continue
        ids = [str(r["id"]) for r in matched]
        where = " OR ".join(f"id = '{i}'" for i in ids)
        if args.delete:
            table.delete(where)
            print(f"   -> DELETED {len(ids)} procedure(s)")
        elif args.reset:
            table.update(where=where, values={"use_count": 0})
            print(f"   -> reset use_count to 0 on {len(ids)} procedure(s)")
        else:
            print("   (dry-run — pass --reset or --delete to act)")

    print(f"\n{total_matched} matching procedure(s) across {len(args.roots)} root(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
