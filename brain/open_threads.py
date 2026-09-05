"""
Open-threads ledger — the DMN's working memory of unfinished ideas.

The brain already keeps a hand-maintained `open_questions.md` ("Open Questions &
Projects"). Until now the DMN only *read* the Projects section from it; thoughts
themselves had no durable home, so the same idea was re-derived every idle tick
and never made progress. This module gives the DMN a managed `## Open threads`
section in that same file: it can OPEN a thread when it starts an unfinished
idea, ADVANCE it as it makes progress, and RESOLVE it when it concludes (the
conclusion is then committed to episodic memory by the caller).

Design choices:
- One file stays the single active ledger (the user's decision). The Projects /
  Architecture / Philosophical sections are hand-authored and never touched here;
  we only manage the `## Open threads` section body.
- The section body is a fenced ```json block. JSON round-trips losslessly (the
  threads carry structured state — advances, wall-clock timestamps, relational
  tags) while still being legible and hand-editable inside the markdown file.
- Wall-clock timestamps (not tick counts) so age-out works even when the DMN is
  suppressed under load and ticks stop firing.

This module is pure/synchronous over a string + a dataclass; the DMN owns the
async read/write through the SchemaStore. That keeps the markdown round-trip
fully unit-testable without a running brain.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field

from brain.bounded_ledger import aged_out, cap_evict

SECTION = "Open threads"

# The ledger is AGENT-scoped (persona x mandate), not persona-scoped like self.md and
# user.md. "What work am I pre-authorized to run autonomously" is a property of the
# job, not the temperament: one persona wearing two mandates (e.g. the_analyst as both
# day_trading_analyst and trading_mispricing) must not share one authorization list,
# and load_core_context() puts this whole file in EVERY turn's prompt — so a shared
# file also leaks one mandate's projects into the other's context.
#
# BASE_LEDGER_FILE stays the name for an unscoped context (local dev, a persona with no
# full-tier agent). LEDGER_FILE is kept as an alias: it is the correct answer whenever
# no mandate resolves, and several tests reference it directly.
BASE_LEDGER_FILE = "open_questions.md"
LEDGER_FILE = BASE_LEDGER_FILE

# Mandate ids are validated slugs (brain/ids.py ID_RE allows dashes), but the schema
# store's filename guard is ^[A-Za-z0-9_-]+\.md$ — no dots. Fold anything else out so a
# malformed mandate can never produce a rejected (silently unwritable) filename.
_MANDATE_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


def ledger_file(mandate_id: str = "") -> str:
    """The projects-ledger filename for one mandate ("" → the base ledger)."""
    slug = _MANDATE_SLUG_RE.sub("_", str(mandate_id or "").strip()).strip("_")
    return f"open_questions__{slug}.md" if slug else BASE_LEDGER_FILE


def active_mandate() -> str:
    """The mandate owning the current context, or "".

    Two lanes reach the ledger and they identify themselves differently:
      - an engine/API turn binds turn_ctx with agent_id ("persona.mandate"), so the
        mandate is read straight off the bound turn;
      - the DMN idle lane binds no turn, so it falls back to the full-tier agent of
        the persona currently bound on the store (agents.owning_mandate).
    Fails closed to "" (the base ledger) rather than guessing another mandate's file.
    """
    try:
        from brain.turn_ctx import current_turn

        agent_id = str(current_turn().get("agent_id") or "")
        if "." in agent_id:
            mandate = agent_id.rsplit(".", 1)[1].strip()
            if mandate:
                return mandate
    except Exception:
        pass
    try:
        from brain import agents
        from brain.second_brain.store import active_persona

        persona = active_persona() or os.environ.get("BRAIN_PERSONA_NAME", "")
        return agents.owning_mandate(persona) if persona else ""
    except Exception:
        return ""


def active_ledger_file() -> str:
    """The ledger filename for the current turn/persona. Use this at every call
    site instead of the LEDGER_FILE constant."""
    return ledger_file(active_mandate())


# Wall-clock age-out: a thread open past this retires at the next idle sweep even
# if it never hit the advance cap (which can't fire while the DMN is suppressed).
THREAD_MAX_AGE_S = float(os.environ.get("BRAIN_DMN_THREAD_MAX_AGE_S", str(3 * 24 * 3600)))
THREAD_MAX_ADVANCES = int(os.environ.get("BRAIN_DMN_THREAD_MAX_ADVANCES", "4"))
MAX_OPEN_THREADS = int(os.environ.get("BRAIN_DMN_MAX_OPEN_THREADS", "6"))

STATUS_OPEN = "open"
STATUS_PENDING = "pending_confirmation"  # an uncertain conclusion awaiting the user

# Matches the fenced ```json ... ``` block we write into the section body.
_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL)


@dataclass
class Thread:
    id: str
    summary: str
    status: str = STATUS_OPEN
    progress: list[str] = field(default_factory=list)
    advances: int = 0
    bears_on: list[str] = field(default_factory=list)  # which work-items/domains it connects to
    bearing: str = ""  # kind of bearing (changes-prioritization, …)
    angle: str = ""
    opened_ts: float = 0.0
    last_ts: float = 0.0
    pending_conclusion: str = ""  # proposed conclusion awaiting user confirmation

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Thread:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def new_thread_id(now: float | None = None) -> str:
    ts = int(now if now is not None else time.time())
    return f"t-{ts}-{uuid.uuid4().hex[:4]}"


# ── markdown round-trip ─────────────────────────────────────────────────────


def parse_threads(section_body: str) -> list[Thread]:
    """Extract threads from the ```json block in a section body. Tolerant of an
    empty/absent block (returns [])."""
    if not section_body:
        return []
    m = _JSON_BLOCK_RE.search(section_body)
    raw = m.group(1).strip() if m else section_body.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: list[Thread] = []
    for d in data:
        if isinstance(d, dict) and d.get("id") and d.get("summary"):
            out.append(Thread.from_dict(d))
    return out


def render_section_body(threads: list[Thread]) -> str:
    """Render the section body (a managed comment + a fenced json block).
    Kept legible so the file stays hand-editable."""
    payload = json.dumps([t.to_dict() for t in threads], indent=2)
    return (
        "<!-- Managed by the DMN: unfinished ideas it is actively working through.\n"
        "     Edit the summaries freely; the DMN reconciles on its next tick. -->\n"
        f"```json\n{payload}\n```"
    )


def extract_section(file_text: str, section: str = SECTION) -> str:
    """Return the body of `## <section>` from a markdown file (empty if absent)."""
    pattern = re.compile(
        r"(?m)^##[ \t]+" + re.escape(section) + r"[ \t]*\r?\n(.*?)(?=^##[ \t]|\Z)",
        re.DOTALL,
    )
    m = pattern.search(file_text)
    return m.group(1).strip() if m else ""


