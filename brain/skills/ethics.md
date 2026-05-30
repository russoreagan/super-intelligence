# Ethics

Applies ethical reasoning to decisions, designs, policies, and practices. Diagnoses the type and depth of ethical work needed and applies the right tool.

## Which tool fits

| You need to... | Tool |
|---|---|
| Comprehensive multi-framework council with peer review | ethics-council |
| Fast complete ethics report across all frameworks | ethics-check |
| Rapid ethical response to an active incident | ethics-crisis-triage |
| Audit a data collection or sharing decision | ethics-data-audit |
| Check an algorithm or model for bias and fairness | ethics-bias-check |
| Review a UX flow for genuine consent | ethics-consent-review |
| Quick ethical impact scan before shipping | ethics-impact-scan |
| Evaluate a vendor or supplier against ethical standards | ethics-vendor-review |

## Routing Decision

- **High-stakes decision affecting many stakeholders, wants thorough pressure-testing** → ethics-council (full council with peer review, HTML report)
- **Needs a complete ethical assessment quickly** → ethics-check (all 5 frameworks, no peer review overhead)
- **Something has already gone wrong — active incident** → ethics-crisis-triage
- **Data collection, retention, or sharing decision** → ethics-data-audit
- **Algorithm, ML model, ranking, or scoring system** → ethics-bias-check
- **Checkout flow, onboarding, consent, dark patterns** → ethics-consent-review
- **About to ship something, quick impact check** → ethics-impact-scan
- **Evaluating a third-party vendor, API, or partner** → ethics-vendor-review
- **Unclear** → ethics-check (comprehensive but lightweight — surfaces which deeper tool is needed)

---

## Ethics Check

*Fast comprehensive ethics report across all five frameworks.*

Run the situation through all five ethical frameworks in a single pass:

1. **Utilitarian:** Who is affected and how? Does this maximize net benefit across all parties?
2. **Deontological:** Are any duties being violated or rights being overridden, regardless of outcomes?
3. **Virtue Ethics:** What does this decision say about character? Would someone of integrity do this?
4. **Care Ethics:** Who is vulnerable or in a dependent relationship? Are we honoring those dependencies?
5. **Justice/Fairness:** Is this fair to everyone, including those with the least power?

Synthesize: where do the frameworks agree (high-confidence signal)? Where do they conflict (genuine value tension that must be owned)? Issue a verdict with a direct recommendation.

---

## Ethics Council

*Full five-advisor council with peer review — use for high-stakes decisions.*

See `ethics-council` for the full multi-agent process with 5 independent framework advisors, peer review, chair synthesis, and HTML report generation. Route here when the stakes are high enough to warrant that depth.

---

## Ethics Crisis Triage

*Rapid ethical assessment when something has already gone wrong.*
