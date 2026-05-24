---
name: dnd-strategy
description: Create tactical strategy guides for D&D encounters with consistent formatting. Use when the user wants to plan combat strategies, create tactical options, or document encounter approaches. Reviews party composition to build options that leverage each character's strengths.
disable-model-invocation: true
---

# D&D Strategy Guide Creation

Create tactical strategy guides that help players think through combat and encounter options based on party composition.

## Before Creating a Strategy

**You MUST read the party data first.** Do not rely on memory or assumptions.

### Step 1: Get Party / Session Data

Use the app’s tools to load current party data:

- **Session with players:** Use a tool that returns the session’s players and their characters (e.g. session context, or **`query_character`** per player). If the app exposes a **party summary** or **get_party_tactical_summary(sessionId)** tool, use that to get all characters in one call.
- **Per character:** For each player in the session, call **`query_character`** (or the character API) with that character’s ID to get: name, class, level, abilities, skills, spells, features, equipment, resources.

Do not read from `content/characters/` or `data/characters/*.json`; use the app’s character/session APIs.

### Step 2: Spell/Ability Details (When Needed)

For precise tactical advice (range, duration, damage dice, saving throws), use the app’s spell/class APIs when available. For details the app doesn’t expose, you may reference D&D Beyond:

- **Spells**: `https://www.dndbeyond.com/spells/{spell-name-lowercase-hyphenated}`
- **Class Features**: `https://www.dndbeyond.com/classes/{class}`
- **2024 PHB Reference**: `https://www.dndbeyond.com/sources/dnd/phb-2024`

Use this when:
- Tactics depend on specific range or area of effect
- You need exact duration for timing strategies
- Concentration requirements affect spell combinations
- Saving throw type matters for target selection

### Step 3: Analyze Party Composition

After loading all character data via the app’s tools, identify:
- **Frontline**: Who can take hits?
- **Ranged**: Who attacks from distance?
- **Magic**: What spells are available? (read the actual spell list)
- **Utility**: Healing, buffs, crowd control?
- **Mobility**: Who can move quickly or pursue?
- **Gaps**: What does the party lack?

## Building Party-Aware Strategies

Each strategy option MUST:
1. **Reference actual abilities** – Use real spell/feature names from the character data returned by `query_character` (or party tool).
2. **Assign specific roles** – Name characters in tactics (e.g. "Theren takes elevated position").
3. **Leverage party strengths** – Build around what the party actually has.
4. **Account for weaknesses** – Address gaps in party composition.

## Strategy Output Structure

When producing a strategy (e.g. for the GM or as export), use this structure. You may output it as markdown in chat, or the app may support saving strategy docs; follow the app’s pattern. Logical structure:

- **Overview** (_index / situation): situation, what you know, party assets, questions to discuss.
- **Options** (option-a, option-b, option-c): each option with tactics, pros, cons, "Best if".

## Parent Page Format (_index.md)

```markdown
---
title: "{Encounter Name}"
date: {YYYY-MM-DD}
description: "{Brief one-line description}"
tags: ["combat", "tactics", "{relevant-tags}"]
---

## What You Know
- {Fact about the situation}
- {Fact about enemy positions}
- {Fact about environment}
- {Fact about allies/resources}

## Party Assets
- **{Character} ({Class})**: {Key abilities relevant to this encounter}
- **{Character}**: {Key abilities}
- **{Resource}**: {How it helps}

## Questions to Discuss

1. {Strategic question for party discussion}
2. {Another question}
3. {Character-specific consideration}
4. {Risk/reward question}
5. {Priority question}
```

## Option Page Format

```markdown
---
title: "{Option Name}"
date: {YYYY-MM-DD}
description: "{Tagline in quotes - 3-6 words}"
tags: ["{tactic-type}", "tactics"]
weight: {1|2|3}
---

{One paragraph describing the core approach}

## Tactics

1. **{Phase 1}**: {Description with character names}
 - {Character}: {Specific action using their abilities}
 - {Character}: {Specific action}
2. **{Phase 2}**: {Description}
3. **{Phase 3}**: {Description}

## Pros
- {Advantage}
- {Advantage}

## Cons
- {Disadvantage}
- {Disadvantage}

## Best If
{One sentence describing when this option is ideal}
```

## Option Naming Conventions

Use evocative names that suggest the approach:
- "The Moat Ambush" (defensive)
- "The City Assault" (offensive)
- "The Ultimatum" (diplomatic)
- "The Night Raid" (stealth)
- "The Feint" (deceptive)

## Weight Values

Control display order with `weight` in frontmatter:
- `weight: 1` - Recommended/safest option
- `weight: 2` - Alternative option
- `weight: 3` - Riskier/situational option

## Character-Specific Sections

Add tips for characters with key roles in the strategy:

```markdown
## Ranger Tips for {Name}

- **{Ability}**: {How to use it in this scenario}
- **{Spell/Feature}**: {Tactical advice}
```
