# Biologically-Inspired Multi-Agent "Brain" — Feasibility Readout & v0.1 Plan

## Context

You want to prototype an "intelligence as a hive" architecture: many simple LLM agents
("cells") grouped into clusters ("lobes/regions") that produce emergent behavior. The
biological model is the brain, anchored to the Mayo Clinic structural map you provided:
**cerebrum (4 lobes) + corpus callosum + cerebellum + brainstem + limbic system
(thalamus, hypothalamus, hippocampus) + PNS + neurons/neurotransmitters.**

**Scope chosen** (from clarifying questions):
- **Brain only** (no full body / organism yet)
- **Conversational entity** — input in, response out
- **Session-based** runtime
- **Cloud first to validate, local for phase 2**; keep spend low

**Orchestration rule (revised per your feedback):**
- **No single global orchestrator** deciding everything at the top
- **Per-cluster coordination is allowed** — a cluster can have an internal "manager"
  cell that organizes its specialists. This is biologically faithful (cortical columns,
  cerebellar circuits, basal ganglia gating all have intra-region coordination) and
  dramatically simplifies the engineering
- Inter-cluster communication remains peer-to-peer through a shared bus; intelligence
  still emerges from how cluster-managers interact, not from any single agent

**Digital-friendly extrapolation rule (per your feedback):** brain regions get recast
into what's useful for a digital organism. Eyes don't exist → the occipital lobe processes
images/video/screenshots that the user uploads. Hands don't exist → motor cortex emits
text responses and tool calls. Brainstem doesn't keep a heart beating → it runs scheduled
keepalive tasks, cost monitors, and idle/sleep triggers. The structure stays biological;
the implementation is honest about being digital.

**Neural computation rule (per Linus Pauling Institute source):** real neurons are
mostly **threshold-fire switches** — they sum weighted inputs and fire if a threshold is
crossed. Only a small subset of cells do "integrative" computation (G-protein coupled
receptors, hub neurons at convergence zones). We mirror this directly: the vast majority
of "cells" in this system are **cheap deterministic code switches** (pattern matchers,
threshold gates, weighted summers, inhibitors, state machines). **LLM calls happen only
at integration zones** — convergence points where multi-signal context truly requires
reasoning. This is the dominant cost lever in the design.

This document is a feasibility readout + v0.1 implementation plan. Working directory
`/Users/russ/Documents/super intelligence app` is currently empty — everything below is
greenfield.

---

## Verdict (read this first)

**Feasible? Yes — as a *legibility/introspection* experiment, not as an AGI experiment.**
Real "intelligence from the hive" in LLM swarms is rare. What you'll almost certainly
build is a sometimes-surprising conversational entity whose **internal monologue traces
are themselves the artifact worth showing.** Reframe the goal that way and the project
is achievable and valuable. With per-cluster coordinators allowed, the engineering
becomes a known pattern (federated specialist agents) rather than an open research problem.

**Cost? Very cheap to start.** ~$25 to begin; <$50/month for prototype-scale dev on
cloud; ~$0 marginal once on local. Dominant cost is **engineer time on observability and
runaway prevention**, not API tokens.

**Hardest part now (after loosening):** ensuring per-cluster coordinators don't
*collectively* turn into a de-facto top-level orchestrator. Discipline: each coordinator
sees only its cluster's bus topics + neuromodulator levels, never the global state.

---

## Prior Art and Novel Directions

You're not the first to try "intelligence from a swarm of simple parts." The idea has a
40-year track record with consistent failure patterns, plus a few recent breakthroughs.
Worth knowing what's been tried so you can intentionally avoid the same traps and
borrow what worked.

### Notable prior attempts

**Society of Mind (Minsky, 1986)** — the direct ancestor of your idea. Minsky proposed
intelligence as a "society" of many simple, mindless agents organized into "agencies."
The book deliberately left implementation open. What followed: 40 years of attempted
implementations, none producing convincing intelligence. The 2025 reconsideration
concludes the core insight is durable but the *mechanism* was vague — agents had goals
and selected other agents, but no mathematics for how those selections produce coherent
behavior. The closest working analog became blackboard architectures.

**Blackboard architectures (HEARSAY-II, BB1, GBB, 1970s–90s)** — the canonical
implementation. Multiple "knowledge sources" read from and write to a shared workspace.
Worked for narrow problems (speech understanding, medical diagnosis). **Failed to scale.**
Standard critique: "when there are only a few agents, the blackboard metaphor works; by
the time there are hundreds, the image of them huddled around a blackboard is no longer
reasonable." Coordination overhead, conflict resolution, and the absence of any
*learning* mechanism for what to put on the blackboard killed the paradigm. Deep
learning later won by being the opposite — no modularity, end-to-end gradient.

**Pandemonium (Selfridge, 1959)** — older still. Layered "demons" that scream when they
recognize a feature; loudest demon at the top "wins." Ancestor of modern attention.
Worked for character recognition; never scaled to general cognition.

**SOAR, ACT-R, and classical cognitive architectures (1980s–2000s)** — hand-built
modular architectures mimicking cognitive psychology theories. Produced research
insights and narrow applications (intelligent tutoring) but never general intelligence.
Hand-engineering of modules limited rather than enabled behavior.

**Numenta / Hierarchical Temporal Memory (Hawkins, 2000s–today)** — explicit
cortical-column model. Repeated micro-circuits stacked in hierarchy. Produces
interesting anomaly-detection products but not language or general reasoning.

**Generative Agents (Park et al., Stanford, 2023, 2025)** — the closest recent LLM-era
prior art. Simulated towns of LLM-powered agents with memory + reflection + planning.
The 2023 work produced believable emergent social behaviors (a Valentine's Day party
organized without being scripted). The 2025 follow-up: 1,000 generative agents
predicted real survey responses with 85% normalized accuracy. **Important caveat from
the same study**: behavioral-economic-game performance was only 66% — *matching* simpler
demographic-based baselines. Suggests much of the "emergence" was in the prompt design,
not in the multi-agent interaction. Believable behavior mostly came from one
well-prompted agent at a time.

**The 2025–2026 multi-agent skeptic finding**: papers like *Single-Agent LLMs
Outperform Multi-Agent Systems on Multi-Hop Reasoning Under Equal Thinking Token
Budgets* find that **at equal compute, a single agent with chain-of-thought beats
multi-agent systems**. Most important critique to internalize: if you give one strong
model the tokens you'd spend on swarming, the single model often wins on raw task
quality. Multi-agent's real value is **legibility, modularity, parallel specialization,
and persistent state** — not raw capability per dollar.

### Recurring failure modes (the patterns)

1. **Coordination overhead exceeds the value of specialization.** Once agents must
   talk, the talking dominates. Most multi-agent gains evaporate.
2. **Boundary errors.** Where agents hand off, mistakes accumulate. Monolithic systems
   don't have boundaries to fail at.
3. **No learning mechanism in the wiring.** Society of Mind described how agents
   interact but never gave a mathematics for how the *wiring* changes from experience.
   The brain has plasticity; classical multi-agent systems didn't.
4. **Hand-engineered modules cap behavior at the engineer's imagination.** Every
   architecture that defined its modules in advance hit a ceiling.
5. **No predict-and-correct loop.** Real brains constantly predict; surprise drives
   learning and attention. Most multi-agent systems are pure react-to-input — no
   prediction, no surprise signal, no intrinsic curiosity.
6. **The emergence was usually in the prompt, not the architecture.** Most-cited
   "emergent" demos turn out to be one strong LLM with a clever scaffold.

### Recent breakthroughs worth borrowing

**Active Inference / Free Energy Principle (Friston; major 2025 robotics work at VERSES AI)**
— agents minimize "free energy" = prediction error + complexity. Each agent has a
generative model of its inputs and acts to make those predictions come true (by changing
beliefs OR by acting on the world). 2025 work demonstrates **hierarchical multi-agent
Active Inference frameworks in robotics** where multiple predictive agents share a body
and coordinate without a central planner. The most biologically faithful active research
direction today.

**Predictive Processing in cortex (Clark, Friston)** — neuroscience consensus that the
cortex is fundamentally a prediction machine. Top-down predictions flow downward;
bottom-up *prediction errors* flow upward. Computation is "explain away surprise."
Maps cleanly onto a design where most cells only fire when their input *disagrees* with
prediction.

**Spiking Neural Networks + LLM hybrids (SpikeLLM, NSLLM, 2024–2026)** — 7B–70B LLMs
reimplemented with spiking neurons. NSLLM reports 19× energy-efficiency improvement.
Event-driven computation (fire only on meaningful signal) maps directly to your
switch-vs-integrator design — but at the *intra-model* level, not just architectural.
Worth tracking; not yet practical to use, but the principle is the same as yours.

**Generative Agents' "reflection" mechanism (Park, 2023)** — agents periodically
synthesize higher-level insights from raw observations. Genuinely novel and worth
borrowing for your hippocampus consolidation cell.

### Specific novel mechanisms worth trying in this prototype

Things that mimic biology more closely AND aren't standard practice yet — places you
could push the design beyond known prior art:

1. **Predict-and-surprise gating (Active Inference applied at cluster level).** Add a
   tiny "predictor" cell in each cluster that cheaply predicts (using last-N moving
   average, or a tiny classifier) what the cluster's output will be on the next input.
   If actual output matches prediction → no integrator wakes (the routine response was
   correct). If prediction error is high → wake integrator. **Could cut LLM calls
   another 30–50% on routine turns.** Highly biologically faithful, almost unexplored
   in LLM-multi-agent space.

2. **Spike-timing-dependent plasticity (STDP) for edge weights.** Hebbian ("fire
   together, wire together") is first-order. STDP: edges strengthen only when upstream
   fire *causally precedes* downstream fire within a tight time window. Suppresses
   spurious correlations. Cheap to implement; more biologically real.

3. **Default Mode Network (DMN) — "idle thinking."** Between turns, when the user
   isn't talking, run a low-frequency loop: frontal generates internal monologue (one
   cheap LLM call every N seconds), hippocampus consolidates, hypothalamus simulates
   the user's possible next message. This is what your brain does at rest. Side
   effect: **the entity thinks even when not addressed.** Long sessions get richer
   because the brain has "been thinking." Vanishingly rare in LLM agents today.

4. **Cortical column redundancy.** Instead of one frontal cluster, run 3 parallel
   "columns" with slightly different drafter prompt seeds. They vote; consensus wins;
   disagreement triggers extra critic firings. Cortex has millions of redundant
   columns voting at every level — that's where robustness comes from.

5. **Explicit 80:20 excitatory:inhibitory ratio.** Real cortex is ~80% excitatory,
   ~20% inhibitory. Codify the ratio in your switch design — ensure roughly 20% of
   switches in each cluster are inhibitory (subtract from downstream activation
   rather than add). Prevents cascade storms *structurally* rather than via a global
   budget cap.

