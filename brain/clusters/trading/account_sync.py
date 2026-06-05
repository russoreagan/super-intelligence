"""Read-only account sync — pull the user's real holdings + fills into the brain.

Uses ONLY read-only Alpaca MCP tools (via AlpacaMCPClient, which hard-blocks every
write tool) to populate the local files the analytical layer already reads:
  - portfolio.json     ← get_account_info + get_all_positions
  - execution_log.jsonl ← get_orders (filled)

There is no order/trade code path here. With a read-only-scoped LIVE key the brain
sees real holdings/fills/performance and still cannot trade.
"""

from __future__ import annotations

import logging
from datetime import UTC

from . import store

logger = logging.getLogger(__name__)


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def sync_portfolio(client) -> dict:
    """Refresh portfolio.json from the broker. Returns the written portfolio dict."""
    if client is None or not client.available:
        return {"error": "alpaca_unavailable"}
    account = await client.call("get_account_info", {})
    positions = await client.call("get_all_positions", {})
    if isinstance(account, dict) and "error" in account:
        return account

    holdings = []
    rows = positions if isinstance(positions, list) else positions.get("positions", []) if isinstance(positions, dict) else []
    for p in rows or []:
        if not isinstance(p, dict):
            continue
        holdings.append(
            {
                "symbol": str(p.get("symbol", "")).upper(),
                "shares": _f(p.get("qty", p.get("quantity"))),
                "avg_cost": _f(p.get("avg_entry_price", p.get("avg_cost"))),
                "market_value": _f(p.get("market_value")),
                "unrealized_pl": _f(p.get("unrealized_pl")),
            }
        )
    portfolio = {
        "updated_at": _iso_now(),
        "cash": _f((account or {}).get("cash")),
        "equity": _f((account or {}).get("equity")),
        "holdings": holdings,
        "source": "alpaca",
    }
    store.save_portfolio(portfolio)
    logger.info("[account_sync] portfolio refreshed: %d holdings", len(holdings))
    return portfolio


async def sync_executions(client, *, limit: int = 100) -> int:
    """Append any new filled orders to execution_log.jsonl. Returns count added."""
    if client is None or not client.available:
        return 0
    orders = await client.call("get_orders", {"status": "closed", "limit": limit})
    rows = orders if isinstance(orders, list) else orders.get("orders", []) if isinstance(orders, dict) else []
    existing = {r.get("order_id") for r in store.load_executions() if r.get("order_id")}
    added = 0
    for o in rows or []:
        if not isinstance(o, dict):
            continue
        filled_qty = _f(o.get("filled_qty"))
        oid = o.get("id") or o.get("order_id")
        if filled_qty <= 0 or oid in existing:
            continue
        store.append_execution(
            {
                "ts": o.get("filled_at") or o.get("submitted_at"),
                "symbol": str(o.get("symbol", "")).upper(),
                "action": str(o.get("side", "")).lower(),
                "shares": filled_qty,
                "price": _f(o.get("filled_avg_price")),
                "reason": "synced from broker",
                "order_id": oid,
            }
        )
        added += 1
    if added:
        logger.info("[account_sync] %d new fills appended", added)
    return added


def _iso_now() -> str:
    import time
    from datetime import datetime

    return datetime.fromtimestamp(time.time(), tz=UTC).isoformat()
