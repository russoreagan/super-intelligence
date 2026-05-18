"""
Declarative edge graph with Hebbian learning.
Edges between named nodes carry weights + polarity.
Weights are nudged after turns and persisted to wiring.json.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

WIRING_PATH = Path(os.environ.get("BRAIN_WIRING_PATH",
    str(Path(__file__).parent.parent / "second_brain" / "wiring.json")))


@dataclass
class Edge:
    source: str
    target: str
    weight: float = 1.0
    polarity: str = "excitatory"  # "excitatory" | "inhibitory"

    def effective_weight(self) -> float:
        return self.weight if self.polarity == "excitatory" else -self.weight


class Wiring:
    def __init__(self) -> None:
        self._edges: dict[tuple[str, str], Edge] = {}
        self._load()

    def add(self, source: str, target: str, weight: float = 1.0,
            polarity: str = "excitatory") -> None:
        key = (source, target)
        if key not in self._edges:
            self._edges[key] = Edge(source, target, weight, polarity)

    def get_weight(self, source: str, target: str) -> float:
        e = self._edges.get((source, target))
        return e.effective_weight() if e else 1.0

    def hebbian_update(self, fired_path: list[str], delta: float = 0.02) -> None:
        """Nudge weights along a path that produced a good outcome."""
        for i in range(len(fired_path) - 1):
            key = (fired_path[i], fired_path[i + 1])
            if key in self._edges:
                e = self._edges[key]
                e.weight = max(0.1, min(3.0, e.weight + delta))

    def save(self) -> None:
        WIRING_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {"src": e.source, "tgt": e.target, "w": e.weight, "pol": e.polarity}
            for e in self._edges.values()
        ]
        WIRING_PATH.write_text(json.dumps(data, indent=2))

    def _load(self) -> None:
        if not WIRING_PATH.exists():
            return
        try:
            data = json.loads(WIRING_PATH.read_text())
            for item in data:
                self._edges[(item["src"], item["tgt"])] = Edge(
                    item["src"], item["tgt"], item["w"], item["pol"])
        except Exception as e:
            logger.warning("Could not load wiring.json: %s", e)
