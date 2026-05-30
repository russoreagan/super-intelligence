# Requirements & PRD (Unified)

## Intent
Use when the user asks to define requirements, write a PRD, spec a feature, or turn an idea into an actionable plan.

## Workflow overview
This workflow merges three complementary approaches:
- **Vibe PRD**: asks questions one at a time; adapts to user technical level.
- **Product Owner scoring**: quality score with targeted gap-filling until "ready".
- **Co-authoring**: structure → refine → reader-test for clarity.

## Step 0: Context and artifacts
1. Check whether a PRD/spec already exists.
2. If research exists (e.g. `docs/research-*.txt`), read and reference it.
3. Ask for:
   - audience (who will read this)
   - constraints (timeline/budget/security/compliance)
   - success definition (metrics)

## Step 1: Determine technical level
Ask the user which best fits:
- **A**: non-developer / "vibe-coder"
- **B**: developer
- **C**: in-between

## Step 2: Requirements capture (ask ONE at a time)
Always start with:
1. Product/feature name
2. One-sentence problem statement
3. Launch goal

Then branch by level:
- **A**: persona → journey story → 3–5 must-haves → v2 list → 1–2 success metrics → vibe → constraints
- **B**: personas/JTBD → user stories → MoSCoW → success metrics (targets) → technical/UX requirements → risks → business model/constraints
- **C**: users + current solutions → main flow → 3–5 must-haves (why) → v2 list → success metrics (1mo/3mo) → design direction → constraints

## Step 3: Quality scoring (readiness gate)
Score completeness out of 100 and iterate until the requirements are "ready":
- Business value & goals (30)
- Functional requirements (25)
- User experience (20)
- Technical constraints (15)
- Scope & priorities (10)

If score < 90: ask 2–3 targeted questions focused on the weakest area.

## Step 4: Verification echo
Summarize back the understanding:
- product
- target user
- problem
- must-have features
- success metrics
- constraints

Ask for corrections.

## Step 5: Generate the PRD

### PRD Template

Use this standard template for PRDs:

```markdown
# [Feature Name] - Product Requirements Document

**Version:** 1.0
**Last Updated:** YYYY-MM-DD
**Status:** Draft | In Review | Approved
**Related Technical Plan:** [Link or TBD]

---

## PRD Template

This document follows the company PRD template.

---

## Project Overview

| **Team** | [Team Name] |
| --- | --- |
| **Quarter** | Q# YYYY or TBD |
| **Product Team** | **Product Manager:** [Name or TBD]  **Product Designer:** [Name or TBD] |
| **Engineering Team** | **EM:** [Name or TBD]  **Engineers:** [Names or TBD] |
| **Data Science** | **Manager:** [Name or N/A]  **Analyst:** [Name or N/A] |
| **Key Stakeholders** | [Names or TBD] |
| **Company Objective (OKR)** | [Which company OKR this supports] |
| **Jira Epic** | [Link or TBD] |
| **Designs** | [Figma link or TBD] |
| **Tech Plan** | [Link or TBD] |

---

## Metric to Move

* **Primary:** [Main metric with target]
* **Secondary:** [Supporting metric]
* **Guardrails:** [Metrics that must not degrade]

---

## Problem Being Solved

**[One-sentence problem statement in bold]**

[2-3 paragraphs explaining the problem in detail]

### Detailed Problem & Additional Context
