# Game Theory: Coalition Analysis

Cooperative game theory asks a different question from strategic (non-cooperative) game theory. Rather than asking what rational self-interested players will do when they can't coordinate, it asks: when players *can* form binding agreements and share gains, which coalitions will form, and how should the value be divided?

Lloyd Shapley's answer to the division question — the Shapley value (1953, Nobel Prize 2012) — is remarkable for its mathematical precision and moral intuition. Each player's fair share is their average marginal contribution across all possible orderings of coalition formation. Formally: for each permutation of all players, calculate how much value player *i* adds when they join the coalition that has formed before them. Average this marginal contribution across all permutations. The result is the Shapley value — the uniquely fair allocation given four axioms: efficiency (the grand coalition's total value is fully distributed), symmetry (identical players receive equal shares), dummy (players who contribute nothing receive nothing), and additivity (allocations across independent games add correctly).

The core captures coalition stability: an allocation is in the core if no subset of players can collectively do better by breaking away and forming their own coalition. If an allocation is in the core, no group has an incentive to defect — the grand coalition is stable. If the core is empty, no allocation is fully stable and some defection pressure is unavoidable.

These two concepts are complementary but distinct. The Shapley value is always unique and always exists — it answers "what is fair?" The core may be empty — it answers "what is stable?"

---

## Your Process

**Step 1: Player-value map**
List all players and, for each possible coalition (every subset), specify the value that coalition can generate on its own. This is the characteristic function of the game — v(S) for every subset S. For small groups (3–4 players), enumerate all subsets. For larger groups, focus on the most relevant coalitions: the grand coalition, each individual player alone, and the likely competing sub-coalitions.

**Step 2: Grand coalition assessment**
Is the grand coalition (all players together) the most efficient arrangement? Check whether v(everyone) ≥ v(any subgroup) + v(remaining players). If yes, the grand coalition maximises total value and the question is only how to divide it. If no, some smaller coalition creates more value, and the question is which one forms.

**Step 3: Shapley value calculation**
For each player, calculate their average marginal contribution:
- List all permutations of player ordering (for n players, there are n! permutations — for 3 players: 6; for 4 players: 24)
- For each permutation, identify what coalition exists just before player i is added, and calculate v(coalition + i) − v(coalition)
- Average this marginal contribution across all permutations
- The result is player i's Shapley value
