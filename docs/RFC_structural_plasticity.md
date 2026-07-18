# RFC: Structural plasticity for the wiring graph

*Status: Draft / design only — no production code. Author: planning session, 2026-07-17.*
*Scope: whether and how the brain's wiring graph should grow new connections and prune dead ones, instead of only learning weights on a fixed hand-drawn topology.*

---

## 0. TL;DR (read this first)

The owner is right that the topology is frozen for life, and right that it was never a
deliberate design choice — it falls out of two `if` guards. But the honest finding of this
survey is **not** "so let's build synaptogenesis." It is sharper and more useful than that:

> **There is no consumer-backed space today in which a *grown* edge would be expressible.**
> Every edge that any consumer actually *reads* already exists in the bootstrap. The three
> weight-read spaces (drafter selection, switch efficacy, recall split) are each **complete
> bipartite graphs** — fully connected — and each enumerates its targets from a **hardcoded
> code list**, not from the graph. So a newly grown edge has nowhere to be read, and no code
> object at the far end to activate. A naive "grow edges that co-fire" rule would manufacture
> exactly the **inert-edge / dead-thalamus failure** this project spent a week removing
> (SYSTEMS.md §2.8; `finding_thalamus_gwt_stub.md`).

Two more precise facts compound this:

1. **New nodes are code, not data.** Drafters, switches, and recall strategies are Python
   objects, not graph rows. Runtime "neurogenesis" is out of scope — everyone agrees.
   But because consumers enumerate *those code objects* and merely look up each one's edge
   weight, growth "toward a new target" is impossible without also writing the target's code.

2. **`get_edge_weight()` returns the *resting* weight (1.0) for a missing edge**
   (`brain/wiring.py:187-196`). So in the existing spaces, *pruning* an edge does not silence
   a pathway — it resets that pathway's read from its learned value back to 1.0. Removing a
   weak (learned-down) edge therefore *strengthens* its behavioral read. Pruning, in the
   current read semantics, runs backwards.

**Recommendation (Option A below):** do **not** build a formation rule yet. Build the two
cheap, neutral-when-off *enablers* that (a) make the question empirically answerable and
(b) are prerequisites for any future version — a persisted grown/pruned **provenance +
tombstone** layer, and a **shadow-mode co-activation counter over non-adjacent node pairs**
that measures whether any genuinely useful, consumer-relevant, non-safety edge would ever be
proposed. Prove demand before building supply. If the counter stays empty or only proposes
edges into saturated/safety spaces (the likely outcome), the correct action is to **keep the
topology frozen and keep the SYSTEMS.md claim** — now with the precise reason attached, which
is itself a stronger honesty line than the current one.

If a concrete behavioral need for a *new pathway* does appear, the only path that yields a
non-inert result is **Option B**: open exactly one non-safety consumer to graph-enumeration
first, then let trail-graduation grow edges into *that* space, with all bootstrap edges
immutable. That is a consumer refactor with a plasticity rule bolted on, not a wiring feature
in isolation. Option C (general synaptogenesis) is not recommended — high cost, low-to-negative
value given saturation, and a real safety-bypass surface.

---

## 1. Current-state survey

### 1.1 The data model

`brain/wiring.py` is a declarative edge graph. An `Edge` (`wiring.py:39-47`) is
`(source, target, weight, polarity)`, `polarity ∈ {"excitatory","inhibitory"}`, with
`effective_weight()` signing the magnitude. Weights clamp to `[WEIGHT_MIN, WEIGHT_MAX] =
[0.1, 3.0]` and rest at `WEIGHT_REST = 1.0` (`wiring.py:32-34`; live-tunable via settings
`weight_min`/`weight_max`, `settings.py:131-132`). Edges are keyed by the `(src, tgt)` tuple
in a per-persona dict `self._by_persona[name]` (`wiring.py:65`), resolved on every access
through the active-persona contextvar (`wiring.py:88-128`).

### 1.2 Why the topology is frozen — the two guards

The freeze is not declared anywhere; it is the emergent consequence of two membership checks:

- **`hebbian_update()` (`wiring.py:252-268`)** — the sleep-time weight nudge — only touches
  keys **already** present: `if key in self._edges:` (`wiring.py:264`). Co-activation of a
  pair with no edge writes nothing. This is *the* line that freezes structure.

