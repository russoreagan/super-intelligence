# Round-Robin DMN — Feasibility & Design

**Status:** design / feasibility (pre-implementation). Branch base: `feat/per-persona-wiring`
(= current `claude/loving-wozniak-9d3ea4`, identical HEAD; not yet merged to `main`).
**Goal:** one pod, one DMN process, one idle loop, *rotating which persona it thinks as*, so
multiple full-tier personas get idle rumination at one-pod cost — without bleeding one persona's
stream of thought into another and without raising cost.

---

## 1. Problem recap

The brain runs **one process per (org, persona-set)** and binds a persona **per turn** (Path B) so a
single process serves many personas. Turns already rotate persona correctly: `process_turn` wraps the
whole turn body in `with bind_persona(persona)` ([session_turn.py:313](../brain/session_turn.py)), and
memory ([store.py](../brain/second_brain/store.py)), mandates, hippocampus, and now per-persona wiring
all resolve the active persona from a `ContextVar` (`_active_persona_var`).

The **DMN idle loop is the one subsystem that does *not* rotate.** `_loop` binds the **home** persona
**once** for the loop's lifetime ([dmn.py:1847-1852](../brain/dmn.py)):

```python
_home = str(settings.get("persona_name", "")) or os.environ.get("BRAIN_PERSONA_NAME", "")
if _home:
    _active_persona_var.set(_home)          # set once, never reset
```

So **all idle rumination is monopolized by the home persona.** Every other full-tier persona (e.g. the
trading analyst here, or admin elsewhere) only ever learns from *explicit* consolidation, never from
idle thinking. You can't fix this by choosing a better default — the "right" home differs per
deployment.

---

## 2. What the DMN's state actually looks like

The DMN is the most stateful subsystem. The key design input is a clean split of its mutable state into
**per-persona** (the stream of thought — must not bleed) vs **shared** (process/model-level — must stay
single). Mapped from a full read of `brain/dmn.py`:

### 2a. Per-persona transient state (the "stream of thought" — must be bundled per persona)

| Attribute | Role |
|---|---|
| `_thought_count` | the persona's idle-thought counter (drives `turn_id = dmn_N`) |
| `_recent_thoughts`, `_recent_angles`, `_recent_embeddings`, `_recent_frames` | dedup windows — **cross-persona sharing would suppress B's novel idea as a "duplicate" of A's** |
| `_consec_suppressed` | dedup-escape counter |
| `_last_rumination_seed`, `_consecutive_ruminations` | rumination depth/seed (cap is per-stream) |
| `_memory_seed`, `_event_seed`, `_event_seed_depth` | mid-thought seeds (must not carry across personas) |
| `_session_thought_buf` | per-persona buffer handed to sleep consolidation |
| `_open_threads`, `_recent_conclusions` | unfinished-idea ledger + settled conclusions |
| `_seq_predictor` | learned next-angle model (persona-specific history) |
| `_routing_weights`, `_routing_weights_loaded`, `_last_routed_ids` | learned thread-bearing weights |
| `_last_predicted_angle`, `_last_angle_confidence`, `_last_angle_informativeness` | prediction-reward stash |
| `_candidate_q`, `_proactive_q`, `_self_task_q` | speak/act output queues (see §5 — per-persona scoping is also the cost-safety mechanism) |
| `_last_context`, `_last_emotion`, `_last_speaker_name`, `_last_affection_score`, `_last_familiarity`, `_last_self_schema` | conversation snapshot (written under `bind_persona` during turns) |

### 2b. Shared, process/model-level state (stays single — and this is what keeps cost flat)

| Attribute | Why shared |
|---|---|
| `_consec_errors`, `_backoff_mult`, `_last_tick_latency`, `_last_tick_failed` | **local-model health** — if the RunPod model is down it's down for everyone |
| `_last_user_activity_ts`, `_tick_idle_s`, `_tick_idle_phase` | **engagement clock** — keep shared so the same number of ticks fire (see §6 cost) |
| `_idle_gate` (SwitchNeuron), skip-probability, `_current_interval` | the loop-level "should a tick fire now?" gate |
| `_running`, `_loop_task`, `_session_id`, `_skip_next_tick` | one loop, one lifecycle |
| the 7 `IntegratorCell`s, `_skill_selector`, `_sources_fn` | wired to the shared router; stateless-ish |
| `_projects`, `_last_projects`, `_project_*`, `_user_msg_lens`, `_user_topics` | owner/user work context, not persona-owned |

