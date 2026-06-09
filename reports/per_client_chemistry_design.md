# Per-(persona, end_user) chemistry — design

Goal: one warm persona process serves many of a partner's end-users concurrently, each
with their own evolving mood/relationship, while the persona's learned competence and
inner life stay shared. Forced by the ~3-min cold start (can't run a process per
conversation). This is the "Version B" (concurrent-safe) build.

## The three-layer model

```
  client active mood        ← per (persona, end_user), transient, lives during a turn
        │ relaxes toward
        ▼
  persona resting mood       ← one per persona process; the DMN's inner life
        │ relaxes toward
        ▼
  persona temperament baseline   ← fixed setpoint from persona_chem resting (the trait)
```

- **Client active mood** — a `Neuromodulators` + `Hormones` pair per end_user. Seeded from
  the temperament baseline on first contact, mutated during that client's turn, snapshotted
  and persisted at turn end, restored (with absence-decay) on their next turn. This is what
  every cluster touches during a turn.
- **Persona resting mood** — a single `Neuromodulators` + `Hormones` pair representing the
  persona's background disposition when not mid-conversation. This is what the **DMN reads
  and writes**. Idle rumination is the persona thinking to *itself* (open threads, projects,
  self-model — all already persona-global), so it must never read or write any one client's
  mood. Already half-exists as `persona_chem` `current`.
- **Temperament baseline** — `persona_chem.load(persona)["resting"]`, the homeostatic
  setpoint. Immutable per turn; both layers above relax toward it.

### Mode-emergent instancing (companion mode = original behavior)
Instancing only activates under fan-out. With **one** end-user (companion mode), turns AND the
DMN bind to a **single** chemistry instance — literally the original code path, one continuous
mood stream shared by conversation and idle rumination. This is provably a no-op for the
single-user product and preserves the biologically-central conversation↔rumination loop
(today's session colors tonight's rumination; tonight's rumination colors tomorrow's mood).
The per-client active instances + a separate persona resting mood emerge only with **≥2**
concurrent end-users (engine mode). "Average of one" trivially equals that one, so the model
degrades cleanly.

### How the day's affect reaches the persona resting mood (engine mode)
Client active moods stay **fully isolated**: each seeds from the temperament baseline + that
client's own restored history, relaxes toward the temperament baseline, and is **never**
influenced by other clients. (Therapy-safe: furious client A cannot sour client B.)

The persona's *overall* mood — what the DMN ruminates in and what sleep consolidates under — is
set from an **interaction-mass-weighted aggregate of the day's per-client end moods**, blended
into the persona resting mood (`persona_chem` `current`) at each consolidation cycle:

```
resting_current ← lerp(resting_current, weighted_avg(client_end_moods), α)   # α = consolidation gain
weight(client)  = f(turns or duration)   # an intense hour weighs more than a 30-sec exchange
```

This replaces the vaguer per-turn `persona_mood_bleed` trickle with a principled, batched event
aligned to sleep/consolidation. It is **privacy-safe**: a mood aggregate is content-free numbers
(5 neuromod + 4 hormonal scalars), reveals nothing about any individual, and an average over
many clients is itself strongly k-anonymous. It leaves every individual chemical profile
untouched. The resting mood still relaxes toward the fixed **temperament baseline** over longer
time, so a string of hard days darkens *state* (mood) without altering the persona's *trait*
(temperament) — the mood-vs-temperament distinction `persona_chem` already encodes as
current-vs-resting.

**The one valve that must stay closed (the subtle failure mode):** the aggregate feeds the
resting mood → DMN rumination → shared learning (through the de-id gate) and **nothing else**.
It must **never** seed an individual client session. Each client session always seeds from the
temperament baseline + that client's own history. If the day-average were allowed to seed client
sessions, it would re-open exactly the cross-client affect channel the isolation was built to
close — at the affect level, attenuated, but re-opened. One-way only: clients → aggregate →
resting/rumination; never resting → clients.

