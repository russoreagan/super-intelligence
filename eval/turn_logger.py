"""
EvalLogger — append-only JSONL writer for every brain turn.

Two record types in the file:
  {"type": "turn",       "turn_id": "...", ...all TurnTrace fields}
  {"type": "eval_patch", "turn_id": "...", ...fields to merge}

The report reader merges patches into turns by turn_id on read.
log_turn() is called synchronously from ObservabilityLayer.record_turn().
patch_turn() is called from background tasks (baseline, scorer) — thread-safe.

Engine-lane text is redacted at write. The log is process-wide and append-only,
so a partner customer's verbatim prompt/response written here could not be
erased by DELETE /v1/end_users/{id} — and no eval reader needs the text itself:
the scorer, emotion judge and baseline runner all read the in-memory TurnTrace,
never this file. So for a turn that carries an `api_session_id` (an engine-API
turn), `user_input`, `response` and `baseline_response` are replaced with
`sha256:<16 hex>/<len>` — enough to dedupe and size, nothing to erase. The owner
lane (no api_session_id: the interactive UI, the idle loop) is logged verbatim as
before. BRAIN_EVAL_LOG_AGENT_TEXT=verbatim is the operator escape hatch for an
eval session that wants the text.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain.observability.timeline import TurnTrace

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = Path("eval/turns.jsonl")

# Text fields a turn record / patch may carry verbatim.
_TEXT_FIELDS = ("user_input", "response", "baseline_response")
# How many engine-lane turn ids to remember so a late patch (baseline, judge) is
# redacted like its turn. Bounded: a long-lived process would otherwise grow it.
_REMEMBERED_TURNS = 2048


def agent_text_policy() -> str:
    """"verbatim" when the operator opted in via BRAIN_EVAL_LOG_AGENT_TEXT, else
    "redacted_at_write" — the value the erasure summary reports for `eval_log`."""
    if os.environ.get("BRAIN_EVAL_LOG_AGENT_TEXT", "").strip().lower() == "verbatim":
        return "verbatim"
    return "redacted_at_write"


def digest(text: object) -> str:
    """`sha256:<16 hex>/<len>` for a text field — dedupable and sizeable, not
    recoverable. Empty text stays empty so absence is still visible."""
    s = "" if text is None else str(text)
    if not s:
        return ""
    return f"sha256:{hashlib.sha256(s.encode('utf-8')).hexdigest()[:16]}/{len(s)}"


def log_path(default: Path | None = None) -> Path:
    """THE eval log path: BRAIN_EVAL_LOG (per tenant in multitenant boots — run.py
    points it under the org root), else the repo-relative default."""
    env_path = os.environ.get("BRAIN_EVAL_LOG")
    return Path(env_path) if env_path else (default or DEFAULT_LOG_PATH)


def scrub_persona(persona_slug: str, path: Path | None = None) -> int:
    """Persona hard purge (best-effort): rewrite the eval log without the rows
    stamped persona_name == slug. The log is append-only and process-wide, so
    this is a rewrite under the module lock; returns rows dropped, never raises.
    Rows are stamped with the slug (run.py normalises BRAIN_PERSONA_NAME)."""
    slug = (persona_slug or "").strip()
    p = path or log_path()
    if not slug or not p.is_file():
        return 0
    dropped = 0
    try:
        with _SCRUB_LOCK:
            lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
            kept: list[str] = []
            for ln in lines:
                try:
                    rec = json.loads(ln)
                except Exception:
                    kept.append(ln)
                    continue
                if str(rec.get("persona_name") or "") == slug:
                    dropped += 1
                    continue
                kept.append(ln)
            if dropped:
                tmp = p.with_suffix(p.suffix + ".tmp")
                tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
                tmp.replace(p)
    except Exception as e:
        logger.warning("[eval] scrub_persona failed: %s", e)
    return dropped


_SCRUB_LOCK = threading.Lock()


class EvalLogger:
    def __init__(self, log_path: Path | None = None) -> None:
        self._path = globals()["log_path"](log_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # turn_id → True for engine-lane turns seen by log_turn (bounded, insertion order).
        self._engine_turns: OrderedDict[str, bool] = OrderedDict()

    def _remember_engine_turn(self, turn_id: str) -> None:
        if not turn_id:
            return
        self._engine_turns[turn_id] = True
        while len(self._engine_turns) > _REMEMBERED_TURNS:
            self._engine_turns.popitem(last=False)

    def _redact(self, record: dict) -> dict:
        for f in _TEXT_FIELDS:
            if f in record:
                record[f] = digest(record[f])
        record["text_redacted"] = True
        return record

    def log_turn(self, trace: TurnTrace) -> None:
        """Write the initial JSONL record for a turn. Called synchronously."""
        record = dataclasses.asdict(trace)
        record["type"] = "turn"
        if record.get("api_session_id") and agent_text_policy() != "verbatim":
            with self._lock:
                self._remember_engine_turn(str(record.get("turn_id") or ""))
            record = self._redact(record)
        self._append(record)

    def patch_turn(self, turn_id: str, **fields) -> None:
        """Append a patch record. Called from background tasks."""
        patch = {"type": "eval_patch", "turn_id": turn_id, **fields}
        with self._lock:
            engine = turn_id in self._engine_turns
        if engine and agent_text_policy() != "verbatim":
            patch = self._redact(patch)
        self._append(patch)

    def _append(self, record: dict) -> None:
        with self._lock:
            try:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, default=str) + "\n")
            except Exception as e:
                logger.warning("EvalLogger: failed to write record: %s", e)
