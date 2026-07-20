# Deck brief: "The Systems of Elyceum"

Instructions for restructuring the deck in Claude Design (`Elyceum Systems.dc.html`).

**Audience.** A potential co-founder: an engineer with his own platform that may merge with this one. He needs depth, and he needs the honest gaps — the status chips and the Appendix A caveats are what make the rest credible to a skeptical reader. An investor version will be derived from this later, so do not optimize for investors now.

**Target length.** ~85 slides.

**Companion file.** `docs/DECK_GLOSSARY.md` is the definition of record for every recurring term, plus the disambiguation table for the words that carry more than one meaning. Read it before writing any detail slide.

**The core problem to fix.** The deck currently opens straight into brain internals. A reader learns how the hippocampus indexes episodes before learning what the product is or who uses it. It also inherits `SYSTEMS.md`'s subsection boundaries wholesale, including boundaries that only exist because a doc needed a heading — 58 of 95 subsections are under 120 words, and one is 16 words. Those become slides with one sentence on them.

---

## Step 0 — Do this before editing anything

Produce an inventory of the current deck: for every `<section>`, its index, `data-label`, which status chip it carries (Live / Gated / Dark / none), and its body word count. Report the total slide count, and which `SYSTEMS.md` subsections (§1.1 through §9.12) currently have a slide and which do not.

Report that inventory back before making any changes. The instructions below assume the deck covers systems 1 through 3 in detail and stops; if it goes further, treat the extra slides as rewrites rather than new authoring.

---

## How every detail slide must be structured

This is the thing the deck is least consistent about today, and the highest-leverage change in this brief.

**The governing principle: every slide must survive being the first slide someone sees.**

The deck was written by someone who already knew the system. Terms arrive mid-sentence with no introduction — §2.9 says a skill is "wired onto one of the drafting cells" and "rides the drafts" before the reader has been told what a cell is, what a drafter is, or what a draft is. `turn` appears 76 times in SYSTEMS.md Part I and is defined nowhere. That works in a reference read front to back. It fails in a deck, where a reader flips to slide 54 first, or reads only the section that concerns him.

So a slide is not finished when it is accurate. It is finished when someone who has read no other slide can say what the thing is, why it exists, and what it touches. Zones 3 and 6 below exist entirely for this, and `docs/DECK_GLOSSARY.md` is the definition of record for every term.

**Eight zones, every detail slide, no exceptions:**

1. **Eyeline** — `System N.N · <System name>`, plus one status chip. **Use this vocabulary, not SYSTEMS.md's raw tags:**

   - **`Live`** — shipped, running by default.
   - **`Live · kill switch`** — what SYSTEMS.md calls *Gated*. It is **running in production right now**; the flag exists to stop it, not to enable it. Never render this as "Gated" or "behind a flag" on a slide. A reader sees "gated" and concludes "not on," which is the opposite of the truth and makes shipped work read as vapor. Where useful, name the flag: `Live · kill switch (self_reflection)`.
   - **`Deferred`** — built on one side, deliberately not wired on the other, because nothing has asked for it yet. Two items only: song recognition (matcher works, fingerprint DB is empty) and the video path inside an otherwise-live §8.10 (analyzer works, nothing publishes frames). Deferred is a scheduling decision, and the slide should read that way.

   **The `Dark` tag is retired.** Do not use it anywhere. Nothing in the deck is abandoned or unreachable-by-accident, and the old tag implied both.

   **§3.7 Reflexes is `Live`.** The mining pass runs every consolidation, unflagged. It has never produced a reflex because the job corpus is dominated by single-step and failed work — an unmet evidence bar, not a broken feature. **It must carry its Appendix A caveat strip.** This is the one place where the status chip and the outcome genuinely differ, and the tag is only defensible because the caveat is present. A reader can check this in seconds by looking for a `chunks.json`, so a bare `Live` with no caveat is a credibility hole, not a rounding error.
2. **Headline** — the "Say it like this" line from the corresponding `SYSTEMS.md` section, verbatim where it works.
3. **Standfirst — two lines, and this is the new zone that fixes the comprehension problem.** Set slightly larger than the body.
   - **Line 1, what it is.** A plain-language definition of the subject, using no term this deck has not already defined. Not "path plasticity attaches vetted skills to drafting cells," which explains nothing to a newcomer. Rather: "A drafting cell is one of five components that each write a candidate reply. This is how a proven new ability gets permanently attached to one."
   - **Line 2, why it exists.** What breaks, or what the system cannot do, without this part. If that sentence cannot be written, the subsystem probably does not deserve its own slide and should be merged.
4. **Body** — **one paragraph, 70 words maximum** (reduced from 90 to make room for the standfirst). The mechanism only; the standfirst has already handled what and why. Slides currently run to 1,800 characters. §1.1 is a document, not a slide.
5. **Diagram** — carries the mechanism, and must still make sense with the body deleted. A slide with no diagram is a bullet list wearing a costume.
6. **Locator strip — the second new zone.** A single line across the bottom, in a fixed format, answering "where does this sit":

   `reads from: <upstream> · this: <the subsystem> · feeds: <downstream>`

   For example, on §3.4 Casting the net: `reads from: chemistry (sets depth) · workspace focus (sets aim) — this: recall — feeds: the drafting context`. Every subsystem has an upstream and a downstream; if one genuinely has neither, say so, because that is itself informative. Where the deck has a master map, highlight this subsystem's position on a small silhouette of it rather than repeating the whole map. This is the zone that answers "how does it connect to everything else," and it does it visually so it costs no words.
