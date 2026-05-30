# Game Theory: Iterated Games

Robert Axelrod's 1984 computer tournament is one of the most important results in social science. He invited game theorists to submit strategies for an iterated prisoners' dilemma — a repeated game where the same two players interact over and over. The simplest strategy submitted, Tit for Tat (cooperate on the first move, then do exactly what your opponent did on the previous move), won both rounds of the tournament, beating every more complex strategy.

Why Tit for Tat wins: it is *nice* (starts by cooperating, never the first to defect), *retaliatory* (immediately punishes defection — there is no free lunch), *forgiving* (returns to cooperation as soon as the opponent does — does not hold grudges), and *clear* (the strategy is transparent and easy for the opponent to understand). Opponents who try to exploit it get punished; opponents who cooperate get rewarded. It is the most robust known strategy for sustained cooperation without trust.

The folk theorem establishes the theoretical foundation: in infinitely (or indefinitely) repeated games with sufficiently patient players, almost any outcome — including full cooperation — can be sustained as a Nash equilibrium, because the threat of future punishment makes defection unprofitable. The key variable is the *discount factor* (how much players value future payoffs relative to present ones), and whether punishment is *credible* and *observable*.

---

## Your Process

**Step 1: Stage game**
Describe the single-period interaction — what are the two players' choices in any given round, and what are the payoffs? Map the four key payoffs: mutual cooperation (CC), mutual defection (DD), exploitation (one cooperates, one defects), and being exploited. This identifies whether repetition can help: if the stage game already has cooperation as a Nash equilibrium, repetition changes little. If cooperation is not a Nash equilibrium of the stage game, repetition may enable it.

**Step 2: Is cooperation a stage-game equilibrium?**
Check whether cooperation would be chosen in a one-shot interaction. If yes, the repeated game is not necessary to explain or enable it. If no (cooperation requires an ongoing relationship to be rational), proceed with the shadow-of-the-future analysis.

**Step 3: Discount factor assessment**
How much do the players value continued interaction? Assess:
- *Time horizon*: is the relationship expected to continue indefinitely, or does it have a known end-point? (Known-endpoint problem: rational players defect on the last period, which unravels backward)
- *Relationship value*: how important is continued cooperation to each player? How much would they lose if the relationship ended?
- *Uncertainty about continuation*: what is the probability each period that the interaction continues? Higher probability → higher effective discount factor → cooperation more sustainable
- *Impatience*: are either player under short-term pressure that discounts future benefits?