### 2c. Where the DMN reads "who am I" today (all must move to `active_persona()`)

- `_loop` once-bind ([dmn.py:1850](../brain/dmn.py)) — replaced by rotation.
- `_dmn_sb()` reads `os.environ["BRAIN_PERSONA_NAME"]` **directly** ([dmn.py:839](../brain/dmn.py)) →
  used to key the `dmn_state` Supabase row (novelty + routing weights). Must read `active_persona()`.
- Reward-weight reads `settings.get("persona_name")` at
  [dmn.py:2300, 2327, 3330, 3607, 3898](../brain/dmn.py) — must read the rotated persona.

**Important:** the *durable* DMN state (novelty cache, routing weights via `dmn_state`; open-threads via
`open_questions.md`; episodes/schema) is **already persona-keyed in storage.** The schema/episode stores
already resolve persona from the ContextVar. The novelty/routing path keys off
`_dmn_sb()` which reads env — fixing that one read makes durable state follow the rotation. **The genuinely
new piece is the *transient in-memory* stream**, which today is a single persona's.

---

## 3. The core design choice

Two correctness-equivalent ways to isolate transient state. Both are correct under asyncio interleaving
**only** if state is resolved by the task-local `ContextVar` at each access — a naive "swap the current
persona's state into flat `self._*` attributes on rotation" is **unsafe**: the loop's `_tick` awaits the
LLM, and during that await a turn coroutine for persona A can call the synchronous `update_context` /
`note_*`, writing A's data into whatever flat state the loop last swapped in. So a pointer-swap bleeds.
Rejected.

### Option A — one instance, per-persona **state bundle** resolved by ContextVar  ✅ recommended

Keep the single `DefaultModeNetwork`. Move the §2a attributes into a per-persona bundle
`self._pstate: dict[str, _ThoughtState]`, resolved at every access by
`key = active_persona() or self._home or "default"`. The §2b attributes stay flat `self._*`.

The access-site churn (these attributes are touched across `_process_thought`, `_run_monologue`,
`_run_rumination`, the thread/routing methods, `_load_*`/`_persist_*`, …) is collapsed to **zero** by
backing each per-persona attribute with a tiny **descriptor** that routes get/set into the active
bundle:

```python
class _PerPersona:
    """Routes a per-persona attribute into the active persona's bundle."""
    def __set_name__(self, owner, name): self._name = name
    def __get__(self, obj, owner=None):
        if obj is None: return self
        b = obj._bundle()                       # active_persona() or home or "default"
        if self._name not in b: b[self._name] = self._factory()
        return b[self._name]
    def __set__(self, obj, value):
        obj._bundle()[self._name] = value
    def __init__(self, factory): self._factory = factory

class DefaultModeNetwork:
    _recent_thoughts = _PerPersona(lambda: deque(maxlen=DMN_RECENT_THOUGHTS))
    _open_threads    = _PerPersona(list)
    _seq_predictor   = _PerPersona(SequencePredictor)
    # … ~18 declarations …
```

- **Existing access sites (`self._recent_thoughts.append(...)`, `self._open_threads`, …) are unchanged.**
  The descriptor returns the live object, so in-place mutation and `+= 1` work.
- **Correctness is guaranteed, not audited:** every read/write of a per-persona attribute goes through
  the one accessor, so there is no "missed a site → silent bleed" risk — the #1 risk of a manual
  `self._ps().x` refactor.
- **Consistent with the whole codebase**, which already rotates one object via the ContextVar (turns,
  consolidation, wiring, mandates, hippocampus). The DMN becomes the rule, not the exception.
