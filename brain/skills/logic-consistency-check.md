# Logic Consistency Check

Requirements drift. Specs accumulate. A document written over weeks by multiple people — or a set of decisions made incrementally — can contain contradictions that nobody noticed because each piece was reviewed in isolation.

This skill reads the whole and finds where the parts disagree.

---

## Your Process

**Step 1: Map the claims**
Before checking for consistency, inventory what the document asserts:
- Goals and objectives stated
- Constraints and non-negotiables stated
- Assumptions stated or implied
- Decisions and their stated rationale
- Any numbered requirements or acceptance criteria

This map is what gets checked for internal coherence — not whether any claim is *true*, but whether the claims are consistent with each other.

**Step 2: Check goal-constraint conflicts**
Do the stated goals require violating stated constraints? Common patterns:
- A performance goal that requires more resources than the budget allows
- A simplicity goal combined with a feature list that requires complexity
- A timeline that requires skipping steps the quality requirements depend on

**Step 3: Check requirement-requirement conflicts**
Do individual requirements contradict each other?
- Two requirements that can't both be satisfied simultaneously
- A requirement that is a special case of another requirement but handled differently
- Requirements that use the same term with different implicit meanings (equivocation across requirements)

**Step 4: Find edge cases that expose conflicts**
Some contradictions only appear at the boundary. Ask: what happens when...
- Input is at its minimum and maximum values simultaneously required
- Two features interact that were designed independently
- The happy path assumption fails
- A stated exception meets a stated rule

**Step 5: Check assumption coherence**
Implicit assumptions are the most dangerous source of inconsistency — stated nowhere, but load-bearing everywhere. Surface them:
- What must be true for each requirement to be satisfiable?
- Do any of those assumptions contradict each other?
- Do any assumptions contradict stated facts?

---

## Human Check-in

Before proceeding, use the `AskUserQuestion` tool:

- **Question:** "How do you want to run this?"
- **Header:** "Scope"
- **Options:**
  - **Full analysis** — Complete all steps, reasoning shown throughout
  - **Key findings only** — Bottom-line output, skip step-by-step detail
  - **Contradictions list only** — Flag the specific inconsistencies without full analysis
  - **Refine the framing** — Adjust what we're analyzing before starting

Proceed based on their selection.

## Output Format

**Subject:** [what was checked]

**Contradictions Found**

| Type | Item A | Item B | Conflict |
|---|---|---|---|
| Goal vs constraint | [goal] | [constraint] | [why they conflict] |
| Requirement vs requirement | [req] | [req] | [why they conflict] |
| Assumption vs fact | [assumption] | [fact] | [why they conflict] |

*"None found" if clean.*

**Edge Cases That Expose Conflicts**
- [scenario]: [which requirements or goals it breaks]

**Hidden Assumptions**
- [assumption]: [which requirements depend on it; whether it's safe]