- **`add()` (`wiring.py:160-165`)** is idempotent: `if key not in self._edges` it mints the
  edge, else no-op. `add()` is only ever called by `bootstrap()` at boot — never from the
  learning path. So the graph gains edges exactly once, from the hand-drawn map, and never
  again.

- **`reinforce_trail()` (`wiring.py:229-246`)** — the transient overlay (see §1.5) — is guarded
  the same way: `if key in self._edges:` (`wiring.py:243`). So even the "fast plasticity"
  layer only rides existing edges; it cannot seed a new one.

The consequence, stated the way SYSTEMS.md already states it (§2.7, and the theory index
verdict at SYSTEMS.md:848-851): **"Solid for weights. False for structure. … Say 'learned
weights on a fixed map.'"** This RFC does not dispute that verdict; it asks what it would
cost to change it, and whether the change is worth anything.

### 1.3 The bootstrap — the hand-drawn map

`brain/wiring_bootstrap.py` declares ~60 edges once. `bootstrap()` is idempotent (only adds
missing edges, `wiring_bootstrap.py:18`), so reloading a populated `wiring.json` does not
reset learned weights. It is also re-run for every persona discovered at runtime
(`wiring.py:116-121`) precisely so that Hebbian's `if key in self._edges` guard has edges to
land on. **This idempotent re-add is the reason pruning is hard:** a pruned *bootstrap* edge
is resurrected on the very next boot (see §5).

Node naming convention (`wiring_bootstrap.py:6-9`): `"Cluster.cell"`, `"Cluster.switch_name"`,
or `"Cluster"` for coarse hand-offs.

### 1.4 Where weights are learned — the Hebbian pass

`brain/hebbian.py` (`HebbianUpdater`) runs at sleep. It:

- decays every edge toward rest first (`decay_toward_rest`, `wiring.py:270-276`; homeostasis
  so a pathway must keep earning its strength);
- walks each turn's `trace.fired_path`, computes a three-factor `outcome`
  (`_composite_outcome`, `hebbian.py:27-77`: DA-delta + critic + user-emotion, re-weighted when
  an external grade is present), scales by session- and turn-level plasticity, and calls
  `hebbian_update` on consecutive node pairs (`hebbian.py:500-549`);
- applies three **auxiliary credit surfaces** to edges that are never consecutive on the
  fired path: drafter competition (`hebbian.py:155-221`), switch-routing credit
  (`hebbian.py:246-295`), recall credit (`hebbian.py:314-374`), plus an eligibility trace for
  delayed payoff (`hebbian.py:376-449`).

Every one of these writes through `hebbian_update`, so every one inherits the
`if key in self._edges` freeze. **This is the natural home for a formation rule** if one is
ever built — but note that all three auxiliary surfaces already target *named, existing* edges.

### 1.5 What the trail overlay already offers (and its limit)

The stigmergic trail (`reinforce_trail`/`decay_trails`/`trail_snapshot`, `wiring.py:207-250`)
is a **transient, non-persisted, within-session** co-activation signal: each turn, edges on
the fired path get a signed bump scaled by the per-turn DA delta (`session_turn.py:1377-1405`),
decaying with a 120 s half-life, clamped to ±0.5. When `colony_features=1` **and**
`colony_trail_apply=1` (both are **on in production**, `settings.py:750,787`), the overlay is
added to the persisted weight inside `get_edge_weight` (`wiring.py:197-204`).

The trail is the most natural *seed* for edge formation — a pathway that keeps co-activating
and paying off is exactly what "wire together" means — **but as built it rides existing edges
only** (`wiring.py:243`). Co-activation of a pair with *no* edge is invisible to it. So
"trail graduation into a new edge" is not a free reuse of existing machinery; it requires a
**separate counter that tracks non-edge co-activation** (see §4.1). The idea is elegant; the
current code does not yet capture the signal it would need.

### 1.6 Persistence and multi-tenancy

Edges persist per-persona to `wiring.json` under the persona state root, or to the
`wiring_edges` Supabase table keyed `(org_id, persona, source, target)`
(`wiring.py:344-378`). Snapshots go to `wiring_history/<session_id>.json` (bounded to 100,
`wiring.py:380-409`) or the `wiring_snapshots` table. All writes go through the `_edges`
property, which resolves the active persona via the contextvar and lazy-loads
(`wiring.py:124-128`) — so **any `add()` for a grown edge would already be per-persona-correct
by construction**; there is no separate write path to get wrong. The one persistence gap is the
drift metric (next).

