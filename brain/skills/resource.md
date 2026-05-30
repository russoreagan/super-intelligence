# Resource

Applies resource reasoning to allocation, constraints, leverage, and waste. Diagnoses what kind of resource work is needed and applies the right tool.

## Which tool fits

| You need to... | Tool |
|---|---|
| Distribute limited resources across competing needs | allocation-analysis |
| Find what is actually constraining throughput | bottleneck-analysis |
| Find the highest-leverage use of available resources | leverage-mapping |
| Find where resources are being lost, duplicated, or underused | waste-audit |

## Routing Decision

- **Have limited resources and competing priorities — need to allocate** → allocation-analysis
- **Things are moving slowly — need to find the actual constraint** → bottleneck-analysis
- **Want to maximize impact with what you have** → leverage-mapping
- **Sense that effort isn't producing commensurate output** → waste-audit
- **Unclear** → bottleneck-analysis; finding the constraint usually determines how to allocate and where the leverage is

---

## Allocation Analysis

*Distributes limited resources across competing needs.*

Make the trade-offs explicit rather than implicit. Map all competing claims on the resource. For each: what is the expected return on investment? What is the cost of under-resourcing? Are any allocations producing diminishing returns? Are any starved below a minimum threshold for effectiveness? Allocation decisions made without explicit trade-off analysis tend to satisfy the loudest voice rather than the best use.

**Output:** Allocation map with expected returns, identified diminishing returns, minimum thresholds, and the recommended distribution with explicit trade-off reasoning.

---

## Bottleneck Analysis

*Identifies what is actually constraining throughput.*

Apply Theory of Constraints logic: the system can only move as fast as its slowest point. Adding resources anywhere except the bottleneck doesn't improve throughput. Identify the current bottleneck: where does work queue up, where does it wait, where is output consistently below demand? Verify it's the actual bottleneck and not a symptom. Once identified: what would it take to elevate it?

**Output:** Bottleneck identified with evidence, verification that it's the system constraint (not a symptom), and the options to elevate it with effort estimates.

---

## Leverage Mapping

*Finds the highest-leverage use of available resources.*

Leverage is where the same input produces the most output. Not all investments are equal — some create multiplying effects (better systems, better people, better tools) while others are purely linear. For each candidate use of resources: is the return linear (1 unit in → 1 unit out) or multiplying (1 unit in → many units out over time)? Prioritize multiplying investments, especially those that unblock other work.

**Output:** Candidate uses of resources mapped by leverage type (linear vs. multiplying), with the highest-leverage options ranked and reasoning for each.

---

## Waste Audit

*Finds where resources are being lost, duplicated, or underused.*
