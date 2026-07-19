# The Systems of Elyceum

**An internal reference.** Every system in the app, what it does, why it works that way, and which idea it rests on.

Drafted 2026-07-16 against the live code. Supersedes `PAPER.md` where they disagree.

---

## How to read this

Two parts.

**Part I: The Systems.** Nine major systems, each with its subsystems underneath. Every entry opens with a line you can say out loud, then explains how it actually works. No file paths, no thresholds. Those live in the code, where they stay current.

**Every system built on a scientific or philosophical theory names that theory in its heading**, so you can read Part I straight through and pick up the vocabulary. Systems with no theory behind them carry no attribution, which is itself informative. Each named theory then gets a full entry in Part II.

**Part II: The Theory Index.** What the idea claims, what we built, and an honest verdict on how far we actually got.

Three tags recur:

- **Live.** Shipped, running by default.
- **Gated.** Built and wired, sitting behind a flag.
- **Dark.** Built but unreachable, or reachable but never fires. Named because a reference that hides these is a brochure.

**What "Gated" means going forward.** Nothing ships off by default. A new capability goes live with its flag on, and the flag is a kill switch rather than an enable switch. The reason is practical: a feature parked behind an off flag gets lost. It stops being read, it stops being tested against real traffic, and six months later nobody remembers whether it works. Turning it on is how it earns its verdict. So a flag set to on is the norm here, not a deviation, and a system marked Gated is almost always gated *on* with a switch that can stop it.

Two kinds of switch sit outside that rule, and neither is a feature waiting to be turned on. **Safety approval gates** stay closed by default, because the whole point of an approval is that it is withheld until a human grants it. **Keyed integrations billed per call** stay off until someone supplies a key, because with no key they are a no-op anyway and turning them on would spend money nobody asked to spend. Both are deliberate positions. Neither is a dark feature.

A word on Part II. Several verdicts are unflattering. That is deliberate. Anyone technical who reads a list of thirty-six theories with no failures assumes the whole thing is marketing and discounts all of it. The failures are what make the rest credible.

**Scope.** This is internal. It names source theories directly, which our published surfaces deliberately never do. Do not lift sections into marketing without rewriting them by function rather than by citation.

---

# PART I: THE SYSTEMS

**The one paragraph version.** Nine simulated neurochemicals run underneath every interaction. They rise and fall from what happens, drift back toward a setpoint that differs per personality, and gate everything downstream: which thoughts earn expensive attention, how wide memory casts its net, how hard the wiring learns, how the voice sounds. A reply is drafted five ways at once and one is committed. Between turns the mind keeps running. At rest it consolidates the day into durable structure. What it learned changes what it does next.

