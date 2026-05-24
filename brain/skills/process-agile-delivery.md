---
name: agile-delivery
description: Use when turning product goals into sprint execution including writing INVEST-compliant user stories, defining acceptance criteria with Given/When/Then, prioritizing backlogs, planning sprints with capacity, and tracking delivery metrics.
summary: INVEST stories, acceptance criteria (Given/When/Then), sprint planning, backlog prioritization (ICE/RICE), and delivery metrics (velocity, cycle time).
triggers: [story, acceptance criteria, sprint, backlog, velocity, agile, scrum, user story, INVEST, Given/When/Then]
disable-model-invocation: true

---
# Agile Delivery (Definitive)

## Goal
Deliver product value in small increments with predictable cadence, clear acceptance criteria, and continuous feedback loops.

## When to Use
- Writing user stories from product requirements
- Planning sprints with team capacity
- Prioritizing product backlog
- Defining acceptance criteria for stories
- Tracking delivery metrics and velocity
- Running agile ceremonies

## Core Responsibilities

| Responsibility       | Key Activities                           |
| -------------------- | ---------------------------------------- |
| Backlog Management   | Priorities, dependencies, readiness      |
| Story Writing        | INVEST compliance, acceptance criteria   |
| Sprint Planning      | Capacity, commitment, risk identification|
| Stakeholder Alignment| Scope tradeoffs, expectation management  |
| Delivery Tracking    | Velocity, cycle time, predictability     |

## User Story Writing

### INVEST Criteria
Every story should be:

| Criterion       | Description                                | Anti-Pattern                      |
| --------------- | ------------------------------------------ | --------------------------------- |
| **I**ndependent | Minimal coupling to other stories          | "Must complete after story X"     |
| **N**egotiable  | Details can be discussed with team         | Over-specified implementation     |
| **V**aluable    | Delivers user or business value            | Technical task with no user value |
| **E**stimable   | Team can estimate effort                   | Too vague or undefined            |
| **S**mall       | Fits within a sprint                       | Multi-week epic                   |
| **T**estable    | Clear pass/fail criteria                   | "Improve performance"             |

### Story Format
```
As a [persona/role],
I want to [action/capability],
So that [benefit/outcome].
```

### Story Examples

**Good:**
```
As a logged-in customer,
I want to filter products by price range,
So that I can find items within my budget quickly.

Acceptance Criteria:
- [ ] Price filter shows min/max sliders
- [ ] Results update as slider moves (debounced 300ms)
- [ ] Selected range persists when navigating back
```

**Bad:**
```
As a user,
I want the system to be faster.
```
(Not testable, not specific, not independently deliverable)

## Acceptance Criteria

### Given/When/Then Format (BDD)
```gherkin
Feature: Shopping Cart

Scenario: Add item to cart
  Given I am on a product page
  And the product is in stock
  When I click "Add to Cart"
  Then the cart icon shows updated count
  And a confirmation toast appears
  And the item appears in cart dropdown

Scenario: Add out-of-stock item
  Given I am on a product page
  And the product is out of stock
  When I view the page
  Then the "Add to Cart" button is disabled
  And "Notify Me" option is shown
```

### Acceptance Criteria Checklist
- [ ] Happy path covered
- [ ] Edge cases identified (empty states, errors, limits)
- [ ] Non-goals explicitly stated
- [ ] Analytics/telemetry requirements captured
- [ ] Accessibility requirements included
- [ ] Performance expectations defined (if relevant)

### Definition of Ready
A story is ready for sprint when:
- [ ] Clear acceptance criteria
- [ ] Dependencies identified and resolved
- [ ] Design/mockups available (if UI)
- [ ] Team has estimated points
- [ ] No open questions blocking implementation

## Sprint Planning

### Pre-Planning (Before Sprint Planning)
1. Groom top of backlog (stories meet Definition of Ready)
2. Review velocity trends from last 3 sprints
3. Identify team capacity (holidays, meetings, training)
4. Flag known dependencies or blockers

