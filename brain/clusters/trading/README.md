# Day-trading capability (ADVISE-ONLY)

Read-only market/account data, technical indicators, a persistent decision journal
with a reflection loop, and analytical tools. **It never places an order or moves
money.**

## Boundary: the trading app's MCP server is the default

The brain is **domain-agnostic** — trading is a vertical that lives in a separate
**trading app**, exposed to the brain as a remote **MCP connector**. The native
in-tree layer in this package (`tools.py`, `alpaca_mcp_client.py`, …) is **retired
and OFF by default** (`trading_enabled` defaults to `0`). It stays in-tree so it
can be re-enabled, but the supported path is the MCP connector below.

### Wire the trading MCP connector

1. **Register the connector** (`name: "trading"`) by pinning it via env. The CMA
   executor reads `BRAIN_CMA_MCP_SERVERS` first, which makes the registry
   read-only/env-managed:
   ```bash
   BRAIN_CMA_MCP_SERVERS='[{"name":"trading","url":"https://exquisite-courtesy-production-e579.up.railway.app/api/mcp/trading"}]'
   ```
2. **Set the shared bearer token.** The connector name `trading` maps to env var
   `BRAIN_CMA_MCP_TRADING_TOKEN` (`<NAME>` = name upper-cased, `-`→`_`). It MUST
   equal the trading app's `TRADING_MCP_SECRET` — **the same value on both
   services** (coordinate via Railway):
   ```bash
   BRAIN_CMA_MCP_TRADING_TOKEN=<same value as the app's TRADING_MCP_SECRET>
   ```
3. **Bind the connector to the trading mandate** so only the trading agent sees
   its tools. The motor cortex intersects the org-level connector allowlist with
   the *bound agent's* `permissions` (`_mode_policy` → `set_connector_filter`), so
   set the trading agent's permission overrides to scope it to `trading`:
   ```json
   { "motor_user_connectors": "trading", "motor_self_connectors": "trading" }
   ```
   (Connector names are newline-separated; one name = just `"trading"`.) With this,
   when the trading agent is bound for a turn it sees **only** the trading
   connector, and the trading connector is the only MCP surface it can reach.
   Manage agent permissions via the Agents UI / `brain/mandates.py` CRUD (the
   `permissions` column of the `agents` table) — it is org-level data, not code.

Cloud actions must be enabled for the bound agent (`motor_user_cloud` ≥ `ro`) and
`BRAIN_MOTOR=true`. The connector is read-only/advise-only on the app side.

### Native layer (legacy / escape hatch — not the supported path)

The in-tree native tools remain for local/offline use. They are gated OFF by
`trading_enabled=0`; `BRAIN_NATIVE_TRADING` is an env override that wins over a
stale per-tenant `settings.json` (`0/false/off/no` → off, any other value → on,
unset → use the setting). To run them:

1. **Enable the flag** in `brain/settings.json`:
   ```json
   "trading_enabled": 1
   ```
   (or `BRAIN_NATIVE_TRADING=1`), and **run with the motor cortex**
   (`--motor` / `BRAIN_MOTOR=true`).
2. Without an Alpaca key it works on **free yfinance** data. For Alpaca, set a
   **read-only-scoped** key:
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