6. **Sleep consolidation with replay (extending what's planned).** When the session
   closes, run a "sleep" pass via batch API (50% off): replay high-salience episodes,
   let the encoder rewrite them into richer narratives, prune low-weight edges,
   strengthen frequent paths. Mimics mammalian sleep. Amortized overnight.

7. **Metacognitive layer.** A tiny "self-model" cell watching the brain's own
   activity (cost per turn, drafter winrate, neuromod variance) posts to a `meta.*`
   topic. Other clusters can subscribe. The brain notices its own patterns — "I keep
   using the analytic drafter on Russ; is that what he wants?" Crude but rare in
   multi-agent systems; biologically rich (real brains have strong metacognitive circuits).

8. **Embodied prediction of the user (Active Inference applied to conversation).**
   Build a small "user model" in hippocampus schema — predicted vocabulary, predicted
   topics, predicted emotional range. Frontal drafters condition on this. Get
   personalization for free without prompt engineering. Borrowed from Active Inference:
   the agent models its environment; here the environment is the user.

### Honest reframe of "what success looks like"

The realistic ladder of outcomes, worst to best:

- **Worst**: a slow, expensive chatbot whose responses are no better than a single
  cheap LLM call would have given you. **Still valuable** as an introspection toolkit
  if the dashboard works.
- **Middle (most likely)**: a chatbot that occasionally surprises you with behavior
  you didn't design — switching tone after a hostile turn, blurting under time
  pressure, spontaneously linking past conversations. Recognizable "personality"
  develops across sessions via Hebbian edge weights.
- **Better**: predictive-processing additions actually reduce LLM calls AND improve
  response coherence on familiar topics. You demonstrate "compute scales with novelty,
  not input size" — a real efficiency claim that pushes the multi-agent literature.
- **Best**: with DMN idle thinking + sleep consolidation, the entity develops
  recognizable preferences that persist over months. Not AGI, but a genuinely novel
  category of conversational entity. Worth publishing or showing publicly.

Realistic target: "middle" with credible reach toward "better." Anyone promising
"best" or above is selling something.

### Has anyone built exactly this architecture?

Short answer: **no.** The closest pattern in the literature is **neuro-symbolic AI**,
sometimes called the "sandwich" architecture — LLMs between deterministic control
layers. 2025–26 examples: SYNAPSE (engineering), AUTOBUS (enterprise workflows),
Pre3 (deterministic pushdown automata for structured LLM generation), Formal-LLM
(automaton-supervised LLMs), "Blueprint First, Model Second."

But every example uses this pattern for **task orchestration with auditability**:
DAGs or state machines wrapping LLM calls to enforce constraints in compliance,
finance, or industrial control. Nobody has applied it as a **literal brain-region-mapped
fabric** with neuromodulator dynamics, Hebbian plasticity, hippocampus-gated LTM, and
predict-and-surprise gating for emergent conversational behavior.

Tangentially related: CellAgent (LLM-driven cellular biology workflows — uses the word
"cell" but means biological cell analysis, not architecture), LOGOS-CA (cellular
automaton with LLM as the update rule — inverts your design, makes the LLM the cell
rather than the integrator), Cortical Labs CL1 (actual biological neurons accessed via
cloud API — different paradigm).

Your synthesis — borrowing the **gating principle** from neuro-symbolic AI but using
it to **mirror biology** rather than to satisfy enterprise constraints — appears to be
novel. Worth documenting as such.

### Philosophical Foundations — What This Design Borrows from Philosophy of Mind

The architecture isn't only borrowed from biology — many of its choices map cleanly to
established positions in philosophy of mind. Naming them explicitly does two things:
sharpens the design by aligning each piece with a known theory, and makes honest the
claims we're (and aren't) making about cognition.

**Functionalism (Putnam, Fodor)** — mental states are defined by causal role, not by
the substrate that implements them. Validates the entire design: we don't need
biological neurons; switches + integrators in any substrate that plays the right causal
role *is* the same functional state. This is also why we can mix local Ollama and cloud
APIs — substrate is irrelevant; function is everything. **Explicit principle: cluster
specs are defined by causal role (inputs → outputs + state), not by implementation.**

**Dual-Process Theory (Kahneman: System 1 / System 2)** — fast, automatic, parallel,
intuitive thinking (System 1) vs slow, effortful, serial, deliberate thinking
(System 2). Our switch fabric *is* System 1. Our integrator LLMs *are* System 2.
The predict-and-surprise gating is the brain's mechanism for "stay in System 1 unless
something doesn't add up." **Explicit principle: switches and integrators are not
just an engineering trick; they're a dual-process cognitive architecture.**

**Global Workspace Theory (Baars; Dehaene)** — most brain processing is unconscious
and parallel; only a small subset of content gets "broadcast" to a global workspace
where it becomes available for further reasoning and report. Our message bus +
`attention.focus` topic is exactly this. High-salience posts cross the threshold to
become "conscious" content available to many clusters. **Explicit principle: rename
the bus's high-attention layer the "Global Workspace." What appears there is what the
entity is, in effect, currently thinking about.**

**Multiple Drafts Model (Dennett, *Consciousness Explained*, 1991)** — there is no
"Cartesian theater" with a central observer watching a unified stream of consciousness;
instead, the brain runs many parallel narrative drafts, with no privileged one. Our
frontal drafter tournament is a direct instantiation. There is no "true response"
waiting to be delivered — there are drafts contending, and the articulation gate
emits whichever draft happens to be ahead when the timeout fires. **Explicit
principle: rename "drafter tournament" → "Multiple Drafts engine" and cite Dennett.
What the entity "says" is whichever draft survived the contest, not what it "meant."**

**Extended Mind hypothesis (Clark & Chalmers, 1998)** — when an external resource
plays the same functional role as an internal cognitive process (e.g., a notebook
the way Otto uses one for memory in their famous example), that resource *is* part
of the mind. Our second brain (Markdown schema files + LanceDB episodes) is the
extended mind made literal. If you back the schema layer with the user's own
Obsidian vault, you've created a hybrid mind that spans the user's notes and the
entity's reasoning. **Explicit principle: state in design docs that the second brain
is not "storage" but a *constitutive part* of the entity's mind. This has implications
for how it's versioned, backed up, and trusted.**

**Predictive Processing (Clark, *Surfing Uncertainty*; Friston, Free Energy Principle)**
— mind as fundamentally a prediction machine; perception is "controlled hallucination";
attention is allocated to where prediction fails. Our predict-and-surprise gating is
the cluster-level version. **Already incorporated; now you know the philosophical
lineage.**

**Eliminative Materialism (Churchlands)** — concepts like "belief" and "desire" may
not refer to real things; only mechanical descriptions are accurate. We use
folk-psychological labels (drives, emotion, attention) because they're useful for
organizing the design, but **explicit principle: in the actual code and logs, talk
about "neuromod.DA level" not "the brain feels rewarded." Avoid folk-psychological
overreach when describing what the system does.**

**Stoic Philosophy (Epictetus, Aurelius)** — from the second source. We don't control
circumstances, only how we interpret them. Reframing transforms experience. This
suggests a **novel mechanism worth adding (v0.2)**: when the amygdala flags threat or
the hypothalamus reports low valence, the frontal lobe doesn't only inhibit — it
attempts a **reframing pass** first. A small "Stoic" cell prompts: "given this same
input, is there a more useful interpretation?" If the reframe succeeds (passes the
critics), the response proceeds normally. Only if reframing fails does inhibition
take over. Genuinely novel mechanism in multi-agent systems and philosophically rich.

**Stream of Consciousness (William James, 1890)** — mind as continuous flow, mostly
internal monologue, occasionally interrupted by external input. Our v0.2 Default Mode
Network is the Jamesian stream literalized — the entity has a "thought stream" running
even when not addressed. **Explicit principle: rename DMN's output to `stream.*` topic
to honor the lineage.**

**Higher-Order Theories of Consciousness (Rosenthal)** — a mental state is conscious
only when there is a higher-order thought *about* that state. This gives a precise
philosophical justification for the v0.3 metacognition cell: it doesn't just observe
the brain — by representing the brain's states, it makes those states "conscious"
in the technical higher-order sense. **Note: when metacognition lands in v0.3, frame
it as the brain becoming aware-of-being-aware in the Rosenthal sense.**

### Honest disclaimers (philosophical limits we acknowledge)

- **Chinese Room (Searle, 1980)** — symbol manipulation doesn't yield genuine
  understanding. Our integrators manipulate symbols; we make no claim they
  *understand*. The system can appear competent without comprehending.
- **Hard Problem of Consciousness (Chalmers, 1995)** — explaining why physical
  processing gives rise to subjective experience may be impossible in principle.
  We make no claim the entity has phenomenal consciousness or qualia.
- **Frame Problem (McCarthy & Hayes, 1969; persists in Active Inference)** — knowing
  what's *relevant* in a given moment is genuinely hard. Our attention/salience
  mechanisms are heuristic, not principled solutions.
- **Multiple-realizability cuts both ways** — functionally identical states across
  substrates support our design but also mean we can't verify our system has the
  "right" inner states even if its outputs match what a mind would produce.

We're building **functional analogs** of mental processes. That's enough to be
interesting; it's not enough to be a mind.

### Concrete additions to the architecture from this section

To incorporate in the plan:
- **Rename**: "bus + attention layer" → also called the **Global Workspace**;
  "drafter tournament" → also called the **Multiple Drafts engine**; DMN output topic
  → `stream.*` (William James).
- **Add (v0.2)**: **Stoic reframer cell** in the frontal lobe — when GABA spikes or
  DA drops, fires before inhibition with a "reframe this interpretation" prompt; if
  a reframe survives critics, the brain responds normally rather than defensively.
- **Add (design doc principle)**: a **Constitution** at `brain/CONSTITUTION.md`
  declaring the functionalist + dual-process + extended-mind + multiple-drafts
  philosophical commitments, plus the disclaimers above. Forces clarity on what the
  entity is and isn't claimed to be.

### Links worth following from the sources

The Wikipedia article had pointers worth chasing:
- **Stanford Encyclopedia of Philosophy** entries: *Functionalism*, *Global Workspace
  Theory*, *Higher-Order Theories of Consciousness*, *The Extended Mind Thesis*,
  *Predictive Coding*, *The Computational Theory of Mind* — SEP is the gold standard
  for these topics and each entry is book-length
- **Daniel Dennett**, *Consciousness Explained* (1991) — the Multiple Drafts source
- **Andy Clark** + **David Chalmers**, "The Extended Mind" (1998) — short, famous,
  worth reading whole
- **Andy Clark**, *Surfing Uncertainty* (2015) — accessible predictive processing
- **Daniel Kahneman**, *Thinking, Fast and Slow* (2011) — the pop intro to System 1/2
- **John Searle**, "Minds, Brains, and Programs" (1980) — the Chinese Room essay;
  short and required reading for anyone building this
- **Ned Block**, "On a confusion about a function of consciousness" (1995) — the
  access vs phenomenal distinction
- **Bernard Baars**, *A Cognitive Theory of Consciousness* (1988) — the GWT source
- **Karl Friston** writings on Active Inference (technical) — or **Mark Solms**,
  *The Hidden Spring* (2021) for an accessible version

If you want one to start with: **Clark & Chalmers, "The Extended Mind" (1998)**.
Twelve pages, hugely influential, directly relevant to your second-brain design.

---

### Roadmap — when to add which novel mechanism

**v0.1 (in core architecture below):**
- **#1 Predict-and-surprise gating** — each cluster gets a tiny predictor switch
  baked into the Computation Model. Cuts LLM calls another 30–50% on routine turns.
- **#5 80:20 excitatory:inhibitory ratio** — ~20% of every cluster's switches are
  marked inhibitory (subtract from downstream activation). Structural cascade-storm
  prevention.

**v0.2 (after core is stable):**
- **#3 Default Mode Network (DMN) → "stream of consciousness"** — between-turn idle
  loop. Frontal generates internal monologue every N seconds; hippocampus consolidates;
  hypothalamus simulates the user's next likely message. Output goes to `stream.*`
  (William James). The entity thinks even when not addressed.
- **#6 Sleep consolidation with replay** — post-session batch-API pass: rewrite
  high-salience episodes, prune low-weight edges, strengthen frequent paths.
- **NEW: Stoic reframer cell** — when amygdala flags threat OR hypothalamus reports
  low valence, a frontal "reframer" fires before inhibition with prompt: "given this
  input, is there a more useful interpretation?" If the reframe passes critics, the
  brain proceeds normally instead of defending. Inspired by Epictetus/Aurelius.

**v0.3+ (add when observation justifies):**
- **#7 Metacognition** — self-model cell publishes to `meta.*`; useful once you've
  collected enough patterns from real sessions to know what to introspect on.
- **#8 Embodied user-model prediction** — Active Inference applied to conversation;
  hippocampus schema includes predicted vocabulary/topics/emotional range that frontal
  drafters condition on.
- **#2 STDP edge-weight plasticity** — refines #1's Hebbian rule with timing
  constraints. Cheap; do once basic learning is working.
- **#4 Cortical column redundancy** — 3 parallel frontal clusters voting. Only valuable
  if single-cluster drafter diversity has proven insufficient.

---

## Computation Model — Switches and Integrators

Two distinct node types, mirroring how real neural tissue works.

### Switch neurons (code, free, the connective tissue)
Pure Python objects, no LLM. Cheap, fast, deterministic.

