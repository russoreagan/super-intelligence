"""
SkillLoader — reads brain-local skill files and caches them in memory.

Skills live in brain/skills/<name>.md and are injected into system prompts
for local (Ollama) model calls only. Cloud calls ignore skills entirely.

To add a skill: drop a .md file in brain/skills/.
To clone from Claude Code: run `python brain/skill_loader.py clone <name>`.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent / "skills"


class SkillLoader:
    _cache: dict[str, str] = {}

    @classmethod
    def load(cls, name: str) -> str:
        """Return skill content by name, or empty string if not found."""
        if name in cls._cache:
            return cls._cache[name]

        path = SKILLS_DIR / f"{name}.md"
        if not path.exists():
            logger.warning("Skill '%s' not found at %s", name, path)
            return ""

        content = path.read_text(encoding="utf-8").strip()
        cls._cache[name] = content
        return content

    @classmethod
    def load_many(cls, names: list[str]) -> str:
        """Load and concatenate multiple skills, separated by dividers."""
        parts = []
        for name in names:
            content = cls.load(name)
            if content:
                parts.append(f"--- SKILL: {name} ---\n{content}")
        return "\n\n".join(parts)

    @classmethod
    def available(cls) -> list[str]:
        """List all skills available in brain/skills/."""
        return sorted(p.stem for p in SKILLS_DIR.glob("*.md"))


def _clone_skill(skill_name: str) -> None:
    """Copy a skill from ~/.claude/skills/ into brain/skills/ as a starting point."""
    import shutil

    claude_skills_root = Path.home() / ".claude" / "skills"
    dest = SKILLS_DIR / f"{skill_name}.md"

    # Support both exact match and prefix search (e.g. "debugging" matches "quality-debugging")
    candidates = list(claude_skills_root.glob(f"{skill_name}/SKILL.md"))
    if not candidates:
        candidates = list(claude_skills_root.glob(f"*{skill_name}*/SKILL.md"))

    if not candidates:
        print(f"No Claude skill found matching '{skill_name}' in {claude_skills_root}")
        return

    if len(candidates) > 1:
        print(f"Multiple matches — using first: {candidates[0].parent.name}")

    source = candidates[0]
    SKILLS_DIR.mkdir(exist_ok=True)
    shutil.copy(source, dest)
    print(f"Cloned {source.parent.name} → {dest}")
    print(f"Edit {dest} to adapt it for local models (trim length, remove Claude-specific instructions).")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "clone":
        for skill in sys.argv[2:]:
            _clone_skill(skill)
    elif len(sys.argv) == 2 and sys.argv[1] == "list":
        available = SkillLoader.available()
        if available:
            print("\n".join(available))
        else:
            print(f"No skills yet. Add .md files to {SKILLS_DIR}")
    else:
        print("Usage:")
        print("  python brain/skill_loader.py clone <skill-name> [<skill-name> ...]")
        print("  python brain/skill_loader.py list")
