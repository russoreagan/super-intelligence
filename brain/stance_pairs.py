"""
stance_pairs — per-persona ledger of (info stance, method stance) pairings.

Phase-1 learning is MARGINAL (per-axis stance credit on the approach anchor);
pairs are recorded from day one because the cost is one dict update per turn and
not recording means starting the clock from zero whenever pair learning becomes
worthwhile.

STORES COUNTS, NEVER A RATE. Pair win rate is confounded by the marginals: if an
info stance wins 70% of the time on its own, every pair containing it looks
strong, and a naive later pass would "discover" pairs that merely restate what
the per-axis weights already encode. The signal that justifies pair learning is
the RESIDUAL — observed pair win rate minus what the two marginal weights
predict — and keeping counts means the residual is computable at read time
against whatever the marginals are THEN.

Columns:
  plays     — the pair was a candidate in a competition
  wins      — the critic selected it (self-graded preference)
  ext_wins  — an external verdict (thumbs/partner grade ≥ 0) landed on the turn
  confirmed — the outcome verifier ratified the pair's information_need
`confirmed` is the column that matters; wins is the fallback where no
verification landed.

Activation (unscheduled): residuals over pairs with ≥ stance_pair_min_plays,
candidate space bounded by WEAK effect heredity — an interaction enters only if
at least one parent main effect is strong (Hamada & Wu 1992; Chipman 1996; Bien,
Taylor & Tibshirani 2013 — see docs/THEORY_CITATIONS.md). If every residual is
~0 the marginal model is sufficient and pair learning should be cancelled, not
built.

Storage: a plain dict the caller owns (persona-scoped, persisted with the
persona's other ledgers); this module is pure functions over it, riding
bounded_ledger's decay/age-out/cap-evict so the ledger can't grow with the pool.
"""

from __future__ import annotations

import time as _time

from brain.bounded_ledger import cap_evict
from brain.settings import settings

_KEY_SEP = "␟"  # unit-separator glyph — cannot appear in a skill id


def pair_key(info_id: str, method_id: str) -> str:
    return f"{info_id}{_KEY_SEP}{method_id}"


def split_key(key: str) -> tuple[str, str]:
    a, _, b = key.partition(_KEY_SEP)
    return a, b


def record_candidate(
    ledger: dict,
    info_id: str,
    method_id: str,
    *,
    won: bool,
    now: float | None = None,
) -> None:
    """One competition appearance. Call for EVERY candidate; won=True only for the
    selected one."""
    if not settings.get("stance_pair_ledger", 1) or not info_id or not method_id:
        return
    now = _time.time() if now is None else now
    row = ledger.setdefault(
        pair_key(info_id, method_id),
        {"plays": 0, "wins": 0, "ext_wins": 0, "confirmed": 0, "last_ts": now},
    )
    row["plays"] += 1
    if won:
        row["wins"] += 1
    row["last_ts"] = now
    cap = int(settings.get("stance_pair_cap", 256))
    if len(ledger) > cap:
        for k, _row in cap_evict(list(ledger.items()), cap, staleness=lambda kv: kv[1]["last_ts"]):
            ledger.pop(k, None)


def record_verdict(
    ledger: dict, info_id: str, method_id: str, *, column: str, now: float | None = None
) -> None:
    """Late-arriving evidence on an existing pair: column ∈ {"ext_wins", "confirmed"}.
    A pair already evicted stays evicted — late evidence never resurrects a row
    (that would let sparse signals defeat the cap)."""
    if column not in ("ext_wins", "confirmed"):
        return
    row = ledger.get(pair_key(info_id, method_id))
    if row is None:
        return
    row[column] += 1
    row["last_ts"] = _time.time() if now is None else now


def residual(ledger: dict, info_id: str, method_id: str, marginal_win_rate: float) -> float | None:
    """Observed pair rate minus the marginal prediction — None until the pair has
    earned trust (stance_pair_min_plays). Uses `confirmed` when any verification
    has landed for the pair, else `wins`."""
    row = ledger.get(pair_key(info_id, method_id))
    if row is None or row["plays"] < int(settings.get("stance_pair_min_plays", 25)):
        return None
    numer = row["confirmed"] if row["confirmed"] > 0 else row["wins"]
    return numer / row["plays"] - max(0.0, min(1.0, float(marginal_win_rate)))
