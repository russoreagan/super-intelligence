"""Presentation helpers — turn trading results into UI table/chart cards.

The brain still speaks its prose recommendation (the turn_end text); these cards
render alongside it. Values are passed as plain data (the frontend inserts them as
text nodes / typed numeric series), so there is no HTML injection from tool output.

Emitting is best-effort: if there's no running event loop or no emitter, the prose
text still carries the recommendation (graceful degradation).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _emitter():
    try:
        from brain.ui.emitter import emitter

        return emitter
    except Exception:  # pragma: no cover
        return None


async def table(turn_id: str, title: str, columns: list[str], rows: list[list], note: str = "") -> None:
    em = _emitter()
    if em is None:
        return
    try:
        await em.emit_table(turn_id, title, columns, rows, note)
    except Exception as e:  # pragma: no cover
        logger.debug("[present] emit_table failed: %s", e)


async def chart(turn_id: str, spec: dict) -> None:
    em = _emitter()
    if em is None:
        return
    try:
        await em.emit_chart(turn_id, spec)
    except Exception as e:  # pragma: no cover
        logger.debug("[present] emit_chart failed: %s", e)


# ── builders ──────────────────────────────────────────────────────────────────

_SNAPSHOT_COLS = ["price", "rsi_14", "macd", "macd_signal", "sma_50", "sma_200", "roc_10", "streak"]


def snapshot_row(symbol: str, snap: dict) -> list:
    return [symbol] + [snap.get(c) for c in _SNAPSHOT_COLS]


def snapshot_columns() -> list[str]:
    return ["symbol"] + _SNAPSHOT_COLS


def candlestick_spec(symbol: str, bars: list[dict], overlays: dict | None = None, markers: list | None = None) -> dict:
    """Build a candlestick chart spec from bars + optional indicator overlays."""
    series = [
        {
            "time": (b.get("t") or "")[:10],
            "open": b.get("open"),
            "high": b.get("high"),
            "low": b.get("low"),
            "close": b.get("close"),
        }
        for b in bars
        if b.get("t")
    ]
    ov = []
    for name, values in (overlays or {}).items():
        line = [
            {"time": (b.get("t") or "")[:10], "value": v}
            for b, v in zip(bars, values, strict=False)
            if b.get("t") and v is not None
        ]
        ov.append({"name": name, "data": line})
    return {
        "title": f"{symbol} — daily",
        "kind": "candlestick",
        "series": series,
        "overlays": ov,
        "markers": markers or [],
    }
