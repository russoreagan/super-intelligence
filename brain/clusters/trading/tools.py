"""TradingTools — the dispatch-facing facade the motor cortex calls.

Each method returns a concise ``[status] result`` string (the convention the
planner/LLM consumes) and, where useful, emits a rich table/chart card to the UI
via present.py. All tools are READ-ONLY analysis; none place trades.

Crypto is deferred: crypto symbols return a clear ``[blocked]`` message.
"""

from __future__ import annotations

import json
import logging

from . import capabilities, indicators, journal, present, reflection
from .market_data import MarketData, closes

logger = logging.getLogger(__name__)

_CRYPTO_HINT = {"BTC", "ETH", "DOGE", "SOL", "XRP", "ADA", "LTC", "BCH", "USDT", "USDC"}


def _is_crypto(symbol: str) -> bool:
    s = symbol.upper()
    return "/" in s or s in _CRYPTO_HINT or s.endswith("USD") and s[:-3] in _CRYPTO_HINT


class TradingTools:
    """Holds the market-data facade + router; exposes the dispatchable tools."""

    # tools handled here (used by motor_cortex to extend the registry)
    TOOL_NAMES = (
        "get_quote",
        "get_history",
        "get_indicators",
        "get_option_chain",
        "scan_watchlist",
        "log_decision",
        "resolve_decision",
        "review_journal",
        "check_contradictions",
        "stress_test_thesis",
        "find_mispricing",
        "sync_account",
        "start_watchlist_stream",
        "stop_watchlist_stream",
    )

    def __init__(
        self,
        *,
        alpaca_client=None,  # paper key — market data only
        alpaca_account_client=None,  # live read-only key — account sync only
        router=None,
        hippocampus=None,
    ) -> None:
        self._md = MarketData(alpaca_client=alpaca_client)
        self._account_client = (
            alpaca_account_client if alpaca_account_client is not None else alpaca_client
        )
        self._router = router
        self._hippocampus = hippocampus
        self._stream = None  # set via set_stream() after construction

    def set_stream(self, stream) -> None:
        """Wire in the WatchlistStream instance (constructed but not started)."""
        self._stream = stream

    async def dispatch(self, tool: str, args: dict, turn_id: str = "") -> str:
        fn = getattr(self, f"_{tool}", None)
        if fn is None:
            return f"[error] Unknown trading tool: {tool}"
        try:
            return await fn(args, turn_id)
        except Exception as e:  # pragma: no cover
            logger.error("[trading] %s failed: %s", tool, e)
            return f"[error] {tool} failed: {e}"

    # ── data tools ────────────────────────────────────────────────────────────

    async def _get_quote(self, args: dict, turn_id: str) -> str:
        symbol = str(args.get("symbol", "")).upper().strip()
        if not symbol:
            return "[error] get_quote needs a symbol"
        if _is_crypto(symbol):
            return f"[blocked] crypto deferred — {symbol} not enabled in this build"
        q = await self._md.quote(symbol)
        if "error" in q:
            return f"[error] no quote for {symbol} ({q['error']})"
        await present.table(
            turn_id,
            f"{symbol} quote",
            ["symbol", "price", "change_pct", "source"],
            [[symbol, q.get("price"), q.get("change_pct"), q.get("source")]],
        )
        return f"[success] {symbol} {q.get('price')} ({q.get('change_pct')}% today) via {q.get('source')}"

    async def _get_history(self, args: dict, turn_id: str) -> str:
        symbol = str(args.get("symbol", "")).upper().strip()
        days = int(args.get("days", 120))
        if not symbol:
            return "[error] get_history needs a symbol"
        if _is_crypto(symbol):
            return f"[blocked] crypto deferred — {symbol} not enabled in this build"
        bars = await self._md.history(symbol, days=days)
        if not bars:
            return f"[error] no history for {symbol}"
        c = closes(bars)
        overlays = {
            "SMA50": list(indicators.sma(c, 50)),
            "SMA200": list(indicators.sma(c, 200)),
        }
        await present.chart(
            turn_id,
            present.candlestick_spec(
                symbol, bars[-days:], overlays={k: v[-days:] for k, v in overlays.items()}
            ),
        )
        return f"[success] {symbol}: {len(bars)} daily bars, latest close {c[-1]:.2f}"

    async def _get_indicators(self, args: dict, turn_id: str) -> str:
        symbol = str(args.get("symbol", "")).upper().strip()
        if not symbol:
            return "[error] get_indicators needs a symbol"
        if _is_crypto(symbol):
            return f"[blocked] crypto deferred — {symbol} not enabled in this build"
        bars = await self._md.history(symbol, days=250)
        if not bars:
            return f"[error] no data for {symbol}"
        snap = indicators.compute_all(closes(bars))
        await present.table(
            turn_id,
            f"{symbol} indicators",
            present.snapshot_columns(),
            [present.snapshot_row(symbol, snap)],
        )
        return f"[success] {symbol} indicators: " + json.dumps(
            {k: snap[k] for k in ("price", "rsi_14", "macd", "sma_50", "roc_10", "streak")},
            default=str,
        )

    async def _get_option_chain(self, args: dict, turn_id: str) -> str:
        symbol = str(args.get("symbol", "")).upper().strip()
        if not symbol:
            return "[error] get_option_chain needs a symbol"
        if _is_crypto(symbol):
            return "[blocked] options on crypto not supported in this build"
        chain = await self._md.option_chain(symbol, args.get("expiration"))
        if "error" in chain:
            return f"[error] no options for {symbol} ({chain['error']})"
        exp = chain.get("expiration", "?")
        n_calls = len(chain.get("calls", []) or [])
        n_puts = len(chain.get("puts", []) or [])
        return f"[success] {symbol} options exp {exp}: {n_calls} calls, {n_puts} puts"

    # ── continuity tools ────────────────────────────────────────────────────────

    async def _log_decision(self, args: dict, turn_id: str) -> str:
        did = journal.log_decision(
            symbol=str(args.get("symbol", "")),
            direction=str(args.get("direction", "long")),
            prediction=str(args.get("prediction", "")),
            rationale=str(args.get("rationale", "")),
            confidence=float(args.get("confidence", 0.5)),
            indicators_at_open=args.get("indicators_at_open"),
            entry_threshold=args.get("entry_threshold"),
            benchmark=str(args.get("benchmark", "QQQ")),
            benchmark_at_open=args.get("benchmark_at_open"),
            turn_id=turn_id,
            hippocampus=self._hippocampus,
        )
        return f"[success] logged decision {did}"

    async def _resolve_decision(self, args: dict, turn_id: str) -> str:
        did = str(args.get("decision_id", ""))
        if not did:
            return "[error] resolve_decision needs a decision_id"
        price = args.get("price_at_resolve")
        bench = args.get("benchmark_at_resolve")
        # auto-fetch if not provided
        if price is None:
            rec = next((r for r in journal.get_records() if r.get("id") == did), None)
            if rec is None:
                return f"[error] decision not found: {did}"
            q = await self._md.quote(rec.get("symbol", ""))
            price = q.get("price")
            if bench is None:
                bq = await self._md.quote(rec.get("benchmark", "QQQ"))
                bench = bq.get("price")
        if price is None:
            return "[error] could not determine resolve price"
        res = await reflection.reflect_and_resolve(
            did,
            price_at_resolve=float(price),
            benchmark_at_resolve=float(bench) if bench is not None else None,
            router=self._router,
            hippocampus=self._hippocampus,
        )
        if "error" in res:
            return f"[error] {res['error']}"
        return f"[success] resolved {did}: {res.get('outcome_label')} ({res.get('raw_return_pct')}% raw, alpha {res.get('alpha_vs_benchmark_pct')})"

    async def _review_journal(self, args: dict, turn_id: str) -> str:
        rows = journal.review_journal(
            symbol=args.get("symbol"),
            status=args.get("status"),
            limit=int(args.get("limit", 10)),
        )
        if not rows:
            return "[success] journal empty (no matching entries)"
        cols = ["symbol", "status", "prediction", "outcome", "raw_return_pct", "lesson"]
        table_rows = []
        for r in rows:
            res = r.get("resolution") or {}
            table_rows.append(
                [
                    r.get("symbol"),
                    r.get("status"),
                    r.get("prediction"),
                    res.get("outcome_label", "—"),
                    res.get("raw_return_pct", "—"),
                    res.get("lesson", ""),
                ]
            )
        await present.table(turn_id, "Decision journal", cols, table_rows)
        return f"[success] {len(rows)} journal entries (see table)"

    # ── analytical tools ──────────────────────────────────────────────────────

    async def _scan_watchlist(self, args: dict, turn_id: str) -> str:
        alerts = await capabilities.scan_watchlist(self._md)
        if not alerts:
            return "[success] watchlist scanned: no triggers fired"
        cols = ["symbol", "fired", "price", "rsi_14", "prior(win/total)", "last_lesson"]
        rows = [
            [
                a["symbol"],
                ", ".join(w.get("name", w.get("trigger", "")) for w in a["fired"]),
                a["snapshot"].get("price"),
                a["snapshot"].get("rsi_14"),
                f"{a['prior_wins']}/{a['prior_count']}",
                a["last_lesson"],
            ]
            for a in alerts
        ]
        await present.table(turn_id, "Watchlist alerts", cols, rows)
        return f"[success] {len(alerts)} watchlist alert(s) fired (see table)"

    async def _check_contradictions(self, args: dict, turn_id: str) -> str:
        items = await capabilities.check_contradictions(self._md)
        if not items:
            return "[success] no contradictions found"
        await present.table(
            turn_id,
            "Contradictions",
            ["symbol", "kind", "detail"],
            [[i["symbol"], i["kind"], i["detail"]] for i in items],
        )
        return f"[success] {len(items)} contradiction(s) (see table)"

    async def _stress_test_thesis(self, args: dict, turn_id: str) -> str:
        result = await capabilities.stress_test_thesis(
            symbol=str(args.get("symbol", "")),
            thesis_text=str(args.get("thesis_text", "")),
            md=self._md,
            router=self._router,
        )
        if result.get("status") == "blocked":
            return (
                f"{result['message']} (author the stress-test role prompts in trading/prompts.py)"
            )
        await present.table(
            turn_id,
            f"Stress-test: {result.get('symbol')}",
            ["view", "summary"],
            [
                ["Bull", result.get("bull", "")],
                ["Bear", result.get("bear", "")],
                ["Risk", result.get("risk", "")],
                ["Rating", result.get("rating", "")],
                ["Breaks story", result.get("breaks_story", "")],
                ["Hedge", result.get("hedge", "")],
            ],
        )
        return f"[success] stress-test: {result.get('rating')} — breaks if {result.get('breaks_story')}"

    async def _find_mispricing(self, args: dict, turn_id: str) -> str:
        result = await capabilities.find_mispricing(
            symbol=str(args.get("symbol", "")),
            md=self._md,
            router=self._router,
        )
        if result.get("status") == "blocked":
            return f"{result['message']} (author MISPRICING_SYSTEM in trading/prompts.py)"
        return f"[success] {result.get('symbol')} mispricing: {result.get('analysis')}"

    async def _start_watchlist_stream(self, args: dict, turn_id: str) -> str:
        if self._stream is None:
            return "[error] stream not available (trading layer not fully initialised)"
        if self._stream._running:
            subscribed = sorted(self._stream._subscribed)
            return f"[success] stream already running — watching {subscribed}"
        if not self._stream.available:
            return "[error] no Alpaca keys configured"
        await self._stream.start()
        return "[success] watchlist stream started — real-time alerts active"

    async def _stop_watchlist_stream(self, args: dict, turn_id: str) -> str:
        if self._stream is None or not self._stream._running:
            return "[success] stream is not running"
        await self._stream.stop()
        return "[success] watchlist stream stopped"

    async def _sync_account(self, args: dict, turn_id: str) -> str:
        """Pull live holdings + fills from the broker into the local data files.

        Uses the dedicated live account client (read-only key) — never the
        market-data client — so the paper key is never sent to account endpoints.
        """
        from . import account_sync

        if self._account_client is None or not self._account_client.available:
            return "[error] account client unavailable — set ALPACA_LIVE_API_KEY / ALPACA_LIVE_SECRET_KEY"
        portfolio = await account_sync.sync_portfolio(self._account_client)
        if "error" in portfolio:
            return f"[error] portfolio sync failed: {portfolio['error']}"
        added = await account_sync.sync_executions(self._account_client)
        holdings = len(portfolio.get("holdings", []))
        return f"[success] account synced: {holdings} positions, {added} new fills added"
