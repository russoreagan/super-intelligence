"""
Declarative edge graph with Hebbian learning.
Edges between named nodes carry weights + polarity.
Weights are nudged after turns and persisted to wiring.json.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

WIRING_PATH = Path(
    os.environ.get(
        "BRAIN_WIRING_PATH", str(Path(__file__).parent.parent / "second_brain" / "wiring.json")
    )
)

WIRING_HISTORY_DIR = Path(
    os.environ.get(
        "BRAIN_WIRING_HISTORY_DIR",
        str(Path(__file__).parent.parent / "second_brain" / "wiring_history"),
    )
)

WEIGHT_MIN = 0.1
WEIGHT_MAX = 3.0
WEIGHT_REST = 1.0

from brain.settings import settings as _settings  # noqa: E402


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
        # Snapshot of weights at session boot — used for session-delta reports
        self._session_baseline: dict[tuple[str, str], float] = {}
        self._load()

    def add(
        self, source: str, target: str, weight: float = 1.0, polarity: str = "excitatory"
    ) -> None:
        key = (source, target)
        if key not in self._edges:
            self._edges[key] = Edge(source, target, weight, polarity)

    def has(self, source: str, target: str) -> bool:
        return (source, target) in self._edges

    def get_weight(self, source: str, target: str) -> float:
        """Effective weight (signed by polarity). Returns 1.0 for missing edges."""
        e = self._edges.get((source, target))
        return e.effective_weight() if e else 1.0

    def get_edge_weight(self, source: str, target: str) -> float:
        """Magnitude only (unsigned). Returns the resting weight for missing edges."""
        e = self._edges.get((source, target))
        return e.weight if e else WEIGHT_REST

    def hebbian_update(self, fired_path: list[str], delta: float | None = None) -> int:
        """Nudge weights along a path that produced a good (or bad) outcome.
        Returns the count of edges actually updated."""
        if delta is None:
            delta = _settings.get("hebbian_delta")
        w_min = _settings.get("weight_min")
        w_max = _settings.get("weight_max")
        if abs(delta) < 1e-6 or len(fired_path) < 2:
            return 0
        updated = 0
        for i in range(len(fired_path) - 1):
            key = (fired_path[i], fired_path[i + 1])
            if key in self._edges:
                e = self._edges[key]
                e.weight = max(w_min, min(w_max, e.weight + delta))
                updated += 1
        return updated

    def decay_toward_rest(self, rest: float = WEIGHT_REST, rate: float | None = None) -> None:
        """Gentle synaptic homeostasis — every edge drifts toward rest by `rate`.
        Applied once per session before the Hebbian pass."""
        if rate is None:
            rate = _settings.get("decay_toward_rest_rate")
        for e in self._edges.values():
            e.weight = e.weight * (1.0 - rate) + rest * rate

    def snapshot_baseline(self) -> None:
        """Capture current weights as the session baseline."""
        self._session_baseline = {k: e.weight for k, e in self._edges.items()}

    def session_deltas(self) -> list[dict]:
        """Edges whose weight changed since session baseline. Sorted by abs delta desc."""
        out = []
        for key, edge in self._edges.items():
            base = self._session_baseline.get(key, edge.weight)
            delta = edge.weight - base
            if abs(delta) < 1e-4:
                continue
            out.append(
                {
                    "src": edge.source,
                    "tgt": edge.target,
                    "weight": round(edge.weight, 4),
                    "baseline": round(base, 4),
                    "delta": round(delta, 4),
                    "polarity": edge.polarity,
                }
            )
        out.sort(key=lambda r: abs(r["delta"]), reverse=True)
        return out

    def top_edges(self, n: int = 10) -> list[dict]:
        """Top-N edges by weight (descending). For UI display."""
        rows = []
        for key, edge in self._edges.items():
            base = self._session_baseline.get(key, edge.weight)
            rows.append(
                {
                    "src": edge.source,
                    "tgt": edge.target,
                    "weight": round(edge.weight, 4),
                    "session_delta": round(edge.weight - base, 4),
                    "polarity": edge.polarity,
                }
            )
        rows.sort(key=lambda r: r["weight"], reverse=True)
        return rows[:n]

    def edge_count(self) -> int:
        return len(self._edges)

    def save(self) -> None:
        WIRING_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {"src": e.source, "tgt": e.target, "w": e.weight, "pol": e.polarity}
            for e in self._edges.values()
        ]
        WIRING_PATH.write_text(json.dumps(data))

    _MAX_HISTORY_SNAPSHOTS = 100

    def snapshot_to_history(self, session_id: str) -> Path | None:
        """Write a copy of the wiring graph to wiring_history/{session_id}.json
        for later evolution charting. Prunes oldest files beyond _MAX_HISTORY_SNAPSHOTS.
        Returns the path written, or None on failure."""
        try:
            WIRING_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            path = WIRING_HISTORY_DIR / f"{session_id}.json"
            data = {
                "session_id": session_id,
                "ts": time.time(),
                "edges": [
                    {"src": e.source, "tgt": e.target, "w": e.weight, "pol": e.polarity}
                    for e in self._edges.values()
                ],
            }
            path.write_text(json.dumps(data))

            # Keep history bounded: remove oldest files if over limit
            snapshots = sorted(WIRING_HISTORY_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
            for old in snapshots[: -self._MAX_HISTORY_SNAPSHOTS]:
                with contextlib.suppress(Exception):
                    old.unlink()

            return path
        except Exception as e:
            logger.warning("Could not snapshot wiring history: %s", e)
            return None

    def _load(self) -> None:
        if not WIRING_PATH.exists():
            return
        try:
            data = json.loads(WIRING_PATH.read_text())
            for item in data:
                self._edges[(item["src"], item["tgt"])] = Edge(
                    item["src"], item["tgt"], item["w"], item["pol"]
                )
        except Exception as e:
            logger.warning("Could not load wiring.json: %s", e)