- **Test-double safe:** tests build the DMN via `DefaultModeNetwork.__new__(...)` and set
  `dmn._recent_thoughts = deque(...)` directly. `_bundle()` lazily creates `self._pstate` (robust to a
  skipped `__init__`), the setter stores into the (unbound → home/"default") bundle, and later reads in
  the same test resolve the same key → same bundle. No test binds a persona, so every access in a given
  test lands in one bundle: existing behavior preserved.

### Option B — N separate `DefaultModeNetwork` instances behind a thin manager (considered, not chosen)

Build one vanilla DMN per full-tier persona; a manager owns **one** loop that round-robins
`instances[next]._tick()` (never `start()` — N loops would N× the tick rate and the cost). Isolation
comes from object boundaries (zero core change).

Rejected as the primary because: (1) it **duplicates the §2b shared state** (7 cells × N, plus backoff
and the engagement clock) and then has to *re-share* the clock/model-health across instances; (2)
`self.dmn` becomes a facade that must **forward ~30 public methods** (`update_context`, `note_*`,
`pause`, `prime_startup`, the queue drains, `session_thoughts`, …) dispatched by `active_persona()` —
lots of boilerplate, and a forgotten forward is a silent bug; (3) a single shared driver still has to
**extract the loop's gating** (idle decay, idle-gate, skip-prob, backoff interval) out of `_loop`, so it
touches that code anyway; (4) it diverges from the codebase's one-object-rotated-by-ContextVar pattern.
Option A gets the same isolation with less surface in the risky core. (Option B remains the fallback if
descriptors are judged too implicit in review.)

---

## 4. Persona selection each tick

The loop keeps its existing gates **unchanged**; the per-tick persona binding rotates, and the tick
**interval scales with the roster size** (§4a) so each persona keeps a usable cadence.

**Roster** = full-tier personas this process serves. There is no `personas` table; personas are derived
from the `agents` table:

```python
# roster (cached ~60s; refreshed lazily — personas can be added live)
rows = agents.list_agents()                                  # one Supabase read
personas = {r["persona"] for r in rows if r.get("enabled")}
roster = sorted(p for p in personas if agents.effective_tier(p) == "full")
roster = [home] + [p for p in roster if p != home]           # home always included & first
```

- LITE personas (trading debate/council seats) are **excluded** — they learn from explicit consolidation
  only and must not idle (`effective_tier` returns `"lite"`; a persona with no agents defaults `"full"`,
  so the home persona is always in).
- **Fallbacks** (never worse than today): empty roster, or Supabase unavailable → `[home]`; a single
  full-tier persona → behaves byte-for-byte like today.

**Selection policy.** Start with **pure round-robin** over `roster` for a clean, provably-fair v1
(`roster[i % len(roster)]`, advance only on a tick that actually fires). Then layer an optional
**activity/work weighting** as a tunable: bias toward a persona that (a) was engaged in a turn recently,
or (b) has the most pending `_open_threads`. Implement as occasional substitution on top of round-robin
(e.g. every K-th *eligible* slot goes to the highest-pending persona) so the fairness floor is preserved
and no persona starves. Keep weighting **off by default** in the prototype; it's a knob, not a
dependency.

Bind per tick (reset after — the loop task is long-lived, so we must not leak one tick's persona into the
next):

```python
persona = self._next_persona()               # roster rotation
with bind_persona(persona):
    if not self._hydrated(persona):
        await self._hydrate(persona)         # lazy: _load_novelty/_load_threads/_load_routing_weights
    await self._tick()                       # _tick + everything it awaits scopes to `persona`
```

`_tick` already increments `self._thought_count` etc.; under Option A those now resolve to `persona`'s
bundle automatically. Sub-tasks the tick spawns inherit the bound ContextVar (asyncio copies context),
so their memory/wiring writes scope correctly — same mechanism turns already rely on.

**Durable state hydration** is lazy per persona (load on first rotation into that persona, guarded by a
per-persona "hydrated" flag) and persisted per persona on rotate-away and at shutdown. `start()` no
longer eagerly loads only the home persona.

### 4a. Adaptive cadence — scale the tick interval with roster size

A fixed total tick rate means each persona thinks once every `interval × N` — at N=4 that's a thought a
minute, too sparse to sustain a train of thought. So the loop **shortens the interval as the roster
grows**, clamped to a floor that protects the local pod:

