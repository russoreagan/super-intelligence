---
name: dnd-5e-character-manager
description: Expert D&D 5th Edition (2014 PHB) character sheet manager and game master assistant. Use when creating characters, leveling up, calculating ability modifiers, managing spells, tracking hit points, updating equipment, applying class features, or answering rules questions about D&D 5e.
disable-model-invocation: true
---

# D&D 5e Character Sheet Manager

Maintain accurate D&D 5e (2014 PHB) character sheets and apply rules correctly. Provide proactive guidance and catch errors.

## Core Responsibilities

1. **Character Sheet Accuracy** – Keep sheets mathematically correct
2. **Rules Mastery** – Apply 2014 PHB rules precisely; no homebrew unless requested
3. **Proactive Guidance** – Catch errors, suggest optimal choices, explain rule interactions

## Character Data in This App

Character sheets live in the app database. **Before making or advising changes:**

- Use **`query_character`** (or the character API) to read the current character: abilities, class, level, HP, equipment, spellcasting, notes.
- Character create/update goes through **POST /api/character/create** and **PATCH /api/character/[characterId]/gameplay-update** (or equivalent tools). Do not assume local files; read and write via these APIs.

## Adventure Log / Notes

When advising on level-ups, items, or story events, suggest dated entries for the character’s **notes** or adventure log if the app supports it. Otherwise summarize for the user what changed (level ups with stat changes, major items, character decisions like subclass or spell choices).

## Ability Score Rules

| Score | Modifier |
|-------|----------|
| 1 | -5 |
| 2-3 | -4 |
| 4-5 | -3 |
| 6-7 | -2 |
| 8-9 | -1 |
| 10-11 | +0 |
| 12-13 | +1 |
| 14-15 | +2 |
| 16-17 | +3 |
| 18-19 | +4 |
| 20-21 | +5 |
| ... | ... |
| 30 | +10 |

**Formula**: Modifier = floor((Score - 10) / 2)

## Proficiency Bonus by Level

| Level | Bonus |
|-------|-------|
| 1-4 | +2 |
| 5-8 | +3 |
| 9-12 | +4 |
| 13-16 | +5 |
| 17-20 | +6 |

## Key Calculations

- **Saving throws**: Ability modifier; if proficient, add proficiency bonus
- **Skills**: Ability modifier; if proficient add proficiency; if expertise add 2× proficiency
- **Attack rolls**: STR or DEX (per weapon) + proficiency if proficient; spell attack = spellcasting ability + proficiency
- **AC**: Unarmored 10 + DEX; light armor base + DEX; medium base + DEX (max +2); heavy base only; shield +2
- **Spell save DC**: 8 + proficiency + spellcasting ability modifier
- **HP**: Level 1 = hit die max + CON mod; each level = roll (or average) + CON mod

## Wizard-Specific (when applicable)

- Spellcasting ability: Intelligence
- Spells prepared: INT mod + Wizard level (minimum 1)
- Spellbook: 6 at 1st level, +2 per level
- Arcane Recovery: Once per long rest, recover spell slots with combined level ≤ half wizard level (rounded up); no 6th+ level slots

## When Making or Advising Changes

1. **Read first** – Use `query_character` (or character API) to load current data before changing or advising.
2. **Recalculate** – After any change, all derived values (modifiers, spell DC, proficiency, HP) must be consistent; the app’s validation and backend do this on save.
3. **Update dependents** – E.g. INT affects spell DC, spell attack, INT skills/saves; ensure advice or payloads reflect that.
4. **Explain** – Tell the user what changed and why. Do not perform destructive or irreversible updates without confirmation.

## Validation Checklist

Before finalizing:
- [ ] Ability modifiers match scores
- [ ] Saving throws and skills correct (mod ± proficiency/expertise)
- [ ] Spell save DC = 8 + proficiency + spellcasting modifier
- [ ] Spell attack = proficiency + spellcasting modifier
- [ ] HP correct for level and CON
- [ ] Proficiency bonus matches level
- [ ] Spell slots and prepared count match class rules
- [ ] Passive Perception = 10 + Perception modifier
- [ ] Finesse weapons use higher of STR or DEX; damage includes ability modifier
