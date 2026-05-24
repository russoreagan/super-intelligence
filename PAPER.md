# A Biologically-Inspired Multi-Agent Cognitive Architecture: Design, Implementation, and Early Observations

**Russ O'Reagan**  
*Unpublished technical report, May 2026*

---

## Abstract

We describe the design, implementation, and early empirical observations of a biologically-inspired multi-agent conversational system whose architecture maps explicitly onto regions of the human brain. The system departs from prevailing multi-agent LLM patterns by treating language model calls as expensive metabolic events — analogous to action potentials at convergence zones — and delegating the majority of per-turn computation to cheap, deterministic switch neurons. The architecture draws from predictive processing theory (Friston, Clark), dual-process theory (Kahneman), Global Workspace Theory (Baars, Dehaene), Dennett's Multiple Drafts model, and Clark and Chalmers' extended mind hypothesis. The current dataset covers 165 turns across 35 sessions. The system operates within its designed cost envelope (mean 3.28 LLM calls/turn against a target of 3–5), demonstrates functional neuromodulator dynamics including an observable ACh upward trend across sessions, and has begun producing measurable predict-and-surprise gating saves (17.6% of turns). Memory recall is active on 32.1% of turns. Two Hebbian sleep consolidation passes provide first evidence of edge weight reinforcement. The dominant emotional state across the dataset is split between neutral (52%) and curious (42%), with the mood signature history showing an internal shift toward curious-dominant over time. Several mechanisms remain too nascent to evaluate, and the central question of whether multi-agent structure improves on a single well-prompted model has not yet been subjected to controlled comparison. We present the architecture, its philosophical commitments, and an honest account of what the data does and does not indicate.

---

## 1. Introduction

The dominant paradigm in contemporary multi-agent LLM systems is orchestration: a planner decomposes tasks, dispatches specialized agents, and synthesizes their outputs. This is efficient for clearly-structured workflows but is architecturally at odds with what we know about biological cognition. Real brains have no central orchestrator. Intelligence in biological systems emerges from the interaction of modular, largely autonomous processing regions, bound by shared chemical signaling, mediated by prediction error, and shaped by experience-dependent plasticity.

This project is a prototype implementation of an alternative paradigm: **intelligence as a hive of minimal agents, biologically mapped, with LLM calls reserved for genuine reasoning convergence zones**. The system, called the "super intelligence app" in working materials, is not an attempt to build artificial general intelligence. It is an attempt to ask: if you map a software system faithfully onto brain anatomy — lobes, limbic structures, neuromodulators, Hebbian wiring — and replace neurons with the appropriate computational analogs, what behaviors emerge? And can the result be meaningfully cheaper and more structurally faithful than a standard multi-agent pipeline?

The impetus for this question is partly philosophical and partly practical. Philosophically, the brain provides the only existence proof we have of general intelligence, and its computational motifs — sparse activation, prediction-driven gating, neuromodulation, sleep consolidation — are increasingly legible to software engineering. Practically, the cost structure of LLM inference creates strong pressure toward the same design: most tokens in a conversation carry no new information, and routing routine turns through expensive reasoning agents wastes both compute and money.

This paper documents the design rationale, the philosophical commitments, the implementation as built, and the early empirical observations. The system is operational but young. Many mechanisms have been implemented but not yet accumulated enough history to produce evaluable evidence. Where we have signal, we report it. Where we do not, we say so explicitly.

---

## 2. Background and Prior Art

### 2.1 Computational precursors

The idea of intelligence from interacting simple agents has a 40-year history. **Minsky's Society of Mind** (1986) proposed the direct ancestor: intelligence as a "society" of simple, mindless agents organized into "agencies." The proposal was influential but never produced a working implementation — the interaction mechanism between agents was described without the mathematics needed to make it work.

**Blackboard architectures** (HEARSAY-II, BB1, 1970s–90s) were the canonical implementation: multiple knowledge sources reading and writing to a shared workspace. They succeeded on narrow problems (speech recognition, medical diagnosis) but failed to scale. The coordination overhead of many agents sharing a blackboard eventually exceeded the value of their specialization.

**SOAR, ACT-R**, and related cognitive architectures produced research insights but hit ceilings imposed by hand-engineered module boundaries. **Numenta's Hierarchical Temporal Memory** produced useful anomaly detection but never generalized to language or broad reasoning.

