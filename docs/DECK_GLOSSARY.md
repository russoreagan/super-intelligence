# Deck glossary

Companion to `docs/DECK_BRIEF.md`. Derived from the vocabulary actually used in `docs/SYSTEMS.md` Part I, by frequency.

**Why this exists.** The deck was written by someone who already knew the system, for someone who already knew the system. Terms arrive mid-sentence with no introduction and are assumed to carry meaning: a slide says a skill is "wired onto one of the drafting cells" and "rides the drafts" before the reader has been told what a cell is, what a drafter is, or what a draft is. `turn` appears 76 times in Part I and is never defined. That is fine in a reference someone reads front to back. It is fatal in a deck, where any slide may be the first one someone looks at.

**How to use it.** Two rules:

1. **First use defines.** The first slide in the deck that uses a term gives it a plain-language definition in the standfirst. After that it is used freely.
2. **No slide is a dead end.** If a slide uses a term defined on an earlier slide, the locator strip names where it came from.

The glossary itself becomes two slides in the deck (see the brief, Part 5 opener). The rest of this file is the source for those slides and the definition of record when writing any individual slide.

---

## The words that carry the most weight

Ordered by how often they appear in Part I. The high-frequency ones are the dangerous ones, because familiarity made them invisible to the author.

| Term | Uses | Definition to use |
|---|---|---|
| **turn** | 76 | One complete exchange: input arrives, the system perceives, understands, recalls, drafts, commits, and replies. The unit of almost everything else — memory stores one record per substantive turn, chemistry updates twice per turn, learning credits the routes that fired during a turn. **Define this on the first slide that uses it. It is currently defined nowhere.** |
| **persona** | 58 | A personality you can talk to: a fixed temperament and cognitive style, plus a mood that moves. Cheap; make as many as you like. |
| **agent** | 52 | A persona put to work — paired with a mandate and given bounded permissions. Persona is who; mandate is what job; agent is the pairing that actually runs. |
| **gate** | 48 | **Overloaded — see Disambiguation below. Never use bare "gate" on a slide.** |
| **chemistry** | 36 | The nine simulated neuromodulator and hormone channels, taken together. Not a mood label the system picks; a state that exists whether or not anyone asks. |
| **draft** | 34 | One candidate reply, written in full. Five are written in parallel each turn and one is sent; the rest are discarded. |
| **cell** | 28 | The unit of thinking. Two kinds: switches and integrators. A cell is code, not a database row. |
| **switch** | 18 | A cell with no model behind it — plain deterministic code. The large majority of cells. About one in five is inhibitory, so the system settles rather than only exciting itself. |
| **integrator** | 2 | A model-powered cell. Fires only where many signals converge. The expensive ones; there are few. |
| **attachment** | 24 | A vetted skill wired onto a drafting cell, with a strength that learns like any other weight. The mechanism behind structural growth (§2.9). |
| **weight** | 23 | The learned strength of one connection in the wiring graph. **Not** the same as a reward weight (see Disambiguation). |
| **wiring** | 19 | The graph of roughly sixty declared connections between named cells. Weights are learned; the core map is fixed. |
| **edge** | 2 | One connection in that graph. |
| **critic** | 17 | The cell that scores the parallel drafts. A separate empathy critic can veto a draft outright. |
| **commit** | 16 | Choosing one draft and sending it. The brainstem waits for a beat with no new draft, then commits the best survivor. |
| **channel** | 13 | One of the nine chemicals. **Also overloaded** — see Disambiguation. |
| **mandate** | 12 | A job description, stored as data rather than prompt text. Swappable; an organization authors a catalog once and assigns any of them to any persona. |
| **cluster** | 9 | A named group of cells doing one job, mapped to a brain region. Eleven of them. Three carry real behavior with no model at all. |
| **drafter** | 9 | One of five frontal cells that each write a full candidate reply, with different dispositions — warmer, terser, more analytical. |
| **plasticity** | 9 | Change in the wiring. Weight plasticity moves strengths; structural plasticity adds attachments and, at the second tier, whole units. |
| **workspace** | 9 | The global broadcast layer. The thalamus is the one reader that sees every topic at once and fuses them into a single verdict. |
| **tenant** | 8 | An organization. The unit of isolation: data, keys, and compute are scoped to it. |
| **reflex** | 8 | A recurring sub-sequence of tool use, compressed into a single unit that fires as a whole. |
| **signature** | 7 | A content-free description of a moment: same chemistry, same structural problem, topic deliberately excluded. What lets the system match a database-debugging session to a conversation about a marriage. |
| **recall** | 7 | Retrieval from the long-term store. Has a fixed lookup budget split across four strategies, and the split is learned. |
| **gain** | 6 | A multiplier, not a message. The slow chemical layer sets the gain on the fast one, which is why accumulated stress makes the same remark land harder. |
| **bond** | 6 | The accumulated state of one relationship with one customer, carried across sessions. |
| **ledger** | 5 | An append-only record. Two matter: the learning ledger (what changed and why) and the open-threads ledger (unfinished thoughts). |
| **episode** | 5 | One stored turn: what was said, what was answered, the mood at the time, who was involved, how surprising it was, and a vector for finding it by meaning. |
| **setpoint** | 4 | The resting value a chemical channel relaxes back toward. Differs per persona — the setpoint is the fingerprint, not the disturbance. |
| **screener** | 4 | The admission check every skill passes before it can be used or attached, whoever submitted it. |
| **chunk / recipe** | 4 | A finished multi-step job kept for reuse, each step carrying what it expects to happen. |
| **ignition** | 3 | The moment a coalition of signals crosses threshold and its content is broadcast system-wide. |
| **salience** | 2 | Accumulated, decaying importance per topic. The field the workspace reads. |
| **schema** | 2 | The human-readable markdown notes — `self.md`, one file per person, open questions. |
| **consolidation** | 2 | The rest-time pass that distills episodes into durable notes, updates wiring, and mines reflexes. |
| **bus** | 2 | The publish/subscribe layer clusters communicate over. Nothing dispatches; cells subscribe to topics and fire on their own thresholds. |
| **efficacy** | 1 | A learned divisor on a switch's threshold. Where feeling becomes behavior: chemistry moves the threshold, efficacy scales it. |
| **DMN** | 1 | Default Mode Network — the idle mind. Spell it out on first use; the abbreviation means nothing to most readers. |
| **PAD** | 1 | Pleasure-Arousal-Dominance, rendered here as pleasantness, energy, confidence. Spell it out on first use. |

