---
name: dnd-simple-combat
description: D&D 5e training-arena combat: equipment, bestiary, initiative, turn-based attack/damage, victory conditions. Use when implementing or running simple D&D combat, turn-based state, or multi-step combat workflows.
disable-model-invocation: true
---

# D&D Simple Combat

Orchestrate D&D 5e training-arena combat: equip characters, select monsters, roll initiative, resolve attacks and damage, track HP, and handle victory/defeat/flee/surrender.

## When to Use

- User wants a character to fight a monster in a training arena
- Implementing turn-based combat with attack rolls and damage
- Managing equipment, bestiary, and combat state across turns

## Workflow (5 Steps)

1. **Bestiary / Monsters** – The app has a monster database (DnDMonster). Use **`instantiate_encounter`** (or the app’s encounter tool) to create enemies for the encounter; do not run local scripts to seed or select monsters.
2. **Check/Equip Character** – Use **`query_character`** to ensure the character is combat-ready (HP, AC, equipment). If the app supports it, use the character gameplay-update API to auto-equip starting gear when missing.
3. **Start Combat** – Call **`update_game_state`** with `inCombat: true`, add a **combatants** array (enemy HP, AC, etc.), then call for initiative. Use **`request_player_roll`** for player initiative; NPC initiative is rolled by the system or by the dice tool. Set **initiativeOrder** and **currentCombatant**.
4. **Combat Loop** – Turn-based: on player turn, prompt action (attack / flee / surrender); use **`request_player_roll`** for attack and damage rolls; use **`execute_npc_attack`** (or the app’s combat tools) only when it’s an NPC’s turn—the app may auto-resolve NPC turns. Update combatants’ HP via **`update_game_state`** after each hit.
5. **End Combat** – When victory/defeat/flee/surrender: **`update_game_state`** with `inCombat: false`, clear **expectingRollFrom**; apply outcome (e.g. heal character via character API if the app supports it).

## Key Concepts

### Multi-Step Workflows
Combat is a multi-phase process. Use the app’s **tools in order**: instantiate_encounter → update_game_state (start combat) → request_player_roll / combat tools (loop) → update_game_state (end combat). Do not call scripts; use the tools and APIs.

### Turn-Based State
- **Persistent**: Character HP (update via character/gameplay-update API after each turn)
- **Transient**: Monster HP, turn order (in **gameStateData**: combatants, initiativeOrder, currentCombatant)
- **Calculated**: AC, attack bonus (from `query_character` and monster data each time)

### Decision Tree
- Character turn: Attack → Hit? → Damage → Monster dead? | Flee | Surrender
- Monster turn: Attack → Hit? → Damage → Character dead?
- End conditions: Monster HP ≤ 0 (victory), Character HP ≤ 0 (defeat), fled, surrendered

### Tool Outputs
Combat tools return structured data (e.g. attack result with roll, bonus, hit, damage). Use that output to narrate and to update state; do not run scripts.

## Error Recovery

- Character has no equipment → use character API to auto-equip starting gear if the app supports it, or prompt the user to equip.
- No monsters for encounter → use **`instantiate_encounter`** with valid parameters (the app’s monster DB is pre-seeded); if the tool fails, suggest checking encounter parameters or session.
- Character doesn’t exist → suggest creating a character via the app’s character create flow or listing characters via the app.

## Attack Resolution (5e)

- d20 + attack_bonus vs target AC
- Natural 20: automatic hit, critical (roll damage dice twice)
- Natural 1: automatic miss
- Damage: roll damage_dice (+ modifier once); on crit add dice again

## Delivering Combat to User

Present combat in narrative form with transparent dice rolls (e.g. “Attack roll: 15 + 4 = 19 vs AC 15 – HIT! Damage: 7 slashing”). After each turn, ask what the player does (attack, flee, surrender) and continue the loop until an end condition.