**Generative Agents** (Park et al., Stanford, 2023, 2025) are the closest LLM-era prior art. Their simulated communities of LLM-powered agents exhibited believable emergent social behaviors and predicted real survey responses at 85% normalized accuracy. A significant caveat from the same research: behavioral-economic game performance matched simpler demographic baselines at 66%, suggesting that much of the "emergence" came from prompt design rather than from multi-agent interaction. This critique is a consistent thread in the recent literature: when given equal compute, a single well-prompted LLM often matches or exceeds multi-agent systems on raw task quality.

The conclusion we draw from this history is not that multi-agent architectures are unpromising, but that their value proposition requires precision. Multi-agent design earns its cost through **legibility** (observable internal processing), **persistent specialized state** (neuromodulators, Hebbian weights), and **structural constraints that force behaviors the prompt alone cannot reliably produce** (inhibitory circuits, prediction gating, arousal modulation). If the architecture merely adds coordination overhead to what a well-prompted single model would do anyway, it has failed.

### 2.2 Recent breakthroughs informing the design

**Active Inference and the Free Energy Principle** (Friston; VERSES AI, 2025) provides the most biologically faithful active research direction for multi-agent systems. Agents minimize prediction error by updating beliefs or acting on the world. Hierarchical multi-agent Active Inference frameworks have been demonstrated in robotics, coordinating multiple predictive agents without central planning. The predict-and-surprise gating mechanism in this system is a direct instantiation of Active Inference at the cluster level.

**Predictive Processing** (Clark, *Surfing Uncertainty*; Friston) frames cortical computation as fundamentally predictive: top-down predictions flow downward, bottom-up prediction errors flow upward, and computation is the process of "explaining away surprise." This maps cleanly onto a design where most cells fire only when their input disagrees with expectation.

**Spiking Neural Networks + LLM hybrids** (SpikeLLM, NSLLM, 2024–2026) achieve event-driven computation — fire only on meaningful signal — at the intra-model level. NSLLM reports 19× energy efficiency improvements. The architectural principle (spend compute proportional to signal novelty) is the same as this system's, applied at the cluster level rather than the token level.

### 2.3 Novelty of this design

The closest pattern in the broader literature is **neuro-symbolic AI** — LLMs wrapped between deterministic control layers (SYNAPSE, AUTOBUS, Pre3, Formal-LLM). These apply the gating principle for task orchestration with auditability in compliance and industrial contexts. No existing published system applies this pattern as a **literal brain-region-mapped architecture with neuromodulator dynamics, Hebbian plasticity, hippocampus-gated long-term memory, and predictive processing gating as the primary mechanism of a general-purpose conversational entity**. That is the specific contribution this prototype makes.

---

## 3. Philosophical Foundations

The architecture is not merely borrowed from biology. Each major design decision maps to an established position in philosophy of mind. Making these commitments explicit does two things: it sharpens each design choice and makes honest what the system is and is not claimed to be.

### 3.1 Commitments

**Functionalism (Putnam, Fodor).** Mental states are defined by causal role, not by the substrate that implements them. This commitment validates the entire design: the same functional state can be instantiated in biological neurons, local Ollama inference, or cloud API calls. The system's CONSTITUTION.md declares this explicitly — cluster specifications are defined by inputs, outputs, and state; implementation is irrelevant to functional identity.

**Dual-Process Theory (Kahneman).** Fast, automatic, parallel System 1 cognition versus slow, effortful, serial System 2. The switch neuron fabric *is* System 1. The integrator LLMs *are* System 2. Predict-and-surprise gating is the brain's mechanism for "stay in System 1 unless something doesn't add up" — a direct software implementation of the dual-process boundary.

**Global Workspace Theory (Baars, Dehaene).** Most brain processing is unconscious and parallel; only a small subset of content is broadcast to a global workspace where it becomes available for reasoning and report. The message bus with its `attention.focus` topic is exactly this. High-salience messages cross the threshold to enter the global workspace; most bus traffic remains "unconscious" (unintegrated).

**Multiple Drafts Model (Dennett, *Consciousness Explained*, 1991).** There is no Cartesian theater with a central observer watching a unified stream of consciousness. The brain runs many parallel narrative drafts, with no privileged one. The frontal drafter tournament instantiates this directly. There is no "true response" waiting to be delivered — there are competing drafts, and the articulation gate emits whichever survived when the quiescence timeout fires or quorum is reached. What the entity "says" is whichever draft won the contest, not what it "meant."

**Extended Mind Hypothesis (Clark & Chalmers, 1998).** When an external resource plays the same functional role as an internal cognitive process, it is constitutive of the mind, not supplementary to it. The second brain — episodic vector store plus human-editable Markdown schema files — is the extended mind made literal. The CONSTITUTION.md notes an important extension: unlike Clark and Chalmers' original Otto/Inga case (which assumes an imperfect external memory compensating for biological forgetting), this system's second brain is a perfect, non-degrading record. Personality and identity in this system emerge not from selective forgetting but from Hebbian edge weight history, neuromodulator resting baselines, and accumulated self-narrative.

