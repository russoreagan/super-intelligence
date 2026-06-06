"""The four analytical capabilities (advise-only).

(a) scan_watchlist      — threshold + historical-pattern alerts
(b) check_contradictions — "you said X, but Y is still true"
(c) stress_test_thesis  — Bull/Bear/Risk/Synthesis debate (dedicated trading-role
                          prompts from prompts.py — NOT the brain's personas)
(d) find_mispricing     — the gap between price action and the underlying data

LLM-driven features ((c) and (d)) are INERT until their prompts.py text is
authored by Russ; they return prompts.BLOCKED_MSG. None of these place trades.
"""

from __future__ import annotations

import json
import logging

from . import indicators, journal, prompts, store
from .market_data import MarketData, closes

logger = logging.getLogger(__name__)


def _similar(a: dict, b: dict, keys=("rsi_14", "price"), tol_pct: float = 0.10) -> bool:
    """Are two indicator snapshots within tolerance on the named keys?"""
    for k in keys:
        av, bv = a.get(k), b.get(k)
        if av is None or bv is None:
            return False
        if bv == 0:
            return False
        if abs(av - bv) / abs(bv) > tol_pct:
            return False
    return True


def _eval_trigger(value: float | None, trigger: str, level: float | None) -> bool:
    if value is None:
        return False
    if trigger in ("<", "below") and level is not None:
        return value < level
    if trigger in (">", "above") and level is not None:
        return value > level
    if trigger == "<=" and level is not None:
        return value <= level
    if trigger == ">=" and level is not None:
        return value >= level
    if trigger == "cross_up":
        return value > 0  # used with price_vs_sma50
    if trigger == "cross_down":
        return value < 0
    return False


# ── (a) watchlist scan ────────────────────────────────────────────────────────


async def scan_watchlist(md: MarketData) -> list[dict]:
    alerts: list[dict] = []
    for entry in store.watchlist_symbols():
        symbol = entry["symbol"].upper()
        bars = await md.history(symbol, days=250)
        if not bars:
            continue
        snap = indicators.compute_all(closes(bars))
        fired = []
        for wi in entry.get("watch_indicators", []) or []:
            name = wi.get("name")
            if _eval_trigger(snap.get(name), wi.get("trigger", ""), wi.get("level")):
                fired.append(wi)
        if not fired:
            continue
        # historical pattern: prior resolved calls with similar conditions
        priors = [
            r
            for r in journal.get_records(symbol, status="resolved")
            if _similar(snap, r.get("indicators_at_open", {}))
        ]
        wins = sum(1 for r in priors if (r.get("resolution") or {}).get("outcome_label") == "win")
        last_lesson = ""
        if priors:
            last_lesson = (priors[-1].get("resolution") or {}).get("lesson", "")
        alerts.append(
            {
                "symbol": symbol,
                "snapshot": snap,
                "fired": fired,
                "prior_count": len(priors),
                "prior_wins": wins,
                "last_lesson": last_lesson,
                "thesis": entry.get("thesis", ""),
            }
        )
    return alerts


# ── (b) contradiction surfacing ───────────────────────────────────────────────


async def check_contradictions(md: MarketData) -> list[dict]:
    out: list[dict] = []
    portfolio = store.load_portfolio()
    held = {
        h.get("symbol", "").upper(): h for h in portfolio.get("holdings", []) if h.get("symbol")
    }

    for rec in journal.get_records(status="open"):
        symbol = rec.get("symbol", "").upper()
        thresholds = rec.get("entry_threshold") or {}
        if symbol not in held:
            continue
        quote = await md.quote(symbol)
        price = quote.get("price")
        if price is None:
            continue
        stop = thresholds.get("stop_below")
        exit_above = thresholds.get("exit_above")
        if stop is not None and price <= float(stop):
            out.append(
                {
                    "symbol": symbol,
                    "kind": "stop_breached_still_held",
                    "detail": f"You set stop_below {stop}; price is {price} and the position is still open.",
                }
            )
        elif exit_above is not None and price >= float(exit_above):
            out.append(
                {
                    "symbol": symbol,
                    "kind": "target_hit_still_held",
                    "detail": f"You set exit_above {exit_above}; price is {price} — take-profit reached but still held.",
                }
            )

    # watchlist thresholds vs current price (drift even without an open prediction)
    for entry in store.watchlist_symbols():
        symbol = entry["symbol"].upper()
        if symbol not in held:
            continue
        stop = entry.get("stop_below")
        if stop is None:
            continue
        quote = await md.quote(symbol)
        price = quote.get("price")
        if price is not None and price <= float(stop):
            out.append(
                {
                    "symbol": symbol,
                    "kind": "watchlist_stop_breached",
                    "detail": f"Watchlist stop_below {stop} breached (price {price}) on a held position.",
                }
            )
    return out


