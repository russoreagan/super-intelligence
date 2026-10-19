"""Who may see a persona-scoped skill.

In an isolated org a self-written skill belongs to the persona that wrote it
(skills.persona, migration 049): it is selected, injected and listed only while
that persona is bound. Org-level skills (persona None — partner skills, and every
skill in a consolidated org) are visible to everyone, exactly as before.

One predicate, used by the selector's index (brain/clusters/skill_selector._Index)
and the partner-body registry (brain/skill_loader.SkillLoader), so every read path
— selection, keyword match, pins, fragment attachment, rumination, the capability
manifest, body injection on local and cloud routes — inherits the same rule.
"""

from __future__ import annotations


def bound_persona() -> str:
    from brain.persona_key import active_or_home_persona, persona_slug

    try:
        return persona_slug(active_or_home_persona() or "")
    except Exception:
        return ""


def visible(owner: str | None) -> bool:
    """True when a skill owned by ``owner`` may be seen by the bound persona. A
    persona-less skill is org-level and always visible; a scoped one only to its
    owner (fail closed: with no persona resolvable, a scoped skill is hidden)."""
    if not owner:
        return True
    return bound_persona() == owner