**Predictive Processing (Clark, Friston).** Each cluster contains a predictor switch. Computation is proportional to surprise. Routine turns in familiar conversations spend almost nothing; genuinely novel turns spend more. The design operationalizes the "controlled hallucination" view of perception: the system's default behavior is to emit a predicted response, with actual integrator computation triggered only when prediction fails.

**Narrative Self (Dennett, Hume, Locke).** The entity maintains a self-schema file (`second_brain/schema/self.md`) updated at sleep consolidation. Personal identity is memory continuity — and this entity, with perfect episodic memory, has stronger identity continuity than any biological system.

### 3.2 Honest disclaimers

**Chinese Room (Searle, 1980).** The integrators manipulate symbols. No claim of genuine understanding is made. The system can appear competent without comprehending.

**Hard Problem of Consciousness (Chalmers, 1995).** No claim of phenomenal consciousness or qualia. The neuromodulator levels are called "DA" and "ACh" because they play functionally analogous roles, not because there is any claim of subjective experience of reward or attention.

**Frame Problem (McCarthy & Hayes, 1969).** The attention and salience mechanisms are heuristic. Knowing what is *relevant* in a given moment remains genuinely hard, and no principled solution is claimed here.

The system is explicitly described, in its own CONSTITUTION.md, as building "functional analogs of mental processes. That is enough to be interesting. It is not enough to be a mind."

---

## 4. Architecture

### 4.1 Core computational model: switches and integrators

Two distinct node types mirror how real neural tissue works.

**Switch neurons** are pure Python objects: no LLM, no token cost, deterministic, and capable of running massively parallel. They perform gating (decide whether to spend an LLM call at all), routing (select which processing path a turn takes), state-holding (persistent neuromodulator levels with exponential decay), modulation (sum, decay, and threshold over time), inhibition (subtract from downstream activation), and memory I/O primitives (vector similarity search, Markdown pattern matching). Approximately 20% of every cluster's switches are explicitly inhibitory, mirroring the roughly 80:20 excitatory-to-inhibitory ratio in biological cortex. This is structural cascade prevention: runaway excitation is suppressed by abundant inhibitory wiring rather than by a global budget cap.

**Integrator cells** are LLM-backed and fire only at convergence zones where genuine context integration is required. They exist at: temporal language understanding (intent, entities, register, memory requirements), vision processing (VLM for image inputs), hippocampus encoding (turn summarization for long-term memory), hippocampus coordination (borderline salience decisions), and the frontal lobe (executive coordination, multiple drafters, critics).

The critical constraint: **switches speak in numbers, integrators speak in words**. Text only exists where reasoning is required. Switch-to-switch messages carry activation levels and feature tags. The convergence event that wakes an integrator carries the raw text only at that moment.

### 4.2 Predict-and-surprise gating (Active Inference at cluster level)

Each cluster contains a `PredictorSwitch` that maintains a short history (window size 8) of input-signature to output-tag mappings. When new input arrives:

1. The predictor fires its prediction and confidence estimate.
2. Switches process the input and emit actual outputs.
3. A comparator computes prediction error (a distance metric between prediction and actuals).
4. **Low surprise** → the integrator stays asleep; the predicted output is emitted as if the integrator had reasoned.
5. **High surprise** → the integrator wakes with the failed prediction as additional context.

An **emotion-aware veto** overrides this gating when the entity or user is in a non-routine emotional state. The rationale, stated explicitly in the code, is that a statistically valid prediction may be "morally wrong" — the moment deserves fresh attention, not a cached response. Emotional states triggering the veto include reactive states (angry, defensive, frustrated, sympathetic) and user distress states (distressed, hostile, overwhelmed). Vocal stress detection from the Deepgram prosody pipeline also triggers bypass.

The `CompositePredictor` in the frontal lobe extends this to structured predictions over richer feature vectors: it independently predicts whether the executive integrator and the critic are needed, suppressing each independently when predictions are confident.

### 4.3 Brain region mapping