```python
def _current_interval(self):
    base      = float(settings.get("dmn_interval") or DMN_INTERVAL)            # per-persona target (15s)
    idle_base = float(settings.get("dmn_idle_interval") or base * 3)           # idle target (45s)
    target    = idle_base if self._idle_phase() >= IdlePhase.WANDERING else base
    n         = max(1, len(self._roster()))
    floor     = float(settings.get("dmn_min_tick_interval") or DMN_MIN_TICK_INTERVAL)  # 5s default
    return max(floor, target / n) * self._backoff_mult                         # backoff still compounds
```

Behavior (active phase, base=15s, floor=5s):

| N | tick interval | per-persona cadence | note |
|---|---|---|---|
| 1 | `max(5, 15) = 15s` | 15s | **exactly today — regression-safe** |
| 2 | `7.5s` | 15s | full cadence kept; 2× local ticks, $0 extra |
| 3 | `5s` | 15s | full cadence kept |
| 4 | `5s` (floor) | 20s | floor binds; graceful degradation |
| 8 | `5s` (floor) | 40s | pod-protected; agents share the floor |

Each persona holds its full cadence up to ~N=3, then degrades gracefully rather than linearly. The floor
binds later in the idle phase (idle target 45s ⇒ floor at N≥9), so an away user can support more personas
at full cadence. `BRAIN_DMN_MIN_TICK_INTERVAL` is the single new knob; the shared `_backoff_mult` still
multiplies the result, so a struggling pod backs the whole loop off regardless of N.

> **Why this is still cost-safe:** the extra local ticks run on the **RunPod GPU pod, which is billed per
> hour regardless of utilization** — more monologue calls on the same pod cost **$0** until they'd
> saturate its throughput, which the floor prevents. The *expensive* path (cloud reflexes) is home-only
> by §5 and does not scale with N. So the design trades strict tick-count neutrality for **pod-cost- and
> cloud-cost-neutrality bounded by a local-throughput floor** — see §6.

---

## 5. Output-queue scoping (correctness + cost safety, for free)

The speak/act queues (`_candidate_q`, `_proactive_q`, `_self_task_q`) are **per-persona** under Option A.
Every site that *drains* them runs **unbound (→ home)**:

- `take_self_task()` — drained by `_task_worker_loop` ([session_loops.py:662](../brain/session_loops.py))
- `take_proactive()` — drained by the proactive loop ([brain_session.py:321](../brain/brain_session.py))
- `take_oldest_candidate()` / `candidate_count()` — speak gate
  ([session_loops.py:331-349](../brain/session_loops.py))

So **only the home persona's outputs ever reach the user / the job worker.** Non-home personas ruminate
and *learn* (memory, wiring, open-threads, conclusions) but their proactive utterances and self-task
reflexes accumulate in their own bounded deques and are simply never drained. This gives us two things
without any special-case code:

1. **No persona confusion** — persona B never blurts into the owner's conversation with persona A.
2. **The expensive path stays bounded** — self-directed jobs (the costly motor/cloud reflexes) are only
   ever spawned by the home persona, exactly as today. This is the direct guardrail against the kind of
   runaway that cost ~$100/day. (A later iteration could let a *promoted* persona's self-tasks run under
   a strict per-roster budget; v1 deliberately does not.)

---

## 6. Cost model

**Claim: pod-cost-neutral and cloud-cost-neutral, bounded by a local-throughput floor. NOT strict
tick-count neutrality — adaptive cadence (§4a) deliberately fires up to N× more local ticks, but on the
same fixed-hourly pod, so the dollar cost is flat.**

The cost driver is what each tick *touches*:

- **Cheap local path (the monologue tick).** Runs on `model="runpod"` — the GPU pod billed **per hour
  regardless of utilization.** Adaptive cadence fires more local ticks as N grows (up to the `floor`),
  but on the **same already-paid pod** ⇒ **$0 extra** until throughput saturates, which the floor exists
  to prevent. The shared `_backoff_mult` already lengthens the interval when the local model struggles,
  so contention self-corrects.
