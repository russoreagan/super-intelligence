"""
Tests for the deliberate emotional expression feature.

Coverage:
  emotion_presets
    - get_tag returns correct ElevenLabs tag for known emotions
    - get_tag returns None for empty-string tag (natural voice)
    - get_tag returns None for unknown emotion name

  PNS._parse_mood_markup
    - strips display text cleanly
    - injects ElevenLabs tag + reset into tts text
    - multiple segments in one response
    - unknown emotion silently strips markup (no tag injected)
    - no markup → passthrough (display == tts)
    - base_tag=None → no reset injected after segment

  PNS Flash 2.5 helpers
    - _strip_all_tags: removes mood markup, bare tags, reaction tags
    - _strip_all_tags: no-op on plain text
    - _extract_mood_map: returns spans in clean-text coordinates
    - _extract_mood_map: no markup → empty list
    - _voice_settings_from_emotion: known emotions map to correct buckets
    - _voice_settings_from_emotion: None emotion uses base_params
    - _voice_settings_from_emotion: unknown emotion uses base_params
    - _make_flash_chunks: plain text produces same chunks as _split_sentences
    - _make_flash_chunks: mood markup drives per-chunk VoiceSettings
    - _make_flash_chunks: chunk count unchanged by mood boundaries
    - _FLASH_EMOTION_CLUSTERS: covers all 4 buckets

  MotorCortexCluster._set_mood (via _dispatch)
    - disabled by settings → [blocked]
    - unknown emotion → [error]
    - "auto" clears override on bus
    - valid emotion publishes meta.deliberate_emotion
    - valid emotion publishes meta.mood_expression (source="tool")
    - valid emotion emits deliberate=True UI event
    - _obs.record_deliberate_emotion called when obs is set

  Tracing — ObservabilityLayer.record_deliberate_emotion
    - appends entry to span._deliberate_emotions
    - no-ops gracefully when no active span

  End-to-end — mood_expression inbox drain
    - deliberate_emotions populated on TurnTrace from bus messages
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bus():
    from brain.bus import Bus

    return Bus()


def _make_motor(tmp_path, settings_override: dict | None = None):
    """Return (motor, bus) with a fake router that always returns tool=none."""
    from brain.bus import Bus
    from brain.clusters.motor_cortex import MotorCortexCluster

    class _FakeRouter:
        _call_log: list = []

        async def call(self, *a, **kw):
            return json.dumps({"tool": "none", "args": {}, "reason": "test"})

        async def embed(self, text):
            return [0.0] * 768

    bus = Bus()
    motor = MotorCortexCluster(bus, _FakeRouter(), allowed_paths=[str(tmp_path)])

    if settings_override is not None:
        from brain.settings import settings as _s

        for k, v in settings_override.items():
            _s._data[k] = v

    return motor, bus


# ---------------------------------------------------------------------------
# emotion_presets.get_tag
# ---------------------------------------------------------------------------


class TestGetTag:
    def test_known_emotion_returns_tag(self):
        from brain.emotion_presets import get_tag

        assert get_tag("angry") == "[angrily]"
        assert get_tag("happy") == "[happy]"
        assert get_tag("laughing") == "[laughs softly]"

    def test_unknown_emotion_returns_none(self):
        from brain.emotion_presets import get_tag

        assert get_tag("nonexistent_emotion_xyz") is None

    def test_natural_voice_emotion_returns_none(self):
        """'calm' maps to empty tag string → natural voice → get_tag returns None."""
        from brain.emotion_presets import get_tag

        assert get_tag("calm") is None

    def test_case_insensitive(self):
        from brain.emotion_presets import get_tag

        assert get_tag("ANGRY") == "[angrily]"
        assert get_tag("Happy") == "[happy]"


# ---------------------------------------------------------------------------
# PNS._parse_mood_markup
# ---------------------------------------------------------------------------


class TestParseMoodMarkup:
    def _parse(self, text, base_tag=None):
        from brain.pns import PNS

        return PNS._parse_mood_markup(text, base_tag)

    def test_strips_display_text(self):
        text = "Sure. [mood:angry] This is wrong! [/mood] Moving on."
        display, _ = self._parse(text)
        assert "[mood:" not in display
        assert "[/mood]" not in display
        assert "This is wrong!" in display
        assert "Sure." in display
        assert "Moving on." in display

    def test_injects_el_tag_in_tts(self):
        text = "Hello. [mood:angry] This is unacceptable! [/mood] Anyway."
        _, tts = self._parse(text, base_tag="[thoughtfully]")
        assert "[angrily]" in tts
        # reset tag is inserted after the segment
        assert "[thoughtfully]" in tts

    def test_no_markup_passthrough(self):
        text = "Plain sentence with no mood markers."
        display, tts = self._parse(text)
        assert display == text
        assert tts == text

    def test_multiple_segments(self):
        text = "[mood:happy] Great news! [/mood] But [mood:sad] this is sad. [/mood] Done."
        display, tts = self._parse(text, base_tag="[curious]")
        assert "[happy]" in tts
        assert "[sadly]" in tts
        # both reset tags injected
        assert tts.count("[curious]") == 2
        # display has no markup
        assert "[mood:" not in display

    def test_unknown_emotion_strips_cleanly(self):
        """Unknown emotion name → no ElevenLabs tag injected, text still clean."""
        text = "Start. [mood:frobnicate] Middle part. [/mood] End."
        display, tts = self._parse(text, base_tag="[curious]")
        assert "frobnicate" not in display
        assert "[frobnicate]" not in tts
        assert "Middle part." in tts
        assert "Middle part." in display

    def test_no_reset_when_base_tag_is_none(self):
        text = "Before. [mood:laughing] Ha ha! [/mood] After."
        _, tts = self._parse(text, base_tag=None)
        assert "[laughs softly]" in tts
        # no reset tag because base_tag is None
        assert tts.count("[") == 1

    def test_display_tts_differ_when_markup_present(self):
        text = "[mood:excited] Wow! [/mood] Normal text."
        display, tts = self._parse(text)
        assert display != tts


# ---------------------------------------------------------------------------
# MotorCortexCluster._set_mood
# ---------------------------------------------------------------------------


class TestSetMood:
    @pytest.mark.asyncio
    async def test_disabled_by_settings(self, tmp_path):
        from brain.settings import settings as _s

        original = _s._data.get("emotional_expression_enabled", 1)
        try:
            _s._data["emotional_expression_enabled"] = 0
            motor, _ = _make_motor(tmp_path)
            result = await motor._set_mood("happy")
            assert result.startswith("[blocked]")
        finally:
            _s._data["emotional_expression_enabled"] = original

    @pytest.mark.asyncio
    async def test_unknown_emotion_returns_error(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        result = await motor._set_mood("nonexistent_xyz")
        assert result.startswith("[error]")
        assert "nonexistent_xyz" in result

    @pytest.mark.asyncio
    async def test_auto_clears_deliberate_emotion(self, tmp_path):
        motor, bus = _make_motor(tmp_path)
        inbox = bus.subscribe("meta.deliberate_emotion")
        with patch("brain.ui.emitter.emitter.emit_event", new_callable=AsyncMock):
            await motor._set_mood("auto")
        msg = inbox.get_nowait()
        assert msg.payload.get("emotion") is None

    @pytest.mark.asyncio
    async def test_valid_emotion_publishes_deliberate_emotion(self, tmp_path):
        motor, bus = _make_motor(tmp_path)
        inbox = bus.subscribe("meta.deliberate_emotion")
        with patch("brain.ui.emitter.emitter.emit_event", new_callable=AsyncMock):
            await motor._set_mood("angry")
        msg = inbox.get_nowait()
        assert msg.payload["emotion"] == "angry"

    @pytest.mark.asyncio
    async def test_valid_emotion_publishes_mood_expression(self, tmp_path):
        motor, bus = _make_motor(tmp_path)
        inbox = bus.subscribe("meta.mood_expression")
        with patch("brain.ui.emitter.emitter.emit_event", new_callable=AsyncMock):
            await motor._set_mood("laughing")
        msg = inbox.get_nowait()
        assert msg.payload["emotion"] == "laughing"
        assert msg.payload["source"] == "tool"

    @pytest.mark.asyncio
    async def test_valid_emotion_emits_deliberate_ui_event(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        events = []
        with patch(
            "brain.ui.emitter.emitter.emit_event",
            new_callable=AsyncMock,
            side_effect=lambda e: events.append(e),
        ):
            await motor._set_mood("sad")
        emotion_events = [e for e in events if e.get("type") == "emotion"]
        assert len(emotion_events) == 1
        assert emotion_events[0]["emotion"] == "sad"
        assert emotion_events[0]["deliberate"] is True

    @pytest.mark.asyncio
    async def test_obs_record_called_when_set(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        mock_obs = MagicMock()
        motor._obs = mock_obs
        motor._current_turn_id = "turn-abc"
        with patch("brain.ui.emitter.emitter.emit_event", new_callable=AsyncMock):
            await motor._set_mood("excited")
        mock_obs.record_deliberate_emotion.assert_called_once_with("turn-abc", "excited", "tool")

    @pytest.mark.asyncio
    async def test_returns_success_string(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        with patch("brain.ui.emitter.emitter.emit_event", new_callable=AsyncMock):
            result = await motor._set_mood("curious")
        assert "curious" in result
        assert "[error]" not in result
        assert "[blocked]" not in result


# ---------------------------------------------------------------------------
# ObservabilityLayer.record_deliberate_emotion
# ---------------------------------------------------------------------------


class TestObservabilityRecordDEliberateEmotion:
    def _make_obs(self):
        from brain.observability.timeline import ObservabilityLayer

        # No Langfuse keys set — tracing disabled but layer still usable
        return ObservabilityLayer(session_id="test-session")

    def test_no_span_noop(self):
        """Should not raise when there's no active span for the turn."""
        obs = self._make_obs()
        obs.record_deliberate_emotion("nonexistent-turn", "angry", "tool")

    def test_stashes_on_span(self):
        obs = self._make_obs()
        # Manually inject a fake span
        fake_span = MagicMock()
        fake_span._deliberate_emotions = []
        obs._active_spans["t1"] = fake_span
        obs.record_deliberate_emotion("t1", "happy", "tool", preview="Great news!")
        assert len(fake_span._deliberate_emotions) == 1
        entry = fake_span._deliberate_emotions[0]
        assert entry["emotion"] == "happy"
        assert entry["source"] == "tool"
        assert entry["preview"] == "Great news!"

    def test_preview_truncated_at_80_chars(self):
        obs = self._make_obs()
        fake_span = MagicMock()
        fake_span._deliberate_emotions = []
        obs._active_spans["t1"] = fake_span
        long_preview = "x" * 200
        obs.record_deliberate_emotion("t1", "angry", "inline", preview=long_preview)
        assert len(fake_span._deliberate_emotions[0]["preview"]) <= 80