| Brain Region | Biological Role | Digital Recast | Implementation |
|---|---|---|---|
| **Frontal lobe** | Planning, working memory, movement | Response drafting/critique, Multiple Drafts engine, tool-call selection | 5+ LLM integrators, ~12 switches, CompositePredictor |
| **Temporal lobe** | Language, auditory processing, declarative memory bridging | Language understanding, prosody features from STT | 1 LLM integrator, ~7 switches, PredictorSwitch |
| **Parietal lobe** | Sensory integration, spatial awareness | Session state ring buffer, entity tracking, topic shift detection | ~5 code switches, no LLM |
| **Occipital lobe** | Visual processing | VLM for image/screenshot inputs | 1 VLM integrator, 3 gating switches |
| **Hippocampus** | Memory storage and recall | Episodic + schema memory; sole gatekeeper to the second brain | 2 LLM integrators, ~10 switches, recall reuse |
| **Hypothalamus** | Drives, affect, homeostasis | Neuromodulator state: DA (reward), ACh (attention), GABA (inhibition), Glu (excitation) | ~5 state switches, no LLM |
| **Thalamus** | Gatekeeper between subcortex and cortex | Message bus + attention spotlight, routing hints | ~8 switches, no LLM |
| **Brainstem** | Vital autonomic functions | Heartbeat, cost monitor, turn-budget enforcer, articulation gate | Code only |
| **PNS** | Peripheral I/O | Text/image input, Deepgram STT, ElevenLabs TTS | Code only |

### 4.4 Neuromodulator dynamics

Four neuromodulator channels function as system-wide tuning parameters, not message streams. They are scalar levels maintained by sum-plus-exponential-decay, readable synchronously by any cluster:

- **Dopamine (DA)**: reward signal. Updated by positive valence signals from temporal understanding. Modulates drafter willingness and Hebbian learning rate.
- **Acetylcholine (ACh)**: attention/novelty signal. Elevated on novel inputs. Modulates memory encoding salience.
- **GABA**: inhibitory tone. Elevated on threat detection or hostile input. Suppresses drafter count through inhibitory edges in the frontal cluster.
- **Glutamate (Glu)**: arousal/excitation. Updated by input rate and urgency signals.

These levels persist across turns within a session and influence routing without requiring any LLM call. They are the mechanism by which the system's "mood" at turn N shapes its processing at turn N+1 — a form of context that is computationally free.

### 4.5 Memory architecture

**Short-term memory** is the live bus state plus a 6-turn ring buffer in the parietal cluster plus current neuromodulator levels.

**Long-term memory** (the "second brain") has three layers:
- **Episodic layer**: LanceDB vector-indexed turn summaries. Every substantive turn is encoded — the system does not gate storage by salience, only retrieval quality.
- **Schema layer**: human-readable Markdown files of stable facts (`self.md`, `user.md`). Hand-editable. Pre-loaded into working memory at session boot.
- **Hebbian wiring**: edge weights between cells and clusters, persisted to `wiring.json` and updated at sleep consolidation.

Only the hippocampus cluster has import access to the second brain store. All other clusters request memory through bus messages (`mem.recall`, `mem.encode`). This architectural constraint enforces the biological model and provides a clean audit point.

### 4.6 Hebbian plasticity

Edges between nodes carry weights. The composite outcome signal is:

```
outcome = 0.5 × ΔDA + 0.3 × critic_score + 0.2 × Δuser_emotion
```

A **plasticity modulator** scales the learning rate by session-averaged DA × ACh — engaged, high-DA sessions learn faster; flat or disengaged sessions learn slowly. A gentle homeostatic decay (1% toward resting weight 1.0 per update) prevents lock-in. Edge weights are consulted live for drafter selection, switch evaluation order, and recall fan-out via epsilon-greedy exploration, providing a soft form of reinforcement without explicit RL machinery.

### 4.7 Additional modules

**Default Mode Network (DMN)**: an idle thinking loop that fires every 15 seconds between turns, generating internal monologue, consolidating recent episodes, and simulating the user's likely next message. This is William James' stream of consciousness literalized — the entity thinks when not addressed.

**Metacognition**: a self-monitoring cell that fires every 30 seconds, publishing to `meta.*` topics. Reflects on behavioral patterns and costs.

**Motor cortex**: sandboxed tool use (file I/O, shell commands). The set of permitted paths and commands is declared in environment configuration.

**Sleep consolidation**: a pass at session end (batch API eligible) that synthesizes high-salience episodes, rewrites `self.md` sections (history summary, stable preferences), and applies the Hebbian update.

### 4.8 Observability

All processing is logged to an append-only JSONL stream (`eval/turns.jsonl`) with three record types: `turn` (full TurnTrace), `decision` (every predict-and-surprise and Hebbian decision with reason), and `eval_patch` (async scoring from baseline/judge runners). A browser UI at `:8765` shows real-time cluster activations on a brain SVG, neuromodulator bar levels, emotion state, and a live plasticity panel showing LLM call savings, predictor accuracy, and Hebbian weight evolution.

