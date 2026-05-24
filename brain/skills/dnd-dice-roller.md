---
name: dnd-dice-roller
description: Roll dice using D&D notation (d20, 2d6+3, advantage, disadvantage, drop lowest). Use when the user asks to roll dice, when playing D&D or similar TTRPGs, or when handling dice notation in the AI GM or game systems.
disable-model-invocation: true
---

# D&D Dice Roller

Roll dice from natural language or notation. Parse user input, use the app’s dice tool or roll API, and present results clearly. Do not run external scripts; use the app’s tools.

## Dice notation supported

- **Single die**: `d20`, `d6`, `d100`
- **Multiple dice**: `2d6`, `3d8`, `4d6`
- **Modifiers**: `d20+5`, `2d6-2`, `1d8+3`
- **Advantage**: `d20 adv` / `d20 advantage` (roll twice, take higher)
- **Disadvantage**: `d20 dis` / `d20 disadvantage` (roll twice, take lower)
- **Drop lowest**: `4d6 drop lowest` (e.g. for ability scores)

## How to roll

1. **Parse** the user’s request into dice notation and any flags (advantage, disadvantage, drop lowest).
2. **Use the app’s dice tool or roll API** – For the AI GM: when a **player** must roll, use **`request_player_roll`** with the appropriate `roll_type`, `damage_dice` (if damage), and description; the app’s UI will show the dice roller and send the result back. When the **system** or **NPC** needs to roll (e.g. NPC attack, 4d6 for ability scores), use the app’s server-side dice service or roll API if available; do not run `roll_dice.py` or other scripts.
3. **Show** the result to the user (individual rolls, modifier, total). If the app returns structured roll data, use it to narrate (e.g. "Rolling 2d6+3... [4, 5] +3 = 12").

## Notation to pass to the app

- `d20` — single die  
- `2d6+3` — multiple dice with modifier  
- `d20` with advantage — advantage (roll twice, take higher)  
- `d20` with disadvantage — disadvantage (roll twice, take lower)  
- `4d6` drop lowest — e.g. for ability scores  

Default to 1 die when count is omitted (`d20` → `1d20`). Accept variants like "roll d20", "1d20", "d20+5".

## Output format

When presenting roll results, show individual rolls, then modifier (if any), then total. Examples:

- `Rolling 1d20... [15] = 15`
- `Rolling 2d6+3... [4, 5] +3 = 12`
- `Rolling d20 with advantage... [15] [8] (advantage) = 15`
- `Rolling 4d6, dropping lowest... [4, 3, 6, 2] → Dropped [2] [4, 3, 6] = 13`

Use the data returned by the app’s dice tool or roll API to format output.

## Validation and errors

- Die size must be positive. Common: d4, d6, d8, d10, d12, d20, d100.
- Invalid (e.g. `d0`, `d-5`): explain politely and suggest valid notation.
- Ambiguous: ask to clarify (e.g. "Did you mean 2d6 or 2d20?") or suggest valid forms.

## Additional resources

- Example output formats: [examples.md](examples.md)