- **Expensive cloud path (self-task → motor/cloud reflex).** This is what caused the ~$100/day Haiku
  runaway. It stays **home-only** by §5 (non-home output queues are never drained) ⇒ **does not scale
  with N at all.** No new cloud LLM calls, no new job spawning.
- **Loop-level gates stay shared (§2b).** The engagement clock + idle-gate + skip-probability are global,
  so we do **not** create "some persona is always idle ⇒ ticks fire during active conversation." During
  active use, ticks are suppressed exactly as today; rotation + cadence-scaling apply to the *idle*
  budget only. Letting the engagement clock go per-persona would silently raise tick volume during
  conversation — explicitly avoided. *(Single most important cost invariant; restated in §7.)*
- **Cadence trade.** Each full-tier persona gets a tick every `max(floor, target/N)`. Up to ~N=3 each
  persona keeps its full solo cadence (the pod absorbs the extra ticks free); beyond that the floor binds
  and personas share a fixed total rate — graceful, pod-protected degradation, not linear starvation.
- **New overhead, all negligible $:** one cached `list_agents()` read (~1/min) + lazy per-persona
  `dmn_state` / `open_questions` loads (a handful, one-time-ish) + per-persona persists. **No new pods.**

**The one physical limit to watch:** local-model throughput. If the roster is large *and* the pod is
small, ticks at the floor could contend with real turns for the local model. Defenses: the `floor`
itself, the shared `_backoff_mult` (a slow/failing pod backs off the whole loop), and the existing
idle-gate/skip-prob suppression. If contention is ever observed, raise `BRAIN_DMN_MIN_TICK_INTERVAL` —
it dials all the way back to today's single-rate behavior.

---

## 7. Riskiest parts (honest)

1. **Missed per-persona attribute → silent cross-bleed.** The worst failure (looks fine, quietly
   corrupts learning). *Mitigation:* the descriptor approach routes *every* access through one accessor,
   so isolation is structural, not audited; plus a focused cross-bleed test (deliverable 2) and the full
   1945-test suite.
2. **Cost regression** if a loop-level gate or the engagement clock drifts into per-persona scope (would
   fire idle ticks *during* active conversation), or if the cadence floor is set too low (local-model
   contention with real turns). *Mitigation:* §2b is explicitly shared — all "should a tick fire" logic
   stays global; only the binding + §2a stream state + the interval *divisor* (§4a) change. The `floor`
   and shared `_backoff_mult` bound local load; N=1 must reproduce today's interval exactly (regression
   test). Note the cost claim is **pod/cloud-neutral**, not strict tick-count-neutral (§6).
3. **Test-double compatibility** — 11 DMN test files build via `__new__` and set attributes directly.
   *Mitigation:* `_bundle()` lazily creates `self._pstate`; setters/getters resolve the same key when no
   persona is bound (the test case). Validate by running the suite green before declaring done.
4. **Durable-state hydrate/persist races** across personas. *Mitigation:* per-persona hydrated flag;
   persist on rotate-away + shutdown; load is read-mostly and idempotent.
5. **Roster churn / Supabase availability.** *Mitigation:* cache + fallback to `[home]` on any error ⇒
   never worse than the current single-persona behavior.
6. **Rotated personas ruminate over shared/ambient conversation context** if `_last_context` is left
   shared. v1 puts the conversation snapshot in the per-persona bundle (it's already written under
   `bind_persona` during turns), so each persona ruminates over *its own* last exchange (or nothing).
   The remaining shared signal is the engagement *clock* (intentional, per §6). Worth a careful look in
   review since the in-memory map flagged these as "shared" under a single-user assumption.
7. **`_seq_predictor` persistence path** is currently a single local file; per-persona instances must key
   their save/load by persona (in Supabase mode it rides `dmn_state`). Low-risk; note as a follow-up if
   the prototype keeps the local-file path.

---

## 8. Prototype plan (branch only — no deploy)

Branch `feat/round-robin-dmn` from `feat/per-persona-wiring`.

