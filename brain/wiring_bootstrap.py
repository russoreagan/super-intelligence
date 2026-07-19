"""
Declares the initial edge graph between named brain nodes.
Called once at session boot; only adds edges that don't already exist
(so reloading a populated wiring.json doesn't reset learned weights).

Node naming convention:
  - Cluster.cell (e.g. "frontal.executive", "temporal.understanding_integrator")
  - Cluster.switch_name (e.g. "temporal.template_match")
  - Cluster as a whole when handing off coarse signals (e.g. "temporal")
"""

from __future__ import annotations

from brain.wiring import Wiring


def bootstrap(wiring: Wiring) -> None:
    """Idempotent: only adds edges that don't already exist."""

    # ── Temporal: input → switches → integrator ──────────────────────────
    for sw in (
        "temporal.template_match",
        "temporal.length_bucket",
        "temporal.salience_prefilter",
        "temporal.self_reference",
        "temporal.epistemic_action",
    ):
        wiring.add("sensory.text", sw, weight=1.0, polarity="excitatory")
        wiring.add(sw, "temporal.understanding_integrator", weight=1.0, polarity="excitatory")
    # The inhibitor sits between the switches and the integrator
    wiring.add("sensory.text", "temporal.integrator_inhibitor", weight=1.0, polarity="excitatory")
    wiring.add(
        "temporal.integrator_inhibitor",
        "temporal.understanding_integrator",
        weight=1.0,
        polarity="inhibitory",
    )

    # ── Temporal integrator → downstream clusters ────────────────────────
    wiring.add(
        "temporal.understanding_integrator", "frontal.executive", weight=1.0, polarity="excitatory"
    )
    wiring.add("temporal.understanding_integrator", "hypothalamus", weight=1.0)
    wiring.add("temporal.understanding_integrator", "hippocampus.recall", weight=1.0)

    # ── Frontal: pre-tool APPROACH stage ─────────────────────────────────
    # Source is the UNDERSTANDING integrator, not the executive — the executive
    # has not run yet when this stage fires; the approach is conditioned on
    # parsed features. approach_critic → tool_planner is the strategy→planning
    # handoff made visible in the graph (the boundary, auditable rather than a
    # comment). Stance credit does NOT live on these edges — it lives on
    # fragment.<sid> → frontal.approach_stage (the anchor; edges are created by
    # learning, not bootstrapped).
    for a in ("frontal.approach_A", "frontal.approach_B", "frontal.approach_C"):
        wiring.add("temporal.understanding_integrator", a, weight=1.0, polarity="excitatory")
        wiring.add(a, "frontal.approach_critic", weight=1.0, polarity="excitatory")
    wiring.add("frontal.approach_critic", "frontal.executive", weight=1.0, polarity="excitatory")
    wiring.add(
        "frontal.approach_critic", "motor_cortex.tool_planner", weight=1.0, polarity="excitatory"
    )
    # The critic's verdict is what writes stance credit to the anchor — declared here so
    # the anchor exists in the graph from boot (learning then hangs fragment.<sid> edges
    # off it) and the registry reconcile stays exact.
    wiring.add(
        "frontal.approach_critic", "frontal.approach_stage", weight=1.0, polarity="excitatory"
    )

    # ── Frontal: executive → drafters → critic ───────────────────────────
    for d in (
        "frontal.drafter_A",
        "frontal.drafter_B",
        "frontal.drafter_C",
        "frontal.drafter_D",
        "frontal.drafter_E",
    ):
        wiring.add("frontal.executive", d, weight=1.0, polarity="excitatory")
        wiring.add(d, "frontal.critic", weight=1.0, polarity="excitatory")
        wiring.add(d, "frontal.empathy_critic", weight=1.0, polarity="excitatory")
        # Self-monitor: each drafter's output flows to the commitment extractor,
        # which can re-enter the loop as a self-directed task → motor.
        wiring.add(d, "frontal.commitment_extractor", weight=1.0, polarity="excitatory")
    wiring.add(
        "frontal.commitment_extractor",
        "motor_cortex.tool_planner",
        weight=1.0,
        polarity="excitatory",
    )

    # ── Frontal inhibitory edges (GABA dampens drafters; satiation dampens repeat) ─
    for d in (
        "frontal.drafter_A",
        "frontal.drafter_B",
        "frontal.drafter_C",
        "frontal.drafter_D",
        "frontal.drafter_E",
    ):
        wiring.add("hypothalamus.threat_to_GABA", d, weight=1.0, polarity="inhibitory")

    # ── Stoic reframer activates when GABA is high ───────────────────────
    wiring.add(
        "hypothalamus.threat_to_GABA", "frontal.stoic_reframer", weight=1.0, polarity="excitatory"
    )

    # ── Hypothalamus → frontal (emotion modulates tone) ──────────────────
    for tone_switch in (
        "hypothalamus.valence_to_DA",
        "hypothalamus.novelty_to_ACh",
        "hypothalamus.arousal_homeostat",
    ):
        wiring.add(tone_switch, "frontal.executive", weight=1.0, polarity="excitatory")

    # ── Hippocampus → frontal (memory provides context) ──────────────────
    for recall_strategy in (
        "hippocampus.cosine_recall",
        "hippocampus.schema_grep",
        "hippocampus.entity_tracker",
        "hippocampus.time_filter",
        "hippocampus.structural_recall",
    ):
        wiring.add("mem.recall", recall_strategy, weight=1.0, polarity="excitatory")
        wiring.add(recall_strategy, "frontal.executive", weight=1.0, polarity="excitatory")
        wiring.add(
            recall_strategy, "hippocampus.recall_aggregator", weight=1.0, polarity="excitatory"
        )

    # ── Parietal session-state → frontal ─────────────────────────────────
    for sw in (
        "parietal.recent_turns_ringbuffer",
        "parietal.topic_vector_holder",
        "parietal.entity_tracker",
    ):
        wiring.add(sw, "frontal.executive", weight=1.0, polarity="excitatory")
