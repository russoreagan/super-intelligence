---
name: enact-dice-roll-rust
description: Roll dice with configurable sides and count; returns individual rolls and total. Use when the user or system needs dice rolls (d4, d6, d8, d10, d12, d20, d100 or custom sides) with a specified number of dice, or when integrating with Enact/MCP dice tools.
disable-model-invocation: true
---

# Dice Roll (configurable sides and count)

Roll any number of dice with configurable sides. Supports common types (d4, d6, d8, d10, d12, d20, d100) and custom sides.

## When to Use

- User or game mechanic needs dice rolled with specific sides and count
- D&D or TTRPG rolls: d20, 2d6, 4d6 (e.g. ability scores), nd100
- Any “roll N dice with S sides” request

## Input

- **sides**: Number of sides per die (default 6). Minimum 2, maximum 100. Use 4, 6, 8, 10, 12, 20, 100 for standard polyhedrals.
- **count**: Number of dice to roll (default 1). Minimum 1, maximum 100.

## Output

Return or expect JSON with:
- **rolls**: Array of individual die results
- **total**: Sum of all rolls
- **sides**: Sides per die used
- **count**: Number of dice rolled

## Integration

**If Enact/MCP is available:** Call the Enact dice tool (e.g. `enact__dice-roll-rust`) with `sides` and `count`; use the returned `rolls` and `total` in narration or mechanics.

**Otherwise:** Use the project’s existing dice roller (e.g. `skills/dnd-dice-roller/scripts/roll_dice.py`) with equivalent notation: `count`d`sides` (e.g. 2d6, 1d20, 4d6). Map the script output to the same structure (rolls array + total) when presenting results.

## Examples

- Single d20: `sides: 20`, `count: 1`
- Two d6: `sides: 6`, `count: 2`
- 4d6 (e.g. ability score): `sides: 6`, `count: 4`
- Percentile: `sides: 100`, `count: 1` (or two d10s if your system uses that)
