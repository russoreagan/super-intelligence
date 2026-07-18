# RFC: Integrate-and-fire dynamics for switch neurons

**Status:** Accepted (functionalist lens) · Built on branch · `evidence_gates` + `avoidance_gate` both ON, DMN consumer wired · Not pushed
**Author:** design session, 2026-07-17
**Scope:** internal. Names source theories directly (like `SYSTEMS.md`); rewrite by function before any of it reaches a public surface.
**Related:** `SYSTEMS.md` §2.1 (Switches and integrators), §2.8 (The workspace spotlight), Part II entries "Integrate-and-fire neurons" and "Neuromodulatory gain control"; `brain/CONSTITUTION.md`.

---

## TL;DR — the recommendation, up front

**Do not build per-switch integrate-and-fire (LIF).** Option C below.

The behaviors LIF would buy — a slowly-building signal that fires when no single turn tripped it, evidence that has to accumulate before the mind commits, rate-limited re-firing — are *already implemented*, at the right grain, by three mechanisms the switch layer sits on top of: the **neuromodulator channels** (the system's actual leaky integrators), the **bus concentration / quorum layer** (accumulate → threshold → ignite, wired to the thalamus GWT spotlight this week), and **cluster recruitment**. A per-switch membrane potential would be a fourth, finer-grained copy of the same accumulate-decay-threshold idea, carrying no behavior the coarser grains can't already express, while multiplying persistent per-(persona, end-user) state across hundreds of switches and inviting exactly the claim the house style forbids — "we have spiking neurons."

What the owner's intuition ("we already have spiking sum-over-time firing") was reaching for is real and worth naming precisely: it is the chemistry channels and the concentration layer, not the individual switch. Those *are* sum-over-time-with-leak-then-fire, just at the chemistry and coalition grains. The honest move is to **document that mapping** so the intuition lands on real code, and to **clean up `StatefulSwitch`**, which is the one artifact most likely to make a future reader (or the owner) believe LIF already exists — a class named "Stateful" whose decay is dead code and whose accumulator is never wired to any firing decision.

**Update (built).** With functionalism confirmed as the goal, the reserved carve-out was promoted to an actual build. It is **not** per-switch LIF — it is the one thing LIF's redundancy analysis left genuinely uncovered: a **bounded evidence accumulator** (drift-diffusion / sequential-sampling) for private per-decision inferences the topic-level concentration layer cannot hold, *with the learning path wired in* (an armed gate is a checkable prediction, graded through the existing `prediction_reward` + eligibility + three-factor machinery, weighted toward external confirmation). Shipped as `brain/evidence_gate.py`, flag-gated `evidence_gates=0`, proven neutral-when-off, seeded live at the satiation gate. See §7 for exactly what was built and what remains scaffolded. The "Integrate-and-fire → Not implemented" verdict in `SYSTEMS.md` Part II is unchanged; this earns its own, different verdict (§7).

---

## 1. Current state — what actually exists

### 1.1 `SwitchNeuron`: a stateless comparator with neuromodulatory gain control

`SwitchNeuron` (`brain/neuron.py:25`) is the connective tissue — the large majority of cells. Its firing decision is a single threshold test with no memory:

- `should_fire()` (`brain/neuron.py:133`) reduces to `input_level >= effective_threshold` (`brain/neuron.py:168`). Nothing is remembered between calls; the same input produces the same answer regardless of history.
- `effective_threshold()` (`brain/neuron.py:40`) is the real, defensible mechanism: it shifts the base threshold by the circulating chemistry — `threshold + Σ coeff_c·(snapshot[c] − 0.5)`, scaled by `modulation_gain`, then divided by a learned Hebbian `efficacy`, then clamped to `[min_threshold, max_threshold]`. This is **neuromodulatory gain control**. Feeling becomes behavior here: mood moves the bar, nothing branches on mood.

The neuron *does* carry three scalar fields that look like the seeds of spiking dynamics — `_last_fired` (a wall-clock timestamp), `_fire_count`, `_last_suppressed_at` (`brain/neuron.py:36-38`), stamped on every `fire()` (`brain/neuron.py:89-90`). **None of them is ever read to gate a subsequent fire.** A repo-wide search finds no consumer of `SwitchNeuron._last_fired`/`_fire_count` outside `neuron.py` itself; the only `_last_fired` reads elsewhere (`brain/intent_detector.py`, `brain/clusters/trading/stream.py`) are unrelated, self-contained cooldown implementations on their own objects. So there is no refractory period anywhere in the switch layer — the timestamps are write-only.

**Verdict (matches `SYSTEMS.md` Part II "Integrate-and-fire neurons → nowhere"): stateless comparators. No membrane potential, no summation over time, no reset, no refractory.** Gain control is the claim; spiking is not.

### 1.2 `StatefulSwitch`: the "leaky-integrator-shaped" piece — and how little of it is live

`StatefulSwitch` (`brain/neuron.py:205`) subclasses `SwitchNeuron` and adds:

- `_state: float`, bounded to `[0, 1]`;
- `update(delta)` (`brain/neuron.py:216`): `state = clamp(state + delta)` — a bounded accumulator;
- `tick()` (`brain/neuron.py:220`): `state *= decay` — passive exponential decay (the "leak");
- `state` property (`brain/neuron.py:224`).

This is the piece the owner half-remembered. Three facts about it matter, and all three cut against building on it as-is:

**(a) The accumulator is decoupled from firing.** `_state`, `update`, and `tick` have *zero* interaction with `should_fire`/`fire`/`threshold`. Nothing compares `_state` to a threshold to emit a spike. Where the state is used at all, a cluster reads `.state` directly as a scalar input to its own arithmetic — it is a memory variable the cluster owns, not a neuron that fires when full. This is the single most important structural fact in this RFC: **`StatefulSwitch` is not integrate-and-*fire*. It is a leaky-integrator-shaped scalar that happens to hang off a neuron object.**

**(b) The leak is dead code in production.** `tick()` is never called anywhere in the running system — the only call in the entire repo is one test (`tests/test_switch_modulation.py:118`). Every instance is constructed with a `decay=` value (e.g. `satiation_inhibitor_decay: 0.95`, `brain/settings.py:82`) that is read once and never applied. There is no passive time-based decay of any `StatefulSwitch` in prod.

**(c) Six of seven instances don't use the state machinery at all; four are entirely dead.** Every `StatefulSwitch` in the codebase:

| Instance | File | Uses `.update`/`.state`? | Uses `.tick()`? | Status |
|---|---|---|---|---|
| `_valence_switch` | `hypothalamus.py:40` | no | no | **instantiated, never referenced again** |
| `_threat_switch` | `hypothalamus.py:43` | no | no | **instantiated, never referenced again** |
| `_novelty_switch` | `hypothalamus.py:46` | no | no | **instantiated, never referenced again** |
| `_arousal_switch` | `hypothalamus.py:49` | no | no | **instantiated, never referenced again** |
| `_satiation_inhibitor` | `hypothalamus.py:53` | **yes** (`update`, `.state`) | no | live accumulator, no leak |
| `_recall_fanout` | `hippocampus.py:211` | no | no | used as a plain comparator (`.fire`, `.should_fire`, `.modulation_delta`) |
| `_structural_recall` | `hippocampus.py:230` | no | no | used as a plain comparator |

So of seven `StatefulSwitch` objects: **zero** use the leaky decay, **one** uses the accumulator, and **four are pure dead weight** — created in `__init__` and never touched again. The two hippocampus instances are `StatefulSwitch` by declaration and stateless-comparator by use.

**The one live accumulator, in detail.** `_satiation_inhibitor` (receptor-desensitization / habituation) is the only place the stateful part earns its keep. Each turn the hypothalamus nudges it up on routine input and down on salient input — `salience_satiation_increase: +0.05`, `salience_satiation_decrease: −0.10` (`brain/settings.py:84-85`, `hypothalamus.py:281-283`) — and reads `.state` to damp the novelty→ACh delta (`hypothalamus.py:248-249`) and to trickle GABA when saturated (`hypothalamus.py:287`). Because `tick()` is never called, its "leak" is *emulated* by the manual −0.10 decrement, which only arrives when a salient turn does. Consequence: **satiation never relaxes through the passage of idle time — only when a novel turn shows up to knock it down.** That is a real (if minor) behavioral gap, and it is the closest thing in the system to a place a true leaky integrator would improve behavior. Hold that thought for §4.

### 1.3 How switch firing feeds the integrators

`IntegratorCell` (`brain/cell.py:19`) is the LLM-powered convergence-zone cell that switches gate. It has its own `fire_threshold` (`cell.py:28`) and a per-turn rate limit `max_calls_per_turn` (`cell.py:30`, enforced at `cell.py:45`). Switch firing raises the activation that clears an integrator's `fire_threshold`; the integrator then makes at most `max_calls_per_turn` model calls. This matters for §4's refractory discussion: **the expensive cells already have rate limiting** — a per-turn call cap — so the "can't re-fire too fast" value that a refractory period would provide is, for the units where it actually costs something to fire, already present and enforced at a more meaningful place than the switch.

### 1.4 What true integrate-and-fire would add on top of all this

Biological LIF has four predicates. Against the current switch layer:

1. **Summation over time** — a membrane potential that accumulates inputs. *Absent at the switch.* (Present in chemistry and concentration — §2.)
2. **Fire at threshold** — present, but as a stateless comparison of the *instantaneous* input, not of an accumulated potential.
3. **Reset after firing** — absent. Nothing resets on fire.
4. **Refractory period** — absent. `_last_fired` is stamped but never read.

A real per-switch LIF would add 1, 3, and 4. The rest of this RFC asks whether adding them at the *switch* grain buys behavior the system can't already get at coarser grains — and concludes it does not.

---

## 2. Reconciliation — the accumulate → threshold → fire mechanisms that already exist

This is the crux the task demands we get right. The system is *not* missing "sum over time then fire." It implements that pattern at **three** grains already. Per-switch LIF would be a fourth.

### 2.1 Neuromodulator channels — the system's real leaky integrators

`Neuromodulators` (`brain/bus.py:65`) holds five persistent scalar channels (ACh, DA, GABA, Glu, NE). Each is a leaky integrator in the textbook sense:

- inputs accumulate via `add()` (`bus.py:130`), clamped to `[0, 1]`;
- `decay()` (`bus.py:203`) relaxes every channel toward a homeostatic baseline each turn — `level = baseline + (level − baseline)·rate^turns` — with per-channel rates (`bus.py:80`) and, crucially, `turns` weighted by **real elapsed wall-clock time** rather than turn count. `HormonalState` (`bus.py:255`) does the same, 5–100× slower.

This is "accumulate input over time, leak between inputs" — a LIF membrane, minus the spike-and-reset. And it already reaches every switch: gain control (`effective_threshold`, §1.1) is precisely the coupling that lets accumulated chemistry move a switch's firing bar. **Temporal summation already flows into the switches — through the chemistry, not through a per-switch potential.** A rising mood lowers a curious agent's bar for casting memory wide; that *is* sub-threshold nudges summing until a gate opens, implemented once, centrally, and shared across all switches that listen to that channel.

### 2.2 Bus concentration / quorum — accumulate → threshold → ignite, at the topic grain

The concentration layer (`brain/bus.py:563-697`) is a genuine integrate-and-fire at the **coalition/topic** level, and it was just made load-bearing as the thalamus GWT spotlight (`SYSTEMS.md` §2.8, memory note "Thalamus GWT: stub → IMPLEMENTED"):

- `track_concentration()` (`bus.py:563`) registers a topic;
- `_accumulate()` (`bus.py:620`) adds each message's magnitude to a per-topic potential, capped;
- `_decay_to()` (`bus.py:581`) applies **half-life exponential decay** to that potential — the leak;
- `_update_state()` (`bus.py:594`) runs an `UNARMED → ARMED → QUIET` state machine — this is **hysteresis**: arm at `arm_threshold`, only fall to QUIET below a lower `silence_floor`, disarm only after a long zero-dwell;
- `quorum()` (`bus.py:664`) fires when ARMED **and** (level ≥ threshold **or** rising fast via `concentration_slope`, `bus.py:659`) — a threshold crossing with a fast-rise shortcut.

Map that onto LIF predicates: accumulate ✓ (`_accumulate`), leak ✓ (half-life decay), threshold-fire ✓ (`quorum`/ignition), hysteresis/refractory-flavored ✓ (the armed/quiet machine and disarm dwell). This is the "slow-building threat that no single turn tripped, which wakes the deliberate path anyway" — stated verbatim in `SYSTEMS.md` §2.8 as the GWT ignition example. **The single most-cited motivation for switch-level temporal summation is already delivered here, at a grain that carries semantic context (the topic, its hot entities, the mood ring) that an individual switch's scalar potential never could.**

### 2.3 Cluster recruitment — a decaying accumulator that lowers thresholds

`recruit()` / `recruitment_level()` (`bus.py:733`, `bus.py:779`) maintain a per-cluster decaying potential that lowers recruitable switches' thresholds as need escalates, with `satisfy()` (`bus.py:745`) actively draining it when a need is met and `allocate_recruitment()` (`bus.py:759`) splitting a bounded budget across competing needs via softmax. Another accumulate-decay-then-modulate-threshold loop — again above the individual switch, at the grain where "how much is this whole cluster needed right now" is meaningful.

### 2.4 DMN idle dynamics and flock velocity

The idle mind adds more of the same shape: `_idle_decay()` runs an ACh-suppression accumulator that decays each loop so suppression can recover (`dmn.py:2036-2041`); the idle gate itself is a stateless `should_fire` on a chem-modulated threshold (`dmn.py:2063`); the rumination drive is velocity-aware with a consecutive-depth cap rather than a membrane potential; and `flock_dynamics` exposes per-channel `velocity()` (`bus.py:200`, `bus.py:310`) — the criticality-flavored derivative that lets a *rising* worry trajectory intrude on quiet where a steady-high one does not (`dmn.py:2050-2062`). None of this is per-switch LIF; all of it is accumulate/decay/velocity at the chemistry or loop grain.

### 2.5 Summary table — the pattern is everywhere except the switch

| Grain | Accumulate | Leak/decay | Threshold-fire | Reset/refractory | Reaches switches via |
|---|---|---|---|---|---|
| **Chemistry channel** (`Neuromodulators`) | `add` | `decay` toward baseline, wall-clock | — (continuous) | homeostatic relaxation | **gain control** (`effective_threshold`) |
| **Topic** (concentration) | `_accumulate` | half-life `_decay_to` | `quorum`/ignition | armed/quiet + disarm dwell | GWT broadcast → cluster gates |
| **Cluster** (recruitment) | `recruit` | half-life decay | threshold lowering | `satisfy` drain | RECRUIT channel in chem snapshot |
| **Individual switch** | — | — | `should_fire` (instantaneous) | — | itself |

The blank bottom row is the entire question. Every column is already filled one or more grains up. **Per-switch LIF proposes to fill the bottom row with mechanisms whose behavior the upper rows already produce.**

---

## 3. Hard question 1 — what is the time substrate?

Any "accumulate input over time" needs a clock. The candidates and their verdicts:

- **Over conversational turns.** The natural fit — but this is *already the chemistry clock*. `decay(turns)` (`bus.py:203`) integrates and leaks per turn, weighted by real elapsed time. A per-switch potential on the turn clock would be a second, redundant per-switch copy of what chemistry already integrates and already feeds the switch through gain control. Redundant.

- **Over individual bus messages within a turn.** The natural fit for sub-turn summation — but this is *already the concentration clock*. `_accumulate` (`bus.py:620`) sums per-message magnitude and `quorum` fires within a turn. A per-switch potential on the message clock would duplicate the concentration layer at a finer, context-poorer grain. Redundant, and the task explicitly warns against reinventing the concentration layer.

- **Over DMN idle ticks.** Only meaningful for the handful of idle-loop gates, which already have their own ACh accumulate/decay and velocity-aware drive (§2.4). No general substrate here.

- **Over wall-clock with decay.** Also already chemistry's substrate (`turns` = real elapsed / reference interval). Nothing new.

**Conclusion.** The system has *already chosen* its accumulate-over-time substrate, twice: **wall-clock-weighted turns for cross-turn summation (chemistry)** and **bus messages for within-turn summation (concentration)**. A per-switch membrane potential must ride one of those two clocks, and on either clock it duplicates an existing mechanism. There is no third, unclaimed clock at which the individual switch is the natural integrator. This is, on its own, close to dispositive: the redundancy is not incidental, it is structural — the switch sits *downstream* of both integrators via gain control, so integrating again at the switch is integrating the integral.

(If different switches "wanted different substrates," that argument would push toward *more* per-switch state on *multiple* clocks — strictly worse on every cost axis in §5 for the same absent behavioral payoff.)

---

## 4. Hard question 2 — what would it add, and is it worth it?

The stateless-comparator + gain-control model is deliberate and, per `SYSTEMS.md` §2.1 / Part II, "strong enough to carry the point." So LIF must earn its place with behavior the current system genuinely can't produce. Evaluate the three candidate values honestly:

### 4.1 Temporal summation — a series of sub-threshold nudges eventually fires

**Can the current system already do this? Yes, two ways.** Chemistry accumulates the mood that lowers thresholds (§2.1); the concentration layer accumulates topic salience until quorum/ignition wakes the deliberate path — the "slow threat no single turn tripped" (§2.2, `SYSTEMS.md` §2.8). A per-switch version would be a third copy. The one nuance a per-switch potential could add is *input-specific* summation (this exact switch's own inputs, not a shared channel), but the system deliberately routes salience through shared channels and topics precisely so that summation carries context and is visible in telemetry. A private per-switch potential would be less inspectable, not more capable. **No net new behavior.**

### 4.2 Refractory periods — a cluster can't re-fire too rapidly

**Can the current system already do this? For the units where firing costs anything, yes.** Refractoriness matters when re-firing is expensive or has outward consequences. Those are the integrator cells and motor actions, and they are already rate-limited where it counts: `IntegratorCell.max_calls_per_turn` (`cell.py:30`), motor cost/approval/budget gates, DMN cadence and backoff, the trading stream's own cooldown. Cheap deterministic switches are *designed* to be free to re-evaluate every turn — a refractory period on them would add state and latency to throttle something that costs nothing and whose re-evaluation is the point. The armed/quiet/disarm machine (§2.2) already provides refractory-flavored damping at the topic grain where it's meaningful. **No worthwhile new behavior at the switch; real value already captured upstream.**

### 4.3 Hysteresis / evidence accumulation before committing

**This is the only candidate with a genuinely distinct shape** — a gate that requires several turns of consistent evidence before flipping and resists flipping back (a Schmitt trigger), so a decision doesn't chatter on a noisy boundary. But:

- The concentration layer's `UNARMED → ARMED → QUIET` machine *is* hysteresis at the topic grain (`bus.py:594`): arm high, release low, disarm only after a dwell.
- Multiple Drafts' "commit on quiet" (`SYSTEMS.md` §2.4) is evidence-settling at the draft grain.
- The one place a *switch-local* version would help is the `_satiation_inhibitor` gap in §1.2 — habituation that should relax with idle time. That is a one-instance fix, and the honest fix is smaller than LIF: either call `tick()` on the idle path, or give that one accumulator a proper leak. It does not require a general per-switch membrane-potential framework.

So even the strongest candidate resolves to "already covered at a coarser grain, plus one small local fix" — not "build LIF everywhere."

### 4.4 The honesty ledger

There is a cost that isn't complexity: **a half-built per-switch integrator invites the exact claim the project forbids.** `SYSTEMS.md` Part II is explicit — "Never claim spiking neurons. Gain control above is the real claim and it is strong enough to carry the point." Appendix A row: "Spiking neurons — Not implemented. Stateless comparators. Never claim it." A mechanism that is 60% of a LIF, used in three places, would let a reader (or a future deck) say "spiking neurons" while the substance stays gain control. Under the house rule "claim the verdict, not the name," that is a liability, not a feature. LIF has to clear a **higher** bar here than a neutral feature would: it must earn a *distinct, defensible verdict* of its own, not just resemble the name.

---

## 5. Design options

Three options, minimal to ambitious. Each assessed on: what it enables, cost/complexity, new state + persistence/multi-tenant, testing, performance, and interaction with gain control / Hebbian efficacy / the concentration layer.

### Option A — minimal: give `StatefulSwitch` a real fire-reset-refract path, use it in 1–2 high-value places

Add an `integrate_and_fire(input, snapshot)` method: `update(input)`; if `_state ≥ effective_threshold(snapshot)` **and** now − `_last_fired` ≥ refractory, then `fire()`, reset `_state = 0`, stamp `_last_fired`; else return None. Keep `tick()` on a real clock so the potential leaks. Wire it into the `_satiation_inhibitor` habituation path (giving it the idle-time leak it lacks) and at most one other gate with a demonstrated evidence-accumulation need.

- **Enables:** genuine per-switch evidence accumulation with reset and refractoriness, at the one or two sites that can show a need. Fixes the §1.2 satiation-never-relaxes gap as a side effect.
- **Cost/complexity:** low–moderate. Reuses existing fields (`_state`, `_last_fired`, `decay`). The hard part is not the code; it's picking a clock (§3) and proving the two sites actually need it rather than a scalar.
- **New state / persistence / multi-tenant:** `_state` becomes behaviorally load-bearing, so it must be snapshotted/restored per-(persona, end-user) alongside chemistry, and must respect the one-way valve (`Bus.is_bound`, `bus.py:500`) so a client's transient potential never leaks into the persona's durable state. Small surface if confined to 1–2 switches; a trap if it spreads.
- **Testing:** unit tests for accumulate/threshold/reset/refractory; a neutral-when-off proof (flag off ⇒ byte-identical to today's comparator); a behavioral test that the satiation gap is closed (habituation relaxes over idle time).
- **Performance:** negligible at 1–2 sites.
- **Interactions:** `effective_threshold` still applies (gain control modulates the fire bar — coherent). Hebbian `efficacy` still divides the threshold (coherent). Must **not** shadow the concentration layer — restrict to switches whose evidence is genuinely local and not already a tracked topic, else it double-counts.

### Option B — ambitious: a general per-switch membrane potential (LIF) across the switch layer

Every switch (or a broad subclass) gets a membrane potential that integrates its inputs over a defined window with LIF decay, fires on threshold, resets, and refracts.

- **Enables:** in principle, uniform spiking dynamics. In practice, no behavior §2 doesn't already produce, because the switch sits downstream of chemistry and topics via gain control (§3).
- **Cost/complexity:** high. Touches ~all switch call sites (the `should_fire` sites in `frontal`, `hippocampus`, `motor_cortex`, `temporal`, `occipital`, `metacognition`, `dmn`), each of which must now decide what "input over the window" means for that gate and on which clock.
- **New state / persistence / multi-tenant:** a per-switch potential × hundreds of switches × every (persona, end-user) pair. This is the killer: the state surface that today is a handful of chemistry channels per tenant becomes hundreds of membrane potentials per tenant, all needing snapshot/restore/decay/valve discipline. It directly multiplies the most expensive dimension of the hosted system.
- **Testing:** very large. Neutral-when-off must be proven across every migrated site; behavioral equivalence is hard to argue when instantaneous comparison becomes windowed integration everywhere.
- **Performance:** per-turn decay/update across the whole switch population; modest per-op but broad, and the persistence I/O is the real cost.
- **Interactions:** maximal collision risk with the concentration layer (integrating the same bus traffic twice, once per-switch and once per-topic) and with chemistry (integrating the integral). High risk of the honesty problem in §4.4 at full scale.
- **Verdict:** not justified. Cost is systemic; behavioral payoff over §2 is ~zero.

### Option C — do nothing new; document honestly and clean up `StatefulSwitch`

Keep the stateless-comparator + gain-control model. Do not add per-switch LIF. Instead:

1. **Document the reconciliation** (§2) so the owner's "we have sum-over-time firing" intuition maps to the real mechanisms (chemistry + concentration/GWT), in `SYSTEMS.md` and/or this RFC's summary. Keep the Part II verdict "Integrate-and-fire → Not implemented" exactly as is.
2. **Clean up `StatefulSwitch`** (the §1.2 liability), one of:
   - **C-lite:** delete the four dead instances (`_valence/_threat/_novelty/_arousal_switch`, `hypothalamus.py:40-51`); collapse the two hippocampus comparator-only instances back to `SwitchNeuron`; leave `_satiation_inhibitor` as the sole `StatefulSwitch` and either fix its dead leak (call `tick()` on the idle path) or drop the unused `decay` field and rename the class to what it is (a bounded scalar accumulator), so nothing named "Stateful"/decaying implies LIF that isn't there.
   - **C-min:** if churn is unwanted, at minimum add a docstring stating plainly that `StatefulSwitch` is a scalar accumulator, its `tick()` is unused in prod, and it is **not** integrate-and-fire — so the next reader isn't misled.
- **Enables:** the intuition lands on real code; the one artifact that fakes LIF stops faking it; zero behavioral change (except the optional satiation-leak fix, which is a genuine small improvement).
- **Cost/complexity:** trivial (docs + dead-code removal).
- **New state / persistence / multi-tenant:** none (C removes state; doesn't add it).
- **Testing:** the existing suite covers the comparator paths; the satiation-leak fix gets one behavioral test; removing dead instances is covered by "still imports, still passes."
- **Performance:** neutral to slightly better (fewer dead objects).
- **Interactions:** none new. Preserves gain control, Hebbian efficacy, and the concentration layer untouched.

---

## 6. Recommendation

**Adopt Option C.** Do not build per-switch integrate-and-fire. Specifically:

1. **Keep the model as-is.** Stateless comparators + neuromodulatory gain control remain the switch story. It is the strongest neuroscience claim the project has and it is deliberate.
2. **Write the reconciliation down** (this RFC's §2, and a one-line pointer from `SYSTEMS.md` §2.1) so the accurate answer to "don't we already sum over time and fire?" is *yes — in the chemistry channels and the concentration/GWT layer, not in the individual switch.* That is the honest home for the owner's intuition.
3. **Clean up `StatefulSwitch`** (C-lite preferred): remove the four dead instances, collapse the two comparator-only hippocampus uses to `SwitchNeuron`, and either fix or retire the dead `tick()` leak on the lone real accumulator. Optionally fix the satiation-relaxes-only-on-salient-turns gap (§1.2) by giving that one accumulator a real idle-time leak — the single concrete behavioral win available here, and it needs no LIF framework.
4. **Build the narrow carve-out** — done, under the functionalist lens: a bounded evidence accumulator (drift-diffusion) with the learning path wired in, flag-gated and seeded at satiation. This is the *opposite* of per-switch LIF; it fills the one uncovered gap without touching the switch layer. See §7.

**Why this is the right call, stated plainly:**

- **The value is already delivered, at better grains.** Every behavior LIF would buy — slow-building signals that fire when no turn did, evidence accumulation, hysteresis, rate limiting — exists in chemistry, in the concentration/quorum/GWT layer (made load-bearing this week), in recruitment, and in integrator-cell/motor rate limits. Per-switch LIF is the integral of an integral (§3).
- **The cost is systemic and the payoff is not.** At any grain broad enough to be "spiking neurons," LIF multiplies persistent per-(persona, end-user) state across the switch population — the single most expensive dimension of the hosted architecture — to reproduce behavior we already have.
- **It protects the thing that makes the project credible.** The house rule is "claim the verdict, not the name." A half-real per-switch integrator would let someone say "spiking neurons" while the substance stayed gain control — the precise failure `SYSTEMS.md` Part II and Appendix A legislate against. The disabled-feature entries (colony threshold jitter; Active Inference relabeled) are the project's most valuable honesty assets *because* nobody fakes the mechanism. Building a decorative LIF would spend that credibility.

**What the owner's intuition was actually reaching for:** the concentration layer and the chemistry channels. Those are genuinely "accumulate over time, leak, cross threshold, fire" — the concentration layer even has the hysteresis and the fast-rise trigger. The intuition is *correct about the system*; it was just attached to the wrong component (the switch) instead of the two components that actually do it. Naming that correctly is more valuable than building a third copy.

**On `StatefulSwitch` specifically:** it should be documented for what it is and mostly removed. It is not a seed to grow LIF from — it is a class whose name promises state, whose leak is dead, whose accumulator never fires anything, and four of whose seven instances are unused. Left as-is it is the single most likely reason a future reader concludes the system has spiking neurons. Cleaning it up is the highest-leverage honesty fix in this whole area.

---

## 7. What was built (functionalist lens)

The owner confirmed functionalism as the goal and asked to build the gap-filler. What shipped to the branch is **not** per-switch LIF — Option B stays rejected. It is the one thing §4 found genuinely uncovered: a **bounded evidence accumulator** for private per-decision inferences, framed and named as what it functionally is (drift-diffusion / sequential-sampling decision-making), with the learning path wired in through machinery that already exists.

### 7.1 The primitive — `brain/evidence_gate.py`

`EvidenceGate`: a leaky evidence accumulator with a hysteresis band.

- **Accumulate + leak + commit.** `observe(evidence, snapshot, now)` adds a signed drift, applies a wall-clock **half-life leak**, and commits when the level crosses a chemistry-modulated bound. Hysteresis (`arm_threshold` high, `release_ratio·arm` low) makes a commitment resist chattering. Two modes: `latch` (a held belief) and `fire_reset` (one-shot + refractory). Also usable as a pure leaky scalar (`.peek()`), which is the habituation/satiation shape.
- **Gain control reused.** The commit bound runs through an internal `SwitchNeuron.effective_threshold`, so circulating chemistry moves the evidence bar exactly as it moves a switch's — "feeling becomes behavior" extended to the decision-accumulation layer. (Its clamp is widened to the accumulator scale `[0.01, cap]`, since switch clamps assume `[0,1]` inputs.)
- **Commit records on the firing path.** An arm edge fires the internal switch, putting `<cluster>.<gate>` on the turn's `fired_path`, so the gate is a wiring-graph node and the **existing session Hebbian pass credits its downstream edge** by the turn outcome — commit-bound (`efficacy`) and downstream-influence (`weight`) learning, for free, no new learning engine.

### 7.2 The learning path — an armed gate is a checkable prediction

`resolve(correct, informativeness, bus, external=…)` grades a committed inference the world later confirmed/refuted and learns the **drift-cue weights**:

- Routes through the existing `neuron.prediction_reward` (confidence floor + informativeness gate + λ-scaled loss on a confident-wrong call), so trivial/uninformative predictions can't farm reward.
- Emits the DA delta via `bus.neuromod.add(..., source=…)`, so it flows through the **audit chokepoint** (`_log_reward_emission`) and shows up in the intrinsic/external DA tally — it cannot hide.
- **Plasticity is weighted toward external/grounded confirmation** (`evidence_external_weight` = 1.0) and discounts self/critic-graded confirmation (`evidence_self_weight` = 0.35), so a gate cannot learn to fire on cues its own appraiser likes — the exact self-grading loop the premise audit flagged. Cues present at commit are captured and credited/penalised on resolve; weights are clamped and per-persona durable.

### 7.3 State lifecycles (both correct, and tested)

- **Transient level** rides the `ChemPair` (`bus.py`, new `evidence` field + `Bus.evidence` resolver), so per-(persona, end-user) isolation, binding, snapshot/restore and the one-way valve are all inherited. Gate objects stay process-global cluster singletons; the per-client state lives in the bound pair's `evidence` dict, loaded/saved around each call via `store=`. (This also *fixes* a latent multi-tenant bug: today's `StatefulSwitch` satiation state is process-global — shared across clients.)
- **Durable learned cue weights** are per-persona (belong with the wiring/efficacy learning that persists per personality).

### 7.4 The live call site — satiation, flag-gated

`hypothalamus._satiation_inhibitor` is reframed behind `evidence_gates`:

- **Flag off (default):** the `StatefulSwitch` path, byte-identical to before (proven by test).
- **Flag on:** an `EvidenceGate` scalar level with a real ~30-min half-life leak (`satiation_half_life_s`) and per-client state — which **fixes the §1.2 dead-`tick()` gap**: habituation now relaxes over idle time instead of only when a salient turn decrements it.

### 7.5 Validation

`tests/test_evidence_gate.py` (19 tests, all green; full suite 2241 passed / 1 skipped, no regressions): temporal summation, half-life leak, a slow signal that never reaches bound, hysteresis hold + no-chatter, fire-once edge, fire_reset + refractory, chemistry-raised bound, scalar mode, **confirmed-external commit strengthens its cues / refuted weakens**, **anti-farm uninformative resolve moves nothing and pays 0 DA**, **external > self plasticity**, **DA-tally provenance audit**, per-client store isolation, snapshot/restore, ChemPair persistence, and satiation flag-off-identical / flag-on-relaxes.

### 7.6 The user-avoidance gate — the first learning gate (`brain/avoidance_gate.py`)

Built and wired. `AvoidanceTracker` accumulates per-entity "the user is avoiding X" evidence from cues parietal already tracks (`not_reengaged`, `topic_shifted`, `discomfort`), commits with hysteresis, and **learns its cue weights from the user's own behaviour**: if the user later re-engages an entity we flagged as avoided, the belief was a false alarm — `resolve(correct=False, external=True)` runs the shared learner (prediction_reward → audited DA → weaken the guilty cues). v1 learns from refutations (conservative, self-correcting; it is punished for crying wolf, never self-rewards); balanced positive confirmation via an active probe is a later increment. Wired live in metacognition, driven per turn from `session_turn` off parietal's entity map.

**Both flags ON, fully consumed** (the owner's call — deferred wires rot, so nothing was left dangling):
- `evidence_gates=1`: the substrate. Satiation idle-leak live; the avoidance tracker runs per turn.
- `avoidance_gate=1`: avoidance is live end to end. It learns per-persona cue weights from the user's behaviour, moves audited external-grader DA on resolve, and the **DMN speak/deflect judge consumes it** — a candidate that would push an avoided topic is flagged (`candidate_pushes_avoided_topic`) and the judge is told to prefer wait/drop. Auditable via the decisions log (`avoidance_armed`/`avoidance_confirmed`/`avoidance_refuted`).

**Correctness the live wiring forced (all done):**
- **Per-client state.** All transient per-entity state (level, armed, cues-at-arm, agent-surfaced flag) lives in the bound `ChemPair.evidence` store, keyed `avoid:<e>` / `avoidmeta:<e>` — no process-global belief state, so two clients never collide.
- **Per-persona learning, persisted.** Cue weights are kept per persona slug and saved to `persona_state_root(persona)/avoidance_cues.json`, so learning survives a restart and never bleeds across personas (tested by reloading into a fresh tracker).
- **Balanced learning, no probe.** Positive confirmation comes from a natural occurrence — the agent's own reply surfaced a flagged topic and the user still dodged it — so the detector is rewarded for correct calls, not only punished for false alarms, without any risky agent-initiated probing.

### 7.7 Honest scope — what is live vs. pending

- **Live now (both flags on):** satiation idle-leak; avoidance accumulation + per-persona learning (refute on re-engagement, confirm on agent-surfaced-and-still-dodged) + audited DA + DMN speak-judge steering.
- **Deliberately conservative, not a gap:** the steer is a *bias* fed to the LLM judge (which already weighs several signals), not a hard gate, so a noisy detector nudges rather than blocks; and the substring surface-match has a length guard.
- **Not built (correctly):** an *active* surface-and-watch probe (the natural-occurrence confirmation covers the common case without one), and per-switch LIF (rejected). Cue-weight saves are per-learn disk writes — fine at this rate; batch to consolidation if it ever gets hot.

### 7.8 Naming discipline

Described everywhere by verdict — "bounded evidence accumulation with reward-modulated drift learning" (drift-diffusion; three-factor plasticity). Never "spiking neurons," never "STDP" (there is no spike timing). `SYSTEMS.md` Part II "Integrate-and-fire → Not implemented / Never claim it" stays exactly as written; this is a different, narrower, defensible claim.

---

## 8. Follow-ups

Done this session (all on branch, full suite 2251 passed / 1 skipped):
- ✅ **Ship-on:** `evidence_gates` flipped to 1 after proving the whole suite green with the flag on. Satiation idle-leak live; avoidance learning live in shadow.
- ✅ **First learning gate:** the user-avoidance tracker built, wired live (shadow), and validated end-to-end — accumulate → arm → external refutation weakens cues → confirmation strengthens, plus a farming test that a self-graded signal is clamped and lands only in the intrinsic (never external) tally.
- ✅ **`StatefulSwitch` C-lite cleanup:** the four dead instances removed, the two comparator-only hippocampus uses collapsed to `SwitchNeuron`, and the class documented for what it is (`brain/neuron.py`).

Also done this session (turned fully on, nothing deferred):
- ✅ `avoidance_gate=1`, DMN speak/deflect judge consumes `deflection_bias()`/`avoided_entities()`, cue weights persisted per-persona, natural-occurrence positive confirmation. Full suite 2250 passed / 1 skipped with both flags on.

Still open:
- **Proposed `SYSTEMS.md` edits:** add a §2.1 pointer to the evidence-gate mechanism and the Part II entry in §8.1 below. Leave the integrate-and-fire verdict unchanged.
- **Watch in a tenant:** the decisions log (`avoidance_armed`/`avoidance_confirmed`/`avoidance_refuted`) and the intrinsic:external DA tally after go-live, to confirm the detector calibrates and does not worsen the self-graded share.
- **Branch/deploy:** built on `claude/dreamy-knuth-8e0b42`, **not pushed**. `main` auto-deploys; merge/deploy stays the owner's call. On merge, `evidence_gates=1` + `avoidance_gate=1` go live: satiation idle-leak, and avoidance learning + a conservative deflection bias in the idle-speak judge.

### 8.1 Proposed Part II entry (for `SYSTEMS.md`, when the learning gate goes live)

> ### Bounded evidence accumulation (drift-diffusion / sequential sampling; Ratcliff · Gold & Shadlen) → §2.1
> **The claim:** Some decisions are made not on one observation but by accumulating noisy evidence to a bound, then committing; the bound height is caution, tuned by experience.
> **What we built:** `EvidenceGate` — a leaky evidence accumulator with a hysteresis band, for private per-decision inferences the topic concentration layer doesn't hold. The commit bound is chemistry-modulated (gain control); an armed gate is a checkable prediction whose confirmation trains the drift-cue weights through the existing three-factor + `prediction_reward` + eligibility machinery, weighted toward external confirmation so it can't self-grade.
> **Verdict: Partial, flag-gated.** The accumulate/leak/hysteresis substrate is live at one gate (habituation); the drift-cue learning loop is built and unit-tested but awaits a live gate with a ground-truth signal. **Not** integrate-and-fire and **not** STDP — a distinct, narrower claim. Say "evidence accumulation," never "spiking."
