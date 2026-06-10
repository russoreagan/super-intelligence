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

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from brain.hypothesis_store import HypothesisStore
from brain.private_rumination import PrivateRuminator

logger = logging.getLogger(__name__)


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


# ── persistence + read surface ────────────────────────────────────────────────
# The store is in-memory by design (the engine layer owns durability); for the
# single-process deployment these helpers give it a disk home so admitted
# principles survive restarts and the turn pipeline can actually read them —
# without persistence + a reader, the whole reflect→gate→store chain is write-only.


def _default_store_path() -> Path:
    root = Path(
        os.environ.get(
            "SECOND_BRAIN_PATH", str(Path(__file__).parent.parent / "second_brain")
        )
    )
    return root / "hypotheses.json"


def load_store(path: Path | None = None) -> HypothesisStore:
    p = path or _default_store_path()
    salt = os.environ.get("BRAIN_HYPOTHESIS_SALT", "")
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return HypothesisStore.from_dict(data, salt=salt)
        except Exception as e:
            logger.warning("[cross-learning] could not read %s: %s — starting fresh", p, e)
    return HypothesisStore(salt=salt)


def save_store(store: HypothesisStore, path: Path | None = None) -> None:
    p = path or _default_store_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(store.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        logger.warning("[cross-learning] could not persist hypothesis store: %s", e)


def established_principles(n: int = 3, path: Path | None = None) -> list[str]:
    """The read surface: top-n established (k-corroborated, de-identified)
    principles, strongest support first. Empty when the store has none — callers
    skip the context block entirely."""
    store = load_store(path)
    ranked = sorted(store.established(), key=lambda h: (h.support, h.last_seen), reverse=True)
    return [h.principle for h in ranked[: max(0, n)]]