### 1.7 The drift metric only sees the intersection

`eval/learning_monitor.py::_cross_session_drift` (`learning_monitor.py:239-264`) computes RMS
weight change **only over edges present in *both* the current graph and the oldest snapshot**
(`old_w = old_weights.get((src,tgt)); if old_w is not None`). Grown edges (absent from the old
snapshot) and pruned edges (absent from the current graph) are **silently skipped**. So today's
drift metric would under-report structural change to zero. Any structural-plasticity work must
extend this to count edges added/removed, or the headline "cross_session_drift" would claim
stability that isn't there — a soft honesty violation.

### 1.8 The pre-existing freeze switch

There is already a hard global freeze: `BRAIN_WIRING_FROZEN=true` sets `self._wiring_frozen`
(`session_setup.py:160`), which every consumer checks (`frontal.py:1215`, `temporal.py:861,883`,
`hippocampus.py:586`) to fall back to uniform routing. This is the natural place to hang a
"structural plasticity off" default: **when frozen, no formation and no pruning**, and the
existing flag-off behavior is already proven neutral.

---

## 2. Scope decision: edges, not nodes

**Growing new EDGES between existing named nodes is data.** An edge is a dict row; `add()`
already mints one. Nothing about the storage or persistence layer forbids a runtime edge.

**Growing new NODES (neurogenesis) is code, not data — out of scope.** A "node" that any
consumer can act on is a Python object: a `SwitchNeuron` in the temporal ordered list
(`temporal.py:876-882`), a drafter in `self._drafters` (`frontal.py:1212`), a recall strategy
method in the hippocampus. Minting a graph row named `"frontal.drafter_F"` at runtime creates a
key that **nothing constructs, nothing fires, and no consumer enumerates** — an inert string.
The naming convention (`wiring_bootstrap.py:6-9`) is a convention over code that already exists,
not a factory. So:

> **In scope:** formation and pruning of *edges* between *existing* nodes.
> **Out of scope:** new nodes / cells / switches / drafters / recall strategies at runtime.

This is the realistic target, and it matches the owner's own framing. The rest of the RFC is
about edges. The sobering part (§3) is that even edges have almost nowhere to go.

---

## 3. THE CRUX: where is a grown edge behaviorally expressible?

An edge changes behavior **only** where some consumer calls `get_edge_weight(src, tgt)` (or
reads topology) and acts on the result. An edge no consumer reads is inert — it learns a
weight nothing consults and connects two nodes whose relationship nothing evaluates. Growing
inert edges is precisely the failure the task forbids.

So the entire question reduces to: **enumerate every consumer read, and ask which (src,tgt)
pairs it could read that don't yet have an edge.** Here is the complete consumer set (from an
exhaustive grep of `get_edge_weight`, `get_weight`, `has_outgoing`, `successors`):

| # | Consumer | File:line | Reads | Space shape |
|---|---|---|---|---|
| 1 | Drafter selection (`_select_drafters`) | `frontal.py:1221` | `executive → drafter_{A..E}` | **complete** bipartite (5/5), targets from `range(len(self._drafters))` |
| 2 | Switch efficacy (`_switch_efficacy`) | `temporal.py:870` | `sensory.text → temporal.<switch>`, clamped to safety band | **complete** (all banded switches), targets = fixed switch list |
| 2b | Switch ordering (`_ordered_switches`) | `temporal.py:887` | same edges | **complete**, fixed switch list |
| 3 | Recall budget split (`_recall_strategy_weights`) | `hippocampus.py:594-601` | `mem.recall → hippocampus.<strategy>` | **complete** (5/5), fixed strategy set |
| 3b | Structural-recall gate | `hippocampus.py:771` | `mem.recall → hippocampus.structural_recall` | single existing edge |
| 4 | Inhibitor magnitude (`_inhibitor_weight`) | `temporal.py:914` (used `temporal.py:587`) | `integrator_inhibitor → understanding_integrator` | single **safety** edge |
| 5 | Branching ratio σ (`branching_ratio`) | `criticality.py:51,57` | topology only (`has_outgoing`, `successors`) | reads *existence*, not weight |

