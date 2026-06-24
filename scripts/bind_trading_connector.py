"""
Bind the trading MCP connector to the trading mandate (boundary move).

The brain is domain-agnostic: the trading vertical lives in a separate trading
app, reached over an MCP connector named "trading" (registered via
BRAIN_CMA_MCP_SERVERS + BRAIN_CMA_MCP_TRADING_TOKEN). This script scopes that
connector to the trading AGENT so only the trading agent ever sees its tools.

The motor cortex intersects the org-level connector allowlist with the *bound
agent's* permissions (`_mode_policy` -> `set_connector_filter`), so the binding
is simply the agent's permission overrides:

    {"motor_user_connectors": "<connector>", "motor_self_connectors": "<connector>"}

Connector names are newline-separated; one name is just "trading". Existing
permission keys on the agent are preserved (this merges, never clobbers).

Usage:
    # auto-discover the trading agent (mandate id contains "trad"):
    python -m scripts.bind_trading_connector

    # or target an explicit agent and/or connector name:
    python -m scripts.bind_trading_connector --agent-id <persona>.<mandate_id>
    python -m scripts.bind_trading_connector --connector trading
    python -m scripts.bind_trading_connector --dry-run   # show, change nothing

Run INSIDE the brain's org context (the pod, or with the same Supabase/org env
the pod uses) — agent rows are org-level data behind RLS.
"""

from __future__ import annotations

import argparse
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _discover(agents) -> list[str]:
    """Agent ids whose mandate id looks like trading (best-effort auto-target)."""
    out = []
    for a in agents.list_agents():
        mid = str(a.get("mandate_id") or "").lower()
        if "trad" in mid:
            out.append(a["agent_id"])
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Bind the trading MCP connector to the trading mandate.")
    ap.add_argument("--agent-id", help="<persona>.<mandate_id>; auto-discovered if omitted")
    ap.add_argument("--connector", default="trading", help="connector name (default: trading)")
    ap.add_argument("--dry-run", action="store_true", help="print the change without writing")
    args = ap.parse_args(argv)

    from brain import agents

    targets = [args.agent_id] if args.agent_id else _discover(agents)
    if not targets:
        print(
            "No trading agent found. Pass --agent-id <persona>.<mandate_id>.\n"
            "List agents with: python -c \"from brain import agents; "
            "print([a['agent_id'] for a in agents.list_agents()])\"",
            file=sys.stderr,
        )
        return 1

    keys = ("motor_user_connectors", "motor_self_connectors")
    rc = 0
    for agent_id in targets:
        row = agents.get(agent_id)
        if row is None:
            print(f"[skip] unknown agent {agent_id!r}", file=sys.stderr)
            rc = 1
            continue
        cur = row.get("permissions") if isinstance(row.get("permissions"), dict) else {}
        merged = {**cur, **{k: args.connector for k in keys}}
        if merged == cur:
            print(f"[ok] {agent_id}: already bound to connector {args.connector!r}")
            continue
        print(f"{agent_id}: {dict((k, cur.get(k)) for k in keys)} -> "
              f"{dict((k, merged[k]) for k in keys)}")
        if args.dry_run:
            continue
        agents.set_permissions(agent_id, merged)
        print(f"[done] {agent_id} bound to connector {args.connector!r}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
