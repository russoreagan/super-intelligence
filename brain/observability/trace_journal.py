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
lightweight summary dict) here. EVERY consolidation pass — the in-process one
(session_loops ``_run_consolidation``) and the end-of-session shutdown pass
(brain_session) alike — rotates the pending journal aside into its OWN in-flight
file (``rotate_inflight`` returns that file's token) at the instant it snapshots
the buffers, and deletes exactly that file (``clear_inflight(token)``) the moment
the pass succeeds. Anything left behind — a ``pending`` file from a crash before
consolidation, an in-flight file from a crash DURING one or from a pass that
raised — is replayed at the next boot (``load_orphans``) back into the session
buffers, so the next consolidation folds it in.

Exactly-once is the contract both directions: a consolidated batch is cleared
once and never replayed, and a batch that did NOT commit is never cleared by a
LATER pass's success. (Before 2026-10-11 the shutdown pass skipped rotate/clear,
so load_orphans' re-staged ``pending`` file survived every graceful restart and
the same turns were re-consolidated — Hebbian double-counted — on every boot.
And a single shared in-flight file let one pass's success delete another pass's
uncommitted batch.) Attribution survives the
round trip: each ``TurnTrace`` carries its own ``persona_name``, and the Hebbian
pass groups by that, so a replayed Analyst turn credits the Analyst exactly as if
the crash never happened.

Tier-agnostic by construction: it journals whatever turns ran, lite or full.

LATE CREDIT. Some of a turn's grounded outcome arrives after the turn ended and
was journaled: a background job the turn started finishes (job_success /
job_failure DA), or an external grade is posted for it (``api_grade_turn``). The
caller credits the LIVE trace in the buffer and ``append_late`` journals the same
credit as a ``late`` line keyed by ``turn_key``. Boot replay applies late lines to
the turns they name, so a crash between the credit and the next consolidation
cannot lose it. Exactly-once holds because the late line rides the same pending ->
in-flight -> cleared lifecycle as the turn line: a credit made while the turn is
buffered is committed (or replayed) with it, and a late line whose turn already
committed finds no turn at replay and is dropped. A credit for a turn that is no
longer buffered is never journaled; the caller records it as dropped.

Never raises into the turn or consolidation path (mirrors ``learning_ledger``):
the durability layer must not be able to break the learning it protects. Toggle
off with BRAIN_TRACE_JOURNAL=false.

A pass that is CUT SHORT — the gateway's SIGKILL at the end of a redeploy's
grace or of the Sleep button's wait, both shorter than a full pass on a slow pod —
leaves its in-flight file for replay, yet the stages that ran before the kill
already committed. The stage ledger (``consolidated_turns.json``) makes each stage
exactly-once per turn on its own: sleep marks a turn's key the moment its Hebbian
pass, and then its persona's memory passes, commit, and every later pass drops
already-marked turns from that stage (``unapplied``). Without it, prod 2026-10-11
re-ran the Hebbian pass over the same 24 turns seven times in four hours — one
cut Sleep, five redeploys killed 3 s in, then the idle pass that finally
completed. Sleep also clears the in-flight file as soon as those core stages
commit, before its optional tail (narrator, chunk mining, authoring, prune).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_PENDING = "pending_traces.jsonl"
# Legacy single in-flight file (pre per-pass tokens) — still read by boot replay
# and scrubbed by erasure so an upgrade can't strand or leak its batch.
_INFLIGHT = "pending_traces.inflight.jsonl"
_INFLIGHT_GLOB = "pending_traces.inflight*.jsonl"
# Stage ledger: per consolidation stage, the keys of the turns it has committed.
_APPLIED = "consolidated_turns.json"
# Keys kept per stage. A replayed turn is consolidated in the very next pass, along
# with everything buffered since, so the ledger only has to outlive one pass.
_APPLIED_KEEP = 5000
STAGE_HEBBIAN = "hebbian"
STAGE_MEMORY = "memory"
STAGE_ALLOSTASIS = "allostasis"