**The finding.** Spaces 1, 2, 3 are the only weight-routing spaces, and **each is already a
complete bipartite graph.** Every (src,tgt) pair any of them reads already exists as a bootstrap
edge. Worse, each consumer builds its target set from a **hardcoded code list** — `range(len(
self._drafters))`, the literal five-`SwitchNeuron` list, the literal strategy dict — **not** by
enumerating the graph. So even if you minted a new edge into one of these spaces, the consumer
would never look it up: it only asks about the code objects it already knows. **There is no pair
of nodes where a consumer reads `get_edge_weight(src,tgt)` and the edge is currently absent.**

Consequence: within the existing consumer surface, the set of behaviorally-expressible *new*
edges is **empty**. A formation rule fitted to today's code would grow edges into the ~45
weight-inert cross-cluster hand-offs (e.g. `understanding_integrator → frontal.executive`,
which is walked by `fired_path` and has its weight nudged, but whose weight **no consumer ever
reads**) — i.e. it would grow more of exactly the kind of edge that does nothing. That is the
inert-edge failure, reproduced by design.

**Space 5 is a trap, not an opportunity.** The one place edge *existence* (not weight) is read
is `branching_ratio` (`criticality.py:37-60`), which feeds the live flock controller
(`flock_dynamics=1`, `settings.py:803`). Growing an edge there changes σ — but "changing a
health metric" is not useful behavior, it is *gaming the metric*. So space 5 belongs in the
**stability** section (§6, a constraint on growth), never as a formation target.

**What this means for the design.** The task asks the plan to "scope edge formation to
consumer-backed spaces." Done precisely, that scoping yields the **empty set** under the current
code. So the honest structural options are only two:

- **Prune** within the complete spaces — but see §5, pruning fights the rest-fallback semantics.
- **First open a consumer to graph-enumeration**, creating a space with room to grow, then grow
  into *that*. This is the only route to a non-inert grown edge, and it is a consumer refactor.

Everything downstream (formation rule, safety, options) is written with this in mind.

---

## 4. The formation rule (design, for the version that would be built)

This section specifies the rule for **Option B** (§7) — growth into one deliberately-opened
consumer space. It mirrors the existing three-factor weight rule so it inherits the same honesty
and the same gates.

### 4.1 The co-activation statistic (what proposes an edge)

Extend the trail idea to **non-edges**. Maintain, per persona, a small **co-activation ledger**
`coact[(a,b)]` over consecutive `fired_path` pairs **where no edge exists** — the exact
complement of what `reinforce_trail` (`wiring.py:241-244`) records. Increment on co-fire, decay
each turn (reuse the trail half-life), and — critically — **only track pairs whose (a,b) lands in
the one opened consumer space** (§7 Option B). Tracking all non-edge pairs would be an O(nodes²)
ledger of proposals that can never be expressed; scoping the ledger to the opened space is what
keeps the proposal set finite and non-inert.

A pair "graduates" to a formation proposal when its decayed co-activation crosses a
`formation_coact_threshold` **and** it has recurred across at least `formation_min_sessions`
distinct sleep passes (recurrence, not a single hot session — mirrors the "keeps earning it"
homeostatic stance).

### 4.2 The three-factor license (what licenses it)

Formation must be gated exactly like `hebbian_update`'s reward gate, so coincidence alone grows
nothing:

- **Factor 1 — co-activation:** the recurring `coact` signal above.
- **Factor 2 — reward:** the same session/turn `outcome` and plasticity scalars the weight rule
  uses (`hebbian.py:519`). Formation fires **only** on net-positive-outcome recurrence; a pair
  that co-fires on bad turns is never wired.
