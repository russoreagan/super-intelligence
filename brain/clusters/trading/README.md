# Day-trading capability (ADVISE-ONLY)

Gives the brain read-only market/account data, technical indicators, a persistent
decision journal with a reflection loop, and four analytical tools. **It never
places an order or moves money.**

## Turn it on

1. **Enable the flag** in `brain/settings.json` (off by default):
   ```json
   "trading_enabled": 1
   ```
2. **Run the brain with the motor cortex** (`--motor` / `BRAIN_MOTOR=true`).
3. Without an Alpaca key it works immediately on **free yfinance** data. To use
   Alpaca (better data + your real account), set a **read-only-scoped** key:
   ```bash
   export ALPACA_API_KEY=...        # read-only scope (create in Alpaca dashboard)
   export ALPACA_SECRET_KEY=...
   export ALPACA_PAPER_TRADE=false  # =true for paper; false to see your REAL account
   ```
   The brain launches `uvx alpaca-mcp-server` itself (Path A).

## Advise-only guarantees (three layers)

1. **Read-only Alpaca key** — broker-side; no order can be placed even if asked.
2. **Per-tool allow-list** — `alpaca_mcp_client.py` only ever calls the ~46
   read-only tools and raises on the 18 write tools (`READ_ONLY_ALPACA_TOOLS` /
   `BLOCKED_ALPACA_TOOLS` in `__init__.py`).
3. **`ALPACA_TOOLSETS` + planner sanitizer** — write categories dropped; any
   hallucinated order tool is neutralized.

## You must author the prompts (governance)

The LLM-driven features ship **inert**. Open `prompts.py` and write your own text
for the ones you want — nothing is authored automatically and nothing is copied
from any third-party repo:
- `REFLECTION_SYSTEM` — the post-trade "what did I miss" lesson.
- `BULL_SYSTEM` / `BEAR_SYSTEM` / `RISK_SYSTEM` / `SYNTHESIS_SYSTEM` — the four
  stress-test roles (these are dedicated trading stances, **not** the brain's
  personality personas).
- `MISPRICING_SYSTEM` — the price-vs-data gap analysis.

Until a prompt is filled in, its feature returns `[blocked] prompt not configured`.

## Data files you maintain (`second_brain/trading/`)

- `watchlist.json` — symbols, `entry_below`/`exit_above`/`stop_below`,
  `watch_indicators` (e.g. `{"name":"rsi_14","trigger":"<","level":30}`), thesis.
- `portfolio.json` — holdings/cash (or auto-synced read-only via `account_sync`).
- `execution_log.jsonl` — your real fills (or auto-synced).

The brain writes `journal.jsonl` (record of record) and `journal.md` (readable
mirror) itself.

## Cloud path (optional, Path B)

To also trigger multi-agent analysis via the Claude apps, register the same server
(same read-only key):
```bash
claude mcp add alpaca --scope user --transport stdio uvx alpaca-mcp-server \
  --env ALPACA_API_KEY=... --env ALPACA_SECRET_KEY=... --env ALPACA_PAPER_TRADE=true \
  --env ALPACA_TOOLSETS=account,stock-data,options-data,corporate-actions,news
```

## Connecting your existing Schwab / Robinhood (read-only)

Alpaca is covered above. For your Schwab/Robinhood activity, add a **SnapTrade**
`AccountProvider` (one read-only connection across brokers) rather than building
Schwab OAuth + an unofficial Robinhood client. Crypto is deferred.

## Tools the brain can call

`get_quote`, `get_history`, `get_indicators`, `get_option_chain`, `scan_watchlist`,
`log_decision`, `resolve_decision`, `review_journal`, `check_contradictions`,
`stress_test_thesis`, `find_mispricing`. Tables/charts render in the chat UI.