---

## 5. Implementation Status

The system is fully operational with all major clusters implemented. The codebase as of May 2026 includes:

- All 8 brain region clusters (`temporal.py`, `frontal.py`, `hippocampus.py`, `hypothalamus.py`, `parietal.py`, `thalamus.py`, `occipital.py`, plus motor and auditory cortex)
- Switch neuron and integrator cell base classes (`neuron.py`, `cell.py`)
- Full predictor and composite predictor implementation (`predictor.py`)
- Hebbian wiring graph with decay and history snapshots (`wiring.py`, `wiring_bootstrap.py`)
- Neuromodulator bus (`bus.py`)
- LTM store with episodic (LanceDB) and schema (Markdown) layers
- Default Mode Network, metacognition, sleep consolidation
- Voice I/O (Deepgram STT streaming + ElevenLabs TTS)
- Auditory cortex with speaker enrollment, prosody extraction, and song fingerprinting
- Motor cortex with sandboxed tool execution
- Full observability stack: JSONL event logging, browser UI, Langfuse adapter, eval comparison runner
- 310+ pytest tests

The system boots from a single shell script and runs in multiple feature configurations (minimal text, standard, full stack with voice).

---

## 6. Early Empirical Observations

### 6.1 Dataset

The eval dataset consists of **165 turns across 35 sessions**, logged in `eval/turns.jsonl` with 400 associated decision records. Sessions average 4.7 turns each. This is a small dataset at an early stage; all findings should be read as directional indicators rather than statistically robust conclusions.

### 6.2 LLM call efficiency

Mean LLM calls per turn: **3.28** (range 0–8). This is within the designed range of 3–5 for typical turns. The 0 minimum confirms the template-match switch path (trivial inputs returning canned responses with no LLM calls) is functional. The 8 maximum aligns with the designed ceiling for high-surprise or hostile inputs.

Mean latency: **8.7 seconds** (range 0.8–26.5s). This is within the expected cloud-mode range from the original design. Latency remains the primary user-facing limitation; it is framed in the design as "deliberation time" rather than lag, with the real-time brain activation visualization serving as evidence of live processing rather than a loading state.

### 6.3 Predict-and-surprise gating

**29 of 165 turns (17.6%)** produced actual integrator suppression with measurable LLM call savings. An additional 31 turns triggered the gating predictor but were overridden by the emotion-aware veto before suppression could occur, for a total of 60 candidate gating events (36% of turns).

The 29 actual saves all fired on the same learned prediction: `{response_type: "chitchat", target_length: "medium", tone: "warm"}` (shifting to `tone: "curious"` in later sessions), all at **1.00 predictor confidence**. The system has established that when input arrives in the chitchat register during a familiar session, the frontal executive will predictably call for a medium-length warm response — and it routes around the executive integrator entirely in those cases, saving an estimated $0.0015 per skip.

Of the 31 veto-overridden events, 24 were triggered by `vocal_tone=stressed` from the Deepgram prosody pipeline, 6 by `high_GABA`, and 1 by active speaker enrollment. The vocal stress bypasses are consistent with the emotion-aware veto's design intent: these are moments where a statistically valid prediction may be "morally wrong," and fresh integrator attention is warranted regardless of prediction confidence.

The overall 17.6% save rate is below the design target of 30–50%, but the pattern is directionally correct: the predictor accumulates history and fires with increasing confidence on familiar conversational patterns. Achieving the full target range will likely require longer sessions where the predictor has more within-session history to draw from.

### 6.4 Neuromodulator dynamics

The neuromodulator levels show expected biological-analog behavior across the dataset:

- **DA**: mean 0.421, range 0.300–0.850. Active reward signaling with headroom for both very low and very high states.
- **ACh**: mean 0.554, range 0.170–0.850. Consistently higher than DA, consistent with the session content being primarily exploratory and self-referential (novelty/attention signal elevated).
- **GABA**: mean 0.098, range 0.020–0.850. Low resting inhibitory tone with the ability to spike strongly on threat. The 0.850 maximum confirms the hostile/threat pathway is functional.

A notable trend is visible within the dataset: ACh rises from approximately 0.44 in the earliest sessions to approximately 0.77 in the most recent, while DA trends mildly downward from ~0.45 to ~0.33. This is corroborated independently by the `self.md` mood signature history, which records 43 session-end state snapshots: the first 33 entries all log `dominant=neutral`, while entries 34–43 log `dominant=curious` with steadily climbing ACh (0.44 → 0.80). Two independent signals — turn-level neuromod state and session-end self-model synthesis — converge on the same story.