# ── ledger operations (pure over a thread list) ─────────────────────────────


def open_thread(
    threads: list[Thread],
    summary: str,
    *,
    angle: str = "",
    bears_on: list[str] | None = None,
    bearing: str = "",
    now: float | None = None,
) -> tuple[list[Thread], Thread]:
    """Append a new open thread, evicting the oldest-least-advanced if at cap."""
    now = now if now is not None else time.time()
    t = Thread(
        id=new_thread_id(now),
        summary=summary.strip(),
        status=STATUS_OPEN,
        bears_on=[b for b in (bears_on or []) if b][:4],
        bearing=bearing.strip(),
        angle=angle.strip(),
        opened_ts=now,
        last_ts=now,
    )
    threads = list(threads)
    threads.append(t)
    # Evict the oldest-least-advanced open threads (never a pending one, never the new one).
    for victim in cap_evict(
        threads,
        MAX_OPEN_THREADS,
        staleness=lambda x: (x.advances, x.opened_ts),
        evictable=lambda x: x.status == STATUS_OPEN and x.id != t.id,
    ):
        threads = [x for x in threads if x.id != victim.id]
    return threads, t


def find(threads: list[Thread], thread_id: str) -> Thread | None:
    for t in threads:
        if t.id == thread_id:
            return t
    return None


def advance_thread(
    threads: list[Thread], thread_id: str, note: str, *, now: float | None = None
) -> tuple[list[Thread], Thread | None]:
    now = now if now is not None else time.time()
    t = find(threads, thread_id)
    if t is None:
        return threads, None
    if note.strip():
        t.progress.append(note.strip())
    t.advances += 1
    t.last_ts = now
    return threads, t


def remove_thread(threads: list[Thread], thread_id: str) -> list[Thread]:
    return [t for t in threads if t.id != thread_id]


def mark_pending(
    threads: list[Thread], thread_id: str, *, now: float | None = None
) -> tuple[list[Thread], Thread | None]:
    now = now if now is not None else time.time()
    t = find(threads, thread_id)
    if t is None:
        return threads, None
    t.status = STATUS_PENDING
    t.last_ts = now
    return threads, t


def reap_aged(
    threads: list[Thread], *, now: float | None = None, max_age_s: float = THREAD_MAX_AGE_S
) -> tuple[list[Thread], list[Thread]]:
    """Enforced wall-clock age-out. Returns (kept, retired). A thread open past
    max_age_s retires regardless of advance/tick count — the safety net for when
    the DMN is suppressed under load and the advance cap never fires."""
    now = now if now is not None else time.time()
    kept: list[Thread] = []
    retired: list[Thread] = []
    for t in threads:
        opened = t.opened_ts or t.last_ts or now
        if aged_out(opened, now, max_age_s):
            retired.append(t)
        else:
            kept.append(t)
    return kept, retired


def should_retire_for_advances(t: Thread) -> bool:
    return t.advances >= THREAD_MAX_ADVANCES
