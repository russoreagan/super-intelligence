---
name: requirements-and-prd
description: Unified requirements gathering + PRD generation workflow. Combines "ask one question at a time", completeness scoring, and structured doc drafting (PRD/spec/proposal).
summary: Requirements gathering + PRD drafting with completeness scoring and structured templates.
triggers: [requirements, PRD, spec, feature, define, proposal, what should we build]
disable-model-invocation: true

---
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

* **[Sub-problem 1]:** Description
* **[Sub-problem 2]:** Description
* **[Sub-problem 3]:** Description

### Evidence

* [Data point or user feedback]
* [Observed behavior or metric]
* [Research findings]

### Customers / Personas

* **[Persona 1]:** Need/goal
* **[Persona 2]:** Need/goal
* **[Persona 3]:** Need/goal

---

## Shape of the Solution

[High-level solution summary - 1 paragraph]

### Solution Summary

1. **[Component 1]**
   - Detail
   - Detail

2. **[Component 2]**
   - Detail
   - Detail

### User Stories

#### [Category 1]
| As a… | I want to… | So that… |
| --- | --- | --- |
| **[Persona]** | [action] | [outcome] |
| **[Persona]** | [action] | [outcome] |

#### [Category 2]
| As a… | I want to… | So that… |
| --- | --- | --- |
| **[Persona]** | [action] | [outcome] |

---

## Acceptance Criteria

### UI / UX
| Given… | When… | Then… |
| --- | --- | --- |
| [Initial state] | [Action] | [Expected result] |
| [Initial state] | [Action] | [Expected result] |

### Functionality
| Given… | When… | Then… |
| --- | --- | --- |
| [Initial state] | [Action] | [Expected result] |

### Edge Cases
| Given… | When… | Then… |
| --- | --- | --- |
| [Edge condition] | [Action] | [Expected handling] |

---

## Non-Goals (Out of Scope)

* [What we're explicitly NOT building]
* [Future considerations]
* [Features for later phases]

---

## Open Questions

* [ ] **[Question]:** Owner: [Name], Deadline: [Date]
* [ ] **[Question]:** Owner: [Name], Deadline: [Date]

---

## Success Metrics

| Metric | Measurement Method | Target | Timeframe |
| --- | --- | --- | --- |
| [Metric name] | [How we measure] | [Numeric goal] | [When] |
| [Metric name] | [How we measure] | [Numeric goal] | [When] |

---

## Technical Considerations

* **[Architecture/System]:** Implication or requirement
* **[Performance]:** Requirement or constraint
* **[Security]:** Requirement or constraint
* **[Dependencies]:** External systems or services

---

## Go-to-Market / Rollout

* **Phase 1:** Description and timeline
* **Phase 2:** Description and timeline
* **Feature Flags:** Which features need flags for gradual rollout
* **Documentation:** What docs need to be created/updated
* **Training:** Who needs training and what format

---

## Risks & Mitigations

| Risk | Impact | Probability | Mitigation |
| --- | --- | --- | --- |
| [Risk description] | High/Med/Low | High/Med/Low | [How to address] |
| [Risk description] | High/Med/Low | High/Med/Low | [How to address] |

---

## Appendix

### Related Documents
* [Link to research]
* [Link to competitive analysis]
* [Link to user feedback]

### Revision History
| Date | Author | Changes |
| --- | --- | --- |
| YYYY-MM-DD | [Name] | Initial draft |
| YYYY-MM-DD | [Name] | [Description] |
```

### Using the Template

1. **Start with Project Overview table** - Fill in team members and links
2. **Define metrics first** - What success looks like (Primary, Secondary, Guardrails)
3. **Problem before solution** - Evidence-based problem statement with detailed context
4. **User stories in table format** - Clear As a/I want/So that structure organized by category
5. **Acceptance criteria as Given/When/Then** - Testable criteria for UI/UX, functionality, and edge cases
6. **Non-goals are critical** - Set clear boundaries to manage scope
7. **Open questions tracked** - Assign owners and deadlines for each question
8. **Go-to-Market section** - Include rollout phases, feature flags, documentation, and training
9. **Risks & Mitigations table** - Identify and address potential issues proactively

**File naming:** `docs/prd/[feature-name]-prd.md`

**Examples in repository:**
- `docs/prd/chat-mvp-data-retrieval-prd.md`
- `docs/prd/creative-library-competitor-search-prd.md`
- `docs/prd/creatives-dashboard-prd.md`

## Optional: Agile user stories (INVEST)
If the user needs a backlog:
- Convert MVP features into user stories with acceptance criteria.
- Validate INVEST (Independent, Negotiable, Valuable, Estimable, Small, Testable).

## Optional: Doc co-authoring loop (for longer docs)
For long/important docs (PRD/spec/RFC):
1. Context gathering
2. Draft section-by-section
3. Reader testing (fresh read for blind spots)