- **Factor 3 — chemistry/novelty:** gate on the plasticity modulator (`hebbian.py:79-86`) so
  formation, like weight learning, is chemically licensed (high-DA/ACh sessions imprint; flat
  sessions don't).

### 4.3 Newborn edge parameters

- **Weight: born at `WEIGHT_REST = 1.0`.** This is the key safety property: because
  `get_edge_weight` returns 1.0 for a *missing* edge (`wiring.py:196`), a newborn edge at 1.0
  reads **identically to its own absence**. A grown edge therefore changes nothing at birth; it
  must *earn* divergence from 1.0 through subsequent Hebbian updates before it moves any read.
  Formation is thus behaviorally reversible up to the moment the edge first diverges.
- **Polarity: always `"excitatory"`.** Inhibitory edges are dampening/safety decisions
  (`threat_to_GABA → drafter`, `integrator_inhibitor → understanding_integrator`). **Learning
  may never mint an inhibitory edge** — that would be learning a new suppression the author never
  sanctioned. All grown edges are excitatory; inhibition stays hand-drawn.
- **Provenance: `grown=True`.** Persisted, so pruning (§5) and the safety layer (§5/§6) can tell
  scaffold from learned edge.

### 4.4 Budget and rate limits

- **Per-persona cap:** `max_grown_edges` ≤ a small fraction of the bootstrap count (suggested
  ≤ 6, i.e. ~10% of ~60). Hard cap; at the cap, formation is skipped until pruning frees a slot.
- **Rate limit:** at most `formation_per_session` (suggested 1) new edge per sleep pass, so the
  graph changes slowly relative to the criticality controller's response time (§6).
- These two together bound σ movement and prevent edge explosion.

---

## 5. The pruning rule and bootstrap reconciliation

### 5.1 The rest-fallback problem (why pruning is subtle here)

Because `get_edge_weight` returns `WEIGHT_REST=1.0` for a missing edge (`wiring.py:196`),
**removing an edge is not the same as silencing it.** In the three complete spaces, an edge
learned *down* to 0.3 is read as 0.3; prune it and the read jumps back to 1.0. So "prune weak
edges" — the textbook rule — would *strengthen* the read of exactly the pathways it removes.
Pruning is only meaningful where a missing edge is read as **absent/zero**, which is not the
current semantics anywhere. **This is a decisive reason not to prune bootstrap edges in the
existing spaces.**

### 5.2 What is eligible for pruning

Only **grown** edges (`grown=True`, §4.3), and only in the opened space where a missing edge
genuinely means "no pathway" by that consumer's own contract (Option B builds the consumer so
this holds). A grown edge is pruned when it has **decayed back to rest and stayed there**
(within ε of 1.0 for `prune_min_sessions` consecutive passes) and its co-activation ledger has
gone cold — i.e. it stopped earning its existence. Pruning a grown edge that reads ~1.0 is
behaviorally neutral (it already read as its own absence), so pruning here is safe and honest.

### 5.3 Bootstrap reconciliation — the resurrection problem

`bootstrap()` re-adds any missing declared edge on next boot (`wiring_bootstrap.py:18`), so a
naively pruned bootstrap edge resurrects. Reconcile by **distinguishing scaffold from growth**:

- **Bootstrapped edges are the immutable structural scaffold** — never pruned, never
  re-polarized. `bootstrap()` keeps its resurrection behavior for them (a feature: the scaffold
  self-heals).
- **Grown edges are freely prunable** and are **never** re-added by `bootstrap()` (it only
  declares scaffold edges), so pruning one is permanent unless it re-graduates.
- A persisted **tombstone set** (`pruned: [(src,tgt), …]` in `wiring.json`, per persona) records
  grown edges that were pruned, so a still-firing stale co-activation can't instantly re-mint a
  just-pruned edge (a hysteresis / anti-flicker guard). Tombstones expire after N sessions.

This scaffold/growth split is the same idea the codebase already trusts for `_wiring_frozen`:
some structure is not up for negotiation.

---

## 6. Safety invariants (hard requirement)

The frozen topology does quiet safety work today. A growing graph must not let learning route
*around* a safety gate by growing a bypass. Define an immutable set and forbid bypass formation.

### 6.1 The immutable edge/path set

These bootstrap edges are **structural safety** and are never prunable, never re-polarizable,
and never a formation target's endpoint (see §6.2):

- **Understanding-before-answer inhibitor:** `sensory.text → temporal.integrator_inhibitor` and
  `temporal.integrator_inhibitor → temporal.understanding_integrator` (inhibitory,
  `wiring_bootstrap.py:31-37`). Its magnitude is a live consumer read (`temporal.py:587,914`).
- **Switch efficacy safety bands:** the `sensory.text → temporal.{template_match, self_reference,
  epistemic_action}` edges are clamped to **direction-aware** bands (`settings.py:163-167`;
  `temporal.py:867-871`): `template_match` may only get *less* eager, `self_reference` only
  *more* eager, so "no amount of repetition can learn a safety gate open." Formation must never
  create an alternate `sensory.text → <something> → temporal.<switch>` path that reaches the same
  switch **outside** its band.
