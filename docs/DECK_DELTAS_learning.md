# Deck deltas — learning / wiring changes (2026-07-19)

Paste-ready edits for `Elyceum Systems.dc.html`. Source of truth is `docs/SYSTEMS.md`
§2.7, §4.1 and §4.7 as of commit `bb22b84`.

Slide numbers are taken from `docs/DECK_BRIEF.md` and should be confirmed against the
live deck. Where a slide is identified by its SYSTEMS.md section instead, that mapping
is stable regardless of renumbering.

---

## 1. Wiring-graph slide (§2.7, Cognitive Core) — CHANGE A NUMBER, ADD A CLAIM

**Replace** "roughly sixty declared connections" with **"about seventy declared
connections"** (the graph seeds 72).

**Add** this paragraph. It is the single most substantive change in this batch, and it
is a strengthening claim rather than a caveat:

> Every declared connection can be learned on. That is worth stating because until
> recently it was not true. Credit travelled only between parts that fire one after
> another, and about half the graph has an endpoint that never fires in that sense — an
> incoming signal, a chemistry reading, a piece of held context. Those connections were
> carried in the graph and drawn in the interface while being structurally incapable of
> ever changing. They now earn credit from having taken part in a turn rather than from
> firing in sequence within it, which is closer to what the underlying idea always
> claimed: what is active together strengthens together.

**Keep** the existing "fixed map, learned weights" precision point unchanged. It is still
exactly true and this addition does not soften it — the possibility space did not grow,
it just became fully reachable.

---

## 2. "The update and the reward signal" slide (§4.1 + §4.2, Part D · Learning) — ADD TWO POINTS

**Add**, after the existing homeostatic-decay line:

> Fading is measured per turn and scaled to the number of turns being consolidated. That
> sounds like bookkeeping and is not: strengthening was already counted per turn, so
> while fading was counted per *session*, how strong a route could ever become depended
> on how long the conversation happened to run — the same route settling roughly three
> times higher after a long session than a short one. Counting both the same way removes
> that.

**Add** as a second point — this is the one a technical reader will push on, so state it
as a deliberate choice rather than leaving it implicit:

> How fast a route can change is a tuned quantity. Too slow and nothing the agent learns
> ever surfaces in its behaviour within a usable number of conversations; too fast and a
> single bad session rewrites how it thinks. It currently takes about two sessions for a
> genuine shift in routing to become visible and about seven to settle.

---

## 3. "What else learns, and how each is graded" (§4.7) — ADD TWO BULLETS

The brief already calls for splitting §4.7 across two slides. These two belong on the
second, after the four existing population rules.

> **Everything else that was active earns credit in proportion to how much it took part**
> — the general rule the four above are special cases of. A part that barely engaged
> moves its connections barely; a part that carried the turn moves them fully. The
> proportionality is the whole point: crediting every co-active connection equally would
> raise them all together, and because every consumer of these weights reads them
> *relative* to their neighbours, moving them in unison changes nothing while looking
> exactly like learning.

> **One thing deliberately does not earn credit twice.** Where a competition already
> picks a winner, the connections it owns are excluded from the general rule. Otherwise
> both apply, and the general rule is about twenty times larger and rewards *whichever
> candidate happened to go first* rather than whichever won — so position drowns
> judgment. This was measured, not hypothesised: the first-listed drafter had
> accumulated the strongest connection in the whole graph, and it had earned it by
> being first.

That last sentence is the most quotable thing in this batch for a skeptical engineer. It
is a bug the system found in itself, in its own logged data, and fixed.

---

## 4. Slide 27, "The measured result" — ⚠️ DO NOT SHIP AS WRITTEN

The brief has this slide citing the wiring-divergence experiment: firing-path Jaccard
**0.117 (CI 0.084–0.152)** against a frozen control of **0.066**, **p = 0.002**; recall
fan-out **0.110 vs 0.012**; caveats "learning rate amplified ×5, synthetic probes, ten
sessions"; and the honest negative that "switch evaluation order showed no
differentiation at all."

**Every one of those numbers is stale, and two of them are now actively misleading.**

- The experiment is commit `b1af736`, dated 2026-06-06 — **more than 374 commits behind
  HEAD.** It predates `eee09c7`, which completed the very switch-ordering surface the
  slide reports a negative result for. That negative measured an unfinished mechanism.
- The **"×5 amplification"** caveat is no longer a caveat about method — it was evidence
  about the product. The harness amplified the learning rate fivefold because the real
  rate was far too small to show an effect in ten sessions. That is precisely the defect
  fixed in `bb22b84`. Restating it as a limitation of the experiment, when it was a
  finding about the system, is the kind of thing this reader will catch.
- Coverage changed underneath it: about half the graph was incapable of moving when that
  experiment ran.

**Options, in order of preference:**

1. **Re-run and restate.** `uv run python eval/wiring_divergence_ab.py` at `--amplify 1`.
   The acceptance bar in the plan is that the new defaults unamplified should match or
   beat the old ×5 curve. If they do, that is a far stronger slide than the current one
   and the amplification caveat disappears entirely.
2. **Hold the slide** until (1) is done. An empty space is better than a stale headline
   number a reader can date.
3. If it must ship now, mark it explicitly as measured against a superseded build and
   drop the switch-ordering negative, which is no longer a finding about current code.

The stale result files are still in `eval/` — deliberately not deleted, since they are
the only quantitative evidence on hand until a re-run. They should not be cited.

---

## 5. NEW material worth adding — the self-grading number is now quantified

The brief identifies §4.3 + §4.4 + §4.8 as "the most credibility-buying run in the deck":
a system that measures its own worst property and publishes the number. That run just got
a sharper number, from calibrating the economy against real logged turns:

> Across 280 logged turns that produced learning, the average strength of the reward
> signal was 0.41 on a scale of −1 to 1, and **97.5% of it was positive.** The agent
> mostly tells itself it did well. That number is not a target and it is not defended —
> it is the measured size of the problem the external verdict channel exists to shrink,
> and it is why reward is deliberately hard to farm.

This belongs on the §4.3 slide as the concrete figure behind the existing "~80% of reward
is self-administered" claim. It is the same admission with a real measurement attached.

---

## 6. Glossary check

`weight` is already flagged in the deck's disambiguation table (edge weight vs reward
weight). No new overloaded terms are introduced. **Do not** introduce "co-activation" as
deck vocabulary — the phrasing above deliberately says "took part in a turn" instead,
consistent with the house rule of describing by function rather than by source theory.

---

## Not for the deck — operational note

Deploying this needs each hosted tenant's `settings.json` backfilled with
`hebbian_outcome_delta: 0.06`. Those files pin the old `0.02` and override the new
default; the new decay key is absent from them and *would* take effect, so an
un-backfilled tenant gets faster forgetting against an unchanged step, which is worse
than not shipping at all.
