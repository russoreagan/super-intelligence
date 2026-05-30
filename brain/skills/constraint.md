# Constraint

Applies constraint reasoning to any situation where limits are shaping (or blocking) what's possible. Diagnoses what kind of constraint work is needed and applies the right tool.

## Which tool fits

| You need to... | Tool |
|---|---|
| Test whether a stated constraint is actually real | hardness-testing |
| Flip a constraint into a creative driver | rule-inversion |
| Find the minimum that satisfies the actual requirement | scope-reduction |
| Find paths around a fixed constraint to reach the same goal | workaround-mapping |

## Routing Decision

- **Constraint feels like it might be an assumption, habit, or politics** → hardness-testing
- **Constraint is real but you want it to generate rather than block** → rule-inversion
- **Scope has grown beyond what's actually needed** → scope-reduction
- **Constraint is genuinely fixed and you need to route around it** → workaround-mapping
- **Unclear** → hardness-testing first; most "constraints" dissolve under scrutiny

---

## Hardness Testing

*Tests whether a stated constraint is real.*

Ask: who said this was fixed? When was that decided, and under what conditions? Has anyone tested it recently? Classify the constraint: physical law (truly hard), technical limit (hard but may change), organizational rule (soft — someone could change it), habit or assumption (not a constraint at all). For soft constraints: what would it take to move them? The goal is to separate genuine limits from constraints that are merely convenient to accept.

**Output:** Constraint classification (hard/soft/assumption), evidence for the classification, and for soft constraints: what it would take to move them.

---

## Rule Inversion

*Flips a constraint into a creative driver.*

Take the constraint and state it as a design requirement: "We must [constraint]." Now ask: what would a solution look like that didn't merely tolerate this limit but was actually *better because of it*? The constraint removes options — which remaining options does it make uniquely possible? Limitations forced some of the most interesting design decisions in history; they don't just restrict, they focus.

**Output:** The constraint reframed as a requirement, 3-5 directions that only become possible under this constraint, and the most promising one to develop.

---

## Scope Reduction

*Finds the minimum that satisfies the actual requirement.*

Separate what is wanted from what is needed. For each element of the current scope: what is the actual job it does? Could that job be done more simply, or not done at all? Apply the question: if we had to deliver this in half the time/budget/scope, what would we cut first? Often the answer reveals which parts were never truly necessary. The minimum viable version is usually clearer than the full version.

**Output:** The actual core requirement. Everything that can be removed. The simplest version that genuinely satisfies the need.

---

## Workaround Mapping

*Finds paths around a fixed constraint without removing it.*
