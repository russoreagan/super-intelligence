"""The trading decision journal — the record of record.

Two artifacts (per the plan), no duplication:
- journal.jsonl : structured, queryable, patched-in-place on resolve. SOURCE OF TRUTH.
- journal.md    : human-readable paragraph per resolved call (the reflection mirror).

Reuse of the brain's primitives is best-effort and never blocks journaling:
- decisions.log(...) : one telemetry event per open/resolve (always attempted).
- hippocampus        : optional; encodes an episode for associative recall.
- open-thread hooks  : optional callables (the subsystem supplies these where a
                       SchemaStore/DMN is available) so a live thesis shows up in
                       the DMN's working memory.

ADVISE-ONLY: this module records predictions and outcomes. It never trades.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime

from . import store

logger = logging.getLogger(__name__)


def _now_iso(now: float | None = None) -> str:
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def new_decision_id(now: float | None = None) -> str:
    ts = now if now is not None else time.time()
    return f"d-{int(ts)}-{os.urandom(2).hex()}"


# ── open a prediction ─────────────────────────────────────────────────────────


def log_decision(
    symbol: str,
    direction: str,
    prediction: str,
    rationale: str,
    confidence: float,
    *,
    indicators_at_open: dict | None = None,
    entry_threshold: dict | None = None,
    benchmark: str = "QQQ",
    benchmark_at_open: float | None = None,
    turn_id: str = "",
    decision_id: str | None = None,
    now: float | None = None,
    hippocampus=None,
    on_open_thread=None,
) -> str:
    """Open a prediction; returns its decision_id."""
    symbol = symbol.upper().strip()
    direction = direction.lower().strip()
    if direction not in ("long", "short"):
        direction = "long"
    did = decision_id or new_decision_id(now)

    thread_id = ""
    if on_open_thread is not None:
        try:
            thread_id = (
                on_open_thread(f"[{symbol}] {prediction} — {rationale}", ["trading", symbol]) or ""
            )
        except Exception as e:  # pragma: no cover - optional path
            logger.debug("[journal] on_open_thread failed: %s", e)

    record = {
        "id": did,
        "ts_opened": _now_iso(now),
        "symbol": symbol,
        "direction": direction,
        "prediction": prediction,
        "rationale": rationale,
        "indicators_at_open": indicators_at_open or {},
        "entry_threshold": entry_threshold or {},
        "confidence": float(confidence),
        "benchmark": benchmark,
        "benchmark_at_open": benchmark_at_open,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "status": "open",
        "resolution": None,
    }
    store.append_jsonl(store.JOURNAL_JSONL_PATH, record)
    store.prune()  # execution_log only; journal compaction is async via compact_journal

    _safe_decision_log(
        "trading_prediction_opened",
        turn_id=turn_id,
        symbol=symbol,
        decision_id=did,
        direction=direction,
        confidence=record["confidence"],
    )
    _safe_encode_episode(
        hippocampus,
        user_input=f"Trading thesis for {symbol}",
        entity_response=f"{direction.upper()} {symbol}: {prediction} — {rationale}",
        topic_tags=["trading", symbol],
        entities=[symbol],
    )
    return did


# ── resolve a prediction ──────────────────────────────────────────────────────


def compute_metrics(
    record: dict,
    price_at_resolve: float,
    benchmark_at_resolve: float | None,
) -> dict:
    """Return/alpha vs entry and benchmark, threshold hit, and an outcome label."""
    open_price = (record.get("indicators_at_open") or {}).get("price")
    direction = record.get("direction", "long")
    raw_return_pct = None
    if open_price:
        raw_return_pct = (price_at_resolve - float(open_price)) / float(open_price) * 100.0
        if direction == "short":
            raw_return_pct = -raw_return_pct
        raw_return_pct = round(raw_return_pct, 2)

    alpha = None
    bench_open = record.get("benchmark_at_open")
    if raw_return_pct is not None and bench_open and benchmark_at_resolve:
        bench_ret = (benchmark_at_resolve - float(bench_open)) / float(bench_open) * 100.0
        alpha = round(raw_return_pct - bench_ret, 2)

    thresholds = record.get("entry_threshold") or {}
    hit = "none"
    exit_above = thresholds.get("exit_above")
    stop_below = thresholds.get("stop_below")
    if exit_above is not None and price_at_resolve >= float(exit_above):
        hit = "exit_above"
    elif stop_below is not None and price_at_resolve <= float(stop_below):
        hit = "stop_below"

    if alpha is not None:
        label = "win" if alpha > 0.5 else "miss" if alpha < -0.5 else "scratch"
    elif raw_return_pct is not None:
        label = "win" if raw_return_pct > 0.5 else "miss" if raw_return_pct < -0.5 else "scratch"
    else:
        label = "unknown"
    if hit == "stop_below":
        label = "miss"

    return {
        "price_at_resolve": price_at_resolve,
        "benchmark_at_resolve": benchmark_at_resolve,
        "raw_return_pct": raw_return_pct,
        "alpha_vs_benchmark_pct": alpha,
        "hit_threshold": hit,
        "outcome_label": label,
    }


def resolve_decision(
    decision_id: str,
    *,
    price_at_resolve: float,
    benchmark_at_resolve: float | None = None,
    lesson: str = "",
    missed_signal: str = "",
    note: str = "",
    now: float | None = None,
    hippocampus=None,
    on_conclude_thread=None,
) -> dict:
    """Resolve an open prediction in place; returns the resolution block.

    The ``lesson`` text is normally produced by reflection.py before calling this.
    """
    records = store.read_jsonl(store.JOURNAL_JSONL_PATH)
    target = None
    for r in records:
        if r.get("id") == decision_id:
            target = r
            break
    if target is None:
        return {"error": f"decision not found: {decision_id}"}
    if target.get("status") == "resolved":
        return {"error": f"already resolved: {decision_id}", "resolution": target.get("resolution")}

    metrics = compute_metrics(target, price_at_resolve, benchmark_at_resolve)
    resolution = {
        "ts_resolved": _now_iso(now),
        **metrics,
        "note": note,
        "missed_signal": missed_signal,
        "lesson": lesson,
    }
    target["status"] = "resolved"
    target["resolution"] = resolution
    store.rewrite_jsonl(store.JOURNAL_JSONL_PATH, records)

    # human-readable mirror
    store.append_text(store.JOURNAL_MD_PATH, _md_paragraph(target))
    store.prune()  # trim old resolved entries + stale execlog + oversized md

    _safe_decision_log(
        "trading_prediction_resolved",
        turn_id=target.get("turn_id", ""),
        symbol=target.get("symbol"),
        decision_id=decision_id,
        outcome=metrics["outcome_label"],
        raw_return_pct=metrics["raw_return_pct"],
        alpha=metrics["alpha_vs_benchmark_pct"],
    )
    if on_conclude_thread is not None and target.get("thread_id"):
        try:
            on_conclude_thread(target["thread_id"])
        except Exception as e:  # pragma: no cover
            logger.debug("[journal] on_conclude_thread failed: %s", e)
    _safe_encode_episode(
        hippocampus,
        user_input=f"How did the {target.get('symbol')} thesis play out?",
        entity_response=(
            f"{metrics['outcome_label'].upper()} ({metrics['raw_return_pct']}% raw, "
            f"alpha {metrics['alpha_vs_benchmark_pct']}). Lesson: {lesson or '(none)'}"
        ),
        topic_tags=["trading", target.get("symbol", ""), "lesson"],
        entities=[target.get("symbol", "")],
        source="trading",
    )
    return resolution


def _md_paragraph(record: dict) -> str:
    res = record.get("resolution") or {}
    return (
        f"## {record.get('symbol')} — {res.get('ts_resolved', '')[:10]} "
        f"({res.get('outcome_label', '?')})\n\n"
        f"**Predicted** ({record.get('ts_opened', '')[:10]}, conf "
        f"{record.get('confidence')}): {record.get('prediction')}\n\n"
        f"**Because:** {record.get('rationale')}\n\n"
        f"**Outcome:** {res.get('raw_return_pct')}% raw, alpha "
        f"{res.get('alpha_vs_benchmark_pct')} vs {record.get('benchmark')}, "
        f"threshold hit: {res.get('hit_threshold')}.\n\n"
        f"**Lesson:** {res.get('lesson') or '(pending)'}\n\n---\n\n"
    )


# ── queries ───────────────────────────────────────────────────────────────────


def get_records(symbol: str | None = None, status: str | None = None) -> list[dict]:
    rows = store.read_jsonl(store.JOURNAL_JSONL_PATH)
    if symbol:
        rows = [r for r in rows if str(r.get("symbol", "")).upper() == symbol.upper()]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows


def review_journal(
    symbol: str | None = None, status: str | None = None, limit: int = 10
) -> list[dict]:
    rows = get_records(symbol, status)
    return rows[-limit:]


def last_lessons(symbol: str, limit: int = 3) -> list[str]:
    """Lessons from resolved calls for a symbol — for re-injection into planning."""
    rows = get_records(symbol, status="resolved")
    lessons = [
        (r.get("resolution") or {}).get("lesson", "")
        for r in rows
        if (r.get("resolution") or {}).get("lesson")
    ]
    return lessons[-limit:]


# ── best-effort side-effects ──────────────────────────────────────────────────


def _safe_decision_log(decision: str, **fields) -> None:
    try:
        from brain.observability.decisions import decisions

        decisions.log(decision, cluster="trading", **fields)
    except Exception as e:  # pragma: no cover
        logger.debug("[journal] decisions.log failed: %s", e)


def _safe_encode_episode(hippocampus, **kwargs) -> None:
    if hippocampus is None:
        return
    try:
        encode = getattr(hippocampus, "encode_conclusion", None)
        if kwargs.get("source") == "trading" and callable(encode):
            encode(
                kwargs.get("entity_response", ""),
                source="trading",
                tags=kwargs.get("topic_tags", []),
            )
            return
        # generic episode encode (best-effort; signature-tolerant)
        enc = getattr(hippocampus, "encode", None)
        if callable(enc):
            from brain.second_brain.store import Episode  # type: ignore

            enc(
                Episode(
                    session_id="trading",
                    turn_id=new_decision_id(),
                    ts=time.time(),
                    user_input=kwargs.get("user_input", ""),
                    entity_response=kwargs.get("entity_response", ""),
                    topic_tags=kwargs.get("topic_tags", []),
                    emotion_state="",
                    user_emotion="",
                    entities=kwargs.get("entities", []),
                    neuromod_snapshot={},
                    surprise_score=0.0,
                    vector=[],
                )
            )
    except Exception as e:  # pragma: no cover - optional path
        logger.debug("[journal] encode episode failed: %s", e)
