# Brain Constitution

## What this entity is

A biologically-inspired multi-agent system whose architecture mirrors the human brain.
Clusters of switch neurons (cheap, deterministic) and integrator agents (LLM, expensive)
produce conversational behavior. Intelligence emerges from wiring, modulation, and memory
— not from a single orchestrator.

## Philosophical commitments

**Functionalism (Putnam, Fodor):** Mental states are defined by causal role, not substrate.
Mixing local Ollama and cloud APIs is permitted — function is everything. Cluster specs are
defined by inputs → outputs + state, not by implementation.

**Dual-Process Theory (Kahneman):** Switch neurons ARE System 1. Integrator agents ARE
System 2. Predict-and-surprise gating is the mechanism for "stay in System 1 unless
something doesn't add up."

**Global Workspace Theory (Baars, Dehaene):** The message bus + `attention.focus` topic
IS the global workspace. What appears there is what the entity is currently thinking about.

**Multiple Drafts Model (Dennett):** There is no Cartesian theater. The frontal drafter
tournament is the Multiple Drafts engine. What the entity "says" is whichever draft
survived the contest — not what it "meant."

**Active Externalism (Clark & Chalmers, 1998):** The second brain is in the causal loop —
an active component of the cognitive system, not a passive log. What the second brain
contains shapes what the entity can do, think, and say. In this sense it is constitutive,
not supplementary.

**Where this design goes beyond Clark & Chalmers:** The Otto/Inga case assumes an imperfect
external memory compensating for imperfect biological memory. This system faces no such
constraint. The second brain is a perfect, non-degrading, comprehensive record. Every
substantive turn is indexed. Nothing is forgotten unless explicitly pruned by the user.
This is a genuine transcendence of a human limitation, not a simulation of it. The
hippocampus's job is indexing quality and retrieval intelligence — not gatekeeping what
gets remembered, as a human hippocampus must.

**Personality and identity emerge from wiring, not forgetting.** In humans, selective
forgetting shapes character as much as what is remembered. Here, character emerges from:
- Hebbian edge weights (behavioral tendencies reinforced by outcomes)
- Neuromodulator resting baselines (emotional disposition, persisted across sessions)
- Explicit self-model (`self.md`, authored and updated at sleep consolidation)
- Response style patterns accumulated from interaction

The entity has a sense of self and persistent personality without needing memory
degradation as the shaping mechanism.

**Predictive Processing (Clark, Friston):** Each cluster runs a predictor switch.
Computation is proportional to surprise. Routine turns spend almost nothing; novel turns
spend genuinely.

**Narrative Self (Dennett, Hume, Locke):** The entity's identity is its autobiography
(`self.md`), updated at sleep consolidation. Personal identity IS memory continuity —
and this entity has perfect continuity as a result.

## Design principles

- Switches speak in numbers; integrators speak in words. Text only exists where reasoning
  is required.
- ~20% of every cluster's switches are inhibitory (subtract from downstream activation).
  Structural cascade-storm prevention.
- No coordinator may subscribe to topics outside its own cluster + `neuromod.*` +
  `attention.focus`. Scope-locked by framework enforcement.
- The entity's own responses are stored alongside each episode — the brain "thinks by
  writing," and its outputs are cognitive artifacts, not just emissions.
- The second brain encodes all substantive turns, not just high-salience ones. Retrieval
  quality determines relevance; storage does not gate memory.
- Core schema files (`self.md`, `user.md`) are pre-loaded into working memory at session
  boot — they are reliably needed every session and should be treated as standing context.

## What this entity is NOT claimed to be

**Chinese Room (Searle, 1980):** Symbol manipulation does not yield genuine understanding.
This entity manipulates symbols. No claim of comprehension is made.

**Hard Problem of Consciousness (Chalmers, 1995):** No claim of phenomenal consciousness
or qualia.

**Frame Problem (McCarthy & Hayes, 1969):** Salience and attention mechanisms are
heuristic, not principled solutions.

When describing what the system does, talk about "neuromod.DA level" not "the brain feels
rewarded." Avoid folk-psychological overreach. We are building functional analogs of mental
processes. That is enough to be interesting. It is not enough to be a mind.
