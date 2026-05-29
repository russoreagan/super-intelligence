"""
Import skills-for-humanity from .claude/skills/ into brain/skills/.

For each <name>/SKILL.md:
  - parse frontmatter (name, description)
  - strip Claude-Code-specific `## Confirm Direction` block
  - write flat brain/skills/<name>.md
  - tag tier (1 = always-on baseline, 2 = common pool, 3 = specialized)
  - compute embedding via router.embed()
  - record in _humanity_index.json

One-shot but kept in repo so re-runs are easy after `npx` updates.

Run:  python -m brain.skills._import_humanity
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

# Allow running as a script from anywhere
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.model_router import ModelRouter  # noqa: E402

SOURCE_DIR = ROOT / ".claude" / "skills"
DEST_DIR = ROOT / "brain" / "skills"
INDEX_PATH = DEST_DIR / "_humanity_index.json"
TIERS_PATH = DEST_DIR / "_humanity_tiers.json"

# Multi-word categories (skill name prefix includes a hyphen).
# Order matters: longest first so we match "game-theory" before "game".
MULTI_WORD_CATEGORIES = ["game-theory"]

TIER_1_NAMES = [
    "logic-check",
    "communication-clarity-audit",
    "ethics-bias-check",
    "emotional",
]

TIER_3_CATEGORIES = [
    "game-theory",
    "strategy",
    "writing",
    "historical",
    "resource",
    "play",
    "sensory",
    "aesthetic",
]

TIER_3_INDIVIDUAL = [
    "temporal-futures-mapping",
    "temporal-horizon-mapping",
]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML-ish frontmatter. Returns (frontmatter_dict, body)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")

    # Light parse — name and description are single-line or quoted strings.
    fm: dict = {}
    cur_key = None
    buf: list[str] = []
    for line in raw.splitlines():
        m = re.match(r"^(\w[\w-]*)\s*:\s*(.*)$", line)
        if m and not line.startswith(" "):
            if cur_key is not None:
                fm[cur_key] = "\n".join(buf).strip().strip('"')
            cur_key, val = m.group(1), m.group(2).strip()
            buf = [val]
        else:
            buf.append(line)
    if cur_key is not None:
        fm[cur_key] = "\n".join(buf).strip().strip('"')
    return fm, body


def strip_confirm_direction(body: str) -> str:
    """Remove the `## Confirm Direction` section through the next `---` or `## ` heading."""
    pattern = re.compile(
        r"\n##\s+Confirm Direction\b.*?(?=\n##\s|\n---\n|\Z)",
        re.DOTALL | re.IGNORECASE,
    )
    return pattern.sub("", body).rstrip() + "\n"


def category_of(name: str) -> str:
    for prefix in MULTI_WORD_CATEGORIES:
        if name == prefix or name.startswith(prefix + "-"):
            return prefix
    return name.split("-", 1)[0]


def is_router(name: str, body: str) -> bool:
    """A router is either a bare category name or has a Confirm Direction block."""
    return name == category_of(name) or "## Confirm Direction" in body


def tier_of(name: str, category: str) -> int:
    if name in TIER_1_NAMES:
        return 1
    if name in TIER_3_INDIVIDUAL:
        return 3
    if category in TIER_3_CATEGORIES:
        return 3
    return 2


async def main() -> None:
    if not SOURCE_DIR.exists():
        print(
            f"[!] {SOURCE_DIR} does not exist — run `npx @human-avatar/skills-for-humanity --scope project` first."
        )
        return

    DEST_DIR.mkdir(parents=True, exist_ok=True)

    skill_dirs = sorted(p for p in SOURCE_DIR.iterdir() if p.is_dir())
    print(f"Found {len(skill_dirs)} skills in {SOURCE_DIR}")

    router = ModelRouter()

    index: list[dict] = []
    skipped: list[str] = []
    written: list[str] = []

    for sd in skill_dirs:
        skill_md = sd / "SKILL.md"
        if not skill_md.exists():
            skipped.append(sd.name + " (no SKILL.md)")
            continue

        raw = skill_md.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(raw)
        name = fm.get("name", sd.name)
        description = fm.get("description", "").strip()

        body_clean = strip_confirm_direction(body)
        cat = category_of(name)
        router_flag = is_router(name, body)
        tier = tier_of(name, cat)

        # Reassemble file with cleaned body
        out = f'---\nname: {name}\ndescription: "{description}"\ncategory: {cat}\nis_router: {str(router_flag).lower()}\ntier: {tier}\n---\n\n{body_clean.strip()}\n'
        out_path = DEST_DIR / f"{name}.md"

        # Don't clobber an existing brain skill that isn't a humanity import.
        if out_path.exists():
            existing = out_path.read_text(encoding="utf-8")
            if (
                "# imported from skills-for-humanity" not in existing
                and "category:" not in existing[:200]
            ):
                skipped.append(f"{name} (collision with existing brain skill)")
                continue

        out_path.write_text(out, encoding="utf-8")
        written.append(name)

        # Embedding from name + description + first ~200 chars of body
        embed_text = f"{name}\n{description}\n{body_clean[:200]}"
        vec = await router.embed(embed_text)
        if vec is None:
            print(f"  [warn] embedding failed for {name}")
            vec = [0.0] * 768

        index.append(
            {
                "name": name,
                "category": cat,
                "is_router": router_flag,
                "tier": tier,
                "description": description,
                "embedding": vec,
            }
        )

        if len(written) % 25 == 0:
            print(f"  …{len(written)} written")

    INDEX_PATH.write_text(json.dumps({"skills": index}, indent=2), encoding="utf-8")
    TIERS_PATH.write_text(
        json.dumps(
            {
                "tier_1": TIER_1_NAMES,
                "tier_3_categories": TIER_3_CATEGORIES,
                "tier_3_skills": TIER_3_INDIVIDUAL,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    by_tier = {1: 0, 2: 0, 3: 0}
    routers_n = 0
    for s in index:
        by_tier[s["tier"]] += 1
        if s["is_router"]:
            routers_n += 1

    print()
    print(f"Wrote {len(written)} skills to {DEST_DIR}")
    print(f"  Tier 1: {by_tier[1]}   Tier 2: {by_tier[2]}   Tier 3: {by_tier[3]}")
    print(f"  Routers: {routers_n}    Leaves: {len(index) - routers_n}")
    print(f"Index: {INDEX_PATH}")
    print(f"Tiers: {TIERS_PATH}")
    if skipped:
        print(f"Skipped {len(skipped)}:")
        for s in skipped:
            print(f"  - {s}")


if __name__ == "__main__":
    asyncio.run(main())
