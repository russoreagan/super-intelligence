"""Progressive memory compaction for the trading journal.

Instead of hard-deleting old resolved entries, the brain condenses them into
progressively shorter "era summaries" using an LLM. This mirrors biological
memory consolidation: recent events are detailed; older events are compressed
into pattern-level insights; very old summaries are extremely terse — but
nothing is ever completely gone until the summaries themselves are too old to
fit even at the most diluted level.

Cascade levels (stored in journal.jsonl by status + depth):
  status="resolved"           depth=0   — full individual record
  status="era_summary"        depth=1   — condensed from BATCH_SIZE resolved records
  status="era_summary"        depth=2   — condensed from BATCH_SIZE depth-1 summaries
  (depth-2 summaries persist essentially forever — they're tiny)

Thresholds (settings.json, all tunable):
  trading_journal_max_resolved        default 200   → compact when exceeded
  trading_journal_max_era_summaries   default 50    → compact depth-1 when exceeded
  trading_compaction_batch_size       default 20    → records per compaction pass

journal.md gets a parallel treatment: when the file exceeds
trading_journal_md_max_kb, the oldest section is LLM-summarized into a
condensed paragraph rather than just deleted.

compact_journal() is async (requires router). Call it from
TradingSubsystem.after_job where the router is available.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime

from . import prompts, store

logger = logging.getLogger(__name__)


# ── thresholds ────────────────────────────────────────────────────────────────

_DEFAULT_MAX_RESOLVED = 200
_DEFAULT_MAX_ERA = 50
_DEFAULT_BATCH = 20
_DEFAULT_MD_MAX_KB = 512


def _cfg(key: str, attr: str) -> int:
    """Read the config value from settings, falling back to the module constant."""
    import brain.clusters.trading.compaction as _self

    fallback = getattr(_self, attr, 0)
    try:
        from brain.settings import settings

        v = settings.get(key)
        return int(v) if v is not None else fallback
    except Exception:
        return fallback


# ── era_summary record schema ─────────────────────────────────────────────────


def _era_id(now: float | None = None) -> str:
    ts = now if now is not None else time.time()
    return f"era-{int(ts)}-{os.urandom(2).hex()}"


def _era_record(
    depth: int,
    source_records: list[dict],
    summary_text: str,
    stats: dict,
    now: float | None = None,
) -> dict:
    ts_list = [r.get("ts_opened") or r.get("ts_condensed") or "" for r in source_records]
    ts_list = sorted(t for t in ts_list if t)
    period_start = ts_list[0][:10] if ts_list else ""
    period_end = ts_list[-1][:10] if ts_list else ""
    symbols: list[str] = []
    for r in source_records:
        for s in r.get("symbols") or ([r["symbol"]] if r.get("symbol") else []):
            if s and s not in symbols:
                symbols.append(s)
    return {
        "id": _era_id(now),
        "status": "era_summary",
        "depth": depth,
        "ts_condensed": datetime.fromtimestamp(now or time.time(), tz=UTC).isoformat(),
        "period_start": period_start,
        "period_end": period_end,
        "count": len(source_records),
        "symbols": symbols,
        "summary": summary_text,
        **stats,
    }


def _stats(records: list[dict]) -> dict:
    outcomes = [(r.get("resolution") or r).get("outcome_label") for r in records]
    wins = sum(1 for o in outcomes if o == "win")
    misses = sum(1 for o in outcomes if o == "miss")
    alphas = [
        float(v)
        for r in records
        for v in [((r.get("resolution") or r).get("alpha_vs_benchmark_pct"))]
        if v is not None
    ]
    return {
        "win_count": wins,
        "miss_count": misses,
        "avg_alpha": round(sum(alphas) / len(alphas), 2) if alphas else None,
    }


# ── LLM call ──────────────────────────────────────────────────────────────────


async def _condense(records: list[dict], depth: int, router, model_key: str) -> str:
    """Call the LLM with CONDENSATION_SYSTEM and return the summary string."""
    payload = []
    for r in records:
        if r.get("status") == "era_summary":
            payload.append(
                {
                    "type": "era_summary",
                    "depth": r.get("depth"),
                    "period": f"{r.get('period_start')} → {r.get('period_end')}",
                    "summary": r.get("summary"),
                    **_stats([r]),
                }
            )
        else:
            payload.append(
                {
                    "symbol": r.get("symbol"),
                    "direction": r.get("direction"),
                    "prediction": r.get("prediction"),
                    "rationale": r.get("rationale"),
                    "indicators": r.get("indicators_at_open"),
                    "outcome": (r.get("resolution") or {}).get("outcome_label"),
                    "alpha": (r.get("resolution") or {}).get("alpha_vs_benchmark_pct"),
                    "lesson": (r.get("resolution") or {}).get("lesson"),
                }
            )

    depth_hint = (
        ""
        if depth == 1
        else f" These are already condensed summaries (depth {depth - 1}); compress further."
    )
    system = prompts.CONDENSATION_SYSTEM + depth_hint

    try:
        result = await router.call(
            model_key,
            system,
            [{"role": "user", "content": json.dumps(payload, default=str)}],
            cluster="trading",
            cell="compaction",
            max_tokens=400,
        )
        return str(result).strip() or "(no summary generated)"
    except Exception as e:  # pragma: no cover
        logger.warning("[compaction] LLM condensation failed: %s", e)
        symbols = list({r.get("symbol") for r in records if r.get("symbol")})
        return f"[auto] {len(records)} entries condensed — key symbols: {', '.join(symbols[:5])}"


# ── main entry points ─────────────────────────────────────────────────────────


async def compact_journal(router=None, model_key: str = "local") -> int:
    """Condense old resolved records into era summaries.

    Returns the total number of records compacted. Safe to call repeatedly —
    no-ops when below thresholds. If no router, hard-drops to keep files bounded
    (last resort; always prefer calling with a router from after_job).
    """
    compacted = 0
    compacted += await _compact_level(
        0, "resolved", "trading_journal_max_resolved", "_DEFAULT_MAX_RESOLVED", router, model_key
    )
    compacted += await _compact_level(
        1, "era_summary", "trading_journal_max_era_summaries", "_DEFAULT_MAX_ERA", router, model_key
    )
    # depth-2 summaries are never compacted further — they persist as permanent
    # condensed memory.
    return compacted


async def _compact_level(
    depth: int,
    status_filter: str | None,
    settings_key: str,
    default_attr: str,
    router,
    model_key: str,
) -> int:
    records = store.read_jsonl(store.JOURNAL_JSONL_PATH)
    if not records:
        return 0

    batch_size = _cfg("trading_compaction_batch_size", "_DEFAULT_BATCH")
    max_count = _cfg(settings_key, default_attr)

    # Separate the records we might compact from everything else
    if status_filter == "resolved":
        candidates = [r for r in records if r.get("status") == "resolved"]
    else:
        candidates = [
            r for r in records if r.get("status") == "era_summary" and r.get("depth") == depth
        ]
    others = [r for r in records if r not in candidates]

    if len(candidates) <= max_count:
        return 0

    # Take the oldest batch from the candidates that exceed the threshold
    overflow = len(candidates) - max_count
    batch = candidates[: min(overflow + batch_size, len(candidates))]
    batch = batch[:batch_size]  # cap at one batch per call to keep latency bounded
    remaining = [r for r in candidates if r not in batch]

    if router is not None:
        summary_text = await _condense(batch, depth + 1, router, model_key)
    else:
        # No router — hard-drop as a last resort so files never grow unbounded
        removed = len(batch)
        store.rewrite_jsonl(store.JOURNAL_JSONL_PATH, others + remaining)
        logger.warning(
            "[compaction] no router — hard-dropped %d depth-%d entries (no compaction)",
            removed,
            depth,
        )
        return removed

    era = _era_record(depth + 1, batch, summary_text, _stats(batch))
    # Era summaries go at the front (oldest position), before remaining candidates
    store.rewrite_jsonl(store.JOURNAL_JSONL_PATH, others + [era] + remaining)
    logger.info(
        "[compaction] condensed %d depth-%d records into era_summary (depth %d)",
        len(batch),
        depth,
        depth + 1,
    )
    return len(batch)


async def compact_journal_md(router=None, model_key: str = "local") -> None:
    """Condense the oldest section of journal.md when it exceeds the size limit.

    If no router, falls back to hard-truncation at a '---' boundary.
    """
    if not store.JOURNAL_MD_PATH.exists():
        return
    max_bytes = _cfg("trading_journal_md_max_kb", "_DEFAULT_MD_MAX_KB") * 1024
    size = store.JOURNAL_MD_PATH.stat().st_size
    if size <= max_bytes:
        return

    try:
        content = store.JOURNAL_MD_PATH.read_text(encoding="utf-8")
    except OSError as e:  # pragma: no cover
        logger.warning("[compaction] journal.md read failed: %s", e)
        return

    # Split at a '---' separator roughly at the midpoint — keep the newer half
    # and condense the older half.
    sections = content.split("\n---\n")
    if len(sections) < 2:
        # No separators — just truncate to the last max_bytes as fallback
        store.atomic_write_text(store.JOURNAL_MD_PATH, content[-max_bytes:])
        return

    mid = len(sections) // 2
    old_sections = sections[:mid]
    new_sections = sections[mid:]
    old_text = "\n---\n".join(old_sections)

    if router is not None:
        try:
            condensed = await router.call(
                model_key,
                prompts.CONDENSATION_SYSTEM,
                [
                    {
                        "role": "user",
                        "content": f"Condense this trading journal section "
                        f"into 2-4 sentences capturing only the most durable insights:\n\n{old_text[:4000]}",
                    }
                ],
                cluster="trading",
                cell="compaction",
                max_tokens=200,
            )
            condensed = str(condensed).strip()
        except Exception as e:  # pragma: no cover
            logger.warning("[compaction] journal.md LLM condensation failed: %s", e)
            condensed = f"[condensed {len(old_sections)} entries]"
    else:
        condensed = f"[condensed {len(old_sections)} entries — no router available]"

    period = ""
    for section in old_sections[:1] + old_sections[-1:]:
        import re

        m = re.search(r"(\d{4}-\d{2}-\d{2})", section)
        if m:
            period = m.group(1)
            break

    prefix = f"## Condensed era ({period}, {len(old_sections)} entries)\n\n{condensed}\n\n"
    new_content = prefix + "---\n\n" + "\n---\n".join(new_sections)
    store.atomic_write_text(store.JOURNAL_MD_PATH, new_content)
    logger.info(
        "[compaction] condensed journal.md: %dKB → ~%dKB",
        size // 1024,
        len(new_content) // 1024,
    )


def era_summary_lessons(symbol: str | None = None) -> list[str]:
    """Return condensed era summaries relevant to a symbol, for before_plan injection.

    Era summaries with no symbol filter always apply (they span the whole history).
    """
    records = store.read_jsonl(store.JOURNAL_JSONL_PATH)
    summaries = []
    for r in records:
        if r.get("status") != "era_summary":
            continue
        if symbol:
            syms = r.get("symbols") or []
            if syms and symbol.upper() not in [s.upper() for s in syms]:
                continue
        text = r.get("summary", "")
        if text:
            depth = r.get("depth", 1)
            period = f"{r.get('period_start', '')}→{r.get('period_end', '')}"
            summaries.append(f"[era memory depth={depth} {period}]: {text}")
    return summaries
