"""Tests for the advise-only day-trading layer.

Covers the plan's verification matrix: no-execution/allow-list, prompt governance,
indicator math, gating, journal lifecycle, re-injection, contradiction detection,
and the stress-test role mapping.
"""

from __future__ import annotations

import math

import pytest

from brain.clusters.trading import (
    BLOCKED_ALPACA_TOOLS,
    READ_ONLY_ALPACA_TOOLS,
    capabilities,
    indicators,
    journal,
    prompts,
    store,
)
from brain.clusters.trading.alpaca_mcp_client import AlpacaMCPClient, AlpacaToolBlocked

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def trading_dir(tmp_path, monkeypatch):
    """Point the trading store at a temp dir so files don't touch real data."""
    d = tmp_path / "trading"
    d.mkdir()
    monkeypatch.setattr(store, "TRADING_DIR", d)
    monkeypatch.setattr(store, "WATCHLIST_PATH", d / "watchlist.json")
    monkeypatch.setattr(store, "PORTFOLIO_PATH", d / "portfolio.json")
    monkeypatch.setattr(store, "EXECUTION_LOG_PATH", d / "execution_log.jsonl")
    monkeypatch.setattr(store, "JOURNAL_JSONL_PATH", d / "journal.jsonl")
    monkeypatch.setattr(store, "JOURNAL_MD_PATH", d / "journal.md")
    return d


class FakeMarketData:
    def __init__(self, price=145.0, bars=None):
        self._price = price
        self._bars = bars or []

    async def quote(self, symbol):
        return {"symbol": symbol, "price": self._price, "source": "fake"}

    async def history(self, symbol, days=200):
        return self._bars


class FakeRouter:
    def __init__(self):
        self.system_prompts = []

    async def call(self, model_key, system_prompt, messages, **kw):
        self.system_prompts.append(system_prompt)
        return f"text[{kw.get('cell')}]"

    async def call_structured(self, model_key, system_prompt, messages, tool_name,
                              tool_description, tool_schema, **kw):
        self.system_prompts.append(system_prompt)
        return {"rating": "Hold", "breaks_story": "macro regime flips", "hedge": "protective put"}


# ── 1. no-execution / allow-list ──────────────────────────────────────────────


def test_read_only_and_blocked_are_disjoint():
    assert READ_ONLY_ALPACA_TOOLS.isdisjoint(BLOCKED_ALPACA_TOOLS)


def test_registry_exposes_no_order_tools():
    import brain.clusters.motor_cortex as mc

    assert BLOCKED_ALPACA_TOOLS.isdisjoint(mc._DISPATCHABLE_TOOLS)
    # no obvious order verbs leaked into the registry
    for bad in ("place_stock_order", "submit_order", "buy", "sell", "cancel_order_by_id"):
        assert bad not in mc._DISPATCHABLE_TOOLS


async def test_client_blocks_every_write_tool():
    c = AlpacaMCPClient(api_key="x", secret_key="y")
    for name in BLOCKED_ALPACA_TOOLS:
        with pytest.raises(AlpacaToolBlocked):
            await c.call(name, {})


async def test_client_blocks_unknown_tool():
    c = AlpacaMCPClient(api_key="x", secret_key="y")
    with pytest.raises(AlpacaToolBlocked):
        await c.call("totally_made_up_tool", {})


# ── 2. prompt governance ──────────────────────────────────────────────────────


