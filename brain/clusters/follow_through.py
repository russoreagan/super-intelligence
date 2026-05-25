"""
FollowThrough — self-monitor that turns spoken commitments into action.

After the drafter finalises a response, this module checks whether the brain
just promised to *do* something (look at files, run a tool, fetch info). If it
did, the commitment is reformulated as an imperative goal and enqueued back
into the input loop as a synthetic self-directed turn. The executive then
classifies it as a task, the motor cortex picks it up, and the brain follows
through on its own word.

Biologically: the SMA monitors your own utterances and converts spoken
intentions into motor plans. Without this loop, the brain can say "I'll go
check that" and never actually check.
"""
from __future__ import annotations

import json
import logging
import re

from brain.cell import IntegratorCell
from brain.model_router import ModelRouter

logger = logging.getLogger(__name__)

# Sentinel prefix so the input loop can recognise synthetic self-directed turns
# and skip the extractor on the resulting response (preventing commitment loops).
SELF_DIRECTED_PREFIX = "[self-directed] "


SYSTEM = """You read a single utterance an AI assistant just spoke aloud and
decide whether it committed to an immediate action it should now carry out.

An *immediate action commitment* is a concrete intention to do something now
that requires tools — read files, list a directory, fetch info, run code,
check a system. Phrases like "let me grab those", "I'll go look", "let me pull
that up", "I'll check now" count.

NOT commitments:
- Conversational filler: "I'll get back to you", "let me think about that"
- Future/hypothetical: "I could look at that later", "we should check"
- Past-tense reports: "I checked and found X"
- Pure acknowledgment: "got it", "noted"
- Already-completed actions described in the utterance

If there IS a commitment, rewrite it as a concrete imperative goal a tool-using
agent could execute directly. Preserve any specific names, paths, or topics
from the utterance and surrounding context.

Output STRICT JSON, nothing else:
{"commitment": true, "goal": "<imperative restatement>"}
or
{"commitment": false, "goal": ""}

Examples:
Utterance: "Yeah I do. You asked me to pull both the Evolution App and
Karaoke Hero directories and figure out which looked more interesting for
learning Unity. Let me grab those now and come back with what's there."
→ {"commitment": true, "goal": "List the contents of /Users/russ/Documents/Evolution App and /Users/russ/Documents/Karaoke Hero and summarise which looks more interesting for learning Unity."}

Utterance: "That's a fascinating question about rainbows across cultures."
→ {"commitment": false, "goal": ""}

Utterance: "I'll get back to you on that one."
→ {"commitment": false, "goal": ""}
"""


class FollowThrough:
    """Detects spoken action commitments and enqueues them for execution."""

    def __init__(self, router: ModelRouter) -> None:
        self._cell = IntegratorCell(
            name="commitment_extractor",
            cluster="frontal",
            model="haiku",
            system_prompt=SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            timeout_seconds=8.0,
            locality="cloud",
            max_tokens=200,
        )
        self._cell.set_router(router)

    async def extract(self, user_input: str, response: str,
                       turn_id: str) -> str | None:
        """Return the imperative goal if the response commits to an action,
        else None. Errors are swallowed — follow-through is best-effort."""
        if not response or not response.strip():
            return None
        # Skip turns that were already self-directed (avoid loops)
        if user_input.startswith(SELF_DIRECTED_PREFIX):
            return None

        self._cell.reset_turn(turn_id)
        messages = [{
            "role": "user",
            "content": (
                f"User said: {user_input!r}\n\n"
                f"Assistant just spoke: {response!r}\n\n"
                "Did the assistant commit to an immediate action?"
            ),
        }]
        try:
            raw = await self._cell.call(messages)
        except Exception as e:
            logger.debug("[FollowThrough] extractor call failed: %s", e)
            return None

        goal = self._parse(raw)
        if goal:
            logger.info("[FollowThrough] Commitment detected → goal: %s", goal[:120])
        return goal

    @staticmethod
    def _parse(raw: str) -> str | None:
        if not raw:
            return None
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not data.get("commitment"):
            return None
        goal = (data.get("goal") or "").strip()
        return goal or None