# ---------------------------------------------------------------------------
# Mood expression inbox drain → TurnTrace
# ---------------------------------------------------------------------------


class TestMoodExpressionDrain:
    @pytest.mark.asyncio
    async def test_deliberate_emotions_populated_from_bus(self):
        """Messages on meta.mood_expression should land in trace.deliberate_emotions."""
        from brain.bus import Bus
        from brain.observability.timeline import TurnTrace

        bus = Bus()
        inbox = bus.subscribe("meta.mood_expression")

        # Simulate tool publishing
        await bus.publish_dict(
            "meta.mood_expression",
            {"emotion": "angry", "source": "tool"},
            source="motor_cortex",
        )
        # Simulate inline markup publishing
        await bus.publish_dict(
            "meta.mood_expression",
            {"emotion": "laughing", "source": "inline", "preview": "Ha ha!"},
            source="pns",
        )

        trace = TurnTrace(turn_id="t1", session_id="s1", user_input="test")

        # Drain (mirrors what session_turn does)
        await asyncio.sleep(0)
        while True:
            try:
                mx = inbox.get_nowait()
                if not mx.expired:
                    trace.deliberate_emotions.append(
                        {
                            "emotion": mx.payload.get("emotion", ""),
                            "source": mx.payload.get("source", ""),
                            **(
                                {"preview": mx.payload["preview"]}
                                if mx.payload.get("preview")
                                else {}
                            ),
                        }
                    )
            except Exception:
                break

        assert len(trace.deliberate_emotions) == 2
        sources = {e["source"] for e in trace.deliberate_emotions}
        assert sources == {"tool", "inline"}
        emotions = {e["emotion"] for e in trace.deliberate_emotions}
        assert emotions == {"angry", "laughing"}
        laughing = next(e for e in trace.deliberate_emotions if e["emotion"] == "laughing")
        assert laughing["preview"] == "Ha ha!"

    @pytest.mark.asyncio
    async def test_no_expressions_gives_empty_list(self):
        from brain.observability.timeline import TurnTrace

        trace = TurnTrace(turn_id="t2", session_id="s1", user_input="test")
        assert trace.deliberate_emotions == []


