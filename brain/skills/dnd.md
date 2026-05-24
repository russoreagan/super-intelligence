---
name: dnd
description: D&D 5e toolkit for players and DMs. Roll dice, look up spells and monsters, generate characters, create encounters, and spawn NPCs using the official SRD API. Use when working with D&D 5e, SRD, dice rolls, spell lookup, monster stat blocks, character generation, or encounter building.
---

# D&D 5e Toolkit

Assistant for Dungeons & Dragons 5th Edition: spell lookup, monster stats, dice rolling, character generation, encounters, and NPCs. Uses the [D&D 5e API](https://www.dnd5eapi.co/) (SRD).

## Features

- **Dice** – Roll XdY+Z (e.g. `2d6+3`, `1d20`, `3d8-2`)
- **Spells** – Search or lookup by index (lowercase, hyphens)
- **Monsters** – Full stat blocks; search or direct lookup
- **Character** – Random character with rolled stats (4d6 drop lowest)
- **Encounter** – Balanced encounters by CR
- **NPC** – Random NPC with name, race, occupation, trait

## Usage

Use the `scripts/dnd.py` script from the skill directory or project root.

### Roll dice

```bash
python3 skills/dnd/scripts/dnd.py roll 2d6+3
python3 skills/dnd/scripts/dnd.py roll 1d20
python3 skills/dnd/scripts/dnd.py roll 8d6
```

### Spells

```bash
python3 skills/dnd/scripts/dnd.py spell --search fireball
python3 skills/dnd/scripts/dnd.py spell fire-ball
python3 skills/dnd/scripts/dnd.py spell --list
```

### Monsters

```bash
python3 skills/dnd/scripts/dnd.py monster --search dragon
python3 skills/dnd/scripts/dnd.py monster ancient-red-dragon
python3 skills/dnd/scripts/dnd.py monster --list
```

### Character, encounter, NPC

```bash
python3 skills/dnd/scripts/dnd.py character
python3 skills/dnd/scripts/dnd.py encounter --cr 5
python3 skills/dnd/scripts/dnd.py npc
```

### JSON output

Add `--json` to any command for structured output.

## Tips

- **Spell/monster indices**: lowercase, hyphens (e.g. `magic-missile`, `cure-wounds`, `ancient-red-dragon`).
- **Search** when unsure: `--search fireball` or `--search dragon`.
- **Dice**: `1d20`, `2d6+5`, `3d8-2`, etc.

## API

All data comes from [dnd5eapi.co](https://www.dnd5eapi.co/); no API key required.