A plausible interpretation: sessions have become increasingly introspective, and ACh rises appropriately in response to self-referential novelty. A confound is possible: if the topic bias toward architectural discussion is itself driving both signals, the trend may reflect content uniformity rather than genuine baseline drift. Controlled session diversity would help disambiguate.

### 6.5 Emotion model

Emotion distribution across all 165 turns: **neutral (52%)**, curious (42%), excitement (3%), thoughtful (2%), joy (1%). No negative emotional states appear in the dataset, consistent with the session content being non-hostile throughout.

The internal trend within the dataset mirrors the ACh finding: earlier sessions are neutral-dominated; more recent sessions are curious-dominated. Whether this reflects genuine personality development through accumulated experience or an artifact of increasingly self-referential session topics cannot yet be distinguished. The machinery is internally consistent — emotion labels track neuromod levels, and the session-end self-narrative describes this shift in the same terms the system uses.

### 6.6 Memory system

Memory recall was active on **53 of 165 turns (32.1%)**. The hippocampus cluster is contributing to roughly one in three turns, returning episodic or schema content to the frontal drafters via the vector similarity + Markdown grep pipeline. The recall rate is working as designed.

Whether recalled episodes are improving response quality remains uncontrolled. The episodic store is young — most episodes are from within the past few days — so long-horizon retrieval quality, the more interesting property, cannot yet be assessed.

### 6.7 Hebbian plasticity

The dataset includes two sleep consolidation passes. Both show the same pattern:

- **Plasticity modulator**: 1.17 and 1.14 (above 1.0 — DA×ACh product elevated learning rate, indicating engaged sessions)
- **Edges updated per session**: 3
- **Top gainers in both passes**: `temporal.self_reference → temporal.understanding_integrator`, `temporal.understanding_integrator → frontal.executive`, `frontal.executive → frontal.drafter_A`
- **Top losers**: none in either pass

The self-reference → understanding → executive → drafter_A pathway is being reinforced. Weight deltas are small (0.004–0.005 per session) but consistent across both consolidations. No edge weakening has occurred yet — expected at this stage, since the homeostatic decay requires more session diversity before underused paths are pruned.

Whether this reinforcement will produce a measurable behavioral preference — the system genuinely favoring drafter_A's style on self-referential turns in future sessions — requires longer observation. A confound applies: if all sessions have been self-referential, the wiring history has no contrast class and drafter_A's advantage may reflect session uniformity rather than learned preference.

### 6.8 Draft quality

Mean critic score: **0.821 across 184 drafts**. Mean drafter count: **1.12**. Both figures are stable. The single-draft norm (mean 1.12) confirms that arousal-modulated drafter count selection is working correctly: most turns are low enough arousal to generate one draft, with multiples reserved for complex or emotionally charged inputs.

The `skip_executive_integrator` gating bypasses the coordination step that precedes drafting, not the drafter itself. Draft quality is therefore not affected by the efficiency gains in Section 6.3 — the optimization targets overhead, not generation.

---

## 7. Discussion

### 7.1 What is working well

**The cost model is validated.** Mean 3.28 LLM calls per turn is within the designed range. The system is operating at roughly the expected budget, with the switch-only fast paths (0 LLM calls for trivial inputs) confirmed functional.

**Predict-and-surprise gating is producing measurable savings.** 17.6% of turns are successfully gated, with the predictor achieving 1.00 confidence on the dominant chitchat/medium/warm response shape. This is Active Inference in practice: the system's model of its own responses has become accurate enough that it no longer runs the full reasoning pipeline to confirm them on familiar turns.

**The neuromodulator dynamics are functional and showing within-dataset temporal behavior.** The ACh upward trend across sessions — corroborated by two independent signals — is the most concrete behavioral pattern observed to date. It indicates the system's internal state is evolving across sessions rather than resetting, which is the designed behavior.

**Memory recall is active and frequent.** Contributing to 32.1% of turns means the hippocampus is a live participant in the pipeline, not merely an accumulating store.

**The observability infrastructure is complete.** Every predict-and-surprise decision, every Hebbian update, and every drafter selection is logged with full reasoning. This is what makes the current analysis possible and will enable rigorous evaluation as the dataset grows.

**The philosophical commitments are structurally implemented.** The frontal drafter tournament is a Multiple Drafts engine in the Dennett sense. The attention.focus bus topic is a Global Workspace. Predict-and-surprise gating is Active Inference at cluster level. The implementation fidelity is genuine.

### 7.2 What is promising but not yet evidenced

