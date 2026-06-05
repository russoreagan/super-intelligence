"""TradingSubsystem — MotorSubsystem hooks for the trading layer.

before_plan : re-inject Russ's own past lessons (and a short note about open
              theses) into the planner prompt when the task looks trading-related.
              Only Russ's data is injected — never external text.
after_job   : safety net that auto-resolves any open prediction whose threshold
              has since been hit, so continuity survives if a call wasn't logged
              explicitly.

ADVISE-ONLY: nothing here trades.
"""

from __future__ import annotations

import logging

from brain.clusters.motor_subsystem import MotorSubsystem

from . import compaction, journal, reflection
from .market_data import MarketData

logger = logging.getLogger(__name__)

_TRADING_HINTS = (
    "trade", "trading", "stock", "ticker", "watchlist", "portfolio", "position",
    "thesis", "rsi", "macd", "bullish", "bearish", "buy", "sell", "hedge", "options",
)


class TradingSubsystem(MotorSubsystem):
    def __init__(self, market_data: MarketData | None = None, hippocampus=None) -> None:
        self._md = market_data or MarketData()
        self._hippocampus = hippocampus

    @property
    def name(self) -> str:
        return "trading"

    def _looks_trading(self, text: str) -> bool:
        t = (text or "").lower()
        if any(h in t for h in _TRADING_HINTS):
            return True
        # match any open-thesis symbol mentioned by name
        return any(
            rec.get("symbol", "").lower() in t for rec in journal.get_records(status="open")
        )

    async def before_plan(self, task_description: str, router) -> str:
        if not self._looks_trading(task_description):
            return ""
        opens = journal.get_records(status="open")
        # Determine symbols involved so we can fetch era memory too
        symbols = {rec.get("symbol", "") for rec in opens}
        if not opens and not compaction.era_summary_lessons():
            return ""
        lines = ["[Trading memory — your own prior calls and lessons]"]
        # Era summaries first — condensed long-term patterns
        for sym in list(symbols)[:3]:
            for era_text in compaction.era_summary_lessons(sym)[-1:]:
                lines.append(f"  {era_text}")
        # Recent open theses + their individual lessons
        for rec in opens[:5]:
            sym = rec.get("symbol", "")
            lines.append(f"- OPEN {sym}: {rec.get('prediction','')}")
            for lesson in journal.last_lessons(sym, limit=1):
                lines.append(f"    lesson: {lesson}")
        return "\n".join(lines) if len(lines) > 1 else ""

    async def after_job(self, goal, steps, results, success, router=None) -> None:
        """Auto-resolve open predictions whose threshold has been hit."""
        try:
            for rec in journal.get_records(status="open"):
                thresholds = rec.get("entry_threshold") or {}
                if not thresholds:
                    continue
                symbol = rec.get("symbol", "")
                quote = await self._md.quote(symbol)
                price = quote.get("price")
                if price is None:
                    continue
                exit_above = thresholds.get("exit_above")
                stop_below = thresholds.get("stop_below")
                hit = (exit_above is not None and price >= float(exit_above)) or (
                    stop_below is not None and price <= float(stop_below)
                )
                if not hit:
                    continue
                bench = rec.get("benchmark", "QQQ")
                bench_quote = await self._md.quote(bench)
                await reflection.reflect_and_resolve(
                    rec["id"],
                    price_at_resolve=price,
                    benchmark_at_resolve=bench_quote.get("price"),
                    router=router,
                    note="auto-resolved: threshold hit",
                    hippocampus=self._hippocampus,
                )
                logger.info("[trading] auto-resolved %s @ %s", symbol, price)
            # Run journal compaction after any resolution (requires router).
            # This is where the summarization cascade fires.
            if router is not None:
                await compaction.compact_journal(router)
                await compaction.compact_journal_md(router)
        except Exception as e:  # pragma: no cover - safety net must never raise
            logger.debug("[trading] after_job auto-resolve failed: %s", e)