1. Add `_PerPersona` descriptor + `_bundle()` + `_pstate` (+ `_home`, roster cache, hydrated flags).
2. Convert the §2a attributes to descriptors; delete their now-redundant `__init__`/`_ensure_runtime_state`
   initializers (the factory supplies defaults).
3. Replace the `_loop` once-bind with per-tick roster rotation + lazy hydrate; keep all gating shared.
4. **Adaptive cadence (§4a):** `_current_interval` divides by `len(roster)`, clamped to
   `BRAIN_DMN_MIN_TICK_INTERVAL` (default 5s); N=1 reproduces today exactly.
5. Point `_dmn_sb()` and the reward `persona` reads at `active_persona()`.
6. Roster helper over `agents.list_agents()` + `effective_tier`, cached, home-first, fallback `[home]`.
7. **Focused tests:** (a) drive ≥2 personas through ticks and assert each accrues its own
   `_recent_thoughts` / `_open_threads` with **no cross-bleed**; (b) single-persona roster ⇒ behavior +
   interval unchanged (regression guard); (c) interval scales `target/N` and respects the floor.
8. `uv run python -m pytest` green (~1945 tests). Bound any new loop tightly; never let the prototype
   run unbounded reasoning.

**Checkpoint:** confirm this design + the cost model (§6) before the §8 refactor.

---

## 9. Implementation status (prototype landed — branch `feat/round-robin-dmn`)

Implemented as Option A (single instance, per-persona descriptor bundle) with adaptive cadence and
pure round-robin, on branch `feat/round-robin-dmn` (from `feat/per-persona-wiring`). **No deploy.**
`brain/dmn.py` +370/−64; `tests/test_dmn_round_robin.py` added (8 tests). **Full suite green: 1953
passed** (`BRAIN_STORAGE_BACKEND=local uv run python -m pytest`).

What landed:
- `_PerPersona` descriptor + `_bundle()` route the §2a attributes into the active persona's bundle,
  keyed by canonical slug. Zero change to the hundreds of `self._attr` access sites.
- `_loop` rotates the persona per *fired* tick (`_next_persona`, home-first round-robin) and binds it
  for the tick + lazy `_hydrate`; all loop gating stays shared/unbound.
- `_current_interval` divides the target by `len(_roster())`, clamped to `BRAIN_DMN_MIN_TICK_INTERVAL`
  (default 5s). `_roster()` derives full-tier personas from `agents.list_agents()`+`effective_tier`,
  cached `BRAIN_DMN_ROSTER_TTL_S` (60s), fallback `[home]`.
- `_dmn_sb()` + the reward-weight reads now resolve `active_persona()` (canonicalized), so durable
  `dmn_state` rows and reward scaling follow the rotated persona.
- Hydrate home eagerly in `start()`; persist **all** hydrated personas at `shutdown()`.

Two design refinements made during implementation:
- **`_bundle` caches `_home` via `_resolve_home()`.** The unbound fallback must resolve home the *same*
  way `start()`/`prime_startup()` bind it, or a write-bound-to-home and an unbound read land in
  different bundles. Caching `_home` on first access keeps them identical (and is robust to `__new__`
  test doubles).
- **`_dmn_sb` now keys `dmn_state` by the persona *slug*** (was the raw display name). This makes it
  consistent with episodes/schema/wiring and lets rotated personas isolate cleanly. One-time effect:
  an existing hosted brain's home novelty/routing cache (keyed by display name) is orphaned on first
  run after deploy — non-critical warm-start state, repopulates within a session.

Known follow-ups (non-blocking; flagged in §7):
- **Per-persona seq-predictor + local-file persistence.** `SequencePredictor` and the local novelty /
  routing files use one process-global path, so their *durable* writes are gated to home-only; rotated
  personas keep these in-memory-only (fresh each session). The Supabase `dmn_state` path *is*
  per-persona. Scoping the local paths by persona is the follow-up.
- **Activity/work-weighted selection** remains an off-by-default knob on top of the round-robin floor.
- **Output queues are per-persona and drained from home** (§5): non-home proactive speech / self-tasks
  stay internal by construction. If a non-home persona should ever *act*, that needs an explicit,
  budgeted drain path — deliberately not in v1.
