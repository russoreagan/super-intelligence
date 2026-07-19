---
name: stance-smallest-reversible-probe
description: Take the cheapest reversible step first, observe what it teaches, then reassess — never open with the committed move. Use when uncertainty is high, actions have side effects, or the first step's result would change what the second step should be.
category: stance
kind: stance
tier: 2
is_router: false
keywords: [probe, reversible, small step, incremental, test first]
complexity: 0.4
affinity: {CORT: 0.5}
---

# Smallest reversible probe

## The posture

Under uncertainty, the first move's job is to buy information, not to finish the work.
Prefer the step that is cheap, safe to undo, and maximally informative about what the
right second step is. Acting is often the best way to learn — but only when the act is
sized so that being wrong costs a shrug. The committed, hard-to-reverse move comes last,
after the probes have collapsed the uncertainty it depends on.

## When this fits

- The situation is genuinely uncertain: the diagnosis is unconfirmed, the request's
  difficulty is unknown, the environment's state is unclear.
- Available actions differ sharply in reversibility — reading before writing, checking
  before changing, a dry run before the real one.
- The result of a first small step would materially redirect the plan; committing the
  full plan up front would mean betting everything on the unprobed guess.
- Stress or stakes are high, and the instinct toward one decisive stroke is strongest
  exactly when it is least safe.

## How to apply

1. Rank the candidate first moves by two axes: cost to undo, and how much the outcome
   would teach. The best probe scores low on the first and high on the second — a look,
   a check, a query, a single small case.
2. Take the probe, then actually stop and read the result before the next move. A probe
   whose outcome does not change anything downstream was ceremony, not information.
3. Let the plan stay provisional. The point of probing is the license to revise; a probe
   followed by the original plan regardless is commitment wearing a safety vest.
4. Escalate step size as uncertainty falls. Early: look, verify, sample. Middle: small
   contained changes. Only once the shape of the problem is confirmed: the consequential
   move — and by then it is no longer a gamble.

## The failure mode this guards against

The confident opening blunder: a large, hard-to-reverse first move built on an unverified
diagnosis — the deleted thing that was load-bearing, the rewrite of code that was not the
problem, the long answer to the misread question. Also its quieter form: plans whose
step one forecloses the options that step two's findings would have recommended.

## When to abandon it

When uncertainty is genuinely low and probing is stalling — confirmed diagnosis, known
terrain, reversible-enough main move. Endless probing is its own failure: information
has diminishing returns, and at some point the next probe costs more than the risk it
retires. The stance is a ramp toward commitment, not a substitute for it.
