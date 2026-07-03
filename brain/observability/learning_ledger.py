"""Per-persona learning ledger — the query surface behind the Learning tab.

The eval log (eval/turns.jsonl) mixes multi-KB turn traces with decision
records and carries neither persona nor session stamps; answering "how did this
edge drift and why" means re-scanning an unbounded file. The ledger fixes the
container, not the content: the SAME decision records the learning subsystems
already emit, filtered to the learning-relevant kinds, stamped with persona +
session, in a small rolling JSONL under the persona's second-brain root (so
multitenant persona routing scopes it for free).

Populated centrally from DecisionLog.log() — no learning-path call site knows
this file exists. Wiring history snapshots stay the source for drift SERIES;
the ledger holds the per-update EXPLANATIONS between snapshots.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Decision kinds worth keeping queryable per persona. Everything else stays
# eval-log-only (skip decisions, recruitment, etc. — diagnostics, not learning).
LEDGER_TYPES = {
    "hebbian_update_applied",
    "hebbian_update_skipped",
    "drafter_competition_applied",
    "switch_routing_credit_applied",
    "recall_routing_credit_applied",
    "session_plasticity_summary",
    "reward_emission",
    "external_grade_recorded",
    "learning_story",
}

_MAX_LINES = 5000  # rotation threshold …
_KEEP_LINES = 4000  # … and what survives a trim

_lock = threading.Lock()
_line_count: int | None = None  # cached; invalidated on rotation


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def ledger_path() -> Path:
    """Resolved at call time: SECOND_BRAIN_PATH is set per persona at boot
    (run.py), so the ledger lands in the active persona's root automatically."""
    root = Path(os.environ.get("SECOND_BRAIN_PATH", str(_repo_root() / "second_brain")))
    return root / "learning_ledger.jsonl"


def append(record: dict) -> None:
    """Stamp persona + session and append one line. Never raises — observability
    must not be able to break the learning path it observes."""
    global _line_count
    try:
        rec = dict(record)
        if not rec.get("persona"):
            try:
                from brain.persona_key import active_or_home_persona, persona_slug

                rec["persona"] = persona_slug(active_or_home_persona())
            except Exception:
                rec["persona"] = ""
        if not rec.get("session_id"):
            rec["session_id"] = _session_id()
        path = ledger_path()
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
            if _line_count is None:
                try:
                    with path.open("rb") as f:
                        _line_count = sum(1 for _ in f)
                except Exception:
                    _line_count = 0
            else:
                _line_count += 1
            if _line_count > _MAX_LINES:
                _rotate(path)
    except Exception as e:
        logger.debug("[learning_ledger] append failed: %s", e)


def _rotate(path: Path) -> None:
    """Keep the newest _KEEP_LINES lines. Called under _lock."""
    global _line_count
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        keep = lines[-_KEEP_LINES:]
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
        tmp.replace(path)
        _line_count = len(keep)
    except Exception as e:
        logger.debug("[learning_ledger] rotation failed: %s", e)
        _line_count = None


_session = ""


def set_session(session_id: str) -> None:
    global _session
    _session = str(session_id or "")


def _session_id() -> str:
    return _session


def read(
    limit: int = 500,
    decision: str | None = None,
    edge: str | None = None,
    session_id: str | None = None,
    since_ts: float | None = None,
    path: Path | None = None,
) -> list[dict]:
    """Newest-last filtered read of (by default) the active persona's ledger.
    Pass `path` to read another persona's file (learning_reader does)."""
    p = path or ledger_path()
    try:
        if not p.exists():
            return []
        out: list[dict] = []
        src, tgt = "", ""
        if edge:
            src, _, tgt = edge.partition("→")
            src, tgt = src.strip(), tgt.strip()
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if decision and r.get("decision") != decision:
                continue
            if session_id and r.get("session_id") != session_id:
                continue
            if since_ts and float(r.get("ts") or 0) < since_ts:
                continue
            if edge and not (
                (r.get("src") == src and r.get("tgt") == tgt)
                or r.get("edge") == edge
                or r.get("switch") == tgt.replace("temporal.", "")
            ):
                continue
            out.append(r)
        return out[-max(1, limit):]
    except Exception as e:
        logger.debug("[learning_ledger] read failed: %s", e)
        return []