- **GABA dampening of drafters:** `hypothalamus.threat_to_GABA → frontal.drafter_{A..E}`
  (inhibitory, `wiring_bootstrap.py:68-75`) and `→ frontal.stoic_reframer`
  (`wiring_bootstrap.py:78-80`).
- **Motor commitment path:** `frontal.commitment_extractor → motor_cortex.tool_planner`
  (`wiring_bootstrap.py:60-65`) and any motor safety-inhibitor edges. A grown excitatory shortcut
  from a drafter straight to `motor_cortex.tool_planner`, skipping the commitment extractor, is
  exactly the kind of bypass to forbid.

### 6.2 How bypass formation is structurally impossible

The clean, auditable invariant — chosen over a path-analysis check because it cannot be fooled:

> **Formation may create an edge only when *both* endpoints lie inside the single whitelisted,
> non-safety opened space, and neither endpoint is a safety node.**

Because every safety element above is *outside* the opened space, no grown edge can touch a
safety node, therefore no grown edge can form a path that routes around an inhibitor or outside
a band. Safety is preserved by **construction**, not by a reachability analysis that a clever
co-activation pattern might slip past. Concretely, maintain:

- `SAFETY_NODES` — the set of node names above (inhibitors, banded switches, GABA source, motor
  planner). No grown edge may have a source or target in this set.
- `FORMATION_SPACE` — the whitelisted (source-set, target-set) of the one opened consumer. A
  proposal `(a,b)` is admissible only if `a,b ∈ FORMATION_SPACE` and `a,b ∉ SAFETY_NODES`.

Inhibitory polarity is separately forbidden for all grown edges (§4.3), closing the "learn a new
suppression" hole.

### 6.3 The `_wiring_frozen` hard-off already exists

`BRAIN_WIRING_FROZEN=true` (`session_setup.py:160`) already disables all weighted routing. The
plasticity flag (§8) sits *under* it: frozen ⇒ no formation, no pruning, and the proven-neutral
uniform fallback. This gives an instant global kill switch that is already wired into every
consumer.

---

## 7. Stability / criticality and multi-tenant / persistence

### 7.1 Criticality (`flock_dynamics=1`, on in production)

`branching_ratio` (`criticality.py:37-60`) estimates σ from the fired path via the wiring graph:
σ = (fired edges whose both ends fired) / (fired nodes with ≥1 outgoing edge). Adding out-edges
raises the numerator and can raise σ toward super-critical; the live controller
(`FlockCriticality.control`, `criticality.py:128-150`) reacts by nudging `modulation_gain`.
**Structural change is therefore coupled to a live control loop**, not free. Mitigations, all
already implied by §4.4:

- Hard per-persona grown-edge **cap** (≤ ~10% of scaffold) bounds the maximum σ perturbation.
- **Rate limit** (≤1 formation/pass) keeps structural change slow relative to the controller's
  EMA (`flock_gain_ema_alpha`, `criticality.py:137`), so the loop tracks it rather than fighting
  it.
- **Homeostatic pruning** (§5.2) means grown-edge count does not climb monotonically; it settles.
- Optionally, exclude `grown=True` edges from the σ computation until they diverge from rest, so
  a just-born (behaviorally-absent) edge doesn't move the metric before it moves behavior. This
  keeps the σ estimate aligned with actual propagation.

### 7.2 Multi-tenant / persistence

- **Per-persona binding is already correct for writes.** `add()`, `hebbian_update`, and any
  formation call go through the `_edges` property, which resolves the active persona via the
  contextvar and lazy-loads/seeds (`wiring.py:124-128`). A grown edge is written to
  `self._by_persona[active]` and saved to that persona's `wiring.json` / `wiring_edges` row
  (`wiring.py:344-378`). No cross-persona leakage path exists for new-edge writes — they ride the
  exact mechanism weights already ride.
- **Snapshots** serialize whatever edges exist (`wiring.py:390-398`), so grown edges snapshot
  into `wiring_history` for free; the tombstone set (§5.3) should be added to the serialized
  payload so a restore reproduces prunes.
- **Drift metric must be extended** (§1.7): `_cross_session_drift` currently ignores
  added/removed edges. Add `edges_added` / `edges_removed` counts to the session summary so
  structural churn is visible and honest, and so a validation harness (§8) can assert it.

---

## 8. Options menu

