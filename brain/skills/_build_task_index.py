"""
Build _task_skills_index.json — the embedding index for the GENERAL-PURPOSE skill
population that the motor cortex draws on.

The self-reflection skills are indexed by `_import_humanity.py` into
`_humanity_index.json`. This script indexes the REMAINING `brain/skills/*.md`
(every skill not already in that index — the general-purpose, no-frontmatter ones)
so `TaskSkillSelector` can cosine-rank them against a task goal.

Incremental: only embeds skills not already present in `_task_skills_index.json`,
so re-running after dropping new skill files is cheap. Requires a local embedder
(`ModelRouter.embed`, i.e. Ollama nomic-embed-text running).

Run:  python -m brain.skills._build_task_index
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.model_router import ModelRouter  # noqa: E402

SKILLS_DIR = ROOT / "brain" / "skills"
HUMANITY_INDEX = SKILLS_DIR / "_humanity_index.json"
TASK_INDEX = SKILLS_DIR / "_task_skills_index.json"


def _derive_description(text: str) -> str:
    """No-frontmatter general-purpose skills: derive a short description from the
    first heading and/or first paragraph of the markdown body."""
    title = ""
    body_lines: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            if body_lines:
                break
            continue
        if s.startswith("#"):
            if not title:
                title = s.lstrip("#").strip()
            continue
        body_lines.append(s)
        if len(" ".join(body_lines)) > 200:
            break
    desc = (title + " — " if title else "") + " ".join(body_lines)
    desc = re.sub(r"\s+", " ", desc).strip()
    return desc[:300]


def _category_of(name: str) -> str:
    return name.split("-", 1)[0] if "-" in name else name


async def main() -> None:
    if not SKILLS_DIR.exists():
        print(f"[!] {SKILLS_DIR} not found.")
        return

    humanity_names: set[str] = set()
    if HUMANITY_INDEX.exists():
        humanity_names = {
            s["name"] for s in json.loads(HUMANITY_INDEX.read_text())["skills"] if s.get("name")
        }

    existing: list[dict] = []
    if TASK_INDEX.exists():
        existing = json.loads(TASK_INDEX.read_text()).get("skills", [])
    indexed = {s["name"] for s in existing if s.get("name")}

    # General-purpose population = all .md not in the self-reflection index.
    candidates = sorted(p for p in SKILLS_DIR.glob("*.md") if p.stem not in humanity_names)
    todo = [p for p in candidates if p.stem not in indexed]
    print(
        f"{len(candidates)} general-purpose skills; {len(indexed)} already indexed; "
        f"{len(todo)} to embed."
    )
    if not todo:
        print("Task index already up to date.")
        return

    router = ModelRouter()
    index = list(existing)
    failed = 0
    for i, path in enumerate(todo, 1):
        name = path.stem
        text = path.read_text(encoding="utf-8")
        description = _derive_description(text)
        vec = await router.embed(f"{name}\n{description}\n{text[:200]}")
        if vec is None:
            failed += 1
            print(f"  [warn] embedding failed for {name} — skipping")
            continue
        index.append(
            {
                "name": name,
                "category": _category_of(name),
                "description": description,
                "embedding": vec,
            }
        )
        if i % 25 == 0:
            print(f"  …{i}/{len(todo)} embedded")

    TASK_INDEX.write_text(json.dumps({"skills": index}, indent=2), encoding="utf-8")
    print(f"Wrote {len(index)} skills to {TASK_INDEX} ({failed} failed).")


if __name__ == "__main__":
    asyncio.run(main())