def _enabled() -> bool:
    return os.environ.get("BRAIN_TRACE_JOURNAL", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _root() -> Path:
    # Process-home root (empty persona → the active/boot root). One journal per
    # process; each line carries its own persona_name, so a shared-instance brain
    # serving many personas replays them all and consolidation re-groups by persona.
    from brain.persona_key import persona_state_root

    return persona_state_root("")


def _paths() -> tuple[Path, Path]:
    root = _root()
    return root / _PENDING, root / _INFLIGHT


def _inflight_files(root: Path) -> list[Path]:
    """Every in-flight file (legacy single + per-pass), oldest first."""
    try:
        files = list(root.glob(_INFLIGHT_GLOB))
    except Exception:
        return []

    def _key(p: Path):
        try:
            return (p.stat().st_mtime, p.name)
        except OSError:
            return (0.0, p.name)

    return sorted(files, key=_key)


def _all_files() -> list[Path]:
    root = _root()
    return [*_inflight_files(root), root / _PENDING]


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


LATE_EMISSION = "reward_emission"
LATE_GRADE = "external_grade"


def append_late(trace, kind: str, **payload) -> None:
    """Journal a credit that reached ``trace`` after its turn was journaled: a
    late grounded DA emission (``LATE_EMISSION``, payload ``emission=``) or an
    external grade (``LATE_GRADE``, payload ``grade=``, ``source=``). The caller
    has already applied it to the live trace; this line re-applies it at boot
    replay. Carries the trace's persona / end-user stamps so erasure and persona
    purge scrub it with its turn. Never raises."""
    if not _enabled():
        return
    key = turn_key(trace)
    if not key or kind not in (LATE_EMISSION, LATE_GRADE):
        return
    try:
        stamps = {
            "persona": getattr(trace, "persona_name", "") or "",
            "end_user_id": getattr(trace, "end_user_id", "") or "",
        }
        line = json.dumps({"late": {"key": key, "kind": kind, **payload}, "s": stamps}, default=str)
        pending, _ = _paths()
        with _lock:
            pending.parent.mkdir(parents=True, exist_ok=True)
            with pending.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as e:
        logger.debug("[trace_journal] append_late failed: %s", e)


def _apply_late(trace, late: dict) -> None:
    kind = late.get("kind")
    if kind == LATE_EMISSION:
        emission = late.get("emission")
        if isinstance(emission, dict):
            if not isinstance(getattr(trace, "reward_emissions", None), list):
                trace.reward_emissions = []
            trace.reward_emissions.append(dict(emission))
    elif kind == LATE_GRADE:
        grade = late.get("grade")
        if grade is not None:
            trace.external_grade = float(grade)
            trace.external_grade_source = str(late.get("source") or "")


def rotate_inflight() -> str | None:
    """Move the pending journal aside into this pass's own in-flight file just
    before a consolidation pass runs, so a crash mid-pass still leaves the traces
    for boot replay. Call at the same point the in-memory buffers are snapshotted
    (+ cleared). Returns the token to hand to ``clear_inflight`` on success, or
    None when there was nothing pending (or the journal is off). Never raises."""
    if not _enabled():
        return None
    try:
        pending, _ = _paths()
        with _lock:
            if not pending.exists():
                return None
            token = f"pending_traces.inflight.{time.time_ns()}-{uuid.uuid4().hex[:8]}.jsonl"
            pending.replace(pending.with_name(token))  # atomic rename, same filesystem
            return token
    except Exception as e:
        logger.debug("[trace_journal] rotate failed: %s", e)
        return None


def clear_inflight(token: str | None) -> None:
    """Drop THIS pass's in-flight file the instant the pass SUCCEEDS — its traces
    are now committed. Only the named file: another pass's uncommitted batch (one
    that raised, or was cancelled at shutdown) stays for boot replay. None is a
    no-op (nothing was rotated). Never raises."""
    if not _enabled() or not token:
        return
    try:
        name = Path(token).name  # a token is a bare file name; never a path elsewhere
        if not name.startswith("pending_traces.inflight"):
            return
        with _lock:
            target = _root() / name
            if target.exists():
                target.unlink()
    except Exception as e:
        logger.debug("[trace_journal] clear failed: %s", e)


def inflight_count() -> int:
    """In-flight batches on disk: one per pass running now, plus any a pass left
    uncommitted (it raised, or the process was killed mid-pass). 0 when off."""
    if not _enabled():
        return 0
    try:
        return len(_inflight_files(_root()))
    except Exception:
        return 0


def turn_key(item) -> str:
    """Stable key for one turn — ``<session_id>:<turn_id>`` — from a ``TurnTrace``
    or its summary dict ("" when it carries no turn id: never deduplicated). The
    session id disambiguates the short per-turn ids; a replayed turn keeps both."""
    if isinstance(item, dict):
        tid, sid = item.get("turn_id"), item.get("session_id")
    else:
        tid, sid = getattr(item, "turn_id", None), getattr(item, "session_id", None)
    if not isinstance(tid, str) or not tid:
        return ""
    return f"{sid if isinstance(sid, str) else ''}:{tid}"


def committed(stage: str, item) -> bool:
    """Whether ``stage`` already committed this turn (stage ledger). False when
    the turn has no key, the journal is off, or the ledger can't be read."""
    key = turn_key(item)
    if not _enabled() or not key:
        return False
    try:
        with _lock:
            return key in set(_read_applied(_root() / _APPLIED).get(stage) or [])
    except Exception:
        return False


def _read_applied(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def unapplied(stage: str, items: list) -> list:
    """``items`` minus the turns ``stage`` already committed in an earlier pass.
    Order is kept; items without a turn key always pass. Never raises — on any
    failure everything passes (the pre-ledger behaviour)."""
    if not _enabled() or not items:
        return list(items or [])
    try:
        with _lock:
            done = set(_read_applied(_root() / _APPLIED).get(stage) or [])
        if not done:
            return list(items)
        return [it for it in items if turn_key(it) not in done]
    except Exception as e:
        logger.debug("[trace_journal] applied read failed: %s", e)
        return list(items)


def mark_applied(stage: str, items: list) -> None:
    """Record that ``stage`` has committed these turns, so a replay of a pass that
    was cut short after this point skips them. Call the moment the stage commits.
    Never raises."""
    if not _enabled():
        return
    keys = [k for k in (turn_key(it) for it in items or []) if k]
    if not keys:
        return
    try:
        path = _root() / _APPLIED
        with _lock:
            data = _read_applied(path)
            have = [k for k in (data.get(stage) or []) if isinstance(k, str)]
            seen = set(have)
            have.extend(k for k in keys if k not in seen and not seen.add(k))
            data[stage] = have[-_APPLIED_KEEP:]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(path)
    except Exception as e:
        logger.debug("[trace_journal] mark_applied failed: %s", e)


def _line_end_user(line: str) -> str:
    try:
        rec = json.loads(line)
    except Exception:
        return ""
    f = rec.get("f") or {}
    s = rec.get("s") or {}
    return str(f.get("end_user_id") or s.get("end_user_id") or "")


def scrub_end_user(end_user_id: str) -> int:
    """Right-to-erasure: rewrite BOTH journal files (pending + inflight) without any
    line stamped with `end_user_id`, so a crash replay can never re-stage — and the
    next consolidation can never relearn — an erased customer's turns. Runs under
    the same lock as append/rotate. Returns the number of lines dropped; never
    raises (an erasure step reports its own failure, it does not abort the rest)."""
    end_user_id = (end_user_id or "").strip()
    if not _enabled() or not end_user_id:
        return 0
    dropped = 0
    try:
        with _lock:
            for p in _all_files():
                if not p.exists():
                    continue
                lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
                kept = [ln for ln in lines if _line_end_user(ln) != end_user_id]
                if len(kept) == len(lines):
                    continue
                dropped += len(lines) - len(kept)
                if kept:
                    tmp = p.with_suffix(p.suffix + ".tmp")
                    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
                    tmp.replace(p)
                else:
                    p.unlink()
    except Exception as e:
        logger.warning("[trace_journal] scrub_end_user failed: %s", e)
    return dropped


def _line_persona(line: str) -> str:
    try:
        rec = json.loads(line)
    except Exception:
        return ""
    f = rec.get("f") or {}
    s = rec.get("s") or {}
    from brain.persona_key import persona_slug

    return persona_slug(s.get("persona") or f.get("persona_name") or "")


def scrub_persona(persona: str) -> int:
    """Persona hard purge: rewrite both journal files without any line stamped
    with `persona`, so a boot replay can never re-stage a purged persona's turns.
    Returns the lines dropped; never raises."""
    from brain.persona_key import persona_slug

    slug = persona_slug(persona)
    if not _enabled() or not slug:
        return 0
    dropped = 0
    try:
        with _lock:
            for p in _all_files():
                if not p.exists():
                    continue
                lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
                kept = [ln for ln in lines if _line_persona(ln) != slug]
                if len(kept) == len(lines):
                    continue
                dropped += len(lines) - len(kept)
                if kept:
                    tmp = p.with_suffix(p.suffix + ".tmp")
                    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
                    tmp.replace(p)
                else:
                    p.unlink()
    except Exception as e:
        logger.warning("[trace_journal] scrub_persona failed: %s", e)
    return dropped


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
        pending, _ = _paths()
        raw: list[str] = []
        fulls: list = []
        summaries: list[dict] = []
        with _lock:
            # In-flight batches (older, mid-pass or failed) first, then pending.
            for p in (*_inflight_files(pending.parent), pending):
                if p.exists():
                    raw.extend(
                        ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()
                    )
                    p.unlink()
            restage: list[str] = []
            late: list[tuple[str, dict]] = []
            by_key: dict[str, object] = {}
            for line in raw:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec.get("late"), dict):
                    late.append((line, rec["late"]))
                    continue
                f = rec.get("f") or {}
                try:
                    fulls.append(TurnTrace(**{k: v for k, v in f.items() if k in field_names}))
                    s = dict(rec.get("s") or {})
                    # Rebuild the attribution stamps sleep groups on from the full
                    # trace, so a line journaled by an older build (no stamp on the
                    # summary) still consolidates under its own persona.
                    if not s.get("persona"):
                        s["persona"] = f.get("persona_name") or ""
                    if not s.get("end_user_id"):
                        s["end_user_id"] = f.get("end_user_id") or ""
                    # The stage ledger keys the summary by its turn (older lines
                    # carry the ids on the full trace only).
                    s.setdefault("turn_id", f.get("turn_id") or "")
                    s.setdefault("session_id", f.get("session_id") or "")
                    summaries.append(s)
                    restage.append(line)
                    key = turn_key(fulls[-1])
                    if key:
                        by_key[key] = fulls[-1]
                except Exception:
                    continue
            # Late credits (job outcomes, grades) onto the turns they name, in the
            # order they were made. One whose turn is not here already committed
            # with it, or was erased: it is dropped, never re-staged.
            for line, rec in late:
                target = by_key.get(str(rec.get("key") or ""))
                if target is None:
                    continue
                try:
                    _apply_late(target, rec)
                    restage.append(line)
                except Exception:
                    continue
            if restage:  # re-stage durably before returning them to the buffers
                pending.parent.mkdir(parents=True, exist_ok=True)
                with pending.open("w", encoding="utf-8") as fh:
                    fh.write("\n".join(restage) + "\n")
        return fulls, summaries
    except Exception as e:
        logger.debug("[trace_journal] load_orphans failed: %s", e)
        return [], []
