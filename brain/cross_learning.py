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

import contextlib
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
    persona: str = "",
) -> LearnOutcome:
    """Run one customer's private material through the full chain. Returns a
    LearnOutcome; on admission the caller records ``hypothesis_id`` silo-side as the
    case→hypothesis pointer. The ruminator is single-use — discard it after."""
    result = await ruminator.ruminate(private_context, source_id)
    if not result.admitted or not result.principle:
        return LearnOutcome(admitted=False, stage=result.stage)

    principle = result.principle
    with contextlib.suppress(Exception):
        principle = (
            await match_existing(
                getattr(ruminator, "_router", None), principle, store, persona=persona
            )
        ) or principle
    hyp = store.add(principle, source_id, persona=persona)
    return LearnOutcome(
        admitted=True,
        stage="admitted",
        hypothesis_id=hyp.id,
        status=hyp.status(store.promote_k),
        principle=principle,
    )


_MATCH_SYS = (
    "You keep a list of lessons learned from many people. A new lesson has just been "
    "learned. Decide whether it says ESSENTIALLY THE SAME THING as one already on the "
    "list: the same approach or observation, the same direction of effect (helps vs "
    "falls flat), and the same kind of situation. Different wording is fine; a "
    "different claim is not. When in doubt, it is new.\n"
    'Reply with JSON only: {"match": <number of the matching lesson, or 0 if new>}'
)
_MATCH_CANDIDATES = 20


async def match_existing(
    router, principle: str, store: HypothesisStore, *, persona: str = ""
) -> str | None:
    """The existing principle a newly admitted one restates, else None.

    Corroboration only counts when two people's lessons land on the same
    hypothesis, and the store keys on normalized wording, so two customers'
    paraphrases of one lesson never met: nothing was ever corroborated. One cheap
    judgment call against the strongest existing lessons fixes that; adding under
    the existing wording is then an ordinary corroboration. Both sides are already
    de-identified. None on an exact match (add() handles it), an empty store, no
    router, `cross_learning_match: 0`, or any failure (the lesson is added as new).
    With a persona, only that persona's lessons (and unattributed legacy ones) are
    candidates, so one persona's lesson never absorbs another's."""
    from brain.settings import settings

    if router is None or not settings.get("cross_learning_match", 1):
        return None
    if store.find_by_text(principle) is not None:
        return None
    pool = [h for h in store.all() if not persona or not h.personas or persona in h.personas]
    candidates = sorted(pool, key=lambda h: (h.support, h.last_seen), reverse=True)[
        :_MATCH_CANDIDATES
    ]
    if not candidates:
        return None
    listing = "\n".join(f"{i + 1}. {h.principle}" for i, h in enumerate(candidates))
    raw = await router.call(
        "haiku",
        _MATCH_SYS,
        [{"role": "user", "content": f"New lesson: {principle}\n\nList:\n{listing}"}],
        cluster="cross_learning",
        cell="lesson_match",
    )
    from brain.deid_gate import _parse_json

    parsed = _parse_json(raw or "") or {}
    try:
        n = int(parsed.get("match") or 0)
    except (TypeError, ValueError):
        return None
    if 1 <= n <= len(candidates):
        logger.info("[cross-learning] lesson matched existing %s", candidates[n - 1].id)
        return candidates[n - 1].principle
    return None


# ── persistence + read surface ────────────────────────────────────────────────
# The store is in-memory by design (the engine layer owns durability); for the
# single-process deployment these helpers give it a disk home so admitted
# principles survive restarts and the turn pipeline can actually read them —
# without persistence + a reader, the whole reflect→gate→store chain is write-only.


def _default_store_path() -> Path:
    root = Path(
        os.environ.get("SECOND_BRAIN_PATH", str(Path(__file__).parent.parent / "second_brain"))
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