# ---------------------------------------------------------------------------
# Flash 2.5 helpers
# ---------------------------------------------------------------------------


# Minimal VoiceSettings stand-in so tests don't require the ElevenLabs SDK.
class _VS:
    def __init__(self, stability, similarity_boost, style, use_speaker_boost, speed=None):
        self.stability = stability
        self.similarity_boost = similarity_boost
        self.style = style
        self.use_speaker_boost = use_speaker_boost
        self.speed = speed


class TestStripAllTags:
    def _strip(self, text):
        from brain.pns import PNS

        return PNS._strip_all_tags(text)

    def test_removes_mood_markup_keeps_inner_text(self):
        result = self._strip("Hello. [mood:sad] I miss you. [/mood] Goodbye.")
        assert "[mood:" not in result
        assert "[/mood]" not in result
        assert "I miss you." in result
        assert "Hello." in result
        assert "Goodbye." in result

    def test_removes_bare_bracket_tags(self):
        result = self._strip("Sure. [gently] That works. [curious] Right?")
        assert "[gently]" not in result
        assert "[curious]" not in result
        assert "Sure." in result
        assert "That works." in result

    def test_removes_reaction_tags(self):
        result = self._strip("Wait. [sighs] Okay then.")
        assert "[sighs]" not in result
        assert "Okay then." in result

    def test_plain_text_unchanged(self):
        text = "This is just plain text with no tags at all."
        assert self._strip(text) == text

    def test_nested_strip(self):
        text = "[mood:excited] Wow! [laughs] Great! [/mood] Done."
        result = self._strip(text)
        assert "[mood:" not in result
        assert "[laughs]" not in result
        assert "Wow!" in result
        assert "Great!" in result
        assert "Done." in result


