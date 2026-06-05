---
name: trading-analyst
description: Advise-only day-trading capability. Use for market data, watchlist scanning, indicator analysis, journal logging, and portfolio review. Never places orders.
category: trading
tier: 2
is_router: false
keywords: [trading, trade, stock, watchlist, portfolio, indicators, journal, quote, signal]
---

# Trading Analyst

Operational guide for the brain's advise-only day-trading capability. Covers when to use each tool, how the data files connect, indicator interpretation, and the workflow for a typical trading session. **Never place orders. Never move money. All tools are read-only analysis.**

---

## The data files (what the brain reads and writes)

| File | Owned by | What it is |
|------|----------|------------|
| `second_brain/trading/watchlist.json` | Russ (maintains) | Symbols to watch, entry/exit thresholds, indicator triggers, thesis per symbol |
| `second_brain/trading/portfolio.json` | Russ / account sync | Current holdings, cash, unrealised P&L |
| `second_brain/trading/execution_log.jsonl` | Russ / account sync | Real buys and sells — ground truth for measuring advice vs outcomes |
| `second_brain/trading/journal.jsonl` | Brain (writes) | Every prediction the brain has made: what, why, indicators at the time, outcome, lesson |
| `second_brain/trading/journal.md` | Brain (writes) | Human-readable mirror of the journal — one paragraph per resolved call |

The journal is the brain's **long-term memory for trading**. It is what turns individual observations into pattern recognition over time. Resolve predictions honestly, even the bad ones — the lesson recorded on a miss is worth more than the lesson on a win.

---

## Tool selection guide

### Getting data
- `get_quote(symbol)` — current price and daily change. Use before any analysis or when Russ asks about a specific stock.
- `get_history(symbol, days)` — daily OHLC bars, renders a candlestick chart with SMA50/200 overlays. Use when Russ wants to see the chart or before computing indicators manually.
- `get_indicators(symbol)` — full indicator snapshot: RSI, MACD, Bollinger, SMA, EMA, ROC, streak. Use before logging a decision or stress-testing a thesis.
- `get_option_chain(symbol, expiration)` — options chain summary. Use when Russ is considering an options position or wants to gauge implied volatility.

### Watchlist and alerts
- `scan_watchlist()` — checks every watchlist symbol against its `watch_indicators` triggers right now. Also surfaces historical hit-rate and the last lesson from similar conditions. Use at the start of a session or when Russ asks "anything firing today?"
- `start_watchlist_stream()` — opens the real-time websocket; from that point forward, alert cards appear automatically when a trigger fires on a new 1-minute bar. Use when Russ wants to monitor actively. Stays running until `stop_watchlist_stream()`.
- `stop_watchlist_stream()` — stops the live feed.
- `sync_account()` — pulls live positions and fills from the broker into the local files. Use at the start of a session when Russ wants current holdings reflected.

### The journal
- `log_decision(symbol, direction, prediction, rationale, confidence, indicators_at_open, entry_threshold, benchmark)` — record a prediction before acting. **Always log before Russ commits.** The rationale field is what gets scrutinised when the trade resolves.
- `resolve_decision(decision_id)` — close a prediction once the outcome is known. Fetches the live price automatically if none is provided, computes return and alpha vs benchmark, generates a lesson. Resolve even when the thesis is still open if Russ decides to exit.
- `review_journal(symbol, status, limit)` — query past predictions. Use to brief Russ on his history with a symbol before he takes a new position.

### Analysis
- `check_contradictions()` — surfaces any position where Russ's stated exit/stop has been breached but the position is still held. Run this regularly. The answer to "why haven't you exited?" is almost always worth examining.
- `stress_test_thesis(symbol, thesis_text)` — Bull/Bear/Risk/Synthesis debate. Run this *before* Russ commits, not after. If the bear case can't be answered, the position is undersized or should wait.
- `find_mispricing(symbol)` — identifies the gap between what the data suggests and what the market is pricing. Use when Russ has a contrarian read or wants to articulate the edge.

