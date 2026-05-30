# Logic Fixer

Diagnosis without repair is incomplete. This skill takes broken reasoning and produces a corrected version — one where the premises actually support the conclusion, the hidden assumptions are made explicit, the fallacies are removed, and the argument can be defended.

The output is not a critique. It is a fixed version of what you were trying to say.

---

## Your Process

**Step 1: Diagnose before repairing**
Don't jump to a fix. First, be specific about what's broken:
- What type of failure is this? (Invalid inference / unsupported premise / logical fallacy / internal contradiction / hidden assumption doing too much work / overclaimed conclusion)
- Where exactly does the reasoning break — at which step?
- What is the argument *trying* to establish? (Separate the intent from the execution — the intent might be sound even if the argument isn't.)

**Step 2: Classify the repair needed**
Different failure modes need different repairs:

| Failure | Repair |
|---|---|
| Unsupported premise | Add evidence, or qualify the premise |
| Overclaimed conclusion | Narrow the conclusion to what the premises actually support |
| Missing step | Make the implicit inference explicit |
| Circular reasoning | Identify the begged question; provide independent support for the conclusion |
| False dichotomy | Reframe to acknowledge the full option space |
| Invalid inference | Add the bridging premise that makes the inference valid |
| Equivocation | Disambiguate the term; use different words for different senses |
| Hidden assumption | Surface it as an explicit premise; assess whether it holds |

**Step 3: Produce the fixed version**
Rewrite the argument so that:
- Every premise is stated explicitly
- Every inference from premise to conclusion is valid
- The conclusion claims no more than the premises support
- Fallacies are removed, not just noted
- Hidden assumptions are surfaced as explicit premises (and labelled as assumptions if they're not established)

**Step 4: Note what changed and why**
Don't just hand over the fixed version without explanation. For each change:
- What was wrong
- What the fix was
- Whether the fix strengthens or narrows the original claim

If the original conclusion cannot be made to hold under any reasonable repair — because the premises are too weak or the claim is simply unsupported — say so directly rather than producing a version that technically avoids fallacies but doesn't actually establish what the author wanted to establish.

---

## Human Check-in

Before proceeding, use the `AskUserQuestion` tool:

- **Question:** "How do you want to run this?"
- **Header:** "Scope"
- **Options:**
  - **Full analysis** — Complete all steps, reasoning shown throughout
  - **Key findings only** — Bottom-line output, skip step-by-step detail
  - **Fixed version first** — Show the corrected reasoning before explaining what was wrong
  - **Refine the framing** — Adjust what we're analyzing before starting

Proceed based on their selection.

## Output Format

**Original reasoning:**
[The broken argument, spec, or reasoning as provided]
