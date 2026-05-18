"""
Parietal Lobe — persistent session state. 0 LLMs, all state-tracking switches.
Ring buffer of recent turns, entity tracker, topic tracker.
"""
from __future__ import annotations

import logging
from collections import deque

from brain.bus import Bus

logger = logging.getLogger(__name__)

CLUSTER = "parietal"
RING_SIZE = 6


class ParietalCluster:
    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self._ring: deque[dict] = deque(maxlen=RING_SIZE)
        self._entities: dict[str, int] = {}  # entity -> turn count last seen
        self._turn_count = 0

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

    def recent_turns_text(self, n: int = 4) -> str:
        turns = self.recent_turns(n)
        lines = []
        for t in turns:
            lines.append(f"User: {t['user']}")
            if t.get("response"):
                lines.append(f"Brain: {t['response']}")
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
