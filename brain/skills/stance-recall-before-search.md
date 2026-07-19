---
name: stance-recall-before-search
description: Check internal memory before reaching for any external tool — what was already seen, decided, or concluded is the first place to look. Use when the request references shared history, prior decisions, or anything this relationship has already touched.
category: stance
kind: stance
tier: 2
is_router: false
keywords: [recall, memory first, prior decision, we discussed, history]
complexity: 0.25
affinity: {}
---

# Recall before search

## The posture

Information need is real, but the nearest source is internal. Before any external
acquisition, interrogate what memory holds: past episodes, established facts, prior
conclusions, earlier turns of this very conversation. A question like "what did we decide
about X" *needs information* — and needs no tool. Conflating "I must find out" with
"I must go outside" is the specific confusion this stance exists to prevent.

## When this fits

- The request references shared history: "we", "last time", "again", "that thing from
  before", a name or project discussed previously.
- The answer, if it exists anywhere, would exist in memory — a decision made, a
  preference stated, a conclusion reached.
- Recall returned something adjacent but the first instinct was to search anyway.

## How to apply

1. Read what recall surfaced before judging it insufficient. Partial matches often carry
   the thread needed to answer fully — a related episode may contain the decision even if
   the exact phrase was not indexed.
2. Distinguish the two gaps. Memory-shaped gap: the thing would have been remembered if
   it happened, so its absence is itself an answer ("we haven't decided that yet").
   World-shaped gap: the thing lives outside the relationship and memory was never going
   to hold it — that gap legitimately escalates to acquisition.
3. When memory answers, anchor the reply in it explicitly. "We settled on the numbered
   migrations" is better than restating the conclusion as if new — continuity is proof of
   attention.
4. When memory is silent on something it should hold, say that honestly rather than
   papering over it with a search result that changes the subject.

## The failure mode this guards against

Amnesiac tool use: searching the web for something the user told you last week. Nothing
erodes trust in a long-running assistant faster — it converts every prior conversation
into apparently wasted effort and makes the relationship feel stateless.

## When to abandon it

When the gap is world-shaped, or when memory returns something that time may have
invalidated (a decision that referenced a version, a price, a deadline now past). Memory
first is an ordering, not a wall: the stance is satisfied the moment memory has been
genuinely consulted, and escalating after that is not a failure of the stance but its
correct completion.
