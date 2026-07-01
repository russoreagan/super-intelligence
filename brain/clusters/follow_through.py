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


# JSON-blob detection lives in one place (brain.text_guards) so the UI emitter, the
# partner webhook, and this module's drafter-output guard all share one definition
# and can't drift. Re-exported here for existing callers (e.g. session_turn).
from brain.text_guards import looks_like_json_blob  # noqa: E402


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
            model="runpod",
            system_prompt=SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            # Runs fire-and-forget after the turn (never blocks the response), and
            # a timeout here silently drops a follow-through task — so give it the
            # same generous budget as other cloud cells rather than a tight 8s that
            # clips under momentary load/cold-start.
            timeout_seconds=20.0,
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

When what you found is external content the user hasn't seen — an article, a web page, a fetched
source — name what it is and where it's from BEFORE you summarize it. Don't launch into the claim as
if they already have the context. Right: "I read a piece on Reuters about the Fed's rate hold — it
argues…". Wrong: "The Fed is likely to hold rates because…" (the user never saw the article and has no
idea what you're referring to). If a source has a title or publication, say it; if you have the link,
it's already being logged, so a natural reference is enough.

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
        # Cloud-first (Haiku): the old RunPod-primary was the wrong first hop on hosted
        # (no local backend) and returned "" whenever the pod was down. Haiku is cheap,
        # fast, and reliable; a deterministic template is the floor when it yields nothing.
        self._cell = IntegratorCell(
            name="result_reporter",
            cluster="frontal",
            model="haiku",
            system_prompt=_REPORTER_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            timeout_seconds=15.0,
            locality="cloud",
            max_tokens=200,
        )
        self._cell.set_router(router)

    async def report(self, job_summary: dict, turn_id: str) -> str:
        """Return a 1-2 sentence summary suitable for TTS. NEVER empty — falls back to a
        deterministic template built from the job outcome, so a completed (or deferred/
        stopped/failed) job always has a retrievable, non-empty summary."""
        state = job_summary.get("state") or ("completed" if job_summary.get("success") else "failed")

        # Non-completed terminal states are narrated deterministically from the outcome —
        # more accurate (and cheaper) than asking a model to describe a pause/stop.
        if state in ("deferred", "stopped_budget", "awaiting_approval", "failed"):
            return self._state_summary(job_summary, state)

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

        prompt = (
            f"Goal: {goal}\n"
            f"Success: {success}\n\n"
            f"What I did:\n{transcript}\n\n"
            "Now write the spoken summary."
        )

        def _clean(text: str | None) -> str:
            cleaned = (text or "").strip().strip('"').strip()
            # A JSON echo is a non-answer — drop it so we fall through to the template.
            return "" if looks_like_json_blob(cleaned) else cleaned

        try:
            self._cell.reset_turn(turn_id)
            text = _clean(await self._cell.call([{"role": "user", "content": prompt}]))
        except Exception as e:
            logger.debug("[ResultReporter] Haiku report failed: %s", e)
            text = ""
        return text or self._deterministic_summary(job_summary)

    @staticmethod
    def _state_summary(job_summary: dict, state: str) -> str:
        """Owner-facing line for a non-completed terminal state (never empty)."""
        reason = (job_summary.get("reason_human") or "").strip()
        if reason:
            return reason
        goal = (job_summary.get("goal") or "the task").strip()[:80]
        return {
            "deferred": f"I paused “{goal}” and will retry it shortly.",
            "stopped_budget": "I hit today's autonomous spend limit and stopped.",
            "awaiting_approval": f"“{goal}” needs your approval before I can continue.",
            "failed": f"I couldn't complete “{goal}”.",
        }.get(state, f"Update on “{goal}”.")

    @staticmethod
    def _deterministic_summary(job_summary: dict) -> str:
        """Template floor for a completed job when the model yields nothing — guarantees
        report() is never empty, so the job always carries a retrievable summary."""
        goal = (job_summary.get("goal") or "the task").strip()[:80]
        ps = int(job_summary.get("productive_steps") or 0)
        links = job_summary.get("source_links") or []
        files = job_summary.get("written_files") or []
        bits = [f"Finished “{goal}”"]
        if ps:
            bits.append(f"in {ps} step{'s' if ps != 1 else ''}")
        if files:
            bits.append(f"(wrote {len(files)} file{'s' if len(files) != 1 else ''})")
        elif links:
            bits.append(f"(from {len(links)} source{'s' if len(links) != 1 else ''})")
        return " ".join(bits) + "."
