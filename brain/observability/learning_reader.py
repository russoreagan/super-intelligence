"""Read-side aggregation for the Learning surface.

Pure, persona-aware readers over the state the learning subsystems already
persist: per-persona wiring (current + history snapshots), the decision records
in the eval log, learning stories/ledger JSONL (written by later increments),
chunks.json and sequence_weights.json. Nothing here writes or touches the
learning path itself — the surface can never perturb what it observes.

Persona resolution mirrors run.py's boot-time env routing: SECOND_BRAIN_PATH
already points at the ACTIVE persona's root (…/personas/<slug> in multitenant
mode, the shared second_brain/ otherwise). Other personas are read from their
sibling directories; the active persona prefers live objects (Wiring, bus,
predictor) because they carry unsaved in-session state.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from brain.persona_key import persona_slug

logger = logging.getLogger(__name__)

# Bounded tail read of the mixed eval log (turn traces + decisions). The ledger
# (increment 2) is the primary query surface; this is the fallback that makes
# increment 1 work against history that predates it.
_TAIL_BYTES = 2 * 1024 * 1024


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _active_root() -> Path:
    return Path(os.environ.get("SECOND_BRAIN_PATH", str(_repo_root() / "second_brain")))


def _personas_dir() -> Path | None:
    """The directory holding per-persona roots, wherever this deployment put it."""
    base = _active_root()
    if base.parent.name == "personas":  # active root IS a persona dir
        return base.parent
    cand = base / "personas"
    return cand if cand.is_dir() else None


def _active_slug() -> str:
    try:
        from brain.persona_key import active_or_home_persona

        return persona_slug(active_or_home_persona())
    except Exception:
        return ""


def persona_root(persona: str = "") -> Path:
    """Filesystem root for a persona's learning state. Empty / active → the
    active root (which may itself be persona-namespaced via env)."""
    slug = persona_slug(persona)
    if not slug or slug == _active_slug():
        return _active_root()
    pdir = _personas_dir()
    if pdir is not None and (pdir / slug).is_dir():
        return pdir / slug
    return _active_root()


def list_personas() -> list[str]:
    """Persona slugs with on-disk learning state, active persona first."""
    out: list[str] = []
    active = _active_slug()
    if active:
        out.append(active)
    pdir = _personas_dir()
    if pdir is not None:
        for p in sorted(pdir.iterdir()):
            if p.is_dir() and p.name not in out:
                out.append(p.name)
    return out


# ── low-level readers ────────────────────────────────────────────────────────


def _read_jsonl_tail(path: Path, max_bytes: int = _TAIL_BYTES) -> list[dict]:
    """Parse the last `max_bytes` of a JSONL file, newest last. Silent on error."""
    try:
        if not path.exists():
            return []
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop the partial first line
            raw = f.read().decode("utf-8", errors="replace")
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception as e:
        logger.debug("[learning] tail read failed for %s: %s", path, e)
        return []


def _eval_log_path() -> Path:
    env = os.environ.get("BRAIN_EVAL_LOG")
    return Path(env) if env else _repo_root() / "eval" / "turns.jsonl"


def _ledger_path(persona: str = "") -> Path:
    return persona_root(persona) / "learning_ledger.jsonl"


def _stories_path(persona: str = "") -> Path:
    return persona_root(persona) / "learning_stories.jsonl"


# Session-end records are sparse (one per consolidation) — a 2 MB tail can miss
# them entirely while catching thousands of per-turn decisions. Sparse kinds get
# a wider (still bounded) fallback window.
_SPARSE_KINDS = {"session_plasticity_summary", "learning_story", "external_grade_recorded"}


def _decisions(persona: str = "", kinds: set[str] | None = None) -> list[dict]:
    """Decision records, ledger-first (persona-stamped), eval-log tail as the
    pre-ledger fallback. Newest last."""
    recs = _read_jsonl_tail(_ledger_path(persona))
    if not recs:
        sparse = bool(kinds) and kinds <= _SPARSE_KINDS
        recs = [
            r
            for r in _read_jsonl_tail(_eval_log_path(), max_bytes=_TAIL_BYTES * (8 if sparse else 1))
            if r.get("type") == "decision"
        ]
    if kinds:
        recs = [r for r in recs if r.get("decision") in kinds]
    return recs


def _wiring_file_edges(persona: str = "") -> list[dict]:
    """Edges from a persona's wiring.json (for non-active personas; the active
    persona should use the live Wiring object instead)."""
    path = persona_root(persona) / "wiring.json"
    try:
        data = json.loads(path.read_text())
        edges = data if isinstance(data, list) else data.get("edges", [])
        return [e for e in edges if isinstance(e, dict)]
    except Exception:
        return []


def _history_dir(persona: str = "") -> Path:
    if not persona_slug(persona) or persona_slug(persona) == _active_slug():
        env = os.environ.get("BRAIN_WIRING_HISTORY_DIR")
        if env:
            return Path(env)
    return persona_root(persona) / "wiring_history"


def _edge_series(persona: str, edge: str) -> list[dict]:
    """[{session_id, ts, w}] for one edge across the history snapshots (≤100)."""
    src, _, tgt = edge.partition("→")
    src, tgt = src.strip(), tgt.strip()
    hist = _history_dir(persona)
    series: list[dict] = []
    try:
        for path in sorted(hist.glob("*.json"), key=lambda p: p.stat().st_mtime):
            try:
                snap = json.loads(path.read_text())
            except Exception:
                continue
            for e in snap.get("edges", []):
                if e.get("src") == src and e.get("tgt") == tgt:
                    series.append(
                        {
                            "session_id": snap.get("session_id", path.stem),
                            "ts": snap.get("ts"),
                            "w": e.get("w"),
                        }
                    )
                    break
    except Exception as e:
        logger.debug("[learning] edge series failed: %s", e)
    return series


# ── public views (what the endpoints serve) ──────────────────────────────────


def wiring_view(persona: str = "", edge: str = "", live_wiring=None) -> dict:
    """Top edges + session deltas (+ drift series and per-update records when a
    specific edge is named)."""
    use_live = live_wiring is not None and (
        not persona_slug(persona) or persona_slug(persona) == _active_slug()
    )
    if use_live:
        try:
            top = live_wiring.top_edges(20)
            deltas = live_wiring.session_deltas()
            count = live_wiring.edge_count()
        except Exception:
            top, deltas, count = [], [], 0
    else:
        edges = _wiring_file_edges(persona)
        ranked = sorted(edges, key=lambda e: float(e.get("w", 1.0)), reverse=True)
        top = [
            {"edge": f"{e.get('src')}→{e.get('tgt')}", "weight": round(float(e.get("w", 1.0)), 3)}
            for e in ranked[:20]
        ]
        deltas = []  # session baseline lives in the live object only
        count = len(edges)
    out = {"top": top, "deltas": deltas, "edge_count": count}
    if edge:
        out["edge"] = edge
        out["edge_series"] = _edge_series(persona, edge)
        recs = _decisions(persona, kinds={"hebbian_update_applied"})
        src, _, tgt = edge.partition("→")
        out["edge_records"] = [
            r for r in recs if r.get("src") == src.strip() and r.get("tgt") == tgt.strip()
        ][-50:]
    return out


def _switch_view(persona: str = "", live_wiring=None) -> list[dict]:
    """Learned switch weights placed inside their safety bands."""
    try:
        from brain.settings import settings

        bands = dict(settings.get("switch_efficacy_bands") or {})
    except Exception:
        bands = {}
    if not bands:
        return []
    weights: dict[str, float] = {}
    use_live = live_wiring is not None and (
        not persona_slug(persona) or persona_slug(persona) == _active_slug()
    )
    if use_live:
        for name in bands:
            try:
                e = live_wiring.get("sensory.text", f"temporal.{name}")
                weights[name] = float(getattr(e, "weight", 1.0)) if e else 1.0
            except Exception:
                weights[name] = 1.0
    else:
        edges = {
            (e.get("src"), e.get("tgt")): float(e.get("w", 1.0))
            for e in _wiring_file_edges(persona)
        }
        for name in bands:
            weights[name] = edges.get(("sensory.text", f"temporal.{name}"), 1.0)
    out = []
    for name, band in bands.items():
        lo, hi = (float(band[0]), float(band[1])) if len(band) == 2 else (0.0, 2.0)
        w = weights.get(name, 1.0)
        pos = (w - lo) / (hi - lo) if hi > lo else 0.5
        out.append(
            {
                "name": name,
                "weight": round(w, 3),
                "band": [lo, hi],
                "position": round(max(0.0, min(1.0, pos)), 3),
            }
        )
    return out


def _chunks_view(persona: str = "") -> dict:
    path = persona_root(persona) / "chunks.json"
    try:
        data = json.loads(path.read_text())
        raw = data.get("chunks", data) if isinstance(data, dict) else data
        # chunks.json keys chunks by signature (mine_chunks); tolerate a list too.
        chunks = list(raw.values()) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        chunks = [c for c in chunks if isinstance(c, dict)]
    except Exception:
        chunks = []
    top = sorted(chunks, key=lambda c: int(c.get("occurrences", 0)), reverse=True)[:8]
    return {
        "total": len(chunks),
        "top": [
            {
                "tools": [s.get("tool", "?") for s in (c.get("sequence") or [])],
                "occurrences": c.get("occurrences", 0),
                "successes": c.get("successes", 0),
                "jobs": len(c.get("jobs") or []),
            }
            for c in top
        ],
    }


def _predictor_view(persona: str = "", live_predictor=None) -> dict:
    if live_predictor is not None and (
        not persona_slug(persona) or persona_slug(persona) == _active_slug()
    ):
        try:
            return {
                "history_len": len(getattr(live_predictor, "_history", []) or []),
                "top_transitions": live_predictor.top_transitions(8),
            }
        except Exception:
            pass
    path = persona_root(persona) / "sequence_weights.json"
    try:
        data = json.loads(path.read_text())
        bigrams = data.get("bigrams") or {}
        ranked = sorted(bigrams.items(), key=lambda kv: kv[1], reverse=True)[:8]
        return {
            "history_len": len(data.get("history") or []),
            "top_transitions": [{"transition": k, "count": v} for k, v in ranked],
        }
    except Exception:
        return {"history_len": 0, "top_transitions": []}


def _reward_mix(persona: str = "", live_bus=None) -> dict:
    """Where the dopamine that gates learning is coming from. Ledger reward_emission
    records when present (increment 3+); the live bus tally as the always-available
    coarse view."""
    out: dict = {"by_signal_type": {}, "self_graded_pct": None, "emissions_per_turn_hist": {}}
    emissions = _decisions(persona, kinds={"reward_emission"})
    if emissions:
        by_type: dict[str, int] = {}
        per_turn: dict[str, int] = {}
        for r in emissions:
            st = r.get("signal_type") or "self_graded"
            by_type[st] = by_type.get(st, 0) + 1
            tid = r.get("turn_id") or ""
            if tid:
                per_turn[tid] = per_turn.get(tid, 0) + 1
        total = sum(by_type.values())
        hist: dict[str, int] = {}
        for n in per_turn.values():
            key = str(n) if n < 5 else "5+"
            hist[key] = hist.get(key, 0) + 1
        out["by_signal_type"] = by_type
        out["self_graded_pct"] = round(100.0 * by_type.get("self_graded", 0) / total, 1) if total else None
        out["emissions_per_turn_hist"] = hist
    elif live_bus is not None:
        try:
            tally = live_bus.neuromod.da_source_tally()
            total = sum(abs(v) for v in tally.values()) or 0
            out["by_signal_type"] = {
                "self_graded": round(abs(tally.get("intrinsic", 0.0)), 3),
                "external_user": round(abs(tally.get("external", 0.0)), 3),
            }
            if total:
                out["self_graded_pct"] = round(100.0 * abs(tally.get("intrinsic", 0.0)) / total, 1)
        except Exception:
            pass
    return out


def summary(persona: str = "", live_wiring=None, live_bus=None, live_predictor=None) -> dict:
    plasticity = _decisions(persona, kinds={"session_plasticity_summary"})[-12:]
    return {
        "persona": persona_slug(persona) or _active_slug(),
        "plasticity": plasticity,
        "reward_mix": _reward_mix(persona, live_bus=live_bus),
        "switches": _switch_view(persona, live_wiring=live_wiring),
        "chunks": _chunks_view(persona),
        "predictor": _predictor_view(persona, live_predictor=live_predictor),
    }


# ── stories ──────────────────────────────────────────────────────────────────


def _template_stories(persona: str = "", live_wiring=None) -> list[dict]:
    """Increment-1 feed: plain-language claims synthesized on read from the
    plasticity summaries + switch positions that already exist. Replaced (not
    removed — it stays as the fallback) once the sleep narrator persists real
    stories."""
    stories: list[dict] = []
    slug = persona_slug(persona) or _active_slug()
    summaries = _decisions(persona, kinds={"session_plasticity_summary"})[-6:]
    for s in reversed(summaries):  # newest first
        sid = s.get("session_id", "")
        ts = s.get("ts") or time.time()
        for g in (s.get("top_gainers") or [])[:2]:
            edge, delta = g.get("edge", ""), float(g.get("delta", 0) or 0)
            if not edge or abs(delta) < 1e-4:
                continue
            stories.append(
                {
                    "id": f"st_tpl_{sid}_{edge}",
                    "session_id": sid,
                    "persona": slug,
                    "ts": ts,
                    "claim": f"The route {edge} strengthened by {delta:+.3f} this session — outcomes kept confirming it.",
                    "subsystem": "routing",
                    "evidence": {
                        "edges": [{"edge": edge, "delta": delta}],
                        "decision_types": ["session_plasticity_summary"],
                        "metrics": {
                            "edges_updated": s.get("edges_updated"),
                            "plasticity_modulator": s.get("plasticity_modulator"),
                        },
                    },
                    "generator": "template",
                }
            )
        for l in (s.get("top_losers") or [])[:1]:
            edge, delta = l.get("edge", ""), float(l.get("delta", 0) or 0)
            if not edge or abs(delta) < 1e-4:
                continue
            stories.append(
                {
                    "id": f"st_tpl_{sid}_{edge}",
                    "session_id": sid,
                    "persona": slug,
                    "ts": ts,
                    "claim": f"The route {edge} weakened by {delta:+.3f} — it stopped paying off.",
                    "subsystem": "routing",
                    "evidence": {
                        "edges": [{"edge": edge, "delta": delta}],
                        "decision_types": ["session_plasticity_summary"],
                        "metrics": {"edges_updated": s.get("edges_updated")},
                    },
                    "generator": "template",
                }
            )
    for sw in _switch_view(persona, live_wiring=live_wiring):
        w, (lo, hi) = sw["weight"], sw["band"]
        if abs(w - 1.0) < 0.02:
            continue
        drift = "more eager" if w > 1.0 else "more cautious"
        stories.append(
            {
                "id": f"st_tpl_switch_{sw['name']}",
                "session_id": "",
                "persona": slug,
                "ts": time.time(),
                "claim": f"I've grown {drift} about {sw['name'].replace('_', ' ')} — its learned efficacy sits at {w:.2f} within its [{lo}, {hi}] safety band.",
                "subsystem": "switches",
                "evidence": {
                    "edges": [{"edge": f"sensory.text→temporal.{sw['name']}", "w": w}],
                    "decision_types": ["switch_routing_credit_applied"],
                    "metrics": {"band": sw["band"], "position": sw["position"]},
                },
                "generator": "template",
            }
        )
    return stories


def stories(persona: str = "", limit: int = 50, before_ts: float | None = None, live_wiring=None) -> dict:
    """Newest-first learning stories: persisted (narrator) first, template
    synthesis when none exist yet."""
    recs = _read_jsonl_tail(_stories_path(persona))
    generated = False
    if not recs:
        recs = _template_stories(persona, live_wiring=live_wiring)
        generated = True
    if before_ts:
        recs = [r for r in recs if float(r.get("ts") or 0) < before_ts]
    recs = sorted(recs, key=lambda r: float(r.get("ts") or 0), reverse=True)[: max(1, min(limit, 200))]
    return {
        "stories": recs,
        "generated_on_read": generated,
        "personas": list_personas(),
    }
