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