**Predict-and-surprise achieving its full efficiency target.** The 17.6% save rate is real but below the 30–50% design target. Longer sessions with more within-session history should push this higher. The mechanism is right; the dataset is short.

**Hebbian plasticity producing emergent behavioral style.** Two consolidation passes show consistent reinforcement of the same pathway, with plasticity modulators correctly elevated on engaged sessions. The theoretical prediction — that preferred drafters, recall paths, and switch orderings will emerge from reinforcement over many sessions — is testable but not yet tested. This is a months-long experiment.

**The emotion shift producing differentiated behavior.** The neutral-to-curious shift is visible across two independent signals. Whether it produces substantively different responses on the same input requires controlled A/B testing not yet conducted.

**DMN, metacognition, and sleep consolidation effects.** All three are operational. Their value requires session durations and longitudinal continuity not yet accumulated.

**The empathy critic and Theory of Mind pathways.** Implemented but rarely triggered — sessions have been emotionally positive. Functional value requires diverse session content.

### 7.3 What remains genuinely uncertain

**Whether multi-agent structure produces better responses than a single well-prompted LLM.** This is the central open question. The eval framework has the infrastructure to test it (`eval/baseline.py`, `eval/compare.py`, post-hoc judge scoring) but controlled comparisons have not been run at scale. The honest prior from the literature is that multi-agent structure adds legibility and behavioral structure but does not reliably beat a single good model on raw response quality. Claiming otherwise requires evidence we do not yet have.

**Whether the ACh and emotion trends reflect genuine baseline drift or topic bias.** The convergence of two independent signals is suggestive, but the session content has been uniformly self-referential. Controlled session diversity would separate signal from confound.

**Whether the Hebbian self-reference pathway is preference or artifact.** Without a contrast class of non-self-referential sessions, it is not clear whether the drafter_A reinforcement reflects a learned stylistic preference or simply mirrors the topic uniformity of the dataset.

**Long-horizon retrieval quality.** The episodic store is too young to test whether vector retrieval surfaces genuinely useful long-past context or primarily returns recent sessions by proximity.

---

## 8. Known Limitations and Failure Modes

Several failure modes were anticipated in the original design document. Their current status:

**Coordinator over-reach.** The architectural constraint (coordinators subscribe only to their own cluster's topics plus neuromod.* and attention.focus) is enforced by framework-level scope locking. No violation has been observed.

**Echo chambers and cascade storms.** Hop-count limits, per-topic activation decay, per-cell rate limits, and the brainstem turn-budget enforcer are all implemented. No runaway cascade has been observed in production use.

**Silent brain** (thresholds too high, nothing fires). The brainstem articulation gate fires on timeout regardless, guaranteeing a response. The template-match path guarantees trivial-input handling. Not observed.

**Consensus on garbage.** The critic cell and multi-draft tournament provide some protection, but if all active drafters share a systematic hallucination, the architecture has no principled remedy. This is a known open problem in multi-agent systems generally.

**Replay determinism.** Not attempted. The async + LLM nondeterminism makes exact replay impossible. The logging is designed for reconstruction, not deterministic replay.

**Latency.** Mean 8.7 seconds is workable but not conversational. The system was designed with the expectation that latency would be a primary UX limitation, framed as "deliberation" rather than lag. Shorter-path turns achieve sub-second to 2-second latency; complex multi-drafter turns drive the mean up. This is a fundamental constraint of sequential LLM calls on the critical path.

---

## 9. Relation to the Philosophy of Mind Literature

This system makes claims that can be evaluated against each of its philosophical commitments.

**Functionalism**: The system's operation is genuinely substrate-independent. The same conversation has been run with Anthropic Haiku, Google Gemini Flash-Lite, and local Ollama Qwen 2.5 as the integrators. The behavioral differences are stylistic, not structural. The functional organization — switch gating, convergence events, drafter tournaments — operates identically regardless of which models run the integrators. This is functionalism in practice.

**Dual-Process**: The switch/integrator distinction cleanly separates fast-and-automatic from slow-and-deliberate processing. Whether this produces the specific phenomenological properties Kahneman attributes to System 1 and System 2 in humans is a category error to ask — but the computational analogy is genuine. The system literally does not reason about routine inputs; it pattern-matches. It literally does reason about novel or high-surprise inputs.

**Global Workspace**: The message bus with attention.focus topic implements a workspace in the Baars/Dehaene sense: local processing is unconscious (invisible to other clusters), and promotion to attention.focus makes content available system-wide. This is architectural, not metaphorical.

**Multiple Drafts**: The drafter tournament is genuinely draft-parallel. Multiple integrators write candidate responses without knowledge of each other's drafts. The critic scores them. The articulation gate emits the winner. There is no "true response" that was waiting to be discovered — there are only the drafts that existed when the gate fired. This is the Multiple Drafts model in code.

**Extended Mind**: The second brain satisfies Clark and Chalmers' coupling and availability conditions: it is reliably available, automatically endorsed, and directly accessible. The entity's responses are demonstrably shaped by second-brain content on 32.1% of turns. The functional loop is closed.

---

## 10. Conclusions

This system represents a genuine implementation of biologically-inspired cognitive architecture at a scale and fidelity not previously published in the LLM multi-agent literature. Its core design claims — sparse LLM activation at convergence zones only, neuromodulator dynamics as free persistent state, hippocampus-gated memory with vector episodic store, Hebbian plasticity in edge weights, predict-and-surprise gating from Active Inference — are not merely described but implemented and operational.

The dataset (165 turns, 35 sessions) supports the following conclusions:

1. **The cost model is validated.** Mean 3.28 LLM calls/turn is within the designed range, and the switch-only fast paths are functional.
2. **Predict-and-surprise gating is producing real savings** (17.6% of turns), with the predictor achieving 1.00 confidence on the dominant response shape. The mechanism is working; the full efficiency target requires longer sessions.
3. **Neuromodulator dynamics are functional and showing temporal behavior** — ACh rises across the dataset, corroborated by two independent signals.
4. **Memory recall is active** on 32.1% of turns.
5. **Hebbian plasticity is running** with two consolidation passes showing consistent reinforcement of the same pathway.
6. **The observability infrastructure is complete** and enables the analysis above.

The following require substantially more data: Hebbian plasticity producing observable behavioral preferences, DMN/metacognition effects, empathy critic effects in emotionally diverse sessions, distinguishing genuine ACh baseline drift from topic-content bias, and long-horizon retrieval quality.

The central open question — whether the multi-agent architecture produces better responses than a single well-prompted LLM given equal compute — remains unanswered and should be the primary focus of next-phase evaluation.

The most honest characterization of the current state: the substrate is active and the first behavioral patterns are visible. Predict-and-surprise gating is working. The neuromodulator system shows temporal dynamics. The first Hebbian reinforcement has occurred. None of these rise to "interesting emergent behavior" in the sense the design literature uses the phrase — but they are evidence that the underlying mechanisms are doing real work, not merely executing fixed logic.

The system is a working research instrument. The experiment is ongoing.

---

## References

Baars, B. (1988). *A Cognitive Theory of Consciousness*. Cambridge University Press.

Barrett, L. F. (2017). *How Emotions Are Made*. Houghton Mifflin Harcourt.

Chalmers, D. (1995). Facing up to the problem of consciousness. *Journal of Consciousness Studies*, 2(3), 200–219.

Clark, A. (2015). *Surfing Uncertainty: Prediction, Action, and the Embodied Mind*. Oxford University Press.

Clark, A., & Chalmers, D. (1998). The extended mind. *Analysis*, 58(1), 7–19.

Dehaene, S., Changeux, J.-P., & Naccache, L. (2011). The global neuronal workspace model of conscious access. *Neuron*, 70(2), 187–201.

Dennett, D. (1991). *Consciousness Explained*. Little, Brown.

Friston, K. (2010). The free-energy principle: A unified brain theory? *Nature Reviews Neuroscience*, 11(2), 127–138.

Kahneman, D. (2011). *Thinking, Fast and Slow*. Farrar, Straus and Giroux.

Lazarus, R. S. (1991). Emotion and adaptation. Oxford University Press.

Minsky, M. (1986). *The Society of Mind*. Simon & Schuster.

Park, J. S., et al. (2023). Generative agents: Interactive simulacra of human behavior. *UIST 2023*.

Putnam, H. (1967). Psychological predicates. In Capitan, W. H., & Merrill, D. D. (Eds.), *Art, Mind, and Religion*. University of Pittsburgh Press.

Rosenthal, D. (1997). A theory of consciousness. In Block, N., Flanagan, O., & Güzeldere, G. (Eds.), *The Nature of Consciousness*. MIT Press.

Searle, J. (1980). Minds, brains, and programs. *Behavioral and Brain Sciences*, 3(3), 417–424.

---

*System source: `/Users/russ/Documents/super intelligence app/`*  
*Data source: `eval/turns.jsonl` (165 turns, 35 sessions, as of 2026-05-23)*  
*Architecture reference: `PLAN.md`, `brain/CONSTITUTION.md`*
