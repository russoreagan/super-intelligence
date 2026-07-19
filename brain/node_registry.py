"""
NodeRegistry — the explicit name→node map for the wiring graph.

The wiring graph (brain/wiring.py) is a set of edges between *name-strings* like
"frontal.drafter_A" or "temporal.template_match". Those strings are meant to point at real
runtime objects (IntegratorCell / SwitchNeuron) living in the clusters — but historically the
mapping was IMPLICIT: it held only because a human typed matching strings in two places. A typo,
a renamed cell, or a deleted behavior produced a silently-inert edge with no backing object and
nothing to catch it. That is the "dead name" failure class behind the thalamus/attention.focus
no-subscriber bug (docs/SYSTEMS.md §2.7–2.8).

This registry makes the mapping EXPLICIT and ENFORCED:
  - object-backed nodes (cells, switches) register themselves at cluster construction, and
  - non-object nodes (bus channels, recall strategies, coarse subsystems) are declared in the
    NON_OBJECT_NODES manifest below.
`audit_node_registry()` then reconciles the registry against the wiring graph at boot and logs
ORPHAN NAMES (graph nodes with no backing object and no classification — the dead-edge danger)
and UNWIRED OBJECTS (registered objects absent from the graph).

Canonical node name = f"{cluster}.{name}". This equals both the wiring-graph node names
(wiring_bootstrap.py) and the fired_path names emitted by observability/firing_path.py
(record_switch_fire / record_integrator_call both build f"{cluster}.{name}"). All three
namespaces are kept aligned.

NEUTRALITY: this registry changes NO runtime behavior. It is write-only at construction and
read-only at audit; nothing routes through it. Consumers (frontal._select_drafters,
temporal._ordered_switches, hippocampus._recall_strategy_weights) are untouched.

PER-PROCESS, NOT PER-PERSONA: the cell/switch objects are process-level singletons shared across
personas (stateless between turns via reset_turn), so this registry is intentionally per-process.
What IS per-persona is the wiring graph's edges/weights (resolved via the active-persona
contextvar in wiring.py). The boot audit reads the boot/active persona's graph — sufficient
because the bootstrap topology is identical across personas; only weights diverge.

Modeled on brain/clusters/lobe_bridge.py (a capability→handler registry for motor tools). Kept
dependency-free (stdlib only) so cluster __init__s can import it without any import cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# The kinds a node may be classified as.
#   cell / switch  → object-backed (resolve() returns the live IntegratorCell / SwitchNeuron)
#   channel        → a literal bus topic (bus.subscribe(topic)); not an object
#   strategy       → a mode of a method (e.g. a hippocampus recall strategy); not an object
#   subsystem      → a whole cluster / planner / virtual convergence node; not an object
#   fragment       → a learned-attached curated capability fragment (Tier 1 structural
#                    plasticity); NON-object-backed (content, not compute). A fragment node
#                    exists in the graph only once it has a per-persona attachment edge
#                    `fragment.<skill_id> → <host_cell>`; register_fragment_nodes() classifies
#                    whichever ones the active persona currently has so the audit sees no
#                    orphan. Not in _OBJECT_KINDS → resolve()→None → never counted UNWIRED.
NODE_KINDS = frozenset({"cell", "switch", "channel", "strategy", "subsystem", "fragment"})
_OBJECT_KINDS = frozenset({"cell", "switch"})


@dataclass(frozen=True)
class RegisteredNode:
    """One registry entry. `obj` is the backing runtime object for object-backed kinds,
    else None."""

    name: str
    obj: Any
    kind: str
    cluster: str


class NodeRegistry:
    """Process-level map from canonical node name → RegisteredNode."""

    def __init__(self) -> None:
        self._nodes: dict[str, RegisteredNode] = {}

    def register(self, name: str, obj: Any, *, kind: str, cluster: str) -> None:
        """Register (or overwrite) a node under its canonical name. `obj` may be None for
        non-object kinds (channel/strategy/subsystem)."""
        if kind not in NODE_KINDS:
            raise ValueError(f"unknown node kind {kind!r}; expected one of {sorted(NODE_KINDS)}")
        self._nodes[name] = RegisteredNode(name=name, obj=obj, kind=kind, cluster=cluster)

    def register_object(self, obj: Any, *, kind: str) -> None:
        """Convenience for object-backed nodes: derive the canonical name and cluster from the
        object's `.cluster`/`.name` attributes (both IntegratorCell and SwitchNeuron expose
        them) and register it. Keeps registration co-located with construction with no repeated
        f-string to drift."""
        if kind not in _OBJECT_KINDS:
            raise ValueError(f"register_object expects an object kind; got {kind!r}")
        cluster = obj.cluster
        name = f"{cluster}.{obj.name}"
        self.register(name, obj, kind=kind, cluster=cluster)

    def resolve(self, name: str) -> Any | None:
        """The backing runtime object for an object-backed node, else None (for
        channel/strategy/subsystem, and for unknown names)."""
        entry = self._nodes.get(name)
        return entry.obj if entry is not None else None

    def classify(self, name: str) -> str | None:
        """The kind of a registered node, or None if the name is not registered."""
        entry = self._nodes.get(name)
        return entry.kind if entry is not None else None

    def all_names(self) -> set[str]:
        return set(self._nodes.keys())

    def by_kind(self, kind: str) -> list[str]:
        return sorted(name for name, e in self._nodes.items() if e.kind == kind)

    def is_object_backed(self, name: str) -> bool:
        entry = self._nodes.get(name)
        return entry is not None and entry.kind in _OBJECT_KINDS

    def clear(self) -> None:
        """Drop all registrations. For test hygiene against the process singleton."""
        self._nodes.clear()

    def __len__(self) -> int:
        return len(self._nodes)


# ── Process singleton ────────────────────────────────────────────────────────
_REGISTRY = NodeRegistry()


def get_node_registry() -> NodeRegistry:
    """The process-level registry. Cluster __init__s register their object-backed nodes here;
    session_setup registers the non-object manifest and audits against it at boot."""
    return _REGISTRY


# ── Non-object node manifest ─────────────────────────────────────────────────
# The wiring graph declares nodes that are NOT backed by a runtime object. Each is listed here
# with its kind, so the boot audit knows it is intentionally non-object rather than a dead name.
# This is the single, human-curated source of "intentionally non-object" truth — keep it in sync
# with wiring_bootstrap.py (the reconcile test enforces that any drift surfaces as an orphan).
NON_OBJECT_NODES: dict[str, str] = {
    # Literal bus topics — nodes fed/consumed via bus.subscribe(topic), not objects.
    "sensory.text": "channel",
    "mem.recall": "channel",
    # Hippocampus recall strategies — modes of a method (hippocampus._recall_strategy_weights),
    # weighted by mem.recall→hippocampus.<strategy> edges; no standalone object per strategy.
    "hippocampus.cosine_recall": "strategy",
    "hippocampus.schema_grep": "strategy",
    "hippocampus.entity_tracker": "strategy",
    "hippocampus.time_filter": "strategy",
    "hippocampus.structural_recall": "strategy",
    # Coarse subsystems / planners / virtual nodes.
    "hypothalamus": "subsystem",  # bare cluster name — coarse "handoff to hypothalamus" edge
    "hypothalamus.threat_to_GABA": "subsystem",  # named signal transforms computed by the
    "hypothalamus.valence_to_DA": "subsystem",  # hypothalamus cluster, not switch/cell objects
    "hypothalamus.novelty_to_ACh": "subsystem",
    "hypothalamus.arousal_homeostat": "subsystem",
    "hippocampus.recall": "subsystem",  # coarse "hand off to recall" target
    # Virtual convergence node: all recall strategies fan into it in the graph, but there is NO
    # backing object (hippocampus has only encoder/coordinator cells). Intentional topology sink.
    "hippocampus.recall_aggregator": "subsystem",
    "frontal.commitment_extractor": "subsystem",  # self-monitor code helper, not a cell/switch
    "frontal.approach_stage": "subsystem",  # stance-credit ANCHOR for the pre-tool approach
    # competition — fragment.<sid> edges land here (single logical host so credit never
    # smears across the interchangeable approach cells). Bookkeeping node, never fires.
    "motor_cortex.tool_planner": "subsystem",  # motor planner subsystem
    # Parietal session-state holders — cluster-internal state, exposed as coarse handoff nodes.
    "parietal.recent_turns_ringbuffer": "subsystem",
    "parietal.topic_vector_holder": "subsystem",
    "parietal.entity_tracker": "subsystem",
}


def register_manifest(
    registry: NodeRegistry | None = None, manifest: dict[str, str] | None = None
) -> None:
    """Register the non-object manifest into `registry` (default: the process singleton).
    Skips any name already registered as an object-backed node, so a manifest entry can never
    silently overwrite a live object with a classification-only (obj=None) entry."""
    registry = registry if registry is not None else get_node_registry()
    manifest = manifest if manifest is not None else NON_OBJECT_NODES
    for name, kind in manifest.items():
        if registry.is_object_backed(name):
            continue
        cluster = name.split(".", 1)[0]
        registry.register(name, None, kind=kind, cluster=cluster)


def _graph_node_names(wiring: Any) -> set[str]:
    """Every distinct node name in the active persona's wiring graph (both edge endpoints).
    The graph exposes nodes only implicitly as edge endpoints — there is no public nodes()."""
    return {n for (src, tgt) in wiring._edges for n in (src, tgt)}


def register_fragment_nodes(wiring: Any, registry: NodeRegistry | None = None) -> int:
    """Classify every learned-attachment fragment currently present in the active persona's
    wiring graph, so the boot audit sees no ORPHAN for `fragment.*` edge endpoints.

    A fragment node enters the graph only once it has an attachment edge (per persona), so
    this registers exactly the fragments the active/boot persona has learned. Fragments are
    NON-object-backed (obj=None) → they never count as UNWIRED. Idempotent (guarded on
    classify). Returns the count newly registered."""
    registry = registry if registry is not None else get_node_registry()
    n = 0
    for name in _graph_node_names(wiring):
        if name.startswith("fragment.") and registry.classify(name) is None:
            registry.register(name, None, kind="fragment", cluster="fragment")
            n += 1
    return n


def audit_node_registry(
    wiring: Any,
    registry: NodeRegistry | None = None,
    *,
    log: logging.Logger = logger,
) -> dict:
    """Reconcile the registry against the wiring graph (active/boot persona) and log a report.

    Returns a dict: {orphans, unwired, graph_nodes, registered, object_backed}.
      - ORPHAN NAMES: graph node names with no registered object AND no classification — the
        potential dead edges. Logged at WARNING (the immediate payoff of the registry).
      - UNWIRED OBJECTS: registered object-backed nodes whose name never appears in the graph.
        Logged at INFO (lower severity).
    """
    registry = registry if registry is not None else get_node_registry()
    graph_names = _graph_node_names(wiring)
    registered = registry.all_names()
    object_backed = {name for name in registered if registry.is_object_backed(name)}

    orphans = sorted(graph_names - registered)
    unwired = sorted(object_backed - graph_names)

    if orphans:
        log.warning(
            "[node-registry] %d ORPHAN graph node(s) with no backing object or classification "
            "(possible dead edges): %s",
            len(orphans),
            orphans,
        )
    if unwired:
        log.info(
            "[node-registry] %d registered object(s) not present in the wiring graph: %s",
            len(unwired),
            unwired,
        )
    log.info(
        "[node-registry] audit: %d graph nodes, %d registered (%d object-backed), "
        "%d orphans, %d unwired",
        len(graph_names),
        len(registered),
        len(object_backed),
        len(orphans),
        len(unwired),
    )
    return {
        "orphans": orphans,
        "unwired": unwired,
        "graph_nodes": len(graph_names),
        "registered": len(registered),
        "object_backed": len(object_backed),
    }