Operational note: for an always-on engine there is no literal "day" — "end of day" means each
**sleep-consolidation cycle** (sleep.py's idle / hard-cap trigger), aggregating clients seen
since the last cycle. `α` and the weighting (mean vs trimmed-mean for robustness to one extreme
client) are tuning knobs.

## The mechanism: contextvar-bound `bus.neuromod` (no churn to 283 call sites)

Every chemistry access already goes through an injected reference — `self.bus.neuromod` /
`self.bus.hormonal` — never a module global. Exploit that: make `neuromod`/`hormonal`
**context-sensitive properties** on `Bus`, backed by a `contextvars.ContextVar` holding the
"current chemistry binding."

```python
# bus.py (sketch)
_active_chem: ContextVar[ChemPair] = ContextVar("active_chem")   # (neuromod, hormonal)

class Bus:
    def __init__(self):
        self._resting = ChemPair(Neuromodulators(), Hormones())   # persona inner life
        _active_chem.set(self._resting)                           # default binding = resting
    @property
    def neuromod(self): return _active_chem.get(self._resting).neuromod
    @property
    def hormonal(self): return _active_chem.get(self._resting).hormonal
    @contextmanager
    def bind(self, chem): t = _active_chem.set(chem); ...; finally: _active_chem.reset(t)
```

- **A turn** wraps `process_turn` in `with bus.bind(client_chem):` — every
  `self.bus.neuromod.add(...)` inside that async task resolves to *that client's* instance.
  `contextvars` are per-asyncio-task, so two concurrent turns for two clients each see their
  own chemistry through the identical access path. **This is what makes B concurrency-safe
  without editing the 283 call sites.**
- **The DMN loop** is a separate task; it leaves the binding at the default (resting) or
  explicitly `with bus.bind(bus._resting):`. Its reads/writes land on the persona resting
  mood, never a client.
- **No binding set** → resolves to resting. Safe fallback for any stray background task.

Caveat to verify in impl: confirm every cluster chemistry touch happens *within* the turn
task (awaited under `process_turn`) so it inherits the bound context. Background loops
(DMN, sleep, hypothalamus idle decay) must set their own binding explicitly.

## What each subsystem operates on

| Subsystem | State | Scope | Change needed |
|---|---|---|---|
| Turn clusters (parietal, frontal, motor, temporal, brainstem, hypothalamus decay) | live neuromod+hormonal | **per client** | none — inherits bound context |
| Hebbian (`hebbian.py`) | reads DA/ACh/NE/GABA/CORT **from `trace.neuromod`** | per trace (already snapshotted) | none — chemistry is in the trace, not live bus |
| Sleep learned state (wiring, sequence weights, chunks, self-model, open-threads) | shared | **persona** | none structurally; just don't assume one current speaker |
| Sleep per-speaker (last-seen, comm style, mood patterns) | per speaker | **per client** | already keyed by speaker_name; ensure persona-namespaced path |
| DMN rumination (drive, gate, thought deltas, conclusion rewards) | live neuromod+hormonal | **persona resting** | bind to resting; see DMN section |
| DMN persisted state (novelty cache, routing weights, open threads) | keyed (user_id, persona) | **persona** | none |
| Relationship (bond, affection, familiarity, register) | per speaker | **per client** | already per-speaker; add chemistry blob alongside |

## Sleep — mostly already correct (the relief)

Sleep does **not** write live chemistry; it only *reads* DA/ACh/NE/GABA/CORT, and it reads
them from each **trace's** snapshot (`hebbian.py` uses `trace.prior_neuromod`/`trace.neuromod`),
not from the live bus. So the chemistry refactor doesn't reach sleep's learning path at all.

Two small things:
1. **Multi-speaker batches.** A consolidation batch may now span several clients. Sleep
   already iterates unique `speaker_name`s for familiarity/comm-style/mood-patterns
   (`_update_familiarity_tiers`, `_observe_personality`) — make sure nothing assumes a single
   "current speaker." Route per-speaker outputs by each trace's speaker.
2. **Namespacing.** Per-speaker schema files must sit under the persona's second_brain root
   (largely already handled via the persona/user-scoped path). Keep shared learned weights
   (wiring.json, sequence_weights.json, chunks.json) one-per-persona.

No persona-resting vs client partition problem in sleep, because sleep operates on traces
(which carry their own chemistry + speaker) and on already-keyed per-speaker files.

## DMN — the real work

The DMN runs a background idle loop (~15s, adaptive), reads live chemistry to decide whether
and how to ruminate, writes chemistry (GABA on inward thought, DA/ACh on outward, rumination
costs, conclusion rewards), and keeps a **single in-memory speaker context**
(`_last_speaker_name`, `_last_affection_score`, `_last_familiarity`) that today is overwritten
on every turn. That singular context is the multi-client hazard.

Resolution — rumination is persona-level by nature (its subject matter — open threads,
projects, self-model — is all persona-global), so:

1. **Bind the DMN to the persona resting mood.** All its chemistry reads/writes land on
   `bus._resting`, never a client's active mood. A persona configured anxious ruminates more;
   correct and persona-appropriate. This cleanly answers "whose chemistry does idle rumination
   read?" — the persona's own, not anyone's it's currently serving.
2. **Drop the singular speaker context in multi-client mode.** `_last_speaker_name` &c. exist
   to color/gate *proactive utterances* ("should I say this thought to the person in front of
   me"). With N concurrent clients there is no single "person in front of me."
3. **Gate proactive utterance off (or per-active-session) in engine mode.** A customer-service
   or therapy persona should not spontaneously volunteer idle musings into a specific client's
   chat. Keep the *inner* loop running (rumination maintains open threads, novelty, self-model,
   and the resting-mood arc, and feeds learning) — just suppress the outward barge-in. If you
   later want proactivity, it must target an explicitly active session and bind to that
   client's mood for the utterance only.

Net: the DMN keeps its full inner life on the persona resting layer; it stops pretending it
knows which single human it's talking to.

## Persistence & lifecycle

- **Client mood** persists keyed by `(persona, end_user)`, stored alongside the relationship
  record (which is already per-speaker). On a client's next turn, restore their snapshot and
  apply **absence-decay toward the temperament baseline** by elapsed time — mirror the existing
  `relationship.apply_absence` (bond/affection) for chemistry. First contact: seed from
  temperament baseline.
- **Persona resting mood** persists as one blob per persona — this is exactly
  `persona_chem.save_current(persona, nm_snap, hs_snap)` / `load(...)["current"]`, which
  already exists. Restored on process boot.
- **Temperament baseline** is `persona_chem.load(persona)["resting"]`, already materialized
  into `chem_baseline_*`.

## Build order

1. **Bus instancing + contextvar binding** — `ChemPair`, `Bus.bind()`, context-sensitive
   `neuromod`/`hormonal` properties, default→resting. Verify cluster touches inherit context.
2. **Turn binding** — wrap `process_turn` in `bus.bind(client_chem)`; resolve client_chem
   from end_user_id (create-from-baseline / restore-with-absence-decay); snapshot + persist at
   turn end.
3. **DMN → resting** — bind DMN loop to `bus._resting`; gate proactive utterance in
   multi-client mode; neutralize the singular speaker context there.
4. **Sleep hardening** — confirm multi-speaker batches and persona-namespaced per-speaker
   paths; no chemistry change.
5. **Persistence** — client chemistry blob in the relationship store + absence-decay; persona
   resting via `persona_chem.save_current`.
6. **Tests** — (a) two interleaved turns for two end_users assert zero cross-client chemistry
   bleed; (b) DMN tick asserts it never mutates any client's active mood; (c) absence-decay
   restores a returning client correctly; (d) the day-aggregate feeds only the resting mood —
   assert it never seeds a client session (the one-way valve); (e) companion mode (one user)
   binds turns + DMN to a single instance and is byte-for-byte the original behavior.

The contextvar test (6a) is the keystone — it proves B is concurrency-safe with synthetic
interleaved turns, no real traffic needed.

## Estimate
~1.5–2.5 weeks for the chemistry instancing itself. Sleep is a near-freebie (operates on
traces, already per-speaker). The bulk is the bus contextvar mechanism + its tests, and the
DMN resting-state rebinding + proactivity gate. The cross-learning de-id gate below is a
separable workstream (~1 week + ongoing test hardening). Everything partner-specific
(routing, API surface, billing) stays deferred.

---

# Cross-learning & data isolation

The persona's end-of-day rumination *should* learn across all the people it talked to — that's
the value. The hazard is exactly one act: the DMN folding **specific** content from one user's
conversation into **shared** persona state (self-model `self.md`, open-threads ledger — both
persona-global). Engineer that single boundary; the rest is already either content-free or
siloed.

## Two kinds of learning — only one can leak
- **Procedural / numeric** — Hebbian, routing, sequence weights, motor chunks. Content-free by
  construction (floats updated by outcome signals). Cross-learn freely; this is most of the
  persona's competence. (Optional k-gate on promotion only if paranoid about membership
  inference — belt-and-suspenders, low risk at this scale.)
- **Episodic / semantic** — specific facts, memories, per-person open threads. PII lives here.
  Hard-siloed per user at the **store layer** (Supabase RLS / keyed rows), never cross-read,
  never an input to anything shared.

## The invariant: de-identification is the gate; corroboration is a confidence dial
Recurrence was a *proxy* for "not about any one person," not the real requirement. The real
invariant is **de-identification** — does the shared artifact let you reconstruct an individual?
Repetition is one sufficient route to safety; abstraction is another. So:

- **De-id is the hard gate.** Everything crossing episodic → shared passes it, single-case or not.
- **Corroboration count is a confidence weight, not an admission gate.** This is what lets novel
  single cases — often the richest signal (the expectation-violating anomaly) — be learned.

## Single novel case → de-identified hypothesis → earned confidence
A striking one-off ("a client grieved at a normally-positive topic — what does that signal?")
decomposes into: episodic (siloed: this client, this topic, this affect) and a **transferable
principle** ("affect incongruent with a topic's expected valence can signal a latent loss/
association" — names no one). The principle enters shared state as a **low-confidence,
provisional hypothesis**; it can inform behavior as a weak prior while tagged unconfirmed.
**Corroboration across distinct users promotes it** hypothesis → established pattern. This is how
a clinician reasons (theory from a striking case, confirmed over time) and it rides existing
rails: novelty-gated structural recall, prediction-error/ACh ("surprising → attend"), and
reward-on-confirmation. De-id decides *whether it can cross*; corroboration decides *how much to
trust it once across*.

## The de-identification gate (the linchpin component — three stages)
**Scope — read this first:** the gate sits *only* on the episodic → cross-user-shared pathway.
It never touches a user's own silo. The companion's full, specific memory of its one user, and
each engine client's own episodic record, keep their specifics — that's the relationship value.
The gate filters only what would be written into state *shared across users*. It abstracts; it
does not lobotomize per-user memory.

With recurrence no longer backstopping single cases, this gate carries the whole guarantee, so
it must be robust, not a regex:

1. **Extract** — LLM pass distills the structural principle; strips entities, quotes, rare
   specifics; keeps only the pattern.
2. **Re-identification check** — second pass: "could this be used to re-identify the source
   individual?" Reject if yes.
3. **Generality test (the key one)** — the abstraction must plausibly fit *many* people even
   though *one* triggered it. This is k-anonymity moved from **origin to form**: single origin +
   general form = safe ("grief at a normally-happy topic" — countless referents); single origin +
   specific form = a disguised fact ("cried about his retriever Max") = reject. The criterion is
   **k-plausible-referents**, not k-distinct-sources. This is the elegant heir to the old k-gate:
   same intuition, but it *admits* the novel case instead of suppressing it.

Only artifacts that pass all three are written to shared state. The shared DMN is structurally
**downstream** of this gate: it can read the de-identified pattern store + numeric outcomes, but
has **no read path** to per-user episodic silos. The customer's secret is never in the room.

## Deletion / right-to-erasure
A properly de-identified, general-form hypothesis is not personal data, so it legitimately
survives a user's erasure. To keep that honest, store the **case→hypothesis pointer on the
siloed side**, not in shared state. On deletion: drop the silo, decrement each contributed
hypothesis's corroboration count; if a hypothesis falls back to single-sourced and you want to
be strict, retire it. Result: "delete the silo" == "fully forgotten," provably — because PII
never entered a shared weight (you can't un-train a weight). This is the compliance line for
HIPAA/GDPR partners.

## Testing (the privacy proof — non-negotiable, ongoing)
The gate's test suite *is* the privacy guarantee. Minimum bar:
- **Re-id adversarial corpus** — fixtures with planted PII / quasi-identifiers (names, rare
  pets, dates, unique combos); assert none survive to shared state. Grow this set continuously;
  treat any escape as a Sev-1 regression.
- **Generality test fixtures** — labeled pairs (general-form vs disguised-fact) the stage-3
  classifier must separate; track precision/recall, tune for *high recall on rejection* (false
  reject a safe insight rather than admit a leak).
- **Cross-user no-bleed** — drive two users' sessions through a full day + rumination; assert no
  user-A specific can be elicited in user-B's context, and that shared state holds only
  de-identified hypotheses + numeric weights.
- **Deletion completeness** — erase a user; assert silo gone, corroboration counts decremented,
  no residual identifier anywhere in shared state.
- **Confidence promotion** — a single case enters provisional; N distinct corroborations promote
  it; deletion of contributors demotes/retires it.

Principle is solid; the gate is only as trustworthy as this suite, so it's built alongside the
gate, not after.

---

# Private (per-user) rumination tier — refinement (2026-06-09, BUILT)

> **Build status:** BUILT — `brain/private_rumination.py` (PrivateRuminator),
> `brain/hypothesis_store.py` (shared store: confidence dial + promotion + deletion
> cascade via opaque tokens), `brain/cross_learning.py` (learn_from_private wiring),
> `brain/client_chem.py::FileChemStore` (durable backend), `eval/deid_corpus.jsonl`
> + `eval/deid_eval.py` (seed adversarial corpus + harness). De-id gate extended with
> `source_context` so reid checks the original private material. All tested.
> STILL DEFERRED (needs the engine/API layer + your input): the triggers/wiring —
> who calls `process_turn(end_user_id)` and when `learn_from_private` runs
> (session-close / anomaly-gated), where the silo-side case→hypothesis pointer lives,
> mapping FileChemStore root → tenant volume, semantic (embedding) hypothesis dedup,
> and ongoing real-model corpus hardening.

**Problem.** As built, the de-id gate's *extract* stage does double duty: discover what an
outlier means AND strip it to an abstraction, in one small LLM call, every time. Relying on a
single pass to turn raw PII outliers into good abstract findings is the weak point — reasoning
and de-identification are different jobs and shouldn't share one call.

**Fix — a two-tier rumination pipeline.** Split "reason WITH the specifics" from "generalize
WITHOUT them":

1. **Private per-user rumination (in-silo, full PII, ephemeral).** A DMN-like reflective pass
   scoped to ONE customer: bound to that customer's active mood (the persona reflects in the
   mood they evoked — biologically right), reading their private episodic memory / turn traces /
   relationship. It reasons in detail, with the specifics, to reach a *candidate conclusion*.
   This is "the therapist reflecting on this specific session in full" — deep, private, unhurried.
2. **De-id handoff.** The finished candidate → the de-id gate (extract→reid→generality). Now
   *extract* only has to ABSTRACT an already-formed conclusion, not simultaneously discover it —
   a much more reliable task. The gate vets a finished thought, not raw PII.
3. **Shared rumination (DMN on resting).** Receives the de-identified insight as input and
   integrates it at the persona level. Already exists.
4. **Close.** The private ruminator is torn down after handoff. Deep PII reasoning happens in an
   ephemeral sandbox that's destroyed; only the gated abstraction ever crosses.

**Why it's better.** Higher-quality insights (the reasoning had full context) AND stronger
privacy (the heavy PII reasoning lives in a disposable, silo-scoped workspace; the gate only
ever sees a finished candidate). It's also truer to how a clinician works: reflect on the
specific case in full, carry only the generalizable lesson into broader practice.

**Architecture fit.** The private ruminator binds to the customer's ChemPair (their active mood)
and reads their silo; it is transient, silo-scoped, and writes to shared state ONLY via the
gate. It supersedes most of the gate's *extract* burden — extract shrinks from "reason +
de-identify" to "de-identify a finished conclusion"; the heavy reasoning moves upstream.

**Build decisions for the future session:**
- **Trigger** — on a customer's session-close / their consolidation, OR gated by
  novelty/anomaly/strong-affect on a turn (the expectation-violation worth reflecting on — ties
  to the prediction-error / ACh / novelty-gated machinery). Bounded so it does not run per
  trivial turn.
- **Lifecycle** — spin up per customer when warranted, reason, hand off the de-identified
  result, tear down. Cannot keep N persistent private ruminators for N customers.
- **Deletion** — the case→hypothesis pointer lives on the silo side (already specified), so
  erasing a customer's silo still cascades to demote/retire any hypothesis it seeded.

Pairs with the deferred shared-pattern store + hypothesis confidence-dial. Not built — captured
for the engine-layer build.

---

# Persona contract: identity vs mandate vs agenda (2026-06-09, design — NOT built)

The engine's core separation: **you define the being; the partner assigns the job; the persona
keeps its own mind.** Fusing these makes a bespoke build per partner instead of a reusable
engine. Three layers, three owners, three lifecycles, one precedence order.

| Layer | What it is | Owner | How set | Mutability | Write-protected from |
|---|---|---|---|---|---|
| **Identity** | who it is: character, drives, temperament | You (platform) | self.md seed + persona_chem | slow-evolving (sleep/DMN earn it) | the partner |
| **Mandate** | the *role/job* in this deployment | Partner admin | config (agent-config; optional per-session override) | declarative, doesn't evolve | the brain's own consolidation |
| **Agenda** | what it works on in *off-time* | admin-directed + self-generated | see channel rule below | append/evolve | end-users |

**Precedence (also the prompt-injection defense):** locked guiding-principles (Asimov / no-
deception, in self.md) **>** identity values **>** mandate **>** agenda. A mandate or an agenda
request can direct the job but can never override who the persona is or the safety floor. The
mandate sits *below* the locked principles, so a partner — or an end-user routed through a
mandate — cannot instruction-inject the persona into harm or deception.

**Turn-context assembly:** compose three separately-sourced, separately-owned blocks — IDENTITY
(self.md), MANDATE (partner config / per-session), AGENDA (open-threads). Today only IDENTITY
(`_core_context["self"]`) is injected; add MANDATE and AGENDA as parallel blocks. Write-ownership
is what makes the separation real: sleep/DMN may rewrite self.md + open-threads (the persona's),
never the mandate (the partner's).

**Agenda is two-sourced and scope-aware. The scope is set by WHO directs, not by forbidding
direction — anyone may ask the persona to think about something; the question is which scope it
lands in.**

- **Shared / global agenda** ← **ADMIN ONLY**, through the platform's own chat interface (NOT
  config — conversational so the persona receives/interprets it, subject to precedence; reuses the
  existing open-threads / follow-through machinery), PLUS the persona's own general self-generated
  rumination. This is what "admin directs the agenda" means — only the admin can put items on the
  SHARED agenda.
- **Per-client isolated rumination (silo)** ← the **end-user MAY direct it** ("can you think about
  X before next time?") — the persona accepts it, no rejection needed — PLUS the persona's own
  self-generated reflection about that customer. Full PII is fine here (it's the private space). It
  does NOT touch the shared agenda directly; it reaches anything shared only via the private
  rumination tier → de-id gate → shared learning.

The abuse vector (one customer hijacking a shared persona's off-time) is closed by **scoping**
end-user direction to the silo — not by forbidding it. The isolation + de-id gate already in place
is what makes accepting end-user "think about this" requests safe.

---

# Security: the isolated rumination layer (2026-06-09 threat model + agreed mitigations)

Accepting end-user *direction* turns isolated rumination into a new trust boundary: **untrusted
input now drives autonomous, resourced, off-line work.** The architecture defends PRIVACY well
(cross-silo isolation; de-id gate; confidence dial so one user can't promote a hypothesis to
established). The gaps are AVAILABILITY (cost/DoS), INTEGRITY (poisoning shared learning), and
CONTENT-SAFETY — surfaces the de-id gate was never meant to cover. The de-id gate proves "not
identifying"; we also need "bounded," "safe," and "not instructions."

**DECIDED — local (isolated/private) rumination is TOOL-LESS.** No motor / network / FS from the
per-client rumination layer. This single rule removes the entire exfiltration/SSRF surface
(directed "think about X by fetching this URL / reading that file"). The built PrivateRuminator is
already pure reasoning; the rule is: the production DMN/rumination must run motor-gated-OFF in the
isolated per-client context, and network/FS must fail closed there regardless.

Agreed mitigations (✓ = must-have before end-user-directed rumination ships; ○ = staged/knob):

- ✓ **Resource bounds (DoS / cost / fairness).** Directed rumination spends real LLM budget on a
  SHARED off-time loop — one user can run up the bill and starve other clients' off-time. Add
  per-client rumination budget + rate limit + agenda-item cap + max reasoning depth, and a fair
  scheduler so one client's queue can't monopolize off-time.
- ✓ **Safety floor in the rumination context.** Precedence (floor > identity > mandate > agenda)
  was defined for live turns; rumination must carry the same guiding-principles/identity so
  directed off-time reasoning can't be steered toward harm. (The PrivateRuminator reflect prompt
  currently omits them — fix.)
- ✓ **Agenda items are DATA, not instructions.** Persisted directed items are re-injected every
  rumination; frame them as quoted content ("the user asked you to consider: …"), never as system
  directives, or they become persistent stored prompt-injection.
- ✓ **Tool-less rumination** (the DECIDED rule above).
- ○ **Content-safety + sybil resistance (the deep one — stage it).** The de-id gate admits a
  de-identified, GENERAL-form principle regardless of whether it's TRUE or HARMFUL ("people who ask
  about refunds are usually lying" passes). The confidence dial limits a single user, but
  distinct-source counting is only as strong as end_user_id being a real distinct person — one
  attacker with many end_user_ids (sybil) defeats it. Need: a soundness/safety check on what
  crosses to SHARED (not just de-identification); sybil-resistance on distinct-source counting (tie
  contributor tokens to something costlier than a free end_user_id, or weight by source trust);
  keep provisional hypotheses low-influence and quarantinable. The confidence dial buys time but is
  not a substitute.
- ○ **Mood-average manipulation (low/attenuated).** A user driving their per-client mood to an
  extreme nudges the interaction-mass-weighted average that folds into the shared resting mood
  (sybil-amplifiable). Mitigation already designed: trimmed-mean (the robustness knob), clamping,
  per-client mass caps — turn them on.

None of this is built — captured for the engine-layer build. The four ✓ items are cheap and close
the sharp edges; #content-safety is the one real research problem and can be staged behind the
confidence dial.

**Mandate vs directed-agenda** are distinct: mandate = standing role, frames every LIVE turn;
directed-agenda = off-time work, adopted into open-threads, processed in rumination. "How I respond
now" vs "what I think about when idle."

**Engine-layer build implications:** (1) self.md gets a locked admin-owned identity/instructions
header (the persona character + non-negotiable principles) above a brain-owned evolving
autobiography. (2) Mandate is a new first-class partner-owned block, injected, never consolidated
into. (3) Agenda items carry provenance (directed/self) + scope (shared/per-client). (4) Agent-config
CRUD edits the identity-character + mandate; it must NOT let admins weaken the locked principles.
