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
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent / "skills"


class SkillLoader:
    _cache: dict[str, str] = {}
    # App-provided skill bodies, registered by SkillSelector.warm_partner_skills.
    # Untrusted partner content — kept separate from disk skills so it can be fenced
    # when injected and so model_router can inject it on CLOUD routes too (a partner
    # skill is domain knowledge Claude does NOT have, unlike the humanity frameworks).
    _partner_bodies: dict[str, str] = {}

    @classmethod
    def load(cls, name: str) -> str:
        """Return skill content by name, or empty string if not found."""
        if name in cls._partner_bodies:
            return cls._partner_bodies[name]
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

    # ── app-provided (partner) skills ──────────────────────────────────────────

    @classmethod
    def register_partner(cls, name: str, body: str) -> None:
        """Register an approved partner skill body (called at warm time)."""
        cls._partner_bodies[name] = str(body or "")

    @classmethod
    def clear_partner(cls) -> None:
        """Drop all partner bodies (called before each re-warm)."""
        cls._partner_bodies.clear()

    @classmethod
    def is_partner(cls, name: str) -> bool:
        return name in cls._partner_bodies

    @classmethod
    def load_partner_block(cls, names: list[str]) -> str:
        """Fenced, precedence-framed block for the given partner skills — the form
        injected on both local and cloud routes. "" when none resolve."""
        bodies = [(n, cls._partner_bodies.get(n, "")) for n in names]
        bodies = [(n, b) for n, b in bodies if b.strip()]
        if not bodies:
            return ""
        from brain.persona_context import partner_skill_block
        from brain.security import fence

        nonce = str(uuid.uuid4())[:8]
        return "\n\n".join(partner_skill_block(b, fence, nonce, n) for n, b in bodies)

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
    print(
        f"Edit {dest} to adapt it for local models (trim length, remove Claude-specific instructions)."
    )


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