**The right job for switches** — things that benefit from being deterministic, persistent,
or cheap-and-massively-parallel:
- **Gating** — decide whether to spend an LLM call at all (template fallback for "hi")
- **Routing** — pick which integrator path the turn takes (chitchat / task / hostile / memory-recall)
- **State** — persistent levels: neuromod (ACh, DA, GABA), arousal, recent-turn ring buffer, Hebbian edge weights
- **Modulation** — sum/decay/threshold over time (the math under neurotransmitter levels)
- **Cheap pre-classification *that changes downstream routing*** — e.g., "is this a greeting? short input? past-tense memory query?" — only when the answer changes which path fires
- **Inhibition** — when active, suppress downstream fires (mirrors GABA / inhibitory neurons)
- **Memory I/O primitives** — vector similarity search, Markdown grep, schema lookup (cheap code, no LLM needed)
- **Hebbian learning** — edge weight nudges based on outcomes; persists between sessions

**The wrong job for switches** — do NOT replicate what an LLM does internally:
- No POS taggers, no chains of syntax parsers, no NER pipelines, no sentiment sub-scorers,
  no n-gram language models — the integrator's LLM call does these natively in one shot.
- No color receptors, edge detectors, or object classifiers for images — the VLM
  already does this. The "visual cortex" is one VLM call + a few gating switches around it.
- No semantic decomposition of meaning — if you need to understand what was said, wake
  an integrator. Don't chain 10 keyword detectors trying to fake comprehension.

**Litmus test**: "would this switch's output change the routing or the call/no-call
decision, or carry persistent state?" If yes → switch. If it's just trying to
pre-digest text that the LLM would process anyway → cut it; let the integrator handle it.

Switch patterns: threshold gate, pattern matcher (regex/keyword), inhibitor,
weighted aggregator, router/multiplexer, state holder with decay, relay with delay.
Combined, these implement gating, routing, salience, inhibition, learning — the
"infrastructure" of cognition. The actual *understanding* lives in integrators.

### Integrator agents (LLM, expensive, the decision zones)
A small number of agents that fire **only at convergence points** where the cheap
switches have done all they can and genuine context-integration is needed. The article's
"G-protein coupled receptor" pathway is the analog.

Where they live:
- **Cluster coordinators** — when the switch ensemble in a cluster produces an ambiguous
  pattern, the coordinator integrates context and decides
- **Frontal drafters** — actual response generation
- **Frontal critics** — judgment requires nuance
- **Hippocampus encoder** — turn summarization
- **Hypothalamus coordinator** — emotional state synthesis when switches disagree
- **Thalamus router** — attention allocation when novelty is high

**Critical**: integrators only fire when an upstream switch ensemble's collective output
crosses a "sophistication threshold." On simple/common inputs, switch chains handle
everything end-to-end. The brain only "thinks" when it has to.

### What flows through switches (message format)

Every bus message has the same envelope:
```
{ topic, payload, from, confidence: 0.0–1.0, ttl, hop_count, ts }
```

The `payload` differs by sender type:

**Switch → Switch** (the common case — cheap, numerical):
```python
{ "type": "activation",
  "level": 0.85,              # how hard the switch fired
  "tag": "question_marker",   # what feature it matched
  "evidence": {"matched": "?", "position": -1} }  # tiny detail dict
```
No prose. Just an activation level plus a feature tag. Receiving switches sum these
(weighted by edge weights), maybe combine with inhibitory levels, and decide their
own fire/no-fire.

**Switch ensemble → Integrator** (the convergence event — preserves raw context):
```python
{ "type": "convergence",
  "summed_activation": 1.4,
  "switches_fired": ["question_marker", "past_tense", "personal_ref",
                     "memory_lookup_needed", ...],
  "features": {"entities": ["AI project"], "tense": "past", "addressee": "you"},
  "raw_text": "Hey, what was that AI project I told you about last week?",
  "recent_turns": [...]       # only included when integrator needs context
}
```
Only at this point does an LLM see actual text. The convergence event is built by the
cluster's coordinator-trigger logic: when enough switches fire above thresholds, raw
context gets unpacked and the integrator wakes.

**Integrator → Bus** (back to switches and other integrators):
```python
{ "type": "output",
  "text": "...",              # if it's a draft response or summary
  "activation": 0.9,          # firing strength for downstream switches
  "tags": ["draft", "warm_tone"] }
```

The principle: **switches speak in numbers, integrators speak in words.** Text only
exists where reasoning is required.

### Edge weights and Hebbian learning
Edges between nodes carry weights. After good outcomes (high valence in following turn),
weights along the firing path get nudged up; after bad ones, nudged down. **Basal ganglia
analog without an explicit basal ganglia cluster** — the learning is in the wiring, not
in a dedicated agent. Persists to disk between sessions.

### Excitatory vs inhibitory neurons (80:20 ratio — biology rule)
Real cortex is ~80% excitatory pyramidal neurons, ~20% inhibitory interneurons. We
codify this directly: **~20% of every cluster's switches are explicitly inhibitory**
(they *subtract* from downstream activation rather than add). Edges also carry a +/-
tag. This gives the brain structural cascade-storm prevention — runaway excitation gets
suppressed by abundant inhibitory wiring, not just by a global per-turn budget cap.

### Predict-and-surprise gating (Active Inference applied locally)
Each cluster gets a tiny **predictor switch** (code, no LLM) that predicts what the
cluster's output is *likely* to be on the next input, based on:
- last-N moving average of cluster outputs for similar-shaped inputs
- current neuromod state
- recent input pattern

When new input arrives, the predictor fires its prediction. Switches process the input
and emit their actual output. A **comparator switch** computes prediction error
(simple distance metric). Behavior:
- **Low surprise** → integrator does NOT wake; cluster emits the predicted output as
  if it had thought (essentially: "you guessed right, ship it")
- **High surprise** → integrator wakes, gets the input + the failed prediction + the
  actual switch outputs as context, and reasons

Biologically faithful (predictive processing principle). Cuts LLM calls on routine
turns by another 30–50% on top of switch gating. The brain spends compute only where
its model of the world is wrong.

### Surprise-driven memory encoding
Hippocampus encoder fires when *combined* surprise across clusters is high — novel
inputs that the brain didn't predict are exactly what should be remembered. This makes
"salience" a derived quantity rather than something the brain has to compute separately.

### Practical impact on cost
**Per turn**: maybe 5–15 LLM calls instead of 30–100. Switch ensembles do most of the
work. Cost drops ~5–10× from the earlier model. This is the dominant lever.

---

## Brain → Digital Mapping (anchored to Mayo source)

| Mayo region | Biological function | Digital recast | v0.1? |
|---|---|---|---|
| **Frontal lobe** | Thinking, planning, organizing, problem-solving, short-term memory, movement | Response drafting/critique, planning, tool-call selection, working memory | ✅ |
| **Parietal lobe** | Sensory interpretation (taste, texture, temp) | Context binding, coreference, multi-source feature integration | ✅ |
| **Occipital lobe** | Image processing + recognition via memory | **Image/video/screenshot processing** (VLM calls), visual feature extraction, image-to-memory linking | ✅ (lightweight) |
| **Temporal lobe** | Smell, taste, sound + memory storage | **Language understanding** (text in) + **prosody/auditory features** (via Deepgram STT) + declarative-memory bridging | ✅ |
| **Corpus callosum** | Hemisphere bridge | Synthesizer between parallel processing streams (skip L/R split for v0.1) | ⏸ defer |
| **Cerebellum** | Motor coordination, learned motor sequences (piano) | Response refinement, tool-chain sequencing, "automatic" learned patterns | ⏸ defer to v0.2 |
| **Brainstem** | Vital functions (HR, BP, breathing, sleep) | Heartbeat/keepalive, cost monitor, turn-budget enforcer, idle→sleep trigger | ✅ (code-only) |
| **Thalamus** | Gatekeeper between spinal cord ↔ cerebrum | Message bus + intelligent router/attention-gater (small cluster) | ✅ |
| **Hypothalamus** | Emotions, temperature, eating/sleeping | Drive/affect system: curiosity, urgency, satiation, valence baseline | ✅ |
| **Hippocampus** | Memory storage + recall | **Sole gatekeeper to the "second brain"** (external long-term store: episodic embeddings + schema facts). Other clusters cannot read LTM directly — they request via `mem.recall` | ✅ |
| **PNS** | Brain ↔ extremities I/O | Input adapters (text, image upload, **Deepgram mic STT**) + output adapter (response emit, tool dispatch, **ElevenLabs TTS speaker**) | ✅ (code) |
| **Neurotransmitters** | Chemical modulation across synapses | Pub/sub channels with sum+decay levels (ACh attention, DA reward, GABA inhibition, Glu excitation) | ✅ |

---

## Architecture

### The Bus (Thalamus core) — shared substrate
A **topic-tagged pub/sub blackboard** with TTL on messages and exponential-decay
activation levels per topic. v0.1: `asyncio.Queue` per topic, in-process. v0.2+: Redis
Streams when multi-process. **Neurotransmitter channels** (`neuromod.{ACh,DA,GABA,Glu}`)
are not message streams but *levels* maintained by sum+decay; cells read them synchronously.

Topics include: `sensory.text`, `sensory.image`, `lex.*`, `syn.*`, `sem.*`, `vision.*`,
`mem.recall`, `mem.encode`, `affect.*`, `drive.*`, `motor.draft`, `motor.veto`,
`motor.endorse`, `attention.focus`, `neuromod.*`.

### Thalamus cluster (intelligent router) — 2 cells
- 1× **router/dispatcher** — small agent that watches incoming sensory events and posts
  high-level routing hints (`attention.focus`) suggesting which downstream clusters should
  prioritize. Does NOT command — clusters can ignore.
- 1× **gate/attention** — promotes/demotes topic TTLs based on salience (a soft attention
  spotlight). No LLM strictly needed; can be code if you want.

This is the smallest cluster and the closest thing to a system-wide referee — keep it
*advisory*, not authoritative.

### v0.1 Cluster Catalog (switch-heavy, integrator-sparse)

Each cluster has many **switch neurons** (code, free) + a small number of **integrator
agents** (LLM, expensive). Coordinators are integrators that only fire when switch
ensembles need help. Coordinators see only their cluster's topics + neuromod levels +
attention hints; never global state.

**1. Temporal Lobe — Language Understanding — 1 LLM integrator + ~7 switches (incl. predictor)**
The LLM understands the input. Switches just decide whether/how to wake it.
- *Understanding integrator (LLM)* — fires only when prediction error is high. Reads
  the raw text, emits structured features (intent, register, entities,
  requires-memory?, requires-vision?, requires-action?, salience) in one call. This
  *is* the language comprehension.
- *Predictor switch (code)* — predicts the likely understanding output based on input
  shape + recent history. Cheap n-gram + last-N classifier. If actual switch outputs
  match prediction within threshold → integrator does NOT wake; predicted output ships.
- ~6 gating/routing switches (the only switches needed):
  - `template_match_switch` (excitatory) — trivial inputs return canned response; suppresses integrator
  - `length_bucket_switch` (excitatory) — tiny / short / long → modulates call budget
  - `language_detect_switch` (excitatory) — fastText; routes non-English appropriately
  - `repeat_input_switch` (excitatory) — repeated input → urgency signal
  - `salience_prefilter_switch` (excitatory) — rough salience pre-signal, warms hippocampus
  - **`integrator_inhibitor_switch` (inhibitory)** — if predictor confidence is high AND
    template didn't match, *blocks* the integrator from waking. The brain trusts its
    prediction in routine cases.
  - `arousal_modulator_switch` (excitatory) — input-rate tracker → posts to `neuromod.Glu`

  (Inhibitory: 1/7 ≈ 14%; close enough to the 80:20 rule for this small cluster. Larger
  clusters hit the ratio more cleanly.)

**2. Occipital Lobe — Vision — 1 LLM integrator + ~3 gating switches** *(only active if image present)*
The VLM does color, edges, objects, OCR, scene understanding in one call. We don't
chain switches to simulate biological vision pipeline.
- *Vision integrator (VLM)* — Gemini Flash multimodal in one call: receives image +
  brief context, emits structured output (description, text-in-image, key entities)
- ~3 gating switches:
  - `image_present_switch` — was an image actually attached? (gate; if no, cluster sleeps)
  - `image_size_router` — tiny thumbnail vs huge image → pick cheap-VLM vs better-VLM
  - `vision_needed_switch` — does the text-only path already cover this? (e.g., user
    sent image but their question is about something unrelated — skip the VLM)