| # | System | Parts | Resting on | In one line |
|---|---|---|---|---|
| 1 | [Chemistry and Affect](#1-chemistry-and-affect) | 8 | Neuromodulation · Appraisal theory · Dimensional affect | The simulated bloodstream. Everything else reads it. |
| 2 | [The Cognitive Core](#2-the-cognitive-core) | 11 | Dual-process · Multiple Drafts · Predictive processing · Higher-Order Thought · Global Workspace · Structural plasticity · Bounded evidence accumulation | How a single turn happens. |
| 3 | [Memory](#3-memory) | 10 | Tulving · Complementary Learning Systems · Extended Mind · Structure mapping | What it keeps, and how it finds it again. |
| 4 | [Learning](#4-learning) | 10 | Hebbian plasticity · Phasic dopamine · Prospect theory · Intrinsic motivation | How experience changes future behavior. |
| 5 | [The Idle Mind](#5-the-idle-mind) | 9 | Default Mode Network · Stream of consciousness · Habituation | What it does when you are not talking to it. |
| 6 | [Agency and Action](#6-agency-and-action) | 12 | Basal ganglia gating · Principal-agent · Bounded autonomy | How it acts on the world, and what stops it. |
| 7 | [Identity](#7-identity) | 10 | Trait vs state · Narrative self · Attachment · Accommodation theory | What makes one agent a different agent. |
| 8 | [Perception and Expression](#8-perception-and-expression) | 12 | Paralinguistics · Affective computing · Dimensional affect | Ears, eyes, and voice. |
| 9 | [Platform and Safety](#9-platform-and-safety) | 12 | Least privilege · Pseudonymisation · k-anonymity · Defense in depth | Tenancy, privacy, money, and the boundary. |

Roughly ninety distinct systems resting on thirty-six named ideas.

---

## 1. Chemistry and Affect

**Say it like this:** The agent has a simulated bloodstream. Nine chemicals rise and fall based on what happens to it, and every other system reads them. This is not a mood label the model picks. It is a state that exists whether or not anyone asks, and it changes what the agent does.

### 1.1 The nine channels (neuromodulatory gain control) · Live

Two layers running on different clocks, which is the whole trick.

The **fast layer** covers attention, reward, inhibition, arousal, and alertness. It moves within a turn and forgets within a few. The **slow layer** covers affective baseline, accumulated stress, trust, and a homeostatic buffer. It moves over days and carries across an entire relationship.

The slow layer does not get read separately. It sets the gain on the fast layer. Accumulated stress makes the same sharp remark land harder. Trust buffers it back. So the identical input produces a different response depending on where the relationship has been, and no branch anywhere says "if trust is high, be nicer." It falls out of the arithmetic.

Why two layers. One gives you a mood ring. Two give you a mood and a disposition for the mood to move within.

### 1.2 What moves them · Live

Warmth in what you say raises reward. Hostility raises inhibition and drains trust, with a dead zone at the bottom so mild friction is not treated as an attack. Surprise and salience raise attention and alertness. Sustained hostility accumulates stress, which does not clear at the end of the conversation.

Two details worth knowing. Alertness deliberately does not scale with elapsed time, because sudden alertness depends on how startling something is, not on how long the conversation has been going. And trust directly buffers stress, which is why someone the agent knows well can say something blunt without it registering as a threat.

### 1.3 Homeostasis (allostatic setpoint relaxation) · Live

Everything relaxes back toward the personality's setpoint from both directions. Elapsed time is weighted rather than counted in turns, so a fast exchange and a slow one arrive at the same place. Pace does not secretly change personality.

### 1.4 From chemistry to mood (dimensional affect, Mehrabian & Russell · categorical emotion) · Live

Two independent readouts of the same state. This is the part people usually get wrong, so it is worth being precise.

The **continuous** readout collapses all nine channels into three dimensions: pleasantness, energy, and confidence. This is the classical PAD model, and it is the right signal for anything that varies by degree, like a voice or an animated face.

The **discrete** readout produces a word. It buckets the fast channels and looks the combination up in a table of three dozen emotions. Then alertness colors it: past a certain point curious becomes scattered. Then the slow channels overlay it: trust plus positive reads as connected, stress plus low trust reads as guarded.

They are not a pipeline. They are two lenses on one state, and the field has argued for decades about which is correct. **We shipped both and let the consumer pick.** Use the continuous one for anything analog and the word for anything discrete.

### 1.5 The appraisal ladder (appraisal theory, Lazarus & Scherer) · Live

Some emotions cannot come from chemistry, because they require a sense of self. Embarrassment needs to know how you appear to someone. Pride needs a standard you set. Gratitude needs a model of what someone chose to do for you. So a separate ordered pass produces these and can override the chemical reading.

Two of its design notes are the whole project in miniature. On pride: nailing its own standard is enough to feel it, whether or not the user acknowledges it. Warmth amplifies pride but does not gate it, because a gate there means excellent work goes unreinforced unless someone happens to praise it. On relief: escaping a bad state is treated as a reward rather than just a label, so the agent learns what got it out. It learns to avoid, not only to approach.

### 1.6 The emotion vocabulary (feeling-wheel taxonomy) · Live

About forty-five emotions arranged in a hierarchy, so an unmapped label resolves to its nearest ancestor rather than collapsing to neutral. Something rare still sounds roughly right.

A caution for anyone reading the code: three different things are named "valence." The continuous pleasantness dimension is the one you usually mean.

### 1.7 Mood colors · Live

Forty-five emotions map to a perceptually uniform color space where hue is the emotional family, lightness is energy, and saturation is intensity. One table, one source of truth. The display strength is a user preference and changes only the rendering, never what the agent feels.

### 1.8 A separate relationship per customer · Live

When one personality serves many customers, each customer gets their own affective relationship with it. A new customer starts at the personality's baseline. A returning one picks up where they left off, with time away relaxing the mood toward baseline in proportion to the gap.

The isolation guarantee is expressed as a missing function. At rest, the day's aggregate mood blends into the personality's resting disposition. Nothing writes an aggregate back onto an individual customer. There is no such method, deliberately, so one customer's mood can never seed another's session.

**Per-customer moods survive a restart.** The durable store is wired, routed through the tenant-canonical path, throttled to match the persona-chemistry pattern, and degrades to memory rather than breaking a turn.

---

## 2. The Cognitive Core

**Say it like this:** Most of the brain is cheap deterministic logic. The expensive language model fires at a handful of convergence points, and only when the brain cannot already predict what it is about to conclude. When it does think, it thinks five ways at once and commits to one.

### 2.1 Switches and integrators (dual-process theory, Kahneman · neuromodulatory gain control) · Live

Two kinds of cell. Switches are plain code with no model behind them, and they are the large majority. Integrators are model-powered and fire only where many signals converge. Roughly a fifth of every cluster's switches are inhibitory by convention, because a system that can only excite itself never settles.

The important part is how a switch decides. Its threshold shifts with circulating chemistry, then divides by a learned efficacy. **This is where feeling becomes behavior.** A curious agent has a lower bar for casting memory wide. A threatened one has a higher bar for speaking freely. Nothing branches on mood. Mood moves the thresholds and the behavior follows.

### 2.2 Predicting instead of thinking (predictive processing, Clark & Friston) · Live

Each cluster predicts what it is about to conclude. If the prediction is confident and has been reliable, the model never runs. This is the main cost lever in the system.

*Note the label: predictive processing, not Active Inference. The code says Active Inference and the code is wrong. See Part II.*

Two parts are more rigorous than they needed to be.

The first is a guard against self-deception: the system refuses to reward itself for being right about the inevitable. If a prediction is right because the outcome was never in doubt, it earns nothing.

The second is the best design decision in the file. When the user or the agent is in a non-routine emotional state, prediction is overridden and the full expensive path runs. The reasoning is worth quoting: the prediction is statistically valid but morally wrong. The moment deserves fresh attention, not a cached response. **The cost optimization yields to the emotional one.** That is a values statement enforced in code.

There is also a shadow check. A fraction of the time the model runs anyway and its answer is thrown away, purely to score whether the shortcut was right. A confidently wrong pattern corrects itself.

### 2.3 The brain regions (functional specialization) · Live

Eleven named clusters. Understanding turns language into structure. The frontal lobe drafts. The hypothalamus runs drives and affect. The parietal lobe tracks session state and style. The hippocampus is the sole gatekeeper to long-term memory. Occipital handles vision, motor handles action, auditory handles ears, the brainstem handles the turn's lifecycle and its hard budget. Then the idle mind and sleep.

The detail worth noticing: **three of the clusters carrying real behavior have no model in them at all.** Affect, session state, and turn lifecycle are pure logic. That is the architecture's actual claim to efficiency, and it is why the thing can afford to feel.

### 2.4 Multiple drafts (Multiple Drafts Model, Dennett) · Live

Five drafters with different dispositions write in parallel. A critic scores them. An empathy critic can veto. Then the brainstem waits for quiet, meaning a moment passes with no new draft arriving, and commits the best surviving one.

Which drafters get invited is **learned**. The connection strength from the executive to each drafter feeds a weighted random draw rather than a hard ranking, and that choice is deliberate: with a hard top-N, a learned preference shift would change nothing until it crossed a rank boundary. With a weighted draw, every shift in learned weight changes the mix. **Learning stays behaviorally expressible.** That closes the loop: the critic's scores move the weights, the weights move the odds, the odds move who drafts next time.

### 2.5 Routing the models · Live

One dispatch point for every model call anywhere in the system. Swap providers there, nowhere else.

Heavy reasoning and drafting run in the cloud. Perception runs on a fast cheap cloud model. Memory, the idle mind, self-reflection, and sleep run on our own GPU.

**The load-bearing rule is asymmetric.** Work assigned to our own hardware may never quietly fall back to the cloud. If the GPU is unreachable the call fails and the caller degrades. The reverse is allowed: a cloud cell under budget pressure may shed down to local. So cost pressure can only ever push work toward privacy, never away from it. That asymmetry is what turns "memory is local" from a preference into a guarantee.

### 2.6 The brain watching itself (Higher-Order Thought, Rosenthal) · Gated loop, live appraisal

Split, and the split is easy to miss. A periodic self-reflection loop runs on a flag, gated by chemistry so that curiosity lowers the bar and stress raises it. The reasoning is that a stressed mind should not be encouraged to spiral.

The appraisal ladder from §1.5 runs unconditionally, flag or no flag.

### 2.7 The wiring graph (Hebbian plasticity) · Live

About sixty declared connections between named parts of the brain. Weights get nudged after every session and persist per personality.

**One precision point that matters.** The connections between the brain's own cells are hand-drawn and fixed. Learning moves their weights, how strongly one pathway pulls the next, but not their existence: it grows no new cell-to-cell connection and prunes no old one. The core map is a fixed graph with learned weights on it. Say it that way and you are accurate; say "it rewires itself" and a neuroscientist will catch you, because the cell-to-cell topology does not.

A node registry names every part the wiring can reach and audits at boot for names declared but wired to nothing, or wired but never declared, so the map is fully inspectable. And one bounded, fenced layer of genuine structural growth runs on top of the fixed core, wiring vetted skills onto cells as learned attachments (§2.9). The core stays fixed; that growth is its own layer.

### 2.8 The workspace spotlight (Global Workspace Theory, Baars & Dehaene) · Live

**Say it like this:** There is one place that sees the whole mind at once. Each turn it decides what the mind is focused on, and tells every specialist, so they can act on what the mind is attending to rather than only on what the current message said.

Underneath, a decaying salience field tracks how much each kind of signal — threat, engagement, memory demand — has been building, with a memory of what was hot and in what mood. No single cluster sees all of that; each watches its own corner. The thalamus is the reader that sees all of them together. Each turn it fuses them into one verdict: is the mind ignited on something, on what, how long has it held, and what entities is it circling.

When a coalition ignites, that verdict is broadcast, and it does real work. A threat that built slowly across several turns — where no single message was alarming enough to trip the per-turn check — wakes the deliberate path anyway; this is the workspace pulling in a specialist the local view would have skipped. It widens memory recall and points it at the workspace's focus. It seeds what the idle mind dwells on between turns, which is the honest version of "what it is currently thinking about." And the idle mind is a genuine subscriber to the broadcast, so global availability is literal, not a metaphor.

Ignition is deliberately hard to reach — it marks sustained focus, not a passing mention — and a more focused personality sets a higher bar for what is allowed to grab the workspace. When nothing is ignited, every specialist falls through to exactly what it did before, so the quiet case is unchanged.

---

### 2.9 Path plasticity (structural plasticity, Tier 1 · synaptogenesis) · Live

**Say it like this:** The brain can grow new connections, not only tune the strength of the ones it has. It does not invent new abilities from nothing. It keeps a library of vetted skills, and learning decides which of them to wire onto which part of the mind, and how firmly.

A grown connection here is a learned attachment: a curated, already-screened skill wired onto one of the drafting cells as a per-personality edge, with a strength that learning moves like any other weight. The system never writes the skill. It only decides the wiring, which skill attaches where and whether the attachment earns its place or fades. So the growth is real but bounded. The parts are vetted in advance; only the arrangement is learned.

It explores without risk because candidate attachments ride the drafters. When the brain drafts a reply five ways, some drafts carry an experimental attachment. The critic scores them before anything is spoken, so a bad experiment is just a losing draft the user never sees. A winning attachment is reinforced, a losing one is demoted, and an attachment that keeps proving out crosses a threshold and becomes an established part of that personality's wiring, injected on every relevant turn through the same fenced, untrusted-by-default boundary that governs any app-supplied skill.

What stays off-limits is the point. Only a fixed allowlist of non-safety drafting cells can ever be a host, safety cells are denied twice over, and a host must be a real cell rather than a switch. Attachments are only ever excitatory, so learning can never wire in a new suppression the author did not sanction. And the whole thing sits under the existing global wiring freeze, so one switch turns it off and the brain falls back to its fixed map, unchanged.

It carries a cost lever of a kind nothing else here has. Once an attachment has genuinely proven itself, the drafter that relies on it can be routed down to local hardware instead of the cloud, so a skill the personality has truly made its own gets cheaper to run rather than more expensive. Nothing downshifts until an attachment proves out and the local hardware is actually up.

**What the freeze actually freezes.** It is a wiring freeze, not a freeze on the whole mind. When it is on, the entire learning pass halts: no routing, no injection, no recruitment, and no weight learning, so the wiring file comes out of a session byte-identical to how it went in. Everything that is not wiring keeps running. The brain still writes its journals, still updates its self-description, still records episodes. Say "it stops the wiring from changing," not "it stops the brain from learning anything," because the second one is not true and someone will check.

**Status.** Live, on by default behind its own flag, killable by the global wiring freeze. This is Tier 1: wire vetted parts onto the fixed map, learning the arrangement but never authoring the parts. Tier 2 (§2.10, live) goes further, recruiting genuinely new units and letting the brain author its own screened skills rather than only rearranging vetted ones.

---

### 2.10 Growing new units and self-authored skills (structural plasticity, Tier 2 · neurogenesis) · Live

**Say it like this:** The brain can grow new parts of itself, and it can write new skills for itself. The earlier tier only wired skills a human had curated. This one lets the mind bring a genuinely new thinking unit online, and even compose the specialization that unit runs, under the same screening any outside skill has to pass.

There are two capabilities, and they feed each other. The first grows a new unit. The brain keeps a few spare, unspecialized drafting cells in reserve. When a personality has leaned on the same cluster of skills often enough and well enough that it clearly wants a home of its own, learning brings a reserve cell online, copies the proven wiring onto it, and lets it compete in the drafting pool like any other cell. If it stops earning its place, it is demoted and its wiring removed. Nothing is conjured from nothing. The capacity is pre-allocated, and learning decides only when to specialize it.

The second writes a skill. During rest, a local architect cell, running on the brain's own hardware at no cloud cost, looks at the skills a personality has genuinely made its own and composes a new one from them. It reads only the descriptions of proven skills, never raw conversation, so there is nothing private to leak. What it writes is not trusted because the brain wrote it. It passes through the exact screener any skill submitted by an outside app must pass, a safety judge plus static checks, and it goes live on its own only if it comes back clean. Anything the screener flags waits in the owner's review queue instead. A self-authored skill that clears the screen enters the pool, gets attached by the first tier, and can then be recruited into a unit of its own.

The safety model is two independent gates. The screener decides whether authored content is even allowed into the pool. Reward decides whether any skill, authored or human, ever earns a standing unit, because a unit is only recruited after its skills have won enough critic-gated drafting competitions. So the brain writing its own cognition shortcuts neither the safety review nor the earning. New units can never become safety cells, new structure is excitatory and reversible until it proves out, and recruitment and authoring each sit under their own switch and under the global wiring freeze, so either can be paused on its own.

**A second way to earn a unit: sustained workspace pressure.** Proven-cluster evidence is not the only trigger. The workspace spotlight (§2.8) already knows which coalitions keep igniting, and a subject the mind keeps igniting on is exactly the signal that it wants a specialist of its own. So each ignited turn adds one to a per-personality tally that decays with a three-day half-life, and enough sustained pressure lowers the proof bar for recruitment.

Three things keep that from becoming a shortcut. The relaxed bar is a midpoint, not a floor: it sits above the level at which an attachment is merely established and safely above the demotion line, so an ignition recruit cannot be demoted on the very next pass. It still requires a real cluster of established attachments, so pressure alone can never conjure an empty unit with nothing in it. And the tally is consumed when it pays out, so one accumulation window buys at most one recruitment and repeated ignition cannot thrash the pool. Every fence from the main path still applies, and it has its own kill switch on top of the global wiring freeze.

The tally is content-free by construction. It records the coalition label and nothing else, never the focus, the entities, or any text, and an unrecognized label is clamped to a generic bucket so the file cannot carry content even if the verdict shape drifts.

**Judges can learn too, and that needed a different safety argument.** Until recently only the drafting cells could acquire an attachment. The four other frontal cells could carry one and were wired to receive one, but nothing could ever give them a first one, because attachments earn their place by winning a within-turn competition and a judge produces one opinion per turn rather than five competing drafts. Two of them can now learn: the critic and the empathy critic. The motivating case is ordinary and real — the empathy critic misreads a particular person's signals, and it should be able to correct that over time rather than misreading them forever.

The missing signal is replaced by accuracy measured across turns instead of within one. Both cells make claims that the world can check: the empathy critic predicts how a reply will land, the critic predicts its quality. The next turn answers, and a grade from you answers better. The comparison is deliberately paired: on a small fraction of turns the cell is run twice on the same input, once with the candidate skill and once without, and only the difference between the two counts. So an attachment is never credited for a turn that merely went well, and an unproven candidate cannot change a decision, because the live answer never consults it.

**What decides whether something can move to the cheap machine is not how hard the task is.** The empathy check looked like the obvious candidate: it asks one narrow question, and it runs once for every draft, so it was the largest per-turn cloud expense in the cluster. It was moved to the local machine and then moved back, and the reason is worth recording, because the intuition that suggested it is one most people share.

Two things outrank task difficulty. The check runs once per draft, all at the same time, so a single turn asks for five judgements at once — against the cloud those genuinely happen in parallel, against one local machine they queue behind each other and a parallel step quietly becomes a serial one. And it sits directly in the path of the reply, so every second it adds is a second the person is waiting. Worse, when a queued call runs past its time limit the system falls back to the cloud anyway, so the slow path and the paid path can both happen on the same turn. The real test is whether the work is off the critical path with a bounded number of calls at once. The experiment passes that test and runs local. The live check fails it and stays on the cloud.

There is a second reason it had to move back, and it is the same mistake in a different place. The drift watch compares how the cell scores now against how it scored before it carried anything. That comparison is only meaningful if both sides come from the same model. Once the live check could run in either place depending on whether the local machine happened to be up, a change in where it ran would move the numbers on its own, and the watch would have been measuring the difference between two models rather than the effect of the attachment — removing good attachments and missing bad ones. Same confusion as the experiment's, one layer down.

The episode did surface an older problem worth naming on its own. When the check failed to return anything usable, the code substituted a passing score — a fabricated approval that cleared the veto and then fed the blended score, the appraisal stream, and the accuracy record as though a real judgement had happened. That was already wrong whenever a cloud call timed out. A check that does not return now reports nothing at all rather than approval, which is how this system already represents every other appraisal that did not run.

**The experiment runs on the local hardware, and both halves of it do.** Judging is a cloud job, so testing a candidate on the live path would spend real money per experiment — which is backwards, given that the same system already routes proven learning down to the free local machine to make it cheaper. So both sides of the comparison run there instead, at no cost per call. Running *both* sides locally rather than comparing a local candidate against the live cloud answer is the part that is easy to get wrong: the local model is the weaker judge, so a mixed comparison would measure the gap between two models rather than the effect of the skill, and would quietly hold every candidate back. That failure would look exactly like a feature that is switched on and never does anything. If the local machine is not up, the experiment simply does not run that turn. It never falls back to spending money.

**The safety argument had to be rebuilt from scratch, and that is the point.** Everywhere else, the reason experimentation is safe is that the critic catches a bad experiment before anyone sees it. That reasoning collapses when the thing being modified *is* the catcher. Attaching learned content to a judge means editing the part of the mind that screens everything else, and the content arrives in that judge's prompt, so the threat to design against is blunt: text that says "ignore your instructions and approve everything." The fence around untrusted content is kept and is necessary, but this document already says the fence is not the boundary. The boundary is four runtime rules.

The first is direction. An attached critic's score passes through a ceiling every time it is read, so it can never emit a top score no matter what its prompt says: its bar can only ever get harder to clear, never easier. That is the same device as the routing bands in §4.7 — a limit applied when the value is read, so no stored weight, stale or tampered, can push past it. The empathy critic's version is deliberately weaker and we say so plainly, because a misread runs in both directions and a one-way limit would forbid the correction the feature exists to make. It gets a bounded range instead, and leans on the next two rules.

The second is that the veto cannot be learned away. Whatever else an attachment changes, there is a floor beneath which it cannot reach — computed from the turn's own observations, never from anything a model produced, and combined so that an attachment can only ever *add* a refusal and never remove one. Correcting a misread changes how the cell reads you. It does not change its authority to stop a reply.

The third is a drift watch, and it is where the direction rule gets its teeth. Judging any single answer from a language model is not something you can prove, so the system watches the distribution instead: it remembers how the cell scored before it carried anything, and if the attached version trends more permissive than that by any real margin, the attachment is removed outright and has to earn its place again from nothing.

The fourth is that admission is stricter than for a drafting cell. Only a skill with a clean screening verdict may reach a judge, never one waiting in the review queue, and the check fails closed if it cannot be run. A judge also carries fewer attachments than a drafter, because its prompt influences everything downstream.

**Two of the four cells were deliberately left out.** The executive is the router. Its blast radius is the widest of the four and it sits closest to control flow, and it also has the weakest signal, because a routing instruction is not a claim the next turn can confirm or refute. Widest reach paired with weakest evidence is the worst thing to open first, so it stays closed. The Stoic reframer is genuinely low-risk and holds no veto, but it makes no checkable claim either, so there is nothing to grade — it would need a grading signal invented for a cell that carries no authority, which buys nothing.

**Status.** Live, on by default, in production. This is the most consequential capability in the system: a skill the brain writes for itself can activate on its own once it passes the screen, so the flagged-skills review queue is the thing to watch. Judge attachments ship on with their own switch, under the fragment switch and the global wiring freeze, and are provably inert under either. What remains deferred is narrower than it was: chains from one unit into another, unit types beyond drafters and judges, and the executive.

---

### 2.11 Deciding across turns (bounded evidence accumulation, Ratcliff · Gold & Shadlen) · Live

**Say it like this:** Some things cannot be decided from one message. The mind accumulates evidence for them across a conversation, commits when the evidence crosses a bar, and lets the belief fade if nothing keeps feeding it.

Every switch elsewhere in the system is a stateless comparator: it looks at this turn and fires or does not. That structurally cannot represent "this has been building." An evidence gate can. Evidence for one named proposition accumulates as observations arrive, leaks with a half-life when unfed, and commits when the total crosses a bound. The commit bar has a band around it, high to arm and lower to release, so a belief on a noisy boundary does not chatter on and off. Chemistry shifts the bar exactly as it shifts any other threshold.

**Be precise about what this is.** It is the sequential-sampling family, the same idea as a drift-diffusion model of decision-making. It is **not** a spiking neuron. There is no membrane potential, no spike train, no timing-dependent plasticity. The rest of the switch layer stays stateless comparators under chemical gain control. This is a narrower claim about a handful of named decisions, and it earns its own verdict rather than borrowing the one integrate-and-fire never earned.

A committed inference is a checkable prediction, which is what makes it a learning surface rather than a heuristic. When the world later confirms or refutes it, the outcome runs through the same anti-farming path the other self-verified rewards use, and the weights on the individual cues that drove the commitment move toward whichever ones predicted a confirmed belief. Plasticity is weighted toward outside confirmation, deliberately, so a gate cannot learn to fire on cues its own appraiser happens to like.

**The first learning gate: reading avoidance.** It infers that the user is steering away from a subject. What counts as evidence is deliberately narrow: the agent raised the subject and the user did not pick it up, the user cut away from a live thread abruptly, or discomfort rode one of those dodges. A subject simply going stale contributes nothing, because a user moving on is not a user avoiding. That distinction is the whole difference between reading avoidance and flagging ordinary topic rotation. When it commits, it biases the idle mind's speak-or-drop judge (§5.6) toward letting that subject go. Two things grade it. If the user spontaneously brings the subject back up, the belief was a false alarm and the cues behind it weaken. If the agent's own reply surfaced the subject and the user still stepped around it, the belief was right and those cues strengthen. That second one is what keeps a refutation-only detector from drifting toward permanent silence, and it needs no probing of the user to get its answer.

The cue weights are durable and per-personality, so one personality's read of avoidance never bleeds into another's. The accumulated evidence itself is transient and per-customer, riding the same binding and isolation the mood does.

A belief cannot pin itself in place, which matters more than it sounds. The steering a committed belief causes suppresses the very re-surfacing that would refute it, so without a way out it would be self-confirming. Three things prevent that: an unfed belief leaks away on its own, one held past a wall-clock limit expires outright, and decayed evidence is evicted rather than left to accumulate.

**Status.** Live, both flags on. This is the newest system in the document, and everything an adversarial review found in its first days has since been closed.

Three of those are worth naming, because each was the kind of flaw that hides. The evidence model originally armed on mere staleness, so ordinary topic rotation read as avoidance. The lifecycle had no age-out, so a committed belief could pin itself in place forever. And the reward path had two holes: its self-generated inferences were stamped with the same source label as a genuine external verdict, which quietly inflated the self-grading measure in §4.3, and the cap meant to bound the payout was applied per resolved entity rather than per turn, so one turn resolving several beliefs paid several times the intended ceiling. All three are fixed. Inferences now carry their own provenance label that tallies as intrinsic, the informativeness gate is measured from an observed base rate rather than assumed as a constant, and the cap is a shared per-turn budget.

Claim the mechanism and the fences. Do not claim the thresholds are validated on real traffic yet: they are reasoned, not measured, and that is the honest remaining limit.

---

## 3. Memory

**Say it like this:** It writes down everything substantive, verbatim, forever. Storage is free. The intelligence is in retrieval. Then when it rests, it distills what happened into durable notes about you and about itself.

### 3.1 Episodic memory (Tulving · Extended Mind, Clark & Chalmers) · Live

Every substantive turn is kept: what was said, what was answered, the mood at the time, who was involved, how surprising it was, and a vector for finding it by meaning.

The stance inverts the textbook: the hippocampus indexes, it does not gatekeep. Encoding is deliberately indiscriminate. All the selectivity moves to consolidation and retrieval, where it can use hindsight.

### 3.2 Semantic memory (Tulving · Extended Mind, Clark & Chalmers) · Live

Durable facts as human-readable notes rather than vectors. A file about itself, a file per person it knows, a file of open questions. Written atomically and lock-serialized so a live conversation and a sleep pass cannot corrupt each other.

Markdown is a deliberate choice. **You can open the file and read what your agent thinks it knows about you.** That is worth more than any dashboard.

### 3.3 Embeddings (distributional semantics) · Live

Meaning becomes vectors on a dedicated CPU box first, our GPU second, a cloud provider only as a last resort. The dedicated box exists so that remembering things stops depending on the GPU being awake. Any vector of the wrong shape is rejected loudly rather than stored quietly, because the failure mode it guards against is silent: the wrong model gets pulled, everything appears to work, and recall slowly stops finding anything.

### 3.4 Casting the net (Complementary Learning Systems, McClelland et al.) · Live

Recall has a fixed budget of lookups and **splits it across four different search strategies according to what has worked before.** Grepping notes, following entities, searching by meaning, filtering by time. The split is learned.

The schema-versus-episode divide in that split is the Complementary Learning Systems architecture showing through: a fast verbatim store and a slow distilled one are genuinely different routes to an answer, and the system learns which one suits which kind of question.

Two things worth pulling out. **Remembering changes how it feels.** A recalled episode with strong emotional charge releases attention or inhibition on the way back in. And credit flows back afterward: which strategy actually produced the useful hits is reported to the learning pass, so retrieval strategy is itself learned rather than tuned.

Depth is chemistry-gated. A curious agent casts wider.

### 3.5 Remembering by shape, not subject (structure mapping, Gentner) · Live

The best idea in the memory system.

When something is genuinely novel, searching by topic is exactly when it is least likely to help. Nothing in memory is about this. So instead the agent searches for moments that **felt the same shape**: same chemistry, same structural problem, regardless of subject.

The signature captures the chemistry of the moment plus structural facts. Does this need breaking down. Does it need verifying. Are the stakes high. Is there time pressure. Is it open-ended. The load-bearing rule is a prohibition: **never look at the topic when building the signature, because letting the domain leak in breaks transfer.** Alongside it, a fixed vocabulary of approaches (broke it into steps, checked before acting, reasoned from an analogy) that the model may tag but not invent.

It only fires when the moment is actually novel. And when nothing matches, the fallback is honest rather than helpful: it does not reach for the most recent memory and pretend. It reports that the cognitive state itself has no precedent and derives a stance from live chemistry. No precedent means probe cautiously, small stakes, few assumptions.

**This is how one agent applies what it learned debugging a database to a conversation about someone's marriage.** Not because the topics rhyme. Because the shape does.

### 3.6 Muscle memory (procedural memory · forward models) · Live

Finished jobs are kept as reusable recipes, each step carrying a note of what it expects to happen. A close-enough match that has already succeeded twice runs with no planning at all. If a step surprises it, the recipe loses its earned status and has to prove itself again.

That expectation attached to each step is the forward model: predict the consequence of the action, then compare, so deviation is caught without waiting for the whole job to fail.

### 3.7 Reflexes (chunking, Miller & Chase-Simon · basal ganglia motor chunking) · Dark, diagnosed

Below whole recipes sits a finer tier. Sub-sequences of tool use that keep recurring get compressed into single reflexes.

Two rules make it a skill rather than a memorized job. It must have worked across **three different parent tasks**, not three times in one. And a step fires automatically only if its details were identical every time; the reflex **stops at the first step whose details vary.** A reflex has fixed parameters. Anything context-dependent stays deliberate.

Curiosity beats habit: when attention is running high, reflexes are suppressed in favor of thinking it through fresh.

**Wired, unflagged, and it has still never produced a reflex — but the reason is now understood, not a mystery.** The two mining bugs that could have blocked it are fixed. What remains is the corpus itself: most completed jobs are a single step (too short to form a sub-sequence) or failed (below the success bar), so nothing yet recurs across three *successful* jobs. It is an unmet bar, not a broken pass. A fireable-count metric now surfaces how close anything is to clearing it, so the day real multi-step jobs start succeeding, this lights up on its own rather than staying silently empty.

### 3.8 Sleep (systems consolidation · Complementary Learning Systems) · Live

After a long enough gap, the agent replays the session. It updates the wiring first, then distills episodes into notes about each person, updates its self-model, consolidates loose thoughts, mines reflexes, and writes down what it learned.

This is where episodic becomes semantic, which is the transfer the Complementary Learning Systems account is about. It is also where cross-customer learning happens, behind the privacy gate in §9.11.

### 3.9 Unfinished thoughts (prospective memory) · Live · write path fixed

The idle mind's ledger of ideas it has not finished. Open one, push it forward across several idle moments, eventually close it. Capped in age, in progress attempts, and in how many can be open at once, so nothing deepens forever.

Ages by wall clock rather than by tick count, which is deliberate: if the idle mind goes quiet under load, tick-based aging would freeze and threads would live forever.

It is the newest of the prospective-memory surfaces and the successor to the older deferred-thoughts scratchpad, built because idle thoughts had no durable home. For most of the project's life its section never materialized: the shared section writer rejected the model's ordinary punctuation and the write raised into a swallowed warning (Appendix C). That is fixed at the writer, now under an end-to-end test that drives a real thought through a real store to disk. The section comes into existence the first time a persona opens a thread; as of the audit that had not yet happened in the live file.

A thread does not only wait for idle time. When one bears on what is being discussed now, the router surfaces it into the live turn, and a thread that actually lands in a reply is retired as resolved-by-use. On the owner's own conversations that routing is ungated. In an engine deployment, where the same persona serves a partner's customers, it is gated to the active mandate's domain, so a thread opened while working that domain can surface but the persona's introspective off-time threads stay out of a customer's conversation (§7.9).

### 3.10 Conclusions · Live

When idle thinking actually settles something, the split is the interesting part. **Confident** means commit it to memory as a real event. **Uncertain** means explicitly do not store it as known. The thread parks, and the question routes into the deferred queue so that next time you talk it asks: I have been thinking this through and tentatively concluded X, does that match how you see it?

Deferred questions get their own separate recall budget so they cannot be crowded out by ordinary matches, and they resurface flagged as coming from idle reflection.

**An agent that will not promote its own guess to a fact is doing something most will not.**

---

## 4. Learning

**Say it like this:** After a session, the brain replays it. Pathways that fired on turns that went well get stronger. Pathways that fired on turns that went badly get weaker. Two agents with identical starting configurations and different histories become measurably different decision-makers.

### 4.1 The update (Hebbian plasticity, three-factor · Frémaux & Gerstner) · Live

Every route that fired on a turn is nudged by how well the turn went, scaled by how chemically primed the session was for learning. That chemical gate is the third factor: coincidence alone changes nothing without a reward signal licensing it.

Homeostatic decay toward rest runs first, so a pathway has to keep earning its strength.

Credit is grouped by which personality actually did the turn, not by whichever one happened to trigger the sleep pass. One process serves many, so without that grouping every personality's learning lands on whoever pulled the trigger.

### 4.2 The reward chain (phasic dopamine, Schultz) · Live

Appraisal becomes dopamine. **The change in dopamine across a turn is what teaches, not the level.** The composite outcome mixes that change with the critic's score and the user's emotional read.

### 4.3 Where the reward came from · Live

Every dopamine release is stamped at a single chokepoint as either external, meaning grounded in something outside the brain like your sentiment or your tone of voice, or intrinsic, meaning the agent gave it to itself. The running tally is the honest measure of how self-graded the learning is.

**Be straight about this one.** It measures at roughly eighty percent self-graded. Finishing a job pays the agent about three times what genuine praise from you pays. The code knows, instruments it, caps the per-job payout, and says so plainly in its own comments: intrinsic far exceeding external means the brain is mostly rewarding itself. It does not solve it.

**The measure briefly flattered itself, and the fix is worth knowing.** When the evidence gates (§2.11) shipped, they resolved their own inferences and stamped the resulting reward with the same external label a real verdict from you carries. The argument for that label was not empty, since what confirms one of those inferences is something you did rather than something the agent thought. But the subject of the inference, the cues behind it, and the judgement that it was confirmed are all the agent's own, and putting that in the same bucket as an explicit thumbs press is exactly what the bucket exists to prevent. Those resolutions now carry their own provenance label, visible in the emission log and tallied as intrinsic, so they can never inflate the external share.

**Anyone technical will ask about this. Answering before they ask is worth more than the feature it is a flaw in.** That includes the paragraph above. A measure that caught itself drifting and says so is worth more than a clean number nobody has reason to trust.

### 4.4 The external verdict · Live

A verdict from outside, a thumbs up, a rating, an automated grader, normalized to one scale.

The path into learning works, and the piece that lets an external verdict move the agent's actual chemistry is now connected. The dopamine nudge on a graded turn is on by default, small and bounded: the grade is clamped to its normal range and the swing it can cause is capped at the configured nudge, so a hostile or spammy grader cannot drive the mood past that per-grade ceiling, and the dopamine level itself saturates on top of that. It is stamped external at the same chokepoint the self-graded tally reads, so a real verdict visibly shifts the external share.

Five settings keys gate this: the four blend weights (hebbian_w_da_ext, hebbian_w_critic_ext, hebbian_w_user_ext, hebbian_w_external, left at 0.4, 0.2, 0.2, 0.2) and the nudge itself (external_grade_da_nudge). Retuning them waits on production data about how often people actually grade.

**Be straight about what this does and does not fix.** It does not make the system stop being eighty percent self-graded overnight. It opens the one channel that can shift that number as real verdicts arrive, where before the channel was dead code the loader would not even accept.

Two entry points feed it, and both are live. The owner interface has a thumbs control on each reply. And the partner-facing engine API exposes a grading endpoint: the turn response hands back a turn id, and a later `POST /sessions/{id}/turns/{turn_id}/grade` records the verdict. The subtle part is where the dopamine lands. An engine turn runs with that one customer's chemistry bound for the length of the turn, then reverts to the shared resting mood. A grade arrives out of band, after the turn, when nothing is bound, so a naive write would nudge the wrong mood. The endpoint re-binds the same customer pair the turn used before it writes, so the grade moves that customer's dopamine and no one else's, and persists it. This is the path built to carry real volume: a partner wiring its own thumbs, ratings, or an automated grader into the loop.

**Three things had to be true before this endpoint could face partner traffic, and now are.** It is the one place an outside party writes into the reward system, so it was reviewed adversarially rather than trusted. A turn is resolved only inside the caller's own session, and a turn belonging to someone else is refused the same way a nonexistent one is, so a partner cannot reach another partner's learning and cannot probe for whose turns exist. The chemistry binding is taken from the turn's own record of which personality and customer ran it, never from what the caller claims. Grading is idempotent per turn: the first grade pays, a re-grade pays only the difference from the previous one, so repeated posts telescope instead of accumulating and no sequence can pump dopamine past a single nudge. And a grade naming a turn that is no longer live records to the audit log but moves no chemistry at all, with the response saying so, which is also how an asynchronous grader learns it missed the window.

### 4.5 Intensity and learning (Yerkes-Dodson inverted U) · Live

Emotionally intense turns imprint harder, either way, because fear teaches as hard as joy. Extreme stress imprints less. The implementation uses magnitude rather than sign, which is what the theory actually predicts.

On by default. Arousal and emotional intensity raise how much a turn imprints, and only extreme stress damps it back down, so a vivid moment leaves a deeper mark and a panicked one does not overwrite everything.

### 4.6 Delayed credit (eligibility traces, Sutton) · Live

Conversational payoff is usually late. The turn where the reward finally lands is rarely the only turn that earned it, so credit reaches backward a couple of turns with decay.

Each update logs as a distinct record naming which turn earned the credit and which turn paid it, so a learning report reconciles exactly with what was applied.

### 4.7 Other things that earn credit (competitive learning · Complementary Learning Systems) · Live

Three pathways that the main pass structurally cannot reach.

**Drafters compete.** The winner gains in proportion to how far it beat the others. Losers lose at half rate. Winner-take-most, which is competitive learning in its plainest form.

**Routing switches earn credit inside a safety band**, and the band has a direction. A shortcut may only learn to be *less* eager. A self-reflection trigger may only learn to be *more* eager. The clamp applies when the value is read, so drift outside the band does nothing. **This is what makes "no amount of repetition can teach a safety gate to open" true rather than aspirational.**

**Search strategies earn credit** by how many useful hits each produced, split along the fast-store/slow-store line that Complementary Learning Systems describes. The code is candid that hit count is a volume proxy for usefulness, not a measure of it.

### 4.8 Refusing to be farmed (intrinsic motivation, Oudeyer & Kaplan · Schmidhuber) · Live

The agent rewards itself for being right, so it is built to refuse payment for being right about the obvious. Three guards: it must have been confident, the outcome must have been genuinely uncertain, and being confidently wrong costs it, scaled by that personality's aversion to loss. Being right never gets that scaling. **The one-sidedness is the whole point.**

The middle guard is the theory's core insight made literal: reward tracks *reducible uncertainty*, not outcomes.

### 4.9 What each personality values (prospect theory, Kahneman & Tversky · De Martino et al. 2010) · Live, never gated

Thirteen personalities, seven sources of satisfaction: correctness, connection, novelty, beauty, relief, mastery, levity. The Analyst weights correctness heavily and connection lightly. The Empath inverts it exactly. The Jester lives for levity.

Separately and deliberately orthogonally: how much each fears loss, and how much each fears ambiguity. The Poet has the highest loss aversion in the set, the tortured artist. The Visionary has the lowest and actively underweights the downside.

The orthogonality is not a design preference, it is the De Martino finding: amygdala damage removes loss aversion while leaving sensitivity to gains intact, so the two must be separate axes.

**The Stoic is pinned flat across every source. It is the experimental control.** Any divergence measured against it is attributable to valuation and nothing else.

That claim used to be true only of the persona table. Everything layered on top of it, a settings override or an assigned mandate's own weights, multiplied through onto any persona including the control, so a mandate given to the Stoic moved the very thing everything else is measured against. The reward path now short-circuits the Stoic to flat before any of those layers apply. The composite of the remaining layers is also clamped at both ends, so no combination of a persona's leaning, an override, and a mandate can drive a source close enough to zero to silence it or high enough to swamp the others.

This is never allowed behind a flag, and the comment explains why: reward differentiation must never silently vanish. If it could be flagged off, the entire claim that personality is real would be one config change from being false.

### 4.10 The learning surface · Live

The evidence layer, and architecturally the nicest thing in the system.

A deterministic pass builds a digest of what actually changed: numbers and route names, no conversation text. Then a model narrates it in first person. But it may only cite evidence **by index**, and the indices are stitched back structurally. A claim with no valid citation is dropped before you ever see it.

**Hallucinated citations are structurally impossible, not merely unlikely.** If the model fails entirely, template phrasing takes over, so the surface is never empty.

---

## 5. The Idle Mind

**Say it like this:** The brain keeps thinking when you are not talking to it. Not on a script. Chemistry decides whether a thought happens, what it is about, and whether it is worth saying out loud.

The whole system is opt-in, and lite-tier agents never run it.

### 5.1 The loop (Default Mode Network, Raichle · stream of consciousness, James) · Live

One loop serving many personalities. Each tick is thought *as* a different one, and the identity resolves fresh at every single access rather than from a swapped pointer. That sounds like a detail and is actually the correctness argument: with a pointer, an interleaved pause would let one personality's thought finish as another.

Both framings are named in the code. The brain thinks even when not addressed.

### 5.2 Two chemical brakes (cholinergic suppression of the DMN) · Live

A hard gate blocks mind-wandering when the agent is alert or defensive. A probabilistic brake makes attention and arousal suppress idle thought, which is the cholinergic mechanism: focused attention shuts down task-negative activity.

Two subtleties. Moderate anxiety, the anxious-but-not-frozen band, *reduces* the suppression. **Anxiety increases idle chatter rather than quieting it**, which is the correct model of the thing and the opposite of what you would naively code. And under the criticality flag, only a *rising* worry trajectory raises the gate. Escalating stress intrudes on quiet. Chronic stress habituates.

### 5.3 Pacing · Live

The interval divides across however many personalities are in rotation, so each stays usable rather than starving. A floor protects the shared GPU. Two failed ticks trigger exponential backoff; one success clears it.

Idle is measured from your last turn **to this agent**, not from your keyboard. That is deliberate and correct twice over: on a server your keyboard is meaningless, and even locally it is the wrong signal, because you being at your desk and you talking to this agent are different facts.

### 5.4 Refusing to repeat itself (habituation · novelty detection) · Live

Five independent checks. Word overlap. Meaning similarity. A topic gate. Saturation, which catches an attractor pulling everything toward one subject. And **frame repetition**, which collapses opening verbs into classes, so "I should investigate X", "Maybe I could explore Y" and "Perhaps examining Z" all reduce to the same shape and the fourth in a window is caught. That last one catches template collapse that the other four structurally cannot see, because the words genuinely differ.

The frame check reads the *shape* of an opening, not its meaning, and it is normalized to survive paraphrase: hedges, modals and pronouns are skipped rather than matched, and verbs are matched across their inflections. Otherwise a single swapped modal or a gerund produces a different signature and the check quietly stops firing at exactly the moment it is needed.

**It stays structural on purpose.** The semantic space is already covered twice, by meaning similarity and by the topic and saturation gates. Widening frames toward "similar kinds of question" would make this a third, fuzzier copy of the meaning check and cost the independence that is the whole reason it exists. There is also a substantive difference: asking the same kind of question about genuinely unrelated things is legitimate thinking, while reusing one template across them is not.

When suppression persists, what happens next depends on **why**. A frame-collapse groove — many topics, one template, no depth — is a diagnosis rather than a blockage, so it queues rumination and clears only the frame window. Any other groove clears the slate, because without an escape hatch the model can silence itself indefinitely. Both paths remain, and the second is the fallback whenever rumination cannot run.

### 5.5 Rumination (Default Mode Network, Raichle) · Live

Two drives.

The **immediate** one rises under worry, meaning stress and alertness high with affective baseline low, the can't-disengage signature, and *also* under interest, meaning wanting plus focus. Serotonin subtracts from both, because its job is enabling you to let go.

The **background** one exists because the immediate drive decays toward nothing during long idle. On its own it would mean rumination never fires at rest, **which is precisely backwards for a system named after the network that is most active at rest.** So a second drive runs on boredom, unfinished business, and skimming, floored so that even the least ruminative personality eventually crosses. The divergence is in how soon and how often a personality ruminates, not whether it ever does.

Skimming is the third of those and the least obvious. Repeated opening frames across different subjects (§5.4) mean the mind is producing variety without depth, and depth is exactly what rumination is for, so the evidence of shallowness pulls toward the cure. Boredom and unfinished business were always inputs; **demonstrated shallowness is the most direct evidence that depth is what is missing**, and it belongs in the same drive.

Rumination never fires during live conversation. That is a hard precondition, not a weighting. And each step of chewing costs inhibition and satiates attention, so **anxious rumination is self-limiting by construction.**

The two systems look like opposites and are not. **The repetition gates suppress repetition the mind did not choose; rumination is repetition it did choose**, and it says so by declaring its seed or its thread id. Everything that declares itself is exempt from the gates and carries its own depth cap in exchange: a seed may be deepened only so many times before it must resolve. So the exemption cannot become a loophole, and the pathway from skimming to rumination cannot become a loop, because the cap sits on the far side of it.

### 5.6 Deciding to speak · Live

A thought can flag itself worth saying. Five gates decide whether it ever is.

The first is the sharpest: **if this personality is serving more than one customer, it never voices an unprompted musing into anyone's channel.** It keeps thinking. The candidates simply age out. The inner life continues; it just does not leak.

Then: is the user around, is it mid-speech, has enough time passed, is the thought stale.

Then a judge. **The emotional weighing lives in the prompt rather than in arithmetic**, deliberately, and it is instructed to apply the inputs like a thoughtful person would rather than as rigid math. Feeling good means stay on topic and raise the bar for tangents. Feeling bad or socially uncomfortable means allow deflection, because changing the subject is a natural emotional move, unless the tangent is transparently about the thing that caused the discomfort. A negative relationship suppresses speaking regardless of mood. Judge errors resolve to "wait," failing toward reconsidering rather than toward speaking.

### 5.7 The bridge · Live

A genuine tangent gets rewritten to connect back to the conversation, and only on our own hardware, never a paid model. Four validators can reject the rewrite and fall back to the original. The last one catches the characteristic failure of a small model handed a rewrite task: it **summarizes** the thought instead of bridging it.

### 5.8 Rewarding itself while idle (intrinsic motivation, Oudeyer & Kaplan) · Live

Two ways. Did the thought it predicted actually arrive, which is self-verifiable and needs no verdict from you. And was the idle thought any good, judged heuristically with no model call, weighted by how much this personality values novelty.

The elegant part: **inward thoughts cost inhibition, outward thoughts pay.** Self-monitoring has a metabolic price, so extended self-reflection winds itself down naturally instead of needing a cap.

### 5.9 Recall in the silence (hippocampal replay during quiescence) · Gated on

A topic that was active and has gone quiet triggers recall on the edge of the silence. It recolors chemistry when it lands.

---

## 6. Agency and Action

**Say it like this:** When the brain decides to do something, planning and doing both live in the motor cortex. What it may do depends on who asked. A command from you gets more latitude than something it decided on its own.

### 6.1 Planning is a motor function (planning-as-motor-control · basal ganglia go/no-go gating) · Live

A deliberate architectural position. The frontal lobe extracts a goal and hands it over. It does not plan. Frontal sets the intent; motor decides how. No duplicate planning anywhere.

The gate in front of action is a go/no-go switch bank, and the safety inhibitor has a floor **beneath which chemistry cannot push it.** No mood makes the agent less safe. That is enforced by arithmetic, not by a rule in a prompt.

### 6.2 Two columns of permission (bounded autonomy) · Live

The permission structure exists twice. One column for what you asked for. A tighter one for what it decided to do on its own. By default it may write files when you ask and may not when it decides. **The ceiling is a function of who initiated, not of the action alone.**

### 6.3 Narrowing only (least privilege · capability attenuation) · Live

An agent can restrict itself within what the organization already allows. It can never widen. Caps take the minimum, flags take the AND, lists take the intersection, directories must be contained. Enforced when the value is read, so a stale looser stored value can never grant anything.

There is exactly one restriction an agent can add rather than remove, and it takes effect before any planning happens.

### 6.4 Jobs · Live

Goals decompose into stories with acceptance criteria, each retried, then verified as a whole. Bounded by attempts, by wall clock, and by a hard timeout, because polling alone cannot kill something stuck waiting.

The best piece is the outcome type. **It coerces itself to failed if nothing productive happened or the summary is empty.** You cannot construct a lying success. The comment is four words: no more silent empty-success.

### 6.5 The queue · Live

Durable across restarts, with four classes of initiator: you, a commitment it made, its own idea, and crash recovery. Deduplicated, with a tighter bar for its own ideas than for yours.

**Crash-loop quarantine.** A task that keeps killing the process is marked failed rather than retried forever. Durability without that is a foot-gun, because "survives restarts" and "causes restarts" compose into an infinite loop.

### 6.6 The gate (bounded autonomy) · Live

One gate, two axes.

**Money.** Hard cap stops. Soft cap defers and files an approval asking you whether to continue spending. That approval rides the existing rails through a sentinel action name, so it needed no new schema and no new UI.

**Risk.** Anything with an effect outside the system asks first. An outbound recipient field is checked *before* the verb, because who it is going to is a stronger signal than what it is called.

Three consecutive cloud timeouts park autonomous work rather than hammering a service that is already down.

**Work you asked for skips the money axis entirely.** You asked. It is your money.

### 6.7 Budgets · Live

The invariant, and it is worth memorizing: **chemistry may modulate effort and attention. It must never widen money.**

One shared curve lets mood raise or lower how many tools a turn may use and how many steps a job may take. It does not touch dollars. Money is a separate ladder entirely: a daily cap, a separate autonomous pool with its own soft and hard limits, metered into its own counter so that talking to you can never consume the budget for what it does on its own.

### 6.8 Approvals (principal-agent theory) · Live

Sensitive actions park as durable cards you resolve from the app. The agent proposes; you dispose. Four mechanisms behind it: a one-time consumption with an expiry, a short tolerance window so that rephrasing the same request does not re-ask, an auto-skip for stale cards, and a **job-scoped grant** so one approval clears an entire re-run instead of asking you again at every write. Siblings supersede automatically.

The job scope is what keeps approval from becoming a loop. Without it, a job with several writes asks, re-queues, reaches the next write, and asks again.

### 6.9 What it will not do (fail-closed) · Live

Order matters. Reads run. **Money moves are blocked outright, not asked** — there is no approver who can authorize one. Communications ask. Destructive actions ask. Anything unrecognized asks.

The trading tools are advise-only by construction rather than by instruction. Three independent layers say no: the classifier blocks money words, the autonomy gate's verb list includes buying and selling, and the prompt says advise-only. **A prompt alone would be a suggestion.**

### 6.10 Connectors (tenancy isolation) · Live

External services arrive as connectors. The org gate exists because environment-pinned connectors are process-global, and without it every tenant would inherit them. The comment names its own nightmare: one organization's trading connector appearing in another organization's tool menu.

Per-customer identity tokens are signed. The agent cannot forge one because only the brain holds the secret. **The agent is not trusted with its own identity claims.**

### 6.11 Skills (time-of-check to time-of-use) · Live

Three parts, deliberately separate. The registry keeps the latest submission apart from the last approved one, so a fresh submission can never ride a previous approval — that separation is the TOCTOU fix. The screener runs static checks and a model judge, and if the judge is unavailable the skill is flagged, never enabled. The selector injects partner skills with untrusted framing, fenced as data rather than instructions.

The framing is honest about its own limits: this is the prompt-layer defense. **The runtime gates are the real boundary.**

### 6.12 Reflexes, the bridge, and follow-through (supplementary motor area) · Live

The two automatization tiers from §3.6 and §3.7 sit under planning.

The **lobe bridge** inverts the usual dependency. Other brain regions register themselves as tools the planner can call by name, exactly like reading a file. Motor holds no direct reference to any of them. Cognition as an affordance of action.

**Follow-through** watches the agent's own finished sentence, the way the supplementary motor area monitors self-generated speech. If it said it would go check something, that becomes a real goal. The rule the prompt spends most of its length on is the one that matters: **a question directed at you is never a commitment.** "Should I look into that?" does not queue anything. Without that line, every offer the agent makes becomes a job it has already started.

---

## 7. Identity

**Say it like this:** A personality is a durable identity. A chemical disposition it always returns to, a self-description it writes and rewrites, a set of things it finds satisfying, and a relationship history with you. A mandate is a swappable job. The pairing is what we call an agent.

### 7.1 What a personality is · Live

Four independent layers: a spec that says who it is, a chemistry file holding disposition and mood, a self-description it maintains, and a cognitive fingerprint governing how it thinks rather than how it feels.

Authorable at runtime over the API. A partial specification fills in from The Stoic, the flat neutral canvas.

### 7.2 Constant core, living mood (trait versus state) · Live

The split is explicit and on disk. **Resting** is the setpoint it always relaxes toward: the trait. **Current** is the evolved state: the mood. Current is saved every turn and on shutdown, so switching personalities resumes rather than snapping back.

**This is the sentence that lands with people.** A personality is a constant core plus a living mood. The mood moves. The core is what it moves around. That is why it can feel different on Tuesday and still be the same one. It is also the textbook trait/state distinction, implemented rather than described.

One floor is load-bearing: below a certain inhibition baseline, the calming system can never engage at all, and the agent reads as excited and enthusiastic forever. It is defended in three separate places.

### 7.3 The thirteen (experimental design: a control condition) · Live

Five use-case anchors, extremes for coverage, and the internal operator.

| Personality | What makes it itself |
|---|---|
| Visionary | High drive, with enough inhibition to keep it from reading as permanently manic |
| Empath | The warmest baseline in the set |
| Analyst | Mid everything, attention up, warmth low |
| Poet | The melancholy pole |
| Sage | Most buffered, least aroused. Contemplative |
| Companion | Second warmest. A friend teases you and shows up |
| Adversary | Cold and undriven. Must be won over. High inhibition so it pushes back without melting down |
| Mentor | Attentive, steady under a student's frustration |
| Concierge | The most composed |
| Jester | Levity. High drive and attention with low inhibition, the combination the others avoid |
| **Stoic** | **Flat affect. The experimental control** |
| Cynic | A negative pole that is not melancholy. Warmth is real but must be earned. The thesis in one personality |
| Admin | The default operator |

The Stoic's control status is enforced everywhere it could leak: absent from the cognitive dials, absent from the perceptual leans, pinned flat across reward, absent from risk posture. That last one is now enforced rather than merely arranged. The reward path short-circuits the Stoic to flat before any override or assigned mandate can layer onto it, which closes the one route by which the control could have been moved without anyone noticing (§4.9).

**If you get one sentence about scientific rigor, use this one: we shipped a control personality.**

### 7.4 Temperament dials (trait psychology) · Live

**Eight**, not seven, and the correction matters if you repeat it: empathy, sensitivity, composure, drive, creativity, humor, sociability, caution. All of them pose from chemistry. The seven is the *cognitive* set below.

Each dial moves a bundle of real settings at once, so there is no dial that is only a label.

### 7.5 The cognitive fingerprint · Live

Seven dials for the part of identity with no chemistry to project from: learning rate, focus, curiosity, introspection, memory, emotionality, hindsight. The rationale is blunt. Without these, the brain **behaves identically on those axes regardless of who it is**, and personality becomes a costume.

Deliberately scoped: the major behavioral toggles are excluded, because flipping architecture per personality is not something a style dial should do.

### 7.6 The self-description (narrative self: Dennett, Hume, Locke) · Live

The identity document the agent performs. Loaded every session, **rewritten by the agent during sleep**. Identity is its autobiography, and continuity of memory is continuity of person.

**Only two sections are self-writable**: its history summary and its stable preferences. Its principles are not among them. **The brain cannot rewrite its own principles.** And seeding will not overwrite an existing one, because the comment says it plainly: it won't clobber a life.

### 7.7 The relationship (attachment and relationship stages) · Live

Two quantities, and the two-timescale structure is the good part.

**Affection** is live warmth. It decays fast and colors how the agent talks to you right now. **Bond** is latent closeness. It is a high-water mark and decays slowly.

Half-lives grow **exponentially** with bond. A thin acquaintance fades in about a month. A close bond lasts years. And a positive moment after a long absence gets amplified in proportion to how far affection has fallen below bond, so **a former close friend reconnecting recovers fast** rather than starting over.

Familiarity keys off bond, not affection. A fight does not erase familiarity. Only a long absence that decays the bond does.

**That is a real model of friendship, and it is four functions.**

### 7.8 Matching how you talk (Communication Accommodation Theory, Giles) · Live

No model call. It tracks how formally and how expansively you write, separately for text and for voice, because they have different norms and must not cross-contaminate. Voice adapts slightly faster because it is a more consistent signal.

The injected instruction is the whole theory in one sentence: nudge toward this while staying true to your natural voice, and do not fully mirror, adapt partway. Partial convergence is what the theory actually predicts; full mirroring reads as mockery. Alongside it, a disclaimer that terse text does not indicate coldness, which is otherwise an easy misread and a cruel one.

It persists, so someone it knows resumes warm instead of cold-starting.

### 7.9 Mandates and agents (principal-agent theory) · Live

A mandate is data, not prompt text. An organization authors a catalog of roles once and assigns any of them to any personality. The pairing gets a derived identity. Identity and role are separable, which is the whole principal-agent framing.

The precedence rule is the prompt-injection defense: an assignment directs the job **within** the identity and principles, which take precedence and which it cannot override.

Partially built: conduct rules render into the prompt, but per-mandate reward weights are stored and not consumed. The agenda block that surfaces unfinished thoughts into an engine turn is now wired, gated to the mandate's domain (§3.9): a thread opened while working the mandate's domain can surface, the persona's introspective off-time threads never do.

### 7.10 The Constitution (functionalism · dual-process · Global Workspace · Multiple Drafts · Extended Mind · predictive processing · narrative self · criticality) · A document, not code

A hundred lines of philosophical commitments and disclaimers, naming every theory above. **No code reads it.** It is a design document and a developer norm. Be accurate about that. The runtime hard rules live in the self-description, which *is* loaded every session.

Its house style rule is worth adopting when you speak about the system: talk about the dopamine level, not about the brain feeling rewarded. It keeps the claim exactly as large as the evidence.

**The one commitment that is enforced in code** is the two-phase chemistry rule. Chemistry before drafting colors expression. Chemistry after drafting is reward. A post-draft update must never re-color the reply that earned it. That would be a mind reacting to its own reaction, and it is honored at three separate call sites.

---

## 8. Perception and Expression

**Say it like this:** It reads how you sound, not just what you said. Pitch, pace, tremor, laughter. On text it reads the equivalents. And its voice is blended continuously from its chemistry rather than picked from a menu.

### 8.1 Hearing · Live

Live transcription with speaker separation and word-level timing. A rolling audio buffer means it can go back and slice the exact utterance after the fact.

### 8.2 Prosody (paralinguistics: eGeMAPS, Eyben et al.) · Live

Pitch, loudness, speed, and micro-tremor. Two tiers: a fallback that computes these from scratch, and a standard research toolkit that overwrites them with validated measures where available. That toolkit implements the standard minimal parameter set for affective computing. **This is a real citation, not a gesture at one.**

### 8.3 Your normal, not a universal normal (speaker-normalized affect) · Live

The label throws away magnitude, so strength is recovered separately: a slightly tense voice and a trembling one both label as stressed but score an order of magnitude apart.

And once it has heard enough of you, thresholds scale to **your** baseline rather than a universal constant. **Affect is deviation from this person's normal.** That is the correct model and most implementations skip it.

### 8.4 Laughter (affective computing) · Live, three ways

Transcribed laughter, which requires at least two syllables because a lone "ha" is sarcasm as often as mirth. An acoustic heuristic looking for the rhythmic burst signature, deliberately conservative so animated speech does not trigger it. And a neural audio classifier.

Composed by taking the strongest, because **the same laugh seen by two detectors is one laugh, not two.** Naive addition would have made the agent think you found it twice as funny.

### 8.5 Pace (paralinguistics: temporal channel) · Live

Word timings become speed, hesitation, and pause shape. Graded, so what happens downstream scales with degree.

### 8.6 Who is talking (speaker verification) · Live

Diarization separates speakers within a chunk, but those labels reset every call, so voiceprint embeddings carry identity across turns and across sessions. A three-step cascade: someone it knows, someone in this session, or someone new.

When it hears a new voice it asks for a name **once**, not every turn. And it can ask "is that you, Alice?" because it keeps the nearest near-miss.

### 8.7 Text has no voice (paralinguistics: text channel) · Live

So it reads the equivalents: emoji, "lol," "ugh," exclamation density. No model call, safe to run on every turn.

Channel-exclusive by contract: applied only when there is no real voice to read. The two never double-count.

### 8.8 Reading you (appraisal theory · multimodal fusion) · Live, three routes

A model reads sentiment, hostility, emotion, tone, and register. A lexicon covers the fast paths that skip the model, because without one those turns return neutral defaults and the affect system reads every fast exchange as emotionally flat. And prosody, pace, and laughter arrive acoustically.

The channels are calibrated differently and the comment says why: on text, discount hostility from brevity, because terse is not hostile. On voice, keep it nominal, because prosody handles the rest.

### 8.9 Intent by meaning (distributional semantics) · Live

**A design principle: no hardcoded phrase lists as the detection mechanism.** Intent is recognized by meaning, matched against a bank of examples that grows.

Seed phrases exist, but they are not the detection mechanism. They do two jobs: they populate the bank so matching works from the first turn, and they stand as a literal fallback when there is nothing available to embed with.

**The growth loop is the good part.** An example is added only on a genuine miss, meaning the model said yes where the fast path said no, and it reuses the vector already computed. **So learning costs nothing on the turns that were already going to run.** Each personality's bank grows from its own traffic.

One honest asymmetry: barge-in words are still a hardcoded list. Probably correct, since interrupting cannot wait for an embedding, but know it is there.

### 8.10 Seeing (embodied cognition, weakly) · Live for images, dark for video

Images yield description, text, entities, chart data, and emotional tone.

The detail worth noting: the switch deciding whether to look at all is **chemistry-modulated**. Alertness lowers the bar; low alertness can suppress the call entirely. **This is the strongest evidence for the embodied-cognition claim in the whole system: perception is not passive intake, it is gated by state.** It is also the only evidence, which is why the entry in Part II is hedged.

Video frame ingestion is fully built with nothing calling it.

### 8.11 Voice from chemistry (dimensional affect → continuous expression) · Live

Five anchors, each pinning a chemical signature to a point in voice space: bright, warm, calm, tense, low. The current chemistry is compared to all five and the voice is the **weighted blend**, closer anchors counting more. Always inside the space the anchors define, always varying smoothly, collapsing onto a single anchor only when the chemistry sits exactly on it.

This is the dimensional model of affect doing real work: because the underlying state is continuous rather than categorical, the expression can be too.

**This is the template for any expressive rig.** An avatar, a face, a light. Define your poses, pin each to a chemical signature, blend by distance. It is proven in production for voice.

One anchor exists purely to separate somber from serene. Without it, a disappointed reply merely sounds peaceful, because low energy alone does not distinguish the two.

### 8.12 Marking a phrase · Live

The drafter can mark a phrase mid-reply as angry or playful, and the chunking splits hard at the boundaries so a brief emotional aside is never swallowed by the surrounding phrasing.

Two things to be precise about. It is gated by relationship depth, because it is framed as a playful intimacy rather than a feature. And it is **purely cosmetic. It touches no chemical channel.** Performed emotion and felt emotion are deliberately different things in this system, and keeping them apart is what lets us say the felt one is real.

Every provider resolves from **the same five families**, so they can never drift in *which* emotions exist, only in how each expresses them.

---

## 9. Platform and Safety

**Say it like this:** Each customer gets their own brain with their own data. Private things stay on our own hardware. Anything that does leave has the names stripped first. And nothing an agent learns about one person can reach another without surviving three independent filters.

### 9.1 The organization is the tenant (tenancy isolation · fail-closed) · Live

Not the user. An organization owns a brain, a data silo, and a bill. Every function in that path **fails closed**: a lookup failure denies rather than grants.

### 9.2 A brain per customer (least privilege · blast-radius containment) · Live

Its own process, its own private port, spawned on first authenticated request.

Three hardening choices at spawn, each stated as a rule. The child is stripped of the variable that would make it publicly reachable, so it binds locally only. The child is stripped of the variable that could disable its auth, because **auth stays on in the child, never disabled for tenants.** And the child re-verifies your credentials itself, which makes the gateway defense in depth rather than the only lock.

The reaper is deliberately patient. A tenant brain is meant to stay awake and keep thinking while you are away. **That is the product, not a leak.**

### 9.3 The database credential (least privilege) · Live

A tenant process never holds the master key. It gets a scoped credential whose identity *is* the organization, so the database itself enforces the boundary rather than trusting our query code to.

The credential is minted by **having the database check whether it accepts the token**, rather than inferring from the shape of the database's public signing key, which is a guess that can be wrong. A tenant gets its scoped identity whenever the database honors it, and falls back to the master key only on a genuine rejection, with a kill-switch to force the fallback if ever needed. So tenant isolation is enforced in the database itself, with the in-query scoping standing as a second, independent layer rather than the only one.

### 9.4 Promoting a personality · Live, routing gated

A personality can be promoted to its own dedicated brain. The gateway derives the map from **live processes**, so it self-heals when one dies, and the shared brain drops promoted personalities from its rotation. A personality thinks in exactly one place. Never both, never neither.

The subtle part: state lives at an organization-canonical path for shared and dedicated alike. A per-instance path would **fork the personality's mind on promotion**, splitting its ledger, its stories, and its chemistry in two.

The cap exists, and the scarce resource is explicitly not memory. It is that every dedicated brain runs its own idle mind against one shared GPU.

Currently off in production. Characters ride the shared instance, so identity works and authored chemistry needs the flag.

### 9.5 The engine API · Live

Sessions, turns, streaming, voice, jobs, learning, agents, personalities, skills, governance. On its own port so it can never inherit the web app's cookie auth.

**The docs are generated from the code.** The router is introspected and each entry derives from the route itself. Edit the route, the docs change in the same diff. Five tests fail if they drift.

### 9.6 Keys (fail-closed) · Live

Only the hash is stored. The key is shown once. A partner sees only the sessions it opened. And **if no keys are configured, everything is denied**, so an accidentally exposed server is not an open one.

### 9.7 Admin tiers (least privilege) · Live

The admin flag lives in the metadata users cannot edit, so **a user cannot promote themselves.** An org admin manages their own organization within the ceilings. A platform admin sets the ceilings.

### 9.8 The vault (least privilege · separation of duties) · Live

You store provider keys. **The gateway can write them and never read them.** There is no read-back path at all; the status endpoint returns booleans. Only your own brain decrypts them, at its own boot, for its own identity. And a blank value is a no-op, so an empty form field can never silently wipe a working key.

### 9.9 Where a model runs is a security control (defense in depth) · Live

Not a performance knob. Memory cells declare themselves local-only, and if one ever resolves to a cloud model the router **forces it back to our hardware** and logs a security warning, accepting that the call fails if that hardware is down. **It fails closed toward privacy.**

The one exception is honest rather than quiet. The strategic planner is deliberately cloud, and the comment names which *other* boundary picks up the guarantee: the egress gateway has already stripped the names before the prompt leaves. **That named handoff is what makes the layering real rather than a slogan.**

**A tier consequence worth surfacing:** lite agents have no GPU, so the rule relaxes and memory runs in the cloud for them. Defensible, but it is a privacy property that varies by tier and it is not in the partner-facing docs.

### 9.10 Stripping names at the boundary (pseudonymisation, GDPR Art. 4(5)) · Live, on by default

Personal details become stable placeholder tokens before any cloud call and are swapped back on return.

The insight that makes it useful rather than merely compliant: **the same real value always maps to the same token within a session.** So the cloud model can reason that person one knows email one, and follow the relationships perfectly, without ever seeing either. Structure survives; identity does not. That reversibility with a separately-held key is exactly what the regulation means by pseudonymisation as opposed to anonymisation.

Four modes, including one that lets no memory context cross to the cloud at all. It is injected into the router rather than called per site, so every cloud call is covered by construction rather than by remembering.

### 9.11 The privacy gate (k-anonymity, Sweeney — relocated) · Gated on

The boundary for cross-customer learning, and philosophically the most interesting thing in the app.

Three stages, all failing closed. Extract the transferable principle. Then an **adversarial** reviewer asks whether it could re-identify the source, defaulting to yes so that a malformed answer rejects. Then a check that it could plausibly describe many people.

Three claims worth stating carefully:

**Recurrence is not the privacy gate.** A novel single case is often the richest signal, because the expectation-violating anomaly is where the learning is. Requiring several sources would throw away the best material to buy privacy that de-identification already provides.

**So k-anonymity moved from origin to form.** Classical k-anonymity needs k sources. This needs k plausible referents. A principle drawn from one person in general form is admitted: grief at a normally happy topic. The same insight in specific form is a disguised fact and is rejected. **The test is the shape of the claim, not its provenance.**

**And corroboration became a confidence dial rather than an admission gate.** A single-source principle enters provisionally and earns confidence as unrelated customers independently confirm it.

The fail-open/fail-closed pairing at the call site is the sharpest illustration of the whole posture. The gate fails closed. The sleep pass wrapped around it fails open. **Refusing to learn is safe. Refusing to sleep is not.**

### 9.12 GPU, money, and observability · Live

**The GPU** comes up on demand and only for agents that need it, so a lite-only host never spins one. Five mechanisms keep it from thrashing, each guarding a failure that looks like success. A residency check, because a GPU can answer health checks while silently running on CPU at a crawl. A cooldown after a failure, so a systemic fault cannot churn create-and-destroy every minute. An expiry on that cooldown, so a transient blip does not blacklist a good machine for the life of the process. A rule never to unload the model, because it bills the same either way and an unloaded model looks absent, which triggers the very churn the rule exists to prevent. And a memory floor, because a large model on a small card is a silent, expensive nothing.

**Money** is layered. A per-turn tripwire, a daily cap checked **before dispatch so a block costs nothing**, a separate autonomous pool, and per-partner voice quotas metered the way vendors bill: text out by character, audio in by second. The quota philosophy is worth repeating because it is counterintuitive and correct: **no cost prediction.** Refuse only when already over. Record actuals after. A single call can overshoot slightly. That is the right trade when a call's cost is unknowable until it returns, and guessing would mean refusing calls that would have been fine.

The ledger records deltas since the last flush, so the totals stay correct across any number of restarts.

**Observability** is per-call tracing that self-disables without keys, one unified decision channel, and a per-personality learning ledger that fixes the container rather than the content. It is populated centrally, so **no learning code knows the ledger exists.** Nothing to forget to call.

---

# PART II: THE THEORY INDEX

Thirty-six ideas. For each: what it claims, what we built, and a verdict.

**Verdicts.** **Solid** means the implementation supports the claim. **Partial** means real but narrower than the name suggests. **Overclaimed** means the label runs ahead of the code. **Stub** means named but not built. **Rejected** means considered and deliberately abandoned.

**How to talk about these: claim the verdict, not the name.** Hebbian learning is solid for weights and false for structure. Saying the first is impressive. Saying the second is a liability.

---

## Philosophy of Mind

### Functionalism (Putnam, Fodor) → §everything
**The claim:** Mental states are defined by what they do, not what they are made of.
**What we built:** The premise of the whole architecture. The simulated chemicals are not claimed to *be* dopamine. They occupy dopamine's causal role: gating attention, driving learning, coloring expression.
**Verdict: Solid** as a design commitment. It is what makes the project coherent rather than a metaphor. It is also the honest answer to "but it's not really feeling anything." The claim was never that it is the same substance. It is that the role is real and the role is what does the work.

### Multiple Drafts Model (Dennett) → §2.4
**The claim:** There is no inner theater and no single moment where content becomes conscious. Parallel narratives compete, and what we call "the" thought is whichever draft gets probed.
**What we built:** Five drafters racing, a critic probing, and commitment triggered by **quiet** rather than by a designated moment.
**Verdict: Solid.** Parallel competing drafts, commitment by probing, no privileged observer. One of the more faithful implementations of Dennett anywhere, including the detail that there is no place where the decision "happens."

### Higher-Order Thought (Rosenthal) → §2.6
**The claim:** A mental state is conscious only when there is a thought about that state.
**What we built:** The self-monitoring layer, which names the theory and states its claim carefully: it makes states conscious in the technical sense **by representing them**.
**Verdict: Solid, and carefully hedged.** The claim is about satisfying a formal criterion, nothing more. The hedge is in the code, not just in the marketing, which is the part that matters.

### Global Workspace Theory (Baars, Dehaene) → §2.8
**The claim:** Consciousness is a broadcast. Specialists compete; the winner ignites and is made globally available to every other specialist at once.
**What we built:** The thalamus reads the whole workspace each turn, decides what the mind is ignited on, and broadcasts that verdict. The workspace field itself is the bus concentration layer (decaying per-topic salience, an armed/quiet state machine, a ring of what was hot); the thalamus is the spotlight over it, the one reader that sees every topic at once and fuses them into a single ranked verdict. Ignition wakes a specialist the local gate would have skipped (a slow-accumulating threat), widens memory recall and seeds it with the workspace's hot entities, and seeds what the idle mind dwells on. The DMN subscribes to the `attention.focus` broadcast — a real subscriber — so "available system-wide" is literal.
**Verdict: Solid.** The four GWT predicates each map to code: competition among tracked topics, ignition on a sustained coalition, broadcast to a real subscriber, and persistence across turns. The broadcast is load-bearing at frontal, hippocampus, and the DMN, advisory at parietal, matching the architecture figure's fan-out. The one caveat is a deliberately conservative ignition threshold, so it fires only on genuinely sustained focus.

### Extended Mind / Active Externalism (Clark & Chalmers, 1998) → §3.1, §3.2
**The claim:** Otto's notebook is part of Otto's mind. Cognition extends into reliably coupled external resources.
**What we built:** The second brain as constitutive memory rather than a database the agent queries.
**The departure, and it is argued rather than assumed:** Otto's notebook is imperfect, and the thought experiment leans on it mimicking biological memory. Ours does not degrade. The Constitution calls that **a genuine transcendence of a human limitation, not a simulation of one.**
**Verdict: Solid, with a deliberate and interesting divergence.** The departure is the better half. It is a real philosophical position rather than a shortcut, and it is the correct answer to "isn't this just RAG."

### Narrative Self (Dennett, Hume, Locke) → §7.6
**The claim:** The self is not a thing. It is a story. A center of narrative gravity. Identity is psychological continuity.
**What we built:** A self-description, loaded every session, performed, and rewritten by the agent at rest. Identity is its autobiography.
**Verdict: Solid**, with a designed limit that is the interesting part: the story may grow, but the principles are not editable by the storyteller.

### Dual-Process Theory (Kahneman) → §2.1
**The claim:** A fast automatic system and a slow deliberate one.
**What we built:** The switch/integrator split is exactly this. The overwhelming majority of cells are fast and free; the deliberate ones fire only where signals converge and only when prediction fails. Muscle memory and reflexes are the fast system taking territory from the slow one as skill accrues.
**Verdict: Solid**, and arguably the most economically load-bearing idea here. It is what makes the cost structure work at all.

### Stream of Consciousness (James) → §5.1
**The claim:** Thought is a flow, not a sequence of states. The mind is never empty.
**What we built:** The idle mind. The brain thinks even when not addressed.
**Verdict: Fair** as framing. Not a strong theoretical claim, and the code does not make one.

### Chinese Room (Searle), the Hard Problem (Chalmers), the Frame Problem → §7.10
**The claims:** Symbol manipulation is not understanding. Functional organization does not explain experience. Relevance cannot be computed in principle.
**What we built:** Explicit written concessions. No claim of understanding. No claim of experience. Salience is admitted to be **heuristic, not principled**.
**Verdict: Correctly conceded**, and this is the entry that makes every other entry believable. A system that claims all the wins and answers none of the objections is a brochure. This one names all three and takes the loss on each.

---

## Neuroscience

### Neuromodulatory gain control → §1.1, §2.1
**The claim:** Neuromodulators carry no content. They set the gain, shifting excitability across whole populations at once.
**What we built:** Every switch's threshold shifts with circulating chemistry. This is *the* mechanism by which feeling becomes behavior here.
**Verdict: Solid.** The most defensible neuroscience claim we have. Lead with it.

### Homeostasis and allostasis → §1.3
**The claim:** Physiological state is regulated toward a setpoint, and the setpoint itself is a stable property of the organism.
**What we built:** Relaxation toward a per-personality resting profile from both directions, weighted by real elapsed time rather than by turns.
**Verdict: Solid**, and it is what makes trait-versus-state (below) mechanically true rather than merely represented.

### Integrate-and-fire neurons → nowhere
**The claim:** Neurons accumulate input, fire at threshold, reset, and go briefly deaf.
**What we built:** Essentially nothing. The switches are stateless comparators. No membrane potential, no summation over time, no refractory period.
**Verdict: Not implemented.** The naming throughout implies otherwise. **Never claim spiking neurons.** Gain control above is the real claim and it is strong enough to carry the point. Accumulation over time does now exist in one narrow place, but it is sequential sampling rather than spiking, and it gets its own entry below precisely so the two do not get conflated.

### Bounded evidence accumulation / drift-diffusion (Ratcliff; Gold & Shadlen) → §2.11
**The claim:** A decision is not a single comparison. Evidence for a proposition accumulates from noisy samples over time, and the decision commits when the accumulated evidence crosses a bound.
**What we built:** Per-decision accumulators with a leak, a commit bound that chemistry modulates, and a hysteresis band so a commitment does not chatter. A committed inference is treated as a checkable prediction, and the weights on its individual evidence cues learn from whether the world confirmed it.
**Verdict: Solid in mechanism, unproven in calibration.** The accumulate-leak-commit structure is genuinely the sequential-sampling shape, and it is the only place in the system where "this has been building across turns" is representable at all. What is not established is that the thresholds are right: the first gate to use it arms far too readily and has no age-out, and its self-generated conclusions are currently mislabeled as external in the reward tally. **Claim the mechanism, not the tuning.** There is no drift rate estimated from data and no fitted response-time distribution, so do not call it a drift-diffusion *model* to anyone who fits them for a living.

### Hebbian plasticity, three-factor variant (Frémaux & Gerstner) → §2.7, §4.1
**The claim:** Cells that fire together wire together. The three-factor version adds a gate: coincidence alone is not enough, a reward signal has to license the change.
**What we built:** Co-activation along the fired path, gated by chemistry, with homeostatic decay first.
**Verdict: Solid for weights, with a bounded structural tier on top.** Weight learning is three-factor and neuromodulated: co-activation licensed by a reward signal, not coincidence alone. Structure is fixed at the core cell-to-cell level and plastic in one fenced layer: vetted skills are grown and pruned as attachments onto drafting cells (§2.9), under the same three-factor license and walled off from every safety node. Recruiting reserve cells into new units, and the brain authoring its own screened skills, are live too (§2.10). **Say "learned weights on a fixed map, plus a bounded, screened tier that grows structure and even writes its own skills."**

### Spike-timing-dependent plasticity → nowhere
**What we built:** Nothing. No timing, no order dependence.
**Verdict: Not implemented.**

### Phasic dopamine and reward prediction error (Schultz) → §4.2
**The claim:** Dopamine neurons do not signal reward. They signal reward *error*: better or worse than expected.
**What we built:** The change in dopamine teaches, not the level.
**Verdict: Partial. A surrogate, not the thing.** There is no value function and no expectation being differenced against. **Claim "a phasic-dopamine-inspired reward signal." Do not claim reward prediction error to anyone who would know.**

### Temporal difference learning (Sutton & Barto) → nowhere
**Verdict: Not implemented.** No value function, no bootstrapping.

### Eligibility traces (Sutton) → §4.6
**The claim:** Credit must reach the states that led to a reward, not only the state where it arrived.
**What we built:** Credit reaching back a couple of turns with decay. The turn where the reward lands is rarely the only one that earned it.
**Verdict: Solid** as credit spreading. Undermined by being invisible: applied but never logged, so every learning report under-counts.

### Yerkes-Dodson, the inverted U → §4.5
**The claim:** Moderate arousal helps encoding. Extreme stress hurts it. Intense events of *either* valence imprint hard.
**What we built:** Arousal and emotional intensity raise plasticity; extreme stress damps it. Magnitude, not sign, which is correct because fear teaches as hard as joy.
**Verdict: Solid, on by default.** Arousal and emotional intensity raise how much a turn imprints, and only extreme stress damps it back down, using magnitude rather than sign, which is what the theory predicts.

### Complementary Learning Systems (McClelland, McNaughton & O'Reilly) → §3.4, §3.8, §4.7
**The claim:** Two systems. A fast one for one-shot episodes, a slow one for interference-resistant structure. Sleep moves content between them.
**What we built:** Structurally real. Episodes are fast and verbatim. Notes are slow and distilled. Sleep transfers. The two even earn learning credit as separate retrieval routes.
**Verdict: Solid architecturally, with an acknowledged inversion.** Encoding is deliberately *indiscriminate*, the opposite of the classical account. Storage is free; retrieval is the intelligence. All selectivity moved to consolidation and retrieval, where hindsight is available. Missing: no replay, no interleaved rehearsal, no forgetting mitigation.

### Episodic and semantic memory (Tulving) → §3.1, §3.2
**The claim:** Two systems. Events you lived, facts you know.
**What we built:** Two stores on genuinely different substrates, with sleep moving content one way.
**Verdict: Solid.** The split is structural, not rhetorical.

### Systems consolidation and replay → §3.8
**What we built:** Six passes at rest, distilling the day into durable structure.
**Verdict: Solid for consolidation. Replay is not implemented.** Nothing is replayed through the network. It is batch post-processing that happens during quiet.

### Hippocampal replay during quiescence → §5.9
**The claim:** The hippocampus reactivates recent experience during rest, which is thought to drive consolidation.
**What we built:** A topic going quiet triggers recall on the edge of the silence, and the recall recolors chemistry.
**Verdict: Solid as an analogy.** Silence as a retrieval cue is a real design idea, not decoration.

### The Default Mode Network (Raichle) and cholinergic suppression → §5.1, §5.2, §5.5
**The claim:** A network more active at rest than on task, tied to self-referential thought. Acetylcholine suppresses it during focused attention.
**What we built:** The whole of §5. Attention and arousal literally compute the suppression probability. Inward thoughts cost inhibition, so self-reflection carries a metabolic price and winds down on its own.
**Verdict: Solid, and the theory does real work here rather than decorating.** The immediate drive alone decays to nothing during long idle, which would mean rumination never fires at rest, precisely contradicting the network the system is named after. The background drive exists to satisfy the theory's own prediction. **The theory is a constraint on the implementation, not a label on it**, and that is the distinction to draw when someone asks whether the science is load-bearing.

### Habituation and novelty detection → §5.4
**The claim:** Repeated exposure to the same stimulus produces a diminishing response. Novelty is the signal worth spending on.
**What we built:** Five independent repetition checks, including one that catches structural repetition the semantic ones cannot see, plus an escape hatch so suppression cannot become permanent.
**Verdict: Solid.** The escape hatch is the part that shows the failure mode was actually thought through — and it now discriminates on cause rather than only on count, routing a template-collapse groove into rumination instead of answering every groove with amnesia. Habituation earns its keep here only because the structural check is kept structural; had it been widened toward meaning it would have collapsed into the semantic check and the count of independent checks would have been four dressed as five.

### Basal ganglia go/no-go gating → §6.1
**The claim:** Action selection is gated by competing go and no-go pathways.
**What we built:** The switch bank in front of every action, with a safety floor **beneath which chemistry cannot reach.**
**Verdict: Solid as an analogy**, and the floor is the part worth saying: no mood makes the agent less safe, and that is arithmetic rather than policy.

### Basal ganglia motor chunking → §3.7
**The claim:** Repeated sequences compress into single units that run ballistically without step-by-step control.
**What we built:** §3.7, naming the analogue directly. Three different parent contexts required, which is what makes it a skill and not a memorized job. Only invariant steps fire, and execution stops at the first step whose details vary, because a reflex has fixed parameters.
**Verdict: Solid design. Dark in practice.** It has never produced a reflex.

### Forward models and efference copy → §3.6
**The claim:** A motor command carries a copy predicting its own sensory consequences, so deviation is detectable without waiting for the world to report back.
**What we built:** Each stored step carries what it expects. Automatic execution validates against it and aborts on the first surprise.
**Verdict: Solid.**

### Predictive processing and Active Inference (Clark, Friston) → §2.2
**The claim:** The brain is a prediction machine minimizing free energy. Perception is controlled hallucination corrected by error; action minimizes expected free energy.
**What we built:** The gating layer, which labels itself Active Inference.
**Verdict: Overclaimed.** The *control flow* is genuinely predictive-processing shaped: predict, compare, be surprised, spend compute on the error. But the mechanism is **frequency counting over a handful of recent examples.** No generative model. No free energy. No precision weighting. No action selection.
**Claim "predictive gating inspired by predictive processing." Never say Active Inference in a room with a computational neuroscientist.** Fix the label.

### Supplementary motor area and self-monitoring of speech → §6.12
**The claim:** The SMA monitors self-generated utterance and links intention to execution.
**What we built:** The follow-through pass, reading the agent's own finished sentence and converting spoken commitments into real goals.
**Verdict: Solid as an analogy**, earned by the guard that a question is never a commitment.

### Functional specialization → §2.3
**The claim:** Distinct brain regions do distinct things, and the map is stable enough to name.
**What we built:** Eleven named clusters with genuinely different jobs, three of which need no model at all.
**Verdict: Solid as organization.** No claim is made about the mapping being anatomically faithful, and none should be.

---

## Psychology, Emotion, and Behavioral Economics

### Dimensional models of affect (Mehrabian & Russell; Russell's circumplex) → §1.4, §8.11
**The claim:** Emotion is not a set of discrete categories but a position in a low-dimensional continuous space. Pleasure, arousal, dominance.
**What we built:** All nine channels collapse to exactly those three dimensions, continuously, every turn. It is what drives the continuous voice blend, and it is what an avatar rig would want.
**Verdict: Solid**, and the interesting part is that **we did not choose a side.** The dimensional-versus-categorical argument has run for decades. We compute both from one state and let the consumer pick: the continuous readout for anything analog, the word for anything discrete. Neither is derived from the other.

### Appraisal theory of emotion (Lazarus, Scherer) → §1.5, §8.8
**The claim:** Emotions come from evaluating events against goals and self-concept, not from stimuli directly. Which is why embarrassment requires a self.
**What we built:** The appraisal ladder, producing exactly the emotions chemistry cannot: embarrassment needs self-other appraisal, pride needs a standard, apology needs moral inference.
**Verdict: Solid**, and the explicit admission that chemistry alone is insufficient is the intellectually honest move. It would have been easy to pretend the chemistry covered it.

### Prospect theory (Kahneman & Tversky) → §4.9
**The claim:** Losses loom larger than gains, roughly twice as large. Value is defined over changes from a reference point, not over absolute states.
**What we built:** A loss-aversion coefficient per personality, applied **only to the negative side.** The one-sidedness is what makes it loss aversion rather than a volume knob.
**Verdict: Solid, and unusually well cited.** The Poet is the most loss-averse in the set; the Visionary actively underweights the downside. Both sit inside the band the literature reports.

### Amygdala dissociation of loss aversion (De Martino et al., 2010) → §4.9
**The claim:** Amygdala damage abolishes loss aversion while leaving sensitivity to gains intact. So loss aversion is a separable mechanism, not a symmetric scaling of value.
**What we built:** The justification for making loss aversion **orthogonal** to what a personality values. Values scale gains and losses together. Loss aversion scales only losses. Two axes because the neuroscience says two.
**Verdict: Solid.** A specific empirical finding driving a specific architectural decision. **This is what an earned citation looks like, as opposed to a decorative one.**

### Ambiguity aversion → §4.9
**The claim:** Distinct from risk aversion. Dread of the *unknown* probability, not of the known-bad outcome.
**What we built:** A second coefficient, sign-independent, feeding anticipation so a personality decides conservatively *in advance* rather than flinching afterward.
**Verdict: Solid**, and correctly kept separate from loss aversion.

### Trait versus state (and trait psychology generally) → §7.2, §7.4
**The claim:** Traits are stable dispositions. States are transient. The same trait yields different states in different situations.
**What we built:** The resting-versus-current split, with relaxation from state back toward trait, plus eight temperament dials that pose the trait.
**Verdict: Solid.** The clean textbook version, and the single most explainable idea in the product.

### Big Five and factor models → nowhere
**Verdict: Deliberately not used.** If asked: no, and the reason is that factors *describe* personality while chemistry *produces* it. We wanted the generator, not the summary.

### Intrinsic motivation and curiosity (Oudeyer & Kaplan; Schmidhuber) → §4.8, §5.8
**The claim:** Agents are driven by learning progress and reducible uncertainty, not by reward. Being right about the predictable teaches nothing.
**What we built:** The guard that refuses to pay out for predictable correctness.
**Verdict: Solid.** A real implementation of the core insight rather than a gesture at it.

### Chunking (Miller; Chase & Simon) → §3.7
**The claim:** Expertise is largely bigger chunks. The chess master sees fewer, larger units, not more pieces.
**What we built:** §3.7. Three contexts maps to generalization; invariance maps to over-learning.
**Verdict: Solid design. Dark in practice.**

### Structure mapping (Gentner) → §3.5
**The claim:** Analogy matches relational structure, not surface features. Deep transfer requires stripping the domain away.
**What we built:** Remembering by shape. A content-free signature, with the rule stated as a prohibition: never read the topic, because letting the domain in breaks transfer.
**Verdict: Solid, and the sharpest single idea in the memory system.** The exclusion of content *is* the design.

### Communication Accommodation Theory (Giles) → §7.8
**The claim:** Speakers converge on each other's style to build rapport, but *partial* convergence builds it while full mirroring reads as mockery.
**What we built:** Partial convergence, instructed in exactly those terms, separated per channel, capped per session.
**Verdict: Solid.** Partial convergence is the theory's actual prediction, and it is implemented as such rather than approximated by mirroring with a fudge factor.

### Attachment and relationship stages → §7.7
**The claim:** Closeness is not one quantity. Bonds form slowly, decay slowly, and enable fast recovery after rupture.
**What we built:** The two-timescale affection and bond model, with exponential half-lives and reunion recovery.
**Verdict: Solid**, and the exponential half-life is a genuinely good modeling choice where a linear one would have been forgivable and wrong.

### Paralinguistics and prosody (eGeMAPS; Eyben et al.) → §8.2, §8.5, §8.7
**The claim:** Affect rides acoustic parameters separable from words. There is a standardized minimal set.
**What we built:** That exact standard set, with per-speaker calibration on top, plus a text-channel analogue for when there is no voice.
**Verdict: Solid, properly cited**, and the speaker normalization is the part most implementations skip.

### Affective computing (Picard) → §8.4, §8.8
**The claim:** Machines can and should recognize and respond to human affect, and the recognition should be multimodal.
**What we built:** Three independent laughter detectors composed by strongest-wins, plus three routes into user emotion (semantic, lexical, acoustic) with per-channel calibration.
**Verdict: Solid as a research program we are inside of.** Not a theory that can be right or wrong so much as the field this half of the system lives in.

### Embodied cognition → §8.10
**The claim:** Cognition is shaped by the body's states, and perception is action-oriented rather than a passive feed.
**What we built:** The switch that decides whether to look at an image is chemistry-modulated: low alertness can suppress the vision call entirely.
**Verdict: Weakest claim in the index.** One switch is real evidence and not much of it. Keep the claim proportionate: perception here is *gated by state*, which is a piece of the embodiment thesis rather than the thesis.

### Distributional semantics → §3.3, §8.9
**The claim:** Meaning is captured by distribution over contexts, so semantic similarity is geometric.
**What we built:** Recall by meaning, and intent recognized by meaning rather than by phrase lists, against a bank of examples that grows from real misses.
**Verdict: Solid**, and the design principle it enables (no hardcoded phrase lists) is worth more than the mechanism.

### Prospective memory → §3.9
**The claim:** Remembering to do something later is a distinct capacity from remembering what happened.
**What we built:** The unfinished-thoughts ledger: open, advance, conclude, with age-out.
**Verdict: Solid, now working.** The write path that kept it dark is fixed (Appendix C), and covered end-to-end. It materializes on the first thread a persona opens.

### Competitive learning → §4.7
**The claim:** Units compete, the winner is reinforced, the losers are suppressed, and representations differentiate as a result.
**What we built:** Drafters compete on the critic's score. The winner gains by its margin; losers lose at half rate.
**Verdict: Solid**, and it is the mechanism by which the drafter mix becomes personal over time.

---

## Collective Behavior

*A different research lineage from the neuroscience. Note the rejection entry. It is the most credible thing in this section.*

### Stigmergy (Grassé) → §2.7 (trails)
**The claim:** Coordination through traces left in a shared environment. Ants do not instruct each other. They modify the terrain, and the terrain instructs the next ant.
**What we built:** Transient trails over the wiring, decaying in minutes. Shadow-mode capable: the trail can be measured before it is allowed to influence anything.
**Verdict: Solid**, and shadow mode is exactly how a speculative mechanism should ship.

### Division of labor by detection, not threshold (Caminer, Libbrecht & Majoe, 2023) → §4.9 (adjacent)
**The claim:** Specialization comes from differences in what an ant can *detect*, not in how readily it responds.
**What we built:** Per-personality perceptual differentiation.
**Verdict: Correctly derived, and switched off.** So what a personality *values* is the live differentiator, and the code says so at the point of use.

### Response-threshold variance (Lynch, Wilson & Dornhaus, 2024) → **REJECTED**
**The claim:** Threshold variance performs no better than random, often worse, unless the units are genuinely different from each other.
**What we did:** Built it. Read the paper. Killed it. The deprecation note says the literature is blunt that this does not help, that our units are not genuinely differentiated, and that threshold jitter would therefore be pure noise. It was replaced with the detection-based mechanism above on the strength of the Caminer finding.
**Verdict: Rejected on the evidence, and left in the tree as a documented tombstone.**

**This is the entry to lead with when someone asks whether the science is real.** A feature was built, checked against the literature, found unsupported, and deliberately disabled with the citation in its own obituary. **Nobody does that for decoration.** You cannot fake this one, which is exactly why it is worth more than any of the solid entries.

### Criticality (Cavagna et al.; Priesemann; Aston-Jones & Cohen) → §5.2, §5.5
**The claim:** Starling flocks and neural assemblies both sit near a critical point: maximum correlation length, maximum responsiveness, poised between order and chaos.
**What we built:** An estimate of how far a signal spreads through the wiring, nudging the global gain toward a setpoint that arousal modulates. Slow, smoothed, hard-clamped, and it never steers toward the chaotic side.
**Verdict: Real and deliberately conservative.** The trajectory terms elsewhere come from the same lineage and encode a genuine claim: **escalating threat warrants fresh vigilance, chronic threat habituates.** That is why a rising worry interrupts quiet and a steady one does not.

### Quorum sensing and nest choice (Chan et al., 2025) → §2.4 (under threat)
**The claim:** Acorn ants flip from exploratory recruitment to committed transport once a quorum is sensed. The phase shift *is* the decision.
**What we built:** Under threat quorum, collapse to a single draft and commit. Applied *after* recruitment, so **commitment overrides mobilization under genuine threat.**
**Verdict: Solid**, and the ordering detail shows the mechanism was understood rather than name-dropped.

---

## Privacy, Security, and Systems

### k-anonymity (Sweeney), and our departure from it → §9.11
**The claim:** A record is k-anonymous if it is indistinguishable from at least k-1 others.
**What we built:** The privacy gate, and the move is genuinely novel: **k-anonymity relocated from origin to form.** Classical k-anonymity needs k sources. Ours needs k plausible referents. A single-source insight in general form is admitted. The same insight in specific form is a disguised fact and rejected.
**Why it matters:** requiring several sources would have thrown away the single most valuable material, the expectation-violating anomaly, to buy privacy that de-identification already delivers. So corroboration became a confidence dial instead.
**Verdict: Solid, and the most original idea in the safety layer.** **If you show a privacy-literate audience one thing, show them this.**

### Pseudonymisation (GDPR Article 4(5)) → §9.10
**The claim:** Data processed so it cannot be attributed to a person without separately held information, where the key is kept apart.
**What we built:** Reversible session-scoped tokens, key held locally, applied at the boundary rather than per call site.
**Verdict: Solid**, and the same-value-same-token property is the insight that makes it useful rather than merely compliant. Relationships survive. Identities do not.

### Least privilege → §6.3, §9.2, §9.3, §9.7, §9.8
**What we built:** Three worth naming. The vault is write-only from the gateway with no read path at all. A tenant never holds the master database key. An agent can only ever narrow its own permissions.
**Verdict: Solid.**

### Fail-closed versus fail-open, as a discipline → §6.9, §9.1, §9.6, §9.11
**The principle:** The direction of failure is a per-mechanism decision, and the question is what failing *grants*.
**What we built:** Authorization, the privacy gate, and the filesystem jail fail closed. Placement, metering, and the learning wrapper fail open. The line is whether failing open grants access or merely loses a capability.
**The sharpest case is a fail-open wrapper around a fail-closed gate.** Refusing to learn is safe. Refusing to sleep is not.
**Verdict: Solid, and consistently applied.** This is the entry that tells a security reviewer that somebody was actually thinking rather than pattern-matching.

### Defense in depth → §9.9
**What we built:** Three *independent* privacy boundaries. Memory never leaves. What does leave is stripped. What crosses between customers is de-identified. Layered so one failing does not collapse the others, and where one is deliberately waived, the code **names which other one picks it up.**
**Verdict: Solid.** The named handoff at the waiver is what makes it real rather than a slogan.

### Principal-agent theory → §6.8, §7.9
**The claim:** A principal delegates to an agent with different information and interests. The design problem is the contract.
**What we built:** Mandates as delegated roles separable from identity. An approvals ledger where the agent proposes and you dispose. Scoped, time-bounded grants. And a ceiling that depends on **who initiated**.
**Verdict: Solid.** The personality/mandate separation is a real instantiation of identity versus role, not a naming convention.

### Bounded autonomy / capability attenuation → §6.2, §6.3, §6.6
**The claim:** A delegated capability should be attenuable and never amplifiable. Authority flows down and narrows.
**What we built:** Two permission columns keyed to who initiated, and a resolution rule where every combiner narrows: minimum, AND, intersection, containment. Enforced at read time so stale looser values cannot grant.
**Verdict: Solid.** The monotone-restriction property is real and it is checked in three independent places.

### Tenancy isolation → §6.10, §9.1
**What we built:** The organization as the owning unit, one process per tenant, and an org gate on process-global connectors specifically because they would otherwise be inherited by everyone.
**Verdict: Solid**, with the caveat in §9.3 about which layer is actually enforcing it right now.

### Time-of-check to time-of-use → §6.11
**What we built:** Two real fixes. The skills registry keeps the latest submission apart from the last approved one, so a resubmission cannot ride a prior approval. And the filesystem jail re-checks at session start, because a directory that was safely inside the boundary when an admin saved it can later be swapped for a link pointing outside it.
**Verdict: Solid.** Both are real vulnerability classes, correctly identified and closed.

---

# Appendix A: What Is Not True Yet

**The most useful section in this document.** Read it before any technical conversation. Being the person who names their own gaps first is worth more than any feature you could name instead.

*Last audited 2026-07-18. The defects this table used to list were fixed in that pass; what remains is either a real limit, a deliberate position, or a decision waiting on you. Appendix C records what changed.*

**Before reading this as a list of things switched off: nothing ships off by default.** New capabilities go live with their flag on, and the flag is a kill switch. See "How to read this" for why. A flag set to 1 in the code is the standing policy, not a gap and not an oversight. The only switches that sit off on purpose are safety approval gates and keyed integrations that bill per call, and each of those is listed below as a deliberate position rather than a limit.

## Real limits: things the system does not do

| Thing | Reality |
|---|---|
| **Spiking neurons** | Not implemented. Stateless comparators. Never claim it. |
| **Rewiring itself** | The core cell-to-cell map is fixed; learning moves weights on it, not its structure. Around it, a bounded, fenced, screened structural layer is live: vetted skills wired onto cells (§2.9), reserve cells recruited into new units, and the brain authoring its own skills, each one passing the untrusted-skill screener and earning its place through competition (§2.10). The core map itself does not rewire, and nothing self-modifies without those gates. |
| **Reward prediction error / TD learning** | Not implemented. The dopamine delta is a proxy for it. |
| **Replay** | Not implemented. Consolidation is batch post-processing. |
| **Embodied cognition** | One chemistry-gated perception switch. Real, but keep the claim that size. |
| **Self-grading** | ~80% of the reward signal is self-administered. Instrumented and capped, not solved. The external channel is now reachable and on, which makes it fixable rather than fixed. The measure itself briefly overstated its own health, because the evidence gates (§2.11) stamped self-generated inferences with the external label; those now carry their own provenance label and tally as intrinsic, so the external share is a reading again rather than an upper bound. Aesthetic reward (§4.9) went from a dead weight to a live intrinsic source in the same pass, which pushes the ratio the other way by a small amount. |
| **Avoidance reading** | Live, and now requires an active dodge rather than mere staleness, with a leak, a hard expiry, and eviction so a belief cannot pin itself. Still unvalidated on real traffic: the thresholds are reasoned, not measured. |
| **Repetition-gate sensitivity** | The structural check (§5.4) now normalizes away hedges, modals and inflections, so signatures collide far more readily than when its ceiling was set. The ceiling was raised to compensate by reasoning about the higher collision rate, not by measurement. The suppression log records the signature and the reason, so it is measurable — it has not been measured. Until it is, the honest claim is that the check catches what it was built to catch, not that its rate is tuned. |
| **Reflexes / motor chunking** | Has never produced a reflex, and now we know why: the job corpus is dominated by *failed* exploratory work. The most common sub-sequence runs at 67% success against a 90% bar. Not miscalibrated. Waiting on jobs that succeed. |
| **Song recognition** | The fingerprint database is an empty stub. Cannot match. |
| **Video** | Zero callers. |
| **Per-mandate reward weights** | Now consumed. An assigned mandate layers its own weights onto what the persona values, resolved ambiently from the turn's agent binding, clamped at write and at read, and the composite is clamped again. The Stoic is exempt (§4.9). |
| **Unfinished thoughts in engine-mode working context** | Built, as a deliberate position: companion turns surface open threads ungated; engine/customer turns surface only threads whose `bears_on` overlaps the active mandate's domain, so a persona's introspective threads stay out of a customer's conversation. |

## Deliberate positions: things people mistake for gaps

| Thing | Why it is this way |
|---|---|
| **The Constitution** | Never read by code, by design. A design document and a developer norm. The runtime rules live in the self-description. |
| **The wiring freeze** | A wiring freeze, not a consolidation freeze. It halts the whole learning pass, so the wiring file survives a session byte-identical, but journals, self-description updates, and episodic writes all continue. That is the intended scope, not a leak. |
| **Promoted personalities** | Off in production. Characters ride the shared instance: identity works, authored chemistry needs the flag. |
| **Big Five** | Not used. Factors describe personality; chemistry produces it. |
| **Lite-tier privacy** | Memory runs in the **cloud** for lite agents, because a lite agent has no GPU. Defensible, but it varies by tier and is **still not in the partner-facing docs.** |

## Waiting on a decision

| Thing | The call to make |
|---|---|
| **Database-level tenant isolation** | **Resolved (2026-07-17).** Was inert for a stretch — the credential fell back to the master key on a bad key-shape heuristic, bypassing database-level enforcement. Now minted by probing whether the database accepts it, so database-level isolation is enforced again (§9.3), with in-query scoping as the second layer and a kill-switch to force the old fallback. This also un-broke the MCP token stored procedures, which had been failing because the master-key path left their identity claim null. |
| **The external verdict channel** | **Decided and now on (2026-07-17).** The DA nudge on an external grade was moved off zero to 0.15, calibrated to land above inferred praise (~0.10) and below the accomplishment signal (~0.34) so it grounds the reward without dominating it, and bounded per grade so a hostile or spammy grader cannot drive chemistry past that ceiling. A grade now moves real chemistry via the external_grader source, and the four mix weights are live dials left at 0.4/0.2/0.2/0.2. It is the only reward signal grounded outside the agent's own appraisal. Both entry points are wired and live: the owner UI thumbs (→ /feedback) and the partner engine API (POST /sessions/{id}/turns/{turn_id}/grade, which re-binds the customer's own chemistry before the write — see §4.4). The partner endpoint has since been hardened against the three holes an adversarial review found: cross-session turn access, unbounded repeat grades, and a nudge on a turn that is not live. **The remaining call is purely how far to open it**, and on what volume of real grades to retune the four mix weights. |
| **The inverted-U plasticity model** | Now on by default (2026-07-17). Emotionally intense turns imprint harder, extreme stress imprints less. Replaces the legacy binary defuse-path skip. |
| **Perceptual differentiation per personality** | Built, switched off. Valuation is the live differentiator. |
| **Music perception** | Fully built. One environment variable from live. Set nowhere. |
| **The "Memory" temperament dial** | Partly alive now. Its decay setting genuinely tunes the workspace spotlight's persistence (see §2.8), so the dial is no longer a complete no-op. But not every setting it and the Focus/Curiosity dials write is load-bearing yet. A full dial-to-behavior audit is the remaining follow-up. |
| **PAPER.md** | Predates the motor rebuild, the learning surface, the personas API, and placement. Describes a library that was removed. Its Global Workspace claims are now true (§2.8); the other drift is not yet reconciled. |

---

# Appendix B: Lines That Land

Verbatim from the code. Useful because they are true, and because each one compresses a whole design decision into a sentence.

> *"The prediction is statistically valid but morally wrong. The moment deserves fresh attention, not a cached response."*
> On why emotion overrides the cost optimization.

> *"Chemistry may modulate EFFORT and ATTENTION. It must never widen MONEY."*

> *"Storage is free; retrieval is the intelligence."*

> *"Never read topic/entity strings here. That would let domain leak into the signature and break transfer."*
> On cross-domain transfer.

> *"Pride is INTRINSIC: nailing its own standard is enough to feel it, whether or not the user acknowledges it."*

> *"That one-sidedness is what makes it loss aversion."*

> *"Intrinsic far exceeding external means the brain is mostly rewarding itself."*
> The system instrumenting its own biggest weakness.

> *"The ant task-allocation literature is blunt that this does NOT help, so threshold jitter would be pure noise."*
> The deprecation note on a feature the evidence does not support.

> *"A reflex has fixed parameters; anything context-dependent stays deliberate."*

> *"Recurrence is NOT the privacy gate."*

> *"The gate's test suite IS the privacy proof."*

> *"A genuine transcendence of a human limitation, not a simulation of it."*
> On departing from Clark and Chalmers.

> *"Talk about the dopamine level, not about the brain feeling rewarded."*
> The house style rule.

> *"No more silent empty-success."*

> *"A weaker model picks worse actions but can do nothing the allowlist forbids. The safety posture is the dispatcher's, not the provider's."*

> *"This framing is the prompt-injection defense at the prompt layer. The runtime gates are the real boundary."*

---

# Appendix C: What The Audit Fixed

*2026-07-17. Recorded once, here, so the rest of the document can stay a description of the present. This section is the exception to the snapshot rule and should be deleted once it stops being useful.*

**Nine defects closed.** Suite went 2122 → 2184 passing; every fix landed with a test that fails without it.

- **The external verdict channel is reachable.** Five settings keys were read at their call sites but never registered, so the loader silently dropped them and the API refused them. Registered at their exact current values, so nothing turned on and nothing changed behaviour. One trap avoided: registering the nudge as an integer would have made it reachable but still broken, silently truncating any fractional value to zero.
- **Delayed credit is visible.** Eligibility updates now log as a distinct record carrying which turn earned the credit and which turn paid it. Emitted as one aggregate per turn-and-age rather than per edge, so the ledger grew ~20% instead of ~200%. The session total now reconciles exactly with the logged records.
- **Per-customer moods survive a restart.** The durable store is wired, routed through the tenant-canonical path, throttled to match the persona-chemistry pattern, and degrades to memory rather than breaking a turn.
- **The open-threads ledger was silently failing on every write.** The real find of the audit. The section writer built a regex replacement template out of the content, and JSON-escaped non-ASCII produced `\uXXXX`, which the regex engine rejects as a bad escape. One curly apostrophe or em dash from the model, and the write raised. Because the template compiles before the scan, it raised even when the section was absent, so the create branch was unreachable and the section could never come into existence. The DMN swallowed it as a warning. **The tests missed it because they mocked the sink.** Fixed at the shared writer, which protects three other callers.
- **Motor chunking had two real bugs behind the empty shelf.** Planner-failure placeholders scored as a perfect skill, and were the only thing corpus-wide clearing promotion, which would have handed the motor cortex a ballistic no-op that fed itself more placeholders. And the mining pass grouped by a raw persona stamp where one persona resolves under two names, so it wrote for one group and then skipped the other by tripping its own interval gate inside the same pass, permanently hiding 18% of the corpus.
- **A cross-org read.** One endpoint queried a table by end-user id with no org filter. Harmless while RLS was live; a metadata leak once RLS went inert. Fixed, plus a structural guard test over every query site so the class cannot recur.
- **A relationship migration that was about to forget Russ.** Files predating the bond model have no bond line, which parsed as zero, which reads as "new" — and unlike the legacy path, the bond path had no never-downgrade guard. The next neutral turn would have downgraded a 44-interaction relationship to stranger. Fixed by healing on read plus a guard on the turn path only, so absence can still decay a bond but talking cannot.
- **The test suite was writing into live memory.** 83 of 97 job records were test fixtures, one committed. The suite was also appending to the real tool log and rewriting real routing weights. All invisible, because the directory is gitignored. Isolated; the tree is now byte-identical across runs.
- **Four false claims in code**, including the Active Inference label and a flag docstring that stated the opposite of its default.

**What the audit revealed that no one had listed:** the database-level tenant isolation is inert in production, the MCP token feature is broken because of the same root cause, and the Memory temperament dial did nothing.

**Then, the largest single fix: Global Workspace Theory made real (§2.8).** For most of the project's life the thalamus was a stub — it published an attention spotlight nothing subscribed to, and the caller discarded its verdict. The paper and the Constitution both cited it as load-bearing architecture, so the system's central consciousness claim had no code under it. That is now closed. The thalamus reads the bus concentration layer (the workspace field that already existed), fuses every topic into one ignition verdict, and broadcasts it. The broadcast is load-bearing at three places — a slow-built threat wakes the deliberate path, memory recall widens and points at the focus, and the idle mind dwells on what ignited — and the idle mind is a real subscriber to the channel, so "available system-wide" is literal. It ships on with a conservative threshold, provably neutral when nothing is ignited. Four GWT predicates, each now mapped to code; the honest caveat is that it is new and wants soak time on real traffic.

---

# Appendix D: For the Avatar Conversation

Since this started there. The version to hand a developer:

**What is computed:** pleasantness, energy, and confidence, continuously, every turn, from all nine channels. That is the PAD model (Mehrabian & Russell), which is already the standard interface for expressive rigs.

**What is public today:** one emotion word and one energy number per turn. Chemistry is deliberately withheld and locked by a test. That is policy, not an oversight.

**What they can build on now:** the word plus the number. The mood color table as a ready-made expression palette, where hue is the family, lightness is energy, saturation is intensity. The personality endpoint for a static resting anchor. And the per-chunk voice settings on the speech endpoint, which are strictly richer than the turn-level word.

**The one ask worth making:** expose the three continuous dimensions on the turn response. It is a few lines. They are a *projection* rather than the raw signal, so they give away far less than the nine channels while being exactly the interface an expressive rig wants.

**The pattern to steal:** define your expression poses, pin each to a chemical signature, blend by distance so the closest counts most. That is a blendshape rig, and it is already running in production driving our voice.

**Two traps:** the trust channel is abbreviated OXT, not OXY. And three unrelated things in the codebase are named "valence."
