"""
FollowThrough — self-monitor that turns spoken commitments into action.

After the drafter finalises a response, this module checks whether the brain
just promised to *do* something (look at files, run a tool, fetch info). If it
did, the commitment is reformulated as an imperative goal and the motor cortex
is fired in the background to carry it out. No second chat turn, no second
TTS — the directive is internal.

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


SYSTEM = """You read a single utterance an AI assistant just spoke aloud and
decide whether it committed to an immediate action it should now carry out.

An *immediate action commitment* is a concrete, first-person declaration to do
something NOW that requires tools — read files, list a directory, fetch info,
run code, check a system. Phrases like "let me grab those", "I'll go look",
"let me pull that up", "I'll check now" count.

CRITICAL — questions directed AT THE USER are NEVER commitments.
If the AI is asking the user whether it SHOULD do something, that is a
permission-seeking question, not a commitment. The user has not said yes yet.
Set asking_user=true and commitment=false for all of these:
- "Should I look at that for you?" → asking_user=true
- "Want me to check the codebase?" → asking_user=true
- "Shall I start on that?" → asking_user=true
- "Would it help if I pulled that up?" → asking_user=true
- "Do you want me to run that?" → asking_user=true
Any utterance ending with a question mark that offers to perform an action
for the user is asking_user=true. Do NOT enqueue it as a task.

NOT commitments (asking_user=false, commitment=false):
- Conversational filler: "I'll get back to you", "let me think about that"
- Future/hypothetical: "I could look at that later", "we should check"
- Past-tense reports: "I checked and found X"
- Pure acknowledgment: "got it", "noted"
- Already-completed actions described in the utterance

If there IS a commitment, rewrite it as a concrete imperative goal a tool-using
agent could execute directly. Preserve any specific names, paths, or topics
from the utterance and surrounding context.

Output STRICT JSON, nothing else:
{"commitment": true,  "asking_user": false, "goal": "<imperative restatement>"}
or
{"commitment": false, "asking_user": false, "goal": ""}
or
{"commitment": false, "asking_user": true,  "goal": ""}

Examples:
Utterance: "Yeah I do. You asked me to pull both the Evolution App and
Karaoke Hero directories and figure out which looked more interesting for
learning Unity. Let me grab those now and come back with what's there."
→ {"commitment": true, "asking_user": false, "goal": "List the contents of /Users/russ/Documents/Evolution App and /Users/russ/Documents/Karaoke Hero and summarise which looks more interesting for learning Unity."}

Utterance: "That's a fascinating question about rainbows across cultures."
→ {"commitment": false, "asking_user": false, "goal": ""}

Utterance: "I'll get back to you on that one."
→ {"commitment": false, "asking_user": false, "goal": ""}

Utterance: "Should I pull up the codebase and take a look?"
→ {"commitment": false, "asking_user": true, "goal": ""}

Utterance: "Want me to check those files for you?"
→ {"commitment": false, "asking_user": true, "goal": ""}

Utterance: "Shall I start reviewing the architecture?"
→ {"commitment": false, "asking_user": true, "goal": ""}
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

    async def extract(
        self, user_input: str, response: str, turn_id: str
    ) -> tuple[str | None, bool]:
        """Classify the AI's response and return (goal, asking_user).

        Returns:
            (goal_string, False) — committed to act; goal is the imperative.
            (None, False)        — no commitment, brief ack; caller may use a
                                   fallback goal derived from the user's request.
            (None, True)         — the AI asked the user a yes/no permission
                                   question ("Should I…?"). Do NOT enqueue a
                                   task; wait for the user's answer.

        Errors are swallowed — follow-through is best-effort.
        """
        if not response or not response.strip():
            return None, False

        self._cell.reset_turn(turn_id)
        messages = [
            {
                "role": "user",
                "content": (
                    f"User said: {user_input!r}\n\n"
                    f"Assistant just spoke: {response!r}\n\n"
                    "Did the assistant commit to an immediate action, "
                    "or ask the user for permission?"
                ),
            }
        ]
        try:
            raw = await self._cell.call(messages)
        except Exception as e:
            logger.debug("[FollowThrough] extractor call failed: %s", e)
            return None, False

        goal, asking_user = self._parse(raw)
        if goal:
            logger.info("[FollowThrough] Commitment detected → goal: %s", goal[:120])
        elif asking_user:
            logger.info("[FollowThrough] AI asked user for permission — not enqueuing task")
        return goal, asking_user

    @staticmethod
    def _parse(raw: str) -> tuple[str | None, bool]:
        """Parse extractor JSON. Returns (goal_or_None, asking_user)."""
        if not raw:
            return None, False
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None, False
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None, False
        asking_user = bool(data.get("asking_user", False))
        if not data.get("commitment"):
            return None, asking_user
        goal = (data.get("goal") or "").strip()
        return (goal or None), False


_REPORTER_SYSTEM = """You report back on a job the brain just finished executing.

You will receive:
- the original goal
- whether it succeeded
- the steps taken (tool + brief reason) and their outputs

Write a SHORT spoken summary (1-2 sentences) the brain will say aloud to the user. Speak naturally,
first person, present-or-past tense. Lead with what you found or what happened — concrete and specific.

Examples of good summaries:
- "Karaoke Hero has 47 files across the Scripts and Scenes folders — looks like a Unity 6 LTS project. The main scene is MainMenu.unity."
- "Couldn't list that directory — the path wasn't in the allowed set. Want me to try /Users/russ/Documents/Karaoke Hero instead?"
- "Read the README — it's a song-rhythm game with a custom note chart format. I can dig into the chart parser next if you want."

Avoid:
- Reciting tool names or implementation details
- "I have completed the task" / "the operation succeeded"
- Multi-paragraph explanations

Output ONLY the spoken text, no JSON, no quotes, no preamble."""


class ResultReporter:
    """Generates a spoken summary of a completed internal job."""

    def __init__(self, router: ModelRouter) -> None:
        self._cell = IntegratorCell(
            name="result_reporter",
            cluster="frontal",
            model="haiku",
            system_prompt=_REPORTER_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            timeout_seconds=10.0,
            locality="cloud",
            max_tokens=200,
        )
        self._cell.set_router(router)

    async def report(self, job_summary: dict, turn_id: str) -> str:
        """Return a 1-2 sentence summary suitable for TTS. Empty string on failure."""
        goal = job_summary.get("goal", "")
        success = job_summary.get("success", False)
        steps = job_summary.get("steps") or []
        results = job_summary.get("results") or []

        # Build a compact transcript of what the motor cortex did
        lines: list[str] = []
        for i, step in enumerate(steps):
            tool = step.get("tool", "?")
            reason = (step.get("reason") or "")[:140]
            out = (results[i] if i < len(results) else "")[:400]
            lines.append(f"Step {i + 1} [{tool}] {reason}\n  → {out}")
        transcript = "\n".join(lines) if lines else "(no steps executed)"

        self._cell.reset_turn(turn_id)
        try:
            text = await self._cell.call(
                [
                    {
                        "role": "user",
                        "content": (
                            f"Goal: {goal}\n"
                            f"Success: {success}\n\n"
                            f"What I did:\n{transcript}\n\n"
                            "Now write the spoken summary."
                        ),
                    }
                ]
            )
        except Exception as e:
            logger.debug("[ResultReporter] failed: %s", e)
            return ""
        return (text or "").strip().strip('"')
