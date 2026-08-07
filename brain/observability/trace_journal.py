"""Crash-safe durable journal for un-consolidated turn traces.

Turn learning — the Hebbian wiring updates, plasticity summaries, and
memory/self-model consolidation — is computed in a BATCH at sleep time from the
in-memory trace buffers (``_session_traces`` / ``_session_traces_full``). The
reward_emissions that gate that learning are already written live to the ledger,
but the fired-path ``TurnTrace`` objects that drive the Hebbian pass live only in
RAM until a consolidation runs. A graceful exit commits them — SIGTERM /
end-of-session shutdown, an idle periodic-sleep pass, or the 300-turn trace-cap
backstop all consolidate the buffer. An UNgraceful exit (OOM, SIGKILL, a hard
crash) drops whatever was buffered.

This journal closes that hole. Every turn appends its full trace (plus the
lightweight summary dict) here. A consolidation pass rotates the journal aside
(``rotate_inflight``) at the instant it snapshots the buffers, and drops it
(``clear_inflight``) the moment the pass succeeds. Anything left behind — a
``pending`` file from a crash before consolidation, an ``inflight`` file from a
crash DURING one — is replayed at the next boot (``load_orphans``) back into the
session buffers, so the next consolidation folds it in. Attribution survives the
round trip: each ``TurnTrace`` carries its own ``persona_name``, and the Hebbian
pass groups by that, so a replayed Analyst turn credits the Analyst exactly as if
the crash never happened.

Tier-agnostic by construction: it journals whatever turns ran, lite or full.

Never raises into the turn or consolidation path (mirrors ``learning_ledger``):
the durability layer must not be able to break the learning it protects. Toggle
off with BRAIN_TRACE_JOURNAL=false.

Exactly-once caveat: a crash in the microsecond window between a consolidation
SUCCEEDING and ``clear_inflight`` running would replay one already-committed
batch, double-applying its (bounded, decay-toward-rest) Hebbian deltas and
duplicating one plasticity-summary row. clear runs immediately on success to keep
that window near-zero; losing the batch entirely is the worse failure, and the
live-written reward_emissions are never double-counted (they don't flow through
here).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_PENDING = "pending_traces.jsonl"
_INFLIGHT = "pending_traces.inflight.jsonl"


def _enabled() -> bool:
    return os.environ.get("BRAIN_TRACE_JOURNAL", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _paths() -> tuple[Path, Path]:
    # Process-home root (empty persona → the active/boot root). One journal per
    # process; each line carries its own persona_name, so a shared-instance brain
    # serving many personas replays them all and consolidation re-groups by persona.
    from brain.persona_key import persona_state_root

    root = persona_state_root("")
    return root / _PENDING, root / _INFLIGHT


def append(full_trace, summary: dict | None) -> None:
    """Append one turn — the full ``TurnTrace`` and its lightweight summary dict —
    to the pending journal. Called once per turn, after both buffers have it.
    Never raises."""
    if not _enabled():
        return
    try:
        full = (
            dataclasses.asdict(full_trace)
            if dataclasses.is_dataclass(full_trace)
            else dict(full_trace or {})
        )
        line = json.dumps({"f": full, "s": summary or {}}, default=str)
        pending, _ = _paths()
        with _lock:
            pending.parent.mkdir(parents=True, exist_ok=True)
            with pending.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as e:  # never break the turn path
        logger.debug("[trace_journal] append failed: %s", e)


def rotate_inflight() -> None:
    """Move the pending journal aside just before a consolidation pass runs, so a
    crash mid-pass still leaves the traces for boot replay. Call under the
    consolidation lock, at the same point the in-memory buffers are snapshotted +
    cleared. Never raises."""
    if not _enabled():
        return
    try:
        pending, inflight = _paths()
        with _lock:
            if not pending.exists():
                return
            if inflight.exists():
                # A prior inflight survived (a crash between two passes that boot
                # replay hasn't cleaned up yet) — merge rather than clobber, so no
                # batch is silently dropped.
                with (
                    inflight.open("a", encoding="utf-8") as dst,
                    pending.open("r", encoding="utf-8") as src,
                ):
                    dst.write(src.read())
                pending.unlink()
            else:
                pending.replace(inflight)  # atomic rename on the same filesystem
    except Exception as e:
        logger.debug("[trace_journal] rotate failed: %s", e)


def clear_inflight() -> None:
    """Drop the in-flight journal the instant a consolidation pass SUCCEEDS — its
    traces are now committed. Never raises."""
    if not _enabled():
        return
    try:
        _, inflight = _paths()
        with _lock:
            if inflight.exists():
                inflight.unlink()
    except Exception as e:
        logger.debug("[trace_journal] clear failed: %s", e)


def load_orphans() -> tuple[list, list[dict]]:
    """Boot-time replay. Read any traces a prior run left un-consolidated (both the
    ``inflight`` file from a crash during a pass and the ``pending`` file from a
    crash before one), reconstruct ``TurnTrace`` objects + their summary dicts, and
    re-stage the survivors in a fresh ``pending`` journal so they stay durable
    across a SECOND crash before they're consolidated. Returns (full_traces,
    summaries) for the caller to extend the session buffers with. Never raises —
    returns ([], []) on any failure."""
    if not _enabled():
        return [], []
    try:
        from brain.observability.timeline import TurnTrace

        field_names = {f.name for f in dataclasses.fields(TurnTrace)}
        pending, inflight = _paths()
        raw: list[str] = []
        fulls: list = []
        summaries: list[dict] = []
        with _lock:
            for p in (inflight, pending):  # inflight = the older, mid-pass batch
                if p.exists():
                    raw.extend(
                        ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()
                    )
                    p.unlink()
            for line in raw:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                f = rec.get("f") or {}
                try:
                    fulls.append(TurnTrace(**{k: v for k, v in f.items() if k in field_names}))
                    summaries.append(rec.get("s") or {})
                except Exception:
                    continue
            if raw:  # re-stage durably before returning them to the buffers
                pending.parent.mkdir(parents=True, exist_ok=True)
                with pending.open("w", encoding="utf-8") as fh:
                    fh.write("\n".join(raw) + "\n")
        return fulls, summaries
    except Exception as e:
        logger.debug("[trace_journal] load_orphans failed: %s", e)
        return [], []
