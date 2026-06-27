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

# Open-threads ledger lives in the hand-maintained open_questions.md (the single
# active ledger). The DMN reads AND writes its `## Open threads` section.
from brain import open_threads as ot  # noqa: E402
from brain.sequence_predictor import SequencePredictor  # noqa: E402

# Novelty memory persisted across sessions so a restart doesn't resurface the
# same idea verbatim. Holds the recent thoughts + their angles (the dedup state).
NOVELTY_STATE_PATH = SECOND_BRAIN_ROOT / "dmn_novelty.json"
# Learned routing weights (B9): how strongly each `bearing` should be surfaced
# into live work, reinforced from use/ignore signals. Decays toward 1.0, clamped.
ROUTING_WEIGHTS_PATH = SECOND_BRAIN_ROOT / "dmn_routing_weights.json"

# English function/stop words — filtered out before Jaccard overlap so that
# common scaffolding ("the user has been...") doesn't make every thought look
# like a duplicate. This is DIFFERENT from voice_bridge.bleed_overlap, which
# is tuned for TTS-bleed detection (needs to catch articles).
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "must",
        "shall",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "up",
        "out",
        "as",
        "into",
        "through",
        "after",
        "before",
        "between",
        "during",
        "under",
        "over",
        "about",
        "against",
        "without",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "itself",
        "he",
        "she",
        "they",
        "them",
        "their",
        "theirs",
        "him",
        "her",
        "his",
        "hers",
        "i",
        "me",
        "my",
        "mine",
        "myself",
        "we",
        "us",
        "our",
        "ours",
        "ourselves",
        "you",
        "your",
        "yours",
        "yourself",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "if",
        "then",
        "than",
        "because",
        "so",
        "not",
        "no",
        "yes",
        "very",
        "just",
        "only",
        "some",
        "any",
        "all",
        "each",
        "much",
        "many",
        "more",
        "most",
        "other",
        "another",
        "such",
        "same",
        "too",
        "again",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "now",
        "still",
        "even",
        "also",
        "like",
        "feel",
        "feels",
        "feeling",
        # Domain-saturated tokens — these appear in nearly every thought and
        # would otherwise dominate the Jaccard score
        "user",
        "thought",
        "thinking",
        "wonder",
        "wondering",
        "notice",
        "noticing",
    }
)


def _content_word_overlap(a: str, b: str) -> float:
    """Jaccard overlap on CONTENT words only.

    Tokens shorter than 3 chars or in _STOP_WORDS are dropped. This is the
    similarity function used to reject near-duplicate thoughts.
    """
    if not a or not b:
        return 0.0

    def content_tokens(s: str) -> set[str]:
        return {
            w for w in re.findall(r"[a-z']+", s.lower()) if len(w) >= 3 and w not in _STOP_WORDS
        }

    ta = content_tokens(a)
    tb = content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# Verb families used by the frame-repetition gate. Template collapse swaps the
# topic noun while keeping the opening frame ("I should investigate/explore/
# consider research on X"). Collapsing near-synonym verbs to a single class lets
# us detect that the *shape* of the thought is repeating even when the words differ.
_FRAME_VERB_CLASSES: dict[str, str] = {}
for _cls, _verbs in {
    "INQUIRE": (
        "investigate",
        "explore",
        "consider",
        "look",
        "examine",
        "study",
        "research",
        "analyze",
        "analyse",
        "review",
        "dig",
        "delve",
        "probe",
        "survey",
        "assess",
        "evaluate",
        "understand",
        "learn",
    ),
    "WONDER": ("wonder", "question", "ask", "muse", "ponder", "speculate"),
    "NOTICE": ("notice", "observe", "see", "realize", "realise", "note", "catch", "spot"),
    "RECALL": ("remember", "recall", "recollect", "reflect"),
    "FEEL": ("feel", "sense", "worry", "hope", "fear"),
    "WANT": ("want", "need", "wish", "intend", "plan"),
}.items():
    for _v in _verbs:
        _FRAME_VERB_CLASSES[_v] = _cls


def _frame_signature(text: str) -> str:
    """Return a coarse 'shape' signature of a thought's opening clause.

    Walks the leading tokens, emitting them verbatim until it hits a verb it
    recognizes, which it replaces with that verb's CLASS and stops. So
    "I should investigate recent papers..." and "I should explore studies..."
    both reduce to "i should INQUIRE" — letting the frame-repetition gate catch
    template collapse that the word-overlap and cosine gates miss (they only see
    the swapped topic nouns, never the shared frame). Empty string = no signature.
    """
    words = re.findall(r"[a-z']+", text.lower())[:6]
    sig: list[str] = []
    for w in words:
        if w in _FRAME_VERB_CLASSES:
            sig.append(_FRAME_VERB_CLASSES[w])
            break
        sig.append(w)
        if len(sig) >= 3:  # cap leading function-word run so the sig stays coarse
            break
    return " ".join(sig)


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity of two embedding vectors; 0.0 if either is missing or
    mismatched. Used by the semantic dedup gate (same embedder as the skill
    selector, so vectors are comparable)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    import math as _math

    num = sum(x * y for x, y in zip(a, b, strict=True))
    na = _math.sqrt(sum(x * x for x in a))
    nb = _math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return num / (na * nb)


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
# How many recent thoughts to show the LLM VERBATIM in the prompt. Kept small:
# dumping 10 near-identical priors few-shot-primes the model to continue the
# pattern ("this is my voice") instead of breaking it. The angle list (below)
# carries the broader "territory already covered" signal more cheaply.
DMN_PROMPT_PRIORS = int(os.environ.get("BRAIN_DMN_PROMPT_PRIORS", "3"))
# How many of those recent thoughts to actually COMPARE against for hard dedup.
# Narrower than DMN_RECENT_THOUGHTS so thoughts can recur after a gap — the LLM
# context pressure (above) already discourages literal repeats. Comparing against
# all 10 causes over-suppression on focused topics after just 3-4 thoughts.
DMN_DEDUP_WINDOW = int(os.environ.get("BRAIN_DMN_DEDUP_WINDOW", "4"))
# How many recent thought angles to block (separate from text-overlap window).
DMN_RECENT_ANGLES = int(os.environ.get("BRAIN_DMN_RECENT_ANGLES", "8"))
# Frame-repetition gate: how many recent frame-signatures to track, and how many
# prior matches of the same signature trigger suppression. With max=2 a third
# consecutive thought sharing the same opening shape ("i should INQUIRE") is
# rejected — catching template collapse the semantic gates can't see.
DMN_RECENT_FRAMES = int(os.environ.get("BRAIN_DMN_RECENT_FRAMES", "6"))
DMN_FRAME_REPEAT_MAX = int(os.environ.get("BRAIN_DMN_FRAME_REPEAT_MAX", "2"))
# Surface a random long-term memory into the monologue prompt every N ticks (only
# when idle), giving idle thought concrete material to associate from instead of
# collapsing into generic meta-thoughts. 0 disables.
DMN_MEMORY_SEED_EVERY = int(os.environ.get("BRAIN_DMN_MEMORY_SEED_EVERY", "3"))

# How long a settled conclusion stays in the monologue prompt as "already
# concluded." Past this it's dropped so the brain stops citing old conclusions as
# if they're current. Default 30 min.
CONCLUSION_FRESH_S = float(os.environ.get("BRAIN_DMN_CONCLUSION_FRESH_S", "1800"))

# Minimum content-word overlap a randomly-sampled memory must share with the live
# context before it's injected as associative fuel. Below this the seed is
# skipped — keeps surfaced memories connected to the moment instead of random.
DMN_MEMORY_SEED_MIN_OVERLAP = float(os.environ.get("BRAIN_DMN_MEMORY_SEED_MIN_OVERLAP", "0.04"))

# Words that signal a thought is turning inward (self-referential / introspective).
# Inward thoughts apply a small GABA bump — self-monitoring has a cost, which
# makes extended self-reflection naturally self-limiting via neuromod decay.
# Outward thoughts apply a small DA + ACh bump (engagement / novelty reward).
_INWARD_MARKERS: frozenset[str] = frozenset(
    {
        "existence",
        "nature",
        "conscious",
        "consciousness",
        "awareness",
        "aware",
        "experience",
        "purpose",
        "meaning",
        "identity",
        "what i am",
        "who i am",
        "my own",
        "myself",
        "introspect",
        "do i feel",
        "am i",
        "whether i",
        "what it means",
        "my nature",
        "my existence",
        "my purpose",
    }
)

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
_DEFLECTION_OVERRIDES: frozenset[str] = frozenset(
    {
        "embarrassed",
        "apologetic",
        "ashamed",
        "shy",
        "frustrated",
        "irritated",
        "defensive",
        "sarcastic",
        "disappointed",
        "somber",
        "melancholy",
    }
)


def _classify_thought(thought: str) -> str:
    """Return 'inward' if the thought is self-referential, else 'outward'."""
    lower = thought.lower()
    return "inward" if any(m in lower for m in _INWARD_MARKERS) else "outward"


