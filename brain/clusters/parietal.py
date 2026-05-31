"""
Parietal Lobe — persistent session state. 0 LLMs, all state-tracking switches.
Ring buffer of recent turns, entity tracker, topic tracker.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field

from brain.bus import Bus
from brain.clusters.skill_selector import ActiveSkillContext

logger = logging.getLogger(__name__)

CLUSTER = "parietal"
RING_SIZE = 6


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

    def update(self, modality: str, formality: float, verbosity: float,
               sentiment: float, alpha: float) -> None:
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


class ParietalCluster:
    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self._ring: deque[dict] = deque(maxlen=RING_SIZE)
        self._entities: dict[str, int] = {}  # entity -> turn count last seen
        self._turn_count = 0
        self.active_skill_context: ActiveSkillContext | None = None
        # Per-modality user style tracking (voice and text tracked independently)
        self._style_state = ModalityStyleState()

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

        # Track entities
        for entity in features.get("entities", []):
            self._entities[entity] = self._turn_count

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
        return "\n".join(lines)

    def session_summary(self) -> dict:
        return {
            "turn_count": self._turn_count,
            "recent_entities": list(self._entities.keys())[-10:],
            "recent_topics": [t.get("topic") for t in self._ring if t.get("topic")],
        }

    @property
    def turn_count(self) -> int:
        return self._turn_count

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
            ):
                return
            payload = {
                "voice": vars(self._style_state.voice),
                "text": vars(self._style_state.text),
            }
            line = f"- vectors: {json.dumps(payload)}"
            schema_file = (
                schema_store.ensure_speaker_schema(speaker_name)
                if speaker_name
                else "user.md"
            )
            await schema_store.upsert_section(schema_file, "Style register", line)
        except Exception:
            logger.debug("parietal: style persistence skipped", exc_info=True)

    def load_style_from_schema(self, schema_store, speaker_name: str = "") -> None:
        """Reload per-modality style vectors saved by a previous session."""
        import json
        import re

        try:
            schema_file = (
                schema_store.speaker_filename(speaker_name)
                if speaker_name
                else "user.md"
            )
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
            "formally" if vec.formality > 0.60
            else "casually" if vec.formality < 0.30
            else "neutrally"
        )
        verbosity_label = (
            "expansively" if vec.verbosity > 0.60
            else "tersely" if vec.verbosity < 0.32
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
