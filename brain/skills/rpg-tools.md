---
name: rpg-tools
description: Solo RPG mechanical tools for dice rolling, tarot draws, oracles, name generation, character/location/memory/faction management, and story retrieval. Also provides guided campaign creation and pre-session setup. Use when the user asks to: roll dice, draw tarot cards, consult oracles, generate names, load characters or locations, track memories or factions, pull from story collections, start a new campaign, or do campaign prep/planning.
disable-model-invocation: true
---

# RPG Tools

Mechanical tools for solo RPG sessions. Run scripts from `scripts/` (create or adapt paths to match this project, e.g. `skills/rpg-tools/scripts/` or project `scripts/`).

## Tool Categories

**Instant Tools** – No data files needed:
- `dice.py` – Dice rolling
- `tarot.py` – Tarot draws
- `oracle.py` – Multi-system oracle
- `pool.py` – Pool/deck management (needs `pools/` for definitions)

**Campaign Tools** – Require JSON in specific directories:
- `namegen.py` – Name generation (needs `namesets/`)
- `characters.py` – Character profiles (needs `characters/`)
- `locations.py` – Location profiles (needs `locations/`)
- `stories.py` – Story collections (needs `stories/`)
- `memories.py` – Memory tracking (needs `memories/`)
- `factions.py` – Faction tracking (needs `factions/`)
- `log.py` – Campaign event log (needs `campaign/log.json`)
- `campaign.py` – Campaign management (needs `campaign/config.json`)

## Instant Tools

### Dice
Roll20-style notation: `kh/kl` (keep), `dh/dl` (drop), `r/rr` (reroll), `!`/`!!`/`!p` (exploding), comparison operators.

```bash
python scripts/dice.py "2d6+5"
python scripts/dice.py "4d6kh3"
python scripts/dice.py "8d6!"
python scripts/dice.py "6d10>=7"
```

### Tarot
```bash
python scripts/tarot.py
python scripts/tarot.py 3
```
Max 10 cards. Full 78-card deck.

### Oracle
```bash
python scripts/oracle.py axis   # Tone/direction/element/action/twist
python scripts/oracle.py omni   # Full reading
python scripts/oracle.py tarot [n]
python scripts/oracle.py rune [n]
python scripts/oracle.py iching
python scripts/oracle.py fate [likelihood]   # Yes/no (impossible→certain)
python scripts/oracle.py prompt   # Action + Theme pair
```

### Pool
```bash
python scripts/pool.py list
python scripts/pool.py create NAME
python scripts/pool.py draw NAME [n]
python scripts/pool.py peek NAME [n]
python scripts/pool.py status NAME
python scripts/pool.py shuffle NAME
python scripts/pool.py reset NAME
```

## Campaign Tools (summary)

- **namegen.py**: `list`, `full --nameset NAME [--count N] [--gender ...]`, `groups --nameset NAME`
- **characters.py**: `list`, `get NAME [--depth full] [--section SECTION]`, `sections NAME`, `memories NAME`, `create`/`update`/`delete`
- **locations.py**: `list`, `get NAME [--depth full]`, `sections NAME`, `tree`, `path NAME`, `connections NAME`, `memories NAME`, `create`/`update`/`delete`
- **stories.py**: `meta --campaign NAME`, `list`/`get`/`show`/`random`/`create --campaign NAME`
- **memories.py**: `list`/`get`/`random`/`recent`/`search`/`character`/`location`/`connections`/`chain`/`meta`/`create --campaign NAME`
- **factions.py**: `list`, `get NAME [--depth full]`, `tree`, `members`, `relationships`, `economy`, `resources`, `create`/`update`/`delete`
- **log.py**: `add "Event" --date Y3.D45` (and other add options), `list`, `show ID`, `delete ID`, `digest`
- **campaign.py**: `init`, `show`, `branch list`/`switch`/`create`, `state show`/`set`/`delete`, `changelog show`, `export`/`import`

Filters (e.g. `--campaign`, `--character`, `--tag`) apply per tool; see tool help or references.

## Session Workflow

- **Campaign Zero** – Pre-campaign brainstorming and bundle creation
- **Session Setup** – Calibrate tone, direction, pacing
- **Session Debrief** – Post-session reflection and character growth

Optional modifiers: Mature Content, Combat Realism (see project `modifiers/` if present).

## Creating Campaign Data

Use reference guides for JSON schemas: character-guide, location-guide, faction-guide, memories-guide, nameset-guide, story-capture-guide, oracle-guide, pool-guide, campaign-state-guide. Create or point to these in the project if you implement full rpg-tools.