7. **Grounding chip** — theory plus citation, **or** an explicit honest label (`Engineering invariant`, `Ours — no prior art`). §2.5 already does this correctly; copy its pattern. Citations for every named theory are in `docs/THEORY_CITATIONS.md`, keyed by SYSTEMS.md section; do not invent one, and if a theory is not in that file use the honest label instead.
8. **Caveat strip** — governed by a mechanical rule, not taste. A slide carries a caveat strip **if and only if** that subsystem has a row in `SYSTEMS.md` Appendix A ("What Is Not True Yet"), and the strip is that row, compressed. No Appendix A row means no strip. This is what makes an absent caveat informative: it means the subsystem is not on the gaps list, which is a checkable claim rather than an authorial mood.

**Hard rules:**

- **Caveats follow Appendix A, mechanically.** Today there are 7 caveat boxes across 38 slides and they appear where someone felt like writing one, which teaches a reader that a missing caveat means nothing. Binding the strip to Appendix A rows fixes that without adding any new visual element.
- **Write from one user's point of view. Isolation is the footnote, never the frame.** Several slides describe a capability entirely through its multi-tenant guarantee, which buries the thing the reader actually cares about. §1.8 is the clearest case: the deck slide is titled "A relationship per customer," its diagram is labelled "Isolated relationship threads," and the takeaway lands as *we prevent cross-contamination* — a compliance fact. The actual fact is **your agent's mood is specific to you, it persists between sessions, and it picks up where you left it.** That is the feature. The isolation is the engineering that protects it.

  So: **lead with what one person experiences, then note the guarantee in a closing line or a small strip.** Rewrite §1.8's slide title to something like "The mood it is in with you," make the diagram one relationship over time (warm, cooling with absence, resuming on return) rather than three parallel lanes with a barrier between them, and put the no-cross-contamination guarantee in the caveat-strip position underneath. SYSTEMS.md §1.8 has already been rewritten this way — follow its structure.

  Apply the same test to **§9.2** ("a brain per customer" — the user-facing fact is that *your* agent keeps thinking while you are away, which the section already calls "the product, not a leak," but buries at the end), **§9.11** (the privacy gate — the user fact is that what you tell it never surfaces in someone else's conversation), **§3.8** (sleep consolidates *your* day; cross-customer learning behind the gate is secondary), and **§9.10**. **§7.7 is the model to copy** — it opens on affection and bond and lands on "that is a real model of friendship, and it is four functions," never once framing itself around tenancy.

  The exception is Part 1's app slides and System 9's genuinely platform-level sections (§9.1, §9.3, §9.6–9.8), where the reader *is* the operator and the tenancy framing is the subject rather than a distraction.
- **First use defines; after that, use freely.** The first slide in the deck that uses a term defines it in the standfirst. `docs/DECK_GLOSSARY.md` holds the definition of record for every recurring term — use its wording so the same concept is not explained two different ways on two slides.
- **Never use an overloaded term bare.** `gate` carries at least seven unrelated meanings across SYSTEMS.md (predictor gate, safety gate, privacy gate, learning gate, spend gate, org gate, and the verb "gated by"). Always use the qualified form. Same for `channel` (chemical channel vs bus topic vs external verdict channel), `weight` (edge weight vs reward weight), `drive`, and `lane`. **`valence` is banned outright** — SYSTEMS.md itself warns that three different things carry that name; say "pleasantness." The full disambiguation table is in the glossary.
- **Spell out every abbreviation on first use.** DA, ACh, GABA, NE, Glu, 5HT, CORT, OXT, AEA, PAD, DMN, GWT, CLS. The nine-channel slide currently shows bare symbols with one-word glosses, which reads as fluent to someone who already knows and as noise to everyone else. Give each its full name once.
- **No slide may reference another slide by number.** "As discussed in §2.4" is useless to someone who opened here. Restate the one fact needed, in four words, and let the locator strip carry the relationship.
- **State bets as bets.** Where a claim is about what the system will achieve over time rather than what it demonstrably does, write it as intent, not as accomplished fact. §3.5 currently says cross-domain transfer *is* "how one agent applies what it learned debugging a database to a conversation about someone's marriage." The mechanism is real; that outcome is a hypothesis. Write "the intent is" or "this is the bet," and the slide gets more credible rather than less.
- **Do not let the status chips imply a three-way split.** Every subsystem in the deck is running; two have a deliberately unwired input. `Live` and `Live · kill switch` should read as neighbors visually, and `Deferred` should read as a roadmap note, not a failure state. No red.
- **Never reuse a sentence from an overview slide.** Right now slide 8 and slide 12 share "No branch says 'if trust is high, be nicer' — it falls out of the arithmetic" word for word, and slides 7 and 21 share "why it can afford to feel." The overview should tease; the detail slide should prove. Repetition makes the second instance read as filler and drains the first.
- **At most one em dash per slide.** The deck currently runs 123 across 38 slides, several sentences carrying two.
- **Do not change the design system.** Tokens, colors, type and the `_ds/` bundle stay as they are. This is a content and structure pass.

---

## Part 0 · Framing — 5 slides

| # | Slide | Instruction |
|---|---|---|
| 1 | Title | Keep. **Do not say "95 systems"** — an audit of all 95 numbered entries found roughly 62 systems, 16 guards, 12 properties and 5 measurement surfaces. A reader who scans the list will catch the inflation, and this deck cannot afford that. Say **"62 systems, and the fences and instruments around them"** — a real safety layer counted honestly is a better claim than a bigger number. Correct the stat strip to **9 systems · 62 subsystems · 65 named theories · 11 brain clusters**. The current numbers are wrong in the other direction (see Corrected facts). |
| 2 | **What Elyceum is, and why it is different** | **New.** Lead with the product positioning: "AI agents that learn and feel." / "It's not AGI. It's AEI: Artificial Emotional Intelligence." Then the differentiator: *"Most AI tries to copy human intelligence. We copied the biology that produced it."* Support with a 2×2, one line each: **instinct at the core** (it reacts before it reasons); **learns your domain** (a specialist, not a generalist); **cheaper with use** (cost per interaction trends down the more it runs); **no orchestrator, no central plan** (coordination emerges from shared chemistry). |
| 3 | **Two ways to run it** | **New.** Left: embed the engine in your product over the API, which uniquely returns emotional state alongside every response. Right: run it from the Elyceum app. This is the integration surface, and it is the slide this particular reader cares most about. |
| 4 | **Persona, mandate, agent** | **New, and load-bearing — nothing later parses without it.** A **persona** is a personality you can talk to; personas are cheap, make as many as you like. A **mandate** is a swappable job. A persona put to work under a mandate, with bounded permissions, is an **agent**. One diagram, three boxes, no prose beyond a caption. |
| 5 | **The five workspaces** | **New.** Agents · Personas · MRI · Learning · API, one line each. Note honestly that these are not URL routes: the app is a single page and navigation toggles sections. MRI is the user-facing name for what the code still calls `labs`. |

## Part 1 · The application — 12 slides

Screenshots carry these. Until they are uploaded, lay each slide out with a correctly proportioned placeholder frame; do not redesign around their absence.

| # | Slide | Screenshot | Instruction |
|---|---|---|---|
| 6 | **App architecture** | none, diagram | **New.** Browser UI (vanilla HTML/JS, no framework) → FastAPI web server plus `/ws` → brain process → **engine API on a separate port** → tenant volume and Supabase. Call out the port separation explicitly: the engine API is deliberately a separate app so it can never inherit the web app's cookie auth. That is a security decision, not an accident, and this reader will notice it. |
| 7 | **Personas — overview** | `personas-grid` | Cards rolled up per persona: cost, tokens, model calls, agent count, status. |
| 8 | **Personas — configuring one** | `persona-temperament`, `persona-chemistry` | Temperament dials (empathy, sensitivity, composure, drive, creativity, humor, sociability, caution, lingering), cognitive-style dials, per-channel resting chemistry. Mention the 13 built-in presets and name a few: Visionary, Empath, Analyst, Stoic, The Admin. |
| 9 | **Personas — memory you can read** | `persona-selfmd` | `self.md` and `user.md` as editable markdown. The punchline: you can open the file and read what your agent thinks it knows about you. Worth more than any dashboard. |
| 10 | **Agents — the fleet** | `agents-grid` | Status dots, per-agent cost and tokens and calls, date-range selector, live GPU pod uptime with accrued cost, all-orgs fleet scope for platform superadmins. |
| 11 | **Agents — jobs and supervision** | `jobs-table`, `job-detail` | Job states (`running · awaiting_approval · completed · failed · stopped_budget · deferred`), the per-step timeline with tool and reason and args and output, spend per job. |
| 12 | **Agents — skills from three sources** | `skills-screen` | Added by you · registered by your apps over the engine API · **self-authored by the brain**. Everything passes the screener; anything not auto-cleared waits in a review queue with the judge verdict and static-analysis findings. Give this one room, it is a genuine differentiator. |
| 13 | **Agents — the permission ceiling** | `account-limits`, `connectors` | The org sets a ceiling; every agent is narrowed within it and can never be widened past it. Connectors reached through the cloud connector, with mutating tools approval-gated. |
| 14 | **MRI — the live interior** | `mri-full` | Nine-channel bar stack, mood dot, idle-thoughts feed, and the read-bar showing how it reads *you* — inferred emotion, energy, pace. |
| 15 | **MRI — watching it think** | `mri-plasticity`, `mri-approval` | Plasticity pane (predictor accuracy, calls saved, live wiring edges, rolling decisions log), the brain atlas in both views, observing a specific agent's lane, and approve-in-chat appearing in both the rail and the conversation. |
| 16 | **Learning — the claims and the receipts** | `learning-stories`, `learning-dashboard` | The stories feed, and the mechanism that makes it trustworthy: a claim may only cite evidence **by index**, indices are stitched back structurally, and any claim without a valid citation is dropped. Quote it: *hallucinated citations are structurally impossible, not merely unlikely.* Headline stat: N% of reward self-graded. |
| 17 | **API — the engine surface** | `api-reference`, `partner-keys` | The three-pane reference is **generated from the live route table and docstrings**, so it cannot drift; five tests fail if it does. Partner keys are hash-only and shown once. |

## Part 2 · How it fits together — 1 slide

| # | Slide | Instruction |
|---|---|---|
| 18 | **The operator loop** | **New, diagram-led.** A cycle: create a persona → give it a mandate and bounded permissions → it becomes an agent → it runs jobs → you supervise in Jobs and step in through MRI → what it learns lands in the Learning ledger → which changes the persona → repeat. Overlay where the engine API enters for embedders who never touch the app. |

## Part 3 · The brain at overview level — 6 slides

| # | Slide | Instruction |
|---|---|---|
| 19 | **The whole thing in a breath** | Existing slide 2. Keep, tighten. The three columns work. **Rewrite its speaker notes** — they currently read "Live = shipped by default. Gated = built, behind a flag. Dark = built but never fires," which is the wrong framing on all three counts. Replace with: everything in this deck is running in production; a kill-switch flag is a stop button on live code, not a feature parked in the dark; two inputs are deliberately deferred and are labelled as such; and anything that does not yet fully work carries its own caveat, drawn mechanically from the gaps list rather than written where it felt appropriate. |
| 20 | **The master map** | Existing slide 4. Keep. **Delete existing slide 3** — it is the same map with 50 characters of text, a duplicate title, and a pager stamped `09 / 10` while sitting in position 3. In the Memory box, change "Storage is free — retrieval is the intelligence" to "Storage is the cheap part; retrieval is the intelligence." |
| 21 | **Cheap by default, expensive on purpose** | Existing slide 5. Keep, but strip the prose that repeats §2.1, §2.2 and §2.4 verbatim. Let it be near-wordless. |
| 22 | **The turn lifecycle** | Existing slide 6. Keep as is. The two-phase chemistry split is the strongest single idea in the deck. |
| 23 | **Eleven clusters** | Existing slide 7. Keep. Remove the sentence that reappears word-for-word on §2.3. |
| 24 | **Nine channels** | Existing slide 8. Keep the diagram, move all prose to §1.1. The two slides currently share whole sentences. |

Existing slide 9 (Node registry) is too granular for an overview. Fold it into §2.7.

## Part 4 · Proven, promising, hypothesized — 3 slides

Per the brief, this closes the overview and opens the detail.

| # | Slide | Instruction |
|---|---|---|
| 25 | **How sure we are of each part** | Existing slide 10, split. Just the framing: some things are true because the code enforces them, some have early signal and want time and data, some are science-based hypotheses. Currently 2,089 characters, the densest slide in the deck, and it lands before the reader has met a single system. This is a one-time framing device — it is **not** repeated as a per-slide badge. |
| 26 | **What is proven, promising, hypothesized** | The three-column table from existing slide 10. |
| 27 | **The measured result** | **New, and the most valuable addition here.** The controlled wiring-divergence experiment is the only quantitative evidence in the project and it is currently nowhere in the deck. Warm versus analytical training regimes, memory wiped and chemistry pinned: firing-path Jaccard divergence **0.117 (95% CI 0.084–0.152)** against a frozen-wiring control of **0.066**, permutation **p = 0.002**; recall fan-out divergence **0.110 vs 0.012**. Give the caveats equal visual weight: learning rate amplified ×5, synthetic probes, ten sessions, no quality claim. Include the honest negative — switch evaluation order showed no differentiation at all. For an engineer deciding whether to merge platforms, this slide outweighs any ten mechanism slides. |

## Part 5 · The systems in detail — ~54 slides

### Open with two vocabulary slides — new, and they pay for themselves

Everything after this point is dense, and the reader has just come out of an app tour with an entirely different vocabulary. Do not drop them straight into §1.1.

| # | Slide | Instruction |
|---|---|---|
| 28 | **The parts, named** | **New.** A single annotated diagram defining the structural vocabulary in one pass: **turn**, **cell** (and its two kinds, **switch** and **integrator**), **cluster**, **wiring**, **edge**, **weight**, **draft**, **drafter**, **critic**, **commit**. Show them in relation rather than as a list — a turn flowing through a cluster of cells, with one edge labelled. Definitions in `docs/DECK_GLOSSARY.md`. This is the single highest-leverage new slide in the deck: ten terms, defined once, that currently appear undefined across roughly forty slides. |
| 29 | **Words that mean more than one thing** | **New, and short.** The disambiguation table from the glossary, trimmed to the four that will actually mislead: **gate** (seven referents — always qualify it), **channel** (chemical vs bus topic), **weight** (edge weight vs reward weight), **lane** (owner vs agent). Frame it as a courtesy to the reader, not an apology. An engineer will recognize this as the mark of someone who has actually maintained the thing. |

### The systems


**Order:** Chemistry, Cognitive Core, Memory, Learning, Identity, Agency, Idle Mind, Perception, Platform.

Chemistry first because it is the vocabulary every later system uses. Then the Core, Memory and Learning, which are the intellectual spine. Identity and Agency next, because they map directly onto the Personas and Agents workspaces the reader just saw in Part 1. Idle Mind, Perception and Platform last.

**Fold each system's divider into the first slide of that system** as a section band across the top. Nine standalone divider cards is nine slides carrying no information.

| System | Parts in SYSTEMS.md | Slides | Merges and splits |
|---|---|---|---|
| **A · Chemistry and Affect** | 8 | 6 | `1.2 + 1.3` → "What moves them, and what pulls them back" — rise and relax belong on one slide. `1.6 + 1.7` → "The vocabulary and its colors" — 1.7 is a rendering of 1.6's table. |
| **B · The Cognitive Core** | 10 | 12 | **Split §2.1 into three slides — see "The §2.1 exception" below. It is the foundation for a dozen later slides and currently gets one dense paragraph.** Otherwise no merges. Structural plasticity has moved out to Learning (see above). **Add §2.9 "Deciding across turns"** — the EvidenceGate / drift-diffusion work, live, no slide exists. **Add §2.10 "Deliberating before acting"** — the newest system in the doc: before any tool runs, three candidate approaches (each an information posture × a reasoning method, drawn by relevance, learning, and chemistry) compete and the winner owns the decision to act. Carry its Appendix A caveats on the slide: thresholds reasoned not measured, pairing ledger does not survive a restart. This is also the slide that makes the two-level Multiple-Drafts story honest — deliberation on WHAT to do, then on what to say. |
| **C · Memory** | 10 | 6 | `3.1 + 3.2 + 3.3` → "What it stores: episodes, notes, vectors" — all three are one-paragraph store descriptions and two share a citation pair. `3.6 + 3.7` → "Automatization: recipes and reflexes" — 3.7 literally opens "Below whole recipes sits a finer tier." `3.8 + 3.10` → "Sleep, and what it concludes." **Also: the "What it stores" slide must carry the retention caveat** (Appendix A, Memory retention). There is no retention window, TTL, or eviction; growth is unbounded and the only deletion path is erasure on request. This reader will ask, and the answer being "it's an open product decision, and here is why it is not trivial" is much stronger than being caught without one. |
| **D · Learning** | 12 | 10 | **Run §4.3 + §4.4 + §4.8 as one consecutive block titled "Can the reward signal be trusted?"** — they are the same story split by unrelated material. §4.3 admits ~80% of reward is self-administered and instruments it; §4.4 opens the external channel that can shift that number; §4.8 is the guards that stop self-reward being farmed in the meantime. Alone, §4.8 reads as a stray safety check, which is why it does not currently land. Together they are the most credibility-buying run in the deck for a skeptical engineer: a system that measures its own worst property, publishes the number, and says it is not solved. Do not let §4.5/§4.6 sit in the middle of them. **Split §4.7 into two slides** — see below. | **Open on the three scales** — weights, attachments, whole units — then walk them smallest to largest. Mention the fourth credited surface in §4.7: stances from the approach competition earn credit from VERIFIED outcomes (grounded, not critic-graded) — one line, it pays off the §2.10 slide. `4.1 + 4.2` → "The update and the reward signal" — 4.2 is 31 words and cannot hold a slide. `4.5 + 4.6` → "Intensity and timing" — both are modifiers on the same weight update. Do **not** merge 4.8 and 4.10; they rhyme thematically but the mechanisms are unrelated. **Split §4.12** (2,091 words, 14% of all Part I prose) into two slides: reserve-cell recruitment, then self-authored specializations. §4.11 stays whole. |
| **E · Identity** | 10 | 7 | `7.1` folds into the section band (52 words duplicating the system intro). `7.4 + 7.5` → "The dials" — splitting them is exactly what creates the seven-versus-eight confusion the doc then has to correct in prose. |
| **F · Agency and Action** | 12 | 7 | The biggest win: 11 of 12 subsections are thin, 963 words across 12 headings. `6.2 + 6.3 + 6.6` → "The permission ceiling" (one idea stated three ways). `6.4 + 6.5` → "Jobs and the queue." `6.7 + 6.9` → "Money and the hard no." `6.10 + 6.11` → "External surface: connectors and skills." Keep 6.1, 6.8 and 6.12 standalone. |
| **G · The Idle Mind** | 9 | 6 | `5.1 + 5.3` → "The loop and its pacing." `5.6 + 5.7` → "Deciding to speak, and how it gets rewritten" — sequential, same pipeline. `5.9` is 24 words; append it to 5.5 Rumination. |
| **H · Perception and Expression** | 12 | 6 | `8.1 + 8.2 + 8.5` → "Hearing: transcription, prosody, pace" — 8.5 is 16 words, the shortest section in the document. `8.3 + 8.7 + 8.8` → "Reading you against your own baseline." `8.4 + 8.6` → "Laughter and speaker identity." Keep 8.9 and 8.10; merge `8.11 + 8.12`, which is the strongest material in this system. |
| **I · Platform and Safety** | 12 | 7 | `9.1 + 9.2` → "The tenant and its brain" — 9.1 is 28 words. `9.6 + 9.7 + 9.8` → "Credentials, tiers, and the vault" — three sub-100-word least-privilege vignettes with identical shape read better as one slide with three beats. `9.5` folds into 9.12. Keep 9.3, 9.4, 9.9, 9.10, 9.11. |

### The wiring graph appears in two systems — handle it with one asset, not two slides

The wiring graph is described under the Cognitive Core (§2.7) and is also the main subject of
System 4 Learning. Left alone this produces two slides that say overlapping things: §2.7
currently states that weights are nudged each session and persist per personality, and §4.1
states that every route that fired is nudged by how the turn went, grouped by personality.
Same fact, twice, and both cite Hebbian plasticity as their grounding theory.

**The ownership rule: the Cognitive Core owns the graph as a noun. Learning owns it as a verb.**

- **§2.7 (Cognitive Core)** — what the graph *is*. Roughly sixty declared connections between
  named parts, each carrying a weight, each personality holding its own copy of the whole graph
  (which is what makes two agents with identical code behave differently). The fixed-topology
  precision point: learning moves weights, not existence, so say "a fixed map with learned
  weights" and never "it rewires itself." The node registry and boot audit. **It states that the
  weights are learned and stops there.** No update rule, no reward, no credit assignment.
- **System 4 (Learning)** — everything that *changes* it. The three-factor rule, homeostatic
  decay running first, where the reward came from, credit grouped by personality, delayed credit.

**Resolve the "does it grow new connections" contradiction, which is live in the deck today.**
The wiring-graph slide says learning "grows no new cell-to-cell connection and prunes no old
one." The structural-plasticity slides say the brain "can grow new connections." A reader who
sees both concludes one slide is wrong, and the wiring-graph slide additionally implies that
growing structure would be a *bad* thing — which is the opposite of the story two slides later.

Both are true under a distinction the deck never gives. Use three distinct words and never blur
them: an **edge** connects two of the brain's own cells and learning never adds or removes one;
an **attachment** connects a cell to a pre-screened skill and is grown and pruned constantly; a
**recruitment** brings a dormant reserve cell online, activating capacity that already existed
in code. Put this reconciling sentence verbatim on both the wiring-graph slide and the
structural-plasticity slides: **"the parts and the possible connections are declared in code;
learning decides which of them are live and how strong."**

Also drop the defensive framing on the wiring-graph slide. The fixed core map is a **scope
claim, not a safety limitation** — it is not fixed because growth would be dangerous, it is
fixed because growth happens in a different layer. Present it as "here is what changes and here
is what does not," not as "we deliberately do not do the risky thing." The full vocabulary is in
`docs/DECK_GLOSSARY.md` under "connection."

**Only one of the two carries the Hebbian citation.** Both sections currently name Hebbian
plasticity as their grounding theory, so as slides they would show the same chip twice and the
reader would reasonably wonder which one is the real claim. **Learning owns it** — the citation
belongs with the update rule. The §2.7 slide's grounding chip should be the engineering point
instead: `Fixed topology, learned weights — an engineering invariant`. That is more honest
anyway, since a graph with weights on it is not itself a scientific claim; the rule that moves
them is.

**Use one diagram across both.** Build the wiring-graph visual once. §2.7 introduces it static —
the named parts, the edges, the weights at rest. System 4's opening slide re-uses **the identical
image** with a learning overlay: which edges moved this session, in which direction, and why.
That is a callback, not a repetition, and it is better than two different pictures of the same
graph, because the reader already recognizes the shape and sees immediately what changed.

**Structural plasticity has moved into Learning.** What were §2.9 (path plasticity) and §2.10
(growing new units) are now **§4.11 and §4.12**, at the end of System 4. Old §2.11 (deciding
across turns) became §2.9. Cognitive Core is now 9 parts, Learning is 12.

The reason is not tidiness. **All three kinds of change run on one licence:** weights on the
fixed map, attachments onto cells, and whole new units are gated by the same third factor
(co-activation changes nothing without a reward signal), run in the same consolidation pass, and
stop dead under the same wiring freeze — which halts the entire learning pass rather than any
one tier. Described apart, that unity is invisible, and the system reads as a weight-learning
system with two bolt-on growth features. Described together, it reads as one mechanism at three
grain sizes, with heavier fences on the larger two because growing structure deserves more
caution than nudging a number.

**So System 4 opens on the three scales**, then walks them smallest to largest: weights
(§4.1–§4.10), attachments (§4.11), whole units (§4.12). Say explicitly that the fences on the
larger two exist because of the stakes, not because they run on different machinery.

Do not split §4.11 or §4.12 across the two systems. §4.11 has a during-turn component —
attachment candidates ride the drafts so the critic scores a bad experiment before anything is
spoken — but that is how an attachment proves itself, which is part of the learning loop, not
part of ordinary turn processing. It stays whole, in Learning.

### Add a "Six rules that hold everywhere" slide — and stop restating them

An audit of all 95 subsections found **six cross-cutting invariants restated roughly 23 times
across different systems.** Each restatement reads as a fresh discovery, so the architecture
looks accreted rather than designed, and the reader has to notice the pattern unaided.

**Put them on one slide, immediately after the master map, and have every later slide reference
rather than re-derive.** Six lines, no diagram needed:

| Rule | Currently restated in |
|---|---|
| **Chemistry biases, never dictates.** No mood can drive a gate fully open or shut, make a stance unreachable, lower a safety floor, or widen a budget. | §2.1, §2.10, §6.1, §6.7 — four systems, same sentence with different nouns |
| **Placement is a privacy control.** Work assigned to our hardware may never fall back to the cloud. Degradation can only ever move toward local. | §2.5, §9.9, §3.3, §5.7, §4.12 — five places |
| **Clamp on read, not on write.** Bounds are applied when a value is used, so a stale or tampered stored value can never grant anything. | §4.7, §6.3, §4.12 |
| **Fail closed.** If a check cannot run, the answer is no. | §6.9, §6.11, §9.1, §9.6, §9.11, §4.12 — six places |
| **Parts are declared in code; learning decides which are live.** | §2.7, §4.11, §4.12 |
| **Display never writes back to felt state.** Rendering and markup are cosmetic and touch no chemical channel. | §1.7, §8.12 |

This is a strong slide on its own merits for this audience — six invariants, each enforced in
code, is the shape of something designed. And it buys back roughly twenty slide-paragraphs.

### Consolidate guards into the systems they guard

The same audit classified every subsection. Roughly **62 are systems** — remove one and a
capability disappears. The rest are **16 guards**, **12 properties**, and **5 measurement
surfaces**. Several sections say so in their own text: §4.8 calls itself "the guards," §7.10 says
"no code reads it," §1.7 says it "changes only the rendering, never what the agent feels."

**A guard does not get a peer slide.** It becomes a labelled closing strip on the system it
protects. Apply this everywhere; these are the worst offenders:

| System | Problem | Consolidation |
|---|---|---|
| **§9 Platform and Safety** | Worst ratio in the doc — 5 systems to 5 guards, four of them under 140 words | §9.3 + §9.7 fold into §9.2 (the tenant process); §9.6 folds into §9.5 (the API surface); §9.9 merges into §2.5 (it restates the same routing asymmetry); §9.11 becomes a labelled sub-part of §3.8 (it is an admission gate on cross-customer learning, not a peer of it); §9.1 becomes the section preamble; §9.12 splits three ways with money returning to §6.7. Twelve entries become about five. |
| **§6 Agency and Action** | §6.2, §6.3, §6.6, §6.7 and §6.9 are five headings describing **one thing** — the autonomy permission model — in 384 words total | One section of 384 words, far stronger than five of 77. This supersedes the earlier merge note for System 6. |
| **§7 Identity** | Worst dilution — only 4 real systems out of 10 | §7.1 becomes the preamble; §7.2 restates §1.3 and should reference it; §7.4 + §7.5 are two halves of one dial set split on a cosmetic criterion, so merge; §7.3 is a catalog; §7.10 is a document no code reads. The genuine mechanisms are §7.6, §7.7, §7.8, §7.9 — and **§7.7 (affection and bond, "four functions") is the strongest of them and currently gets less space than the personality catalog.** Give it the room. |
| **§1** | §1.6 is the label space of §1.4's discrete readout; §1.7 is a renderer | Both become sub-parts of §1.4, as already planned. |
| **§4** | §4.3 and §4.10 are instrumentation, not architecture | Keep §4.3 in the reward-integrity block (it is the *measurement* that makes the honesty claim land) but present it as the instrument panel it is. §4.10 becomes a closing strip on §4.1. |

### Promote what is buried — the reverse problem

Two genuine systems are currently sub-bullets and should be slides:

- **Interoceptive load** (one paragraph inside §2.1). This is **the only cognition-to-chemistry
  feedback path in the entire document** — the loop that closes System 2 back into System 1. The
  total amount chemistry has *raised* thresholds is summed and read as felt inhibitory load.
  Everything else in the deck flows chemistry → behavior; this is the return leg, and it is
  currently a bullet whose placement the source text apologises for. **Give it a slide** and put
  it on the master map as an arrow going back.
- **The lobe bridge** (three sentences inside §6.12). Other brain regions register themselves as
  tools the planner calls by name; motor holds no direct reference to any of them. "Cognition as
  an affordance of action" is an inverted dependency structure and one of the more consequential
  design decisions in the system. It currently shares a 129-word heading with an unrelated
  mechanism.

### Split §4.7 — it is currently three unrelated things under a residual title

"Other things that earn credit" is a leftovers label, and readers bounce off it because the
section never says *why* these belong together. The reason: the main pass (§4.1) nudges weights
on the wiring graph, but several things carry a learnable strength that is **not** a wiring edge
— which drafter gets invited, which strategic stance is trusted, how eager a routing shortcut is,
how the recall budget is split. The wiring pass structurally cannot reach any of them, so each
needs its own rule. **State that first, or the section reads as a grab-bag.**

Then split it, because it is not one kind of thing:

| Slide | Content |
|---|---|
| **"Two kinds of competition"** | Drafters and stances, presented as a deliberate contrast — the source section itself calls one "the deliberate inversion" of the other. **Phrasing credit is contrastive and self-graded:** the winning draft gains in proportion to how far it beat the others, losers lose at half rate, and the judge is the critic's taste. **Strategy credit is outcome-grounded:** a turn later the committed approach is checked against what actually happened, and stances gain or lose durable weight accordingly — including losing it when an executed approach was refuted, because refutation is real evidence. Losing candidates barely move, since they never ran and nothing grounded exists against them. Fold the fourth item in as a closing line (search strategies earn credit by useful-hit count, split along the fast-store/slow-store line), keeping the code's own candour that hit count is a volume proxy for usefulness rather than a measure of it. |
| **"Learning cannot open a safety gate"** | **Its own slide, and a strong one.** This is not a credit rule at all — it is a safety invariant expressed as a constraint on learning, and it is currently buried as the third bullet of a list. Routing switches learn inside a band with a *direction*: a shortcut may only ever learn to be less eager, a self-reflection trigger only ever more eager. The clamp is applied when the value is read rather than when it is written, so even a corrupted stored value cannot escape it. Headline it with the claim it earns: **learning is allowed to make the system more cautious and is structurally incapable of making it less so.** For this audience that is worth more than any three mechanism slides. |

### The §2.1 exception — three slides, and the word budget does not apply

Switches and integrators are how the entire system works. A dozen later slides assume it —
every mention of a threshold, a gate, chemistry changing behavior, learning moving a weight,
or a model call being expensive resolves back here. The current slide skips from "there are two
kinds of cell" straight to inhibitory ratios, which is the middle of the explanation. A reader
who does not fully get this slide does not get the deck.

So: **three slides, and the 70-word body cap is lifted for these three.** Build up in order,
never assuming a step not yet given.

| Slide | What it must establish |
|---|---|
| **2.1a · What a switch is** | There are exactly two kinds of thinking unit. A **switch** is plain deterministic code, no model, and handles nearly every decision the system makes — should this memory be recalled, is this a topic shift, should this tool run. An **integrator** is model-powered and expensive and handles the few that need judgment; name it here, then set it aside until 2.1c. Then define a switch completely, because that is this slide's job: it is a yes-or-no decision point, an input arrives with some strength, it is compared against a threshold, it fires if it clears the bar. **Polarity belongs here, not later** — an excitatory switch adds to downstream activation, an inhibitory one subtracts, and roughly a quarter are inhibitory (by convention, not enforcement) because a system that can only excite itself never settles, it spirals. A bar for firing and a direction of effect are together what a switch *is*. Diagram: a mesh of many small switches, some marked as subtracting, with a few large integrators sitting where paths converge — **not** a chain, **not** a hierarchy. |
| **2.1b · How a switch decides** | Everything on this slide is about how the bar for firing moves. The mechanism, in the order it happens, as a four-step visual: **(1)** the switch has a base threshold; **(2)** chemistry shifts that threshold — each switch declares which chemical channels it listens to and how strongly, and roughly two thirds do (the rest are chemistry-blind and always use the base); **(3)** the result is divided by a learned efficacy, so a route that has worked before fires more readily — chemistry is how it feels now, efficacy is what it has learned over its whole life, and both act on the same number; **(4)** the total is clamped, so no chemical state can drive a gate fully open or fully shut. Then the input is compared and it fires or does not. **The payoff, and it should be the headline: this is where feeling becomes behavior.** A curious agent has a lower bar for casting memory wide. A threatened one has a higher bar for speaking freely. Nothing branches on mood — there is no `if anxious:` anywhere in the codebase; mood moves thresholds and behavior falls out of the arithmetic. Closing strip, three items: the clamp means feeling can bias a decision and never dictate it; the whole modulation system sits behind one gain knob that can be set to zero, which is how it is tested; and the total amount chemistry has *raised* thresholds is summed and fed back as interoceptive load, so being inhibited is itself a felt state. |
| **2.1c · Integrators** | Its own slide. Some questions cannot be answered by comparing a number to a threshold — what did this person actually mean, what should the reply say, is this plan sound. Those go to integrators. **State the mechanism precisely, because the intuitive version is wrong and contradicts the "no orchestrator" claim made elsewhere in the deck: an integrator is not handed a decision by a switch that gave up.** It subscribes to a set of bus topics and wakes when activation across them clears its own firing threshold. It sits at a convergence zone and fires when enough evidence has piled up there. Nothing routes a case upward; there is no supervisor. Each integrator is capped at a small number of model calls per turn, so the expensive path cannot run away. **Do not close on "why the split is the whole architecture" — that payoff moves to §2.2, where both filters are finally in view.** Close instead on the tension §2.2 resolves: an integrator is the most expensive thing in the system, one call costs more than every switch put together, and convergence on its own is not a tight enough filter — on an ordinary turn several zones would clear it, and paying for all of them would sink the design. So something has to decide that a convergence is not worth thinking about at all, and that is where nearly all the saving happens. **End there, on the open question, and do not add "see §2.2." A hook beats a pointer: the next slide answers it in its first line.** |

Do not compress these three back into one. If the deck needs to lose slides, take them from
Perception or Platform.

**§2.1c and §2.2 are a pair — sequence them, do not merge them.** Merging was considered and
rejected: both are full slides (integrators carry the subscribe-and-converge mechanism and the
no-supervisor correction; prediction carries the confidence gate, the emotional override, the
shadow check, and the Active Inference caveat), and one slide holding all of that is the density
problem this brief exists to fix.

The clunkiness comes from the summary sitting one slide too early. "Why the split is the whole
architecture" names *both* filters — convergence and prediction — so placing it on §2.1c forces a
forward reference to a mechanism the reader has not met. **Move that paragraph to the end of
§2.2.** Then §2.1c ends on a question and §2.2 opens by answering it, which reads as momentum
rather than administration:

> **§2.1c closes:** …so something has to decide that a convergence is not worth thinking about
> at all, and that is where nearly all the saving happens.
>
> **§2.2 opens:** That decision is this. Before an integrator fires, the cluster predicts what it
> is about to conclude.

The general rule, worth applying anywhere else a forward reference appears: **a forward reference
means the information is in the wrong order, or a summary is sitting ahead of the material it
summarizes.** Fix the order. Pointers are a patch over a sequencing problem, and a reader feels
the seam every time.

**Total: 5 framing + 12 app + 1 operator loop + 6 brain overview + 3 confidence + 2 vocabulary + 56 detail = 85 slides.**

If the deck needs to come down, move Perception and Platform detail into an appendix. Do not cut the Cognitive Core, and do not cut the two vocabulary slides — they are what make the other 79 readable.

---

## Corrected facts

These are wrong in the current deck. `SYSTEMS.md` has already been corrected to match; use these numbers.

| Claim in the deck | Correct value |
|---|---|
| "36 named theories" | **65** theory-index entries |
| "~90 subsystems" | **95** |
| §1.4 "~36 emotions" | **37 distinct emotion words** resolved from **81** chemical bucket combinations |
| §1.6 "~45 emotions" | **57 labels** under **8 families** — a *different* table from §1.4, not the same number stated twice |
| §1.7 mood colors | **41** colored emotions; 6 more resolve through the hierarchy; **10 fall through to neutral**, and 9 of those are reachable from the chemistry lookup. Say this plainly, it is a real and small gap |
| §1 divider lists 8 subsystems | Only 6 slides after merging 1.2+1.3 and 1.6+1.7. Renumber the list; do not author two new slides |
| §2 divider lists 2.1–2.10 | System 2 now has **10** parts (structural plasticity moved out to Learning; §2.10 approach competition moved in). Both §2.9 "Deciding across turns" and §2.10 "Deliberating before acting" have no slide and must be added |
| §4 divider lists 4.1–4.10 | System 4 now has **12** parts — §4.11 path plasticity and §4.12 growing new units moved in from the Cognitive Core |
| §7.9 "per-mandate reward weights stored and not consumed" | Now **consumed**, with the Stoic control exempt |
| Slide 3 pager `09 / 10` in position 3 | Slide is deleted as a duplicate |

---

## The self-containment check

Run this on every detail slide before calling it done. It is the acceptance test for the whole restructure.

1. **Cover everything but this one slide. Can a reader who has seen no other slide say what this thing is?** If not, the standfirst is failing.
2. **Circle every noun that is specific to this system.** For each one, was it defined on this slide, on the vocabulary slides, or on an earlier slide that the locator strip points to? If none of those, it is undefined and the slide is not finished.
3. **Is there a sentence saying what breaks without this part?** If not, either write it or merge the slide into its neighbour.
4. **Does the slide open on what one person experiences, or on a guarantee?** If the first thing a reader meets is isolation, tenancy, or "per customer," it is framed backwards. Move the guarantee to the closing line and lead with the human fact. (Exempt: Part 1 app slides and §9.1, §9.3, §9.6–9.8, where the reader is the operator.)
5. **Does the locator strip name a real upstream and downstream?** "Feeds: everything" is not an answer.
6. **Any bare `gate`, `channel`, `weight`, `lane`, or `valence`?** Qualify it, or in the case of `valence`, replace it with "pleasantness."
7. **Any abbreviation used before its full name appears anywhere in the deck?** Spell it out.

## Screenshots needed

Capture from the running app and upload to the project. Names match the table above.

`mri-full` · `mri-plasticity` · `mri-approval` · `agents-grid` · `jobs-table` · `job-detail` · `skills-screen` (showing the self-authored section and the review queue) · `account-limits` · `connectors` · `personas-grid` · `persona-temperament` · `persona-chemistry` · `persona-selfmd` · `learning-stories` (with one claim expanded to show its evidence) · `learning-dashboard` · `api-reference` · `partner-keys`

---

## Pull quotes worth using as headlines

These are verbatim from the code and better than anything a rewrite will produce.

- "Storage is the cheap part; retrieval is the intelligence." (**Do not use the old form, "storage is free." It is not free, and an engineer will ask about retention within seconds of hearing it.** The argument is that storage is cheap *relative to the cost of deciding what to keep*, which is what justifies indiscriminate encoding. Say that.)
- "Chemistry may modulate EFFORT and ATTENTION. It must never widen MONEY."
- "The prediction is statistically valid but morally wrong. The moment deserves fresh attention, not a cached response."
- "The gate's test suite IS the privacy proof."
- "A reflex has fixed parameters; anything context-dependent stays deliberate."
- "Never read topic/entity strings here. That would let domain leak into the signature and break transfer."
- "Pride is INTRINSIC: nailing its own standard is enough to feel it, whether or not the user acknowledges it."
- "Intrinsic far exceeding external means the brain is mostly rewarding itself." (the system instrumenting its own biggest weakness)
- "A weaker model picks worse actions but can do nothing the allowlist forbids. The safety posture is the dispatcher's, not the provider's."
