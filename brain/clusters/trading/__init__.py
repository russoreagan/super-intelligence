"""Advise-only day-trading capability for the brain.

This package gives the brain a set of READ-ONLY trading tools: market data
(via the official Alpaca MCP server, with a keyless yfinance fallback),
technical indicators, a persistent decision journal with a reflection loop,
and four analytical capabilities (watchlist scan, contradiction surfacing,
thesis stress-test, mispricing detection).

HARD CONSTRAINT — ADVISE ONLY. Nothing in this package ever places an order,
cancels an order, closes a position, or moves money. The Alpaca MCP client
(``alpaca_mcp_client``) exposes only an allow-listed set of read-only tools and
hard-blocks every state-changing tool; the recommended Alpaca key is read-only
scoped as the broker-side guarantee. The whole capability is dark unless
``trading_enabled`` is set in settings.

See the approved plan for the full design.
"""

from __future__ import annotations

# Read-only Alpaca MCP tool names the brain is allowed to call. Anything not in
# this set — in particular every order/position/account-write tool — is refused
# by AlpacaMCPClient before it can reach the server. Kept here as the single
# source of truth so tests can assert no write tool is ever reachable.
READ_ONLY_ALPACA_TOOLS: frozenset[str] = frozenset(
    {
        # account (read)
        "get_account_info",
        "get_account_config",
        "get_portfolio_history",
        "get_account_activities",
        "get_account_activities_by_type",
        # orders / positions (read)
        "get_orders",
        "get_order_by_id",
        "get_order_by_client_id",
        "get_all_positions",
        "get_open_position",
        # watchlists (read)
        "get_watchlists",
        "get_watchlist_by_id",
        # assets / calendar
        "get_all_assets",
        "get_asset",
        "get_option_contracts",
        "get_option_contract",
        "get_calendar",
        "get_clock",
        "get_corporate_action_announcements",
        "get_corporate_action_announcement",
        # stock data
        "get_stock_bars",
        "get_stock_quotes",
        "get_stock_trades",
        "get_stock_latest_bar",
        "get_stock_latest_quote",
        "get_stock_latest_trade",
        "get_stock_snapshot",
        "get_most_active_stocks",
        "get_market_movers",
        # crypto data (deferred at the tool layer, but reads are harmless)
        "get_crypto_bars",
        "get_crypto_quotes",
        "get_crypto_trades",
        "get_crypto_latest_bar",
        "get_crypto_latest_quote",
        "get_crypto_latest_trade",
        "get_crypto_snapshot",
        "get_crypto_latest_orderbook",
        # options data
        "get_option_bars",
        "get_option_trades",
        "get_option_latest_trade",
        "get_option_latest_quote",
        "get_option_snapshot",
        "get_option_chain",
        "get_option_exchange_codes",
        # corporate actions / news
        "get_corporate_actions",
        "get_news",
    }
)

# Every state-changing / money-moving Alpaca MCP tool. The brain must NEVER call
# any of these. Listed explicitly so a test can assert the two sets are disjoint
# and that none of these is ever exposed.
BLOCKED_ALPACA_TOOLS: frozenset[str] = frozenset(
    {
        "place_stock_order",
        "place_crypto_order",
        "place_option_order",
        "replace_order_by_id",
        "cancel_order_by_id",
        "cancel_all_orders",
        "close_position",
        "close_all_positions",
        "exercise_options_position",
        "do_not_exercise_options_position",
        "update_account_config",
        "create_watchlist",
        "update_watchlist_by_id",
        "delete_watchlist_by_id",
        "add_asset_to_watchlist_by_id",
        "remove_asset_from_watchlist_by_id",
    }
)
