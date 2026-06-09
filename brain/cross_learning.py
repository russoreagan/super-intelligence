"""
Cross-learning pipeline — ties the private reflective tier to the shared store.

This is the seam the engine layer calls at a customer's consolidation (or on an
anomaly-gated turn): reflect privately over that customer's material, gate the
result, and — only if admitted — fold the de-identified principle into the shared
hypothesis store, returning the hypothesis id so the customer's silo can record the
case→hypothesis pointer for later deletion cascade.

The whole privacy chain in one place:
  private material → PrivateRuminator (reason WITH specifics, ephemeral)
                   → DeidGate (extract → reid → generality, fail-closed)
                   → HypothesisStore (provisional → established on distinct corroboration)

Nothing identifying crosses: only a gate-admitted, de-identified principle reaches
the shared store, and it's recorded against an opaque token, never a plaintext id.
"""

from __future__ import annotations

from dataclasses import dataclass

from brain.hypothesis_store import HypothesisStore
from brain.private_rumination import PrivateRuminator


@dataclass
class LearnOutcome:
    admitted: bool
    stage: str  # where it ended: reflect | extract | reid | generality | admitted
    hypothesis_id: str | None = None
    status: str | None = None  # provisional | established (when admitted)
    principle: str | None = None


async def learn_from_private(
    ruminator: PrivateRuminator,
    store: HypothesisStore,
    private_context: str,
    source_id: str,
) -> LearnOutcome:
    """Run one customer's private material through the full chain. Returns a
    LearnOutcome; on admission the caller records ``hypothesis_id`` silo-side as the
    case→hypothesis pointer. The ruminator is single-use — discard it after."""
    result = await ruminator.ruminate(private_context, source_id)
    if not result.admitted or not result.principle:
        return LearnOutcome(admitted=False, stage=result.stage)

    hyp = store.add(result.principle, source_id)
    return LearnOutcome(
        admitted=True,
        stage="admitted",
        hypothesis_id=hyp.id,
        status=hyp.status(store.promote_k),
        principle=result.principle,
    )
