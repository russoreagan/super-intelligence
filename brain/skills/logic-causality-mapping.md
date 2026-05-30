# Logic Causal Reasoning

Correlation isn't causation. Neither is sequence. "This happened, then that happened" is not the same as "this caused that" — but it's treated as equivalent constantly, and it produces wrong diagnoses, failed fixes, and surprised engineers.

This skill makes causal structure explicit: what actually depends on what, what change produces what effect, and what must be true for a plan to hold.

---

## Four Modes

Use the mode that matches the question.

### Mode 1: Root Cause Tracing
*"Why did this happen?"*

Work backwards from an observed effect to its cause — and to the cause of that cause.

**Process:**
1. State the observed effect precisely. Not "the system is slow" — "p95 latency increased from 120ms to 840ms after the Tuesday deploy."
2. Ask: what are the immediate causes that could produce this effect? List all plausible candidates.
3. For each candidate: what evidence would confirm or rule it out?
4. Eliminate candidates. For the survivors: what caused them?
5. Continue until you reach a cause that has no upstream cause within scope — or a point where further tracing requires different expertise or data.
6. Distinguish: **root cause** (the origin), **proximate cause** (the immediate trigger), **contributing factors** (conditions that allowed it).

### Mode 2: Impact Mapping
*"What breaks if I change X?"*

Work forwards from a proposed change through its downstream effects.

**Process:**
1. State the change precisely.
2. Identify direct dependents: what immediately relies on the thing being changed?
3. For each dependent: what behaviour changes, and what downstream systems rely on that behaviour?
4. Continue for two to three levels of dependency.
5. Mark irreversible effects — changes that, once made, cannot be cleanly undone.
6. Mark cascade risks — places where a small effect triggers a large one.

### Mode 3: Dependency Mapping
*"What must be true for this to work?"*

Identify the full set of conditions a plan depends on.

**Process:**
1. State the plan or goal.
2. Ask: what must be true in the environment for this to succeed? List all dependencies.
3. For each dependency: is it guaranteed, assumed, or unknown?
4. For each assumed dependency: what is the cost if the assumption is wrong?
5. Surface single points of failure — dependencies that, if they break, cause total plan failure with no fallback.

### Mode 4: Counterfactual Testing
*"Would Y still have happened without X?"*

Test a causal claim by reasoning about the counterfactual world.

**Process:**
1. State the causal claim: "X caused Y."
2. Imagine removing X. Would Y still happen via another path?
3. If yes: X is a contributing factor, not the root cause.
4. If no: X is necessary for Y — strong causal evidence.
5. Consider: was X sufficient alone, or did it require other conditions?

---

## Human Check-in

Before proceeding, use the `AskUserQuestion` tool:
