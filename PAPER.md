# A Biologically-Inspired Multi-Agent Cognitive Architecture: Design, Implementation, and Early Observations

**Russ O'Reagan**  
*Unpublished technical report, May 2026*

---

## Abstract

We describe the design, implementation, and early empirical observations of a biologically-inspired multi-agent conversational system whose architecture maps explicitly onto regions of the human brain. The system departs from prevailing multi-agent LLM patterns by treating language model calls as expensive metabolic events — analogous to action potentials at convergence zones — and delegating the majority of per-turn computation to cheap, deterministic switch neurons. The architecture draws from predictive processing theory (Friston, Clark), dual-process theory (Kahneman), Global Workspace Theory (Baars, Dehaene), Dennett's Multiple Drafts model, and Clark and Chalmers' extended mind hypothesis. After 91 turns across 27 sessions, the system operates within its designed cost envelope (mean 3.48 LLM calls/turn against a target of 3–5), demonstrates functional neuromodulator dynamics, and has accumulated initial Hebbian edge weight history. Predict-and-surprise gating has fired on 34% of candidate turns, though the emotion-aware veto — particularly vocal stress detection — has suppressed most of those saves. Memory recall is active (26/91 turns, 28.6%). Several mechanisms remain too nascent to evaluate. We present the architecture, its philosophical commitments, and an honest account of what the early data does and does not indicate.

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

The current eval dataset consists of **91 turns across 27 sessions**, logged in `eval/turns.jsonl` with 190 associated decision records. This is a small dataset, and the observations below should be read as directional indicators rather than statistically robust conclusions.

### 6.2 LLM call efficiency

Mean LLM calls per turn: **3.48** (range 0–8). This is within the designed range of 3–5 for typical turns. The minimum of 0 confirms that the template-match switch path (trivial inputs returning canned responses) is functional. The maximum of 8 aligns with the designed upper bound for high-surprise or hostile inputs. The distribution is well-behaved.

Mean latency: **9.2 seconds** (range 0.8–23.6s). This is within the expected cloud-mode range cited in the original design (3–5s for routine, up to ~24s for complex turns with multiple LLM calls in sequence). Latency is currently the most significant user-facing limitation.

### 6.3 Predict-and-surprise gating: early and mixed signals

**34% of turns** generated a candidate gating event (predictor confidence high enough to suggest suppressing the integrator). Of these 31 candidate events, **27 (87%) were overridden by the emotion-aware veto** and only 4 (13%) resulted in actual integrator suppression. The net LLM call savings to date: mean **0.04 per turn**.

This result requires careful interpretation. The emotion-aware veto broke down as follows: **23 of 27 bypasses were triggered by `vocal_tone=stressed`** from the Deepgram prosody pipeline, with the remaining 4 triggered by `high_GABA`.

Two competing explanations exist:
1. **The vocal stress detector is too sensitive**, triggering bypass on ordinary conversational speech patterns that are not genuinely distress-indicating. If so, the veto calibration needs adjustment — the system is correctly building prediction history but over-blocking gating.
2. **Voice mode introduces genuine speech stress signals** that reflect the exploratory, uncertain quality of early-session interaction, and the veto is working correctly — these are exactly the moments where fresh attention is warranted.

We cannot currently distinguish between these explanations. The predictor window (size 8) also means that any given session accumulates insufficient history to build confident predictions before it ends. With 27 sessions averaging only 3.4 turns each, the predictor is being reset frequently. As sessions grow longer and more focused, prediction accuracy should improve and gating should fire more reliably.

**Preliminary verdict**: gating machinery is functional and structurally sound. The interaction between vocal stress bypass and the early-session prediction history explains the low savings to date. This mechanism is **promising but insufficiently evidenced** — it requires longer sessions and calibration of the vocal stress detector threshold before a meaningful efficiency claim can be made.

### 6.4 Neuromodulator dynamics

The neuromodulator levels show expected biological-analog behavior:

- **Dopamine (DA)**: mean 0.443, range 0.300–0.850. The range indicates active reward signaling — DA is responding to turn quality and valence. A mean below 0.5 suggests the system spends most of its time in a "not strongly rewarded" baseline state, which is appropriate for exploratory early sessions.
- **Acetylcholine (ACh)**: mean 0.472, range 0.170–0.850. Slightly higher mean than DA, consistent with the system being frequently in an "paying attention to something novel" state — expected during setup and exploration sessions.
- **GABA**: mean 0.113, range 0.020–0.850. Low baseline with the ability to spike substantially on threat. This is structurally correct: GABA as a resting inhibitory tone at ~10% with headroom for strong inhibition. The maximum of 0.850 confirms that the hostile/threat pathway fires as designed.

