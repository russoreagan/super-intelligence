"""
TaskSkillSelector — picks general-purpose skills for motor-cortex tasks.

Distinct from the self-reflection selector (`skill_selector.py`, backed by
`_humanity_index.json`): this ranks the *general-purpose* skill population
— every `brain/skills/*.md` indexed in `_task_skills_index.json` — against a task
goal, so the motor cortex can inject relevant know-how into its planning and
execution prompts (skills only influence local-model calls, which the motor
planners use).

Selection is hybrid, per the intended design:
  1. Cosine-rank the goal against the index. If the best match clears a
     (deliberately high) threshold, take the top-K directly — no LLM cost.
  2. Otherwise fall back to a lightweight LLM that picks from a menu of skill
     names + descriptions (how a human/Claude would choose when nothing is
     obviously on-topic).

Degrades gracefully to [] when the index file or the local embedder is absent,
so the app runs fine before `_build_task_index` has been run.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

INDEX_PATH = Path(__file__).resolve().parents[1] / "skills" / "_task_skills_index.json"

# "fairly high" obvious-match threshold; below this we ask an LLM to choose.
DEFAULT_THRESHOLD = 0.62

_PICK_SYSTEM = (
    "You help an autonomous coding/ops agent choose reference skills for a task. "
    "Given the task goal and a menu of available skills (name: description), return "
    "ONLY the names of the skills that would genuinely help — comma-separated, most "
    "relevant first — or the single word NONE if none clearly apply. Do not invent names."
)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return num / (na * nb)


class TaskSkillSelector:
    def __init__(self, router, index_path: Path = INDEX_PATH) -> None:
        self._router = router
        self._skills: list[dict] = []
        self._names: set[str] = set()
        self._load(index_path)

    def _load(self, path: Path) -> None:
        try:
            if path.exists():
                self._skills = json.loads(path.read_text(encoding="utf-8")).get("skills", [])
                self._names = {s["name"] for s in self._skills if s.get("name")}
                logger.info(
                    "TaskSkillSelector: loaded %d general-purpose skills", len(self._skills)
                )
            else:
                logger.info(
                    "TaskSkillSelector: %s not found — task-skill injection disabled "
                    "(run `python -m brain.skills._build_task_index`)",
                    path.name,
                )
        except Exception as e:
            logger.warning("TaskSkillSelector: could not load %s: %s", path, e)

    @property
    def available(self) -> bool:
        return bool(self._skills)

    async def select(
        self,
        goal: str,
        *,
        k: int = 2,
        threshold: float = DEFAULT_THRESHOLD,
        llm_fallback: bool = True,
    ) -> list[str]:
        """Return up to `k` skill names relevant to `goal` (possibly empty)."""
        if not self._skills or not goal or not goal.strip():
            return []
        try:
            vec = await self._router.embed(goal)
        except Exception as e:
            logger.debug("TaskSkillSelector: embed failed: %s", e)
            return []
        if not vec:
            return []

        ranked = sorted(
            ((s, _cosine(vec, s.get("embedding") or [])) for s in self._skills),
            key=lambda t: t[1],
            reverse=True,
        )
        if ranked and ranked[0][1] >= threshold:
            return [s["name"] for s, _ in ranked[:k]]
        if not llm_fallback:
            return []
        return await self._llm_pick(goal, [s for s, _ in ranked[:12]], k)

    async def _llm_pick(self, goal: str, candidates: list[dict], k: int) -> list[str]:
        if not candidates:
            return []
        menu = "\n".join(f"- {s['name']}: {(s.get('description') or '')[:120]}" for s in candidates)
        prompt = f"Task goal:\n{goal}\n\nAvailable skills:\n{menu}\n\nWhich skills help?"
        try:
            raw = await self._router.call(
                "haiku",
                _PICK_SYSTEM,
                [{"role": "user", "content": prompt}],
                cluster="motor",
                cell="task_skill_picker",
                turn_id="",
            )
        except Exception as e:
            logger.debug("TaskSkillSelector: llm pick failed: %s", e)
            return []
        picked: list[str] = []
        for tok in (raw or "").replace("\n", ",").split(","):
            name = tok.strip()
            if name in self._names and name not in picked:
                picked.append(name)
        return picked[:k]
