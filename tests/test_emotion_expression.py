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
