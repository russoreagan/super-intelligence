"""
Thalamus — intelligent router and attention gater. Advisory only; clusters can ignore.
~8 switches, usually no LLM. Manages global workspace spotlight.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from brain.bus import Bus

logger = logging.getLogger(__name__)

CLUSTER = "thalamus"

# Topics that count as "global workspace" content when salience is high
WORKSPACE_TOPICS = {"temporal.features", "mem.recall", "affect.state", "sensory.image"}


class ThalamusCluster:
    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self._topic_activation: dict[str, float] = defaultdict(float)
        self._attention_focus: str | None = None

    async def route(self, features: dict, affect: dict) -> dict:
        """
        Compute routing hints based on current sensory features + neuromod state.
        Posts attention.focus with highest-priority cluster to wake.
        Returns routing hints dict for run.py.
        """
        nm = self._bus.neuromod.snapshot()
        salience = features.get("salience", 0.3)
        intent = features.get("intent", "other")

        # Update topic activations with decay
        for topic in self._topic_activation:
            self._topic_activation[topic] *= 0.7

        # Compute priority hints
        priorities = {
            "hippocampus": 0.0,
            "frontal": 0.5,
            "occipital": 0.0,
        }

        if features.get("requires_memory") or features.get("epistemic_action"):
            priorities["hippocampus"] += 0.6 + salience * 0.3
        if features.get("requires_vision"):
            priorities["occipital"] += 0.8
        if intent in ("hostile", "task"):
            priorities["frontal"] += 0.3
        if nm["ACh"] > 0.5:  # high novelty → more frontal engagement
            priorities["frontal"] += nm["ACh"] * 0.2

        top_cluster = max(priorities, key=priorities.get)
        focus_topic = f"cluster.{top_cluster}"

        if focus_topic != self._attention_focus:
            self._attention_focus = focus_topic
            await self._bus.publish_dict(
                "attention.focus",
                {"cluster": top_cluster, "salience": salience, "priorities": priorities},
                source=CLUSTER,
            )
            logger.debug("Thalamus: attention → %s (salience=%.2f)", top_cluster, salience)

        # Promote TTL for high-salience topics (soft spotlight)
        if salience > 0.6:
            self._topic_activation["temporal.features"] = salience
            logger.debug("Thalamus: promoting temporal.features to global workspace")

        return priorities
