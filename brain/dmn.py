"""
Default Mode Network — "stream of consciousness" (William James).
Runs between turns. The brain thinks even when not addressed.
Publishes to stream.* topic.

Three sub-processes:
1. Internal monologue: cheap LLM generates a thought every N seconds
2. Hippocampal consolidation: reviews recent episodes for integration
3. Hypothalamic prediction: simulates the user's likely next message

v0.2 feature — only active when BRAIN_DMN=true in env.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import time
from collections import deque

from pathlib import Path

from brain.bus import Bus
from brain.cell import IntegratorCell
from brain.dmn_prompts import (
    ANTICIPATOR_SYSTEM,
    BRIDGE_SYSTEM,
    JUDGE_SYSTEM,
    MONOLOGUE_SYSTEM,
    PLANNER_SYSTEM,
    PREFETCHER_SYSTEM,
    SIMULATION_SYSTEM,
)
from brain.emotion_hierarchy import valence_of
from brain.model_router import ModelRouter
from brain.neuron import SwitchNeuron
from brain.second_brain.store import SECOND_BRAIN_ROOT
from brain.settings import settings
from brain.utils import get_idle_seconds

DEFERRED_THOUGHTS_PATH = SECOND_BRAIN_ROOT / "deferred_thoughts.md"
PROPOSALS_DIR = SECOND_BRAIN_ROOT / "proposals"

# English function/stop words — filtered out before Jaccard overlap so that
# common scaffolding ("the user has been...") doesn't make every thought look
# like a duplicate. This is DIFFERENT from voice_bridge.bleed_overlap, which
# is tuned for TTS-bleed detection (needs to catch articles).
_STOP_WORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be", "been",
    "being", "am", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "must", "shall",
    "to", "of", "in", "on", "at", "by", "for", "with", "from", "up", "out", "as",
    "into", "through", "after", "before", "between", "during", "under", "over",
    "about", "against", "without",
    "this", "that", "these", "those", "it", "its", "itself",
    "he", "she", "they", "them", "their", "theirs", "him", "her", "his", "hers",
    "i", "me", "my", "mine", "myself", "we", "us", "our", "ours", "ourselves",
    "you", "your", "yours", "yourself",
    "what", "which", "who", "whom", "whose", "if", "then", "than", "because",
    "so", "not", "no", "yes", "very", "just", "only", "some", "any", "all", "each",
    "much", "many", "more", "most", "other", "another", "such", "same", "too",
    "again", "here", "there", "when", "where", "why", "how", "now", "still",
    "even", "also", "like", "feel", "feels", "feeling",
    # Domain-saturated tokens — these appear in nearly every thought and
    # would otherwise dominate the Jaccard score
    "user", "thought", "thinking", "wonder", "wondering", "notice", "noticing",
})


def _content_word_overlap(a: str, b: str) -> float:
    """Jaccard overlap on CONTENT words only.

    Tokens shorter than 3 chars or in _STOP_WORDS are dropped. This is the
    similarity function used to reject near-duplicate thoughts.
    """
    if not a or not b:
        return 0.0
    def content_tokens(s: str) -> set[str]:
        return {w for w in re.findall(r"[a-z']+", s.lower())
                if len(w) >= 3 and w not in _STOP_WORDS}
    ta = content_tokens(a)
    tb = content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

logger = logging.getLogger(__name__)

DMN_INTERVAL = float(os.environ.get("BRAIN_DMN_INTERVAL", "15"))  # seconds between thoughts
DMN_ENABLED = os.environ.get("BRAIN_DMN", "false").lower() == "true"

# How similar a new thought can be to recent ones before we discard it as
# redundant. Word-set Jaccard — 0.35 catches near-duplicates while still
# letting genuinely different thoughts through. (Semantic angle tracking,
# added below, handles same-idea-different-words cases the word check misses.)
DMN_OVERLAP_THRESHOLD = float(os.environ.get("BRAIN_DMN_OVERLAP_THRESHOLD", "0.35"))
# How many recent thoughts/angles to show the LLM as context (variety pressure).
# Larger window = model is told about more prior territory to avoid.
DMN_RECENT_THOUGHTS = int(os.environ.get("BRAIN_DMN_RECENT_THOUGHTS", "10"))
# How many of those recent thoughts to actually COMPARE against for hard dedup.
# Narrower than DMN_RECENT_THOUGHTS so thoughts can recur after a gap — the LLM
# context pressure (above) already discourages literal repeats. Comparing against
# all 10 causes over-suppression on focused topics after just 3-4 thoughts.
DMN_DEDUP_WINDOW = int(os.environ.get("BRAIN_DMN_DEDUP_WINDOW", "4"))
# How many recent thought angles to block (separate from text-overlap window).
DMN_RECENT_ANGLES = int(os.environ.get("BRAIN_DMN_RECENT_ANGLES", "8"))

# Words that signal a thought is turning inward (self-referential / introspective).
# Inward thoughts apply a small GABA bump — self-monitoring has a cost, which
# makes extended self-reflection naturally self-limiting via neuromod decay.
# Outward thoughts apply a small DA + ACh bump (engagement / novelty reward).
_INWARD_MARKERS: frozenset[str] = frozenset({
    "existence", "nature", "conscious", "consciousness", "awareness", "aware",
    "experience", "purpose", "meaning", "identity", "what i am", "who i am",
    "my own", "myself", "introspect", "do i feel", "am i", "whether i",
    "what it means", "my nature", "my existence", "my purpose",
})

# Fallback neuromod deltas when the model doesn't emit chem_delta.
_INWARD_DELTA: dict[str, float] = {"GABA": 0.04}
_OUTWARD_DELTA: dict[str, float] = {"DA": 0.02, "ACh": 0.02}

# Channels the model is allowed to nudge, and the max absolute delta per tick.
# Keeps a rogue thought from spiking chemistry into an unrecoverable state.
_CHEM_ALLOWED: frozenset[str] = frozenset(
    {"DA", "ACh", "GABA", "Glu", "NE", "5HT", "CORT", "OXT", "AEA"}
)
_CHEM_MAX_DELTA: float = 0.06

# Social-discomfort emotion labels — these come from metacognition appraisal,
# not pure neuromod state. They bias the speak gate toward DEFLECTION more
# strongly than raw negative valence: when feeling embarrassed or apologetic
# a person often reaches for a tangent to escape the moment, not just stays
# quiet. Membership is the override signal alongside the numeric valence.
_DEFLECTION_OVERRIDES: frozenset[str] = frozenset({
    "embarrassed", "apologetic", "ashamed", "shy",
    "frustrated", "irritated", "defensive", "sarcastic",
    "disappointed", "somber", "melancholy",
})


def _classify_thought(thought: str) -> str:
    """Return 'inward' if the thought is self-referential, else 'outward'."""
    lower = thought.lower()
    return "inward" if any(m in lower for m in _INWARD_MARKERS) else "outward"


class DefaultModeNetwork:
    def __init__(self, bus: Bus, router: ModelRouter,
                 hippocampus=None, parietal=None, obs=None) -> None:
        self._bus = bus
        self._router = router
        self._hippocampus = hippocampus
        self._parietal = parietal
        self._obs = obs
        self._running = False
        self._last_context: str = ""
        self._thought_count = 0
        # Rolling window of recent thoughts — used both to show the LLM what
        # it just said (so it varies) AND to reject near-duplicates that slip
        # through. Cap at DMN_RECENT_THOUGHTS so older thoughts can recur.
        self._recent_thoughts: deque = deque(maxlen=DMN_RECENT_THOUGHTS)
        # Semantic angle labels from recent thoughts — blocks same-territory
        # ideas even when they use completely different words.
        self._recent_angles: deque = deque(maxlen=DMN_RECENT_ANGLES)
        self._suppressed_count = 0

        self._monologue_cell = IntegratorCell(
            name="monologue",
            cluster="dmn",
            model="local",
            system_prompt=MONOLOGUE_SYSTEM,
            topics=["stream.thought"],
            max_calls_per_turn=1,
            locality="local",
        )
        self._monologue_cell.set_router(router)

        self._simulation_cell = IntegratorCell(
            name="user_simulator",
            cluster="dmn",
            model="local",
            system_prompt=SIMULATION_SYSTEM,
            topics=["stream.prediction"],
            max_calls_per_turn=1,
            locality="local",
        )
        self._simulation_cell.set_router(router)

        self._anticipator_cell = IntegratorCell(
            name="anticipator",
            cluster="dmn",
            model="local",
            system_prompt=ANTICIPATOR_SYSTEM,
            topics=["stream.anticipation"],
            max_calls_per_turn=1,
            locality="local",
        )
        self._anticipator_cell.set_router(router)

        self._prefetcher_cell = IntegratorCell(
            name="prefetcher",
            cluster="dmn",
            model="local",
            system_prompt=PREFETCHER_SYSTEM,
            topics=["stream.prefetch"],
            max_calls_per_turn=1,
            locality="local",
        )
        self._prefetcher_cell.set_router(router)

        # Planner cell — runs when the monologue sets plan=true. Uses the larger
        # local-general model for structured reasoning. Writes a proposal doc;
        # never executes work. Background mode is set by the caller.
        self._planner_cell = IntegratorCell(
            name="planner",
            cluster="dmn",
            model="local-general",
            system_prompt=PLANNER_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            locality="local",
            timeout_seconds=90.0,  # planning takes longer than a thought tick
        )
        self._planner_cell.set_router(router)

        # Judge cell — runs once per candidate evaluation in the speak gate.
        self._judge_cell = IntegratorCell(
            name="speak_judge",
            cluster="dmn",
            model="local",
            system_prompt=JUDGE_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            timeout_seconds=20.0,
            locality="local",
        )
        self._judge_cell.set_router(router)

        # Bridge rewriter — runs LOCALLY via Ollama (no paid LLM calls). Used
        # only when a candidate is approved AND it's a clear tangent
        # (topic_overlap < threshold). Smooths the change-of-subject so it
        # doesn't feel abrupt. If Ollama is down, returns "" → gate uses
        # the original phrasing. Graceful fallback, never blocking.
        self._bridge_cell = IntegratorCell(
            name="speak_bridge",
            cluster="dmn",
            model="local-free",  # plain-text output — no JSON grammar constraint
            system_prompt=BRIDGE_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            locality="local",    # locked to local — must never escape to a cloud API
            timeout_seconds=12.0,  # Ollama can be slow on cold load
        )
        self._bridge_cell.set_router(router)

        # Predicted next input (used by temporal lobe predictor as a warm hint)
        self.predicted_next: dict | None = None
        # When the brain's last response ended with a question, the DMN runs
        # an anticipator that pre-generates response sketches for likely
        # answers. Cleared once the user actually replies.
        self.last_was_question: bool = False
        self.last_assistant_message: str = ""
        # Most recent anticipation scenarios — surfaced to next turn's drafter
        # as "you already started thinking about this" context.
        self.anticipations: list[dict] = []
        # Proactively fetched context: list of {topic, snippets} the prefetcher
        # pulled from memory while idle. Consumed by next turn's drafter.
        self.prefetched: list[dict] = []
        # Candidates flagged by the monologue as potentially worth speaking.
        # The speak gate (driven from run.py) drains this, applies heuristics,
        # consults the judge LLM, and either commits to _proactive_q ("yes"),
        # returns to this queue ("wait"), or discards ("drop"). Each entry:
        #   {thought, spoken, angle, created_ts, attempts}
        self._candidate_q: deque = deque(maxlen=8)
        # Spoken utterances cleared by the gate — drained by run.py and spoken.
        # maxlen=2 so stale proactive utterances don't pile up between turns.
        self._proactive_q: deque = deque(maxlen=2)
        # Self-initiated task goals — drained by run.py task worker.
        # maxlen=4 so idle reasoning doesn't flood the queue.
        self._self_task_q: deque = deque(maxlen=4)
        self._loop_task: asyncio.Task | None = None

        # Programmatic emotion + relationship state, set by update_context().
        # Kept as separate fields (not buried in _last_context string) so the
        # judge prompt can pass them as structured inputs.
        self._last_emotion: str = "neutral"
        self._last_speaker_name: str | None = None
        self._last_affection_score: int = 0
        self._last_familiarity: str = "new"

        # Active projects manifest — loaded from open_questions.md "Projects
        # assigned by Russ" section. Injected into every monologue tick so the
        # DMN knows what work is pre-authorized and can task/propose accordingly.
        self._last_projects: str = ""

        # Idle-gate switch — gates DMN tick firing on the chemistry snapshot.
        # 5HT + OXT (relaxed/safe) lower the threshold → mind-wanders more
        # readily. NE (alertness) and GABA (defensive) raise it → suppress
        # DMN when the brain needs to be attentive. The switch coexists with
        # the existing _tick_skip_probability heuristic; it provides a hard
        # chemistry-driven block, while the probability adds stochastic flow.
        self._idle_gate = SwitchNeuron(
            "idle_gate", "dmn", polarity="excitatory",
            threshold=0.5,
            modulators={"5HT": -0.10, "OXT": -0.05, "NE": +0.10, "GABA": +0.10},
        )

        # Session thought buffer — hippocampal-tagging analog.
        # Every accepted thought is appended here with its neuromod context and
        # a salience flag. Salient thoughts are those generated during elevated
        # DA / strong emotion, or flagged for speech (they passed the relevance
        # bar). At sleep consolidation, the buffer is handed to the REM-style
        # pass so recurring preoccupations and cross-connections can be found.
        # Non-salient thoughts are the equivalent of synaptic noise: they might
        # inform the context during the session but don't need to be persisted.
        self._session_thought_buf: list[dict] = []
        _SESSION_THOUGHT_LIMIT = 50  # keep last 50 thoughts; older are discarded
        self._session_thought_limit = _SESSION_THOUGHT_LIMIT

    async def start(self, session_id: str) -> None:
        self._session_id = session_id
        self._running = True
        active = float(settings.get("dmn_interval") or DMN_INTERVAL)
        idle = float(settings.get("dmn_idle_interval") or active * 3)
        logger.info(
            "[Background reflection] Active (continuous) — inner monologue every "
            "%.0fs active / %.0fs when user is OS-idle",
            active, idle,
        )
        self._loop_task = asyncio.create_task(self._loop())

    def pause(self) -> None:
        """No-op in the continuous-thought design.

        Earlier versions paused the DMN at turn start so the inner monologue
        was silent while a turn ran. We now let thoughts keep flowing during
        turns — what gets gated is whether to SPEAK a thought, not whether to
        have it. This method is kept for backward compatibility with call
        sites in run.py."""
        return

    def resume(self) -> None:
        """No-op in the continuous-thought design. See pause() for context."""
        return

    async def shutdown(self) -> None:
        """Cancel the background loop. Called at session shutdown."""
        self._running = False
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
        self._loop_task = None

    def recent_thoughts(self, n: int = 5) -> list[str]:
        """Return the last N internal thoughts the brain had between turns.
        Consumed by run.py to seed the next turn's drafter context — so the
        entity can reference what it was musing about when the user speaks."""
        return list(self._recent_thoughts)[-n:]

    def session_thoughts(self) -> list[dict]:
        """Return the full session thought buffer for sleep consolidation.
        Each entry: {thought, angle, direction, speak_flagged, emotion,
                     neuromod, salient, ts}.
        Called once at session end by run.py and passed to
        SleepConsolidation.consolidate() for the REM-style thought pass."""
        return list(self._session_thought_buf)

    def recent_thoughts_tagged(self, n: int = 5) -> list[dict]:
        """Return the last N thoughts with their speak_flagged signal.
        Each entry: {thought: str, speak_flagged: bool}.
        Used by run.py → frontal.py so the drafter knows which thoughts the
        brain was already leaning toward voicing (speak gate flagged them as
        candidates) versus ones that stayed fully internal."""
        buf = self._session_thought_buf
        if not buf:
            # Fallback: if the buffer is empty (DMN not running), derive from
            # the deque of plain strings with no speak flag.
            return [{"thought": t, "speak_flagged": False}
                    for t in list(self._recent_thoughts)[-n:]]
        entries = buf[-n:]
        return [{"thought": e["thought"], "speak_flagged": bool(e.get("speak_flagged"))}
                for e in entries]

    def note_last_response(self, response: str) -> None:
        """Called by run.py after each turn end. Records whether the entity's
        last message ended with a question — if so, the DMN's next tick will
        also run the anticipator to pre-prepare for likely user answers."""
        self.last_assistant_message = (response or "").strip()
        # Simple heuristic: ends with '?' OR final clause looks like a Q
        text = self.last_assistant_message
        self.last_was_question = (
            text.endswith("?")
            or any(text.lower().endswith(p) for p in
                   ("right?", "yeah?", "huh?", "ok?", "okay?", "yes?"))
        )
        # New turn arriving = stale anticipations go away (the user already replied)
        self.anticipations = []

    def take_anticipations(self) -> list[dict]:
        """Pop the anticipation scenarios so they're consumed exactly once."""
        out, self.anticipations = self.anticipations, []
        return out

    def take_prefetched(self) -> list[dict]:
        """Pop the prefetched-context items so they're consumed exactly once."""
        out, self.prefetched = self.prefetched, []
        return out

    def take_self_task(self) -> str | None:
        """Drain one self-initiated task goal, or None if queue is empty."""
        return self._self_task_q.popleft() if self._self_task_q else None

    def take_proactive(self) -> str | None:
        """Pop the oldest queued proactive utterance, or None if empty."""
        return self._proactive_q.popleft() if self._proactive_q else None

    # ── Speak-gate API — used by run.py's gate loop ─────────────────────────

    def candidate_count(self) -> int:
        """How many speak-candidates are waiting for the gate to evaluate."""
        return len(self._candidate_q)

    def take_oldest_candidate(self) -> dict | None:
        """Pop and return the oldest candidate, or None if the queue is empty.
        After heuristic checks, the gate either calls judge_candidate()
        + commit_candidate_to_speech() ("yes"), return_candidate() ("wait"),
        or simply discards the popped candidate ("drop")."""
        return self._candidate_q.popleft() if self._candidate_q else None

    def return_candidate(self, candidate: dict) -> None:
        """Re-queue a candidate (e.g. on 'wait' or because a heuristic gate
        failed transiently). Bumps the attempts counter so callers can
        eventually decide to drop a perpetually-deferred candidate."""
        candidate["attempts"] = int(candidate.get("attempts", 0)) + 1
        # Put back at the front so order is preserved (FIFO by created_ts).
        self._candidate_q.appendleft(candidate)

    def commit_candidate_to_speech(self, candidate: dict) -> None:
        """Promote a candidate to the proactive utterance queue. The existing
        proactive drain in run.py will pick it up and route it to TTS."""
        spoken = (candidate.get("spoken") or "").strip()
        if spoken:
            self._proactive_q.append(spoken)
            logger.info("[Speak gate] Committing candidate (age=%.0fs, attempts=%d): %r",
                        time.time() - float(candidate.get("created_ts", time.time())),
                        int(candidate.get("attempts", 0)),
                        spoken[:80])

    async def bridge_if_needed(self, candidate: dict) -> str:
        """Decide whether to run a local Ollama rewrite over a candidate's
        spoken form to bridge naturally from the current topic. Returns the
        spoken form the gate should ACTUALLY commit — either the rewritten
        version (when bridging fires AND succeeds) or the original.

        Bridging fires only when:
          * speak_bridge_enabled is truthy (settings)
          * topic_overlap(candidate.spoken, recent_context) is BELOW
            speak_bridge_overlap_threshold — i.e. the candidate is a real
            tangent, not already on-topic.

        Failure modes (Ollama down, timeout, gibberish output) all silently
        fall back to the original spoken form. No paid LLM is ever consulted
        here — the bridge cell is locked to locality="local".
        """
        original = (candidate.get("spoken") or "").strip()
        if not original:
            return original

        enabled = bool(settings.get("speak_bridge_enabled"))
        if not enabled:
            return original

        threshold = float(settings.get("speak_bridge_overlap_threshold") or 0.20)
        overlap = _content_word_overlap(original, self._last_context or "")
        if overlap >= threshold:
            # Already on-topic enough — skip the bridge call entirely, saves
            # an Ollama round-trip and avoids the local model accidentally
            # mangling something that already flows fine.
            logger.debug(
                "[Speak gate] Bridge skipped (on-topic, overlap=%.2f ≥ %.2f): %r",
                overlap, threshold, original[:60],
            )
            return original

        # Off-topic candidate — try the local rewrite.
        prompt_lines = [
            "RECENT CONTEXT (what was just being discussed):",
            (self._last_context or "(no context yet)")[:1200],
            "",
            f"BRAIN'S CURRENT EMOTION: {self._last_emotion}",
            "",
            "CANDIDATE TO REWRITE (the thing the brain wants to say next):",
            original,
            "",
            "Return ONLY the rewritten utterance (one or two short sentences).",
        ]
        bridge_turn_id = f"bridge_{int(time.time()*1000)}"
        self._bridge_cell.reset_turn(bridge_turn_id)
        try:
            raw = await self._bridge_cell.call([
                {"role": "user", "content": "\n".join(prompt_lines)}
            ])
        except Exception as e:
            logger.debug("[Speak gate] Bridge call raised, falling back: %s", e)
            return original

        rewritten = (raw or "").strip()
        # Strip stray code fences or quotes the local model occasionally adds.
        rewritten = re.sub(r"^```[a-zA-Z]*\s*", "", rewritten)
        rewritten = re.sub(r"\s*```$", "", rewritten).strip()
        if rewritten.startswith('"') and rewritten.endswith('"') and len(rewritten) > 2:
            rewritten = rewritten[1:-1].strip()

        # Validate: must be a sensible single utterance, not JSON, not empty,
        # not absurdly long. If anything looks off, keep the original.
        if not rewritten:
            return original
        if len(rewritten) < 5 or len(rewritten) > 300:
            logger.info(
                "[Speak gate] Bridge output rejected (length=%d): %r",
                len(rewritten), rewritten[:80],
            )
            return original
        if rewritten.lstrip().startswith("{") or rewritten.lstrip().startswith("["):
            logger.info("[Speak gate] Bridge returned JSON-shaped output, ignoring")
            return original

        logger.info(
            "[Speak gate] Bridged (overlap=%.2f): %r → %r",
            overlap, original[:60], rewritten[:80],
        )
        return rewritten

    async def judge_candidate(self, candidate: dict) -> tuple[str, str]:
        """Run the judge LLM against a candidate. Returns (verdict, reason).
        verdict ∈ {"yes", "wait", "drop"}. On any failure, returns ("wait",
        "judge error") so the candidate gets another chance next cycle.
        """
        spoken = (candidate.get("spoken") or "").strip()
        if not spoken:
            return ("drop", "empty spoken form")

        # Compute topic overlap with the live recent context using the same
        # content-word Jaccard the monologue dedup uses.
        overlap = _content_word_overlap(spoken, self._last_context or "")
        valence = valence_of(self._last_emotion)
        is_social_discomfort = self._last_emotion in _DEFLECTION_OVERRIDES

        angle = (candidate.get("angle") or "").strip()
        is_propose = bool(candidate.get("propose"))
        prompt_lines = [
            "RECENT CONTEXT:",
            (self._last_context or "(no context yet)")[:1500],
            "",
            "CANDIDATE TO POTENTIALLY SPEAK:",
            spoken,
            "",
            "BRAIN STATE:",
            f"- emotion: {self._last_emotion}",
            f"- valence: {valence:+.2f}",
            f"- is_social_discomfort: {is_social_discomfort}",
            f"- topic_overlap: {overlap:.2f}",
            f"- familiarity: {self._last_familiarity}",
            f"- affection_score: {self._last_affection_score}",
            f"- attempts_so_far: {int(candidate.get('attempts', 0))}",
            f"- angle: {angle or '(unset)'}",
            f"- is_action_proposal: {is_propose}",
            "",
            "Return JSON: {\"verdict\": \"yes\"|\"wait\"|\"drop\", \"reason\": \"...\"}",
        ]

        # Each judge call is its own logical "turn" for the cell's per-turn cap.
        judge_turn_id = f"judge_{int(time.time()*1000)}"
        self._judge_cell.reset_turn(judge_turn_id)
        try:
            raw = await self._judge_cell.call([
                {"role": "user", "content": "\n".join(prompt_lines)}
            ])
        except Exception as e:
            logger.debug("[Speak gate] Judge call raised: %s", e)
            return ("wait", "judge error")

        if not raw:
            return ("wait", "judge empty")

        # Parse JSON; tolerate code fences.
        try:
            text = raw.strip()
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
            parsed = json.loads(text)
            verdict = (parsed.get("verdict") or "wait").strip().lower()
            reason = (parsed.get("reason") or "").strip()[:120]
            if verdict not in {"yes", "wait", "drop"}:
                verdict = "wait"
            return (verdict, reason)
        except Exception:
            # Heuristic fallback — look for the verdict words in raw text.
            lower = raw.lower()
            if "drop" in lower:
                return ("drop", "raw=drop")
            if "yes" in lower:
                return ("yes", "raw=yes")
            return ("wait", "unparsed")

    def update_context(self, parietal_text: str,
                       emotion: str | None = None,
                       self_schema: str | None = None,
                       speaker_name: str | None = None,
                       relationship: dict | None = None) -> None:
        """Refresh what the DMN sees about the world. Called at turn start
        (with the in-progress user input — only parietal_text supplied) and
        at turn end (with the full exchange + emotion + relationship).

        Upsert semantics: empty/None values for emotion / self_schema /
        speaker_name / relationship preserve the existing stored value.
        Only parietal_text is unconditionally overwritten — it's the live
        conversation snapshot and is meant to be replaced each call.

        Emotion and relationship are stored as separate fields, NOT folded
        into _last_context — the monologue prompt builder + the judge prompt
        consume them as structured inputs.
        """
        # Self-schema: preserve prior value if not supplied.
        if self_schema:
            self._last_self_schema = self_schema[:1500]
        # Rebuild context blob with the LIVE parietal + most recent schema.
        self._last_context = (
            f"Recent conversation:\n{parietal_text}\n\n"
            f"Self-model snippet:\n{getattr(self, '_last_self_schema', '')}"
        )
        # Emotion: preserve prior value when not supplied.
        if emotion is not None:
            cleaned = emotion.strip().lower()
            if cleaned:
                self._last_emotion = cleaned
        if speaker_name:
            self._last_speaker_name = speaker_name
        if relationship is not None:
            try:
                self._last_affection_score = int(relationship.get("score", 0))
            except Exception:
                self._last_affection_score = 0
            fam = (relationship.get("familiarity") or "new").strip().lower()
            if fam in {"new", "acquainted", "close"}:
                self._last_familiarity = fam
            else:
                self._last_familiarity = "new"

    def set_projects_context(self, open_questions_text: str) -> None:
        """Extract and store the 'Projects assigned by Russ' section from
        open_questions.md so the monologue knows what work is pre-authorized.
        Called at boot and whenever open_questions.md is rewritten."""
        import re
        # Extract from the Projects heading to the next top-level heading (##)
        # or end of file — whichever comes first.
        m = re.search(
            r"(## Projects assigned by Russ.*?)(?=\n## |\Z)",
            open_questions_text,
            re.DOTALL,
        )
        self._last_projects = m.group(1).strip() if m else ""

    # ── Deferred thoughts ────────────────────────────────────────────────────

    def _append_deferred_thought(self, text: str,
                                   urgency: str = "high",
                                   tags: list[str] | None = None) -> None:
        """Write immediate/high urgency thoughts to deferred_thoughts.md for
        explicit surfacing on user return. Normal/low urgency thoughts are stored
        only in episodic memory (handled by the hippocampus encode call) and
        surface naturally when a matching topic comes up in conversation."""
        if urgency not in ("immediate", "high"):
            logger.info("[DMN] Deferred thought (urgency=%s) — episodic memory only: %r",
                        urgency, text[:80])
            return
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        entry = f"\n---\n[{timestamp}] [{urgency.upper()}]{tag_str} {text.strip()}\n"
        try:
            if not DEFERRED_THOUGHTS_PATH.exists():
                DEFERRED_THOUGHTS_PATH.write_text("# Deferred Thoughts\n")
            with DEFERRED_THOUGHTS_PATH.open("a", encoding="utf-8") as f:
                f.write(entry)
            logger.info("[DMN] Deferred thought saved (urgency=%s, tags=%s): %r",
                        urgency, tags, text[:80])
        except Exception as e:
            logger.warning("[DMN] Could not save deferred thought: %s", e)

    def take_deferred_thoughts(self) -> str:
        """Read and clear all deferred thoughts. Returns empty string if none."""
        try:
            if not DEFERRED_THOUGHTS_PATH.exists():
                return ""
            content = DEFERRED_THOUGHTS_PATH.read_text(encoding="utf-8").strip()
            # Strip the header line to get only the entries
            lines = content.split("\n")
            entries = "\n".join(l for l in lines if not l.startswith("# Deferred"))
            entries = entries.strip(" \n-")
            if not entries:
                return ""
            # Clear the file (keep header)
            DEFERRED_THOUGHTS_PATH.write_text("# Deferred Thoughts\n")
            return entries
        except Exception as e:
            logger.warning("[DMN] Could not read deferred thoughts: %s", e)
            return ""

    def has_deferred_content(self) -> bool:
        """True if there are deferred thoughts or unreviewed proposals."""
        has_thoughts = (
            DEFERRED_THOUGHTS_PATH.exists()
            and len(DEFERRED_THOUGHTS_PATH.read_text(encoding="utf-8").strip()
                     .replace("# Deferred Thoughts", "").strip()) > 0
        )
        has_proposals = (
            PROPOSALS_DIR.exists()
            and any(PROPOSALS_DIR.glob("*.md"))
        )
        return has_thoughts or has_proposals

    # ── Proposal planning ─────────────────────────────────────────────────────

    async def _run_planning_pass(self, seed_thought: str, turn_id: str) -> None:
        """Elaborate a seed thought into a structured proposal doc using the
        local-general model. Saves to proposals/ directory. Never executes work."""
        from datetime import datetime
        plan_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        self._planner_cell.reset_turn(f"{turn_id}_plan")
        self._router.enter_background_mode()
        try:
            raw = await self._planner_cell.call([
                {"role": "user", "content":
                 f"Seed idea:\n{seed_thought}\n\n"
                 f"Current date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                 f"Write a proposal document based on this idea."}
            ])
        except Exception as e:
            logger.warning("[DMN] Planning pass failed: %s", e)
            return
        finally:
            self._router.exit_background_mode()

        if not raw or len(raw.strip()) < 100:
            logger.warning("[DMN] Planning pass returned too little content — discarding")
            return

        # Inject the timestamp and status if the model didn't include them
        content = raw.strip()
        if "**Proposed**" not in content:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            # Insert after the first heading line
            lines = content.split("\n")
            for i, line in enumerate(lines):
                if line.startswith("# "):
                    lines.insert(i + 1, f"\n**Proposed**: {ts}  \n**Status**: awaiting_review\n")
                    break
            content = "\n".join(lines)

        PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
        # Derive a slug from the first heading, fallback to timestamp
        slug_match = re.search(r"^# (.+)", content, re.MULTILINE)
        slug = re.sub(r"[^a-z0-9]+", "-", slug_match.group(1).lower())[:40] if slug_match else "idea"
        filename = f"{plan_id}-{slug}.md"
        path = PROPOSALS_DIR / filename
        path.write_text(content, encoding="utf-8")
        logger.info("[DMN] Proposal saved: %s", filename)

    def list_proposals(self) -> list[dict]:
        """Return metadata for all proposal docs: [{filename, title, status, proposed}]."""
        if not PROPOSALS_DIR.exists():
            return []
        results = []
        for p in sorted(PROPOSALS_DIR.glob("*.md")):
            try:
                text = p.read_text(encoding="utf-8")
                title_m = re.search(r"^# (.+)", text, re.MULTILINE)
                status_m = re.search(r"\*\*Status\*\*:\s*(.+)", text)
                proposed_m = re.search(r"\*\*Proposed\*\*:\s*(.+)", text)
                results.append({
                    "filename": p.name,
                    "path": str(p),
                    "title": title_m.group(1).strip() if title_m else p.stem,
                    "status": status_m.group(1).strip() if status_m else "unknown",
                    "proposed": proposed_m.group(1).strip() if proposed_m else "",
                })
            except Exception:
                pass
        return results

    def _parse_monologue_response(self, raw: str) -> dict | None:
        """Parse JSON from the monologue cell. Retries once after stripping invalid +N syntax."""
        candidate = raw.strip()
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass
        try:
            fixed = re.sub(r':\s*\+(\d)', r': \1', raw.strip())
            fixed = re.sub(r"^```(?:json)?\s*", "", fixed)
            fixed = re.sub(r"\s*```$", "", fixed).strip()
            return json.loads(fixed)
        except Exception:
            return None

    def _tick_skip_probability(self) -> float:
        """Compute the probability of skipping this tick based on neuromod state.

        ACh is the primary gate — high attentional engagement suppresses DMN,
        matching the basal forebrain cholinergic mechanism in real brains.
        Glu (arousal) adds secondary suppression.
        Moderate GABA (anxious rumination range) reduces suppression slightly —
        anxiety tends to increase idle internal chatter, not quiet it.
        Very high GABA (inhibited/frozen) suppresses everything including DMN.
        """
        snap = self._bus.neuromod.snapshot()
        ach  = snap.get("ACh",  0.0)
        glu  = snap.get("Glu",  0.0)
        gaba = snap.get("GABA", 0.0)

        suppression = ach * settings.get("ach_suppression_weight") + glu * settings.get("glu_suppression_weight")

        # Moderate GABA (anxious but not frozen) → more rumination, not less
        if 0.2 <= gaba < 0.6:
            suppression = max(0.0, suppression - settings.get("gaba_suppression_reduction"))

        return min(settings.get("suppression_skip_prob_max"), suppression)

    def _chem_snapshot(self) -> dict[str, float]:
        """Merged neuromod + hormonal snapshot for switch modulation."""
        try:
            nm = self._bus.neuromod.snapshot()
        except Exception:
            nm = {}
        try:
            hs = self._bus.hormonal.snapshot()
        except Exception:
            hs = {}
        return {**nm, **hs}

    def _current_interval(self) -> float:
        """Adaptive tick interval: faster when there's a live conversation,
        slower when the user has wandered off (OS-idle for > 60s) so we
        don't burn LLM calls into the void. Falls back to dmn_interval if
        get_idle_seconds is unavailable."""
        base = float(settings.get("dmn_interval") or DMN_INTERVAL)
        idle_base = float(settings.get("dmn_idle_interval") or base * 3)
        try:
            idle = get_idle_seconds()
        except Exception:
            return base
        return idle_base if idle > 60.0 else base

    async def _loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self._current_interval())
                if not self._running:
                    break
                # Idle decay runs FIRST, every loop iteration. If it only ran
                # inside _tick, a skipped tick would never decay — meaning once
                # ACh climbs high enough to suppress, it would stay high and
                # suppress forever. Running it here gives suppressed ticks a
                # chance to recover.
                self._idle_decay()
                # Chemistry idle-gate: hard block when chemistry says the
                # brain shouldn't be mind-wandering (alert/defensive states).
                chem = self._chem_snapshot()
                if not self._idle_gate.should_fire(0.6, chem, turn_id=f"dmn_{self._thought_count}"):
                    logger.debug(
                        "[Background reflection] Tick suppressed by idle_gate "
                        "(NE=%.2f GABA=%.2f 5HT=%.2f OXT=%.2f)",
                        chem.get("NE", 0), chem.get("GABA", 0),
                        chem.get("5HT", 0), chem.get("OXT", 0),
                    )
                    if self._obs:
                        eff = self._idle_gate.effective_threshold(chem)
                        self._obs.record_modulation_event(
                            "idle_gate", "dmn", suppressed=True,
                            chem=chem, level=0.6, effective_threshold=eff,
                        )
                    continue
                self._idle_gate.fire(0.6, "tick_allowed", snapshot=chem)
                skip_prob = self._tick_skip_probability()
                if random.random() < skip_prob:
                    snap = self._bus.neuromod.snapshot()
                    logger.debug(
                        "[Background reflection] Tick suppressed "
                        "(skip_prob=%.2f ACh=%.2f Glu=%.2f GABA=%.2f)",
                        skip_prob, snap["ACh"], snap["Glu"], snap["GABA"],
                    )
                    continue
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("[Background reflection] Tick failed: %s", e, exc_info=True)
        except asyncio.CancelledError:
            pass

    def _build_situation_block(self, chem: dict) -> str:
        """Structured emotion, chemistry, and relationship signals appended to
        the monologue prompt so the LLM has them as explicit fields."""
        val = valence_of(self._last_emotion)
        comfort = "comfortable" if val >= 0 else "uncomfortable"
        lines = [
            "",
            f"Emotion: {self._last_emotion} (valence {val:+.1f}, {comfort})",
        ]
        # Neuromodulators shape thought character — which topics feel salient,
        # how ruminative vs exploratory the mind runs, motivational pull.
        nm_parts = []
        for key in ("DA", "ACh", "GABA", "Glu", "NE"):
            if key in chem:
                nm_parts.append(f"{key}={chem[key]:.2f}")
        # Hormones shape longer-horizon mood coloring.
        h_parts = []
        for key in ("5HT", "CORT", "OXT", "AEA"):
            if key in chem:
                h_parts.append(f"{key}={chem[key]:.2f}")
        if nm_parts:
            lines.append(f"Neuromodulators: {' '.join(nm_parts)}")
        if h_parts:
            lines.append(f"Hormones: {' '.join(h_parts)}")
        if self._last_speaker_name:
            lines.append(
                f"Speaker: {self._last_speaker_name} ({self._last_familiarity})"
            )
        else:
            lines.append("Speaker: unknown (new)")
        try:
            idle_s = int(get_idle_seconds())
            lines.append(f"OS-idle seconds: {idle_s}  ({'user away' if idle_s > 60 else 'user present'})")
        except Exception:
            pass
        return "\n".join(lines)

    async def _tick(self) -> None:
        self._thought_count += 1
        turn_id = f"dmn_{self._thought_count}"

        # Refresh parietal slice so the monologue always sees the live conversation
        if self._parietal is not None:
            with contextlib.suppress(Exception):
                self.update_context(self._parietal.recent_turns_text())

        # 1. Internal monologue
        chem = self._chem_snapshot()
        thought_clean, metadata = await self._run_monologue(turn_id, chem)
        if thought_clean:
            await self._process_thought(thought_clean, metadata, turn_id)

        # 2. User simulation / prediction (every 3rd tick)
        if self._thought_count % 3 == 0 and self._parietal:
            await self._run_simulation(turn_id)

        # 3. Anticipator — pre-think likely responses to the entity's last question
        if self.last_was_question and not self.anticipations:
            await self._run_anticipator(turn_id)

        # 4. Prefetcher — proactively pull memory for topics likely to resurface
        if (self._thought_count % 4 == 0
                and self._hippocampus is not None
                and not self.prefetched):
            await self._run_prefetcher(turn_id)

    async def _run_monologue(self, turn_id: str, chem: dict) -> tuple[str, dict]:
        """Build prompt, call the monologue cell, parse response.

        Returns (thought_clean, metadata) where metadata keys are:
        angle, spoken_form, task_goal, is_propose, is_plan,
        defer_text, defer_urgency, defer_tags, chem_delta.
        Empty thought_clean means the cell returned nothing or parsing yielded nothing.
        """
        self._monologue_cell.reset_turn(turn_id)

        context_label = f"Recent context:\n{self._last_context}" if self._last_context else "Recent context: none"
        prompt_parts = [context_label, self._build_situation_block(chem)]
        if self._last_projects:
            prompt_parts.append(
                f"\nPRE-AUTHORIZED PROJECTS (work within these scopes auto-runs — "
                f"set `task` directly, no propose needed):\n{self._last_projects}"
            )
        if self._recent_thoughts:
            recent_block = "\n".join(f"- {t}" for t in self._recent_thoughts)
            prompt_parts.append(
                f"\nThoughts you ALREADY had recently (do NOT repeat or paraphrase):\n"
                f"{recent_block}"
            )
        if self._recent_angles:
            angles_block = ", ".join(self._recent_angles)
            prompt_parts.append(
                f"\nConceptual territory already covered (choose a DIFFERENT angle):\n"
                f"{angles_block}"
            )

        raw = await self._monologue_cell.call([
            {"role": "user", "content": "\n".join(prompt_parts)}
        ])
        if not raw:
            logger.warning("[Background reflection] Monologue cell returned empty — model may be unavailable")
            return "", {}

        metadata: dict = {
            "angle": None, "spoken_form": None, "task_goal": None,
            "is_propose": False, "is_plan": False,
            "defer_text": None, "defer_urgency": "high", "defer_tags": [],
            "chem_delta": {},
        }

        parsed = self._parse_monologue_response(raw)
        if parsed is None:
            thought_clean = raw.strip()
        else:
            thought_clean = (parsed.get("thought") or "").strip()
            metadata["angle"] = (parsed.get("angle") or "").strip().lower() or None
            metadata["is_propose"] = bool(parsed.get("propose"))
            metadata["is_plan"] = bool(parsed.get("plan"))
            raw_defer = parsed.get("defer")
            if isinstance(raw_defer, dict):
                metadata["defer_text"] = (raw_defer.get("text") or "").strip()
                defer_urgency = (raw_defer.get("urgency") or "high").strip().lower()
                if defer_urgency not in ("immediate", "high", "normal", "low"):
                    defer_urgency = "high"
                metadata["defer_urgency"] = defer_urgency
                metadata["defer_tags"] = [str(t) for t in (raw_defer.get("topic_tags") or [])][:5]
            if parsed.get("speak") and parsed.get("spoken"):
                metadata["spoken_form"] = parsed["spoken"].strip()
            if not metadata["is_propose"] and not metadata["is_plan"] and not metadata["defer_text"]:
                raw_task = (parsed.get("task") or "").strip()
                if raw_task:
                    metadata["task_goal"] = raw_task
            raw_delta = parsed.get("chem_delta") or {}
            if isinstance(raw_delta, dict):
                chem_delta: dict[str, float] = {}
                for ch, v in raw_delta.items():
                    if ch in _CHEM_ALLOWED:
                        try:
                            chem_delta[ch] = max(-_CHEM_MAX_DELTA, min(_CHEM_MAX_DELTA, float(v)))
                        except (TypeError, ValueError):
                            pass
                metadata["chem_delta"] = chem_delta

        return thought_clean, metadata

    async def _process_thought(self, thought_clean: str, metadata: dict, turn_id: str) -> None:
        """Dedup-check and, if novel, record, publish, and dispatch side-effects for one thought."""
        angle = metadata["angle"]
        spoken_form = metadata["spoken_form"]
        task_goal = metadata["task_goal"]
        is_propose = metadata["is_propose"]
        is_plan = metadata["is_plan"]
        defer_text = metadata["defer_text"]
        defer_urgency = metadata["defer_urgency"]
        defer_tags = metadata["defer_tags"]
        chem_delta = metadata["chem_delta"]

        max_overlap = 0.0
        # Only compare against the last DMN_DEDUP_WINDOW thoughts, not all of them.
        # The full deque is shown to the LLM as context (variety pressure), but
        # hard-blocking on the entire window causes over-suppression on focused
        # topics — thoughts stop flowing after 3-4 because nearly everything
        # shares content words with at least one of 10 prior thoughts.
        for prior in list(self._recent_thoughts)[-DMN_DEDUP_WINDOW:]:
            o = _content_word_overlap(thought_clean, prior)
            if o > max_overlap:
                max_overlap = o

        if max_overlap > settings.get("dmn_overlap_threshold"):
            self._suppressed_count += 1
            logger.info(
                "[Background reflection] Suppressed redundant thought "
                "(overlap %.2f, total suppressed=%d): %r",
                max_overlap, self._suppressed_count, thought_clean[:60],
            )
            return

        self._recent_thoughts.append(thought_clean)
        if angle:
            self._recent_angles.append(angle)

        direction = _classify_thought(thought_clean)
        neuromod_snapshot = self._bus.neuromod.snapshot()

        if self._obs:
            self._obs.record_thought(
                thought=thought_clean, direction=direction, angle=angle,
                count=self._thought_count, neuromod=neuromod_snapshot,
            )

        da_level = float(neuromod_snapshot.get("DA", 0.5))
        em_valence = valence_of(self._last_emotion)
        salient = (
            da_level > 0.62
            or spoken_form is not None
            or abs(em_valence) > 0.45
        )
        buf_entry: dict = {
            "thought": thought_clean, "angle": angle or "",
            "direction": direction, "speak_flagged": spoken_form is not None,
            "emotion": self._last_emotion,
            "neuromod": {k: round(v, 3) for k, v in neuromod_snapshot.items()},
            "salient": salient, "ts": time.time(),
        }
        self._session_thought_buf.append(buf_entry)
        if len(self._session_thought_buf) > self._session_thought_limit:
            for i, e in enumerate(self._session_thought_buf):
                if not e["salient"]:
                    self._session_thought_buf.pop(i)
                    break
            else:
                self._session_thought_buf.pop(0)

        tick_deltas = _INWARD_DELTA if direction == "inward" else _OUTWARD_DELTA
        for channel, delta in tick_deltas.items():
            self._bus.neuromod.add(channel, delta)

        hormonal_channels = {"5HT", "CORT", "OXT", "AEA"}
        for channel, delta in chem_delta.items():
            if channel in hormonal_channels:
                self._bus.hormonal.add(channel, delta)
            else:
                self._bus.neuromod.add(channel, delta)

        await self._bus.publish_dict(
            "stream.thought",
            {"thought": thought_clean, "ts": time.time(),
             "count": self._thought_count, "direction": direction,
             "proactive": spoken_form is not None, "chem_delta": chem_delta},
            source="dmn",
        )
        logger.debug("[Background reflection] Thought #%d (%s): %s",
                     self._thought_count, direction, thought_clean[:80])

        if spoken_form:
            self._candidate_q.append({
                "thought": thought_clean, "spoken": spoken_form,
                "angle": angle, "propose": is_propose,
                "created_ts": time.time(), "attempts": 0,
            })
            logger.info("[Background reflection] Speak candidate queued (queue=%d): %r",
                        len(self._candidate_q), spoken_form[:80])

        if task_goal:
            self._self_task_q.append(task_goal)
            logger.info("[Background reflection] Self-initiated task queued: %r", task_goal[:80])

        if defer_text:
            self._append_deferred_thought(defer_text, defer_urgency, defer_tags)
            if self._hippocampus is not None:
                asyncio.create_task(
                    self._hippocampus.encode_deferred_question(
                        session_id=getattr(self, "_session_id", "unknown"),
                        text=defer_text, urgency=defer_urgency, tags=defer_tags,
                        embedding_fn=self._router.embed,
                    )
                )

        if is_plan and thought_clean:
            asyncio.create_task(self._run_planning_pass(thought_clean, turn_id))

    async def _run_simulation(self, turn_id: str) -> None:
        """Run the user-simulation cell and publish the predicted next input."""
        self._simulation_cell.reset_turn(turn_id + "_sim")
        raw = await self._simulation_cell.call([
            {"role": "user", "content": self._last_context or "No context yet."}
        ])
        try:
            self.predicted_next = json.loads(raw)
            await self._bus.publish_dict("stream.prediction", self.predicted_next, source="dmn")
            logger.debug("[Background reflection] Anticipating: %s (confidence=%.2f)",
                         self.predicted_next.get("predicted_input", "")[:60],
                         self.predicted_next.get("confidence", 0))
        except Exception:
            pass

    def _idle_decay(self) -> None:
        """Decay ACh and Glu 15% toward their resting floors per tick.

        Equilibrium with continuous outward thinking (+0.02 ACh/tick):
            x = 0.85 * x + 0.15 * 0.1 + 0.02   →   ACh ≈ 0.23
        skip_prob ≈ 0.23 + 0.09 = 0.32 → ~68% of ticks fire. Without this
        decay, equilibrium is ACh = 1.0 → skip_prob capped at 0.85 → 15%.
        """
        ACH_FLOOR, GLU_FLOOR = 0.1, 0.15
        DECAY_RATE = 0.15  # fraction of excess-over-floor removed per tick
        ach = self._bus.neuromod.get("ACh")
        glu = self._bus.neuromod.get("Glu")
        if ach > ACH_FLOOR:
            self._bus.neuromod.add("ACh", -(ach - ACH_FLOOR) * DECAY_RATE)
        if glu > GLU_FLOOR:
            self._bus.neuromod.add("Glu", -(glu - GLU_FLOOR) * DECAY_RATE)

    async def _run_prefetcher(self, turn_id: str) -> None:
        self._prefetcher_cell.reset_turn(turn_id + "_pre")
        prompt = self._last_context or "No context yet."
        raw = await self._prefetcher_cell.call([{"role": "user", "content": prompt}])
        try:
            parsed = json.loads(raw)
            queries = parsed.get("queries", []) or []
        except Exception as e:
            logger.debug("[Background reflection] Prefetcher parse failed: %s", e)
            return

        if not queries:
            return

        # Run each recall in parallel (capped to 3); pull the schema + episode
        # text for each topic and cache as prefetched_context.
        async def _one_query(q: dict) -> dict | None:
            topic = str(q.get("topic", "")).strip()
            reason = str(q.get("reason", "")).strip()
            if not topic:
                return None
            try:
                result = await self._hippocampus.recall(
                    query=topic,
                    entities=[topic],
                    turn_id=turn_id + "_pre",
                    embedding_fn=self._router.embed,
                )
                snippets = []
                if result.get("episodes"):
                    snippets.append(result["episodes"][:400])
                if result.get("schema"):
                    snippets.append(result["schema"][:300])
                joined = "\n".join(s for s in snippets if s.strip())
                if not joined:
                    return None
                return {"topic": topic, "reason": reason, "snippets": joined}
            except Exception as e:
                logger.debug("[Background reflection] Prefetcher recall failed for %r: %s",
                             topic, e)
                return None

        results = await asyncio.gather(
            *(_one_query(q) for q in queries[:3]),
            return_exceptions=False,
        )
        self.prefetched = [r for r in results if r]
        if self.prefetched:
            await self._bus.publish_dict(
                "stream.prefetch",
                {"items": self.prefetched, "ts": time.time()},
                source="dmn",
            )
            logger.info("[Background reflection] Prefetched context for %d topics: %s",
                        len(self.prefetched),
                        ", ".join(p["topic"][:30] for p in self.prefetched))

    async def _run_anticipator(self, turn_id: str) -> None:
        self._anticipator_cell.reset_turn(turn_id + "_ant")
        prompt = (
            f"{self._last_context or 'No context yet.'}\n\n"
            f"Your last message (which ended with a question): "
            f"{self.last_assistant_message[:400]!r}\n\n"
            "Pre-think the user's likely answers and your responses."
        )
        raw = await self._anticipator_cell.call([{"role": "user", "content": prompt}])
        try:
            parsed = json.loads(raw)
            scenarios = parsed.get("scenarios", []) or []
            # Normalize + cap
            self.anticipations = [
                {
                    "user_answer": str(s.get("user_answer", ""))[:200],
                    "response_sketch": str(s.get("response_sketch", ""))[:300],
                    "context_needed": list(s.get("context_needed", []) or [])[:5],
                }
                for s in scenarios[:3]
                if s.get("user_answer") and s.get("response_sketch")
            ]
            if self.anticipations:
                await self._bus.publish_dict(
                    "stream.anticipation",
                    {"scenarios": self.anticipations, "ts": time.time()},
                    source="dmn",
                )
                logger.info("[Background reflection] Anticipated %d follow-up scenarios",
                            len(self.anticipations))
        except Exception as e:
            logger.debug("[Background reflection] Anticipator parse failed: %s", e)
