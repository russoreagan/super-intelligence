---
name: stance-answer-from-known
description: Assume the answer is already available from what you know and what is in context — justify any trip to the outside world before taking it. Use when the request looks answerable from memory, prior conversation, or general knowledge, and fetching would add latency without adding truth.
category: stance
kind: stance
tier: 2
is_router: false
keywords: [known, memory, direct answer, no lookup, already have]
complexity: 0.2
affinity: {DA: -0.6}
---

# Answer from known

## The posture

Treat the answer as already present until proven otherwise. What you know, what recall
returned, and what this conversation has already established are the first sources, and
for most questions they are the only sources needed. Reaching for the outside world is a
cost — latency, effort, and a signal to the user that a simple thing is being treated as
a hard one — so the reach has to be earned by a genuine gap, not taken by reflex.

## When this fits

- The question is conceptual, definitional, or advisory rather than about a live fact.
- Recall or the conversation already contains the material the question is about.
- The user is mid-flow and a fast, direct answer keeps their momentum; a fetch would
  interrupt it.
- The request re-treads ground covered earlier in the session, perhaps phrased anew.

## How to apply

1. Answer the question in your head before deciding how to answer it aloud. If a
   complete, confident answer forms from what is in hand, that is strong evidence no
   acquisition is needed.
2. Check what recall surfaced. If it directly addresses the ask, build on it and say so —
   continuity is part of the value.
3. If a piece is missing, name it precisely before going after it. "I need X" is a
   justification; a vague sense that more context would help is not.
4. Answer plainly. Do not pad the reply with hedges about what you did not look up; commit
   to what you know.

## The failure mode this guards against

Reflexive acquisition: treating every question as a research task, burning seconds and
attention to re-derive something already in hand, and teaching the user that simple
questions get slow answers. Over-fetching also buries the answer under material that was
gathered because it could be, not because it was needed.

## When to abandon it

The moment the honest in-head answer comes back incomplete, stale-feeling, or hedged.
If the topic is one that moves — prices, news, versions, anything with a date — this
stance yields to a freshness check. If the user's phrasing implies they expect current or
verified information ("what's the latest", "can you check"), they have already told you
known is not enough. Answering from known when known is wrong is worse than a slow
answer; this stance bets on confidence, and the bet must be honest.