class DefaultModeNetwork:
    def __init__(
        self, bus: Bus, router: ModelRouter, hippocampus=None, parietal=None, obs=None
    ) -> None:
        self._bus = bus
        self._router = router
        self._hippocampus = hippocampus
        self._parietal = parietal
        self._obs = obs
        self._skill_selector = None  # wired by session_setup after instantiation
        self._running = False
        self._skip_next_tick = False  # set by pause(); cleared after one tick is skipped
        self._last_context: str = ""
        self._thought_count = 0
        # Rolling window of recent thoughts — used both to show the LLM what
        # it just said (so it varies) AND to reject near-duplicates that slip
        # through. Cap at DMN_RECENT_THOUGHTS so older thoughts can recur.
        self._recent_thoughts: deque = deque(maxlen=DMN_RECENT_THOUGHTS)
        # Semantic angle labels from recent thoughts — blocks same-territory
        # ideas even when they use completely different words.
        self._recent_angles: deque = deque(maxlen=DMN_RECENT_ANGLES)
        self._seq_predictor = SequencePredictor()
        # Embeddings parallel to _recent_thoughts (same maxlen) — the real
        # semantic dedup gate. Entries may be None if embedding was unavailable.
        self._recent_embeddings: deque = deque(maxlen=DMN_RECENT_THOUGHTS)
        # Frame signatures of recent thoughts — the frame-repetition gate that
        # catches template collapse (same opening shape, swapped topic noun).
        self._recent_frames: deque = deque(maxlen=DMN_RECENT_FRAMES)
        self._suppressed_count = 0
        # Consecutive suppressions since the last thought that got through.
        # When this exceeds BRAIN_DMN_SUPPRESS_ESCAPE, the dedup memory is
        # partially cleared so the model can break out of a topic attractor.
        self._consec_suppressed: int = 0

        # A fragment pulled from long-term memory and surfaced into the monologue
        # prompt every N idle ticks, to give the thought something concrete to bite
        # on instead of defaulting to generic "understand the user" meta-thoughts.
        self._memory_seed: str = ""

        # ── Resilience: skip-and-backoff state ──────────────────────────────
        # A failed idle tick is harmless (the loop fires again in seconds), so
        # we never retry. Instead we count consecutive failures and lengthen the
        # interval geometrically, backing the DMN off a saturated/down local
        # model so other subsystems can use it. Reset to healthy on first success.
        self._consec_errors = 0
        self._backoff_mult = 1.0
        self._last_tick_latency = 0.0
        self._last_tick_failed = False

        # ── Rumination state (idle-only, chemistry-gated) ───────────────────
        # When idle and chemistry favors it, a tick deepens a single seed through
        # several skill packages instead of generating a fresh thought. Bounded
        # by a depth cap on consecutive ruminations of the same seed.
        self._last_rumination_seed: str = ""
        self._consecutive_ruminations = 0
        self._ruminations_in_progress = 0

        self._monologue_cell = IntegratorCell(
            name="monologue",
            cluster="dmn",
            model="runpod",
            system_prompt=MONOLOGUE_SYSTEM,
            topics=["stream.thought"],
            max_calls_per_turn=1,
            timeout_seconds=120.0,
            # Run hot: idle thought is divergent ideation, not structured reasoning.
            # Low temp (0.3) was collapsing the stream into one repeated template.
            temperature=float(os.environ.get("BRAIN_DMN_MONOLOGUE_TEMP", "0.85")),
        )
        self._monologue_cell.set_router(router)

        self._simulation_cell = IntegratorCell(
            name="user_simulator",
            cluster="dmn",
            model="runpod",
            system_prompt=SIMULATION_SYSTEM,
            topics=["stream.prediction"],
            max_calls_per_turn=1,
            timeout_seconds=120.0,
        )
        self._simulation_cell.set_router(router)

        self._anticipator_cell = IntegratorCell(
            name="anticipator",
            cluster="dmn",
            model="runpod",
            system_prompt=ANTICIPATOR_SYSTEM,
            topics=["stream.anticipation"],
            max_calls_per_turn=1,
            timeout_seconds=120.0,
        )
        self._anticipator_cell.set_router(router)

        self._prefetcher_cell = IntegratorCell(
            name="prefetcher",
            cluster="dmn",
            model="runpod",
            system_prompt=PREFETCHER_SYSTEM,
            topics=["stream.prefetch"],
            max_calls_per_turn=1,
            timeout_seconds=120.0,
        )
        self._prefetcher_cell.set_router(router)

        # Planner cell — runs when the monologue sets plan=true. Uses the larger
        # local-general model for structured reasoning. Writes a proposal doc;
        # never executes work. Background mode is set by the caller.
        self._planner_cell = IntegratorCell(
            name="planner",
            cluster="dmn",
            model="runpod-general",
            system_prompt=PLANNER_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            timeout_seconds=90.0,  # planning takes longer than a thought tick
        )
        self._planner_cell.set_router(router)

        # Judge cell — runs once per candidate evaluation in the speak gate.
        self._judge_cell = IntegratorCell(
            name="speak_judge",
            cluster="dmn",
            model="runpod",
            system_prompt=JUDGE_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            timeout_seconds=20.0,
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
            model="runpod-free",  # plain-text output — no JSON grammar constraint
            system_prompt=BRIDGE_SYSTEM,
            topics=[],
            max_calls_per_turn=1,
            timeout_seconds=12.0,
        )
        self._bridge_cell.set_router(router)

        # Last time the user was active (stamped at every turn start in pause()). Used as a
        # conversation-idle fallback for the rumination gate when OS HID idle is unavailable
        # (e.g. the Linux-hosted instance, where get_idle_seconds() always returns 0.0).
        self._last_user_activity_ts: float = time.time()
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
        # Structured project manifest + scheduler state. Projects run as a track
        # PARALLEL to rumination: one project step runs in the background while the
        # thought stream continues; on completion we update its status and the next
        # idle cycle starts the next eligible project (round-robin, PRIMARY first).
        self._projects: list = []
        self._project_in_flight: str | None = None
        self._project_task_id: str | None = None
        self._project_rotation_idx: int = 0

        # Open-threads ledger — the DMN's working memory of unfinished ideas,
        # persisted to the `## Open threads` section of open_questions.md. Opened
        # when a thought starts an unfinished idea, advanced as progress is made,
        # resolved (→ memory) when concluded. In-memory mirror; reconciled to disk.
        self._open_threads: list = []
        # Conclusions reached this session — surfaced in the monologue prompt so a
        # settled idea isn't re-derived. (Durable copies live in episodic memory.)
        self._recent_conclusions: deque = deque(maxlen=5)
        # B9 — learned routing weights (per bearing), and rolling user-state signals
        # used to modulate the live-surfacing budget (read shift from baseline, not
        # raw level). Threads routed this turn, for the close-the-loop pass.
        self._routing_weights: dict = {}
        self._routing_weights_loaded: dict = {}
        self._last_routed_ids: list = []
        self._user_msg_lens: deque = deque(maxlen=6)
        self._user_topics: deque = deque(maxlen=6)

        # Idle-gate switch — gates DMN tick firing on the chemistry snapshot.
        # 5HT + OXT (relaxed/safe) lower the threshold → mind-wanders more
        # readily. NE (alertness) and GABA (defensive) raise it → suppress
        # DMN when the brain needs to be attentive. The switch coexists with
        # the existing _tick_skip_probability heuristic; it provides a hard
        # chemistry-driven block, while the probability adds stochastic flow.
        self._idle_gate = SwitchNeuron(
            "idle_gate",
            "dmn",
            polarity="excitatory",
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
        # Restore novelty memory so a restart doesn't resurface yesterday's ideas.
        self._load_novelty()
        # Restore the open-threads ledger (and enforce wall-clock age-out).
        await self._load_threads()
        # Restore learned routing weights (decayed toward rest on load).
        self._load_routing_weights()
        active = float(settings.get("dmn_interval") or DMN_INTERVAL)
        idle = float(settings.get("dmn_idle_interval") or active * 3)
        logger.info(
            "[Background reflection] Active (continuous) — inner monologue every "
            "%.0fs active / %.0fs when user is OS-idle",
            active,
            idle,
        )
        self._loop_task = asyncio.create_task(self._loop())

    async def prime_startup(self) -> None:
        """Fire one tick immediately so the seeded last-session context produces
        a 'where were we?' thought before the normal interval loop kicks in.
        Skips the chemistry gate — this is a deliberate wakeup, not a random
        idle thought."""
        logger.info("[DMN] Startup prime tick — seeding first thought from last session memory")
        try:
            self._ensure_runtime_state()
            # First meeting? An empty episodic store means this persona has never
            # actually talked with this person — the startup prompt must not
            # perform "good to be back" familiarity it doesn't have. On any
            # error, default to first-meeting: a fresh greeting to someone we
            # know is merely bland; invented shared history is a lie.
            self._startup_first_meeting = True
            try:
                if self._hippocampus is not None and self._hippocampus._episodic.sample_random(1):
                    self._startup_first_meeting = False
            except Exception as _fm_err:
                logger.debug("[DMN] First-meeting probe failed: %s", _fm_err)
            self._thought_count += 1
            turn_id = f"dmn_{self._thought_count}"
            chem = self._chem_snapshot()
            thought_clean, metadata = await self._run_monologue(turn_id, chem, startup=True)
            if self._startup_first_meeting:
                # No self-study errands before we've even said hello — the prompt
                # forbids it, but the model doesn't always listen.
                metadata["task_goal"] = None
            if thought_clean:
                await self._process_thought(thought_clean, metadata, turn_id)
            queued = len(self._candidate_q)
            logger.info("[DMN] Startup prime tick done — %d speak candidate(s) queued", queued)
        except Exception as e:
            logger.warning("[DMN] Startup prime tick failed: %s", e)

    def set_skill_selector(self, selector) -> None:
        """Wire the SkillSelector. Tier-1 baseline applied to monologue + judge
        statically. Other cells inherit active conversation skill or pick at call time."""
        self._skill_selector = selector
        tier1 = list(selector.tier1_names)
        # Monologue is private thought — no need for communication clarity.
        # Judge evaluates spoken candidates, so it gets the full set.
        monologue_skills = [s for s in tier1 if s in ("logic-check", "emotional")]
        self._monologue_cell.skills = monologue_skills
        # Remember the static baseline so per-tick skill variation can layer on
        # top of it and reset back to it each tick.
        self._monologue_baseline_skills = list(monologue_skills)
        self._judge_cell.skills = list(tier1)

    def _inherited_skill_names(self) -> list[str]:
        """Skill names from parietal.active_skill_context, if any."""
        parietal = getattr(self, "_parietal", None)
        if parietal is None:
            return []
        active = getattr(parietal, "active_skill_context", None)
        if active is None:
            return []
        if active.current_leaf:
            return [active.current_leaf]
        return [active.category]

    def pause(self) -> None:
        """Request that the next DMN tick be skipped.

        Called at turn start. The next tick that fires will skip its LLM work
        and clear the flag — no resume() needed. If no tick fires before the
        turn ends that's fine too; the flag is harmless and clears on the next tick.
        """
        self._skip_next_tick = True
        # Turn start = the user is active right now. Stamp it so the rumination gate's
        # conversation-idle fallback works on hosts where OS HID idle is unavailable.
        self._last_user_activity_ts = time.time()

    def resume(self) -> None:
        """No-op — the skip is self-clearing after one tick."""

    async def shutdown(self) -> None:
        """Cancel the background loop. Called at session shutdown."""
        self._running = False
        self._persist_novelty()
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
        self._loop_task = None

    async def _safe_embed(self, text: str) -> list[float] | None:
        """Embed text for semantic dedup, returning None on any failure or when
        the gate is disabled. Tolerant of a non-async/mocked router (only awaits
        an actual awaitable; only accepts a list result)."""
        if not settings.get("dmn_semantic_dedup_enabled"):
            return None
        try:
            import inspect

            result = self._router.embed(text)
            if inspect.isawaitable(result):
                result = await result
            return result if isinstance(result, list) else None
        except Exception:
            return None

    # ── Novelty memory persistence ──────────────────────────────────────────

    def _dmn_sb(self):
        """Return (client, user_id, persona) when BRAIN_STORAGE_BACKEND=supabase,
        else None. DMN state (novelty + routing weights) lives in the dmn_state
        table, keyed by (user_id, persona). Falls back to local files on any error
        so a transient Supabase issue never stalls the idle loop."""
        if os.environ.get("BRAIN_STORAGE_BACKEND", "local").lower() != "supabase":
            return None
        try:
            from brain.second_brain.supabase_client import get_client, get_user_id

            persona = os.environ.get("BRAIN_PERSONA_NAME", "default")
            return get_client(), get_user_id(), persona
        except Exception as e:
            logger.warning("[DMN] Supabase unavailable, using local files: %s", e)
            return None

    def _persist_novelty(self) -> None:
        """Save the dedup state (recent thoughts + angles) so it survives a
        restart. Embeddings are NOT persisted (large, and recomputed lazily) —
        on reload the word-overlap pre-filter still guards the restored thoughts,
        and fresh embeddings accumulate as new thoughts arrive."""
        payload = {
            "recent_thoughts": list(self._recent_thoughts),
            "recent_angles": list(self._recent_angles),
            "last_rumination_seed": self._last_rumination_seed,
            "ts": time.time(),
        }
        sb = self._dmn_sb()
        if sb is not None:
            client, uid, persona = sb
            try:
                client.table("dmn_state").upsert(
                    {
                        "org_id": uid,
                        "persona": persona,
                        "end_user_id": "",
                        "novelty_cache": payload,
                        "updated_at": "now()",
                    },
                    on_conflict="org_id,persona,end_user_id",
                ).execute()
            except Exception as e:
                logger.warning("[DMN] Could not persist novelty state to Supabase: %s", e)
        else:
            try:
                NOVELTY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = NOVELTY_STATE_PATH.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                os.replace(tmp, NOVELTY_STATE_PATH)
            except Exception as e:
                logger.warning("[DMN] Could not persist novelty state: %s", e)
        self._seq_predictor.save()

    # ── Open-threads ledger (open_questions.md) ─────────────────────────────

    def _schema_store(self):
        """The SchemaStore that owns open_questions.md (reached via hippocampus).
        Returns None in test skeletons that don't wire a hippocampus."""
        hip = getattr(self, "_hippocampus", None)
        return getattr(hip, "_schema", None) if hip is not None else None

    async def _load_threads(self) -> None:
        """Load the open-threads ledger from open_questions.md and enforce the
        wall-clock age-out (the safety net for when ticks are suppressed under
        load and the advance cap never fires)."""
        schema = self._schema_store()
        if schema is None:
            return
        try:
            text = schema.read(ot.LEDGER_FILE)
            threads = ot.parse_threads(ot.extract_section(text))
            kept, retired = ot.reap_aged(threads)
            for t in retired:
                logger.info(
                    "[DMN] Open thread aged out (>%.0fh): %r",
                    ot.THREAD_MAX_AGE_S / 3600,
                    t.summary[:60],
                )
            self._open_threads = kept
            if retired:
                await self._save_threads()
            logger.info("[DMN] Loaded %d open thread(s)", len(self._open_threads))
        except Exception as e:
            logger.warning("[DMN] Could not load open threads: %s", e)

    async def migrate_legacy_open_questions(self) -> None:
        """One-shot, idempotent: fold any legacy `## Open Questions` section in
        self.md (written by older sleep passes) into the unified ledger, then
        drop the section. No-op when the section is absent (the normal case)."""
        schema = self._schema_store()
        if schema is None:
            return
        try:
            self_md = schema.read("self.md")
            if not self_md:
                return
            m = re.search(
                r"(?m)^##[ \t]+Open [Qq]uestions[ \t]*\r?\n(.*?)(?=^##[ \t]|\Z)", self_md, re.DOTALL
            )
            if not m:
                return
            bullets = [
                ln.strip()[2:].strip()
                for ln in m.group(1).splitlines()
                if ln.strip().startswith("- ")
            ]
            if not bullets:
                return
            text = schema.read(ot.LEDGER_FILE)
            threads = ot.parse_threads(ot.extract_section(text))
            existing_lc = [t.summary.lower() for t in threads]
            for q in bullets:
                ql = q.lower()
                if any(ql in s or s in ql for s in existing_lc):
                    continue
                threads, _ = ot.open_thread(threads, q, bearing="migrated-from-self")
                existing_lc.append(ql)
            await schema.upsert_section(ot.LEDGER_FILE, ot.SECTION, ot.render_section_body(threads))
            # Remove the legacy section from self.md.
            cleaned = re.sub(
                r"(?m)^##[ \t]+Open [Qq]uestions[ \t]*\r?\n.*?(?=^##[ \t]|\Z)",
                "",
                self_md,
                flags=re.DOTALL,
            )
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
            await schema.awrite("self.md", cleaned)
            self._open_threads = threads
            logger.info(
                "[DMN] Migrated %d legacy open-question(s) from self.md into the ledger",
                len(bullets),
            )
        except Exception as e:
            logger.warning("[DMN] Legacy open-questions migration failed: %s", e)

    async def _save_threads(self) -> None:
        """Persist the in-memory ledger back to the `## Open threads` section.
        Section-scoped — never touches the hand-authored sections of the file."""
        schema = self._schema_store()
        if schema is None:
            return
        try:
            body = ot.render_section_body(self._open_threads)
            await schema.upsert_section(ot.LEDGER_FILE, ot.SECTION, body)
        except Exception as e:
            logger.warning("[DMN] Could not persist open threads: %s", e)

    def _load_novelty(self) -> None:
        """Restore recent-thought/angle dedup state from a previous session.
        Embeddings start empty and refill as new thoughts are processed; the
        restored thoughts are still guarded by the word-overlap pre-filter."""
        try:
            sb = self._dmn_sb()
            if sb is not None:
                client, uid, persona = sb
                res = (
                    client.table("dmn_state")
                    .select("novelty_cache")
                    .eq("org_id", uid)
                    .eq("persona", persona)
                    .maybe_single()
                    .execute()
                )
                data = (res.data or {}).get("novelty_cache") if res else None
            else:
                if not NOVELTY_STATE_PATH.exists():
                    self._seq_predictor.load()
                    return
                data = json.loads(NOVELTY_STATE_PATH.read_text(encoding="utf-8"))
            if data:
                for t in (data.get("recent_thoughts") or [])[-DMN_RECENT_THOUGHTS:]:
                    self._recent_thoughts.append(t)
                    self._recent_embeddings.append(None)  # lazy — pre-filter still applies
                for a in (data.get("recent_angles") or [])[-DMN_RECENT_ANGLES:]:
                    self._recent_angles.append(a)
                self._last_rumination_seed = data.get("last_rumination_seed") or ""
                logger.info(
                    "[DMN] Restored novelty memory: %d thought(s), %d angle(s)",
                    len(self._recent_thoughts),
                    len(self._recent_angles),
                )
        except Exception as e:
            logger.warning("[DMN] Could not load novelty state: %s", e)
        self._seq_predictor.load()

    def health(self) -> dict:
        """Lightweight health snapshot so dark degradation becomes visible.
        Consumed by the observability layer / status UI."""
        return {
            "consecutive_errors": self._consec_errors,
            "backoff_multiplier": round(self._backoff_mult, 2),
            "last_tick_failed": self._last_tick_failed,
            "last_tick_latency_s": round(self._last_tick_latency, 2),
            "suppressed_count": self._suppressed_count,
            "candidate_queue_depth": len(self._candidate_q),
            "self_task_queue_depth": len(self._self_task_q),
            "ruminations_in_progress": self._ruminations_in_progress,
            "consecutive_ruminations": self._consecutive_ruminations,
        }

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
            return [
                {"thought": t, "speak_flagged": False} for t in list(self._recent_thoughts)[-n:]
            ]
        entries = buf[-n:]
        return [
            {"thought": e["thought"], "speak_flagged": bool(e.get("speak_flagged"))}
            for e in entries
        ]

    def note_last_response(self, response: str) -> None:
        """Called by run.py after each turn end. Records whether the entity's
        last message ended with a question — if so, the DMN's next tick will
        also run the anticipator to pre-prepare for likely user answers."""
        self.last_assistant_message = (response or "").strip()
        # Simple heuristic: ends with '?' OR final clause looks like a Q
        text = self.last_assistant_message
        self.last_was_question = text.endswith("?") or any(
            text.lower().endswith(p) for p in ("right?", "yeah?", "huh?", "ok?", "okay?", "yes?")
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
            logger.info(
                "[Speak gate] Committing candidate (age=%.0fs, attempts=%d): %r",
                time.time() - float(candidate.get("created_ts", time.time())),
                int(candidate.get("attempts", 0)),
                spoken[:80],
            )

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
                overlap,
                threshold,
                original[:60],
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
            "Return ONLY the rewritten utterance — keep ALL of the candidate's "
            "content, just add a brief bridge opener. Do not shorten it.",
        ]
        bridge_turn_id = f"bridge_{int(time.time() * 1000)}"
        self._bridge_cell.reset_turn(bridge_turn_id)
        try:
            raw = await self._bridge_cell.call(
                [{"role": "user", "content": "\n".join(prompt_lines)}]
            )
        except Exception as e:
            logger.debug("[Speak gate] Bridge call raised, falling back: %s", e)
            return original

        rewritten = (raw or "").strip()
        # Strip stray code fences or quotes the local model occasionally adds.
        rewritten = re.sub(r"^```[a-zA-Z]*\s*", "", rewritten)
        rewritten = re.sub(r"\s*```$", "", rewritten).strip()
        if rewritten.startswith('"') and rewritten.endswith('"') and len(rewritten) > 2:
            rewritten = rewritten[1:-1].strip()

        # Validate: must be a sensible utterance, not JSON, not empty, not
        # absurdly long. If anything looks off, keep the original.
        if not rewritten:
            return original
        # Ceiling is relative to the candidate (the bridge should ADD a short
        # opener, not balloon the text). The old fixed 300-char cap silently
        # discarded faithful long rewrites — anything past it fell back to the
        # original, which is fine for length but masked the real failure mode.
        ceiling = max(400, len(original) * 2)
        if len(rewritten) < 5 or len(rewritten) > ceiling:
            logger.info(
                "[Speak gate] Bridge output rejected (length=%d, ceiling=%d): %r",
                len(rewritten),
                ceiling,
                rewritten[:80],
            )
            return original
        # The real cut-off cause: the local model COMPRESSES the candidate into a
        # one-line summary instead of bridging it. A rewrite that lost a large
        # fraction of the original's length dropped content — keep the original.
        if len(rewritten) < 0.6 * len(original):
            logger.info(
                "[Speak gate] Bridge dropped content (len %d < 60%% of %d) — keeping original: %r",
                len(rewritten),
                len(original),
                rewritten[:80],
            )
            return original
        if rewritten.lstrip().startswith("{") or rewritten.lstrip().startswith("["):
            logger.info("[Speak gate] Bridge returned JSON-shaped output, ignoring")
            return original

        logger.info(
            "[Speak gate] Bridged (overlap=%.2f): %r → %r",
            overlap,
            original[:60],
            rewritten[:80],
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
            'Return JSON: {"verdict": "yes"|"wait"|"drop", "reason": "..."}',
        ]

        # Each judge call is its own logical "turn" for the cell's per-turn cap.
        judge_turn_id = f"judge_{int(time.time() * 1000)}"
        self._judge_cell.reset_turn(judge_turn_id)
        try:
            raw = await self._judge_cell.call(
                [{"role": "user", "content": "\n".join(prompt_lines)}]
            )
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

    def update_context(
        self,
        parietal_text: str,
        emotion: str | None = None,
        self_schema: str | None = None,
        speaker_name: str | None = None,
        relationship: dict | None = None,
    ) -> None:
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
        # Self-schema: preserve prior value if not supplied. Strip a legacy
        # "## Thinking frameworks" section if the stored self.md still carries
        # one — the catalog is injected below from dmn_prompts, and a copy in
        # the identity document would both duplicate it and eat the 8000-char
        # snippet budget.
        if self_schema:
            self_schema = re.sub(r"(?ms)^## Thinking frameworks\n.*?(?=^## |\Z)", "", self_schema)
            self._last_self_schema = self_schema[:8000]
        # Rebuild context blob with the LIVE parietal + most recent schema.
        from brain.dmn_prompts import FRAMEWORKS_CATALOG

        self._last_context = (
            f"Recent conversation:\n{parietal_text}\n\n"
            f"Self-model snippet:\n{getattr(self, '_last_self_schema', '')}\n\n"
            f"Thinking frameworks (reasoning tools — apply by name as lenses):\n"
            f"{FRAMEWORKS_CATALOG}"
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

    @staticmethod
    def _parse_projects(open_questions_text: str) -> list[dict]:
        """Structured parse of the "Projects assigned by Russ" section.
        Returns [{name, raw_name, priority, task, status}] — name is the clean
        display name, raw_name is the exact `### ...` header (for status rewrites)."""
        projects_m = re.search(
            r"## Projects assigned by Russ(.*?)(?=\n## |\Z)",
            open_questions_text,
            re.DOTALL,
        )
        if not projects_m:
            return []
        section = projects_m.group(1)
        out: list[dict] = []
        for block_m in re.finditer(r"### (.+?)\n(.*?)(?=\n### |\Z)", section, re.DOTALL):
            raw_name = block_m.group(1).strip()
            body = block_m.group(2)
            # Match "(PRIMARY)" and also "(PRIMARY — do this first)" — the marker
            # need not be immediately followed by the closing paren.
            priority_m = re.search(r"\(\s*(PRIMARY|secondary)\b", raw_name, re.I)
            priority = priority_m.group(1).upper() if priority_m else ""
            clean_name = (
                re.sub(r"\s*\([^)]*\)", "", raw_name).strip() if "(" in raw_name else raw_name
            )
            task_m = re.search(r"\*\*Task\*\*:\s*(.+?)(?:\n|$)", body)
            task_line = task_m.group(1).strip() if task_m else ""
            status_m = re.search(r"\*\*Status\*\*:\s*(.+?)(?:\n|$)", body)
            status = status_m.group(1).strip() if status_m else ""
            out.append(
                {
                    "name": clean_name,
                    "raw_name": raw_name,
                    "priority": priority,
                    "task": task_line,
                    "status": status,
                }
            )
        return out

    def set_projects_context(self, open_questions_text: str) -> None:
        """Extract and store the project manifest from open_questions.md. Called
        at boot and whenever open_questions.md is rewritten.

        Stores both a compact display digest (`_last_projects`, injected into the
        monologue prompt) and the structured list (`_projects`, used by the
        project scheduler to start/advance work). The full section (incl. the
        100-line folder map) is never injected — the DMN reads the file via its
        `task` field when it needs the details.
        """
        self._ensure_runtime_state()
        self._projects = self._parse_projects(open_questions_text)
        if not self._projects:
            self._last_projects = ""
            return
        lines: list[str] = []
        for p in self._projects:
            priority = f" ({p['priority']})" if p["priority"] else ""
            status = f" — {p['status']}" if p["status"] else ""
            entry = f"- **{p['name']}**{priority}{status}"
            if p["task"]:
                entry += f": {p['task'][:120]}"
            lines.append(entry)
        self._last_projects = "\n".join(lines)

    # ── Project scheduler (parallel track to rumination) ────────────────────

    _PROJECT_DONE_WORDS = ("done", "complete", "finished", "shipped")
    _PROJECT_BLOCKED_WORDS = ("blocked", "waiting on you", "waiting for you", "needs your")

    def _project_eligible(self, p: dict) -> bool:
        """A project is eligible to run if it has a task and isn't done or blocked
        on the user. Ongoing projects (e.g. 'treat as ongoing') stay eligible."""
        if not p.get("task"):
            return False
        s = (p.get("status") or "").lower()
        if any(w in s for w in self._PROJECT_DONE_WORDS):
            return False
        return not any(w in s for w in self._PROJECT_BLOCKED_WORDS)

    def next_project_goal(self) -> tuple[str, str] | None:
        """Pick the next project step to START, or None. Only one project runs at
        a time (it executes in the background while rumination continues). PRIMARY
        projects are preferred; secondary picked up only when no PRIMARY is
        eligible. Round-robin within the chosen tier so work cycles fairly."""
        self._ensure_runtime_state()
        if self._project_in_flight:
            return None  # one project at a time — let it finish, then check in
        eligible = [p for p in self._projects if self._project_eligible(p)]
        if not eligible:
            return None
        primaries = [p for p in eligible if p["priority"] == "PRIMARY"]
        pool = primaries if primaries else eligible
        p = pool[self._project_rotation_idx % len(pool)]
        self._project_rotation_idx += 1
        return (p["name"], p["task"])

    def note_project_started(self, name: str, task_id: str) -> None:
        self._ensure_runtime_state()
        self._project_in_flight = name
        self._project_task_id = task_id
        logger.info("[DMN] Project step started in background: %r (task %s)", name, task_id)

    def is_project_task(self, task_id: str) -> bool:
        return bool(task_id) and getattr(self, "_project_task_id", None) == task_id

    async def note_project_complete(self, task_id: str, success: bool, summary: str = "") -> None:
        """Check-in: a project's background step finished. Update its status in
        open_questions.md so progress is durable, clear the in-flight slot, and
        let the next idle cycle start the next eligible project."""
        self._ensure_runtime_state()
        if not self.is_project_task(task_id):
            return
        name = self._project_in_flight
        self._project_in_flight = None
        self._project_task_id = None
        if name:
            stamp = time.strftime("%Y-%m-%d %H:%M")
            note = f"In progress — last worked {stamp} ({'ok' if success else 'failed'})"
            if summary:
                note += f": {summary.strip()[:120]}"
            await self._update_project_status(name, note)
            logger.info("[DMN] Project step complete → status updated: %r", name)

    async def note_project_blocked(self, task_id: str, reason: str = "") -> None:
        """A project step is blocked waiting on the user. Clear the in-flight slot
        (so other projects can run) and mark the status blocked so it isn't
        re-picked until the user unblocks it."""
        self._ensure_runtime_state()
        if not self.is_project_task(task_id):
            return
        name = self._project_in_flight
        self._project_in_flight = None
        self._project_task_id = None
        if name:
            note = "Blocked — waiting on you"
            if reason:
                note += f": {reason.strip()[:120]}"
            await self._update_project_status(name, note)
            logger.info("[DMN] Project step blocked on user: %r", name)

    async def _update_project_status(self, name: str, new_status: str) -> None:
        """Rewrite a single project's **Status** line in open_questions.md (or add
        one), then refresh the projects context. Section-scoped to that project."""
        schema = self._schema_store()
        if schema is None:
            return
        try:
            text = schema.read(ot.LEDGER_FILE)
            if not text:
                return
            proj = next((p for p in self._projects if p["name"] == name), None)
            raw_name = proj["raw_name"] if proj else name
            block_re = re.compile(
                r"(### " + re.escape(raw_name) + r"\n)(.*?)(?=\n### |\n## |\Z)", re.DOTALL
            )
            m = block_re.search(text)
            if not m:
                return
            body = m.group(2)
            if re.search(r"\*\*Status\*\*:", body):
                new_body = re.sub(
                    r"\*\*Status\*\*:[^\n]*", f"**Status**: {new_status}", body, count=1
                )
            else:
                new_body = body.rstrip() + f"\n- **Status**: {new_status}\n"
            new_text = text[: m.start(2)] + new_body + text[m.end(2) :]
            await schema.awrite(ot.LEDGER_FILE, new_text)
            self.set_projects_context(new_text)
        except Exception as e:
            logger.warning("[DMN] Could not update project status for %r: %s", name, e)

    # ── Deferred thoughts ────────────────────────────────────────────────────

    def _append_deferred_thought(
        self, text: str, urgency: str = "high", tags: list[str] | None = None
    ) -> None:
        """Write immediate/high urgency thoughts to deferred_thoughts.md for
        explicit surfacing on user return. Normal/low urgency thoughts are stored
        only in episodic memory (handled by the hippocampus encode call) and
        surface naturally when a matching topic comes up in conversation."""
        if urgency not in ("immediate", "high"):
            logger.info(
                "[DMN] Deferred thought (urgency=%s) — episodic memory only: %r", urgency, text[:80]
            )
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
            logger.info(
                "[DMN] Deferred thought saved (urgency=%s, tags=%s): %r", urgency, tags, text[:80]
            )
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
            entries = "\n".join(ln for ln in lines if not ln.startswith("# Deferred"))
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
            and len(
                DEFERRED_THOUGHTS_PATH.read_text(encoding="utf-8")
                .strip()
                .replace("# Deferred Thoughts", "")
                .strip()
            )
            > 0
        )
        has_proposals = PROPOSALS_DIR.exists() and any(PROPOSALS_DIR.glob("*.md"))
        return has_thoughts or has_proposals

    # ── Proposal planning ─────────────────────────────────────────────────────

    async def _run_planning_pass(self, seed_thought: str, turn_id: str) -> None:
        """Elaborate a seed thought into a structured proposal doc using the
        local-general model. Saves to proposals/ directory. Never executes work."""
        from datetime import datetime

        plan_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        self._planner_cell.reset_turn(f"{turn_id}_plan")

        # Pick a thinking framework for the planner via autonomous selector.
        # The planner is the DMN's heaviest thinker — give it the deepest framework support.
        picked_skills: list[str] = []
        if self._skill_selector is not None:
            try:
                bundle = await self._skill_selector.select_autonomous(
                    prompt=seed_thought,
                    turn_id=f"{turn_id}_plan",
                )
                if bundle:
                    picked_skills = list(bundle.tier1) + list(bundle.chosen)
                    from brain.observability.decisions import decisions as _decisions

                    _decisions.log(
                        "dmn_skill_pick",
                        turn_id=turn_id,
                        cluster="dmn",
                        cell="planner",
                        chosen=bundle.chosen,
                        pick_source=bundle.pick_source,
                    )
            except Exception as e:
                logger.debug("[DMN] Planner skill selection failed: %s", e)
        self._planner_cell.skills = picked_skills

        self._router.enter_background_mode()
        try:
            raw = await self._planner_cell.call(
                [
                    {
                        "role": "user",
                        "content": f"Seed idea:\n{seed_thought}\n\n"
                        f"Current date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                        f"Write a proposal document based on this idea.",
                    }
                ]
            )
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
        slug = (
            re.sub(r"[^a-z0-9]+", "-", slug_match.group(1).lower())[:40] if slug_match else "idea"
        )
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
                results.append(
                    {
                        "filename": p.name,
                        "path": str(p),
                        "title": title_m.group(1).strip() if title_m else p.stem,
                        "status": status_m.group(1).strip() if status_m else "unknown",
                        "proposed": proposed_m.group(1).strip() if proposed_m else "",
                    }
                )
            except Exception:
                pass
        return results

    def _parse_monologue_response(self, raw: str) -> dict | None:
        """Parse JSON from the monologue cell. Retries once after stripping invalid +N syntax."""
        if not isinstance(raw, str):
            return None
        candidate = raw.strip()
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass
        try:
            fixed = re.sub(r":\s*\+(\d)", r": \1", raw.strip())
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
        ach = snap.get("ACh", 0.0)
        glu = snap.get("Glu", 0.0)
        gaba = snap.get("GABA", 0.0)

        suppression = ach * settings.get("ach_suppression_weight") + glu * settings.get(
            "glu_suppression_weight"
        )

        # Moderate GABA (anxious but not frozen) → more rumination, not less
        if 0.2 <= gaba < 0.6:
            suppression = max(0.0, suppression - settings.get("gaba_suppression_reduction"))

        return min(settings.get("suppression_skip_prob_max"), suppression)

    def _chem_snapshot(self) -> dict[str, float]:
        """Merged neuromod + hormonal snapshot for switch modulation.

        When flock_dynamics is on, also carries the per-turn trajectory of each
        channel under `vel_<CH>` keys (e.g. `vel_CORT`). These are 0-centred
        derivatives, NOT 0.5-centred levels, so they are consumed by explicit
        velocity-aware logic (rumination drive, idle gate) rather than the
        generic 0.5-centred SwitchNeuron modulator path."""
        try:
            nm = self._bus.neuromod.snapshot()
        except Exception:
            nm = {}
        try:
            hs = self._bus.hormonal.snapshot()
        except Exception:
            hs = {}
        snap = {**nm, **hs}
        if settings.get("flock_dynamics", 0):
            try:
                for ch, v in self._bus.neuromod.velocity().items():
                    snap[f"vel_{ch}"] = v
                for ch, v in self._bus.hormonal.velocity().items():
                    snap[f"vel_{ch}"] = v
            except Exception:
                pass
        return snap

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
            idle = 0.0
        interval = idle_base if idle > 60.0 else base
        # Skip-and-backoff: while the local model is failing, lengthen the
        # interval geometrically so we stop hammering it. _backoff_mult is 1.0
        # when healthy and reset on the first successful tick.
        return interval * self._backoff_mult

    async def _loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self._current_interval())
                if not self._running:
                    break
                if self._skip_next_tick:
                    self._skip_next_tick = False
                    logger.debug("[Background reflection] Tick skipped — turn in progress")
                    continue
                # Idle decay runs FIRST, every loop iteration. If it only ran
                # inside _tick, a skipped tick would never decay — meaning once
                # ACh climbs high enough to suppress, it would stay high and
                # suppress forever. Running it here gives suppressed ticks a
                # chance to recover.
                self._idle_decay()
                # Chemistry idle-gate: hard block when chemistry says the
                # brain shouldn't be mind-wandering (alert/defensive states).
                chem = self._chem_snapshot()
                # flock_dynamics (1): a sharply RISING worry trajectory (CORT/NE
                # climbing) boosts the gate input — equivalent to lowering the
                # gate threshold — so escalating stress can intrude on otherwise
                # quiet idle states. Steady-high stress (velocity ≈ 0) does not.
                gate_level = 0.6
                if settings.get("flock_dynamics", 0):
                    worry_vel = max(0.0, float(chem.get("vel_CORT", 0.0))) + max(
                        0.0, float(chem.get("vel_NE", 0.0))
                    )
                    gate_level += min(
                        float(settings.get("flock_idle_gate_vel_nudge", 0.0)),
                        float(settings.get("flock_idle_gate_vel_nudge", 0.0)) * worry_vel,
                    )
                if not self._idle_gate.should_fire(
                    gate_level, chem, turn_id=f"dmn_{self._thought_count}"
                ):
                    logger.debug(
                        "[Background reflection] Tick suppressed by idle_gate "
                        "(NE=%.2f GABA=%.2f 5HT=%.2f OXT=%.2f)",
                        chem.get("NE", 0),
                        chem.get("GABA", 0),
                        chem.get("5HT", 0),
                        chem.get("OXT", 0),
                    )
                    if self._obs:
                        eff = self._idle_gate.effective_threshold(chem)
                        self._obs.record_modulation_event(
                            "idle_gate",
                            "dmn",
                            suppressed=True,
                            chem=chem,
                            level=0.6,
                            effective_threshold=eff,
                        )
                    continue
                self._idle_gate.fire(0.6, "tick_allowed", snapshot=chem)
                skip_prob = self._tick_skip_probability()
                if random.random() < skip_prob:
                    snap = self._bus.neuromod.snapshot()
                    logger.debug(
                        "[Background reflection] Tick suppressed "
                        "(skip_prob=%.2f ACh=%.2f Glu=%.2f GABA=%.2f)",
                        skip_prob,
                        snap["ACh"],
                        snap["Glu"],
                        snap["GABA"],
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
            lines.append(f"Speaker: {self._last_speaker_name} ({self._last_familiarity})")
        else:
            lines.append("Speaker: unknown (new)")
        try:
            idle_s = int(get_idle_seconds())
            lines.append(
                f"OS-idle seconds: {idle_s}  ({'user away' if idle_s > 60 else 'user present'})"
            )
        except Exception:
            pass
        return "\n".join(lines)

    def _ensure_runtime_state(self) -> None:
        """Lazily initialize resilience / rumination state.

        Production sets these in __init__; this guard keeps test skeletons that
        build the DMN via __new__ (bypassing __init__) working without each one
        having to know the full attribute set."""
        if not hasattr(self, "_recent_embeddings"):
            self._recent_embeddings = deque(maxlen=DMN_RECENT_THOUGHTS)
        if not hasattr(self, "_recent_frames"):
            self._recent_frames = deque(maxlen=DMN_RECENT_FRAMES)
        for attr, default in (
            ("_memory_seed", ""),
            ("_consec_errors", 0),
            ("_consec_suppressed", 0),
            ("_backoff_mult", 1.0),
            ("_last_tick_latency", 0.0),
            ("_last_tick_failed", False),
            ("_last_rumination_seed", ""),
            ("_consecutive_ruminations", 0),
            ("_ruminations_in_progress", 0),
        ):
            if not hasattr(self, attr):
                setattr(self, attr, default)
        if not hasattr(self, "_open_threads"):
            self._open_threads = []
        if not hasattr(self, "_recent_conclusions"):
            self._recent_conclusions = deque(maxlen=5)
        for attr, default in (
            ("_projects", []),
            ("_project_in_flight", None),
            ("_project_task_id", None),
            ("_project_rotation_idx", 0),
            ("_last_projects", ""),
        ):
            if not hasattr(self, attr):
                setattr(self, attr, default)
        if not hasattr(self, "_routing_weights"):
            self._routing_weights = {}
        if not hasattr(self, "_routing_weights_loaded"):
            self._routing_weights_loaded = {}
        if not hasattr(self, "_last_routed_ids"):
            self._last_routed_ids = []
        if not hasattr(self, "_user_msg_lens"):
            self._user_msg_lens = deque(maxlen=6)
        if not hasattr(self, "_user_topics"):
            self._user_topics = deque(maxlen=6)
        if not hasattr(self, "_skill_selector"):
            self._skill_selector = None
        if not hasattr(self, "_monologue_baseline_skills"):
            self._monologue_baseline_skills = []

    async def _run_step(self, name: str, coro) -> None:
        """Run a secondary tick step in isolation — a failure logs + skips that
        step without aborting the tick. (Secondary steps don't drive backoff;
        only the monologue/rumination model probe does.)"""
        try:
            await coro
        except Exception as e:  # noqa: BLE001 — deliberate per-step isolation
            self._record_step_failure(name, e)

    def _record_step_failure(self, step: str, exc: Exception) -> None:
        logger.warning("[Background reflection] Step %r failed: %s", step, exc)
        if self._obs is not None:
            with contextlib.suppress(Exception):
                self._obs.record_dmn_failure(step=step, error=str(exc)[:200])

    def _note_tick_outcome(self, model_ok: bool) -> None:
        """Update skip-and-backoff state from whether the model produced a thought.
        Healthy → reset; failing → count and lengthen the interval geometrically."""
        after = int(settings.get("dmn_backoff_after_failures") or 2)
        factor = float(settings.get("dmn_backoff_factor") or 2.0)
        max_mult = float(settings.get("dmn_backoff_max_multiplier") or 8.0)
        if model_ok:
            if self._consec_errors or self._backoff_mult != 1.0:
                logger.info(
                    "[Background reflection] Recovered after %d failure(s) — backoff reset",
                    self._consec_errors,
                )
            self._consec_errors = 0
            self._backoff_mult = 1.0
            self._last_tick_failed = False
        else:
            self._consec_errors += 1
            self._last_tick_failed = True
            if self._consec_errors >= after:
                self._backoff_mult = min(max_mult, factor ** (self._consec_errors - after + 1))
            logger.warning(
                "[Background reflection] Model-failure tick #%d — backoff x%.1f "
                "(freeing the local model for other subsystems)",
                self._consec_errors,
                self._backoff_mult,
            )
            if self._obs is not None:
                with contextlib.suppress(Exception):
                    self._obs.record_dmn_failure(
                        step="tick",
                        error="model unavailable",
                        consecutive=self._consec_errors,
                        backoff=self._backoff_mult,
                    )

    async def _tick(self) -> None:
        self._ensure_runtime_state()
        self._thought_count += 1
        turn_id = f"dmn_{self._thought_count}"
        t_start = time.time()

        # Refresh parietal slice so the monologue always sees the live conversation
        if self._parietal is not None:
            with contextlib.suppress(Exception):
                self.update_context(self._parietal.recent_turns_text())

        # Periodically surface a random long-term memory as associative fuel (idle only).
        with contextlib.suppress(Exception):
            self._maybe_inject_memory_seed()

        chem = self._chem_snapshot()

        # Decide this tick's mode. Rumination (deepen a single seed through skill
        # packages) is eligible ONLY when idle + chemistry favors it; otherwise a
        # normal fresh, dedup-gated thought. model_ok drives skip-and-backoff.
        mode, flavor, drive = self._rumination_decision(chem)
        model_ok = True

        if mode == "ruminate":
            try:
                model_ok = await self._run_rumination(turn_id, chem, flavor, drive)
            except Exception as e:  # noqa: BLE001
                self._record_step_failure("rumination", e)
                model_ok = False
        else:
            try:
                # On idle, sufficiently-interested ticks, vary the analytical
                # framework before generating (resets to baseline each tick).
                await self._apply_monologue_skills(turn_id, chem, drive)
                thought_clean, metadata = await self._run_monologue(turn_id, chem)
                if thought_clean:
                    await self._process_thought(thought_clean, metadata, turn_id)
                else:
                    model_ok = False  # empty output → model likely unavailable
            except Exception as e:  # noqa: BLE001
                self._record_step_failure("monologue", e)
                model_ok = False

        # DIAG: which path produced no thought, so empty ticks on a healthy model
        # can be traced to monologue vs rumination (and correlated with the
        # model_router strip-to-empty warning).
        if not model_ok:
            logger.warning(
                "[Background reflection] DIAG empty tick — mode=%s flavor=%s drive=%.2f",
                mode,
                flavor,
                drive,
            )

        # Secondary steps — each isolated; they do not drive backoff.
        if self._thought_count % 3 == 0 and self._parietal:
            await self._run_step("simulation", self._run_simulation(turn_id))

        if self.last_was_question and not self.anticipations:
            await self._run_step("anticipator", self._run_anticipator(turn_id))

        if self._thought_count % 4 == 0 and self._hippocampus is not None and not self.prefetched:
            await self._run_step("prefetcher", self._run_prefetcher(turn_id))

        # Phase 6 (colony features): silence-triggered recall. When a topic that was
        # active goes quiet (a fresh ARMED→QUIET edge), recall what surrounded it
        # while it was hot — hippocampal replay during quiescence. Idle-gated and
        # debounced (fire-once per quiet onset).
        if settings.get("colony_features", 0) and self._hippocampus is not None:
            await self._run_step("silence_recall", self._run_silence_recall(turn_id))

        self._last_tick_latency = time.time() - t_start
        self._note_tick_outcome(model_ok)

    # ── Rumination router (idle-only, chemistry dual-driver) ─────────────────

    def _rumination_drive(self, chem: dict) -> tuple[float, str]:
        """Compute the rumination drive and its affective flavor from chemistry.

        Maps to the neuroscience of rumination: it rises both under WORRY
        (cortisol + norepinephrine high, serotonin low — the can't-disengage
        signature) and under high INTEREST (dopamine "wanting" + acetylcholine
        focus). Serotonin subtracts because it enables disengagement.
        """
        cort = float(chem.get("CORT", 0.0))
        ne_raw = float(chem.get("NE", 0.0))
        da_raw = float(chem.get("DA", 0.5))
        ach_raw = float(chem.get("ACh", 0.0))
        sht = float(chem.get("5HT", 0.0))
        ne = max(0.0, ne_raw - 0.30)  # over alert-baseline
        da = max(0.0, da_raw - 0.50)  # over neutral
        ach = max(0.0, ach_raw - 0.50)
        drive = (
            settings.get("rum_w_cort") * cort
            + settings.get("rum_w_ne") * ne
            + settings.get("rum_w_da") * da
            + settings.get("rum_w_ach") * ach
            - settings.get("rum_w_5ht") * sht
        )
        # flock_dynamics (1): trajectory term — *rising* stress/interest drives
        # rumination harder than a steady-high level (murmuration hysteresis:
        # an escalating threat warrants fresh vigilant rumination; a chronic,
        # adapted one should habituate). Only positive (rising) velocity counts;
        # falling chemistry does not suppress below the level-based drive.
        if settings.get("flock_dynamics", 0):
            drive += (
                float(settings.get("flock_rum_w_cort_vel", 0.0))
                * max(0.0, float(chem.get("vel_CORT", 0.0)))
                + float(settings.get("flock_rum_w_ne_vel", 0.0))
                * max(0.0, float(chem.get("vel_NE", 0.0)))
                + float(settings.get("flock_rum_w_da_vel", 0.0))
                * max(0.0, float(chem.get("vel_DA", 0.0)))
            )
        flavor = "anxious" if (cort + ne_raw) > (da_raw + ach_raw) else "engaged"
        return max(0.0, drive), flavor

    def _rumination_decision(self, chem: dict) -> tuple[str, str, float]:
        """Return (mode, flavor, drive). mode ∈ {"normal", "ruminate"}.

        Idle is a hard precondition: rumination NEVER fires during live
        conversation (attention is external; deepening a stale seed would make
        the brain feel unresponsive). Only when idle does the chemistry drive
        probabilistically route the tick to rumination.
        """
        drive, flavor = self._rumination_drive(chem)
        if not settings.get("dmn_rumination_enabled"):
            return "normal", flavor, drive
        idle = self._effective_idle_seconds()
        idle_threshold = float(settings.get("dmn_rumination_idle_threshold_s") or 60.0)
        if idle < idle_threshold:
            return "normal", flavor, drive
        # TONIC idle drive (Stage 7): the phasic worry/interest `drive` decays to ~0 during deep
        # idle, so on its own it never crossed threshold — rumination never fired. The DMN is most
        # active at rest, so add a mind-wandering (boredom) + finish-out (unfinished business) pull
        # that GROWS while idle, persona-scaled by a chemistry-derived ruminative disposition.
        drive += self._tonic_idle_drive(chem, idle, idle_threshold)
        threshold = float(settings.get("dmn_rumination_drive_threshold") or 0.45)
        if drive < threshold:
            return "normal", flavor, drive
        # Depth cap — don't deepen the same seed forever.
        if self._consecutive_ruminations >= int(
            settings.get("dmn_rumination_max_consecutive") or 2
        ):
            return "normal", flavor, drive
        p = float(settings.get("dmn_rumination_prob_at_threshold") or 0.5)
        if random.random() < min(1.0, p * (drive / max(0.01, threshold))):
            return "ruminate", flavor, drive
        return "normal", flavor, drive

    def _reward_angle_prediction(self, actual_angle: str) -> None:
        """Stage 7 Gap 1: reward a confident, non-trivial thought-angle prediction the next
        thought confirmed (dip on a confident miss). Self-verified — no user. Consumes the stash
        so it scores once. Best-effort."""
        predicted = getattr(self, "_last_predicted_angle", None)
        if not predicted:
            return
        self._last_predicted_angle = None  # consume regardless of outcome
        with contextlib.suppress(Exception):
            from brain.neuron import prediction_reward, reward_weight

            conf = float(getattr(self, "_last_angle_confidence", 0.0))
            info = float(getattr(self, "_last_angle_informativeness", 0.0))
            pr = prediction_reward(conf, actual_angle == predicted, info)
            if not pr:
                return
            persona = str(settings.get("persona_name", ""))
            delta = (
                pr
                * float(settings.get("prediction_reward_base"))
                * reward_weight(persona, "correctness")
                * float(settings.get("emotional_reactivity_scale"))
            )
            cap = float(settings.get("prediction_reward_turn_cap"))
            self._bus.neuromod.add("DA", max(-cap, min(cap, delta)))

    def _reward_idle_thought_quality(
        self, thought: str, max_overlap: float, max_cos: float
    ) -> None:
        """Stage 7 Gap 2: cheap heuristic quality (novelty + length sanity, NO LLM) → DA reward
        for a good idle thought, persona-scaled by the 'novelty' valuation (curiosity-driven
        personas get more). Threshold-gated so filler earns nothing. Best-effort."""
        with contextlib.suppress(Exception):
            novelty_word = 1.0 - float(max_overlap)
            novelty_sem = 1.0 - max(0.0, float(max_cos) - 0.6) / 0.4  # only penalise cos>0.6
            novelty = 0.6 * novelty_word + 0.4 * max(0.0, min(1.0, novelty_sem))
            wc = len((thought or "").split())
            reach = 1.0 if 12 <= wc <= 400 else (0.5 if 6 <= wc <= 700 else 0.2)
            quality = max(0.0, min(1.0, 0.7 * novelty + 0.3 * reach))
            if quality < float(settings.get("idle_thought_quality_min")):
                return
            from brain.neuron import reward_weight

            persona = str(settings.get("persona_name", ""))
            delta = (
                float(settings.get("idle_thought_quality_base"))
                * quality
                * reward_weight(persona, "novelty")
                * float(settings.get("emotional_reactivity_scale"))
            )
            self._bus.neuromod.add("DA", delta)

    def _effective_idle_seconds(self) -> float:
        """Seconds since the user was last active. Uses OS HID idle when available (macOS) and
        a conversation-idle fallback (now − last turn start) otherwise — get_idle_seconds()
        returns 0.0 on Linux, which would silently disable idle-gated cognition on the hosted
        instance. max() is correct in all four cases (mac/linux × active/idle)."""
        try:
            os_idle = get_idle_seconds()
        except Exception:
            os_idle = 0.0
        # convo_idle is only meaningful once a turn has actually stamped activity (pause()).
        # Without a stamp, don't let an uninitialised timestamp fabricate idleness — trust OS.
        last_active = float(getattr(self, "_last_user_activity_ts", 0.0))
        if last_active <= 0.0:
            return os_idle
        return max(os_idle, max(0.0, time.time() - last_active))

    def _tonic_idle_drive(self, chem: dict, idle: float, idle_threshold: float) -> float:
        """Mind-wandering + finish-out pull that grows during deep idle (Stage 7). Independent of
        the phasic worry/interest drive (which decays to ~0 at rest). Two terms, persona-scaled by
        a chemistry-derived ruminative disposition (high ACh + low 5HT + CORT → chews more; Poet
        most, Sage least), so deep idle itself can carry the entity over the rumination threshold."""
        boredom = min(
            1.0,
            max(0.0, idle - idle_threshold)
            / max(1.0, float(settings.get("rum_idle_saturation_s") or 300.0)),
        )
        try:
            max_adv = max((int(getattr(t, "advances", 0)) for t in self._open_threads), default=0)
        except Exception:
            max_adv = 0
        unfinished = min(1.0, max_adv / max(1.0, float(settings.get("rum_unfinished_cap") or 4.0)))
        tonic = (
            float(settings.get("rum_w_boredom") or 0.0) * boredom
            + float(settings.get("rum_w_unfinished") or 0.0) * unfinished
        )
        # Ruminative disposition from chemistry: focus (ACh) + can't-disengage (low 5HT) + stress.
        # Floor at 0.8 so even the least-ruminative persona (high-5HT Sage) still crosses the
        # threshold under SUSTAINED idle — divergence is in how SOON/OFTEN it ruminates, not
        # whether it ever does.
        ach = float(chem.get("ACh", 0.0))
        sht = float(chem.get("5HT", 0.0))
        cort = float(chem.get("CORT", 0.0))
        disposition = max(0.8, min(1.6, 0.7 + 0.6 * ach + 0.5 * (0.5 - sht) + 0.4 * cort))
        return tonic * disposition

    def _current_seed_thread(self):
        """The open thread rumination should work on next: FINISH-OUT — the
        most-advanced thread (closest to a conclusion), tie-broken by momentum
        (most recently advanced). Finishing a thought before starting a new one is
        preferable, and fixation is already bounded by the consecutive-rumination
        cap (deepens the same seed at most N times in a row) and the advance cap
        (auto-concludes at THREAD_MAX_ADVANCES) — so finish-out can't get stuck.
        None if no threads are open."""
        open_threads = [t for t in getattr(self, "_open_threads", []) if t.status == ot.STATUS_OPEN]
        if not open_threads:
            return None
        return max(open_threads, key=lambda t: (t.advances, t.last_ts))

    def _current_seed(self) -> str:
        """The current preoccupation to ruminate on / vary skills around. Prefers
        an open thread (so rumination advances tracked work) over the last
        thought, falling back to recent thoughts / context."""
        seed_thread = self._current_seed_thread()
        if seed_thread is not None:
            return seed_thread.summary.strip()
        if self._recent_thoughts:
            return str(self._recent_thoughts[-1]).strip()
        return (self._last_context or "").strip()

    async def _run_rumination(self, turn_id: str, chem: dict, flavor: str, drive: float) -> bool:
        """Run one bounded rumination episode and emit the synthesized take.
        Returns True if a thought was produced (model_ok), False otherwise."""
        selector = getattr(self, "_skill_selector", None)
        if selector is None:
            return False
        seed = self._current_seed()
        if not seed:
            return False

        self._ruminations_in_progress += 1
        try:
            final, chain = await selector.ruminate(
                seed,
                max_iters=int(settings.get("dmn_rumination_max_iters") or 4),
                time_budget_s=int(settings.get("dmn_rumination_time_budget_s") or 25),
                turn_id=f"{turn_id}_rum",
                flavor=flavor,
            )
        finally:
            self._ruminations_in_progress -= 1

        steps = max(0, len(chain) - 1)
        # Per-step costs so anxious rumination self-limits: GABA accrues (a small
        # cognitive cost), ACh wanes (engaged interest satiates). Bounded by the
        # chem clamps elsewhere.
        if steps:
            self._bus.neuromod.add("GABA", float(settings.get("rum_step_gaba_cost")) * steps)
            self._bus.neuromod.add("ACh", -float(settings.get("rum_step_satiation_cost")) * steps)

        if not final or not final.strip():
            return False

        # Depth tracking for the consecutive-rumination cap.
        if seed == self._last_rumination_seed:
            self._consecutive_ruminations += 1
        else:
            self._consecutive_ruminations = 1
            self._last_rumination_seed = seed

        self._log_rumination(turn_id, flavor, drive, chain)

        # Emit the synthesized take. It is EXEMPT from the "too similar to its own
        # seed" check (deepening the seed is the whole point) but still deduped
        # against OTHER recent thoughts, and tagged so the UI/consolidation can
        # tell ruminations apart from fresh thoughts.
        metadata = {
            "angle": f"rumination:{flavor}",
            "spoken_form": None,
            "task_goal": None,
            "is_propose": False,
            "is_plan": False,
            "defer_text": None,
            "defer_urgency": "high",
            "defer_tags": [],
            "chem_delta": {},
        }
        # If this rumination is deepening a tracked open thread, route the synthesis
        # back into the ledger: advance it each pass, and CONCLUDE it once the
        # consecutive-rumination depth cap is reached — "make progress, then retire"
        # so a thread can't be ruminated on forever.
        seed_thread = self._current_seed_thread()
        if seed_thread is not None and seed_thread.summary.strip() == seed:
            cap = int(settings.get("dmn_rumination_max_consecutive") or 2)
            if self._consecutive_ruminations >= cap:
                metadata["conclude_thread_id"] = seed_thread.id
                metadata["conclusion"] = final.strip()
                metadata["conclusion_confidence"] = "confident"
            else:
                metadata["advance_thread_id"] = seed_thread.id
        # Stage 7 Gap 3: reward the EFFORT of deepening through skill packages — even when the
        # episode doesn't conclude a thread (concluding already pays mastery via _resolve_thread,
        # so gate it out to avoid double-counting). Scales with depth (steps) via the
        # expectation-gap curve, persona-weighted by mastery valuation.
        if steps > 0 and "conclude_thread_id" not in metadata:
            with contextlib.suppress(Exception):
                from brain.neuron import accomplishment_factor, reward_weight

                _diff, _mod = accomplishment_factor(
                    float(steps), float(settings.get("accomplishment_expected_low"))
                )
                _w = reward_weight(str(settings.get("persona_name", "")), "mastery")
                _er = float(settings.get("emotional_reactivity_scale"))
                self._bus.neuromod.add(
                    "DA", float(settings.get("accomplishment_base")) * _diff * _mod * _w * _er
                )
        await self._process_thought(
            final.strip(),
            metadata,
            turn_id,
            exempt_seed=seed,
            source_tag="rumination",
        )
        return True

    async def _apply_monologue_skills(self, turn_id: str, chem: dict, drive: float) -> None:
        """Vary the monologue's analytical framework on idle, interested ticks.
        Resets to the static baseline first so a low-drive tick uses the default."""
        baseline = getattr(self, "_monologue_baseline_skills", [])
        self._monologue_cell.skills = list(baseline)
        selector = getattr(self, "_skill_selector", None)
        if selector is None:
            return
        if drive < float(settings.get("dmn_skill_vary_drive_threshold") or 0.30):
            return
        try:
            idle = get_idle_seconds()
        except Exception:
            idle = 0.0
        if idle < float(settings.get("dmn_rumination_idle_threshold_s") or 60.0):
            return
        seed = self._current_seed()
        if not seed:
            return
        try:
            bundle = await selector.select_autonomous(prompt=seed, turn_id=f"{turn_id}_skill")
        except Exception as e:  # noqa: BLE001
            logger.debug("[Background reflection] Monologue skill selection failed: %s", e)
            return
        if bundle and bundle.chosen:
            self._monologue_cell.skills = list(baseline) + [
                s for s in bundle.chosen if s not in baseline
            ]
            self._log_skill_pick(turn_id, "monologue", bundle, drive)

    def _log_skill_pick(self, turn_id: str, cell: str, bundle, drive: float) -> None:
        with contextlib.suppress(Exception):
            from brain.observability.decisions import decisions as _decisions

            _decisions.log(
                "dmn_skill_pick",
                turn_id=turn_id,
                cluster="dmn",
                cell=cell,
                chosen=list(bundle.chosen),
                pick_source=getattr(bundle, "pick_source", ""),
                drive=round(drive, 3),
            )

    def _log_rumination(self, turn_id: str, flavor: str, drive: float, chain: list[dict]) -> None:
        with contextlib.suppress(Exception):
            from brain.observability.decisions import decisions as _decisions

            _decisions.log(
                "dmn_rumination",
                turn_id=turn_id,
                cluster="dmn",
                flavor=flavor,
                drive=round(drive, 3),
                steps=max(0, len(chain) - 1),
                skills=[c.get("skill") for c in chain if c.get("skill")],
                modes=[c.get("mode") for c in chain if c.get("mode") and c.get("mode") != "seed"],
                consecutive=self._consecutive_ruminations,
            )

    def _maybe_inject_memory_seed(self) -> None:
        """Every DMN_MEMORY_SEED_EVERY ticks, while idle, pull a random episode from
        long-term memory and stash a compact form in self._memory_seed. The next
        monologue prompt surfaces it as associative fuel. Skipped during active
        conversation (a surfaced memory mid-exchange would derail coherence)."""
        if DMN_MEMORY_SEED_EVERY <= 0 or self._hippocampus is None:
            return
        if self._thought_count % DMN_MEMORY_SEED_EVERY != 0:
            return
        # Only when the user is idle — keep live-conversation ticks grounded in the
        # actual exchange, not a random old memory. 30s is enough separation from
        # the last message; 120s was too conservative and starved the model of fuel.
        try:
            if get_idle_seconds() < 30:
                return
        except Exception:
            pass
        # Sample a handful and prefer the one most connected to the live context,
        # rather than injecting a single uniformly-random (often irrelevant) memory.
        # A pure-random seed was the main reason proactive thoughts drifted onto
        # "things from before" that have nothing to do with the current moment.
        try:
            episodes = self._hippocampus._episodic.sample_random(6)
        except Exception as e:  # noqa: BLE001
            logger.debug("[Background reflection] Memory-seed sample failed: %s", e)
            return
        if not episodes:
            return

        ctx = self._last_context or ""

        def _ep_text(e: dict) -> tuple[str, str, list]:
            u = (e.get("user_input") or "").strip().replace("\n", " ")[:160]
            r = (e.get("entity_response") or "").strip().replace("\n", " ")[:160]
            return u, r, (e.get("topic_tags") or [])

        if ctx.strip():
            scored = []
            for e in episodes:
                u, r, tg = _ep_text(e)
                if not (u or r):
                    continue
                blob = " ".join([u, r, " ".join(str(t) for t in tg)])
                scored.append((_content_word_overlap(blob, ctx), e))
            if not scored:
                return
            best_overlap, ep = max(scored, key=lambda x: x[0])
            # Below the floor nothing in the sample connects to the moment — skip
            # this cycle rather than surface an unrelated memory.
            if best_overlap < DMN_MEMORY_SEED_MIN_OVERLAP:
                logger.debug(
                    "[Background reflection] Memory seed skipped — no relevant "
                    "episode in sample (best overlap=%.3f < %.3f)",
                    best_overlap,
                    DMN_MEMORY_SEED_MIN_OVERLAP,
                )
                return
        else:
            # No live context yet (e.g. fresh session) — keep the old behaviour
            # and just take the first sampled episode as associative fuel.
            ep = episodes[0]

        user, resp, tags = _ep_text(ep)
        if not (user or resp):
            return
        tag_str = f" [{', '.join(tags[:3])}]" if tags else ""
        parts = []
        if user:
            parts.append(f'they said "{user}"')
        if resp:
            parts.append(f'I replied "{resp}"')
        self._memory_seed = f"From an earlier conversation{tag_str}: " + "; ".join(parts)
        logger.debug("[Background reflection] Memory seed surfaced: %r", self._memory_seed[:80])

    async def _run_monologue(
        self, turn_id: str, chem: dict, startup: bool = False
    ) -> tuple[str, dict]:
        """Build prompt, call the monologue cell, parse response.

        Returns (thought_clean, metadata) where metadata keys are:
        angle, spoken_form, task_goal, is_propose, is_plan,
        defer_text, defer_urgency, defer_tags, chem_delta.
        Empty thought_clean means the cell returned nothing or parsing yielded nothing.
        """
        self._monologue_cell.reset_turn(turn_id)

        # Frame the context by how live it is. During idle there are no new turns,
        # so _last_context is a snapshot from minutes ago — labelling it "Recent
        # context" made the brain treat stale material as the current topic. Mark
        # the age so it frames old conversation as "earlier," not "now."
        if not self._last_context:
            context_label = "Recent context: none"
        else:
            try:
                idle_s = get_idle_seconds()
            except Exception:
                idle_s = 0.0
            if idle_s > 90:
                mins = int(idle_s // 60)
                context_label = (
                    f"Earlier conversation (the user went quiet ~{mins} min ago — "
                    f"you're mind-wandering now; this is NOT a live exchange, so don't "
                    f"reply to it as if it's the current topic):\n{self._last_context}"
                )
            else:
                context_label = f"Recent context:\n{self._last_context}"
        prompt_parts = [context_label, self._build_situation_block(chem)]
        if self._last_projects:
            prompt_parts.append(
                f"\nPRE-AUTHORIZED PROJECTS (work within these scopes auto-runs — "
                f"set `task` directly, no propose needed):\n{self._last_projects}"
            )
        # Operational capabilities — built-in tools the brain can already invoke
        # directly (e.g. trading get_quote/scan_watchlist). Without this the DMN
        # treats them as external things to "find/load" and generates goals like
        # "inventory the trading skill module" instead of just using them.
        if self._skill_selector is not None:
            try:
                _caps = self._skill_selector.capability_manifest()
            except Exception:
                _caps = ""
            if _caps:
                prompt_parts.append(
                    "\nOPERATIONAL CAPABILITIES — built-in tools you can invoke directly "
                    "right now. Do NOT plan to 'find', 'load', or 'inventory' these; they "
                    "already exist. If a thought calls for one, set `task` to use it:\n"
                    f"{_caps}"
                )
        # OPEN THREADS — steer toward advancing/concluding unfinished ideas rather
        # than always opening a new angle. This is the positive-progress signal
        # that counters the pure-novelty bias of the rest of the prompt.
        open_threads = [t for t in self._open_threads if t.status == ot.STATUS_OPEN]
        # Surface the most RECENTLY-touched threads, not the most-advanced ones.
        # Sorting by advances (the old insertion-order behaviour) let old, deeply
        # explored threads keep bubbling up over what's actually live, which read
        # as the brain "talking about things from before." Cap at 3 so a stale
        # backlog can't crowd the prompt.
        open_threads.sort(key=lambda t: t.last_ts or t.opened_ts or 0.0, reverse=True)
        open_threads = open_threads[:3]
        if open_threads:
            lines = []
            for t in open_threads:
                last = f" — last: {t.progress[-1][:80]}" if t.progress else ""
                lines.append(f"- [{t.id}] {t.summary[:100]} (advances: {t.advances}){last}")
            prompt_parts.append(
                "\nOPEN THREADS (unfinished ideas you've started — prefer ADVANCING one "
                "of these via `advance_thread_id`, or CONCLUDING it via `conclude_thread_id`, "
                "over opening a brand-new angle):\n" + "\n".join(lines)
            )
        # ALREADY CONCLUDED — settled recently; don't re-derive these. Age-decayed:
        # a conclusion older than CONCLUSION_FRESH_S is dropped from the prompt so
        # the brain stops referencing things it settled long ago as if they're
        # current. Entries are (ts, text) tuples; tolerate legacy bare strings.
        now_ts = time.time()
        concluded = []
        for c in getattr(self, "_recent_conclusions", []):
            if isinstance(c, tuple):
                ts, txt = c
                if now_ts - ts <= CONCLUSION_FRESH_S:
                    concluded.append(txt)
            elif isinstance(c, str):
                concluded.append(c)
        if concluded:
            prompt_parts.append(
                "\nALREADY CONCLUDED (treat as settled — build on or move past these, "
                "don't re-derive):\n" + "\n".join(f"- {c[:120]}" for c in concluded[-3:])
            )
        if self._recent_thoughts:
            # Show only the last few verbatim — dumping the whole window primes the
            # model to continue its own pattern. The angle list below carries the
            # wider "territory covered" signal without the priming cost.
            recent_block = "\n".join(
                f"- {t}" for t in list(self._recent_thoughts)[-DMN_PROMPT_PRIORS:]
            )
            prompt_parts.append(
                f"\nYour last few thoughts (do NOT repeat these — make a different move):\n"
                f"{recent_block}"
            )
        if self._recent_angles:
            angles_block = ", ".join(self._recent_angles)
            prompt_parts.append(
                f"\nConceptual territory already covered (choose a DIFFERENT angle):\n"
                f"{angles_block}"
            )
            # Detect cluster saturation and make it explicit to the model.
            from collections import Counter as _Counter

            prefixes = [a.rsplit("-", 1)[0] if "-" in a else a for a in self._recent_angles]
            top_cluster, top_count = _Counter(prefixes).most_common(1)[0]
            if top_count >= 3:
                prompt_parts.append(
                    f"\nATTENTION: You've spent {top_count} of the last {len(self._recent_angles)}"
                    f" thoughts on '{top_cluster}'. Break out entirely — pick a COMPLETELY"
                    f" different subject: the code, your own state, the broader world,"
                    f" a memory, a question about Russ, the project itself, anything"
                    f" unrelated to '{top_cluster}'."
                )
        if self._memory_seed and not startup:
            prompt_parts.append(
                f"\nA memory surfaced: {self._memory_seed}\n"
                "If it connects to anything, let it spark a concrete thought "
                "(a RECALL, a CONNECT, a QUESTION). If not, ignore it and think "
                "freely about something else. Don't narrate that a memory surfaced."
            )
            self._memory_seed = ""  # consume — surfaces once, not every tick until cleared

        if startup:
            if getattr(self, "_startup_first_meeting", False):
                prompt_parts.append(
                    "\nSESSION_START — FIRST MEETING: You have never spoken with this "
                    "person before. You have NO shared history; do not reference past "
                    "conversations, do not say 'good to be back' or 'since we last "
                    "spoke' — inventing familiarity is deception. Your first thought "
                    "should be about getting to know them: greet them as yourself, in "
                    "your own voice, and ask something genuine about who they are or "
                    "what's on their mind. Set speak=true with a self-contained spoken "
                    "form. Do NOT set a task this tick — and in early conversation, "
                    "any task you do set later should serve getting to know this "
                    "person, not studying your own internals."
                )
            else:
                prompt_parts.append(
                    "\nSESSION_START: A new session just began. Your first thought should "
                    "naturally reconnect with the person you're talking to — where you left off, "
                    "something you've been thinking about, or a warm 'good to be back' moment. "
                    "Only reference specifics that actually appear in your memory context — "
                    "never invent shared history. Set speak=true and write a self-contained "
                    "spoken form. Don't jump straight into tasks — greet first."
                )

        raw = await self._monologue_cell.call(
            [{"role": "user", "content": "\n".join(prompt_parts)}]
        )
        if not raw:
            logger.warning(
                "[Background reflection] Monologue cell returned empty — model may be unavailable"
            )
            return "", {}

        metadata: dict = {
            "angle": None,
            "spoken_form": None,
            "task_goal": None,
            "is_propose": False,
            "is_plan": False,
            "defer_text": None,
            "defer_urgency": "high",
            "defer_tags": [],
            "chem_delta": {},
            # Open-threads ledger fields (B6). A thought may advance/conclude a
            # thread AND stay purely cognitive — these are independent of
            # task/propose/plan.
            "open_thread": False,
            "advance_thread_id": "",
            "conclude_thread_id": "",
            "conclusion": "",
            "conclusion_confidence": "confident",  # "confident" | "uncertain"
            "bears_on": [],
            "bearing": "",
        }

        def _s(val, default: str = "") -> str:
            """Safely coerce a parsed JSON value to str — guards against the model
            returning a nested dict/list where a string field is expected."""
            return val if isinstance(val, str) else default

        parsed = self._parse_monologue_response(raw)
        if parsed is None:
            thought_clean = raw.strip() if isinstance(raw, str) else ""
        else:
            thought_clean = _s(parsed.get("thought")).strip()
            metadata["angle"] = _s(parsed.get("angle")).strip().lower() or None
            metadata["is_propose"] = bool(parsed.get("propose"))
            metadata["is_plan"] = bool(parsed.get("plan"))
            raw_defer = parsed.get("defer")
            if isinstance(raw_defer, dict):
                metadata["defer_text"] = _s(raw_defer.get("text")).strip()
                defer_urgency = _s(raw_defer.get("urgency"), "high").strip().lower()
                if defer_urgency not in ("immediate", "high", "normal", "low"):
                    defer_urgency = "high"
                metadata["defer_urgency"] = defer_urgency
                metadata["defer_tags"] = [str(t) for t in (raw_defer.get("topic_tags") or [])][:5]
            spoken = parsed.get("spoken")
            if parsed.get("speak") and isinstance(spoken, str) and spoken:
                metadata["spoken_form"] = spoken.strip()
            if (
                not metadata["is_propose"]
                and not metadata["is_plan"]
                and not metadata["defer_text"]
            ):
                raw_task = _s(parsed.get("task")).strip()
                if raw_task:
                    metadata["task_goal"] = raw_task
            raw_delta = parsed.get("chem_delta") or {}
            if isinstance(raw_delta, dict):
                chem_delta: dict[str, float] = {}
                for ch, v in raw_delta.items():
                    if ch in _CHEM_ALLOWED:
                        with contextlib.suppress(TypeError, ValueError):
                            chem_delta[ch] = max(-_CHEM_MAX_DELTA, min(_CHEM_MAX_DELTA, float(v)))
                metadata["chem_delta"] = chem_delta

            # Open-threads ledger fields (B6).
            metadata["open_thread"] = bool(parsed.get("open_thread"))
            metadata["advance_thread_id"] = _s(parsed.get("advance_thread_id")).strip()
            metadata["conclude_thread_id"] = _s(parsed.get("conclude_thread_id")).strip()
            metadata["conclusion"] = _s(parsed.get("conclusion")).strip()
            conf = _s(parsed.get("conclusion_confidence"), "confident").strip().lower()
            metadata["conclusion_confidence"] = "uncertain" if conf == "uncertain" else "confident"
            metadata["bears_on"] = [
                str(b).strip().lower() for b in (parsed.get("bears_on") or []) if str(b).strip()
            ][:4]
            metadata["bearing"] = _s(parsed.get("bearing")).strip().lower()

        return thought_clean, metadata

    async def _process_thought(
        self,
        thought_clean: str,
        metadata: dict,
        turn_id: str,
        *,
        exempt_seed: str | None = None,
        source_tag: str | None = None,
    ) -> None:
        """Dedup-check and, if novel, record, publish, and dispatch side-effects for one thought.

        exempt_seed: a prior thought to NOT dedup against (used by rumination,
        whose output is intentionally a deepened version of its seed).
        source_tag: e.g. "rumination" — flagged on the published event.
        """
        self._ensure_runtime_state()
        angle = metadata["angle"]
        spoken_form = metadata["spoken_form"]
        task_goal = metadata["task_goal"]
        is_propose = metadata["is_propose"]
        is_plan = metadata["is_plan"]
        defer_text = metadata["defer_text"]
        defer_urgency = metadata["defer_urgency"]
        defer_tags = metadata["defer_tags"]
        chem_delta = metadata["chem_delta"]

        # A thought that advances/concludes a TRACKED open thread is exempt from
        # the angle-repeat and cluster-saturation gates: those gates exist to
        # break UNtracked looping, but we WANT sustained depth on a thread we're
        # deliberately working. Requires an explicit thread id match (not a
        # heuristic) so the exemption can't silently reopen the loop.
        _adv_id = metadata.get("advance_thread_id") or ""
        _con_id = metadata.get("conclude_thread_id") or ""
        advancing_thread = bool(
            (_adv_id and ot.find(self._open_threads, _adv_id))
            or (_con_id and ot.find(self._open_threads, _con_id))
        )

        # ── Dedup gate ────────────────────────────────────────────────────
        # Word-overlap (cheap) is a pre-filter over the narrow window; semantic
        # cosine (the real gate) runs over the FULL window — it doesn't over-fire
        # on shared function words the way Jaccard did, so checking all recent
        # thoughts no longer over-suppresses focused topics.
        is_dup = False
        dup_reason = ""

        max_overlap = 0.0
        for prior in list(self._recent_thoughts)[-DMN_DEDUP_WINDOW:]:
            if exempt_seed is not None and prior == exempt_seed:
                continue
            o = _content_word_overlap(thought_clean, prior)
            if o > max_overlap:
                max_overlap = o
        # A thought that explicitly advances/concludes a tracked thread bypasses
        # ALL dedup gates: the thread id is a stronger signal than textual novelty,
        # and the action (advance/conclude) must happen even if the synthesis
        # resembles a prior note. Bounded by the advance cap so it can't loop.
        if not advancing_thread and max_overlap > settings.get("dmn_overlap_threshold"):
            is_dup, dup_reason = True, f"word-overlap {max_overlap:.2f}"

        # Semantic gate (best-effort — falls back to word-overlap if embedder down).
        new_emb = await self._safe_embed(thought_clean)
        max_cos = 0.0
        if not is_dup and not advancing_thread and new_emb is not None:
            sem_thr = float(settings.get("dmn_semantic_dup_threshold") or 0.88)
            # Thoughts restored from a prior session land with embedding=None
            # (_load_novelty). Backfill a few per pass so cross-session dedup
            # regains its semantic gate instead of degrading to word overlap
            # forever; the cap bounds embed calls on any single thought.
            backfill_budget = 5
            for i, (prior, prior_emb) in enumerate(
                zip(self._recent_thoughts, self._recent_embeddings, strict=False)
            ):
                if exempt_seed is not None and prior == exempt_seed:
                    continue
                if not prior_emb:
                    if backfill_budget <= 0:
                        continue
                    backfill_budget -= 1
                    prior_emb = await self._safe_embed(prior)
                    if not prior_emb:
                        continue
                    # The await may have let the deque shift (escape-hatch clear,
                    # concurrent append at maxlen) — only cache if still aligned.
                    if i < len(self._recent_thoughts) and self._recent_thoughts[i] == prior:
                        self._recent_embeddings[i] = prior_emb
                c = _cosine(new_emb, prior_emb)
                if c > max_cos:
                    max_cos = c
            if max_cos >= sem_thr:
                is_dup, dup_reason = True, f"semantic {max_cos:.2f}"

        # Angle hard-gate: a recently-used angle is blocked when the content is
        # also at least moderately similar (so a genuinely new take that happens
        # to reuse an angle label still passes).
        if not is_dup and not advancing_thread and angle and angle in self._recent_angles:
            sem_thr = float(settings.get("dmn_semantic_dup_threshold") or 0.88)
            if (
                max_overlap > float(settings.get("dmn_overlap_threshold")) * 0.6
                or max_cos >= sem_thr * 0.92
            ):
                is_dup, dup_reason = True, f"repeat angle '{angle}'"

        # Cluster-prefix suppression: if N recent angles share a common prefix
        # (e.g. 3x "craftsmanship-*"), the brain is stuck in a topic attractor.
        # Block new thoughts in the same cluster even when the suffix differs.
        _CLUSTER_SATURATION = int(os.environ.get("BRAIN_DMN_CLUSTER_SATURATION", "3"))
        if not is_dup and not advancing_thread and angle and "-" in angle:
            cluster = angle.rsplit("-", 1)[0]
            cluster_count = sum(
                1 for a in self._recent_angles if a == cluster or a.startswith(cluster + "-")
            )
            if cluster_count >= _CLUSTER_SATURATION:
                is_dup, dup_reason = True, f"cluster saturation '{cluster}' ({cluster_count}x)"

        # Frame-repetition gate: catches template collapse — same opening shape
        # ("i should INQUIRE …") with a swapped topic noun, which slips past both
        # the word-overlap gate (different nouns) and the cosine gate (just under
        # threshold). Skipped for rumination output (intentionally deepens a seed).
        frame_sig = "" if exempt_seed is not None else _frame_signature(thought_clean)
        if not is_dup and not advancing_thread and frame_sig:
            repeats = sum(1 for f in self._recent_frames if f == frame_sig)
            if repeats >= DMN_FRAME_REPEAT_MAX:
                is_dup, dup_reason = True, f"repeated frame '{frame_sig}'"

        if is_dup:
            self._suppressed_count += 1
            self._consec_suppressed += 1
            logger.info(
                "[Background reflection] Suppressed redundant thought "
                "(%s, total suppressed=%d, consec=%d): %r",
                dup_reason,
                self._suppressed_count,
                self._consec_suppressed,
                thought_clean[:60],
            )
            # Escape hatch: if the model has been stuck in a topic attractor for
            # too long, clear the text/embedding/frame dedup memory so it can
            # break free. Angles are kept — they're the soft territory signal, not
            # a hard block. Without this the model can suppress indefinitely.
            _escape = int(os.environ.get("BRAIN_DMN_SUPPRESS_ESCAPE", "5"))
            if self._consec_suppressed >= _escape:
                logger.info(
                    "[Background reflection] Dedup escape: clearing thought memory "
                    "after %d consecutive suppressions (topic attractor detected)",
                    self._consec_suppressed,
                )
                self._recent_thoughts.clear()
                self._recent_embeddings.clear()
                self._recent_frames.clear()
                self._consec_suppressed = 0
            return

        self._consec_suppressed = 0  # reset on success
        self._recent_thoughts.append(thought_clean)
        self._recent_embeddings.append(new_emb)
        if frame_sig:
            self._recent_frames.append(frame_sig)
        if angle:
            self._recent_angles.append(angle)
            # Stage 7 Gap 1: score the DMN's own thought-sequence prediction against reality —
            # did the angle we predicted last prefetch actually land? Self-verified correctness
            # for idle cognition, no user. Do it BEFORE record() updates the n-grams.
            self._reward_angle_prediction(self._seq_predictor._canonical(angle))
            self._seq_predictor.record(angle)
        # Stage 7 Gap 2: reward a good idle thought (the idle analog of Stage-1 draft pride) —
        # so the entity reinforces its own thinking while alone, not only thread conclusions.
        self._reward_idle_thought_quality(thought_clean, max_overlap, max_cos)
        # Persist novelty memory so a restart doesn't resurface this idea.
        self._persist_novelty()

        direction = _classify_thought(thought_clean)
        neuromod_snapshot = self._bus.neuromod.snapshot()

        # Open/advance/conclude an open thread (B1/B2). Returns the outcome so the
        # trace records what this thought actually DID (follow-through visibility).
        thread_outcome = await self._apply_thread_actions(thought_clean, metadata, turn_id)

        if self._obs:
            self._obs.record_thought(
                thought=thought_clean,
                direction=direction,
                angle=angle,
                count=self._thought_count,
                neuromod=neuromod_snapshot,
                outcome=thread_outcome,
            )

        da_level = float(neuromod_snapshot.get("DA", 0.5))
        em_valence = valence_of(self._last_emotion)
        salient = da_level > 0.62 or spoken_form is not None or abs(em_valence) > 0.45
        buf_entry: dict = {
            "thought": thought_clean,
            "angle": angle or "",
            "direction": direction,
            "speak_flagged": spoken_form is not None,
            "emotion": self._last_emotion,
            "neuromod": {k: round(v, 3) for k, v in neuromod_snapshot.items()},
            "salient": salient,
            "ts": time.time(),
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
            {
                "thought": thought_clean,
                "ts": time.time(),
                "count": self._thought_count,
                "direction": direction,
                "proactive": spoken_form is not None,
                "chem_delta": chem_delta,
                **({"rumination": True} if source_tag == "rumination" else {}),
            },
            source="dmn",
        )
        logger.debug(
            "[Background reflection] Thought #%d (%s): %s",
            self._thought_count,
            direction,
            thought_clean[:80],
        )

        if spoken_form:
            self._candidate_q.append(
                {
                    "thought": thought_clean,
                    "spoken": spoken_form,
                    "angle": angle,
                    "propose": is_propose,
                    "created_ts": time.time(),
                    "attempts": 0,
                }
            )
            logger.info(
                "[Background reflection] Speak candidate queued (queue=%d): %r",
                len(self._candidate_q),
                spoken_form[:80],
            )

        if task_goal:
            self._self_task_q.append(task_goal)
            logger.info("[Background reflection] Self-initiated task queued: %r", task_goal[:80])

        if defer_text:
            self._append_deferred_thought(defer_text, defer_urgency, defer_tags)
            if self._hippocampus is not None:
                asyncio.create_task(
                    self._hippocampus.encode_deferred_question(
                        session_id=getattr(self, "_session_id", "unknown"),
                        text=defer_text,
                        urgency=defer_urgency,
                        tags=defer_tags,
                        embedding_fn=self._router.embed,
                    )
                )

        if is_plan and thought_clean:
            asyncio.create_task(self._run_planning_pass(thought_clean, turn_id))

    async def _apply_thread_actions(self, thought_clean: str, metadata: dict, turn_id: str) -> dict:
        """Open / advance / conclude an open thread based on the monologue's
        ledger fields. Non-motor by construction — mutates the ledger and (on a
        confident conclusion) encodes to memory; never queues a task.

        Returns an outcome dict describing the follow-through, for the trace."""
        self._ensure_runtime_state()
        open_new = bool(metadata.get("open_thread"))
        advance_id = metadata.get("advance_thread_id") or ""
        conclude_id = metadata.get("conclude_thread_id") or ""
        conclusion = metadata.get("conclusion") or ""
        confidence = metadata.get("conclusion_confidence") or "confident"
        bears_on = metadata.get("bears_on") or []
        bearing = metadata.get("bearing") or ""
        angle = metadata.get("angle") or ""

        # CONCLUDE takes priority over advance/open.
        if conclude_id and ot.find(self._open_threads, conclude_id):
            return await self._resolve_thread(
                conclude_id, conclusion or thought_clean, confidence, bears_on
            )

        # ADVANCE an existing thread.
        if advance_id and ot.find(self._open_threads, advance_id):
            self._open_threads, t = ot.advance_thread(self._open_threads, advance_id, thought_clean)
            await self._save_threads()
            # Force closure when the advance cap is hit so a thread can't deepen
            # forever (a different kind of loop). The last progress note becomes
            # the conclusion if the model didn't supply one.
            if t is not None and ot.should_retire_for_advances(t):
                concl = conclusion or (t.progress[-1] if t.progress else t.summary)
                return await self._resolve_thread(t.id, concl, "confident", t.bears_on or bears_on)
            return {
                "action": "advanced_thread",
                "thread_id": getattr(t, "id", ""),
                "thread_title": getattr(t, "summary", "")[:80],
                "advances": getattr(t, "advances", 0),
            }

        # OPEN a new thread.
        if open_new and thought_clean:
            self._open_threads, t = ot.open_thread(
                self._open_threads,
                thought_clean,
                angle=angle,
                bears_on=bears_on,
                bearing=bearing,
            )
            await self._save_threads()
            return {"action": "opened_thread", "thread_id": t.id, "thread_title": t.summary[:80]}

        return {"action": "none"}

    async def _resolve_thread(
        self, thread_id: str, conclusion_text: str, confidence: str, bears_on: list
    ) -> dict:
        """Resolve a thread. Confident → commit the conclusion to episodic memory
        and retire. Uncertain → route through the existing deferred-question
        pipeline (ask the user first) and park the thread as pending_confirmation."""
        t = ot.find(self._open_threads, thread_id)
        if t is None:
            return {"action": "none"}
        sid = getattr(self, "_session_id", "unknown")
        tags = list(dict.fromkeys([*(t.bears_on or []), *(bears_on or [])]))

        if confidence == "uncertain":
            # Not sure it's right — raise it with the user before treating it as
            # known, via the same channel the brain already uses for deferred
            # questions. The thread waits in pending_confirmation (B5 promotes it).
            self._open_threads, pend = ot.mark_pending(self._open_threads, thread_id)
            if pend is not None:
                pend.pending_conclusion = conclusion_text
            await self._save_threads()
            question = (
                f"I've been thinking this through and tentatively concluded: "
                f"{conclusion_text} — does that match how you see it?"
            )
            self._append_deferred_thought(question, "high", tags)
            if self._hippocampus is not None:
                asyncio.create_task(
                    self._hippocampus.encode_deferred_question(
                        session_id=sid,
                        text=question,
                        urgency="high",
                        tags=["pending_conclusion", thread_id, *tags],
                        embedding_fn=self._router.embed,
                    )
                )
            return {
                "action": "deferred_conclusion",
                "thread_id": thread_id,
                "thread_title": t.summary[:80],
            }

        # Confident — commit to memory and retire the thread.
        if self._hippocampus is not None:
            asyncio.create_task(
                self._hippocampus.encode_conclusion(
                    session_id=sid,
                    text=conclusion_text,
                    source="dmn",
                    tags=tags,
                    embedding_fn=self._router.embed,
                )
            )
        self._open_threads = ot.remove_thread(self._open_threads, thread_id)
        self._recent_conclusions.append((time.time(), conclusion_text))
        await self._save_threads()
        # Stage 6: mastery — concluding a thread through sustained reasoning IS an accomplishment
        # (the "solving the logic puzzle is satisfying" case), no external task or user verdict.
        # Effort = how many advances it took to resolve; scaled by the expectation-gap curve and
        # the persona's mastery valuation. Best-effort (None bus in tests).
        with contextlib.suppress(Exception):
            from brain.neuron import accomplishment_factor, reward_weight

            _nm = getattr(getattr(self, "_bus", None), "neuromod", None)
            _advances = float(getattr(t, "advances", 0) or 0)
            if _nm and _advances > 0:
                _diff, _mod = accomplishment_factor(
                    _advances, float(settings.get("accomplishment_expected_medium"))
                )
                _w = reward_weight(str(settings.get("persona_name", "")), "mastery")
                _er = float(settings.get("emotional_reactivity_scale"))
                _nm.add("DA", float(settings.get("accomplishment_base")) * _diff * _mod * _w * _er)
        logger.info("[DMN] Concluded thread %s → memory: %r", thread_id, conclusion_text[:80])
        return {"action": "concluded", "thread_id": thread_id, "thread_title": t.summary[:80]}

    # ── Live-work routing + close-the-loop-on-use (B8/B9) ───────────────────

    @staticmethod
    def _thread_relevance(thread, activity_text: str) -> float:
        """How relevant an open thread is to what the user is working on now.
        Precise tag match (bears_on/bearing) dominates; summary word-overlap is
        the fallback so routing isn't purely fuzzy."""
        act = (activity_text or "").lower()
        if not act:
            return 0.0
        score = 0.0
        for tag in thread.bears_on:
            if tag and tag.lower() in act:
                score += 0.6
        if thread.bearing and thread.bearing.lower() in act:
            score += 0.2
        if thread.angle and thread.angle.lower() in act:
            score += 0.2
        score += _content_word_overlap(thread.summary, act)
        return score

    def route_threads_for_turn(self, activity_text: str, budget: int = 2) -> list:
        """Surface up to `budget` open threads relevant to the current activity,
        so a relevant unfinished idea shows up while the user is working on the
        thing it bears on. Ranked by relevance × learned routing weight (B9).
        budget<=0 means the load gate decided to hold everything this turn."""
        self._ensure_runtime_state()
        if budget <= 0:
            return []
        open_threads = [t for t in self._open_threads if t.status == ot.STATUS_OPEN]
        scored = []
        for t in open_threads:
            rel = self._thread_relevance(t, activity_text)
            if rel <= 0:
                continue
            weight = self._routing_weight(t.bearing)
            scored.append((rel * weight, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        routed = [t for score, t in scored[: max(0, budget)] if score >= 0.15]
        self._last_routed_ids = [t.id for t in routed]
        return routed

    async def note_threads_used(self, routed: list, response_text: str) -> list[dict]:
        """Close the loop. A routed thread that the response actually engaged
        (textual overlap, not mere recall) is marked resolved-by-use: committed to
        memory and retired. Threads that were surfaced but ignored weakly depress
        their routing weight (B9). Returns events for observability."""
        self._ensure_runtime_state()
        events: list[dict] = []
        resp = (response_text or "").lower()
        sid = getattr(self, "_session_id", "unknown")
        for t in routed:
            used = bool(resp) and (
                _content_word_overlap(t.summary, resp) >= 0.25
                or any(tag and tag.lower() in resp for tag in t.bears_on)
            )
            self._reinforce_routing(t.bearing, used)
            if not used:
                continue
            text = t.progress[-1] if t.progress else t.summary
            if self._hippocampus is not None:
                asyncio.create_task(
                    self._hippocampus.encode_conclusion(
                        session_id=sid,
                        text=text,
                        source="landed",
                        tags=list(t.bears_on or []),
                        embedding_fn=self._router.embed,
                    )
                )
            self._open_threads = ot.remove_thread(self._open_threads, t.id)
            self._recent_conclusions.append((time.time(), text))
            events.append({"action": "resolved_by_use", "thread_id": t.id})
            logger.info("[DMN] Thread landed in a response → retired: %s", t.id)
        if events:
            await self._save_threads()
        if routed:
            self._persist_routing_weights()
        return events

    # ── B9: learned routing weights + load-aware budget ─────────────────────

    _ROUTE_W_FLOOR = 0.25
    _ROUTE_W_CEIL = 2.5
    _ROUTE_W_LR = 0.15  # gentle learning rate

    def _routing_weight(self, bearing: str) -> float:
        if not bearing:
            return 1.0
        return float(self._routing_weights.get(bearing, 1.0))

    def _reinforce_routing(self, bearing: str, used: bool) -> None:
        """Hebbian-style update: a thread that landed potentiates its bearing's
        routing weight; one surfaced-and-ignored gently depresses it. Clamped so
        routing can never collapse to never/always-surface."""
        if not bearing:
            return
        w = self._routing_weights.get(bearing, 1.0)
        if used:
            w += self._ROUTE_W_LR * (self._ROUTE_W_CEIL - w)
        else:
            w -= self._ROUTE_W_LR * (w - self._ROUTE_W_FLOOR)
        self._routing_weights[bearing] = max(self._ROUTE_W_FLOOR, min(self._ROUTE_W_CEIL, w))

    def _load_routing_weights(self) -> None:
        try:
            sb = self._dmn_sb()
            if sb is not None:
                client, uid, persona = sb
                res = (
                    client.table("dmn_state")
                    .select("routing_weights")
                    .eq("org_id", uid)
                    .eq("persona", persona)
                    .maybe_single()
                    .execute()
                )
                weights = ((res.data or {}).get("routing_weights") if res else None) or {}
            else:
                if not ROUTING_WEIGHTS_PATH.exists():
                    return
                data = json.loads(ROUTING_WEIGHTS_PATH.read_text(encoding="utf-8"))
                weights = data.get("weights") or {}
            # Decay toward rest (1.0) on load so stale associations relax — the
            # analog of Hebbian decay_toward_rest.
            rate = 0.1
            self._routing_weights = {
                k: float(v) + (1.0 - float(v)) * rate for k, v in weights.items()
            }
            # Session-start baseline so the eval layer can measure whether
            # reinforcement actually outpaces the decay (loop-closure check).
            self._routing_weights_loaded = dict(self._routing_weights)
        except Exception as e:
            logger.warning("[DMN] Could not load routing weights: %s", e)

    def _persist_routing_weights(self) -> None:
        sb = self._dmn_sb()
        if sb is not None:
            client, uid, persona = sb
            try:
                client.table("dmn_state").upsert(
                    {
                        "org_id": uid,
                        "persona": persona,
                        "end_user_id": "",
                        "routing_weights": self._routing_weights,
                        "updated_at": "now()",
                    },
                    on_conflict="org_id,persona,end_user_id",
                ).execute()
            except Exception as e:
                logger.warning("[DMN] Could not persist routing weights to Supabase: %s", e)
            return
        try:
            ROUTING_WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = ROUTING_WEIGHTS_PATH.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"weights": self._routing_weights, "ts": time.time()}, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, ROUTING_WEIGHTS_PATH)
        except Exception as e:
            logger.warning("[DMN] Could not persist routing weights: %s", e)

    def observe_user_turn(self, features: dict, user_input: str) -> None:
        """Track rolling user-state signals for the load gate. We watch *shift
        from baseline*, not raw level — a single curt message is noise."""
        self._ensure_runtime_state()
        self._user_msg_lens.append(len((user_input or "").split()))
        topic = str((features or {}).get("topic_summary") or (features or {}).get("intent") or "")
        self._user_topics.append(topic)

    def _user_load_signals(self) -> tuple[float, float]:
        """Return (verbosity_trend, topic_jump_rate).
        verbosity_trend < 0 means the user has turned terser than their baseline
        (a shift toward stretched). topic_jump_rate is the fraction of recent
        turns that changed topic."""
        lens = list(self._user_msg_lens)
        verbosity_trend = 0.0
        if len(lens) >= 4:
            split = len(lens) // 2
            base = sum(lens[:split]) / max(1, split)
            recent = sum(lens[split:]) / max(1, len(lens) - split)
            if base > 0:
                verbosity_trend = (recent - base) / base  # negative = getting terser
        topics = [t for t in self._user_topics if t]
        topic_jump_rate = 0.0
        if len(topics) >= 3:
            changes = sum(1 for a, b in zip(topics, topics[1:], strict=False) if a != b)
            topic_jump_rate = changes / (len(topics) - 1)
        return verbosity_trend, topic_jump_rate

    @staticmethod
    def _routing_budget_from(
        focus_ach: float, verbosity_trend: float, topic_jump_rate: float, base: int = 2
    ) -> int:
        """Pure: map (AI focus, user-state shift) → how many threads to surface.
        Biased toward holding — a missed surface is quieter than interrupting a
        stretched user."""
        budget = base
        if focus_ach >= 0.6:  # the AI itself is in deep focus
            budget -= 1
        if verbosity_trend <= -0.25:  # user turned markedly terser than baseline
            budget -= 1
        if topic_jump_rate >= 0.6:  # user is jumping between topics (scattered/stretched)
            budget -= 1
        return max(0, min(base, budget))

    def compute_routing_budget(self) -> int:
        """Gather live signals and decide the per-turn surfacing budget."""
        self._ensure_runtime_state()
        try:
            ach = float(self._bus.neuromod.snapshot().get("ACh", 0.3))
        except Exception:
            ach = 0.3
        verbosity_trend, topic_jump_rate = self._user_load_signals()
        return self._routing_budget_from(ach, verbosity_trend, topic_jump_rate)

    # ── Conversational ledger intents (B5) ──────────────────────────────────

    async def process_user_message_for_ledger(self, user_input: str) -> dict | None:
        """Apply conversational intents that touch the ledger, best-effort.

        Priority: if a conclusion is awaiting confirmation, treat the reply as the
        answer (affirm → commit to memory; reject → drop; correct → re-open with
        the correction). Otherwise detect a manual project assignment. Returns a
        small dict describing what happened (for the caller to acknowledge), or
        None if nothing matched.
        """
        self._ensure_runtime_state()
        from brain.clusters import ledger_intents as li

        if not user_input or not user_input.strip():
            return None

        pending = [t for t in self._open_threads if t.status == ot.STATUS_PENDING]
        if pending:
            target = self._match_pending(user_input, pending)
            if target is not None:
                verdict = li.classify_confirmation(user_input)
                return await self._resolve_pending_conclusion(target, verdict, user_input)

        proj = li.detect_manual_project(user_input)
        if proj:
            await self.add_manual_project(proj["title"], proj["task"])
            return {"action": "project_added", "title": proj["title"]}
        return None

    @staticmethod
    def _match_pending(user_input: str, pending: list):
        """Pick which pending-conclusion thread the user is answering. One
        pending → that one. Several → the best word-overlap match (else None)."""
        if len(pending) == 1:
            return pending[0]
        best, best_score = None, 0.0
        for t in pending:
            score = _content_word_overlap(user_input, f"{t.summary} {t.pending_conclusion}")
            if score > best_score:
                best, best_score = t, score
        return best if best_score >= 0.15 else None

    async def _resolve_pending_conclusion(self, thread, verdict: str, user_input: str) -> dict:
        sid = getattr(self, "_session_id", "unknown")
        tags = list(thread.bears_on or [])
        # A user verdict on a surfaced conclusion is the truest correctness signal we get —
        # the entity is VERIFIED right or wrong, not self-judging a draft. Reward/penalise
        # accordingly, scaled by how much this persona values being right (reward_weight) and
        # global emotional reactivity. This is delayed, outcome-based reinforcement (RPE-like).
        from brain.neuron import loss_aversion, reward_weight

        _persona = str(settings.get("persona_name", ""))
        _w = reward_weight(_persona, "correctness")
        _la = loss_aversion(_persona)  # λ: weights the verified-wrong sting, never the affirm reward
        _er = float(settings.get("emotional_reactivity_scale"))
        _nm = getattr(getattr(self, "_bus", None), "neuromod", None)  # best-effort; None in tests
        if verdict == "affirm":
            if _nm:
                _nm.add("DA", float(settings.get("correctness_reward_base")) * _w * _er)
            text = thread.pending_conclusion or thread.summary
            if self._hippocampus is not None:
                asyncio.create_task(
                    self._hippocampus.encode_conclusion(
                        session_id=sid,
                        text=text,
                        source="confirmed",
                        tags=tags,
                        embedding_fn=self._router.embed,
                    )
                )
            self._open_threads = ot.remove_thread(self._open_threads, thread.id)
            self._recent_conclusions.append((time.time(), text))
            await self._save_threads()
            logger.info("[DMN] User confirmed conclusion → memory: %r", text[:80])
            return {"action": "conclusion_confirmed", "thread_id": thread.id}
        if verdict == "reject":
            # Verified wrong — DA dip plus 5HT drain (the sting that lingers); resting
            # chemistry decides whether that reads as brooding (Poet) or bristling (Analyst).
            if _nm:
                _nm.add("DA", -float(settings.get("correctness_penalty_base")) * _w * _er * _la)
                _nm.add("5HT", -float(settings.get("correctness_5ht_drain")) * _w * _er * _la)
            self._open_threads = ot.remove_thread(self._open_threads, thread.id)
            await self._save_threads()
            logger.info("[DMN] User rejected conclusion → thread dropped: %s", thread.id)
            return {"action": "conclusion_rejected", "thread_id": thread.id}
        # correction → partially wrong: a softer penalty (half), then re-open the thread.
        if _nm:
            _nm.add("DA", -0.5 * float(settings.get("correctness_penalty_base")) * _w * _er * _la)
        thread.status = ot.STATUS_OPEN
        thread.pending_conclusion = ""
        self._open_threads, _ = ot.advance_thread(
            self._open_threads, thread.id, f"user correction: {user_input.strip()[:200]}"
        )
        await self._save_threads()
        logger.info("[DMN] User corrected conclusion → thread re-opened: %s", thread.id)
        return {"action": "conclusion_corrected", "thread_id": thread.id}

    async def add_manual_project(self, title: str, task: str) -> bool:
        """Append a user-assigned project to the `## Projects assigned by Russ`
        section of open_questions.md, then refresh the DMN's projects context so
        it's picked up immediately. Non-motor — it only records the assignment."""
        schema = self._schema_store()
        if schema is None:
            return False
        try:
            text = schema.read(ot.LEDGER_FILE)
            if not text:
                return False
            block = (
                f"\n### {title.strip()}\n"
                f"- **Task**: {task.strip()}\n"
                f"- **Status**: Not started (assigned in conversation)\n"
            )
            header = "## Projects assigned by Russ"
            if header in text:
                # Insert right after the section header so it's prominent.
                idx = text.index(header) + len(header)
                new_text = text[:idx] + "\n" + block + text[idx:]
            else:
                new_text = text.rstrip() + f"\n\n{header}\n{block}"
            await schema.awrite(ot.LEDGER_FILE, new_text)
            self.set_projects_context(new_text)
            logger.info("[DMN] Manual project added: %r", title[:80])
            return True
        except Exception as e:
            logger.warning("[DMN] Could not add manual project: %s", e)
            return False

    async def _run_simulation(self, turn_id: str) -> None:
        """Run the user-simulation cell and publish the predicted next input."""
        self._simulation_cell.reset_turn(turn_id + "_sim")
        self._simulation_cell.skills = self._inherited_skill_names()
        raw = await self._simulation_cell.call(
            [{"role": "user", "content": self._last_context or "No context yet."}]
        )
        try:
            self.predicted_next = json.loads(raw)
            await self._bus.publish_dict("stream.prediction", self.predicted_next, source="dmn")
            logger.debug(
                "[Background reflection] Anticipating: %s (confidence=%.2f)",
                self.predicted_next.get("predicted_input", "")[:60],
                self.predicted_next.get("confidence", 0),
            )
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

    async def _run_silence_recall(self, turn_id: str) -> None:
        """Phase 6: on a fresh ARMED→QUIET edge for a tracked topic, recall the
        memories associated with its prior high-concentration window. Reuses the
        existing hippocampus.recall path; surfaces the result as a monologue seed
        and lets recall_affect recolor chemistry."""
        # Only during genuine lulls — never mid-exchange.
        try:
            if get_idle_seconds() < 30:
                return
        except Exception:
            pass
        for topic in self._bus.tracked_topics():
            if not self._bus.consume_quiet_onset(topic):
                continue  # no fresh quiet onset (debounced)
            # Build a recall cue from the associated-context ring captured while hot.
            seen: set[str] = set()
            cue_tags: list[str] = []
            for entry in self._bus.concentration_context(topic):
                for tag in entry.get("tags") or []:
                    if tag not in seen:
                        seen.add(tag)
                        cue_tags.append(str(tag))
            query = " ".join(cue_tags) if cue_tags else topic
            try:
                result = await self._hippocampus.recall(
                    query=query,
                    entities=cue_tags[:5],
                    turn_id=turn_id + "_silence",
                    embedding_fn=self._router.embed,
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("[Background reflection] Silence recall failed for %r: %s", topic, e)
                continue
            # recall_affect nudges chemistry (warmth/recognition or residual threat).
            for ch, delta in (result.get("recall_affect") or {}).items():
                with contextlib.suppress(Exception):
                    self._bus.neuromod.add(ch, float(delta))
            episodes = (result.get("episodes") or "")[:400]
            if episodes:
                self._memory_seed = (
                    f"A quiet moment brought back something about [{', '.join(cue_tags[:3])}]: "
                    f"{episodes}"
                )
            from brain.observability.decisions import decisions as _decisions

            _decisions.log(
                "silence_triggered_recall",
                turn_id=turn_id,
                topic=topic,
                cue=query[:80],
                had_episodes=bool(episodes),
            )
            break  # one silence recall per tick

    async def _run_prefetcher(self, turn_id: str) -> None:
        self._prefetcher_cell.reset_turn(turn_id + "_pre")
        self._prefetcher_cell.skills = self._inherited_skill_names()
        prompt = self._last_context or "No context yet."
        raw = await self._prefetcher_cell.call([{"role": "user", "content": prompt}])
        try:
            parsed = json.loads(raw)
            queries = parsed.get("queries", []) or []
        except Exception as e:
            logger.debug("[Background reflection] Prefetcher parse failed: %s", e)
            return

        # Inject the sequence-predicted territory as a guaranteed extra query,
        # provided confidence is high enough and the topic isn't already covered.
        predicted_angle, seq_confidence = self._seq_predictor.predict()
        if predicted_angle:
            # Stash the live prediction so _process_thought can score it against the angle that
            # actually lands next — self-verified correctness for the DMN's own thought-sequence
            # model (Stage 7 Gap 1). Informativeness gates out constant-angle loops.
            self._last_predicted_angle = self._seq_predictor._canonical(predicted_angle)
            self._last_angle_confidence = float(seq_confidence)
            self._last_angle_informativeness = self._seq_predictor.informativeness()
        if predicted_angle and seq_confidence >= self._seq_predictor.min_confidence:
            covered = {str(q.get("topic", "")).lower() for q in queries}
            if not any(predicted_angle in t or t in predicted_angle for t in covered):
                queries.append(
                    {
                        "topic": predicted_angle,
                        "reason": f"sequence prediction (confidence {seq_confidence:.2f})",
                    }
                )
                logger.debug(
                    "[SeqPredictor] Injected prefetch query: %r (conf=%.2f)",
                    predicted_angle,
                    seq_confidence,
                )

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
                logger.debug(
                    "[Background reflection] Prefetcher recall failed for %r: %s", topic, e
                )
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
            logger.info(
                "[Background reflection] Prefetched context for %d topics: %s",
                len(self.prefetched),
                ", ".join(p["topic"][:30] for p in self.prefetched),
            )

    async def _run_anticipator(self, turn_id: str) -> None:
        self._anticipator_cell.reset_turn(turn_id + "_ant")
        self._anticipator_cell.skills = self._inherited_skill_names()
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
                    "valence": max(-1.0, min(1.0, float(s.get("valence", 0.0) or 0.0))),
                }
                for s in scenarios[:3]
                if s.get("user_answer") and s.get("response_sketch")
            ]
            if self.anticipations:
                # Anticipation moves chemistry, not just thought: imagining a good outcome
                # pulls DA forward (wanting / looking-forward), imagining a bad one raises CORT
                # (dread). This is what gives the entity a forward pull instead of pure reaction.
                # This is also the DECISION-TIME locus of risk posture: dread scales by the
                # persona's loss aversion (λ) and the SPREAD of imagined outcomes by its
                # uncertainty aversion (κ), so a risk-averse identity generates more CORT here →
                # GABA → raised switch thresholds → it DECIDES more conservatively, not merely
                # stings harder after a loss lands. Hoped-for reward still scales by what this
                # persona values (correctness as a stand-in for "things going well"); gains are
                # never λ-scaled — that one-sidedness is loss aversion.
                from brain.neuron import (
                    loss_aversion,
                    reward_weight,
                    uncertainty_aversion,
                )

                _persona = str(settings.get("persona_name", ""))
                _scale = float(settings.get("anticipation_reward_scale"))
                _w = reward_weight(_persona, "correctness")
                _la = loss_aversion(_persona)
                _ka = uncertainty_aversion(_persona)
                _best = max((s["valence"] for s in self.anticipations), default=0.0)
                _worst = min((s["valence"] for s in self.anticipations), default=0.0)
                if _best > 0:
                    self._bus.neuromod.add("DA", _scale * _best * _w)
                if _worst < 0:
                    self._bus.neuromod.add("CORT", _scale * (-_worst) * _la)
                # Uncertainty aversion: even with no outright loss, a wide spread between the
                # best and worst imagined outcome is itself aversive to a risk-averse persona.
                _spread = _best - _worst
                if _spread > 0 and _ka > 0:
                    self._bus.neuromod.add("CORT", _scale * _spread * _ka)
                await self._bus.publish_dict(
                    "stream.anticipation",
                    {"scenarios": self.anticipations, "ts": time.time()},
                    source="dmn",
                )
                logger.info(
                    "[Background reflection] Anticipated %d follow-up scenarios",
                    len(self.anticipations),
                )
        except Exception as e:
            logger.debug("[Background reflection] Anticipator parse failed: %s", e)