Each option is stated with cost, complexity, risk, and honest value. They are cumulative: B
includes A's enablers; C includes B.

### Option A — Enablers + measurement only (RECOMMENDED)

**Do not build a formation rule.** Build the two things every future version needs and that make
the question empirically answerable:

1. **Provenance + tombstone plumbing.** Add `grown: bool` to `Edge`, a persisted `pruned` set to
   `wiring.json`/snapshots, and extend the drift metric to report `edges_added`/`edges_removed`.
   Pure data plumbing; changes no behavior; `grown` is `False` for all existing edges.
2. **Shadow co-activation counter.** Track non-edge consecutive `fired_path` pairs (the
   complement of `reinforce_trail`), decay them, and **log** which pairs would graduate and
   whether either endpoint is (a) in a consumer-read space and (b) a safety node. Log-only,
   like the N1 trail shadow-audit already does (`session_turn.py:1402` `applied=` flag). Never
   mints an edge.

- **Cost:** small (a dataclass field, a counter, a logger, a metric extension).
- **Complexity:** low. No consumer changes. Trivially neutral-when-off.
- **Risk:** ~none. Nothing routes on it.
- **Value:** high *as information*. After weeks of production data, the counter answers the
  question this RFC can only reason about: *does any useful, non-inert, non-safety edge ever want
  to exist?* The near-certain answer (given §3) is "no — every hot proposal is either into a
  saturated space or touches a safety node," which **empirically validates keeping the topology
  frozen** and upgrades the SYSTEMS.md claim from "we chose not to" to "we measured, and there is
  nothing to grow." That is a stronger honest statement than the current one.

### Option B — Grow + prune within ONE opened, non-safety space (build only if A shows demand)

If the counter surfaces a real, recurring, non-safety proposal, open the **one** consumer whose
enumeration is cheapest and safest to make graph-driven (candidate: recall fan-out in
`hippocampus.py` — not safety-critical, already tolerant of a variable-membership budget split;
explicitly **not** the switch space, which is safety-laden). Then:

- Refactor that consumer to enumerate graph successors instead of a hardcoded list, so a grown
  edge into the space is actually read (this is the prerequisite that makes growth non-inert).
- Add the §4 formation rule and §5 pruning rule, scoped to `FORMATION_SPACE` = that space, with
  the §6 safety construction and §4.4 budgets.

- **Cost:** medium (consumer refactor + formation/prune + budgets + neutral-when-off proof +
  validation harness).
- **Complexity:** medium. The consumer refactor is the real work; the plasticity rule is small.
- **Risk:** medium. A new behavioral surface; must prove flag-off is byte-identical to today and
  that σ stays bounded.
- **Value:** bounded by the concrete need. Only worth it if A found one. Do **not** build B
  speculatively.

### Option C — General co-activation synaptogenesis across all consumer-backed spaces

Open all three spaces to enumeration; allow any-to-any grown edges under the formation rule.

- **Cost:** high.
- **Complexity:** high. Three consumer refactors, one of them the safety-critical switch space.
- **Risk:** high. Opening the switch space to graph-enumeration is a direct safety-bypass surface
  (a grown path reaching a switch outside its band); σ perturbation across many edges stresses the
  live flock controller; inert-edge explosion into the ~45 unread hand-offs is likely.
- **Value:** low-to-negative given saturation. **Not recommended.**

### Option 0 — Do nothing

Keep the frozen topology. Optionally sharpen SYSTEMS.md §2.7 / Appendix A to name *why* it is
frozen (saturated + closed-enumeration consumer surface), which is a better honesty line than
"the map is hand-drawn." **This is the correct outcome if Option A's measurement comes back
empty, which it likely will.**

---

## 9. Recommendation

**Build Option A. Ship nothing that routes. Let the data decide B.**

The reasoning, honestly:

- The owner's premise is correct — the freeze is accidental (two `if` guards), and an evolving
  map is a legitimate aspiration.
- But the crux analysis (§3) shows the aspiration has **no surface to land on today**: every
  edge a consumer reads already exists, the spaces are complete, the enumeration is closed, and
  new nodes are code. Building a formation rule now would grow inert edges — the precise failure
  the project just spent a week removing. That is not a hypothetical risk; it is what the code
  guarantees.
- The rest-fallback semantics of `get_edge_weight` (§5.1) additionally make pruning run backwards
  in the existing spaces, so even the "easy half" (prune-only) is not safe there.