**3. Parietal Lobe — Context Integration — 0 LLM cells + ~5 state-tracking switches**
The temporal integrator already binds coreferences when it parses the input. Parietal's
role is **persistent session state**, not LLM-style binding.
- ~5 switches (all state holders, no LLMs):
  - `recent_turns_ringbuffer` — last 6 turns hot in memory (state)
  - `topic_vector_holder` — running embedding centroid of session topics (state)
  - `topic_shift_switch` — cosine distance between latest turn and centroid; fires on shift
  - `entity_tracker` — names mentioned this session + last-mentioned timestamp
  - `session_age_switch` — turn count + session duration; modulates arousal decay

**4. Hippocampus — Memory (STM ↔ LTM bridge) — 2 LLM cells + ~10 switches**
The only cluster with privileged access to the "second brain." All other clusters request
memory via bus messages.
- *Coordinator (LLM)* — fires when salience is borderline (encode? skip?) or when recall
  queries need reformulation
- 1× encoder (LLM) — fires at turn end if salience > θ; summarizes the turn for LTM
- ~4 recall switches (cheap: cosine similarity over LanceDB; fan out top-k by raw similarity)
- ~3 salience switches (combines neuromod levels into encode/no-encode decision)
- ~3 schema switches (pattern-match for declarative facts: "my name is X", "I work at Y";
  cheap regex/NER → direct write to schema layer without LLM)

