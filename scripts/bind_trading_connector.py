"""
Bind the trading MCP connectors to the trading agents (boundary move).

The brain is domain-agnostic: the trading vertical lives in a separate trading
app, reached over MCP connectors. There are TWO connectors with different blast
radius, and they bind to different agents:

  • "trading"          — full read/WRITE surface (log decisions, post briefings,
                         edit the watchlist, stress_test_thesis, …). Bound to the
                         MAIN trading agent ONLY (mandate id "trading").
  • "trading-readonly" — read-only surface (get_quote, get_indicators,
                         get_portfolio, scan_watchlist, check_contradictions,
                         review_journal, review_signals, scan_source,
                         find_mispricing). NO mutation, NO stress_test_thesis.
                         Bound to the six reasoning/debate agents so they can pull
                         live data during analysis but can never write or recurse.

Both connectors are registered via env (BRAIN_CMA_MCP_SERVERS) with their tokens
in BRAIN_CMA_MCP_TRADING_TOKEN / BRAIN_CMA_MCP_TRADING_READONLY_TOKEN (both = the
app's TRADING_MCP_SECRET). See brain/clusters/trading/README.md.

The motor cortex intersects the org-level connector allowlist with the *bound
agent's* permissions (`_mode_policy` -> `set_connector_filter`), so a binding is
just the agent's permission overrides:

    {"motor_user_connectors": "<connector>", "motor_self_connectors": "<connector>"}

Connector names are newline-separated; here each agent gets exactly one name.
Existing permission keys on the agent are preserved (this merges, never clobbers).

The same binding is settable over the owner API at provision time — the Scheduler
App's installAgents() can do it directly instead of running this script:

    PUT /v1/agents/<persona>.<mandate_id>
    {"permissions": {"motor_user_connectors": "trading-readonly",
                     "motor_self_connectors": "trading-readonly"}}

(unknown keys are dropped; values are re-bounded against the org ceiling at read
time, so a stale value can never widen access).

Usage:
    # apply the whole intended topology (idempotent): main -> trading,
    # the six debate mandates -> trading-readonly. Skips unknown trad mandates.
    python -m scripts.bind_trading_connector

    python -m scripts.bind_trading_connector --dry-run   # show, change nothing

    # escape hatch — bind one explicit agent to one explicit connector:
    python -m scripts.bind_trading_connector --agent-id <persona>.<mandate_id> \
        --connector trading-readonly

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


# The six reasoning/debate mandates get the READ-ONLY connector. Keyed by mandate
# id (the part after the dot) so the mapping is persona-independent — the bull may
# be the_visionary today and someone else tomorrow; the mandate is what's stable.
DEBATE_MANDATES = frozenset(
    {
        "trading_bull",
        "trading_bear",
        "trading_risk",
        "trading_pm",
        "trading_mispricing",
        "trading_reflection",
    }
)
# The write-capable main trading agent(s). The live deployment uses
# "day_trading_analyst" (agent the_analyst.day_trading_analyst); "trading" is the
# name the README/boundary docs use for the mandate, kept here for robustness.
MAIN_MANDATES = frozenset({"day_trading_analyst", "trading"})

READONLY_CONNECTOR = "trading-readonly"
FULL_CONNECTOR = "trading"

_KEYS = ("motor_user_connectors", "motor_self_connectors")


def _connector_for(mandate_id: str) -> str | None:
    """The connector a 'trad…' mandate should be bound to, or None to skip.

    Fail-safe: only the known writer mandate(s) get the WRITE connector; the six
    known debate mandates get the read-only connector; any other trad-ish mandate
    is left alone (don't guess a write grant)."""
    mid = (mandate_id or "").lower()
    if mid in DEBATE_MANDATES:
        return READONLY_CONNECTOR
    if mid in MAIN_MANDATES:
        return FULL_CONNECTOR
    return None


def _bind(agents, agent_id: str, connector: str, *, dry_run: bool) -> int:
    row = agents.get(agent_id)
    if row is None:
        print(f"[skip] unknown agent {agent_id!r}", file=sys.stderr)
        return 1
    cur = row.get("permissions") if isinstance(row.get("permissions"), dict) else {}
    merged = {**cur, **{k: connector for k in _KEYS}}
    if merged == cur:
        print(f"[ok] {agent_id}: already bound to connector {connector!r}")
        return 0
    print(
        f"{agent_id}: {dict((k, cur.get(k)) for k in _KEYS)} -> "
        f"{dict((k, merged[k]) for k in _KEYS)}"
    )
    if dry_run:
        return 0
    agents.set_permissions(agent_id, merged)
    print(f"[done] {agent_id} bound to connector {connector!r}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Bind the trading MCP connectors (full vs read-only) to the trading agents."
    )
    ap.add_argument(
        "--agent-id",
        help="<persona>.<mandate_id>; if given, bind ONLY this agent (needs --connector)",
    )
    ap.add_argument(
        "--connector",
        help="connector name for the explicit --agent-id (e.g. trading / trading-readonly)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print the changes without writing")
    args = ap.parse_args(argv)

    from brain import agents

    # Escape hatch: explicit single-agent binding.
    if args.agent_id:
        if not args.connector:
            print("--connector is required with --agent-id", file=sys.stderr)
            return 2
        return _bind(agents, args.agent_id, args.connector, dry_run=args.dry_run)

    # Default: apply the whole intended topology across every trading agent.
    rc = 0
    matched = False
    for a in agents.list_agents():
        mid = str(a.get("mandate_id") or "")
        if "trad" not in mid.lower():
            continue
        matched = True
        connector = _connector_for(mid)
        if connector is None:
            print(
                f"[skip] {a['agent_id']}: mandate {mid!r} is neither the main trading "
                f"agent nor a known debate mandate — bind explicitly with "
                f"--agent-id/--connector if intended.",
                file=sys.stderr,
            )
            continue
        rc = _bind(agents, a["agent_id"], connector, dry_run=args.dry_run) or rc

    if not matched:
        print(
            "No trading agents found (no mandate id contains 'trad').\n"
            "List agents with: python -c \"from brain import agents; "
            "print([a['agent_id'] for a in agents.list_agents()])\"",
            file=sys.stderr,
        )
        return 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