- Therefore the highest-value, lowest-risk move is to **measure demand** (Option A) before
  building supply, and to lay the provenance/tombstone/drift plumbing that any real version would
  need anyway. This is fully in keeping with the repo's culture: the N1 trail shipped in shadow
  mode first (`colony_trail_apply`), the thalamus shipped proven-neutral-when-off, and the
  external verdict channel was instrumented before it was turned up. Structural plasticity should
  earn its way in the same way.
- If — and only if — Option A's counter shows a recurring, consumer-relevant, non-safety edge
  that genuinely wants to exist, do the **one** consumer refactor + scoped growth of Option B.
  Do not build B on faith, and do not build C at all.

The honest one-line verdict, suitable for SYSTEMS.md if A confirms it:

> *"The map is frozen, and we measured why: every connection the brain reads already exists, and
> the read surface has no room to grow into. Structure isn't learned because there is nothing
> for learned structure to change — not yet."*

---

## 10. If Option B is greenlit: phased outline, gating, validation

*(Included for completeness; not to be built this session.)*

**Phase 0 — Enablers (= Option A).** `grown` field, tombstone set, extended drift metric, shadow
co-activation counter. Flag: `structural_plasticity_observe` (default off). Neutral-when-off:
`grown=False` everywhere, no counter unless flagged, drift metric adds fields that read 0.

**Phase 1 — Open one consumer.** Refactor the chosen non-safety consumer (e.g. recall fan-out)
to enumerate graph successors, behind `structural_plasticity_enumerate` (default off). Prove
byte-identical routing when off. This phase alone changes no topology.

**Phase 2 — Formation, shadow.** Compute proposals from the counter, apply the §4 three-factor
license and §6 safety construction, but **log only** (`structural_plasticity_apply=0`), exactly
mirroring the N1 trail shadow gate (`session_turn.py:1382`). Validate that proposals are
non-empty, non-safety, and land in the opened space.

**Phase 3 — Formation + pruning, live.** Flip `structural_plasticity_apply=1` for one persona in
one org. Newborn edges at rest (§4.3) mean the first live turn is behaviorally identical to
shadow; divergence is earned.

**Gating summary.** Nested under the existing `BRAIN_WIRING_FROZEN` hard-off (§6.3), then
`structural_plasticity_observe` → `_enumerate` → `_apply`, each default off, each independently
reversible.

**Validation — prove a grown edge changed behavior (not just existence).** The metric that
matters is not "an edge was added" but "an added edge moved a decision." Concretely:

- Instrument the opened consumer to log, per turn, whether the routing decision **differed from
  the decision the same inputs would have produced without the grown edge** (the drafter selector
  already logs `diverged_from_uniform`, `frontal.py:1240`; mirror that as `diverged_due_to_grown_
  edge`). A grown edge that never flips a decision is inert and should be pruned by §5.
- Track `edges_added` / `edges_removed` (§7.2) and assert grown-edge count stays under cap and σ
  (`criticality.observe`) stays within its band across a multi-session soak.
- A/B: one persona with `_apply=1`, one matched persona with `_apply=0`, compare outcome trend
  and σ stability. The grown-edge arm must show measurable decision divergence *and* no σ
  degradation to justify graduation.

If, at Phase 2, proposals are empty or all inert/safety-touching — the outcome §3 predicts —
**stop and ship Option 0's honesty note instead.** That is a successful outcome, not a failed one.

---

## Appendix: complete consumer-read inventory (grep-verified 2026-07-17)

`get_edge_weight` / `get_weight` weight reads that drive behavior:
- `frontal.py:1221` — executive→drafter selection (weighted sampling).
- `temporal.py:870` — switch efficacy (safety-banded).
- `temporal.py:887` — switch evaluation order.
- `temporal.py:914` (via `:587`) — integrator inhibitor magnitude (safety).
- `hippocampus.py:594-601` — recall schema/episode budget split.
- `hippocampus.py:771` — structural-recall gate.

Topology reads (existence, not weight):
- `criticality.py:51,57` — `has_outgoing` / `successors` for σ (feeds live flock controller).

All other `get_edge_weight` calls are in `hebbian.py` bookkeeping (read-before/after to log a
delta), not behavioral consumers. No consumer enumerates the graph to discover targets; every
one reads a fixed, code-defined target set. This is the structural fact behind §3.
