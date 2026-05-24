---
name: dnd-character-stats
description: D&D 5e character management with rolled or manual ability scores, app persistence (character APIs), modifiers, HP, and proficiency. Use when creating characters (4d6 drop lowest, auto-assign by class), showing/listing/updating characters, or implementing character CRUD and calculated stats.
disable-model-invocation: true
---

# D&D Character Stats

Manage D&D 5e characters: roll ability scores, assign by class, confirm or adjust, persist via the app’s character APIs, and derive modifiers, HP, and proficiency.

## When to Use

- User wants to create a character with rolled or manual stats
- Listing, showing, or updating existing characters
- Implementing or extending character persistence and calculated properties (modifiers, HP, proficiency)

## Workflow: Creating a Character with Rolled Stats

1. **Roll dice** – Get 6 ability scores via 4d6 drop lowest (use the app’s **dice tool** or `request_player_roll` with notation `4d6` and drop-lowest behavior, or a server-side dice service if available). Roll 6 times (or 6 sets) to get six numbers.
2. **Auto-assign** – Sort the 6 results and assign to abilities by class priority (e.g. Fighter: STR, CON, DEX, WIS, CHA, INT).
3. **Display table** – Show proposed assignment as a markdown table (Ability | Score | Modifier).
4. **Confirm loop** – Ask the user to accept or adjust (e.g. "swap DEX and CON"); update the table and repeat until they confirm.
5. **Save** – Call **POST /api/character/create** (or the app’s character create tool) with final scores and other required fields (gameSystem: dnd5e, species, class, etc.). Do not write to local files or SQLite.

Pattern: **generate → display → confirm → adjust → repeat → commit**.

## Creating with Manual Stats

When the user provides exact ability scores (e.g. "Create wizard Elara with STR 8, DEX 14, CON 12, INT 16, WIS 13, CHA 10"), skip rolling and confirmation; assign directly and call the app’s character create API/tool with those scores and required fields.

## App Persistence (This App)

Characters are stored in the app database (Prisma). The character create/update APIs accept (among other fields):

- **Identity:** name, class (dndClassId), level, gameSystem (dnd5e)
- **Abilities:** str, dex, con, int, wis, cha
- **HP:** hpCurrent, hpMax (or equivalent)
- **Species, background, equipment, spells**, etc. as required by the D&D creation flow

Modifiers and proficiency bonus are derived in the app (computed on read or at validation); use the same formulas below when advising or when the app does not return them.

## Calculated Properties

- **Modifier:** floor((score - 10) / 2)
- **HP (level 1):** class hit die maximum + CON modifier (e.g. Fighter d10 → 10 + CON mod)
- **HP (higher levels):** add (hit die average rounded up + CON modifier) per level, or roll each level
- **Proficiency bonus:** 2 at levels 1–4, 3 at 5–8, 4 at 9–12, 5 at 13–16, 6 at 17–20 (i.e. 2 + floor((level - 1) / 4))

## Class Priority (Auto-Assign)

Use when sorting rolled scores into abilities. Example order (highest roll → first ability):

- **Fighter:** STR, CON, DEX, WIS, CHA, INT
- **Wizard:** INT, DEX, CON, WIS, CHA, STR
- **Rogue:** DEX, CON, WIS, INT, CHA, STR
- **Cleric:** WIS, CON, STR, DEX, CHA, INT

Adjust for other classes as needed (e.g. Paladin: STR/CHA, Bard: CHA/DEX).

## Updates

- **Level up:** Recalculate hp_max (add hit die for new level), update proficiency if threshold crossed (5, 9, 13, 17).
- **Ability score change:** Recalculate modifier and any dependents (e.g. spell DC if INT/CHA/WIS).
- **Damage/healing:** Set hp_current (clamp to 0..hp_max).

## Error Handling

- Duplicate name on create → error, suggest different name or update existing.
- Show/list non-existent character → clear error, suggest `list` to see names (case-sensitive).

## Integration with This App

- **Dice:** Use the app’s **dice tool** or **request_player_roll** (or server-side dice service when available) for 4d6 drop lowest; do not run external scripts.
- **Combat:** The app’s combat flow (dnd-simple-combat style) uses character HP, AC, and equipment from `query_character` and the character/combat APIs.
- **Character sheet detail:** Use [dnd-5e-character-manager](skills/dnd-5e-character-manager/) for full PHB-derived calculations and validation; read/write character data via `query_character` and the character APIs.