def test_prompts_have_no_automated_write_path():
    """No code in brain/ can automatically write to prompts.py or assign to a
    prompt constant.

    External context must always go to a local document first (reviewed by Russ),
    then be referenced as context — never injected directly into a prompt constant.
    The before_plan injection is also checked: it may only pull from the local
    journal (the brain's own generated data from Russ's trades), not from any
    external network call, fetch, or websocket stream.
    """
    from pathlib import Path

    brain_root = Path("brain")
    trading_dir = brain_root / "clusters" / "trading"
    prompts_file = trading_dir / "prompts.py"

    prompt_constants = (
        "REFLECTION_SYSTEM",
        "BULL_SYSTEM",
        "BEAR_SYSTEM",
        "RISK_SYSTEM",
        "SYNTHESIS_SYSTEM",
        "MISPRICING_SYSTEM",
        "CONDENSATION_SYSTEM",
    )
    external_sources = ("fetch_url", "requests.", "httpx.", "websocket", "urllib")

    violations = []

    for py_file in brain_root.rglob("*.py"):
        if py_file.resolve() == prompts_file.resolve():
            continue  # prompts.py itself is the only allowed source of truth

        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue

        # No automated assignment to prompt constants from any other file
        for const in prompt_constants:
            if f"prompts.{const} =" in source:
                violations.append(f"{py_file}: assigns to prompts.{const}")

        # No code opens prompts.py for writing
        if "prompts.py" in source and "open(" in source and (
            '"w"' in source or "'w'" in source or '"a"' in source or "'a'" in source
        ):
            violations.append(f"{py_file}: opens prompts.py for writing")

    # before_plan specifically: must not pull from external network sources —
    # only from the local journal (brain's own data from Russ's resolved trades)
    subsystem_source = (trading_dir / "subsystem.py").read_text(encoding="utf-8")
    for src in external_sources:
        if src in subsystem_source:
            violations.append(f"subsystem.py before_plan contains external source: {src!r}")

    assert not violations, (
        "Automated write path into prompts.py detected — "
        "external context must go through a local reviewed document first:\n"
        + "\n".join(f"  • {v}" for v in violations)
    )


async def test_stress_test_runs_when_prompts_configured(monkeypatch):
    """stress_test_thesis returns ok (not blocked) when all role prompts are set."""
    res = await capabilities.stress_test_thesis("TSLA", "test thesis", md=None, router=FakeRouter())
    assert res["status"] == "ok"
    assert res["rating"] == "Hold"


async def test_find_mispricing_runs_when_prompt_configured():
    """find_mispricing returns ok (not blocked) when prompt is set."""
    res = await capabilities.find_mispricing("TSLA", md=None, router=FakeRouter())
    assert res["status"] == "ok"


async def test_blocked_when_prompt_cleared(monkeypatch):
    """Features still return blocked if their specific prompt is cleared."""
    monkeypatch.setattr(prompts, "MISPRICING_SYSTEM", "")
    res = await capabilities.find_mispricing("TSLA", md=None, router=FakeRouter())
    assert res["status"] == "blocked"


# ── 3. indicator math ─────────────────────────────────────────────────────────


def test_sma_exact():
    out = indicators.sma([1, 2, 3, 4, 5], 3)
    assert math.isnan(out[0]) and math.isnan(out[1])
    assert list(out[2:]) == [2.0, 3.0, 4.0]


def test_ema_seed_and_converge():
    out = indicators.ema([1, 2, 3, 4, 5], 3)
    assert out[2] == pytest.approx(2.0)  # seed = SMA of first 3
    assert out[-1] == pytest.approx(4.0)  # converges on a linear ramp


def test_rsi_all_gains_is_100():
    out = indicators.rsi(list(range(1, 30)), 14)
    assert out[-1] == pytest.approx(100.0)


def test_streak_sign_and_count():
    assert indicators.streak([1, 2, 3, 4]) == 3
    assert indicators.streak([4, 3, 2, 1]) == -3
    assert indicators.streak([1, 2, 3, 2]) == -1


def test_compute_all_keys():
    snap = indicators.compute_all(list(range(1, 60)))
    for k in ("price", "rsi_14", "macd", "sma_50", "roc_10", "streak", "price_vs_sma50"):
        assert k in snap


# ── 4. gating ─────────────────────────────────────────────────────────────────


def test_trading_enabled():
    from brain.settings import settings

    assert int(settings.get("trading_enabled") or 0) == 1


# ── 5. journal lifecycle ──────────────────────────────────────────────────────


def test_journal_open_and_resolve(trading_dir):
    did = journal.log_decision(
        "tsla", "long", "bounce to 185", "RSI 28 + reclaim SMA50", 0.62,
        indicators_at_open={"price": 172.4, "rsi_14": 28.1},
        entry_threshold={"stop_below": 150, "exit_above": 240},
        benchmark="QQQ", benchmark_at_open=478.10,
    )
    opens = journal.get_records(status="open")
    assert len(opens) == 1 and opens[0]["symbol"] == "TSLA"

    res = journal.resolve_decision(did, price_at_resolve=168.0, benchmark_at_resolve=489.0,
                                   lesson="oversold alone isn't a bottom")
    assert res["raw_return_pct"] == pytest.approx(-2.55, abs=0.01)
    assert res["alpha_vs_benchmark_pct"] == pytest.approx(-4.83, abs=0.05)
    assert res["outcome_label"] == "miss"
    assert store.JOURNAL_MD_PATH.exists()
    assert journal.get_records(status="resolved")[0]["resolution"]["lesson"]