The neuromodulator dynamics are working as designed. Their behavioral consequences (suppressing drafters under high GABA, adjusting learning rate via DA×ACh) are functional. However, with only 91 turns, we cannot yet characterize how these levels evolve over the course of long relationships or whether they develop meaningful session-to-session "mood baselines."

### 6.5 Emotion model

The current emotion distribution: **neutral (71%)**, curious (22%), excitement (5%), thoughtful (1%). No negative emotional states have appeared in the dataset.

The predominantly neutral baseline with positive emotional states (curious, excitement) is characteristic of exploratory, intellectually engaging sessions — which matches the actual session content (mostly testing and discussion of the system itself). The absence of negative emotional states does not indicate that the negative pathways are non-functional; it indicates the sessions to date have not encountered hostile input, prolonged frustration, or sustained negative valence.

The emotion model is **working structurally**. The Plutchik-style neuromodulator-to-emotion mapping is operational. Whether the emotion labels produce meaningfully differentiated behavior at scale requires more diverse session data.

### 6.6 Memory system

Memory recall was active on **26 of 91 turns (28.6%)**. This is notable: in roughly one in three turns, the hippocampus cluster successfully executed a recall query and returned episodic or schema content to the frontal drafters. The recall mechanism — vector similarity over LanceDB plus Markdown grep against schema files — is functional.

Memory write (episode encoding) is harder to evaluate from current data. The hippocampus encoder fires when turn salience exceeds a threshold, but the skip logic (low-surprise/low-DA turns skip LLM summarization) means many episodes are stored with code-generated summaries rather than LLM-synthesized ones. The episodic store is accumulating, but whether the memory quality is sufficient for meaningful long-horizon retrieval requires multi-week longitudinal testing.

### 6.7 Hebbian plasticity: too early to evaluate

Six Hebbian update events in 91 turns. This is too sparse for any conclusion about whether the plasticity mechanism produces coherent behavior change. The low update rate has multiple candidate explanations: turn-level outcomes must meet a minimum signal threshold (|outcome| < 0.05 skips the update), defuse turns are explicitly skipped to avoid reinforcing reactive behavior, and sessions have been short (mean 3.4 turns). Hebbian learning is designed for longitudinal effects across many sessions; it is not expected to produce observable results in this dataset.

This mechanism is **correctly designed but entirely unevaluated**. Its theoretical basis (Hebbian potentiation, homeostatic decay, plasticity modulation by arousal) is sound, and the implementation is complete. Whether it produces the desired behavioral effects — recognizable stylistic preferences emerging from reinforcement — requires months of sustained interaction.

### 6.8 Draft quality

The Multiple Drafts engine produces drafts with a mean critic score of **0.819 across all generated drafts**. The frontal critic scores coherence, relevance, tone-fit, and empathy; the composite score of 0.82 indicates consistently good draft quality. The mean drafter count of 1.12 confirms that the arousal-modulated drafter count switch is working correctly: most turns are low-enough arousal to generate a single draft, with multiple drafts reserved for higher-arousal or more complex inputs.

Draft selection (highest overall score) is functioning. The critic veto (score below threshold) has not been needed to fire in the current dataset, consistent with the high mean quality. Whether the tournament produces meaningfully different outcomes than a single-drafter approach requires controlled comparison, which the eval framework (`eval/compare.py`) supports but which has not yet been run at scale.

---

## 7. Discussion

### 7.1 What is working well

**The cost model is validated.** Mean 3.48 LLM calls per turn at a designed range of 3–5 represents accurate architectural cost prediction. The system is running within budget with the template-match short-circuit (0 LLM calls for trivial inputs) confirmed operational.

**The neuromodulator dynamics are correct.** The GABA/DA/ACh/Glu system is producing biologically plausible level distributions, responding appropriately to input valence, and the inhibitory pathways (GABA suppressing drafter count) are functional. This is one of the more novel aspects of the design — most multi-agent systems have no equivalent — and it is working as designed.

**Memory recall is active and frequent.** 28.6% of turns triggering hippocampus recall is a strong indicator that the memory system is contributing to turns, not merely accumulating. Whether those contributions are improving response quality requires controlled evaluation but the infrastructure is operational.

