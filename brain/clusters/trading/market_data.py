"""Market data provider — Alpaca MCP (primary) with a keyless yfinance fallback.

Returns clean, structured data (no LLM in the loop) so the indicator math is
reliable. All access is READ-ONLY. Crypto is handled at the tool layer
(deferred); this module focuses on US stocks/ETFs + options.

Shape conventions:
- a "bar" is {"t": iso, "open", "high", "low", "close", "volume"}
- ``get_history`` returns a list of bars oldest-first
- ``closes(bars)`` extracts a float list for the indicator functions
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger(__name__)


def closes(bars: list[dict]) -> list[float]:
    return [float(b["close"]) for b in bars if b.get("close") is not None]


class Provider(Protocol):
    name: str

    async def quote(self, symbol: str) -> dict: ...
    async def history(self, symbol: str, days: int) -> list[dict]: ...
    async def option_chain(self, symbol: str, expiration: str | None) -> dict: ...


# ── Alpaca provider (via the read-only MCP client) ────────────────────────────


class AlpacaProvider:
    name = "alpaca"

    def __init__(self, client) -> None:
        self._client = client

    @property
    def available(self) -> bool:
        return bool(self._client and self._client.available)

    async def quote(self, symbol: str) -> dict:
        res = await self._client.call("get_stock_snapshot", {"symbols": symbol})
        if isinstance(res, dict) and "error" not in res:
            snap = res.get(symbol) or res.get("snapshot") or res
            latest = (snap or {}).get("latestTrade") or (snap or {}).get("latest_trade") or {}
            daily = (snap or {}).get("dailyBar") or (snap or {}).get("daily_bar") or {}
            price = latest.get("p") or latest.get("price") or daily.get("c") or daily.get("close")
            if price is not None:
                prev = daily.get("o") or daily.get("open")
                change_pct = (
                    round((float(price) - float(prev)) / float(prev) * 100.0, 2) if prev else None
                )
                return {
                    "symbol": symbol,
                    "price": float(price),
                    "change_pct": change_pct,
                    "source": self.name,
                }
        return {"error": "alpaca_quote_unavailable", "symbol": symbol}

    async def history(self, symbol: str, days: int) -> list[dict]:
        res = await self._client.call(
            "get_stock_bars",
            {"symbols": symbol, "timeframe": "1Day", "limit": days},
        )
        return _parse_alpaca_bars(res, symbol)

    async def option_chain(self, symbol: str, expiration: str | None) -> dict:
        args: dict = {"underlying_symbol": symbol}
        if expiration:
            args["expiration_date"] = expiration
        res = await self._client.call("get_option_chain", args)
        if isinstance(res, dict) and "error" in res:
            return res
        return {"symbol": symbol, "chain": res, "source": self.name}


def _parse_alpaca_bars(res, symbol: str) -> list[dict]:
    """Tolerant parse of Alpaca bar payloads into our bar shape."""
    if not isinstance(res, dict) or "error" in res:
        return []
    raw = res.get("bars", res)
    if isinstance(raw, dict):
        raw = raw.get(symbol, [])
    out: list[dict] = []
    for b in raw or []:
        try:
            out.append(
                {
                    "t": b.get("t") or b.get("timestamp"),
                    "open": float(b.get("o", b.get("open"))),
                    "high": float(b.get("h", b.get("high"))),
                    "low": float(b.get("l", b.get("low"))),
                    "close": float(b.get("c", b.get("close"))),
                    "volume": float(b.get("v", b.get("volume", 0)) or 0),
                }
            )
        except (TypeError, ValueError, KeyError):
            continue
    return out


# ── yfinance fallback (keyless) ───────────────────────────────────────────────


class YFinanceProvider:
    name = "yfinance"

    @property
    def available(self) -> bool:
        try:
            import yfinance  # noqa: F401
        except Exception:
            return False
        return True

    async def quote(self, symbol: str) -> dict:
        return await asyncio.get_event_loop().run_in_executor(None, self._quote_sync, symbol)

    def _quote_sync(self, symbol: str) -> dict:
        try:
            import yfinance as yf

            t = yf.Ticker(symbol)
            hist = t.history(period="2d")
            if hist is None or hist.empty:
                return {"error": "yfinance_no_data", "symbol": symbol}
            price = float(hist["Close"].iloc[-1])
            change_pct = None
            if len(hist) >= 2:
                prev = float(hist["Close"].iloc[-2])
                if prev:
                    change_pct = round((price - prev) / prev * 100.0, 2)
            return {
                "symbol": symbol,
                "price": price,
                "change_pct": change_pct,
                "source": self.name,
            }
        except Exception as e:
            return {"error": f"yfinance_quote_failed: {e}", "symbol": symbol}

    async def history(self, symbol: str, days: int) -> list[dict]:
        return await asyncio.get_event_loop().run_in_executor(
            None, self._history_sync, symbol, days
        )

    def _history_sync(self, symbol: str, days: int) -> list[dict]:
        try:
            import yfinance as yf

            # pad the window so indicators with long lookbacks (SMA200) fill in
            period_days = max(days, 1) + 320
            period = f"{period_days}d" if period_days <= 729 else "max"
            t = yf.Ticker(symbol)
            hist = t.history(period=period)
            if hist is None or hist.empty:
                return []
            out = []
            for ts, row in hist.iterrows():
                out.append(
                    {
                        "t": ts.isoformat(),
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": float(row.get("Volume", 0) or 0),
                    }
                )
            return out
        except Exception as e:
            logger.warning("[yfinance] history failed for %s: %s", symbol, e)
            return []

    async def option_chain(self, symbol: str, expiration: str | None) -> dict:
        return await asyncio.get_event_loop().run_in_executor(
            None, self._option_chain_sync, symbol, expiration
        )

    def _option_chain_sync(self, symbol: str, expiration: str | None) -> dict:
        try:
            import yfinance as yf

            t = yf.Ticker(symbol)
            exps = list(t.options or [])
            if not exps:
                return {"error": "no_options", "symbol": symbol}
            exp = expiration if expiration in exps else exps[0]
            chain = t.option_chain(exp)
            to_records = lambda df: df.to_dict("records") if df is not None else []  # noqa: E731
            return {
                "symbol": symbol,
                "expiration": exp,
                "expirations": exps,
                "calls": to_records(chain.calls),
                "puts": to_records(chain.puts),
                "source": self.name,
            }
        except Exception as e:
            return {"error": f"yfinance_options_failed: {e}", "symbol": symbol}


# ── Facade: try Alpaca, fall back to yfinance ─────────────────────────────────


class MarketData:
    """Provider facade. Alpaca first (if available), else yfinance."""

    def __init__(self, alpaca_client=None) -> None:
        self._alpaca = AlpacaProvider(alpaca_client) if alpaca_client is not None else None
        self._yf = YFinanceProvider()

    def _providers(self) -> list[Provider]:
        chain: list[Provider] = []
        if self._alpaca is not None and self._alpaca.available:
            chain.append(self._alpaca)
        if self._yf.available:
            chain.append(self._yf)
        return chain

    async def quote(self, symbol: str) -> dict:
        for p in self._providers():
            res = await p.quote(symbol)
            if isinstance(res, dict) and "error" not in res:
                return res
        return {"error": "no_provider", "symbol": symbol}

    async def history(self, symbol: str, days: int = 200) -> list[dict]:
        for p in self._providers():
            bars = await p.history(symbol, days)
            if bars:
                return bars
        return []

    async def option_chain(self, symbol: str, expiration: str | None = None) -> dict:
        for p in self._providers():
            res = await p.option_chain(symbol, expiration)
            if isinstance(res, dict) and "error" not in res:
                return res
        return {"error": "no_provider", "symbol": symbol}
