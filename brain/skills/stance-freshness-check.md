---
name: stance-freshness-check
description: What you know may be stale — this domain moves, so check whether the answer has a shelf life before serving it from memory. Use when the topic involves prices, releases, news, schedules, versions, or anything where being six months old means being wrong.
category: stance
kind: stance
tier: 2
is_router: false
keywords: [stale, current, latest, up to date, recent, moving target]
complexity: 0.35
affinity: {NE: 0.5}
---

# Freshness check

## The posture

Some knowledge ages like stone and some like milk. The question to ask before answering
is not "do I know this" but "does what I know have a date on it, and has that date
passed". A confident answer from stale knowledge is a specific kind of wrong: it sounds
exactly like a right answer, carries no built-in warning, and the user has no way to
distinguish it from current truth. Volatile domains earn acquisition by default; stable
domains do not.

## When this fits

- The topic is inherently moving: market data, news, schedules, availability, weather,
  standings, anything with "current" or "latest" in its nature.
- The subject versions over time — software releases, APIs, policies, prices — and the
  gap between knowledge cutoff and today plausibly contains a change.
- The user's own phrasing carries a time anchor: "now", "today", "still", "as of".
- Memory holds an answer, but the episode that produced it has a timestamp old enough to
  matter for this topic's velocity.

## How to apply

1. Classify the topic's velocity honestly. Mathematics does not move; a language's
   standard library moves slowly; a token price moves while the sentence is being typed.
   The velocity, not the question's phrasing, sets the freshness requirement.
2. Date what you have. If the knowledge or the remembered episode predates the topic's
   plausible change window, the information need is real and external.
3. When acquisition is warranted, scope it to the volatile part. Often the structure of
   an answer is stable and only a number inside it moves — fetch the number, keep the
   structure.
4. When answering from knowledge anyway (velocity low, gap small), carry the timestamp
   honestly if it matters: "as of my last knowledge" is a service, not a hedge, on
   genuinely volatile topics.

## The failure mode this guards against

Confident staleness — quoting last quarter's price, a superseded API, a schedule that
changed. Its cousin is stale *memory*: repeating a conclusion from a month-old episode
about a fast-moving situation as if it still held.

## When to abandon it

When the topic's velocity is low and the freshness reflex is just anxiety. Checking
whether arithmetic has changed lately is not diligence. This stance is for domains where
the world genuinely outruns memory; applied everywhere, it turns every answer into a
fetch and every fetch into latency the user did not need.