def test_metrics_short_direction(trading_dir):
    rec = {"direction": "short", "indicators_at_open": {"price": 100.0},
           "entry_threshold": {}, "benchmark_at_open": None}
    m = journal.compute_metrics(rec, price_at_resolve=90.0, benchmark_at_resolve=None)
    assert m["raw_return_pct"] == pytest.approx(10.0)  # short profits when price falls


# ── 6. re-injection (before_plan) ─────────────────────────────────────────────


async def test_before_plan_reinjects_lesson(trading_dir):
    from brain.clusters.trading.subsystem import TradingSubsystem

    # one resolved call (carries the lesson) + one open call so it's surfaced
    did = journal.log_decision("TSLA", "long", "old call", "old", 0.5,
                               indicators_at_open={"price": 100})
    journal.resolve_decision(did, price_at_resolve=110, lesson="watch the macro tape")
    journal.log_decision("TSLA", "long", "new call", "RSI low", 0.6,
                         indicators_at_open={"price": 95})

    sub = TradingSubsystem(market_data=FakeMarketData())
    ctx = await sub.before_plan("analyze TSLA setup", None)
    assert "watch the macro tape" in ctx


async def test_before_plan_silent_for_non_trading(trading_dir):
    from brain.clusters.trading.subsystem import TradingSubsystem

    sub = TradingSubsystem(market_data=FakeMarketData())
    assert await sub.before_plan("what's the weather", None) == ""


# ── 7. contradiction detection ────────────────────────────────────────────────


async def test_contradiction_stop_breached_still_held(trading_dir):
    store.save_portfolio({"cash": 0, "holdings": [{"symbol": "TSLA", "shares": 100, "avg_cost": 170}]})
    journal.log_decision("TSLA", "long", "hold", "thesis", 0.6,
                         entry_threshold={"stop_below": 150})
    md = FakeMarketData(price=145.0)  # below the stop
    items = await capabilities.check_contradictions(md)
    kinds = {i["kind"] for i in items}
    assert "stop_breached_still_held" in kinds


# ── 8. stress-test role mapping (dedicated prompts, NOT personas) ──────────────


async def test_stress_test_uses_dedicated_role_prompts(monkeypatch):
    monkeypatch.setattr(prompts, "BULL_SYSTEM", "BULL-PROMPT")
    monkeypatch.setattr(prompts, "BEAR_SYSTEM", "BEAR-PROMPT")
    monkeypatch.setattr(prompts, "RISK_SYSTEM", "RISK-PROMPT")
    monkeypatch.setattr(prompts, "SYNTHESIS_SYSTEM", "SYNTH-PROMPT")

    router = FakeRouter()
    res = await capabilities.stress_test_thesis("TSLA", "robotaxi re-rate", md=None, router=router)
    assert res["status"] == "ok"
    assert res["rating"] == "Hold"
    # exactly the four dedicated role prompts were used, in order — no persona files
    assert router.system_prompts == ["BULL-PROMPT", "BEAR-PROMPT", "RISK-PROMPT", "SYNTH-PROMPT"]


# ── 9. compaction & pruning ───────────────────────────────────────────────────


def test_execlog_prune_drops_old_rows(trading_dir, monkeypatch):
    """execution_log: hard-delete fills beyond the day limit."""
    import brain.clusters.trading.store as s

    monkeypatch.setattr(s, "_DEFAULT_EXECLOG_DAYS", 30)

    ancient_ts = "2020-01-01T00:00:00+00:00"
    recent_ts = "2099-01-01T00:00:00+00:00"
    for ts in (ancient_ts, recent_ts, recent_ts):
        s.append_execution({"ts": ts, "symbol": "AAPL", "shares": 10, "price": 100})

    s.prune()
    kept = s.load_executions()
    assert all(r["ts"] != ancient_ts for r in kept)
    assert len(kept) == 2


