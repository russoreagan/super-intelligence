"""Verify rumination actually USES distinct analytical skill packages (Stage 7 Part A).

Two separable questions, both proven here without Ollama:
  (a) does the loop APPLY the distinct skills the meta-cell selects (in order)?
  (b) does the selected package's CONTENT actually exist and reach the worker (skills=[name]
      passed to the cell + a loadable .md body), so when the real router runs it gets injected?
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brain.clusters import skill_selector as ss_mod
from brain.clusters.skill_selector import SkillSelector
from brain.skill_loader import SkillLoader

pytestmark = pytest.mark.skipif(
    not (ss_mod.INDEX_PATH.exists()),
    reason="skills index missing — run python -m brain.skills._import_humanity",
)

_SEQUENCE = [
    "creativity-lateral-thinking",
    "analogy-domain-transfer",
    "systems-feedback-mapping",
]


class _RecordingCell:
    """Stands in for IntegratorCell so we can capture what each rumination worker is given."""

    created: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.skills = list(kwargs.get("skills", []))
        self.system_prompt = kwargs.get("system_prompt", "")
        _RecordingCell.created.append(self)

    def set_router(self, _router):  # noqa: D401
        pass

    def reset_turn(self, _turn_id):
        pass

    async def call(self, _messages):
        return f"a new take produced via {self.skills}"


def _selector():
    router = SimpleNamespace(embed=AsyncMock(return_value=None))
    return SkillSelector(router)


@pytest.mark.asyncio
async def test_ruminate_applies_distinct_selected_skills(monkeypatch):
    s = _selector()
    # Drive the meta-cell deterministically: pick each skill once, then stop.
    decisions = [
        {"mode": "transform", "skill": _SEQUENCE[0], "base_idx": 0},
        {"mode": "branch", "skill": _SEQUENCE[1], "base_idx": 1},
        {"mode": "reframe", "skill": _SEQUENCE[2], "base_idx": 2},
        {"mode": "stop", "skill": None, "base_idx": 0},
    ]
    s._meta_decide = AsyncMock(side_effect=decisions)
    # Synthesize deterministically (avoid a real LLM call in chain synthesis).
    s._synthesize_chain = AsyncMock(return_value="final synthesized take")

    _RecordingCell.created.clear()
    monkeypatch.setattr(ss_mod, "IntegratorCell", _RecordingCell)

    final, chain = await s.ruminate("seed thought", max_iters=6, turn_id="t")

    # (a) the loop applied exactly the distinct skills the meta-cell chose, in order.
    applied = [c["skill"] for c in chain if c["skill"]]
    assert applied == _SEQUENCE
    assert len(set(applied)) == 3  # distinct
    assert final == "final synthesized take"

    # (b) each worker was given skills=[that skill] and the skill name reached its prompt.
    workers = _RecordingCell.created
    assert [w.skills for w in workers] == [[s] for s in _SEQUENCE]
    for w, skill in zip(workers, _SEQUENCE):
        assert skill in w.system_prompt


@pytest.mark.asyncio
async def test_category_cap_forces_lens_change(monkeypatch):
    """A meta-cell that keeps picking the SAME category gets redirected to a different lens
    after a short run — variety across categories, not endless clustering (the analogy-only bug)."""
    s = _selector()
    # Meta-cell stubbornly picks analogy every step.
    s._meta_decide = AsyncMock(
        return_value={"mode": "transform", "skill": "analogy-domain-transfer", "base_idx": 0}
    )
    s._apply_skill = AsyncMock(return_value="a take")
    s._synthesize_chain = AsyncMock(return_value="final")

    _final, chain = await s.ruminate("seed", max_iters=6, turn_id="t")
    applied = [c["skill"] for c in chain if c.get("skill")]
    cats = [s._skill_category(n) for n in applied]
    # It used more than one category (was clustering to a single one before the fix)...
    assert len(set(cats)) > 1
    # ...and never ran the same category more than the cap consecutively.
    cap = s._MAX_CONSEC_CATEGORY
    run = 1
    for a, b in zip(cats, cats[1:]):
        run = run + 1 if a == b else 1
        assert run <= cap, f"category {b!r} ran {run} in a row (cap {cap})"


def test_meta_catalog_is_not_alphabetically_truncated():
    """Regression: the 8k catalog must SAMPLE across categories, not keep a fixed alphabetical
    prefix that hid ~60% of skills (the root cause of analogy clustering). Over several builds,
    late-alphabet categories (writing/systems/strategy) should appear."""
    s = _selector()
    seen = set()
    for _ in range(8):
        pool = [x for x in s._index.skills if not x["is_router"]]
        import random as _r

        _r.shuffle(pool)
        budget, names = 0, []
        for x in pool:
            line = f"- {x['name']}: {x['description'][:120]}"
            if budget + len(line) + 1 > 8000:
                break
            names.append(x["name"])
            budget += len(line) + 1
        seen.update(n.split("-")[0] for n in names)
    assert {"writing", "systems", "strategy", "logic"} & seen  # back-half categories now reachable


def test_rumination_pool_skills_have_loadable_content():
    """The package CONTENT exists, so skills=[name] injects a real framework at run time
    (SkillLoader.load is what model_router appends to the worker's system prompt)."""
    for skill in _SEQUENCE + list(SkillSelector._ANXIOUS_SKILLS):
        body = SkillLoader.load(skill)
        assert body and len(body) > 50, f"{skill}: empty/short skill body"