class TestExtractMoodMap:
    def _extract(self, text):
        from brain.pns import PNS

        return PNS._extract_mood_map(text)

    def test_no_markup_returns_empty(self):
        spans = self._extract("Just plain text here.")
        assert spans == []

    def test_single_span_found(self):
        text = "Before. [mood:sad] I miss you. [/mood] After."
        spans = self._extract(text)
        assert len(spans) == 1
        start, end, mood = spans[0]
        assert mood == "sad"
        assert start >= 0
        assert end > start

    def test_inner_text_in_clean_coords(self):
        text = "[mood:happy] Great news! [/mood] Moving on."
        spans = self._extract(text)
        assert len(spans) == 1
        start, end, mood = spans[0]
        assert mood == "happy"
        # In clean text "Great news! Moving on.", the span should start at 0
        assert start == 0

    def test_multiple_spans_ordered(self):
        text = "[mood:sad] First. [/mood] Middle. [mood:excited] Second! [/mood] End."
        spans = self._extract(text)
        assert len(spans) == 2
        assert spans[0][2] == "sad"
        assert spans[1][2] == "excited"
        assert spans[0][0] < spans[1][0]  # ordered by position in clean text


class TestVoiceSettingsFromEmotion:
    BASE = {"stability": 0.45, "style": 0.40, "speed": 1.00}

    def _vs(self, emotion):
        from brain.pns import PNS

        return PNS._voice_settings_from_emotion(emotion, self.BASE, VoiceSettings=_VS)

    def test_none_emotion_uses_base_params(self):
        vs = self._vs(None)
        assert vs.stability == 0.45
        assert vs.style == 0.40

    def test_unknown_emotion_uses_base_params(self):
        vs = self._vs("totally_unknown_xyz")
        assert vs.stability == 0.45
        assert vs.style == 0.40

    def test_bright_bucket(self):
        vs = self._vs("excited")
        assert vs.stability == 0.35
        assert vs.style == 0.55

    def test_calm_bucket(self):
        vs = self._vs("thoughtful")  # neutral low-arousal stays calm
        assert vs.stability == 0.55
        assert vs.style == 0.25

    def test_low_bucket(self):
        # Low-valence emotions get the subdued "low" bucket, distinct from calm.
        vs = self._vs("sad")
        assert vs.stability == 0.60
        assert vs.style == 0.15
        for emo in ("disappointed", "somber", "melancholy", "wistful", "flat"):
            assert self._vs(emo).style == 0.15, f"{emo} should map to low bucket"

    def test_warm_bucket(self):
        vs = self._vs("warmly")
        assert vs.stability == 0.50
        assert vs.style == 0.35

    def test_tense_bucket(self):
        vs = self._vs("angry")
        assert vs.stability == 0.65
        assert vs.style == 0.25

    def test_hierarchy_fallback(self):
        # "joy" is a parent of many leaf emotions; should hit bright bucket
        vs = self._vs("joy")
        assert vs.stability == 0.35

    def test_similarity_boost_always_080(self):
        for emotion in [None, "sad", "excited", "angry"]:
            vs = self._vs(emotion)
            assert vs.similarity_boost == 0.80