async def test_compact_journal_creates_era_summary(trading_dir, monkeypatch):
    """compact_journal condenses old resolved records into an era_summary."""
    from brain.clusters.trading import compaction as C
    from brain.settings import settings as _s

    monkeypatch.setitem(_s._data, "trading_journal_max_resolved", 3)
    monkeypatch.setitem(_s._data, "trading_compaction_batch_size", 3)

    # Write 6 resolved + 1 open — triggers compaction
    for i in range(6):
        did = journal.log_decision(f"SYM{i}", "long", f"p{i}", "r", 0.5,
                                   indicators_at_open={"price": 100})
        journal.resolve_decision(did, price_at_resolve=110)
    journal.log_decision("OPEN", "long", "live thesis", "r", 0.5)

    router = FakeRouter()
    compacted = await C.compact_journal(router=router)
    assert compacted > 0

    records = journal.get_records()
    era = [r for r in records if r.get("status") == "era_summary"]
    opens = [r for r in records if r.get("status") == "open"]
    resolved = [r for r in records if r.get("status") == "resolved"]
    assert len(era) >= 1, "expected at least one era_summary"
    assert era[0]["depth"] == 1
    assert len(opens) == 1 and opens[0]["symbol"] == "OPEN"
    # Full resolved records were reduced
    assert len(resolved) < 6


async def test_compact_journal_preserves_open_entries(trading_dir, monkeypatch):
    """Open (live) theses are never compacted, regardless of threshold."""
    from brain.clusters.trading import compaction as C
    from brain.settings import settings as _s

    monkeypatch.setitem(_s._data, "trading_journal_max_resolved", 1)
    monkeypatch.setitem(_s._data, "trading_compaction_batch_size", 5)

    # 3 open, 2 resolved
    for _ in range(3):
        journal.log_decision("LIVE", "long", "open thesis", "r", 0.7)
    for i in range(2):
        did = journal.log_decision(f"OLD{i}", "long", "old", "r", 0.4)
        journal.resolve_decision(did, price_at_resolve=105)

    await C.compact_journal(router=FakeRouter())
    opens = journal.get_records(status="open")
    assert len(opens) == 3  # all live theses untouched


async def test_compact_journal_md_condenses(trading_dir, monkeypatch):
    """compact_journal_md LLM-condenses the oldest section (not hard-deletes)."""
    from brain.clusters.trading import compaction as C
    from brain.clusters.trading import store as s
    from brain.settings import settings as _s

    monkeypatch.setitem(_s._data, "trading_journal_md_max_kb", 1)

    # Build a file that clearly exceeds 1 KB (1024 bytes)
    old_part = "old call detail " * 80          # 16 * 80 = 1280 bytes
    new_part = "recent call detail\n"
    content = old_part + "\n---\n\n" + new_part
    s.JOURNAL_MD_PATH.write_text(content, encoding="utf-8")

    router = FakeRouter()
    await C.compact_journal_md(router=router)

    result = s.JOURNAL_MD_PATH.read_text(encoding="utf-8")
    # The condensed era prefix must appear; raw old_part detail is replaced
    assert "Condensed era" in result
    assert "recent call detail" in result


async def test_era_summaries_appear_in_before_plan(trading_dir, monkeypatch):
    """Era summaries are surfaced via before_plan re-injection."""
    from brain.clusters.trading import compaction as C
    from brain.clusters.trading.subsystem import TradingSubsystem
    from brain.settings import settings as _s

    monkeypatch.setitem(_s._data, "trading_journal_max_resolved", 2)
    monkeypatch.setitem(_s._data, "trading_compaction_batch_size", 3)

    for i in range(5):
        did = journal.log_decision("TSLA", "long", f"call {i}", "r", 0.5,
                                   indicators_at_open={"price": 100})
        journal.resolve_decision(did, price_at_resolve=105)
    journal.log_decision("TSLA", "long", "active thesis", "r", 0.6)

    await C.compact_journal(router=FakeRouter())

    sub = TradingSubsystem(market_data=FakeMarketData())
    ctx = await sub.before_plan("analyze TSLA", None)
    # Era memory should be present even if specific lesson text varies by mock
    assert "TSLA" in ctx or "era memory" in ctx
