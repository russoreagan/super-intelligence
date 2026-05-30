# Game Theory: Auction Analysis

William Vickrey's 1961 discovery is one of the cleanest results in economics: in a second-price sealed-bid auction, bidding your true value is a *dominant strategy* — the best move regardless of what others bid. The mechanism works because you pay the second-highest bid, not your own. Overbidding your true value doesn't help you (you might win but pay more than the item is worth); underbidding doesn't help you either (you might lose an item worth more than you'd have paid). So you bid your true value and let the second-highest bid determine the price. Vickrey received the Nobel Prize in 1996 for this result and related work.

First-price auctions are strategically different: you pay what you bid, so optimal play requires *shading* your bid below your true value. The optimal shade depends on the number of competitors (shade more with more competitors) and the distribution of their valuations (shade more when competition is intense). In equilibrium, first-price and second-price auctions generate the same expected revenue — the revenue equivalence theorem — under standard conditions.

The winner's curse is the most common failure mode in *common-value* auctions (where the item has an underlying objective value everyone is trying to estimate, rather than a private personal value). Winning means you bid highest, which means your estimate was the most optimistic among all bidders. In expectation, if you bid your unconditional estimate and win, you've overpaid — because winning reveals that you were the most optimistic, not the most accurate. The correct bid is your estimate *conditional on winning*, which is lower than your unconditional estimate.

Paul Milgrom and Robert Wilson (Nobel 2020) developed the modern theory of auction design, including the simultaneous ascending auction used in FCC spectrum allocation — showing how auction design directly affects both revenue and efficient allocation.

---

## Your Process

**Step 1: Auction type identification**
Identify the auction format:
- *First-price sealed bid*: all bidders submit one bid simultaneously; highest bid wins and pays their own bid
- *Second-price sealed bid (Vickrey)*: highest bid wins but pays the second-highest bid
- *Ascending (English)*: price rises until only one bidder remains; winner pays the final price
- *Descending (Dutch)*: price falls from a high start until the first bidder claims the item at the current price
- *Other*: procurement reverse auctions, combinatorial auctions, multi-round formats

**Step 2: Private vs. common value**
Determine the value structure:
- *Private value*: each bidder has their own subjective valuation, independent of others. What the item is worth to you doesn't depend on what it's worth to others. Most art auctions, personal property sales.
- *Common value*: the item has an underlying value that is the same for all bidders, but each has an imperfect estimate. Mineral rights, spectrum licenses, antique coins (where the value is objective but uncertain). *Winner's curse applies here.*
- *Affiliated values*: intermediate case — your valuation is positively correlated with others'. Most real situations fall here.