class TestMakeFlashChunks:
    BASE = {"stability": 0.45, "style": 0.40, "speed": 1.00}

    def _chunks(self, text):
        from brain.pns import PNS

        return PNS._make_flash_chunks(PNS, text, self.BASE, VoiceSettings=_VS)

    def test_plain_text_same_chunk_count_as_split_sentences(self):
        from brain.pns import PNS

        text = "Short text."
        chunks = self._chunks(text)
        sentences = PNS._split_sentences(text)
        assert len(chunks) == len(sentences)

    def test_returns_list_of_tuples(self):
        chunks = self._chunks("Hello world.")
        assert isinstance(chunks, list)
        for item in chunks:
            assert len(item) == 2
            assert isinstance(item[0], str)

    def test_no_markup_all_chunks_get_base_params(self):
        text = "Plain sentence. Another one. And more."
        chunks = self._chunks(text)
        for _, vs in chunks:
            assert vs.stability == self.BASE["stability"]
            assert vs.style == self.BASE["style"]

    def test_mood_markup_stripped_from_chunk_text(self):
        text = "Normal. [mood:sad] Sad part. [/mood] Back to normal."
        chunks = self._chunks(text)
        combined = " ".join(t for t, _ in chunks)
        assert "[mood:" not in combined
        assert "[/mood]" not in combined
        assert "Sad part." in combined
        assert "Normal." in combined

    def test_mood_changes_voice_settings_for_covered_chunk(self):
        # Intro must exceed the 120-char first-chunk threshold so it flushes as its own chunk.
        # Then the sad section starts the second chunk, which should get calm params.
        intro = (
            "Everything has been going really well lately, I have to say, "
            "and I feel genuinely optimistic about the direction things are heading. "
        )  # ~131 chars — forces a flush before the sad section
        sad = "[mood:sad] I deeply miss the old days and feel heavy about it all. [/mood] "
        outro = "But we move forward regardless."
        text = intro + sad + outro
        chunks = self._chunks(text)
        assert len(chunks) >= 2, (
            f"Expected ≥2 chunks, got {len(chunks)}: {[t[:40] for t, _ in chunks]}"
        )
        # At least one chunk should have low (sad) params (stability=0.60, style=0.15)
        settings = [(vs.stability, vs.style) for _, vs in chunks]
        assert any(s == (0.60, 0.15) for s in settings), f"No low chunk found: {settings}"

    def test_chunk_count_not_inflated_by_mood_boundaries(self):
        from brain.pns import PNS

        text = (
            "Normal intro sentence. "
            "[mood:sad] This is the sad part in the middle. [/mood] "
            "And back to normal here."
        )
        chunks = self._chunks(text)
        clean = PNS._strip_all_tags(text)
        sentences = PNS._split_sentences(clean)
        assert len(chunks) == len(sentences)


class TestFlashEmotionClusters:
    def test_all_buckets_represented(self):
        from brain.pns import PNS

        buckets = set(PNS._FLASH_EMOTION_CLUSTERS.values())
        assert buckets == {"bright", "warm", "calm", "tense", "low"}

    def test_no_stray_values(self):
        from brain.pns import PNS

        valid = {"bright", "warm", "calm", "tense", "low"}
        for emotion, bucket in PNS._FLASH_EMOTION_CLUSTERS.items():
            assert bucket in valid, f"{emotion!r} → invalid bucket {bucket!r}"

    def test_core_emotion_coverage(self):
        from brain.pns import PNS

        clusters = PNS._FLASH_EMOTION_CLUSTERS
        assert clusters.get("excited") == "bright"
        assert clusters.get("sad") == "low"  # low-valence → subdued voice
        assert clusters.get("thoughtful") == "calm"  # neutral low-arousal stays calm
        assert clusters.get("angry") == "tense"
        assert clusters.get("warmly") == "warm"