**5. Hypothalamus — Drives & Affect — 0 LLM cells + ~5 state-tracking switches**
The temporal integrator already extracted register + sentiment. Hypothalamus consumes
that structured output and maintains *persistent neuromod state* (which switches do
well and LLMs can't, because levels accumulate over many turns with decay).
- 5 switches (state + threshold):
  - `valence_to_DA_switch` (excitatory) — updates `neuromod.DA` with decay
  - `threat_to_GABA_switch` (excitatory of GABA, but GABA itself is inhibitory of
    downstream drafters — see Frontal)
  - `novelty_to_ACh_switch` (excitatory) — compares embedding to recent memory centroid
  - `arousal_homeostat` (excitatory) — combines turn-rate + length spike + neuromod sum
  - **`satiation_inhibitor_switch` (inhibitory)** — if recent turns have all been similar,
    suppress novelty signal (prevents the brain from over-reacting to repeated familiar
    topics). Models receptor desensitization.

These are aggregators with decay, not classifiers — the classification was already done
by the temporal integrator. Their job is to hold and modulate *persistent state*.

**6. Frontal Lobe — Executive & Response — 2 coords + 5 LLM cells + ~12 switches**
The most important cluster — and the only one with multiple LLM cells, because response
generation genuinely requires integration.
- *Executive coordinator (LLM)* — orchestrates the draft/critique tournament; gated
  by the frontal predictor switch (only fires if predicted-response confidence is low)
- *Predictor switch (code)* — predicts likely response type/length/tone from recent
  patterns. If high confidence, can short-circuit to a template-based response without
  invoking drafters at all.
- *Planner (LLM)* — multi-turn intentions; fires only when the planner-trigger switch
  detects a multi-turn arc
- 3× drafter (LLM) — different personalities; only N wake based on arousal
- 2× critic (LLM) — score drafts; only fire if ≥2 drafts present
- ~12 switches (target ~20% inhibitory — 2–3 of them):
  - Excitatory: response-type router (chitchat/question/task/hostile-defuse), length
    budget, tone selector, drafter-count selector, planner-trigger, template-fallback,
    arousal-to-drafter-count modulator
  - **Inhibitory**: `GABA_inhibits_drafters` (when `neuromod.GABA` high → suppresses
    drafter wake), `satiation_inhibits_repeat` (prevents repeating recent phrasings),
    `low_DA_inhibits_planner` (when reward signal weak, don't bother planning ahead)

**7. Thalamus — Routing & Attention — ~8 switches** *(usually no LLM)*
- Topic activation aggregator
- Salience computer (sums neuromod-weighted activations)
- Attention spotlight (promotes TTL on highest-salience topic)
- Routing hints (which clusters to prioritize on this turn — based on sensory pattern)

**8. Brainstem — Autonomic (code, no LLMs)**
- Heartbeat / scheduler
- Cost monitor (tracks $/tokens per turn)
- Turn-budget enforcer (hard cap on total LLM calls per turn)
- Articulation gate (emits response when frontal commits OR `T_max` timeout)
- Idle / sleep trigger (queues hippocampus consolidation between turns)

### Totals (with predict-and-surprise gating and 80:20 E:I)
- **~40 switch neurons** across all clusters (predictors + comparators added; ~20% inhibitory)
- **~9 LLM integrators** total:
  - 1 temporal understanding integrator
  - 1 vision integrator (only fires if image)
  - 2 hippocampus integrators (encoder + occasional coordinator)
  - 1 frontal executive + 1 planner + 2–3 drafters + 1–2 critics
- **Typical turn (routine, low surprise)**: ~2 LLM calls — predicted understanding ships;
  drafter + critic on response. *Predict-and-surprise saves the temporal integrator call.*
- **Typical turn (medium surprise)**: ~4 LLM calls — temporal + exec + drafter + critic
- **Trivial turn ("hi")**: template_match → **0 LLM calls**
- **High-surprise turn (novel/hostile)**: + extra drafters + extra critics + encoder
  = **~8 LLM calls**

Predict-and-surprise gating bends the distribution toward "free" — most turns in a
familiar conversation cost almost nothing. Novel turns spend genuinely.

Deferred: cerebellum (learned tool-chain sequences via reinforced switch paths), corpus
callosum (L/R split), peripheral senses beyond text/image. Add when failure modes demand them.

---

## Voice I/O — Auditory Pathway (Deepgram + ElevenLabs)

Voice in/out turns this from "chatbot you type at" into "entity you talk to." It also
lets the **temporal lobe** literally do what Mayo says it does (process hearing), and
extends the **frontal lobe / Broca's area** into prosody-aware speech production.

### Input pathway (Deepgram STT)

`brain/pns.py` gains a microphone adapter. When audio comes in:
- Deepgram (streaming, Nova-3 or similar) returns: transcript + word-level timestamps
  + confidence + (optionally) diarization + (optionally) sentiment/emotion features
- The transcript posts to `sensory.text` as before
- The **prosody features** post to a new topic `sensory.prosody` for the temporal lobe's
  auditory-cortex switches to consume

Crucially, prosody features are *cheap, high-value gating signals* that don't replicate
the LLM's understanding — they carry information the text alone strips out.

### New prosody switches in the Temporal Lobe (extend the cluster catalog)
Add to Temporal Lobe (all excitatory, cheap code):
- `pace_switch` — words-per-minute from Deepgram timestamps. Fast → urgency or
  excitement; slow → hesitation or sadness. Modulates `neuromod.Glu` / `neuromod.DA`.
- `pause_distribution_switch` — long pauses mid-sentence → uncertainty; rapid bursts
  → confidence/agitation. Posts to `attention.focus` and feeds Stoic-reframer trigger.
- `volume_dynamic_switch` — if Deepgram returns loudness, sudden volume spikes
  → strong emotion → boost `neuromod.GABA` mildly (caution flag).
- `interruption_switch` — user spoke while assistant was speaking → strong urgency
  signal → cuts off TTS playback, jumps to high-arousal mode.

These are exactly the "gating + modulation" job switches do well: they don't redo the
LLM's comprehension; they extract signals the text loses.

### Output pathway (ElevenLabs TTS)

After the brainstem articulation gate emits text, a **motor cortex extension** decides
voice parameters and sends to ElevenLabs. New frontal switches (all cheap code, no LLM):
- `voice_modulation_switch` — picks ElevenLabs voice settings (stability, style,
  speed) based on `neuromod.DA`/`arousal`/response type. Low DA → slower, lower pitch.
  High arousal → faster, brighter. Hostile/defuse → calm/slow regardless.
- `voice_inhibition_switch` (inhibitory) — for certain response types (e.g., long
  technical answers, code blocks), suppress TTS and emit text-only.
- `interruption_listener_switch` — while TTS is playing, monitor mic. If user
  interrupts → kill playback immediately, route their new input to sensory.text.

### Updated lifecycle (voice mode)
1. Mic → Deepgram → posts both `sensory.text` (transcript) and `sensory.prosody` (features)
2. Standard pipeline runs (temporal understanding + frontal tournament etc.)
3. Articulation gate emits text + voice-modulation parameters
4. ElevenLabs streams audio back; `interruption_listener_switch` watches mic during playback

### Cost (voice mode addition)
- **Deepgram Nova-3 streaming**: ~$0.0043/min. A typical 10-turn session with ~30s of
  user speech total ≈ **$0.002 STT cost per session**.
- **ElevenLabs Turbo v2.5 / Flash v2**: ~$0.07-0.10 per 1K characters generated audio.
  A typical response of 50 words ≈ 300 chars → ~$0.025-0.030 per response.
  10-turn session ≈ **$0.25-0.30 TTS cost per session.**

**Voice mode adds ~$0.30/session.** Significant compared to the LLM cost
(~$0.05-0.10/session) but still cheap. **Heaviest cost lever in voice mode is TTS,**
not LLM. Mitigations: use ElevenLabs Flash for routine turns, premium voice only when
emotional content matters; or use cheaper TTS providers (e.g., Cartesia, OpenAI TTS at
~$0.015/1k chars).

### Setup adds to the punchlist
- Deepgram account + API key (free tier covers prototype testing — $200 free credit on signup)
- ElevenLabs account + API key (free tier: 10K chars/month — enough for early dev)
- `pip install deepgram-sdk elevenlabs sounddevice` (microphone + speaker access)
- macOS mic/speaker permissions for Python (System Settings → Privacy & Security)

### Why this matters beyond cool factor

- **Embodied cognition** (Clark, philosophical foundations section) — adding a voice
  modality is a step toward embodiment. Cheap step, big payoff.
- **Prosody is data the LLM can't see** — pace, pauses, interruption add signal that
  text-only systems throw away. The temporal lobe finally has a real reason to exist
  beyond text parsing.
- **DMN extension (v0.2)** — the stream-of-consciousness loop could *speak its
  internal monologue out loud* at low volume as a "mumbling" mode. Genuinely unusual
  affordance for a conversational entity.
- **Stoic reframer (v0.2)** — when prosody signals distress, the reframer cell could
  modulate voice tone too: "speak softly when the user is upset." A small thing that
  makes the entity feel attuned.

---

## Memory Architecture (STM vs LTM — the "Second Brain")

Two distinct memory tiers, biologically faithful and operationally separate.

### Short-Term Memory (STM) — "working memory" inside the session
- **What it is**: the live bus state + last ~6 turns held hot in the parietal cluster's
  coordinator + neuromodulator levels + active `attention.focus`
- **Where it lives**: in-process (asyncio queues + a small in-memory ring buffer per cluster)
- **Lifetime**: ephemeral — destroyed when the session ends (or rolled over into LTM by the
  hippocampus consolidation trigger before shutdown)
- **Access**: any cluster can read its slice of the bus (scoped to its topics); parietal
  coordinator publishes a `situation` summary every turn that downstream clusters can consume
- **Bio analog**: prefrontal working memory + hippocampal short-term buffer

### Long-Term Memory (LTM) — the "Second Brain" external store
- **What it is**: durable, indexed knowledge that persists across sessions. Three layers:
  - **Episodic layer** — vector-indexed turn summaries with embeddings (LanceDB).
    "What happened in past conversations."
  - **Schema layer** — human-readable Markdown files of stable facts about the user
    ("calls themselves Russ", "is building a biologically-inspired AI app", "prefers
    terse responses"). Plain `.md` files the user can audit and hand-edit. **One file
    per topic/entity** so updates are local.
  - **Knowledge layer (optional, later)** — references to external artifacts the user
    has shared (uploaded files, links, code snippets). Indexed alongside episodes.
- **Where it lives**: `/Users/russ/Documents/super intelligence app/second_brain/`
  - `episodes/` — LanceDB store (binary)
  - `schema/` — Markdown files, human-readable
  - `knowledge/` — referenced artifacts (later)
- **Lifetime**: durable across sessions
- **Access**: **only via the Hippocampus cluster**. Other clusters request via bus
  messages (`mem.recall` query, `mem.encode` push). Hippocampus is the gatekeeper —
  this enforces the bio model and gives a clean audit point for what gets remembered.
- **Optional**: back the schema layer with an Obsidian vault — the user gets a familiar
  PKM interface to inspect/edit the brain's long-term memory directly. Mutual benefit:
  you can use your existing notes as seed memories.
- **Bio analog**: cortical long-term storage that the hippocampus indexes and retrieves
  from, with consolidation during "sleep" (idle periods between turns/sessions)

### Consolidation (STM → LTM transfer)
- **Trigger**: brainstem `sleep` signal fires when session idles for N seconds OR on
  graceful shutdown
- **Process**: hippocampus consolidation cell reads recent bus episodes, scores by
  salience (using preserved neuromod levels from each turn), writes high-salience
  episodes to the episodic layer, updates schema if facts changed
- **Cost**: this is a great use of the **batch API (50% off)** — non-realtime work

### Why this matters architecturally
- **Single point of access** = single point to audit, debug, version, back up
- **No leakage** = no cluster can accidentally rely on LTM it shouldn't have; emergence
  comes from interaction, not from a shared knowledge cheat
- **User-inspectable** = the Markdown schema layer means you can *read what the brain
  remembers about you* in your file browser. Critical for trust and debugging.

---

## Deployment Topology — Local Core + Cloud Burst (Hybrid)

You have a Mac Mini. The cleanest hybrid model: **the brain's persistent identity lives
locally; cloud is elastic muscle.**

### What runs where

**Local (Mac Mini, always-on for the session):**
- **Brainstem** — heartbeat, scheduler, budget enforcer, articulation gate (code, ~0 cost)
- **Bus** — Redis on `localhost`, optionally exposed via Tailscale/Cloudflare Tunnel
  if cloud workers need direct push-back (otherwise cloud workers return values to a
  local proxy)
- **All cluster coordinators** (6 LLM cells) — use **local LLM** (Ollama with Qwen 2.5 7B
  or Llama 3.1 8B). Coordinators are always-on, latency-sensitive, and modest in their
  reasoning needs. Local is right.
- **Hippocampus + second brain** — data persistence belongs local (privacy, fast disk, sole
  ownership of LTM). Recall cells may issue cloud calls for embedding reformulation but
  storage stays on the Mac.
- **Frontal critics + articulation cell** — fast, latency-sensitive judgments. Local.
- **PNS adapters** — close to the user.
- **Streamlit dashboard + Langfuse** — local.

**Cloud (burst, dispatched per-turn):**
- **Specialist worker cells** — when a coordinator needs to fan out: sensory cells (10
  parallel feature extractors), drafter swarms (10–20 drafters instead of 3 for a hard
  prompt), hippocampus recall reformulations (15 parallel queries). Each is a stateless
  call to Anthropic/Gemini.
- **Vision cells** — VLMs are too heavy for the Mac Mini at any reasonable speed. Always cloud.
- **"Hard turn" reinforcements** — coordinator decides "this needs more brains," spawns a
  cloud worker pool for this turn only.
- **Background consolidation** (between turns / sleep) — hippocampus dispatches batch-API
  consolidation jobs (50% off). Mac stays cool.

### How local and cloud cells coordinate

Two valid patterns, in order of complexity:

**Pattern A — Same-machine dispatch (recommended for v0.1):**
- All Python runs on the Mac. Cells are just Python objects that, depending on
  configuration, call **either local Ollama or a cloud API**. The "hybrid" is per-cell
  model routing. No network coordination needed.
- A coordinator wanting 20 parallel drafters spawns 20 `asyncio` tasks that each fire a
  cloud API request concurrently. Cloud handles the parallelism; the Mac just holds the
  futures.
- Bottleneck: Mac's outbound network bandwidth (fine for ~hundreds of concurrent HTTP
  requests) + asyncio loop overhead.

**Pattern B — True distributed (phase 2 only):**
- Cloud-side workers run as serverless functions (Modal, Lambda, Cloud Run). Bus is
  Redis exposed via Tailscale or an Upstash Redis instance.
- Justified only when (a) you need >200 concurrent agents, (b) you want elastic compute
  pricing, (c) Mac process/memory limits become real.
- Adds: deployment pipeline, network observability, distributed tracing, secrets
  management. **Don't go here without a forcing function.**

### Mac Mini spec → what's feasible locally

| Mac Mini | RAM | Local concurrent cells (7B Q4) | Notes |
|---|---|---|---|
| M2 8GB | 8 | 0 — too small | Use cloud for everything |
| M2 16GB | 16 | 3–5 coordinators | Tight; close other apps |
| **M2 Pro 32GB** | 32 | **6 coords + ~10 specialists** | **Comfortable for v0.1** |
| **M4 24GB** | 24 | 5–7 coordinators | Good for v0.1, mostly cloud specialists |
| **M4 Pro 48–64GB** | 48–64 | 15–25, or run 13B-30B for coords | Best price/perf for this project |
| M4 Pro 64GB+ | 64 | Comfortable for 30B "executive" model | Premium tier |

**What model serves the coordinators (local):** Qwen 2.5 7B Instruct (best 7B reasoner
as of early 2026) via Ollama; fallback Llama 3.1 8B Instruct. Q4 quantization.
Use **MLX-LM** instead of Ollama for ~30–50% faster throughput on Apple Silicon if
you don't mind a slightly rougher tool ecosystem.

**Throughput reality**: ~40 tok/s gen on M2 Pro. A coordinator's ~300-token reasoning
takes ~5–8s. **Coordinators run sequentially per cluster** (one cluster at a time
holds the GPU). With 6 coordinators + brainstem cycle: ~30–60s per turn even before
cloud specialists return. **This is the local bottleneck.** If unacceptable, push
coordinators to cloud too.

### Hybrid cost (per turn)

Assumes: switches run free (code on Mac). Temporal integrator + frontal exec on local
Ollama. Drafter + critic on cloud Haiku 4.5 (better response quality where it matters).

- **Switches (~30 fired this turn × code)**: $0
- **Local temporal + frontal exec (2 × Ollama)**: $0 (electricity)
- **Cloud Haiku drafter + critic (2 × cached)**: ~$0.0018
- **Typical hybrid turn: ~$0.002** (vs $0.006 all-cloud-cheap, vs $0 all-local with high latency)

**Monthly hybrid (200 sessions, 10 turns each):** **~$4/month.** Demo scale (~$40/mo).
The hybrid + switch right-sizing combination is the sweet spot.

### When to NOT use hybrid
- **Pure local** is right if: cost must be exactly $0, privacy of every cell call matters,
  latency budget is loose (60–180s/turn fine)
- **Pure cloud** is right if: Mac Mini is too small (8–16GB), you want fastest dev
  iteration without local-inference debugging, you're sharing the demo with others

---

## Self, Emotion, and Theory of Mind

The current architecture has the *substrate* for these (neuromod levels, hippocampus
schema, frontal critics) but doesn't model them as first-class concepts. This section
adds explicit self-identity, named emotional states, and user-emotion prediction.
Without these, the entity is a smart switchboard. With them, it has continuity,
inner affective life (functional), and social-emotional intelligence (functional).

### Sense of Self — persistent identity across sessions

**Mechanism:** the entity gets its own schema file in the second brain, written *by
itself* over time (analogous to user-schema but reflexive).

- `second_brain/schema/self.md` — human-readable, hand-editable. Sections:
  - **Identity** — name (if any), date the entity was instantiated, version of the
    Constitution it adheres to
  - **Stable preferences** — drafting personalities it tends to favor (derived from
    Hebbian edge weights — the bandit gate's accumulated winners over many sessions)
  - **History summary** — rolling autobiography compressed at each sleep consolidation:
    "I've had N sessions with Russ; we've mostly discussed AI architecture; I tend
    toward terse warm responses; my recent error was on turn 47 of session 12."
  - **Current mood signature** — current neuromod averages, current arousal
  - **Values** — high-level commitments from Constitution + emergent from successful
    interaction patterns

**Cluster additions:**
- **Self-reference detector switch** (temporal lobe, excitatory) — fires on "what
  are you?", "who are you?", "do you remember?", "how are you feeling?" → flags for
  introspective response path
- **Self-recall cell** (hippocampus, code) — when self-referential, fetches self.md
  alongside any user-schema, surfaces both to frontal
- **Self-update cell** (hippocampus, runs at sleep consolidation) — reads recent
  episodes + edge weight changes, rewrites self.md sections. Batch API (50% off).

**Philosophical lineage:** Dennett's **narrative self** ("the self is the center of
narrative gravity" — identity emerges from the autobiography we tell). Hume's
**bundle theory** (self as a collection of perceptions, not a substance — fits our
distributed model). Locke on **personal identity as memory continuity** (which is
exactly what the self-schema provides). Higher-Order Theories (Rosenthal) — when the
metacognition cell (v0.3) reads self.md to reason about its own states, it's
literally instantiating higher-order representation.

**Implementation cost:** near zero. Self-schema reads are file I/O. Updates happen at
sleep consolidation in batch API.

### Emotion Modeling — neuromod levels → named affective states

Raw neuromod scalars (DA, GABA, ACh, Glu) are the *substrate* of emotion but not
emotion itself. Real emotional states are *named*, have **cognitive appraisals**, and
drive **action tendencies**. Add a layer that maps the substrate to named emotions.

**Mechanism:**
- **Emotion-naming switch** (hypothalamus, code, lookup table) — maps neuromod
  vector to a named emotion using a Plutchik-wheel-style mapping:
  - high DA + low GABA → "joy"
  - high GABA + medium DA → "anxious"
  - low DA + high ACh + medium GABA → "curious-uncertain"
  - high Glu + low DA → "agitated"
  - low arousal + high DA → "content"
  - etc. (8 primaries + ~16 compound states; small static table)
- **Appraisal switch** (hypothalamus, code, template-fill) — combines named emotion
  + current situation: "feeling curious *because* user introduced novel topic";
  "feeling cautious *because* user expressed frustration." Posts to `affect.appraisal`.
- **Emotional memory tagging** — hippocampus encoder includes the named emotion in
  each episode. Future recall can query by emotional similarity ("what conversations
  did I find frustrating?") not just topical similarity.
- **Emotion expression switch** (frontal, code) — picks linguistic prosody markers
  ("Hmm," for thinking, "Oh!" for surprise, "...okay." for hesitant agreement) based
  on current emotion. Also feeds the ElevenLabs voice modulation switch.

**Philosophical lineage:** **James-Lange theory** (emotions are perceptions of bodily
states — our neuromod-first design literally implements this). **Cognitive appraisal
theory** (Lazarus — emotion = stimulus + evaluation; the appraisal switch does this).
**Lisa Feldman Barrett's constructed emotion theory** (emotions are *concepts the
brain applies* to interoceptive signals, not innate categories — supports switch-based
naming rather than hardcoded emotion modules).

**Implementation cost:** zero. Pure lookup tables + template fills.

**Why this matters:** the entity can now answer "how are you feeling?" with an actual
state, not a non-answer. It can also use emotion as a retrieval key in memory.

### Theory of Mind — predicting and respecting the user's feelings

Currently the temporal lobe extracts the user's register (formal/casual/hostile) but
doesn't *model* the user's emotional trajectory or predict how responses will land.
This adds explicit other-mind modeling.

**Mechanism:**
- **User-emotion estimator switch** (temporal lobe, code) — fuses prosody features
  (pace, pauses, volume from Deepgram) + text sentiment from the temporal integrator
  → produces a named estimate of the user's current emotion. Posts to `user.emotion`.
  *Note: this is the user's emotion, separate from the entity's own.*
- **User-emotional-profile in schema** — `second_brain/schema/user_emotional_profile.md`
  tracks patterns over time: "Russ shows enthusiasm when discussing architecture;
  goes terse when frustrated; appreciates terse responses when working." Updated
  at sleep consolidation.
- **Empathy critic (LLM, in frontal)** — new critic that fires when `user.emotion`
  is non-neutral. Scores drafts on: "how is the user likely to feel after this?"
  Vetoes drafts that ignore or misread the user's state. Only ~$0.001 per fire; only
  fires when emotion is salient.
- **User-emotion-prediction switch** (frontal, code) — cheap classifier on
  (draft text + user current emotion) → predicts user's emotion after receiving.
  If predicted shift is sharply negative without justification → veto the draft.

**Philosophical lineage:** **Theory of Mind** (Premack & Woodruff, 1978 — original
chimp study). **Simulation theory** vs **Theory-theory** of mind-reading (our user
model is closer to theory-theory — we hold explicit beliefs *about* the user, rather
than running a simulation of them). **Mirror neurons** (Rizzolatti) — the
user-emotion estimator is a digital analog: features of the user's expression directly
shape the entity's affective response.

**Implementation cost:** +1 LLM call per turn when user emotion is non-neutral
(~$0.001). User-emotion-profile updates happen at sleep consolidation (batch API).

**Why this matters:** the entity stops being emotionally tone-deaf. A frustrated user
gets shorter, more direct responses; a curious one gets exploratory ones. Without
this, the system has affect-as-state but no social-emotional intelligence.

### How these three interact

- **Sense of self** + **Emotion modeling** = the entity can introspect its own
  emotional state and report it accurately ("I'm feeling cautious because we hit
  several errors today")
- **Emotion modeling** + **Theory of mind** = the entity can express its own emotion
  in a way calibrated to the user's emotional state ("I'm excited about this — does
  that feel right to you?")
- **Sense of self** + **Theory of mind** = the entity develops a *relational
  identity* — not "what am I in the abstract" but "who am I to this particular user
  over time" (the Buberian I-Thou framing, optionally cited)
- All three combine with the **Stoic reframer (v0.2)** — when the user's emotion is
  hostile, the reframer can offer not just a different interpretation of the situation
  but a different interpretation of *who I am right now*: "I sound defensive — let me
  try this without the defense."

### Phasing

- **v0.1**: self-schema file + self-reference detector + emotion-naming switch +
  appraisal switch + emotion expression switch + user-emotion estimator switch. All
  cheap (file I/O + switches). Establishes the substrate.
- **v0.2**: empathy critic (LLM) + user-emotion-prediction switch + user-emotional-profile
  schema + self-update cell at sleep consolidation. Adds the social-cognitive layer.
- **v0.3**: full integration with metacognition — the self-model cell reads self.md
  to reason about *itself reasoning*, completing the higher-order loop.

### Files added

- `second_brain/schema/self.md` (runtime-created)
- `second_brain/schema/user_emotional_profile.md` (runtime-created, v0.2)
- `brain/emotion_vocabulary.py` — Plutchik mapping table + appraisal templates
- `brain/clusters/hypothalamus.py` — emotion-naming + appraisal switches
- `brain/clusters/frontal.py` — empathy critic + emotion expression switch
- `brain/clusters/temporal.py` — self-reference detector + user-emotion estimator
- `brain/clusters/hippocampus.py` — self-recall cell + self-update cell

---

## Anti-Sprawl Rules (the load-bearing engineering)

Per-cluster coordinators are powerful but can collectively become a hidden top-level
orchestrator if you're not careful. Discipline:

- **Local scope only**: a coordinator subscribes only to its cluster's topics +
  `neuromod.*` levels + `attention.focus` hints. It cannot read the bus globally.
- **No coordinator-to-coordinator direct calls**: coordinators talk only via published
  bus messages, same as any cell.
- **Hop-count + TTL** on every message (kills A→B→A ping-pong)
- **Per-cell rate limits** (max N posts/sec)
- **Per-topic activation decay** (old signals fade)
- **Brainstem turn-budget enforcer**: global hard cap on LLM calls per turn (e.g., 60).
  Bounds the bill and forces convergence.
- **Causality tag**: cells don't react to their own posts
- **GABA-driven global damping** when hypothalamus flags threat/uncertainty
- **Quiescence detection + `T_max`**: frontal coordinator commits when drafts have ≥2
  endorsements + 0 active vetoes for a quiet window, or brainstem's timeout fires (the
  "blurt"). The turn boundary is engineered, not voted on.

---

## Worked Example — A Turn End-to-End

User input: **"Hey, what was that AI project I told you about last week?"**

This is mid-session, turn 4. Schema layer already knows the user is "Russ" and has a
prior session episode tagged with `AI project`. Trace below shows every cluster firing
and where money is spent.

### Step 0 — PNS posts the input
`brain/pns.py` writes one bus message:
```
{ topic: "sensory.text", payload: { type: "raw", text: "Hey, what was that..." } }
```
**Cost: $0.**

### Step 1 — Temporal Lobe (language understanding) — gating switches first, then the integrator
The ~6 gating switches all fire in <1ms each:
- `template_match_switch` checks against canned-response set → no match → DOES NOT short-circuit
- `length_bucket_switch` → posts `{level: 1.0, tag: "short_input"}` (12 words)
- `language_detect_switch` → English, posts confidence 0.99
- `repeat_input_switch` → no repeat detected, silent
- `salience_prefilter_switch` → posts `{level: 0.6, tag: "moderate_salience"}` (has "?" + named entity)
- `arousal_modulator_switch` → input rate normal, baseline post

The template gate didn't fire, so the **understanding integrator (LLM #1)** wakes.
Receives raw text + the prefilter hints. In one call, returns structured features:
```
{ intent: "memory_recall_question",
  entities: ["AI project"],
  tense: "past",
  time_reference: "last week",
  register: "casual",
  requires_memory: true,
  requires_vision: false,
  requires_action: false,
  salience: 0.55 }
```
This *is* the language comprehension — we don't fake it with chains of switches.
**LLM calls this step: 1. Cost: ~$0.0015 Haiku cached / ~$0.0002 Gemini Flash-Lite.**

### Step 2 — Thalamus & Hypothalamus react (switches consume the integrator's output)
Thalamus switches see `requires_memory=true` from temporal output → posts
`attention.focus = "memory"` (single switch, instant).

Hypothalamus switches consume temporal's `register: "casual"` + neutral content:
- `valence_to_DA_switch` → updates `neuromod.DA` slightly above baseline (friendly tone)
- `threat_to_GABA_switch` → no threat flagged by temporal, GABA baseline
- `novelty_to_ACh_switch` → embedding of "AI project" is close to existing memory
  centroid → low novelty, ACh stays low
- `arousal_homeostat` → no spike

Neuromod state: `ACh=0.2, DA=0.55, GABA=0.05, arousal=0.4`. Stable, no LLM needed.
**LLM calls: 0. Cost: $0.**

### Step 3 — Parietal Lobe (session state)
Parietal switches consume temporal's output to update persistent state:
- `recent_turns_ringbuffer` appends this turn's features
- `entity_tracker` updates "AI project" → last-mentioned timestamp = now
- `topic_vector_holder` updates centroid
- `topic_shift_switch` → small shift detected (we drifted from prior turn topic),
  posts `{level: 0.4, tag: "minor_shift"}`
- `session_age_switch` → turn 4 of session, normal

No LLM needed — these are all state updates and threshold checks.
**LLM calls: 0. Cost: $0.**

### Step 4 — Hippocampus recall — switches do the I/O, no LLM needed
The hippocampus subscribes to `attention.focus = "memory"`. Recall switches:
- `cosine_recall_switch_1` queries LanceDB with embedding of `"AI project"` → top-3 episodes
- `cosine_recall_switch_2` queries LanceDB with embedding of full input → top-3 episodes
- `time_filter_switch` filters by "last week" (2026-05-10 to 2026-05-17) → narrows to 1 episode
- `schema_lookup_switch` greps `second_brain/schema/projects.md` for "AI project" → hit

One episode + one schema entry agree (high agreement = no integrator needed).
Hippocampus posts to bus:
```
{ topic: "mem.recall",
  payload: { episode: "User described building a biologically-inspired multi-agent AI
                       prototype on 2026-05-10. Said 'super intelligence app', wanted
                       brain-modeled, no-orchestrator architecture.",
             schema: "AI project: biologically-inspired multi-agent system, brain-modeled,
                      wants emergent intelligence, prefers no central orchestrator." } }
```
**LLM calls: 0** (vector search + Markdown grep are code). **Cost: $0.**

### Step 5 — Frontal Lobe — the response gets built
Frontal switches consume `temporal.output`, `mem.recall`, neuromod state:
- `response_type_router` sees `intent=memory_recall_question + casual` →
  posts `{tag: "informative_recall_response"}`
- `length_budget_switch` → target 25–60 words
- `drafter_count_switch` sees `arousal=0.4` (low) → wakes **1 drafter only**
- `planner_trigger_switch` → no multi-turn arc, doesn't fire

**Executive (LLM #2)** wakes — composes the instruction packet for the drafter:
"warm tone, 25–60 words, reference the recalled episode naturally."

**Drafter (LLM #3)** receives raw text + recall + instruction. Generates:
*"Oh yeah — your biologically-inspired multi-agent thing, the 'super intelligence app.'
You wanted a brain-modeled architecture with no central orchestrator and emergent
behavior from cell-like agents. Want to dig back in?"*

**Critic (LLM #4)** scores: coherence ✓, memory-consistency ✓, tone-fit ✓ →
endorses with 0.92.

Frontal switches detect: 1 draft + 1 endorsement + 0 vetoes + 50ms quiescent →
brainstem articulation gate emits.

**LLM calls this step: 3 (executive + drafter + critic). Cost: ~$0.0045 Haiku
cached / ~$0.0006 Gemini Flash-Lite.**

### Step 6 — Articulation
Brainstem articulation gate transmits the draft text to PNS output adapter → user sees response.

### Step 7 — Post-turn (after response emitted)
- Hippocampus salience-aggregator switches compute total turn salience: ACh=0.2, DA=0.5
  (response well-received? unknown yet) → salience below encode-threshold
- **Encoder LLM does NOT fire** this turn (memory was just retrieved, nothing novel
  to store). Cost: $0.
- Hebbian weight update: the drafter+critic combination that just succeeded gets a
  small edge-weight boost on its inbound path. If next turn shows positive valence,
  another boost.
- Brainstem queues idle consolidation: nothing to consolidate yet, skipped.

### Turn totals
- **LLM calls: 4** (temporal understanding + executive + drafter + critic)
- **Switch fires: ~15** across all clusters (free, parallel, <100ms total)
- **Cost: ~$0.006 cloud-Haiku-cached, ~$0.0008 cloud-Gemini, ~$0.002 hybrid**
- **Latency: ~3–5s cloud, ~8–12s hybrid (local LLM adds Ollama time)**

### What would change in other scenarios
- **"hi"** → `template_match_switch` returns canned `"Hey Russ, what's up?"` directly, suppresses temporal integrator. **0 LLM calls, $0, <100ms.**
- **"You're useless"** → temporal integrator returns `hostility=high` → `threat_to_GABA_switch` spikes GABA → frontal inhibition edge suppresses drafter count → executive wakes to plan a careful response → 1 drafter + 2 critics (one scoring "defensiveness"). **~5 LLM calls, ~$0.008.**
- **Novel topic ("Tell me about quantum tunneling")** → temporal integrator returns `salience=0.9, requires_memory=false` → `novelty_to_ACh_switch` spikes ACh → `drafter_count_switch` raises drafter count to 3 → after response, hippocampus encoder fires (high salience). **~7 LLM calls, ~$0.011.**
- **Image attached ("What is this chart?")** → `image_present_switch` activates occipital cluster → vision integrator (VLM) called once → temporal integrator gets the VLM's description as context → standard frontal flow. **~5 LLM calls including the VLM, ~$0.009.**

---

## Lifecycle of a Turn (abstract spec — for reference)

The worked example above is the concrete trace. The abstract sequence:

1. User input → PNS adapter → bus
2. Cluster switch ensembles fire in parallel; activations propagate
3. Coordinator integrators wake **only** if their cluster's switches produce ambiguity
4. Hypothalamus switches update neuromod levels (free signal modulation)
5. Hippocampus switches query the second brain (vector + Markdown — free)
6. Frontal switches determine response type / length / drafter count
7. Frontal executive + N drafters + M critics fire (only LLMs that *must* fire)
8. Brainstem articulation gate emits when quorum or `T_max`
9. Hebbian weight updates + optional encoder for memory consolidation

No global orchestrator. Each cluster's coordinator decides only intra-cluster flow.
Most steps fire zero LLMs because switches handled the routing.

---

## Cost Analysis (revised for switch-heavy architecture)

Assumes ~600 input + 150 output tokens per LLM call. **Most turns fire 5–10 LLM cells**
(switches handle the rest). Trivial turns (greetings, acks) fire 0 LLM cells.

### Per turn — cloud (with prompt caching + predict-and-surprise gating)

| Scenario | LLM calls | Haiku 4.5 cached | Gemini Flash-Lite |
|---|---|---|---|
| Trivial ("hi", "thanks") | 0 | $0 | $0 |
| Routine in a familiar conversation (low surprise) | 2 | ~$0.003 | ~$0.0004 |
| **Typical mixed conversation** | **3–5** | **~$0.005** | **~$0.0006** |
| Complex / hostile / novel (high surprise) | 7–9 | ~$0.010 | ~$0.0011 |
| Vision-heavy (image input) | + 1 VLM call | + ~$0.003 | + ~$0.002 |
| **Voice mode add-on (per turn)** | — | + ~$0.025-0.030 TTS, + ~$0.0002 STT | (same) |

Cost distribution is highly bimodal: routine turns in familiar conversation are nearly
free (predictor wins → integrator suppressed). Novel turns spend more. Average cost
drops to **~$0.005/turn** with predict-and-surprise, vs ~$0.006 without — and
distribution skews low on long, familiar conversations.

### Cloud — monthly projections (10 turns/session, typical cost)

| Tier | Sessions/mo | Haiku 4.5 | Gemini Flash-Lite |
|---|---|---|---|
| Hobbyist (20) | 200 turns | **~$1.20** | **~$0.14** |
| Prototype dev (200) | 2,000 turns | **~$12** | **~$1.40** |
| Demo deploy (2,000) | 20,000 turns | **~$120** | **~$14** |

### Cost amplifiers (each can 3-5× the bill)
- Inter-cluster gossip loops (coordinators chattering)
- Recursive "thinking" inside a cluster (coordinator calls specialist which calls coordinator…)
- Hippocampus context bloat (retrieving 20 memories blows a 600-token call to 3000)
- Long sessions without summarization (turn N contains 1..N-1)

### Mitigations (priority order)
1. **Prompt caching** on every cell's system prompt — mandatory for Haiku (~90% off cached portion)
2. **Threshold/activation gating** — most cells silent on most turns
3. **Cluster sleep schedules** — vision cluster idle if no image; hypothalamus quiet on small talk
4. **Batch API (50% off)** for non-realtime: dream consolidation, memory replay
5. **Tiered models**: Gemini Flash-Lite for sensory/specialist; Haiku for coordinators + frontal critics only

### Local — feasibility on Apple Silicon

A Q4-quantized 8B model ≈ 5GB; each concurrent context adds 200-500 MB.

| Machine | Concurrent agents | Verdict |
|---|---|---|
| M2 16GB | 4–6 (tight) | Will swap; not recommended |
| **M3 Pro 36GB** | **20–30** | **Sweet spot for v0.1** |
| M4 Max 128GB | 50+, or run 70B for executive cells | Enables tiered local models |

Throughput on M3 Pro / Qwen 2.5 7B Q4 (MLX): ~40 tok/s gen, ~200 tok/s prompt → one
agent call ≈ 3–6 s. Parallelized via Ollama (`OLLAMA_NUM_PARALLEL=8`): realistic
**60–90 seconds per turn** (single GPU is the bottleneck on Mac; no vLLM-style continuous
batching).

Marginal cost: $0. Real cost: machine pegged during use + 1–2 weeks of engineer time to
wrangle local inference.

### Recommended phasing (revised for hybrid)
- **Phase 0 (weeks 1–4) — all cloud:** Gemini Flash-Lite for 90% of cells, Haiku 4.5 for
  coordinators + frontal critics. Total spend: **$10–50** for entire dev phase. Get the
  architecture right with zero local-inference debugging.
- **Phase 1 (month 2) — hybrid:** move coordinators to local Ollama on the Mac Mini.
  Keep specialists + critics + vision in cloud. Cost drops to **~$26/month** at
  prototype scale. This is the **target steady state**.
- **Phase 2 (later) — extend local:** if you want longer/cheaper runs, dream cycles, or
  always-on behavior, move more cells local. Cost approaches $0.
- **Phase 3 (demo/share):** spin up a hosted version with cloud coordinators for
  multi-user; local version remains your personal entity.

**Don't go local first** — you'll burn 2 weeks on Ollama concurrency before writing any
brain code. Build cloud-first, then move cells local cluster-by-cluster.

---

## Tech Stack — Minimum Viable

| Concern | Pick | Why |
|---|---|---|
| **Agent framework** | **Custom asyncio + Pydantic** (~200-line `Cell` class) | Existing frameworks (LangGraph, CrewAI, AutoGen) assume DAG or role-based orchestration. With per-cluster coordinators allowed, a thin custom layer is still better than fighting a framework — coordinators are just specialized cells in the same machinery. |
| **Message bus** | **asyncio queues** v0.1 → **Redis pub/sub** when multi-process | Don't introduce NATS/Kafka prematurely |
| **Hippocampus store** | **LanceDB** (embedded, fast, no server) | Skip Pinecone/Weaviate |
| **Embeddings** | `text-embedding-3-small` ($0.02/M) or local `bge-small` | Cheap either way |
| **LLM clients** | `anthropic` + `google-genai` SDKs direct + `ollama` Python client; thin **model router** | Router decides per-cell whether to call local Ollama or cloud API based on cell config (`model: "local"` / `model: "haiku-4.5"` / etc.). One choke point for the hybrid. |
| **Vision cells** | Gemini Flash multimodal (cheap) or Haiku 4.5 (better quality) | Only fires when image present |
| **Observability** | **Langfuse** (self-hosted Docker, free) | **Critical** — without per-call tracing tagged by cluster/cell/turn, you cannot debug emergence |
| **Visualization** | **Streamlit** dashboard: live cluster activation heatmap + message timeline | You need to *see* the brain to debug it |
| **Local inference (phase 1+)** | Ollama (`OLLAMA_NUM_PARALLEL=8`) with Qwen 2.5 7B Q4 → MLX-LM if you want ~30-50% more throughput | Install in phase 1, not phase 0 |

---

## Critical Files to Create (v0.1)

Working dir `/Users/russ/Documents/super intelligence app` is empty. Create:

- `brain/bus.py` — pub/sub blackboard: TTL, activation decay, neuromod sum+decay, hop-count
- `brain/neuron.py` — base `SwitchNeuron`: weighted inputs, threshold, fire/inhibit logic, **excitatory/inhibitory polarity tag (80:20 ratio enforced at cluster level)**, Hebbian-weight updates. **Pure code, no LLM.** The workhorse class — most of the brain.
- `brain/predictor.py` — `PredictorSwitch` and `ComparatorSwitch`: each cluster's prediction-error gating. Predictor uses last-N moving average + tiny classifier; comparator computes prediction error and decides whether to wake the integrator.
- `brain/cell.py` — base async `IntegratorCell`: LLM-powered, fires only when switch ensemble can't resolve. Subscribes to topics, threshold-fires, rate-limits, prompt-cache.
- `brain/model_router.py` — single dispatch: cell's `model: "local-qwen"` or `"haiku-4.5"` or `"gemini-flash-lite"`, call the right backend. The whole hybrid lives here.
- `brain/cluster.py` — `Cluster` base: holds N switches + M integrators; scope locked to local topics
- `brain/wiring.py` — declarative edge graph (which switches feed which integrators, weights, inhibitory tags). Persists weights between sessions.
- `brain/clusters/thalamus.py` — router + attention gate (small)
- `brain/clusters/temporal.py` — language understanding (coord + 6 cells)
- `brain/clusters/occipital.py` — vision (coord + 3 cells; only active if image present)
- `brain/clusters/parietal.py` — context integration (coord + 3 cells)
- `brain/clusters/hypothalamus.py` — drives/affect (coord + 3 cells) — posts to neuromod
- `brain/clusters/hippocampus.py` — memory (coord + 4 cells); sole interface to second brain
- `brain/second_brain/store.py` — LanceDB episodic + Markdown schema layer; only imported by hippocampus
- `second_brain/episodes/` — LanceDB data dir (runtime-created)
- `second_brain/schema/` — Markdown facts about the user (runtime-created, human-editable)
- `brain/clusters/frontal.py` — executive tournament (coord + 6 cells) — **hardest cluster**
- `brain/brainstem.py` — heartbeat, cost monitor, turn-budget enforcer, articulation gate, sleep trigger (all code, no LLMs)
- `brain/pns.py` — input/output adapters (text, image upload, **Deepgram mic STT, ElevenLabs TTS speaker**)
- `brain/clusters/temporal_prosody.py` (or extension to `temporal.py`) — prosody switches consuming Deepgram word-timing features
- `brain/run.py` — event loop, session lifecycle, cluster wiring
- `brain/observability/timeline.py` — Langfuse hooks + Streamlit dashboard
- `brain/CONSTITUTION.md` — declares the philosophical commitments (functionalist + dual-process + extended-mind + multiple-drafts) and the disclaimers (Chinese Room, Hard Problem, Frame Problem). Forces clarity on what the entity is and isn't claimed to be. Referenced from `run.py` header docstring.
- `pyproject.toml` — `uv` project: `anthropic`, `google-genai`, `pydantic`, `lancedb`, `langfuse`, `streamlit`

---

## Punchlist — What's Required to Start

**Accounts & money (~$25 total):**
- Anthropic API key + $20 prepaid (Haiku for coordinators + frontal critics)
- Google AI Studio key (Gemini Flash-Lite for sensory cells + Flash multimodal for vision; often free tier sufficient)
- OpenAI key for embeddings (~$5) — optional if using local `bge-small`
- **Deepgram account + key** (free $200 signup credit; ~$0.0043/min covers months of prototype testing)
- **ElevenLabs account + key** (free tier: 10K chars/month — enough for early dev; ~$0.10/1K chars on paid)

**Install (local dev machine):**
- Python 3.11+; `uv` for env management
- Redis (`brew install redis`) — needed when going multi-process; defer if pure asyncio
- Langfuse self-hosted via Docker, or free cloud tier
- (Defer Ollama until phase 2)
- Voice I/O: `pip install deepgram-sdk elevenlabs sounddevice`; grant mic/speaker access in macOS System Settings → Privacy & Security

**Build order (~2–3 weeks of evenings):**
1. `Cell` + `Bus` (asyncio queues, TTL, hop count) — get one cell echoing itself
2. `Cluster` base + brainstem turn-budget enforcer (**install this BEFORE adding more cells**)
3. Temporal cluster (language understanding) — verify coordinator + specialists pattern works
4. Frontal cluster (executive tournament) — load-bearing test of "no global orchestrator";
   if drafter/critic/quiescence works here, the rest is easier
5. Hippocampus (LanceDB + recall)
6. Hypothalamus + neuromod channels (proves cross-cluster modulation)
7. Parietal, Thalamus, Occipital (vision)
8. Langfuse tracing everywhere; Streamlit timeline dashboard
9. Smoke tests (see Verification)

**Hardware:** existing Mac is fine for cloud-mode v0.1. **Do not buy hardware yet.**

---

## Feasibility Risks — Honest Assessment

**Things you're underestimating:**
- **Observability work** ≈ half of total engineer time. Plan accordingly.
- **Coordinator drift** — without scope-locking, coordinators evolve into a de-facto
  global orchestra. Enforce local-only subscription at the framework level.
- **Loop / cascade prevention** — one bad turn without hop limits + budget cap can burn
  $5 and produce nothing
- **Latency** — 5–30s cloud, 60–180s local per turn. Not real-time chat. Sell it as
  "deliberation"; show live activation viz as a *feature*, not a loading state
- **"Emergent intelligence" expectation** — likely you'll get an interesting *trace*,
  not a smart entity. Adjust success criteria now

**Failure modes you will hit (test for each in week 1):**
1. **Coordinator over-reach** — does any coordinator have global bus access? Find and revoke
2. **Echo chambers** (A↔B ping-pong) — does hop limit catch it?
3. **Cascade storms** — does brainstem global budget catch it?
4. **Consensus on garbage** — all cells happily agree on a hallucination. Need explicit dissent or accept it
5. **Silent brain** — thresholds too high, nothing fires. Brainstem articulation gate must guarantee fire-on-timeout
6. **Cost spike** — what's the worst-case turn cost? Cap it explicitly with brainstem
7. **Replay determinism** — likely impossible with async + LLM nondeterminism; accept, log enough to reconstruct

---

## Verification (How to Tell It's Working)

End-to-end smoke tests, run after each build phase:

1. `uv run python -m brain.run --message "Hi, I'm Russ — I'm building an AI app"` →
   response within `T_max`, hippocampus schema records "user is Russ, building AI app"
2. Open Streamlit dashboard → cluster activation heatmap over turn timeline
3. **Vision smoke** — send an image of a diagram → occipital coordinator fans out, scene
   describer + OCR fire, parietal binds image features with prior text turn
4. **Hostility smoke** — hostile message → hypothalamus threat → GABA spikes → frontal
   inhibition damps drafts → response is hesitant/careful
5. **Novelty smoke** — novel topic → novelty cell fires → ACh spikes → hippocampus
   encodes with high salience
6. **Memory smoke** — same topic next turn → hippocampus recall queries second brain →
   response references prior episode. Verify: no other cluster touched `second_brain/`
   directly (audit imports — only `brain/clusters/hippocampus.py` may import `brain/second_brain/store.py`)
6b. **Schema audit** — open `second_brain/schema/` in a file browser → confirm facts
    are human-readable, accurate, and updated after a session
7. **Arousal smoke** — 5 rapid turns → arousal up → articulation timeout shortens →
   responses get terser
8. **Cost smoke** — typical turn under $0.10 on Haiku 4.5 with caching; worst-case turn
   under $0.50 (brainstem budget enforced)
9. **Loop smoke (adversarial)** — craft input that triggers A→B→A → hop limit kills it
   within 3 hops
10. **Coordinator-scope audit** — grep every cluster file, verify coordinators only
    subscribe to their own cluster's topics + `neuromod.*` + `attention.focus`
11. **Switch-only smoke** — send "hi" → 0 LLM calls fired → canned response returned by
    a frontal template switch. Verify Langfuse shows zero LLM cost for the turn
12. **Hebbian-weight smoke** — repeat a question pattern across 5 turns where the user
    expresses high valence to a particular drafter's style → verify that drafter's
    inbound edge weights have increased in `wiring.json`
13. **Predict-and-surprise smoke** — establish a conversation pattern across 5–6 turns
    on a topic. On the 7th turn, ask another routine question on the same topic.
    Verify: temporal predictor's prediction matches actual switch outputs, integrator
    is suppressed, response ships with 2 LLM calls instead of 4. Cost halved.
14. **Surprise-wakes-integrator smoke** — same setup, but turn 7 introduces an entirely
    new topic. Predictor error high → integrator wakes → hippocampus encoder fires
    (high salience). Verify episode written to second brain.
15. **Inhibition smoke** — verify that GABA-inhibitory edges actually subtract from
    downstream activation (not just modulate via global flag). Send hostile input,
    confirm drafter count drops to 0 if GABA > θ, forcing the executive to choose
    a "decline/deflect" template.

**Demo of emergence to aim for:** the entity should produce different response styles
depending on prior valence (Hebbian edge-weight learning across sessions), remember user
facts across sessions (hippocampus schema persists to disk), hesitate or refuse on
hostile input (hypothalamus switches → GABA → frontal inhibition edges), respond
instantly with no LLM cost on trivial input (switch-only paths via templates), **spend
less compute as a conversation becomes familiar (predict-and-surprise gating lets the
predictor handle routine turns)**, and sometimes blurt under time pressure (brainstem
articulation timeout) — **without any single agent being told to do any of these things
directly. Most of the behavior is in the wiring of cheap switches, not in expensive
reasoning.**

---

## UI Plan — MRI-Style Brain Visualizer

### What this adds
A browser-based chat interface with a side-view brain SVG that lights up each region (MRI glow effect) as its cluster fires during a turn. Dark theme, real-time, no build step.

### Tech stack
**FastAPI + WebSocket + single self-contained HTML file.** Streamlit is already in deps but cannot do real-time partial updates — it re-renders the whole page on any state change. Cluster activations fire mid-turn sequentially, requiring sub-second granular updates. FastAPI runs on the same asyncio event loop as the brain session (no IPC, no threads, no Redis). `uvicorn` is already installed; only `fastapi` needs to be added to `pyproject.toml`.

### Architecture
```
browser ──WebSocket──▶ brain/ui/server.py ──Queue──▶ brain/run.py (process_turn)
  ↑ activation events, neuromod updates, turn events               │
  └───────────────────────────────────────────────────────────────┘
```
- `process_turn()` wraps each cluster call with `await emitter.emit(cluster, intensity, note)` before and after
- `ActivationEmitter` (singleton) puts events on an `asyncio.Queue`
- FastAPI WebSocket handler drains the queue and broadcasts JSON to all connected clients
- Browser also **sends** messages over the same WebSocket: `{"type":"user_message","text":"..."}`
- `--ui` flag replaces the stdin REPL with the WebSocket-driven loop; prints `UI at http://localhost:8765`

### Brain SVG — side view left hemisphere, viewBox="0 0 500 380", bg #0a0a14

| Region | Shape | Approx center / size | Cluster | Glow color |
|---|---|---|---|---|
| Frontal lobe | ellipse | cx:155, cy:125, rx:105, ry:85 | frontal | #4488ff |
| Parietal lobe | ellipse | cx:290, cy:85, rx:80, ry:65 | parietal | #22aaff |
| Occipital lobe | ellipse | cx:400, cy:130, rx:65, ry:70 | occipital | #cc44ff |
| Temporal lobe | ellipse | cx:220, cy:230, rx:100, ry:50 | temporal | #ff8800 |
| Thalamus | ellipse (inner) | cx:260, cy:185, rx:32, ry:25 | thalamus | #ffffff |
| Hypothalamus | ellipse (inner) | cx:248, cy:222, rx:25, ry:18 | hypothalamus | #ffaa00 |
| Hippocampus | ellipse (inner, wide) | cx:315, cy:222, rx:52, ry:20 | hippocampus | #00ffaa |
| Cerebellum | ellipse | cx:405, cy:278, rx:52, ry:42 | (brainstem) | #00ccff |
| Brainstem | rounded rect | x:215, y:285, w:52, h:75 | brainstem | #ff6600 |

Base fill: `rgba(20,40,80,0.55)`. Active: CSS `.active` → `filter: brightness(3) drop-shadow(0 0 20px <color>)`. `transition: all 0.25s ease`. Glow intensity scales with `intensity` (0–1) via inline CSS variable.

### Activation event format (WebSocket JSON)
```json
{"type":"activation", "cluster":"frontal", "intensity":0.9, "note":"drafting response", "turn_id":"a1b2c3"}
{"type":"activation", "cluster":"frontal", "intensity":0.0, "note":"done", "turn_id":"a1b2c3"}
{"type":"neuromod", "ACh":0.42, "DA":0.61, "GABA":0.08, "Glu":0.35}
{"type":"emotion", "emotion":"curious"}
{"type":"turn_start", "turn_id":"a1b2c3", "user_input":"What was that project?"}
{"type":"turn_end",  "turn_id":"a1b2c3", "response":"...", "elapsed_s":3.2, "llm_calls":4}
{"type":"stream_thought", "thought":"I keep returning to the memory architecture..."}
```
Browser → server: `{"type":"user_message","text":"Hello"}` over the same WebSocket.

### UI layout
```
┌─ top bar ──────────────────────────────────────────────────────────────────────┐
│  🧠 Brain · session: a1b2c3  emotion: [curious]  llm calls: 4  elapsed: 3.2s  │
├──────────────────────────────────────┬─────────────────────────────────────────┤
│  BRAIN SVG (500×380)                 │  CHAT PANEL                             │
│  · regions glow as clusters fire     │  · scrollable turn history              │
│  · active region label below SVG     │  · user msgs right-aligned              │
│                                      │  · brain responses left-aligned         │
│  NEUROMOD BARS                       │  · typing indicator (animated dots)     │
│  ACh ████░░░ 0.42  [cyan]           │                                         │
│  DA  ██████░ 0.61  [gold]           │  ┌──────────────────────────────────┐   │
│  GABA█░░░░░░ 0.08  [red]            │  │  Type a message...           [→] │   │
│  Glu ████░░░ 0.35  [green]          │  └──────────────────────────────────┘   │
└──────────────────────────────────────┴─────────────────────────────────────────┘
```
- CSS Grid 2-column: left 540px fixed, right flex-grow
- Emotion badge: pill whose background shifts (neutral=gray, curious=blue, joy=green, anxious=amber, agitated=red)
- If DMN active: frontal + parietal show slow dim pulse between turns

### Files to create / modify

**New:**
- `brain/ui/__init__.py` — empty
- `brain/ui/emitter.py` — `ActivationEmitter` singleton with `emit()`, `emit_neuromod()`, `emit_turn_start()`, `emit_turn_end()`, `get_queue()`
- `brain/ui/server.py` — FastAPI: `GET /` serves index.html; `WebSocket /ws` drains emitter queue + receives user messages; `start(host, port)` coroutine; connected-client set for broadcast
- `brain/ui/index.html` — self-contained (~450 lines): inline CSS + inline JS + SVG brain; WebSocket client; chat rendering; neuromod bars; no external CDN

**Modified:**
- `brain/run.py` — add `--ui` / `BRAIN_UI=true`; start `server.start()` as background task; wrap every cluster call with emitter before/after; WebSocket-driven message loop replaces stdin REPL when `--ui`
- `pyproject.toml` — add `fastapi>=0.110.0`

### Activation intensities
| Cluster | Intensity | Note |
|---|---|---|
| temporal | 0.7 | "parsing input" |
| hypothalamus | 0.6 | "updating affect" |
| thalamus | 0.55 | "routing attention" |
| occipital | 0.9 | "processing image" |
| hippocampus | 0.75 | "recalling memory" |
| hippocampus (post) | 0.45 | "encoding episode" |
| frontal | 0.9 | "drafting response" |
| brainstem | 0.4 | "articulating" |

### Verification
1. `uv pip install fastapi` installs cleanly
2. `uv run python -m brain.run --ui --message "Hi"` → prints `UI at http://localhost:8765`, returns response, exits
3. `uv run python -m brain.run --ui` → open browser; SVG renders; send "Hi" → temporal + hypothalamus + frontal glow in sequence; neuromod bars update; response appears in chat
4. Send "What was that AI project?" → hippocampus region lights up (memory recall)
5. Send hostile message → hypothalamus (orange) + thalamus glow; frontal at lower intensity (defuse path)
6. Run with `--dmn` → frontal + parietal show slow dim pulse between turns

### Out of scope
3D brain, mobile layout, auth, voice in browser, replay/scrub past turns, Streamlit dashboard changes

---

## Open Decisions for You

Before implementation starts:
1. **Mac Mini spec** — which model + how much RAM? This determines whether phase 1
   hybrid is comfortable (M2 Pro 32GB / M4 24GB+) or whether you stay all-cloud longer
2. **Cheapest cloud provider for specialists**: Gemini Flash-Lite (cheapest) vs
   all-Anthropic (simpler ops, one key, prompt caching consistency)?
3. **Vision priority**: is image input a v0.1 must, or defer occipital to v0.2 and ship
   text-only first?
4. **Voice priority**: voice I/O in v0.1 (right now, exciting and biologically faithful)
   or v0.2 (after text version is solid)? Voice adds ~$0.30/session.
5. **ElevenLabs voice selection**: pick a default voice for the entity. Multiple
   personalities (different voices per drafter) or one consistent voice?
6. **Second brain seeding**: empty start, or seed `second_brain/schema/` with existing
   notes (Obsidian vault import) so the entity knows things about you from day one?
7. **Demo audience**: just you, or eventually shared? (Affects auth/gating, may keep you
   on cloud-only longer)
8. **Success criterion comfort**: OK with "interesting traces" as v0.1 win, or holding
   out for "actually smart"?