---

## Disambiguation: terms that mean more than one thing

These are the ones that will actively mislead, because the reader will carry the first meaning forward into the second use. **Never use the bare word on a slide — always use the qualified form.**

### "gate" — at least seven distinct referents

| Qualified form | What it actually is |
|---|---|
| **predictor gate** | The cheap check that skips the model when a cluster can already predict its own conclusion. §2.2. |
| **safety gate** | The approval a human must grant before a risky action runs. Closed by default, deliberately. §6.6. |
| **privacy gate** | The k-anonymity check on cross-customer learning. §9.11. |
| **learning gate** | The chemical third factor licensing a weight change. §4.1. |
| **spend / money gate** | The budget ceiling on autonomous work. Chemistry may never widen it. §6.7. |
| **org gate** | Tenant isolation at the query boundary. §9.1. |
| **gated by chemistry** | The verb, meaning "modulated by" — not a gate at all. Prefer "modulated by" and reserve "gate" for nouns. |

### "channel"

| Qualified form | What it is |
|---|---|
| **chemical channel** | One of the nine neuromodulators or hormones. |
| **bus topic** | A publish/subscribe channel between clusters. Say "topic," never "channel." |
| **external verdict channel** | The path by which an outside grade reaches the reward system. |

### "valence"

SYSTEMS.md §1.6 already warns that three different things carry this name. On a slide, always say which:

- **pleasantness** — the continuous PAD dimension. This is the one usually meant; prefer this word and drop "valence" entirely.
- **core valence** — the fixed positive/negative sign attached to an emotion family.
- the third is an internal appraisal variable. If a slide needs it, name it explicitly rather than calling it valence.

### "weight"

- **edge weight** — the learned strength of a wiring connection.
- **reward weight** — what a given persona finds satisfying. Unrelated to the wiring graph.

### "connection" — the one that will actively contradict itself

The wiring-graph slide says learning grows no new connections. The structural-plasticity slides
say it grows new connections. Both are true, and a reader given neither distinction will
conclude one of the slides is wrong. **Three different things, three different words:**

| Word | What it connects | Can learning create or destroy it? |
|---|---|---|
| **edge** (or **core edge**) | one of the brain's own cells to another | **No.** Hand-drawn, fixed. Learning moves the weight on it, never its existence. |
| **attachment** | a cell to a *skill* from a pre-screened library | **Yes.** Grown at runtime, strengthened, pruned when it stops earning its place. |
| **recruitment** | brings a dormant reserve cell online and specializes it | **Yes**, but the cell already existed in code. Activation of latent capacity, not invention. |

**The sentence that reconciles all three, and it should appear verbatim on both the wiring-graph
slide and the structural-plasticity slides:** *the parts and the possible connections are
declared in code; learning decides which of them are live and how strong.*

Never write "grows no new connections" unqualified — say "adds no edge between two of its own
cells." Never write "it rewires itself" — say "it grows new attachments and recruits new units
on top of a fixed core map."

### "drive"

- **chemical drive** — what pushes a channel up.
- **rumination drive** — the idle mind's two drivers (immediate and background). §5.5.

### "lane"

- **owner lane** — the persona's own inner life.
- **agent lane** — work done under a mandate for a customer. The distinction is load-bearing for metering, voice, and privacy; define it before using it.

---

## Terms to stop using on slides

| Instead of | Say |
|---|---|
| "storage is free" | "storage is the cheap part" — it is not free, and there is no retention bound |
| "it rewires itself" | "bounded structural growth over a fixed core, from a vetted parts library" |
| "Active Inference" | "predictive gating, inspired by predictive processing" — the label is overclaimed and the code now says so |
| "replay" (for sleep) | "batch post-processing during quiet" — nothing is replayed through the network |
| "Dark" | retired; use `Deferred`, or `Live` with the caveat strip |
| "Gated" (as a slide label) | `Live · kill switch` — gated means running, and the flag stops it |
| bare "the gate" | the qualified form from the table above |
| bare "valence" | "pleasantness" |
