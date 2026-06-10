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


_WIRING_STORAGE = os.environ.get("BRAIN_STORAGE_BACKEND", "local").lower()


class Wiring:
    def __init__(self, user_id: str | None = None, persona: str = "") -> None:
        self._user_id = user_id
        self._persona = persona
        self._use_supabase = _WIRING_STORAGE == "supabase"
        self._edges: dict[tuple[str, str], Edge] = {}
        # Snapshot of weights at session boot — used for session-delta reports
        self._session_baseline: dict[tuple[str, str], float] = {}
        # N1 (colony-features-ii): transient, NON-PERSISTED "trail" overlay — fast
        # within-session plasticity over the slow, sleep-consolidated weights. Paths
        # that fire AND pay off get reinforced live (stigmergy: trails strengthen as
        # they're walked); the overlay decays each turn and evaporates at session end.
        self._trail: dict[tuple[str, str], float] = {}
        self._trail_decay_ts: float | None = None
        self._load()

    def _sb(self):
        from brain.second_brain.supabase_client import get_client

        return get_client()

    def _uid(self) -> str:
        if self._user_id:
            return self._user_id
        from brain.second_brain.supabase_client import get_user_id

        return get_user_id()

    def _persona_name(self) -> str:
        if self._persona:
            return self._persona
        return os.environ.get("BRAIN_PERSONA_NAME", "default")

    def add(
        self, source: str, target: str, weight: float = 1.0, polarity: str = "excitatory"
    ) -> None:
        key = (source, target)
        if key not in self._edges:
            self._edges[key] = Edge(source, target, weight, polarity)

    def has(self, source: str, target: str) -> bool:
        return (source, target) in self._edges

    def successors(self, source: str) -> set[str]:
        """All targets reachable from `source` in one hop. Used by the
        flock_dynamics criticality observable to reconstruct cascade ancestry
        from a flat per-turn firing path. Read-only; no overlay/weight logic."""
        return {tgt for (src, tgt) in self._edges if src == source}

    def has_outgoing(self, source: str) -> bool:
        """True if `source` is the source of at least one edge (i.e. it can
        propagate). Non-terminal nodes are the denominator of the branching
        ratio σ — terminal nodes can't have descendants and would bias σ down."""
        return any(src == source for (src, _tgt) in self._edges)

    def get_weight(self, source: str, target: str) -> float:
        """Effective weight (signed by polarity). Returns 1.0 for missing edges."""
        e = self._edges.get((source, target))
        return e.effective_weight() if e else 1.0

    def get_edge_weight(self, source: str, target: str) -> float:
        """Magnitude only (unsigned). Returns the resting weight for missing edges.

        N1: when colony trail-reinforcement is APPLIED (colony_features AND
        colony_trail_apply), the transient trail overlay is added to the persisted
        weight (clamped to [WEIGHT_MIN, WEIGHT_MAX]). In shadow mode (apply=0) the
        overlay is recorded but NOT reflected here — reads stay on the persisted
        weight so the feature can be measured before it influences routing."""
        e = self._edges.get((source, target))
        base = e.weight if e else WEIGHT_REST
        if _settings.get("colony_features", 0) and _settings.get("colony_trail_apply", 0):
            overlay = self._trail.get((source, target), 0.0)
            if overlay:
                # Clamp to the LIVE weight ceiling (the Learning Rate dial raises
                # weight_max for accumulation headroom; the hardcoded constant
                # would silently cap trails below what Hebbian updates may reach).
                w_max = float(_settings.get("weight_max", WEIGHT_MAX) or WEIGHT_MAX)
                return max(WEIGHT_MIN, min(w_max, base + overlay))
        return base

    # ── N1: live trail reinforcement (transient, non-persisted) ───────────────

    def decay_trails(self, now: float | None = None) -> None:
        """Exponentially decay all trail overlays toward zero (half-life from
        colony_trail_half_life_s). Called once per turn."""
        if not self._trail:
            self._trail_decay_ts = now if now is not None else time.time()
            return
        now = time.time() if now is None else now
        if self._trail_decay_ts is None:
            self._trail_decay_ts = now
            return
        elapsed = max(0.0, now - self._trail_decay_ts)
        hl = float(_settings.get("colony_trail_half_life_s", 120.0))
        if hl > 0 and elapsed > 0:
            factor = 0.5 ** (elapsed / hl)
            for k in list(self._trail):
                self._trail[k] *= factor
                if abs(self._trail[k]) < 1e-4:
                    del self._trail[k]
        self._trail_decay_ts = now

    def reinforce_trail(
        self, fired_path: list[str], amount: float, now: float | None = None
    ) -> int:
        """Bump the trail overlay along a fired path by signed `amount` (clamped to
        ±colony_trail_clamp). Only existing edges. Records even in shadow mode — it's
        get_edge_weight that gates whether the overlay influences live reads. Returns
        the count of edges touched. No-op when colony features are off."""
        if not _settings.get("colony_features", 0) or abs(amount) < 1e-9 or len(fired_path) < 2:
            return 0
        self.decay_trails(now)
        clamp = float(_settings.get("colony_trail_clamp", 0.5))
        n = 0
        for i in range(len(fired_path) - 1):
            key = (fired_path[i], fired_path[i + 1])
            if key in self._edges:
                self._trail[key] = max(-clamp, min(clamp, self._trail.get(key, 0.0) + amount))
                n += 1
        return n

    def trail_snapshot(self) -> dict[str, float]:
        """Current would-be trail overlays (for the N1 shadow-audit correlation gate)."""
        return {f"{s}→{t}": round(v, 4) for (s, t), v in self._trail.items()}

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
        if self._use_supabase:
            self._sb_save()
            return
        WIRING_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {"src": e.source, "tgt": e.target, "w": e.weight, "pol": e.polarity}
            for e in self._edges.values()
        ]
        WIRING_PATH.write_text(json.dumps(data))

    def _sb_save(self) -> None:
        try:
            sb = self._sb()
            uid = self._uid()
            persona = self._persona_name()
            rows = [
                {
                    "org_id": uid,
                    "persona": persona,
                    "source": e.source,
                    "target": e.target,
                    "weight": e.weight,
                    "polarity": e.polarity,
                    "updated_at": "now()",
                }
                for e in self._edges.values()
            ]
            if rows:
                sb.table("wiring_edges").upsert(
                    rows, on_conflict="org_id,persona,source,target"
                ).execute()
        except Exception as e:
            logger.warning("Supabase wiring save failed: %s", e)

    _MAX_HISTORY_SNAPSHOTS = 100

    def snapshot_to_history(self, session_id: str) -> Path | None:
        """Archive wiring snapshot for evolution charting."""
        if self._use_supabase:
            return self._sb_snapshot(session_id)
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

    def _sb_snapshot(self, session_id: str) -> None:
        try:
            sb = self._sb()
            uid = self._uid()
            persona = self._persona_name()
            edges = [
                {"src": e.source, "tgt": e.target, "w": e.weight, "pol": e.polarity}
                for e in self._edges.values()
            ]
            sb.table("wiring_snapshots").insert(
                {
                    "org_id": uid,
                    "persona": persona,
                    "session_id": session_id,
                    "ts": time.time(),
                    "edges": edges,
                }
            ).execute()
            # Prune to last 100 snapshots
            res = (
                sb.table("wiring_snapshots")
                .select("id")
                .eq("org_id", uid)
                .eq("persona", persona)
                .order("ts", desc=True)
                .execute()
            )
            ids = [r["id"] for r in (res.data or [])]
            if len(ids) > self._MAX_HISTORY_SNAPSHOTS:
                old_ids = ids[self._MAX_HISTORY_SNAPSHOTS :]
                sb.table("wiring_snapshots").delete().in_("id", old_ids).execute()
        except Exception as e:
            logger.warning("Supabase wiring snapshot failed: %s", e)

    def _load(self) -> None:
        if self._use_supabase:
            self._sb_load()
            return
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

    def _sb_load(self) -> None:
        try:
            sb = self._sb()
            uid = self._uid()
            res = (
                sb.table("wiring_edges")
                .select("source,target,weight,polarity")
                .eq("org_id", uid)
                .eq("persona", self._persona_name())
                .execute()
            )
            for item in res.data or []:
                self._edges[(item["source"], item["target"])] = Edge(
                    item["source"], item["target"], item["weight"], item["polarity"]
                )
            logger.debug("[Wiring] Loaded %d edges from Supabase", len(self._edges))
        except Exception as e:
            logger.warning("Could not load wiring from Supabase: %s", e)
