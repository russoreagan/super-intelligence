# Game Theory: Equilibrium Analysis

John Nash's central insight: in any finite game, there exists at least one outcome — the Nash equilibrium — where no player can improve their payoff by unilaterally switching strategy, assuming all other players hold theirs. This is the point of stability; it's where rational play converges.

The power of equilibrium analysis is not that it finds the best outcome. It finds the *actual* outcome — where unconstrained, rational, self-interested players end up. Many Nash equilibria are collectively inefficient: the classic prisoners' dilemma equilibrium is both stable and bad for everyone. Knowing where the game ends up is the prerequisite for deciding whether to play, to change the rules, or to engineer a better outcome.

Thomas Schelling added a critical extension: when multiple equilibria exist, players coordinate on *focal points* — outcomes that feel natural or salient without explicit communication. The focal point is often obvious in context (the prominent location, the round number, the culturally expected choice) and determines which of several possible equilibria is reached.

---

## Your Process

**Step 1: Map the players and strategies**
Identify every player in the interaction. For each player, list their available strategies — the distinct actions they can choose. Keep the strategy set realistic: exhaustive but not so granular it becomes unmanageable.

**Step 2: Build the payoff matrix**
Construct a matrix showing each player's payoff for every combination of strategies. Fill in all cells. If payoffs are uncertain, use expected values. If precise payoffs aren't available, use ordinal rankings (best, good, neutral, bad, worst) — the analysis still holds.

**Step 3: Find dominant strategies**
A dominant strategy is one that is better for a player regardless of what others do. Check each player: is there a strategy that beats or ties all alternatives across every possible opponent choice? If a dominant strategy exists, rational players will always choose it — it simplifies the analysis substantially. Iterated elimination of dominated strategies can further reduce the game.

**Step 4: Identify Nash equilibria**
For each cell in the matrix, ask: given what the other player(s) are doing, would this player want to switch? If no player wants to switch — the combination is a Nash equilibrium. Mark all such outcomes. (A game may have one, several, or in mixed-strategy form, infinitely many equilibria.)

**Step 5: Efficiency assessment**
Evaluate the equilibrium outcome(s): is this collectively good, or is there a better outcome that rational play fails to reach? An outcome is *Pareto-inefficient* if there exists an alternative where everyone would be at least as well off and at least one player would be strictly better off. Name exactly why the efficient outcome is unreachable without external intervention.