# ── (c) thesis stress-test (dedicated trading roles, NOT personas) ────────────

_RATING_SCHEMA = {
    "type": "object",
    "required": ["rating", "breaks_story"],
    "properties": {
        "rating": {
            "type": "string",
            "enum": ["Buy", "Overweight", "Hold", "Underweight", "Sell"],
        },
        "breaks_story": {"type": "string"},
        "hedge": {"type": "string"},
    },
}


async def stress_test_thesis(
    symbol: str = "",
    thesis_text: str = "",
    *,
    md: MarketData | None = None,
    router=None,
    model_key: str = "local",
) -> dict:
    # Inert unless every role prompt is authored by Russ.
    role_prompts = {
        "bull": prompts.BULL_SYSTEM,
        "bear": prompts.BEAR_SYSTEM,
        "risk": prompts.RISK_SYSTEM,
        "synthesis": prompts.SYNTHESIS_SYSTEM,
    }
    missing = [k for k, p in role_prompts.items() if not prompts.is_configured(p)]
    if missing or router is None:
        return {"status": "blocked", "message": prompts.BLOCKED_MSG, "missing_prompts": missing}

    symbol = symbol.upper().strip()
    context: dict = {"symbol": symbol, "thesis": thesis_text}
    if md is not None and symbol:
        bars = await md.history(symbol, days=250)
        if bars:
            context["indicators"] = indicators.compute_all(closes(bars))
        context["lessons"] = journal.last_lessons(symbol)
    ctx_msg = [{"role": "user", "content": json.dumps(context, default=str)}]

    bull = await router.call(
        model_key, prompts.BULL_SYSTEM, ctx_msg, cluster="trading", cell="bull"
    )
    bear = await router.call(
        model_key, prompts.BEAR_SYSTEM, ctx_msg, cluster="trading", cell="bear"
    )
    risk = await router.call(
        model_key, prompts.RISK_SYSTEM, ctx_msg, cluster="trading", cell="risk"
    )

    synth_msg = [
        {
            "role": "user",
            "content": json.dumps(
                {**context, "bull": bull, "bear": bear, "risk": risk}, default=str
            ),
        }
    ]
    verdict = await router.call_structured(
        model_key=model_key,
        system_prompt=prompts.SYNTHESIS_SYSTEM,
        messages=synth_msg,
        tool_name="render_verdict",
        tool_description="Synthesize the debate into a rating, the key risk, and a hedge.",
        tool_schema=_RATING_SCHEMA,
        cluster="trading",
        cell="synthesis",
    )
    return {
        "status": "ok",
        "symbol": symbol,
        "bull": bull,
        "bear": bear,
        "risk": risk,
        "rating": (verdict or {}).get("rating"),
        "breaks_story": (verdict or {}).get("breaks_story"),
        "hedge": (verdict or {}).get("hedge"),
    }


# ── (d) mispricing detection ──────────────────────────────────────────────────


async def find_mispricing(
    symbol: str,
    *,
    md: MarketData | None = None,
    router=None,
    model_key: str = "local",
) -> dict:
    if router is None or not prompts.is_configured(prompts.MISPRICING_SYSTEM):
        return {"status": "blocked", "message": prompts.BLOCKED_MSG}
    symbol = symbol.upper().strip()
    context: dict = {"symbol": symbol}
    if md is not None:
        bars = await md.history(symbol, days=250)
        if bars:
            context["indicators"] = indicators.compute_all(closes(bars))
            context["recent_closes"] = closes(bars)[-20:]
    analysis = await router.call(
        model_key,
        prompts.MISPRICING_SYSTEM,
        [{"role": "user", "content": json.dumps(context, default=str)}],
        cluster="trading",
        cell="mispricing",
    )
    return {"status": "ok", "symbol": symbol, "analysis": analysis}
