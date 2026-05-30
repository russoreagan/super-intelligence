# Game Theory: Mechanism Design

Standard game theory takes the rules as given and asks what rational players will do. Mechanism design inverts this: it takes the *desired outcome* as given and asks what rules will produce it. This is why it is often called reverse game theory.

The central insight, formalised by Leonid Hurwicz and developed by Eric Maskin and Roger Myerson (who shared the 2007 Nobel Prize), is that private information is the root challenge. Players know things the designer doesn't — their true valuations, their effort levels, their costs — and they have incentives to misrepresent that information if doing so serves them. A well-designed mechanism elicits honest behaviour not by demanding honesty, but by making honesty the dominant strategy: the player's best move given the rules, regardless of what others do.

The revelation principle is the foundational theorem: any equilibrium of any mechanism can be replicated by a *direct incentive-compatible mechanism* — one where each player simply reports their private information truthfully and the rules process it correctly. This means the designer never needs to think about indirect or complicated mechanisms; there is always an honest, direct mechanism that achieves the same outcome.

William Vickrey's second-price auction is the canonical example: by having the winner pay the second-highest bid rather than their own, the dominant strategy becomes truthful bidding. The mechanism extracts honest valuations without demanding or relying on honesty.

---

## Your Process

**Step 1: Desired outcome**
State precisely what behaviour or allocation the mechanism should produce. Vague goals produce vague mechanisms. "People should behave better" is not a desired outcome. "Employees should report their true performance levels" is. "Suppliers should bid their true costs" is. Be specific about whose behaviour, what information, and what allocation.

**Step 2: Player map**
For each player involved:
- What *private information* do they hold? (True valuation, effort level, ability, cost, intent)
- What are their *incentives*? What would they do if there were no mechanism and pure self-interest governed?
- What would they prefer to report or do under a naive mechanism?

**Step 3: Misalignment diagnosis**
Describe the current equilibrium or default behaviour — what players actually do without the mechanism, or what they do under the current flawed system. Why is this bad? Identify the specific gap between what players find individually rational and what would be collectively desirable.

**Step 4: Mechanism specification**
Design the rules and payoffs. Work through three components:

**a. Information revelation**: How do you incentivise players to truthfully reveal their private information? Apply the revelation principle: design payoffs so truth-telling is the dominant strategy. Ask: "If a player with private information X reports Y instead, do they gain or lose?" The mechanism should ensure they lose — or at minimum don't gain — from misreporting.
