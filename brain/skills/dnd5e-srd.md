---
name: dnd5e-srd
description: Retrieval-augmented generation (RAG) skill for the D&D 5e System Reference Document (SRD). Use when answering questions about D&D 5e core rules, spells, combat, equipment, conditions, monsters, and other SRD content. This skill provides agentic search-based access to the SRD split into page-range markdown files.
disable-model-invocation: true
---

# D&D 5e SRD RAG

Search-based retrieval access to the D&D 5e System Reference Document (SRD), organized by page ranges as markdown files in a `references/` directory.

## When to Use

Use whenever answering questions about D&D 5e SRD content:
- Core rules and gameplay procedures
- Ability checks, saving throws, and skill use
- Combat, actions, conditions, and movement
- Classes, backgrounds, equipment, and magic items
- Spells and spellcasting
- Creatures and stat blocks in the SRD

## Search Strategy

1. **Identify relevant page ranges** using the file index (see below).
2. **Search** using a Python search tool if available: e.g. `scripts/search_with_positions.py "term" --all` to get character positions for citations.
3. **Expand context** when needed: e.g. `scripts/expand_context.py "term" --result N --mode section --all`.
4. **Cite sources** with character position format: `[filename, chars START-END]`.

## File Index (by page range)

- `DND5eSRD_001-018.md`: Intro through Character Creation
- `DND5eSRD_019-035.md`: Barbarian, Bard, Cleric (start)
- `DND5eSRD_036-046.md`: Cleric/Druid, Fighter, Monk (start)
- `DND5eSRD_047-063.md`: Monk, Paladin, Ranger, Rogue
- `DND5eSRD_064-076.md`: Sorcerer, Warlock, Wizard (start)
- `DND5eSRD_077-086.md`: Wizard, Origins, Feats
- `DND5eSRD_087-103.md`: Equipment
- `DND5eSRD_104-120.md` through `DND5eSRD_155-175.md`: Spells
- `DND5eSRD_176-191.md`: Rules Glossary (part)
- `DND5eSRD_192-252.md`: Gameplay Toolbox, Magic Items
- `DND5eSRD_253-364.md`: Monsters

If this project has SRD files in a different path (e.g. `skills/dnd5e-srd/references/` or `data/srd/`), use that path. To list files: `ls references/` or `grep -l "term" references/*.md`.

## Python Tools (if present)

- **search_with_positions.py**: Search SRD files, return character ranges and context. Usage: `python scripts/search_with_positions.py "term" --all`; optional `--pages 200-300`, `--max-results N`, `--context N`.
- **expand_context.py**: Expand a search result by paragraph/section. Usage: `python scripts/expand_context.py "term" --result N --mode section --all`; modes: paragraph, section, document.

## Best Practices

- Prefer targeted search over reading whole large files
- Always cite sources with `[filename, chars START-END]`
- For multi-part questions, search each part and synthesize
- Use exact SRD terminology in searches

## Notes

- SRD only; no non-SRD supplements
- Some files are large; use search and expand rather than full-file reads
