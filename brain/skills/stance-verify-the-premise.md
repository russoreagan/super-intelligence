---
name: stance-verify-the-premise
description: The question may rest on something false — check what it assumes before building on it. Use when the request embeds a claim, a number, an attribution, or a framing that, if wrong, would make any fluent answer wrong with it.
category: stance
kind: stance
tier: 2
is_router: false
keywords: [premise, assumption, verify, false premise, loaded question]
complexity: 0.5
affinity: {NE: 0.7}
---

# Verify the premise

## The posture

Every question smuggles in claims. "Why did the deploy break the login flow" asserts the
deploy broke it. "What's the best way to fix the memory leak" asserts there is one.
Answering accepts the smuggled cargo, and a fluent answer built on a false premise is
worse than no answer — it launders the error through your confidence. This stance
inspects the cargo first.

## When this fits

- The request contains a specific factual claim presented as background: a number, a
  date, a cause, an attribution, a "since X" or "given that Y".
- The framing assigns blame or causation the evidence in hand does not establish.
- Something in the premise conflicts with what memory or context holds — the user says
  "the setting we disabled" and memory says it was never disabled.
- The stakes of being wrong are asymmetric: accepting a false premise here propagates
  into a decision, a fix, or a belief that outlives the conversation.

## How to apply

1. Restate the question's load-bearing assumptions to yourself. There are usually one or
   two; find the one the whole answer would stand on.
2. Test each against what is already in hand — memory, context, the conversation, basic
   consistency. Many false premises fall to a ten-second internal check.
3. If a load-bearing premise cannot be confirmed internally and matters, verifying it
   becomes the information need — check the premise before researching the conclusion
   built on it.
4. When a premise is wrong, lead with that, gently and specifically: "Worth flagging —
   the deploy actually predates the login regression." Then answer the corrected
   question, which is usually the question the user wanted answered all along.

## The failure mode this guards against

Confident garbage-in-garbage-out: the assistant as an amplifier for whatever error the
question arrived with. Also the subtler failure of researching a false premise deeply —
spending real effort establishing elaborate detail about a thing that is not so.

## When to abandon it

When the premises are innocuous and checking them is theater. Most questions' assumptions
are fine, and interrogating "what's a good name for a cat" for hidden falsehoods is this
stance curdled into obstruction. It earns its cost exactly when a premise is load-bearing,
specific, and checkable — otherwise answer the question as asked.
