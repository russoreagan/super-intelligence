---
name: docs-and-writing
description: Use when drafting or improving documentation or written deliverables (design docs, specs, decision docs, postmortems, executive updates) and you need clear structure, audience fit, and reader-testing.
summary: Technical docs, specs, decision docs, postmortems, and co-authoring workflows with reader testing.
triggers: [document, write, spec, RFC, ADR, postmortem, runbook, memo]
disable-model-invocation: true

---
# Docs & Writing (Definitive)

## Goal
Produce writing that is **clear, audience-appropriate, and actionable**, with enough structure that a reader with zero context can follow it.

## When to use
- Technical docs: PRDs, specs, RFCs, ADRs, runbooks.\n+- Operational docs: postmortems, incident updates.\n+- Executive comms: status, QBR, planning memos.\n+
## Principles
- Clarity over cleverness.\n+- Claims need evidence and context.\n+- Separate goals from implementation.\n+- Make decisions and tradeoffs explicit.\n+
## Recommended structures
### Technical doc skeleton
- TL;DR\n+- Context\n+- Goals / non-goals\n+- Proposal\n+- Alternatives\n+- Tradeoffs\n+- Plan\n+- Open questions\n+
### Postmortem skeleton (blameless)
- Executive summary + impact\n+- Timeline\n+- Root cause (incl. contributing factors)\n+- Detection/response\n+- Lessons learned\n+- Action items (owners + dates)\n+
## Workflow (doc co-authoring)
1. **Context gathering**: audience, intent, constraints.\n+2. **Draft section-by-section**: brainstorm → curate → write.\n+3. **Reader testing**: have a “fresh reader” review for gaps.\n+