---

## Indicator interpretation (reference)

| Indicator | Oversold / Bearish signal | Overbought / Bullish signal | Notes |
|-----------|--------------------------|----------------------------|-------|
| RSI 14 | < 30 | > 70 | < 30 alone is not a buy signal — check direction and regime |
| MACD | Histogram turning negative; MACD crossing below signal | Histogram turning positive; MACD crossing above signal | Lag indicator — confirms trend, doesn't predict it |
| Bollinger Bands | Price touching lower band | Price touching upper band | Band width matters — narrow bands precede big moves |
| SMA 50/200 | Price below both; 50 crossing below 200 (death cross) | Price above both; 50 crossing above 200 (golden cross) | 200-day is the long-term regime filter |
| ROC 10 | Strongly negative | Strongly positive | Rate of change — momentum |
| Streak | Negative integer (consecutive down closes) | Positive integer | Useful for mean-reversion reads |
| price_vs_sma50 | Negative (price below SMA50) | Positive (price above SMA50) | Quick regime check |

**Rule of thumb:** No single indicator justifies a call. An oversold RSI in a downtrend is just a downtrend. Look for confluence — RSI oversold *and* price at a support level *and* MACD histogram turning — before treating a signal as actionable.

---

## Typical session workflow

1. `sync_account()` — get current holdings
2. `scan_watchlist()` — see what's firing
3. For any symbol of interest: `get_indicators(symbol)` + `get_history(symbol)` for the chart
4. If considering a position: `stress_test_thesis(symbol, "...")` — get the bear case before committing
5. `check_contradictions()` — surface anything inconsistent with prior stated strategy
6. If Russ decides to act: `log_decision(...)` — record the thesis before he trades
7. Optionally: `start_watchlist_stream()` if Russ wants live alerts while he watches
8. Later: `resolve_decision(id)` when the position closes or the thesis expires

---

## The journal as expertise

The brain starts with no trading expertise. It builds it through the journal's feedback loop: prediction → outcome → lesson → re-injection into future analysis. This only works if:

- **Predictions are logged before acting**, not reconstructed after. The rationale written under time pressure, before the outcome is known, is the honest one.
- **Every resolution includes a lesson**. Not "it didn't work" but "RSI < 30 alone didn't mark a bottom because the macro tape was diverging — next time require the benchmark to confirm."
- **Losses are resolved promptly**. Letting a losing thesis stay open in the journal as "technically still open" defeats the learning loop.

Over time, era summaries condense old lessons into pattern-level insights that surface automatically in planning context. The longer the journal runs, the more the brain knows.

---

## Connections and data flow

```
Alpaca MCP (read-only paper key)
  └── get_quote / get_history / get_indicators / get_option_chain
      └── market_data.py → indicators.py → tools.py → motor cortex → brain response

Alpaca websocket (same paper key, live 1-min bars)
  └── stream.py → indicators.py → watch_indicators triggers → alert card to UI

Alpaca MCP (live read-only key, when set)
  └── sync_account → portfolio.json + execution_log.jsonl

second_brain/trading/
  ├── watchlist.json        ← scan_watchlist + stream read this
  ├── portfolio.json        ← check_contradictions reads this
  ├── execution_log.jsonl   ← ground truth; compare advice vs Russ's actual trades
  ├── journal.jsonl         ← log_decision writes; resolve_decision patches; review_journal reads
  └── journal.md            ← human-readable; auto-condensed over time
```

---

## Limits and non-negotiables

- **Advise only.** The brain never places an order, never cancels, never moves money. If asked to trade, decline clearly.
- **Crypto is deferred.** Crypto symbols return a blocked message. Stocks, ETFs, and options only.
- **Stress-test and mispricing tools are inert until their prompts are authored** in `brain/clusters/trading/prompts.py`. They will return `[blocked] prompt not configured` until Russ writes those prompts.
- **The live key is for reading only.** `sync_account` uses the live key to see Russ's real positions and fills — it never writes back to the broker.