**The observability system is complete.** The decision log, turn trace, browser UI with real-time activation visualization, and eval comparison framework represent genuine research infrastructure. The ability to inspect every predict-and-surprise decision, every Hebbian update, and every drafter selection with full reasoning is unusual in this space and would allow rigorous evaluation when more data accumulates.

**The philosophical commitments are structurally implemented.** This is not a system that merely describes the Multiple Drafts model or Global Workspace Theory — it has actually implemented them. The frontal drafter tournament is a Multiple Drafts engine in the Dennett sense. The attention.focus bus topic is a Global Workspace. The predict-and-surprise gating is Active Inference applied at the cluster level. Whether these implementations produce the properties their philosophical progenitors attribute to them is an empirical question, but the implementation fidelity is genuine.

### 7.2 What is promising but not yet evidenced

**Predict-and-surprise gating as an efficiency mechanism.** The design target — saving 30–50% of LLM calls on routine turns in familiar conversations — has not been achieved in early data, but the structural explanation (short sessions, vocal stress bypass calibration) is plausible. As sessions lengthen and vocal stress threshold tuning is applied, this mechanism may deliver on its promise. It is the single largest potential efficiency gain in the system and warrants focused testing.

**Hebbian plasticity producing emergent behavioral style.** The mechanism is sound and implemented. The theoretical prediction — that preferred drafters, recall paths, and switch orderings will emerge from accumulated reinforcement signal over many sessions — is testable but not yet tested. This is a 3–6 month experiment, not a 91-turn experiment.

**DMN and metacognition producing richer long-session context.** Both the Default Mode Network (idle thinking between turns) and the metacognition cell (self-reflection every 30 seconds) are implemented and operational. Their value — making the entity richer in long sessions because it has been "thinking" between turns — requires session durations and continuity not yet seen in the dataset.

**Sleep consolidation and session-to-session identity continuity.** The self.md file shows 31 accumulated mood signature entries from multiple sessions, confirming that sleep consolidation is running. Whether the accumulated self-model produces meaningfully consistent cross-session behavior requires longitudinal study.

**The empathy critic and Theory of Mind pathways.** These are implemented but have rarely fired in the current dataset (sessions have been emotionally neutral). Their functional value — modulating responses when user emotional state is non-neutral — requires sessions with frustrated, distressed, or emotionally engaged users.

### 7.3 What remains genuinely uncertain

**Whether the multi-agent structure produces better responses than a single well-prompted LLM.** This is the central open question. The eval framework has the infrastructure to test it (`eval/baseline.py`, `eval/compare.py`, post-hoc judge scoring) but has not yet run controlled comparisons at scale. The literature suggests this is a hard bar to clear. The honest prior, based on prior art, is that the multi-agent structure adds legibility and behavioral structure but may not beat a single good model on raw response quality. Claiming otherwise requires evidence we do not yet have.

**Whether the emotional state labels map to meaningfully differentiated behavior.** The emotion-naming switch maps neuromodulator vectors to labels like "curious" or "neutral." Whether those labels produce substantively different responses (via the emotion expression switch and voice modulation) or merely label states whose behavioral effects were already captured by the raw neuromodulator levels is unclear.

**Long-horizon session coherence.** Can the entity maintain a consistent relational identity across many sessions over months? The architectural substrate for this (perfect episodic memory, self.md autobiography, Hebbian wiring) is in place. The evidence is not.

**Whether predict-and-surprise gating is actually saving compute or merely describing the already-gated state.** The 27 emotional bypasses suggest the gating predictor may be building valid predictions but having them systematically overridden. If the bypass rate remains high after calibration, it would indicate that the system's actual turn mix is too emotionally charged for routine prediction to fire — which would be interesting in its own right (the system is rarely in a truly routine state) but would undermine the efficiency claim.

---

## 8. Known Limitations and Failure Modes

Several failure modes were anticipated in the original design document. Their current status:

