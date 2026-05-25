"""
EvalLogger — append-only JSONL writer for every brain turn.

Two record types in the file:
  {"type": "turn",       "turn_id": "...", ...all TurnTrace fields}
  {"type": "eval_patch", "turn_id": "...", ...fields to merge}

The report reader merges patches into turns by turn_id on read.
log_turn() is called synchronously from ObservabilityLayer.record_turn().
patch_turn() is called from background tasks (baseline, scorer) — thread-safe.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain.observability.timeline import TurnTrace

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = Path("eval/turns.jsonl")


class EvalLogger:
    def __init__(self, log_path: Path | None = None) -> None:
        env_path = os.environ.get("BRAIN_EVAL_LOG")
        self._path = Path(env_path) if env_path else (log_path or DEFAULT_LOG_PATH)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log_turn(self, trace: TurnTrace) -> None:
        """Write the initial JSONL record for a turn. Called synchronously."""
        record = dataclasses.asdict(trace)
        record["type"] = "turn"
        self._append(record)

    def patch_turn(self, turn_id: str, **fields) -> None:
        """Append a patch record. Called from background tasks."""
        patch = {"type": "eval_patch", "turn_id": turn_id, **fields}
        self._append(patch)

    def _append(self, record: dict) -> None:
        with self._lock:
            try:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, default=str) + "\n")
            except Exception as e:
                logger.warning("EvalLogger: failed to write record: %s", e)
