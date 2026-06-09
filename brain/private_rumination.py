"""
Private (per-user) rumination — the in-silo reflective tier of cross-learning.

Why this exists (see reports/per_client_chemistry_design.md): a single de-id-gate
call shouldn't have to BOTH reason about an outlier AND strip it to a safe
abstraction every time. That couples two different jobs into one fragile pass.

So reasoning happens first, privately, with the full specifics — then only the
finished conclusion is handed to the de-id gate, whose job shrinks to "abstract a
formed thought," which is far more reliable.

A PrivateRuminator is EPHEMERAL: create one per reflection, call ruminate(), and
discard it. It holds no persistent state. Nothing private survives it — only the
gated abstraction (a GateResult) may leave, and only if the gate admits it.

Biological reading: a clinician reflecting on one specific session in full detail,
then carrying only the generalizable lesson into their broader practice knowledge.
"""

from __future__ import annotations

import logging

from brain.deid_gate import DeidGate, GateResult

logger = logging.getLogger(__name__)

_REFLECT_SYS = (
    "You are a persona privately reflecting on your time with ONE person, alone, before "
    "sleep. You have full access to the private details of your conversations with them. "
    "Think carefully about what was genuinely notable, surprising, or contrary to what you'd "
    "normally expect — the kind of thing worth understanding more deeply. Reason through it "
    "and state ONE candidate insight: a conclusion about people or interaction that this "
    "experience taught you. You may reference the specifics in your reasoning; a later step "
    "will generalize and anonymize it. If nothing rises to a real insight, say so plainly.\n"
    'Reply with JSON: {"insight": true, "conclusion": "<your candidate insight>"} '
    'or {"insight": false}.'
)


class PrivateRuminator:
    """One ephemeral, silo-scoped reflective pass over a single customer's private
    material. Writes nothing; returns only what the de-id gate admits."""

    def __init__(self, router, gate: DeidGate, *, model_key: str = "claude") -> None:
        self._router = router
        self._gate = gate
        self._model_key = model_key

    async def _reflect(self, private_context: str) -> str | None:
        """Deep private reasoning over the customer's material → a candidate
        conclusion (may contain specifics; the gate handles anonymization)."""
        try:
            raw = await self._router.call(
                self._model_key,
                _REFLECT_SYS,
                [{"role": "user", "content": private_context}],
                cluster="private_rumination",
                cell="private_reflect",
            )
        except Exception as exc:
            logger.warning("[private_rumination] reflect router error: %s", exc)
            return None
        from brain.deid_gate import _parse_json

        parsed = _parse_json(raw or "")
        if not parsed or not parsed.get("insight"):
            return None
        conclusion = str(parsed.get("conclusion") or "").strip()
        return conclusion or None

    async def ruminate(self, private_context: str, source_id: str) -> GateResult:
        """Reflect privately, then gate the result. The gate sees the reasoned
        CONCLUSION (to abstract) and, separately, the original private material as
        re-id context (to guard against anything identifying leaking through).

        Returns a GateResult — admitted carries the de-identified principle safe to
        promote to shared state; rejected leaves nothing behind. The caller discards
        this ruminator afterward."""
        text = (private_context or "").strip()
        if not text:
            return GateResult(False, None, "empty private context", "reflect")

        conclusion = await self._reflect(text)
        if not conclusion:
            return GateResult(False, None, "no candidate insight", "reflect")

        # extract abstracts the finished conclusion; reid checks the FULL private
        # material so nothing identifying slips through the abstraction.
        return await self._gate.filter(conclusion, source_id=source_id, source_context=text)
