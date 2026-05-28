"""
Parietal Lobe — persistent session state. 0 LLMs, all state-tracking switches.
Ring buffer of recent turns, entity tracker, topic tracker.
"""
from __future__ import annotations

import logging
import re
from collections import deque

from brain.bus import Bus
from brain.clusters.skill_selector import ActiveSkillContext

logger = logging.getLogger(__name__)

CLUSTER = "parietal"
RING_SIZE = 6


class ParietalCluster:
    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self._ring: deque[dict] = deque(maxlen=RING_SIZE)
        self._entities: dict[str, int] = {}  # entity -> turn count last seen
        self._turn_count = 0
        self.active_skill_context: ActiveSkillContext | None = None

    def seed(self, episodes: list[dict]) -> None:
        """Pre-populate the ring from recent episodic history (called once at boot).
        Episodes arrive newest-first; ring wants oldest-first so we reverse."""
        for ep in reversed(episodes):
            entry = {
                "turn": self._turn_count,
                "user": ep.get("user_input", ""),
                "response": ep.get("entity_response", ""),
                "intent": (ep.get("topic_tags") or [None])[0],
                "topic": None,
                "emotion": ep.get("emotion_state"),
            }
            self._ring.append(entry)

    def update(self, features: dict, user_input: str, entity_response: str = "") -> None:
        self._turn_count += 1
        entry = {
            "turn": self._turn_count,
            "user": user_input,
            "response": entity_response,
            "intent": features.get("intent"),
            "topic": features.get("topic_summary"),
            "emotion": features.get("emotion"),
        }
        self._ring.append(entry)

        # Track entities
        for entity in features.get("entities", []):
            self._entities[entity] = self._turn_count

    def recent_turns(self, n: int = 4) -> list[dict]:
        return list(self._ring)[-n:]

    @staticmethod
    def _strip_role_tags(text: str) -> str:
        """Remove lines that start with 'User:' or 'Brain:' to prevent role spoofing."""
        return "\n".join(
            line for line in text.splitlines()
            if not re.match(r"^\s*(User|Brain)\s*:", line)
        )

    def recent_turns_text(self, n: int = 4) -> str:
        turns = self.recent_turns(n)
        lines = []
        for t in turns:
            lines.append(f"User: {self._strip_role_tags(t['user'])}")
            if t.get("response"):
                lines.append(f"Brain: {self._strip_role_tags(t['response'])}")
        return "\n".join(lines)

    def session_summary(self) -> dict:
        return {
            "turn_count": self._turn_count,
            "recent_entities": list(self._entities.keys())[-10:],
            "recent_topics": [t.get("topic") for t in self._ring if t.get("topic")],
        }

    @property
    def turn_count(self) -> int:
        return self._turn_count

    def set_active_skill_context(self, ctx: ActiveSkillContext | None) -> None:
        """Selector writes back the updated context after each turn."""
        self.active_skill_context = ctx

    def clear_active_skill_context(self, reason: str = "") -> None:
        if self.active_skill_context is not None:
            logger.debug("parietal: clearing active skill context (%s)", reason)
            self.active_skill_context = None
