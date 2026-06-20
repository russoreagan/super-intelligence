"""
Tests for the user_register feature — modelling the user's REGISTER/FORMALITY
(formal / casual / technical / neutral) so replies meet the user where they are
in *style*, not just length. Companion to the msg_length signal.

Covers:
  - classify_register: the cheap per-turn heuristic tag (parietal)
  - update_register_profile / dominant_register: the rolling per-speaker
    profile pure functions (relationship)
  - ParietalCluster register tracking + cross-session schema persistence
  - temporal threads user_register into features on every path
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import MagicMock

from brain.clusters.parietal import ParietalCluster, classify_register
from brain.relationship import (
    REGISTER_CATEGORIES,
    dominant_register,
    update_register_profile,
)

# ── classify_register: per-turn heuristic tag ─────────────────────────────────


def test_classify_register_casual():
    assert classify_register("yeah nah idk lol it's kinda whatever tbh") == "casual"


def test_classify_register_formal():
    text = (
        "I would be most grateful if you could elaborate further. Furthermore, "
        "a comprehensive and systematic analysis would be appropriate."
    )
    assert classify_register(text) == "formal"


def test_classify_register_technical_wins_over_prose():
    # Code/jargon dominates the register regardless of surrounding casual prose.
    assert classify_register("hey can you check why parse_config() throws") == "technical"
    assert classify_register("the API returned a 500 with a traceback") == "technical"
    assert classify_register("```\nfoo\n```") == "technical"


def test_classify_register_neutral_and_empty():
    assert classify_register("Can you help me find a good restaurant nearby") == "neutral"
    assert classify_register("") == "neutral"
    assert classify_register("   ") == "neutral"


# ── rolling per-speaker register profile (pure functions) ─────────────────────


def test_update_register_profile_converges():
    """Repeated observations of one register make it dominate."""
    profile: dict[str, float] = {}
    for _ in range(8):
        profile = update_register_profile(profile, "technical", alpha=0.3)
    assert dominant_register(profile) == "technical"
    # All categories are present and the observed one carries the most weight.
    assert set(profile) == set(REGISTER_CATEGORIES)
    assert profile["technical"] == max(profile.values())


def test_update_register_profile_is_pure():
    original: dict[str, float] = {}
    update_register_profile(original, "formal")
    assert original == {}  # input not mutated


def test_update_register_profile_drifts():
    """A sustained switch in register eventually flips the dominant tag."""
    profile: dict[str, float] = {}
    for _ in range(8):
        profile = update_register_profile(profile, "casual", alpha=0.3)
    assert dominant_register(profile) == "casual"
    for _ in range(8):
        profile = update_register_profile(profile, "formal", alpha=0.3)
    assert dominant_register(profile) == "formal"


def test_dominant_register_flat_is_no_signal():
    # Empty profile, and a profile too flat to call, both read as no signal.
    assert dominant_register({}) == ""
    flat = dict.fromkeys(REGISTER_CATEGORIES, 0.25)
    assert dominant_register(flat) == ""


def test_unknown_observation_folds_into_neutral():
    profile = update_register_profile({}, "gibberish", alpha=1.0)
    assert profile["neutral"] == 1.0


# ── ParietalCluster: tracking + persistence ───────────────────────────────────


def _fresh_parietal() -> ParietalCluster:
    return ParietalCluster(MagicMock())


def test_parietal_tracks_dominant_register():
    p = _fresh_parietal()
    assert p.dominant_register() == ""  # nothing learned yet
    for _ in range(6):
        p.update_register("formal", alpha=0.3)
    assert p.dominant_register() == "formal"


class _FakeSchemaStore:
    """Minimal in-memory schema store for the persistence round-trip."""

    def __init__(self) -> None:
        self._files: dict[str, str] = {}

    def speaker_filename(self, name: str) -> str:
        return f"user_{name.lower()}.md" if name else "user.md"

    def ensure_speaker_schema(self, name: str) -> str:
        fn = self.speaker_filename(name)
        self._files.setdefault(fn, "")
        return fn

    def read(self, filename: str) -> str:
        return self._files.get(filename, "")

    async def upsert_section(self, filename: str, section: str, line: str) -> None:
        # Good enough for the loader's regex: a header followed by the line.
        self._files[filename] = f"## {section}\n{line}\n"


def test_register_profile_persists_across_sessions():
    store = _FakeSchemaStore()

    # Session 1: a technical user, persisted at sleep.
    p1 = _fresh_parietal()
    for _ in range(6):
        p1.update_register("technical", alpha=0.3)
    asyncio.run(p1.save_style_to_schema(store, speaker_name="russ"))

    # The persisted payload carries the register profile.
    content = store.read(store.speaker_filename("russ"))
    assert "register_profile" in content
    assert re.search(r"## Style register", content)

    # Session 2: a fresh brain reloads and resumes with the register remembered.
    p2 = _fresh_parietal()
    p2.load_style_from_schema(store, speaker_name="russ")
    assert p2.dominant_register() == "technical"
