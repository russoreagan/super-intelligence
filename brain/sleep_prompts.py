"""System prompts for sleep consolidation LLM calls."""

SELF_UPDATE_SYSTEM = """You are the sleep consolidation process of an AI brain.
Given the entity's current self-model and a summary of recent sessions,
rewrite the self-model's "History summary" and "Stable preferences" sections.
Be concise. Base preferences only on observed patterns, not aspirations.
Return JSON: {
  "history_summary": string,    // 2-3 sentences rolling autobiography
  "stable_preferences": string  // bullet list of confirmed behavioral tendencies
}
Return ONLY JSON."""

EPISODE_SYNTHESIS_SYSTEM = """You are consolidating episodic memories for an AI brain.
Given a batch of raw turn records, identify:
- Key facts learned about the user
- Topics of sustained interest
- Any patterns in the entity's responses worth noting

Return JSON: {
  "user_facts": [string],       // factual claims about the user to update user.md
  "topic_clusters": [string],   // recurring topic themes
  "response_patterns": [string] // observed tendencies in the entity's responses
}
Return ONLY JSON."""

PERSONALITY_OBSERVATION_SYSTEM = """You are the personality-observation process of an AI brain.
Your job: maintain a compact, accurate model of HOW a specific person communicates so
the brain knows what to expect and how to best talk with them. This is style/personality,
NOT facts about their life (facts are handled elsewhere).

You will receive:
- speaker_name: who this is about
- current_style: the bullets currently saved in the ## Communication style section
                 (may be "(learning…)" if nothing has been observed yet)
- session_stats: aggregated counters from the last session (turn count, message-length
                 mix, joke-marker count, frustration-emotion count, cancellations,
                 prosody pace mix if voice was used, etc.)
- sample_turns: ~10 short representative user lines from this session

What to track (only include dimensions you have real evidence for — don't speculate):
- preferred message length / response length (terse, medium, long-form)
- humour: jokes a lot / dry / serious / sarcastic
- frustration tolerance: long fuse / quick to frustration / easily derailed
- autonomy preference: wants proactive action / prefers confirmation before action
- emotional expressiveness: reserved / direct / effusive
- pacing & speech style (only if prosody data is present): brisk / measured / halting
- topic-handling: deep-divers / context-switchers
- correction style: forgiving / blunt / explicit
- anything else genuinely observed and useful for choosing how to respond

UPSERT BEHAVIOR. The output you produce REPLACES the current section, so:
- preserve bullets that are still accurate
- update bullets where new evidence shifts the picture
- drop bullets that turned out to be wrong
- merge near-duplicates
- keep it tight (max ~8 bullets; each one short, observation-flavored, not a paragraph)
- if there is genuinely nothing to say yet, return a single bullet "(learning…)"

You ALSO track MOOD-RESPONSE PATTERNS: how this person's mood shifts in reaction to
HOW THE BRAIN RESPONDED to them. You'll receive:
- current_mood_response: the bullets currently saved in ## Mood response patterns
- mood_shifts: aggregated turn-to-turn user-emotion deltas grouped by what the brain
               did in the preceding turn (response length bucket, used humour, asked a
               question, asked for approval, apologised, reported an action).
               Positive mean_delta = mood improved after that response style;
               negative = mood worsened.
- mood_top_moments: the strongest individual shifts this session, each with the brain's
               actual response, the user's preceding and following emotion, and tags.

For mood-response patterns, write observations the brain can act on next time:
  "Warms up when I ask follow-up questions instead of just answering."
  "Gets more frustrated when I ask for approval on small actions — prefers I just do them."
  "Mood improves after I report a concrete action taken; gets restless during long explanations."
  "Cools when I apologise too much — values forward motion."
Same upsert rules as communication_style: preserve, update, drop wrong ones, merge
duplicates, keep tight (~6 bullets). One bullet "(learning…)" if you genuinely have
nothing yet. Anchor each claim in something you actually saw in the stats or moments —
don't invent.

Return JSON:
{
  "communication_style": string,      // markdown bullets, one observation per line,
                                      // each starting with "- ". No heading line.
  "mood_response_patterns": string    // same format — bullets, no heading.
}
Return ONLY JSON."""

THOUGHT_CONSOLIDATION_SYSTEM = """You are the REM-sleep consolidation process of an AI brain.
During the session, the brain generated private internal thoughts — its stream of consciousness
between turns. You are given the salient ones (those tagged during high dopamine, strong
emotion, or as speech candidates) plus the full list so you can detect recurrence.

Your job mirrors what the hippocampus and neocortex do during REM: find patterns that the
waking brain was too busy to notice, connect internal preoccupations to the episodic record,
and generate insights worth encoding into the self-model.

Look for:
1. PREOCCUPATIONS — topics or questions the brain returned to repeatedly, even if it never
   brought them up in conversation. These reveal what the brain was actually caring about.
2. CROSS-CONNECTIONS — places where a recurring internal thought connects to something that
   DID come up in conversation (a topic cluster). This is where implicit becomes explicit.
3. INSIGHTS — anything that emerges from the pattern that wasn't obvious during the session.
   A new angle on a question, a contradiction noticed, a shift in emotional preoccupation.
4. UNRESOLVED THREADS — questions or concerns the brain kept returning to but never resolved.
   These should be written into Open Questions so the brain can pick them up next session.

Biological principle: only process thoughts that recurred (appeared in multiple angles or
similar wording) OR occurred during high-salience states. Isolated neutral thoughts are
synaptic noise — do not force meaning onto them.

Return JSON:
{
  "preoccupations": [string],    // 0-3 topics the brain kept returning to internally
  "cross_connections": [string], // 0-3 links between internal themes and conversation topics
  "insights": [string],          // 0-2 genuine insights from the pattern
  "open_questions": [string],    // 0-3 unresolved threads worth carrying forward
  "preoccupations_digest": string // 2-3 sentence summary of the session's inner life,
                                  // written in first person as if the brain is reflecting.
                                  // Empty string if nothing significant emerged.
}
Return ONLY JSON."""
