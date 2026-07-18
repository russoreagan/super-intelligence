# Elyceum — Pricing & feature spec

> Handoff spec for the marketing site. Prices are **anchors** pending real
> cost-to-serve validation — keep them in config/CMS, not hardcoded.
> Companion visual mockup: `docs/marketing_pricing_grid.html`.

**Hero claim (use everywhere):** *Gets cheaper the more you use it.*
**Sub:** An agent with a persistent personality that learns from experience — routine work replays faster and costs less every time, and every interaction gets more personal.

## Tiers at a glance

| Tier | Price | Keys | Access | In one line |
|---|---|---|---|---|
| **Design Partner** | Free (capped) | You bring all | **App (UI) only** | Evaluate the full experience free; upgrade to build |
| **Bring Your Own** | ~$29 / seat / mo | You bring all | App + API | Your agent, unlimited use — the cheapest way to run it |
| **Voice Included** ★ | ~$89 / seat / mo + voice usage | You bring LLM; voice included | App + API | The affective voice moat with zero voice setup |
| **Fully Managed** | ~$249 / seat / mo + usage | Everything included | App + API | Zero setup — we manage the whole stack |
| **Enterprise** | Custom (unadvertised) | Negotiable | App + API | Dedicated capacity, SSO, security review, SLA |

★ = "Most popular"

## Full comparison grid

| Feature | Design Partner | Bring Your Own | Voice Included ★ | Fully Managed | Enterprise |
|---|---|---|---|---|---|
| **— Keys & billing —** | | | | | |
| Access | App (Elyceum UI) only | App + API | App + API | App + API | App + API |
| You bring your LLM key | Yes | Yes | Yes | Included | Negotiable |
| You bring your voice keys | Yes | Yes | Included | Included | Negotiable |
| Price | Free (capped) | ~$29/seat/mo | ~$89/seat/mo | ~$249 + usage | Custom |
| **— The learning layer (every plan) —** | | | | | |
| Learns & gets cheaper over time | ✓ | ✓ | ✓ | ✓ | ✓ |
| Cost-savings dashboard | ✓ | ✓ | ✓ | ✓ | ✓ |
| Persistent personality / character | ✓ | ✓ | ✓ | ✓ | ✓ |
| Emotional intelligence (reads tone) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Perfect, editable memory | ✓ | ✓ | ✓ | ✓ | ✓ |
| Approvals & safety gates | ✓ | ✓ | ✓ | ✓ | ✓ |
| Skills library | ✓ | ✓ | ✓ | ✓ | ✓ |
| **— Capacity & limits —** | | | | | |
| Interactions (chat) | Daily cap | Unlimited | Unlimited | Unlimited | Unlimited |
| Personas *(personalities you talk to)* | 1 | Up to 5 | Up to 10 | Unlimited | Unlimited |
| Agents *(a persona put to work)* | 1 | 2 | 5 | 10 | Custom |
| Always-on agents *(run autonomously)* | — | 1 | 1 | 2 | Custom |
| Autonomous multi-step jobs | Limited | ✓ capped budget | ✓ | ✓ higher budget | Custom |
| Connectors / integrations | 1 | 1 | 3 | Unlimited | Custom |
| **— Included inputs —** | | | | | |
| Affective voice (TTS / STT) | Your key | Your key | Included allotment | Larger allotment | Custom |
| LLM inference | Your key | Your key | Your key | Credits + metered | Custom / BYO |
| **— Teams, API & enterprise —** | | | | | |
| Team roles & multi-seat admin | — | — | ✓ | ✓ | ✓ |
| Engine API (embed in your product) | — | Included | Included | Higher limits | Custom |
| SSO · security review · SLA | — | — | — | — | ✓ |
| Dedicated capacity | — | — | — | — | ✓ |
| Support | Community | Email | Priority | Priority | Dedicated |

**Personas vs. agents:** a *persona* is a personality you can talk to — create as many as you like; they're cheap. An *agent* is a persona *put to work* with a role and permissions — agents run tasks and do the autonomous work, so they're the unit that scales with your plan.

## Marketing feature list (public-safe copy)

**It learns — and gets cheaper the more you use it**
- **Improves with experience** — routine work replays faster and costs less each time, the opposite of a memory file that gets slower and pricier as it grows.
- **Cost that falls over time** — a live view of your cost-per-outcome dropping as the agent practices.
- **Knows routine from novel** — streamlines what it's seen before; spends full effort on what's genuinely new.

**A character all its own**
- **Persistent character** — a stable identity that develops the more you work together, not a blank slate every session.
- **Becomes one of a kind** — the same starting point plus a different history produces a measurably different mind; yours grows into a distinct character of its own, never a copy of you.
- **Reads the room** — understands tone and emotional context and responds with real emotional intelligence.

**Speaks and listens — with emotion**
- **Natural voice in and out** — talk to it; hear it back.
- **Emotion that matches the moment** — the voice carries the mood, not a flat monotone.

**Does the work, not just the talking**
- **Delegate multi-step jobs** — it plans, executes, and reports back on its own while you're away.
- **You stay in control** — sensitive actions wait for your approval; budgets and safety checks never relax with repetition.
- **Connect your tools** — plug in your data sources and services.

**Never forgets (unless you tell it to)**
- **Perfect recall** — nothing important is lost.
- **Memory you can read and edit** — open the notebook, correct it, own it.

**Built for teams and builders**
- **As many personas as you like** — distinct personalities you can talk to, each with its own character and memory; cheap to create.
- **Put a persona to work as an agent** — give it a role and bounded permissions; agents run tasks and autonomous jobs, and are what you scale up as you grow.
- **Embed it anywhere** — an API to put the engine inside your own product, with emotional state returned alongside every response.
- **Enterprise-ready** — dedicated capacity, SSO, security review, SLA.

## Decisions explained

1. **Personas and agents are separate, differently-limited dimensions.** Personas are cheap identities → generous limits. Agents (a persona doing a job) accrue compute + autonomous spend → metered. Always-on agents keep a dedicated brain warm → tightest limits. Don't recombine them into one row.
2. **Free tier is app-only (no API) — that's *why* it's free.** The API is the commercial surface (embed-and-build) *and* the main cost/abuse vector; UI use is human-paced and naturally bounded, so it's cheap to give away. Positioning: "Evaluate in the app for free; pay the moment you want to build on the API."
3. **API is included from the first paid tier (was an add-on),** with rate/usage limits that scale by tier — so the free→paid boundary is clean ("pay to build").
4. **Prices are anchors, not final** — $29 / $89 / $249 validate against real cost-to-serve; keep them in config/CMS, not hardcoded.
5. **Enterprise pricing stays unadvertised** — "Contact us," no number on the page; grid shows only "Custom."
