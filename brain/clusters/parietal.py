"""
Parietal Lobe — persistent session state. 0 LLMs, all state-tracking switches.
Ring buffer of recent turns, entity tracker, topic tracker.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field

from brain.bounded_ledger import cap_evict
from brain.bus import Bus
from brain.clusters.skill_selector import ActiveSkillContext

logger = logging.getLogger(__name__)

CLUSTER = "parietal"
RING_SIZE = 6
# How many ignited workspace foci to keep in the rolling record (advisory only).
FOCUS_HISTORY_SIZE = 8
# Bound on the entity tracker: keep only the most recently seen. Its consumers are
# recency-shaped (avoidance staleness, session summary), so an entity unmentioned
# for hundreds of turns carries no signal — without a cap the map grows for the
# life of the session and bloats everything that snapshots it.
MAX_TRACKED_ENTITIES = 128


def _record_active(node: str, level: float) -> None:
    """Mark a parietal state holder as having participated this turn.

    Recorded on READ rather than on update(), deliberately. These nodes have no
    SwitchNeuron/IntegratorCell so they never reach fired_path, and the edge they
    anchor is `parietal.X → frontal.executive` — whose meaning is "this state fed the
    executive". update() runs unconditionally every turn, so recording there would
    emit a constant, and a constant carries no signal for the sleep pass to learn
    from. Recording on read makes the weight track whether the holder actually
    contributed, which is what the edge is supposed to mean."""
    if level <= 0:
        return
    try:
        from brain.observability.firing_path import record_node_active

        record_node_active(node, level)
    except Exception:  # observability must never break a state read
        pass


@dataclass
class UserStyleVector:
    """Per-modality style vector tracking user's communication register.

    Values are 0–1 floats; updated with EMA each turn.
    formality: 0=very casual, 1=very formal
    verbosity: 0=terse, 1=expansive
    sentiment: -1 to +1 (tracking user's emotional tone in text)
    """

    formality: float = 0.5
    verbosity: float = 0.5
    sentiment: float = 0.0
    turns_tracked: int = 0


@dataclass
class ModalityStyleState:
    """Separate style vectors per input channel.
    Text and voice have different norms — they must not cross-contaminate.
    """

    voice: UserStyleVector = field(default_factory=UserStyleVector)
    text: UserStyleVector = field(default_factory=UserStyleVector)

    def get(self, modality: str) -> UserStyleVector:
        return self.voice if modality == "voice" else self.text

    def update(
        self, modality: str, formality: float, verbosity: float, sentiment: float, alpha: float
    ) -> None:
        """EMA update for the given modality's style vector."""
        vec = self.get(modality)
        if vec.turns_tracked == 0:
            # Cold start: initialise to observed values directly
            vec.formality = formality
            vec.verbosity = verbosity
            vec.sentiment = sentiment
        else:
            vec.formality = alpha * formality + (1 - alpha) * vec.formality
            vec.verbosity = alpha * verbosity + (1 - alpha) * vec.verbosity
            vec.sentiment = alpha * sentiment + (1 - alpha) * vec.sentiment
        vec.turns_tracked += 1


def _measure_verbosity(text: str) -> float:
    """Normalise word count to 0–1 over a realistic CHAT span.
    Calibrated for conversational text (not essays): ≤5 words → terse (0.10),
    ≥45 words → expansive (0.90), linear between. This spread actually crosses
    the 0.35/0.65 label thresholds for normal short vs. long messages."""
    words = len(text.split())
    if words <= 5:
        return 0.10
    if words >= 45:
        return 0.90
    return 0.10 + (words - 5) / (45 - 5) * 0.80


_CASUAL_RE = re.compile(
    r"\b(gonna|wanna|gotta|kinda|sorta|prolly|ya|yep|yeah|nope|nah|ok|okay|"
    r"tbh|ngl|idk|idc|imo|btw|nvm|lol|haha|lmao|hehe|sure|cool|"
    r"n't|'m|'re|'ve|'ll|'d)\b",
    re.IGNORECASE,
)
_FORMAL_RE = re.compile(
    r"(therefore|furthermore|however|consequently|nevertheless|regarding|"
    r"accordingly|pursuant|moreover|thus|hence|wherein|whereby|"
    r"appreciate|grateful|comprehensive|elaborate|appropriate|"
    r"systematic|rationale|considerable)",
    re.IGNORECASE,
)


def _measure_formality(text: str) -> float:
    """Heuristic formality score 0–1.  Casual markers drive it down;
    formal connectors and register words drive it up. Sensitivity is high
    enough that a few markers in a short message move the needle across the
    0.30/0.60 label thresholds."""
    words = max(len(text.split()), 1)
    casual = len(_CASUAL_RE.findall(text))
    formal = len(_FORMAL_RE.findall(text))
    # Base at 0.42 (chat skews slightly casual); markers swing it ±.
    # Per-marker weight scaled so 2–3 markers in a ~20-word message clear a label.
    score = 0.42 - (casual / words) * 2.0 + (formal / words) * 2.4
    return max(0.0, min(1.0, score))


# Code / jargon markers. When any of these appear the message reads as
# technical regardless of how formal or casual the surrounding prose is, so
# this dominates the discrete register tag below. Kept conservative — patterns
# that are strong signals of code (fences, call syntax, snake/camelCase
# identifiers, operators, dev keywords) and rare in ordinary prose.
# NOTE: deliberately NOT re.IGNORECASE — the camelCase branch relies on a real
# case transition, and a global ignore-case flag would degrade [a-z]+[A-Z] to
# [a-z]+[a-z], matching every ordinary word. The keyword list carries its own
# (?i:…) so it still matches "API", "JSON", etc.
_TECHNICAL_RE = re.compile(
    r"```|`[^`]+`"  # code fences / inline code
    r"|\b\w+\([^)]*\)"  # function-call syntax foo(...)
    r"|\b[a-z][a-z0-9]*_[a-z0-9_]+\b"  # snake_case identifiers
    r"|\b[a-z]+[A-Z][a-zA-Z0-9]+\b"  # camelCase identifiers
    r"|(?:==|!=|=>|->|::|&&|\|\|)"  # code operators
    r"|(?i:\b(?:def|class|async|await|import|function|const|return|stdout|stderr|"
    r"json|http|https|api|sql|regex|traceback|exception|stacktrace|"
    r"git|npm|pip|docker|kubectl|localhost|nginx|stdin|cli)\b)",
)


def classify_register(text: str) -> str:
    """Cheap, single-token classification of a user message's *register*.

    The length analogue of msg_length, for style: a coarse tag computed from
    heuristics with zero LLM cost, threaded through `features` and into the
    drafter's tone logic so a reply meets the user's formality, not just their
    length. Returns one of: casual | neutral | formal | technical.

    Reuses the same formality heuristic as the rolling style vector so the
    per-turn tag and the persisted register profile stay consistent. 'technical'
    wins when code/jargon is present — it dominates perceived register regardless
    of formal/casual prose markers."""
    if not text or not text.strip():
        return "neutral"
    if _TECHNICAL_RE.search(text):
        return "technical"
    formality = _measure_formality(text)
    if formality > 0.60:
        return "formal"
    if formality < 0.30:
        return "casual"
    return "neutral"


class ParietalCluster:
    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self._ring: deque[dict] = deque(maxlen=RING_SIZE)
        self._entities: dict[str, int] = {}  # entity -> turn count last seen
        self._turn_count = 0
        self.active_skill_context: ActiveSkillContext | None = None
        # Per-modality user style tracking (voice and text tracked independently)
        self._style_state = ModalityStyleState()
        # Rolling per-speaker register profile (casual/neutral/formal/technical).
        # Modality-independent — register is a property of how the person writes,
        # not which channel — so a single distribution rather than per-modality.
        self._register_profile: dict[str, float] = {}
        # Global-workspace spotlight record (advisory arm of the thalamus → parietal
        # fan-out). Parietal is a real subscriber to the spotlight: it *records* what
        # coalition holds the workspace each turn but gates nothing on it. None when
        # the workspace is not ignited (which includes the flag-off path); a compact
        # dict when it is. A short rolling record keeps the recent ignited foci.
        self._last_workspace_focus: dict | None = None
        self._focus_history: deque[dict] = deque(maxlen=FOCUS_HISTORY_SIZE)

    def seed(self, episodes: list[dict]) -> None:
        """Pre-populate the ring from recent episodic history (called once at boot).
        Episodes arrive newest-first; ring wants oldest-first so we reverse."""
        for ep in reversed(episodes):
            entry = {
                "turn": self._turn_count,
                "user": ep.get("user_input", ""),
                "response": ep.get("entity_response", ""),
                "intent": (ep.get("topic_tags") or [None])[0],
                "topic": None,
                "emotion": ep.get("emotion_state"),
            }
            self._ring.append(entry)

    def update(self, features: dict, user_input: str, entity_response: str = "") -> None:
        self._turn_count += 1
        entry = {
            "turn": self._turn_count,
            "user": user_input,
            "response": entity_response,
            "intent": features.get("intent"),
            "topic": features.get("topic_summary"),
            "emotion": features.get("emotion"),
        }
        self._ring.append(entry)

        # Track entities (bounded: evict the least recently seen past the cap)
        for entity in features.get("entities", []):
            self._entities[entity] = self._turn_count
        for ent, _ in cap_evict(
            self._entities.items(), MAX_TRACKED_ENTITIES, staleness=lambda kv: kv[1]
        ):
            del self._entities[ent]

        # Record the current global-workspace spotlight (advisory; gates nothing).
        self._record_workspace_focus(features)

    def _record_workspace_focus(self, features: dict) -> None:
        """Fold this turn's spotlight verdict into session state.

        Reads the locked-contract spotlight the thalamus wrote onto ``features``
        before parietal ran. This is the advisory arm of the thalamus → parietal
        fan-out: parietal RECORDS what the workspace is focused on, it does not
        gate anything on it.

        No-op guarantee: when the workspace is not ignited — the spotlight key is
        absent, malformed, or ``ignited`` is false (which includes the flag-off
        path that yields a neutral verdict) — this records ``None`` and touches
        nothing else, so every pre-existing output is byte-identical to before.
        """
        spotlight = features.get("spotlight")
        if not isinstance(spotlight, dict) or not spotlight.get("ignited"):
            self._last_workspace_focus = None
            return
        record = {
            "turn": self._turn_count,
            "focus": spotlight.get("focus"),
            "coalition": spotlight.get("coalition"),
            "sustained_turns": int(spotlight.get("sustained_turns", 0) or 0),
            "salience": float(spotlight.get("salience", 0.0) or 0.0),
        }
        self._last_workspace_focus = record
        self._focus_history.append(record)

    def last_workspace_focus(self) -> dict | None:
        """The coalition holding the workspace this turn, or None when not ignited."""
        return self._last_workspace_focus

    def recent_workspace_foci(self, n: int = 4) -> list[dict]:
        """The last few ignited workspace foci (advisory record, oldest-first)."""
        return list(self._focus_history)[-n:]

    def workspace_focus_note(self) -> str:
        """One-line 'current focus' note for a drafter prompt — only when ignited.

        Returns '' whenever the workspace is not ignited, so a caller that always
        appends this note contributes nothing on a neutral turn (the no-op path).
        """
        rec = self._last_workspace_focus
        if not rec:
            return ""
        focus = rec.get("focus")
        coalition = rec.get("coalition")
        turns = int(rec.get("sustained_turns", 0) or 0)
        held = f" (held {turns} turns)" if turns > 1 else ""
        if coalition and focus:
            _record_active("parietal.topic_vector_holder", 1.0)
            return f"Workspace focus — the {coalition} coalition holds attention on {focus}{held}."
        if focus:
            _record_active("parietal.topic_vector_holder", 1.0)
            return f"Workspace focus — attention is on {focus}{held}."
        return ""

    def recent_turns(self, n: int = 4) -> list[dict]:
        return list(self._ring)[-n:]

    @staticmethod
    def _strip_role_tags(text: str) -> str:
        """Remove lines that start with 'User:' or 'Brain:' to prevent role spoofing."""
        return "\n".join(
            line for line in text.splitlines() if not re.match(r"^\s*(User|Brain)\s*:", line)
        )

    def recent_turns_text(self, n: int = 4) -> str:
        turns = self.recent_turns(n)
        lines = []
        for t in turns:
            lines.append(f"User: {self._strip_role_tags(t['user'])}")
            if t.get("response"):
                lines.append(f"Brain: {self._strip_role_tags(t['response'])}")
        out = "\n".join(lines)
        # Scaled by how full the buffer is: an early turn with one line of history
        # contributed less to the executive than a full window did.
        _record_active("parietal.recent_turns_ringbuffer", len(turns) / max(1, n) if out else 0.0)
        return out

    def session_summary(self) -> dict:
        return {
            "turn_count": self._turn_count,
            "recent_entities": list(self._entities.keys())[-10:],
            "recent_topics": [t.get("topic") for t in self._ring if t.get("topic")],
        }

    @property
    def turn_count(self) -> int:
        return self._turn_count

    def entity_last_seen(self) -> dict[str, int]:
        """entity → turn index it was last mentioned. Read-only copy; used by the
        avoidance gate to find stale (unre-engaged) entities."""
        out = dict(self._entities)
        _record_active("parietal.entity_tracker", 1.0 if out else 0.0)
        return out

    def set_active_skill_context(self, ctx: ActiveSkillContext | None) -> None:
        """Selector writes back the updated context after each turn."""
        self.active_skill_context = ctx

    def clear_active_skill_context(self, reason: str = "") -> None:
        if self.active_skill_context is not None:
            logger.debug("parietal: clearing active skill context (%s)", reason)
            self.active_skill_context = None

    # ── User style tracking ───────────────────────────────────────────────────

    def update_user_style(
        self, user_input: str, modality: str, sentiment: float, alpha: float
    ) -> None:
        """Update the per-modality style vector from the current user message.

        Verbosity and formality are measured from the text; sentiment is passed
        in from temporal_features (already extracted there). EMA alpha differs
        per modality: voice adapts slightly faster (more consistent signal)
        than text (more variable and naturally terser).
        """
        verbosity = _measure_verbosity(user_input)
        formality = _measure_formality(user_input)
        self._style_state.update(modality, formality, verbosity, sentiment, alpha)

    def get_user_style(self, modality: str) -> UserStyleVector:
        """Return the current style vector for the given modality."""
        return self._style_state.get(modality)

    # ── Register profile (rolling, per-speaker) ───────────────────────────────

    def update_register(self, register_tag: str, alpha: float = 0.3) -> None:
        """Fold this turn's discrete register tag into the rolling profile."""
        from brain.relationship import update_register_profile

        self._register_profile = update_register_profile(
            self._register_profile, register_tag, alpha
        )

    def dominant_register(self) -> str:
        """The user's typical register so far, or '' if not yet established."""
        from brain.relationship import dominant_register

        return dominant_register(self._register_profile)

    # ── Cross-session persistence (F3) ────────────────────────────────────────

    async def save_style_to_schema(self, schema_store, speaker_name: str = "") -> None:
        """Persist the per-modality style vectors into the speaker schema so a
        known user resumes warm instead of cold-starting every session.
        Stored as a compact JSON line under a `## Style register` section.
        Skips a vector that hasn't tracked any turns (nothing learned yet)."""
        import json

        try:
            if (
                self._style_state.voice.turns_tracked == 0
                and self._style_state.text.turns_tracked == 0
                and not self._register_profile
            ):
                return
            payload = {
                "voice": vars(self._style_state.voice),
                "text": vars(self._style_state.text),
                "register_profile": self._register_profile,
            }
            line = f"- vectors: {json.dumps(payload)}"
            schema_file = (
                schema_store.ensure_speaker_schema(speaker_name) if speaker_name else "user.md"
            )
            await schema_store.upsert_section(schema_file, "Style register", line)
        except Exception:
            logger.debug("parietal: style persistence skipped", exc_info=True)

    def load_style_from_schema(self, schema_store, speaker_name: str = "") -> None:
        """Reload per-modality style vectors saved by a previous session."""
        import json
        import re

        try:
            schema_file = schema_store.speaker_filename(speaker_name) if speaker_name else "user.md"
            content = schema_store.read(schema_file)
            if not content:
                return
            m = re.search(r"## Style register.*?- vectors:\s*(\{.*\})", content, re.DOTALL)
            if not m:
                return
            payload = json.loads(m.group(1))
            for modality in ("voice", "text"):
                d = payload.get(modality) or {}
                vec = self._style_state.get(modality)
                vec.formality = float(d.get("formality", vec.formality))
                vec.verbosity = float(d.get("verbosity", vec.verbosity))
                vec.sentiment = float(d.get("sentiment", vec.sentiment))
                vec.turns_tracked = int(d.get("turns_tracked", vec.turns_tracked))
            rp = payload.get("register_profile")
            if isinstance(rp, dict):
                self._register_profile = {k: float(v) for k, v in rp.items()}
        except Exception:
            logger.debug("parietal: style reload skipped", exc_info=True)

    def user_style_note(self, modality: str) -> str:
        """Return a one-line style note for injection into drafter prompts.

        Returns empty string if not enough turns have been tracked, or if
        the feature is disabled. Includes a text-brevity disclaimer when
        the user's text verbosity is low (brevity ≠ coldness in text channel).
        """
        from brain.settings import settings as _s

        if not _s.get("enable_style_synchrony"):
            return ""

        vec = self.get_user_style(modality)
        min_turns = int(_s.get("style_min_turns_for_injection"))
        if vec.turns_tracked < min_turns:
            return ""

        formality_label, verbosity_label = self._style_labels(modality)
        note = (
            f"Register ({modality}) — user is currently writing {formality_label} "
            f"and {verbosity_label}. Nudge your phrasing toward this while staying "
            f"true to your natural voice (don't fully mirror — adapt partway)."
        )
        # Text-brevity disclaimer: terse text ≠ coldness
        if modality == "text" and vec.verbosity < 0.40:
            note += " Note: text register tends toward brevity — this does not indicate coldness."
        return note

    def _style_labels(self, modality: str) -> tuple[str, str]:
        """Return (formality_label, verbosity_label) describing the USER's actual
        register, from the raw measured style (unclamped). Thresholds are applied
        to the real measurements; the entity's bounded adaptation is expressed in
        the note's prose, not by clamping the description."""
        vec = self.get_user_style(modality)
        formality_label = (
            "formally"
            if vec.formality > 0.60
            else "casually"
            if vec.formality < 0.30
            else "neutrally"
        )
        verbosity_label = (
            "expansively"
            if vec.verbosity > 0.60
            else "tersely"
            if vec.verbosity < 0.32
            else "concisely"
        )
        return formality_label, verbosity_label

    def user_style_register(self, modality: str) -> str:
        """Compact 'formality/verbosity' label string for trace instrumentation,
        e.g. 'casually/tersely'. Empty until enough turns are tracked."""
        from brain.settings import settings as _s

        vec = self.get_user_style(modality)
        if vec.turns_tracked < int(_s.get("style_min_turns_for_injection")):
            return ""
        f, v = self._style_labels(modality)
        return f"{f}/{v}"
