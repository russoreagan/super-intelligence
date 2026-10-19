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
import threading
from dataclasses import dataclass
from pathlib import Path

from brain.hypothesis_store import PEOPLE_OR_DAYS, HypothesisStore
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
        status=store.status_of(hyp),
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


# ── Isolated orgs: one private lesson store per persona ──────────────────────
# In an isolated org every persona is one customer's own individual, so the
# org-wide store above (shared by every persona) must stay inert there. Each
# persona instead keeps its OWN store in its state root: it learns lessons from
# its own sessions, they reach only its own turns and practice, a clone never
# carries them (personas._CURRENT_SEED_FILES), and persona purge removes them
# with the rest of the state root. One person by construction, so promotion is
# always 'people_or_days' (distinct people can never fire, distinct days can). `isolated_persona_learning: 0` restores
# the earlier behavior (no lessons at all in an isolated org).
PRIVATE_STORE_NAME = "hypotheses_private.json"


def private_learning_on() -> bool:
    try:
        from brain.settings import settings

        return bool(int(settings.get("isolated_persona_learning", 1) or 0))
    except (TypeError, ValueError):
        return True


def private_store_path(persona: str | None = None) -> Path:
    """The persona's private store (isolated orgs). None/'' = the bound persona."""
    from brain.persona_key import active_or_home_persona, persona_slug, persona_state_root

    slug = persona_slug(persona if persona else (active_or_home_persona() or ""))
    return persona_state_root(slug) / PRIVATE_STORE_NAME


def scoped_store_path(persona: str | None = None) -> Path | None:
    """Where THIS persona's lessons live under the org's learning mode: the org-wide
    store in a consolidated org, the persona's private store in an isolated one.
    None when the mode is unread or private learning is switched off — the caller
    learns and injects nothing (fail closed: misfiling a lesson into the shared
    store is the one outcome that must never happen)."""
    try:
        from brain import org_settings

        if not org_settings.is_isolated():
            return _default_store_path()
        if org_settings.is_isolated_known() and private_learning_on():
            return private_store_path(persona)
    except Exception:
        pass
    return None


def private_store_paths() -> list[Path]:
    """Every persona private store on this volume (home root + personas/*), for the
    cascades that must reach all of them (end-user erasure, source merge/forget)."""
    from brain.persona_key import persona_state_root

    base = persona_state_root("")
    dirs = [base]
    sib = base.parent if base.parent.name == "personas" else base / "personas"
    with contextlib.suppress(OSError):
        dirs += [d for d in sib.iterdir() if d.is_dir() and d != base]
    return [d / PRIVATE_STORE_NAME for d in dirs if (d / PRIVATE_STORE_NAME).is_file()]


def is_private_store(path: Path) -> bool:
    return path.name == PRIVATE_STORE_NAME


def org_store_path() -> Path:
    """The org-wide (consolidated) store."""
    return _default_store_path()


def _corroboration_rule() -> tuple[str, int]:
    """(corroboration, promote_days) for this org: the org row's
    lesson_corroboration (migration 048; 'people' when unread — the stricter rule)
    and settings `cross_learning_promote_days`. Never raises."""
    mode = "people"
    with contextlib.suppress(Exception):
        from brain import org_settings

        mode = org_settings.lesson_corroboration()
    days = 3
    with contextlib.suppress(Exception):
        from brain.settings import settings

        days = max(1, int(settings.get("cross_learning_promote_days", 3) or 3))
    return mode, days


def load_store(path: Path | None = None) -> HypothesisStore:
    p = path or _default_store_path()
    salt = os.environ.get("BRAIN_HYPOTHESIS_SALT", "")
    mode, days = _corroboration_rule()
    if is_private_store(p):
        mode = PEOPLE_OR_DAYS  # one person by construction: distinct people can never fire
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return HypothesisStore.from_dict(data, salt=salt, corroboration=mode, promote_days=days)
        except Exception as e:
            logger.warning("[cross-learning] could not read %s: %s — starting fresh", p, e)
    return HypothesisStore(salt=salt, corroboration=mode, promote_days=days)


def save_store(store: HypothesisStore, path: Path | None = None) -> None:
    p = path or _default_store_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(store.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        logger.warning("[cross-learning] could not persist hypothesis store: %s", e)


_edit_lock = threading.Lock()


def source_for(end_user_id: str) -> str:
    """The hypothesis-store source id for a speaker, exactly as the sleep pass keys
    it (brain/sleep.py _cross_learning_pass): "eu:<end_user_id>" for an engine-lane
    customer, the brain's own user for "owner"."""
    eu = str(end_user_id or "").strip()
    if eu == "owner":
        return os.environ.get("BRAIN_USER_ID", "").strip() or "primary"
    return f"eu:{eu}"


def edit_all_stores(fn) -> list:
    """Run ``fn(store)`` on the org-wide store and every persona private store,
    saving each that changed. For the cascades that follow a PERSON wherever their
    lessons are (erasure, merging one person's two ids). Returns fn's results."""
    out = [edit_store(fn)]
    out += [edit_store(fn, p) for p in private_store_paths()]
    return out


def edit_store(fn, path: Path | None = None):
    """Load → ``fn(store)`` → save, serialized against other edits in this process.
    Returns fn's result. The caller holds the consolidation lock (so a sleep pass
    mid-reflection cannot write its older copy over this edit); this lock only
    orders concurrent API edits."""
    with _edit_lock:
        store = load_store(path)
        before = store.to_dict()
        out = fn(store)
        if store.to_dict() != before:
            save_store(store, path)
        return out


def lesson_view(store: HypothesisStore, h) -> dict:
    """One lesson as the review surface shows it: never the contributor tokens."""
    return {
        "id": h.id,
        "principle": h.principle,
        "status": store.status_of(h),
        "support": h.support,
        "days": h.days,
        "personas": sorted(h.personas),
        "first_seen": h.first_seen,
        "last_seen": h.last_seen,
    }


def established_principles(
    n: int = 3, path: Path | None = None, persona: str | None = None
) -> list[str]:
    """The read surface: top-n established (corroborated, de-identified)
    principles, strongest support first. Empty when the store has none — callers
    skip the context block entirely.

    With ``persona``, only lessons that persona learned (plus unattributed legacy
    records, which predate attribution and were shown to everyone): one process's
    store holds every persona's lessons, and a trading analyst's lesson about
    judging predictions has no business in a companion's turn. None = all."""
    store = load_store(path)
    pool = store.established()
    if persona is not None:
        pool = [h for h in pool if not h.personas or h.learned_by(persona)]
    ranked = sorted(pool, key=lambda h: (h.support, h.days, h.last_seen), reverse=True)
    return [h.principle for h in ranked[: max(0, n)]]
