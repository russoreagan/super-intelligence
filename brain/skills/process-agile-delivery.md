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