**Coordinator over-reach.** The architectural constraint (coordinators subscribe only to their own cluster's topics plus neuromod.* and attention.focus) is enforced by framework-level scope locking. No violation has been observed.

**Echo chambers and cascade storms.** Hop-count limits, per-topic activation decay, per-cell rate limits, and the brainstem turn-budget enforcer are all implemented. No runaway cascade has been observed in production use.

**Silent brain** (thresholds too high, nothing fires). The brainstem articulation gate fires on timeout regardless, guaranteeing a response. The template-match path guarantees trivial-input handling. Not observed.

**Consensus on garbage.** The critic cell and multi-draft tournament provide some protection, but if all active drafters share a systematic hallucination, the architecture has no principled remedy. This is a known open problem in multi-agent systems generally.

**Replay determinism.** Not attempted. The async + LLM nondeterminism makes exact replay impossible. The logging is designed for reconstruction, not deterministic replay.

**Latency.** 9.2-second mean is workable but not conversational. The system was designed with the expectation that latency would be a primary UX limitation, framed as "deliberation" rather than lag. Shorter-path turns (template match, single drafter) achieve sub-second to 2-second latency. Complex multi-drafter turns drive the mean up. This is a fundamental constraint of sequential LLM calls on the critical path.

---

## 9. Relation to the Philosophy of Mind Literature

This system makes claims that can be evaluated against each of its philosophical commitments.

**Functionalism**: The system's operation is genuinely substrate-independent. The same conversation has been run with Anthropic Haiku, Google Gemini Flash-Lite, and local Ollama Qwen 2.5 as the integrators. The behavioral differences are stylistic, not structural. The functional organization — switch gating, convergence events, drafter tournaments — operates identically regardless of which models are running the integrators. This is functionalism in practice.

**Dual-Process**: The switch/integrator distinction cleanly separates fast-and-automatic from slow-and-deliberate processing. Whether this produces the specific phenomenological properties Kahneman attributes to System 1 and System 2 in humans is a category error to ask — but the computational analogy is genuine. The system literally does not reason about routine inputs; it pattern-matches. It literally does reason about novel or high-surprise inputs.

**Global Workspace**: The message bus with attention.focus topic implements a workspace in the Baars/Dehaene sense: local processing is unconscious (invisible to other clusters), and promotion to attention.focus makes content available system-wide. This is architectural, not metaphorical.

**Multiple Drafts**: The drafter tournament is genuinely draft-parallel. Multiple integrators write candidate responses without knowledge of each other's drafts. The critic scores them. The articulation gate emits the winner. There is no "true response" that was waiting to be discovered — there are only the drafts that existed when the gate fired. This is the Multiple Drafts model in code.

**Extended Mind**: The second brain satisfies Clark and Chalmers' coupling and availability conditions: it is reliably available, automatically endorsed, and directly accessible. The entity's responses are demonstrably shaped by second-brain content (28.6% of turns recall from it). The functional loop is closed.

---

## 10. Conclusions

This system represents a genuine implementation of biologically-inspired cognitive architecture at a scale and fidelity not previously published in the LLM multi-agent literature. Its core design claims — sparse LLM activation at convergence zones only, neuromodulator dynamics as free persistent state, hippocampus-gated memory with vector episodic store, Hebbian plasticity in edge weights, predict-and-surprise gating from Active Inference — are not merely described but implemented and operational.

The early data (91 turns, 27 sessions) supports the following conclusions:

1. The cost model is validated: the system operates within its designed LLM call budget.
2. Neuromodulator dynamics are functional and produce biologically plausible level distributions.
3. Memory recall is active and contributing to turns at meaningful frequency.
4. The observability infrastructure is complete and capable of supporting rigorous evaluation.

The following mechanisms are implemented and structurally sound but require substantially more data to evaluate: predict-and-surprise efficiency gains, Hebbian plasticity producing emergent behavioral style, DMN/metacognition effects on long-session richness, cross-session identity coherence, and empathy critic effects on emotionally charged conversations.

The central open question — whether the multi-agent architecture produces better responses than a single well-prompted LLM given equal compute — is not answered and should be the primary focus of next-phase evaluation.

The most honest characterization of the current state: this system has built the **substrate for emergent behavior** that its philosophical and biological models predict. The behavior itself requires months of longitudinal interaction to either appear or fail to appear. The interesting finding from early sessions is not that emergent behavior has been observed — it has not, at least not in any rigorous sense — but that the system is running correctly, accumulating history, and has not hit any of the catastrophic failure modes (cascades, coordinator drift, cost explosions) that the design literature identified as likely early blockers.

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
*Data source: `eval/turns.jsonl` (91 turns, 27 sessions, as of 2026-05-23)*  
*Architecture reference: `PLAN.md`, `brain/CONSTITUTION.md`*
