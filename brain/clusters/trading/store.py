"""File-backed storage for the trading layer.

Owns the read/write of the user-maintained data files and the shared atomic-IO
helpers. Paths live under ``second_brain/trading/`` (resolved from the same root
as the rest of the brain's persistence). All loaders are tolerant: a missing or
corrupt file yields a safe default rather than raising, mirroring the brain's
``parse_threads`` philosophy.

Files:
- watchlist.json        — user-owned; symbols, thresholds, watch_indicators, thesis
- portfolio.json        — user-owned OR synced read-only from the broker
- execution_log.jsonl   — user-owned OR synced read-only; real buys/sells
- journal.jsonl         — brain-owned record of record (see journal.py)
- journal.md            — brain-owned human-readable mirror (see journal.py)

Pruning
-------
``prune()`` is called from journal.py after every write. Defaults (configurable
in settings.json):
- trading_journal_max_resolved: max resolved journal entries to keep (default 500).
  Open (live) entries are NEVER pruned — they're active theses.
- trading_execlog_max_days: drop execution_log entries older than N days (default 365).
- trading_journal_md_max_kb: truncate journal.md to the last N KB (default 512),
  since it's a human-readable mirror and the JSONL is authoritative.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from brain.second_brain.store import SECOND_BRAIN_ROOT

logger = logging.getLogger(__name__)

TRADING_DIR = SECOND_BRAIN_ROOT / "trading"

WATCHLIST_PATH = TRADING_DIR / "watchlist.json"
PORTFOLIO_PATH = TRADING_DIR / "portfolio.json"
EXECUTION_LOG_PATH = TRADING_DIR / "execution_log.jsonl"
JOURNAL_JSONL_PATH = TRADING_DIR / "journal.jsonl"
JOURNAL_MD_PATH = TRADING_DIR / "journal.md"


def _ensure_dir() -> None:
    TRADING_DIR.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically (tmp file + os.replace), matching JobStore’s pattern."""
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def append_jsonl(path: Path, record: dict) -> None:
    _ensure_dir()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.debug("[trading.store] skipping bad jsonl line in %s", path.name)
    except OSError as e:
        logger.warning("[trading.store] read_jsonl %s failed: %s", path, e)
    return out


def rewrite_jsonl(path: Path, records: list[dict]) -> None:
    """Rewrite a whole JSONL file atomically (used to patch a record in place)."""
    text = "".join(json.dumps(r, default=str) + "\n" for r in records)
    atomic_write_text(path, text)


def append_text(path: Path, text: str) -> None:
    _ensure_dir()
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[trading.store] read_json %s failed: %s", path, e)
        return default


# ── user-maintained files ─────────────────────────────────────────────────────


def load_watchlist() -> dict:
    """Return {"updated_at", "symbols": [...]} with safe defaults."""
    data = _read_json(WATCHLIST_PATH, {})
    if not isinstance(data, dict):
        return {"symbols": []}
    data.setdefault("symbols", [])
    return data


def watchlist_symbols() -> list[dict]:
    return [
        s for s in load_watchlist().get("symbols", []) if isinstance(s, dict) and s.get("symbol")
    ]


def load_portfolio() -> dict:
    data = _read_json(PORTFOLIO_PATH, {})
    if not isinstance(data, dict):
        return {"cash": 0.0, "holdings": []}
    data.setdefault("cash", 0.0)
    data.setdefault("holdings", [])
    return data


def save_portfolio(portfolio: dict) -> None:
    atomic_write_text(PORTFOLIO_PATH, json.dumps(portfolio, indent=2, default=str))


def load_executions(symbol: str | None = None, since: str | None = None) -> list[dict]:
    rows = read_jsonl(EXECUTION_LOG_PATH)
    if symbol:
        rows = [r for r in rows if str(r.get("symbol", "")).upper() == symbol.upper()]
    if since:
        rows = [r for r in rows if str(r.get("ts", "")) >= since]
    return rows


def append_execution(record: dict) -> None:
    append_jsonl(EXECUTION_LOG_PATH, record)


# ── pruning ───────────────────────────────────────────────────────────────────
#
# Two very different strategies, per user guidance:
#
# execution_log.jsonl  — hard-delete entries older than N days. It's a
#   transaction ledger; old fills have no pattern-learning value.
#
# journal.jsonl / journal.md  — progressive summarization cascade, not deletion.
#   Handled by compaction.py (needs an LLM router) which is called from
#   TradingSubsystem.after_job. See compaction.py for the full design.
#   prune_execlog() is the only sync pruning still done here.

_DEFAULT_EXECLOG_DAYS = 365  # execution_log: drop fills older than N days


def _get_int(key: str, module_default_attr: str) -> int:
    """Read an int setting, falling back to this module's own default constant."""
    import brain.clusters.trading.store as _self

    fallback = getattr(_self, module_default_attr, 0)
    try:
        from brain.settings import settings

        v = settings.get(key)
        return int(v) if v is not None else fallback
    except Exception:
        return fallback


def prune() -> None:
    """Trim execution_log (hard-delete old fills). Journal compaction is async —
    call compaction.compact_journal(router) from the subsystem's after_job hook."""
    prune_execlog()


def prune_execlog() -> None:
    """Drop execution_log entries older than trading_execlog_max_days days."""
    max_days = _get_int("trading_execlog_max_days", "_DEFAULT_EXECLOG_DAYS")
    cutoff_ts = time.time() - max_days * 86400
    rows = read_jsonl(EXECUTION_LOG_PATH)
    if not rows:
        return
    kept = [r for r in rows if _ts_of(r) >= cutoff_ts]
    removed = len(rows) - len(kept)
    if not removed:
        return
    rewrite_jsonl(EXECUTION_LOG_PATH, kept)
    logger.info("[trading.store] pruned %d old execution_log entries (>%dd)", removed, max_days)


def _ts_of(row: dict) -> float:
    """Parse a row's 'ts' field to a Unix timestamp, defaulting to now."""
    ts = row.get("ts", "")
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return time.time()  # unknown age — keep it
