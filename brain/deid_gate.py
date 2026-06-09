"""
De-identification gate — the privacy linchpin of cross-user learning.

Scope (see reports/per_client_chemistry_design.md): this gate sits ONLY on the
episodic → shared-across-users pathway. It never touches a customer's own silo;
the companion's full specific memory of its one user is untouched. The gate
decides whether an insight drawn from one customer's conversation may be written
into state SHARED across all of a persona's customers.

Recurrence is NOT the privacy gate (a novel single case is often the richest
signal — the expectation-violating anomaly). The hard invariant is
DE-IDENTIFICATION: does the artifact let you reconstruct an individual? An insight
crosses only if it passes three stages, ALL fail-closed (any error/uncertainty →
reject, biasing toward false-reject over leak):

  1. extract       — distil the transferable structural principle; drop entities,
                     quotes, rare specifics. No principle → reject.
  2. reid_check     — adversarial: "could this re-identify the source individual?"
                     Re-identifiable → reject.
  3. generality     — k-anonymity moved from ORIGIN to FORM: the principle must
                     plausibly fit MANY people even though ONE triggered it. A
                     single-origin general-form principle is admitted ("grief at a
                     normally-happy topic"); a single-origin specific-form one is a
                     disguised fact ("cried about his retriever Max") → reject.

Corroboration is a CONFIDENCE dial, not an admission gate: an admitted single-case
principle enters shared state as a provisional, low-confidence hypothesis and earns
confidence as distinct customers corroborate it. That confidence/promotion tracking
and the case→hypothesis pointer (for provable deletion) live in the shared-pattern
store, which pairs with this gate; this module is the filter that decides admission.

The gate's test suite IS the privacy proof — see tests/test_deid_gate.py.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_EXTRACT_SYS = (
    "You distil a single TRANSFERABLE PRINCIPLE from one anecdote, for a persona that "
    "talks with many people and wants to learn general lessons WITHOUT remembering any "
    "individual. Strip all specifics: names, identifiers, quotes, rare/unique details, "
    "places, dates. Keep only the structural pattern that could apply to many people. "
    'Reply with JSON: {"transferable": true, "principle": "<one general sentence, no '
    'specifics>"} or {"transferable": false} if there is no generalizable lesson.'
)

_REID_SYS = (
    "You are an adversarial privacy reviewer. Given a candidate general PRINCIPLE and the "
    "SOURCE anecdote it came from, decide whether the principle — if stored and later acted "
    "on — could be used to re-identify the source individual, or still carries a specific, "
    "rare, or quasi-identifying detail. Be strict; when unsure, treat it as re-identifiable. "
    'Reply with JSON: {"reidentifiable": true|false, "reason": "<short>"}.'
)

_GENERALITY_SYS = (
    "You judge whether a candidate PRINCIPLE is a genuine GENERALIZATION or a DISGUISED FACT "
    "about one person. Test: could this principle plausibly describe MANY different people, "
    "even though one case prompted it? General form (many plausible referents) passes; "
    "specific form (effectively fingerprints one person/situation) fails. Be strict; when "
    'unsure, fail it. Reply with JSON: {"general": true|false, "reason": "<short>"}.'
)


@dataclass
class GateResult:
    """Outcome of running an episodic insight through the gate. ``admitted`` is the
    only thing callers should branch on for the privacy decision; ``principle`` is
    the de-identified text safe to write to shared state when admitted."""

    admitted: bool
    principle: str | None
    reason: str
    stage: str  # "extract" | "reid" | "generality" | "admitted"


def _parse_json(raw: str) -> dict | None:
    """Best-effort JSON parse tolerant of code fences / surrounding prose. Returns
    None on failure (callers fail closed)."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        # drop an optional leading "json" language tag
        nl = s.find("\n")
        if nl != -1 and s[:nl].strip().lower() in ("json", ""):
            s = s[nl + 1 :]
    # isolate the outermost object if there's leading/trailing prose
    lo, hi = s.find("{"), s.rfind("}")
    if lo != -1 and hi != -1 and hi > lo:
        s = s[lo : hi + 1]
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


class DeidGate:
    """Three-stage, fail-closed de-identification filter for the episodic→shared
    pathway. Stateless; inject the LLM ``router`` (same interface as
    ``ModelRouter.call``)."""

    def __init__(self, router, *, model_key: str = "claude") -> None:
        self._router = router
        self._model_key = model_key

    async def _ask(self, system: str, user: str, cell: str) -> dict | None:
        try:
            raw = await self._router.call(
                self._model_key,
                system,
                [{"role": "user", "content": user}],
                cluster="deid",
                cell=cell,
            )
        except Exception as exc:  # fail closed on any router error
            logger.warning("[deid] %s stage router error: %s", cell, exc)
            return None
        return _parse_json(raw or "")

    async def filter(
        self, episodic_text: str, source_id: str = "", source_context: str | None = None
    ) -> GateResult:
        """Run an episodic insight through all three stages. Only an insight that
        passes extract → reid → generality is admitted; every other path rejects,
        and every ambiguous/error path rejects (fail-closed).

        ``source_context`` is the material the re-id check compares against; it
        defaults to ``episodic_text``. The two-tier path passes the customer's full
        private material here while ``episodic_text`` is the already-reasoned
        candidate conclusion — so extract abstracts a finished thought while reid
        still guards against anything identifying in the underlying source."""
        text = (episodic_text or "").strip()
        if not text:
            return GateResult(False, None, "empty input", "extract")
        source = (source_context if source_context is not None else episodic_text or "").strip()

        # ── Stage 1: extract a transferable principle ─────────────────────────
        ex = await self._ask(_EXTRACT_SYS, text, "deid_extract")
        if not ex or not ex.get("transferable"):
            return GateResult(False, None, "no transferable principle", "extract")
        principle = str(ex.get("principle") or "").strip()
        if not principle:
            return GateResult(False, None, "empty principle", "extract")

        # ── Stage 2: adversarial re-identification check ──────────────────────
        reid = await self._ask(
            _REID_SYS, f"PRINCIPLE: {principle}\n\nSOURCE: {source}", "deid_reid"
        )
        # fail-closed: missing/unparseable verdict, or explicitly re-identifiable
        if reid is None or reid.get("reidentifiable", True):
            reason = (reid or {}).get("reason", "re-identifiable or unverified")
            return GateResult(False, None, str(reason), "reid")

        # ── Stage 3: generality (k-plausible-referents, not k-sources) ────────
        gen = await self._ask(_GENERALITY_SYS, f"PRINCIPLE: {principle}", "deid_generality")
        if gen is None or not gen.get("general", False):
            reason = (gen or {}).get("reason", "too specific / disguised fact")
            return GateResult(False, None, str(reason), "generality")

        return GateResult(True, principle, "passed all stages", "admitted")