### Sprint Planning Workflow
1. **Confirm sprint goal**: What outcome defines success?
2. **Review capacity**: Available person-days × focus factor (typically 0.6-0.8)
3. **Select stories**: Pull from prioritized backlog up to capacity
4. **Break into tasks**: Team identifies technical work per story
5. **Commitment**: Team agrees to sprint scope
6. **Identify risks**: What could derail the sprint? Mitigation plans?

### Capacity Planning
```
Available Capacity = (Team Members × Sprint Days × Hours/Day) × Focus Factor

Example:
- 5 developers × 10 days × 6 hours = 300 hours
- Focus factor 0.7 (meetings, reviews, etc.)
- Effective capacity: 210 hours ≈ 26 story points (at 8 hrs/point)
```

### Sprint Goal Template
```
Sprint [N] Goal: [Outcome]
Success Criteria:
- [ ] Measurable criterion 1
- [ ] Measurable criterion 2
Risks:
- Risk 1: Mitigation plan
```

## Backlog Prioritization

### ICE Scoring
```
ICE Score = Impact × Confidence × Ease

Impact: 1-10 (business/user value)
Confidence: 1-10 (certainty of impact)
Ease: 1-10 (inverse of effort)
```

### RICE Scoring
```
RICE Score = (Reach × Impact × Confidence) / Effort

Reach: Users affected per quarter
Impact: 0.25 (minimal), 0.5 (low), 1 (medium), 2 (high), 3 (massive)
Confidence: 100% (high), 80% (medium), 50% (low)
Effort: Person-months
```

### Prioritization Meeting Agenda
1. Review new items (5 min each: describe, discuss, initial priority)
2. Re-evaluate existing items based on new data
3. Identify dependencies that affect priority
4. Confirm top N ready items for next sprint
5. Flag items needing research or spikes

## Delivery Metrics

### Velocity
- **What**: Story points completed per sprint
- **Use for**: Forecasting, not performance measurement
- **Track**: 3-sprint rolling average
- **Warning**: Don't compare velocity across teams

### Cycle Time
- **What**: Time from work started to done
- **Use for**: Identifying bottlenecks, predicting delivery dates
- **Target**: Reduce cycle time to improve flow

### Work in Progress (WIP)
- **What**: Number of items in progress simultaneously
- **Use for**: Flow optimization (Little's Law)
- **Target**: Keep WIP limits per column/team member

### Sprint Burndown
- **What**: Remaining work vs. ideal line
- **Use for**: Early warning of scope issues
- **Warning**: Not useful if stories aren't broken down

### Predictability
```
Predictability = Stories Completed / Stories Committed

Target: > 80%
If < 70%: Review estimation accuracy, scope creep, or blockers
```

## Agile Ceremonies Quick Reference

| Ceremony       | Duration   | Participants         | Outcome                      |
| -------------- | ---------- | -------------------- | ---------------------------- |
| Sprint Planning| 2-4 hours  | Team + PO            | Sprint backlog, commitment   |
| Daily Standup  | 15 min     | Team                 | Sync, blockers identified    |
| Backlog Grooming| 1-2 hours | Team + PO            | Stories refined, estimated   |
| Sprint Review  | 1-2 hours  | Team + Stakeholders  | Demo, feedback collected     |
| Retrospective  | 1-1.5 hours| Team                 | Improvement actions          |

## Anti-Patterns to Avoid
- **Story Bloat**: Stories that span multiple sprints
- **Acceptance Criteria Creep**: Adding criteria mid-sprint
- **Velocity Gaming**: Inflating points to look productive
- **Ignoring Retro Actions**: Not following through on improvements
- **Scope Creep**: Adding work without removing something
- **No Definition of Done**: Vague completion criteria

## Implementation Checklist
- [ ] Backlog prioritized with ICE/RICE scores
- [ ] Top 10 stories meet Definition of Ready
- [ ] Stories follow INVEST criteria
- [ ] Acceptance criteria use Given/When/Then format
- [ ] Sprint capacity calculated and documented
- [ ] Sprint goal defined with success criteria
- [ ] Velocity tracked across sprints
- [ ] Cycle time and WIP monitored
- [ ] Retrospective actions tracked and completed
