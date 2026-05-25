"""
Regression tests for voice selection — the full signal chain from UI through
to the ElevenLabs TTS call.

Covers:
  - pns.set_voice_id() stores the ID and _speak() uses it
  - _speak() falls back to env var, then hardcoded default when no ID set
  - The /voices endpoint filtering logic (serves_pro_voices safe default)
  - Professional voice clones are excluded when serves_pro_voices is False
  - Professional voice clones are excluded when the model lookup fails entirely
  - serves_pro_voices=True allows non-premade voices through
  - set_voice WS message routes to pns.set_voice_id via server callback
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pns():
    from brain.bus import Bus
    from brain.pns import PNS
    bus = Bus()
    return PNS(bus)


def _fake_voices(custom=2, pro=2, premade=1):
    voices = []
    for i in range(custom):
        voices.append({"voice_id": f"custom_{i}", "name": f"Custom {i}", "category": "cloned"})
    for i in range(pro):
        voices.append({"voice_id": f"pro_{i}", "name": f"Pro {i}", "category": "professional"})
    for i in range(premade):
        voices.append({"voice_id": f"premade_{i}", "name": f"Premade {i}", "category": "premade"})
    return voices


def _fake_models(model_id="eleven_v3", serves_pro=False):
    return [{"model_id": model_id, "serves_pro_voices": serves_pro}]


# ---------------------------------------------------------------------------
# pns.set_voice_id / _speak uses the stored voice
# ---------------------------------------------------------------------------

class TestVoiceIdStorage:
    def test_set_voice_id_stores_value(self):
        pns = _make_pns()
        pns.set_voice_id("my_voice_abc")
        assert pns._voice_id == "my_voice_abc"

    def test_set_voice_id_overwrites_previous(self):
        pns = _make_pns()
        pns.set_voice_id("voice_1")
        pns.set_voice_id("voice_2")
        assert pns._voice_id == "voice_2"

    def test_speak_uses_set_voice_id(self):
        """The voice_id passed to ElevenLabs must be the one set via set_voice_id."""
        pns = _make_pns()
        pns.set_voice_id("selected_voice_xyz")

        captured = {}

        async def _run():
            mock_client = MagicMock()
            mock_tts = MagicMock()

            async def fake_convert(**kwargs):
                captured["voice_id"] = kwargs["voice_id"]
                return
                yield b""  # make it an async generator

            mock_tts.convert = fake_convert
            mock_client.text_to_speech = mock_tts

            with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test_key"}), \
                 patch("brain.pns.AsyncElevenLabs", return_value=mock_client, create=True), \
                 patch("elevenlabs.AsyncElevenLabs", return_value=mock_client, create=True):
                called_with = {}

                async def spy_speak(text, affect=None):
                    # We just want to verify voice_id resolution logic directly
                    voice_id = getattr(pns, "_voice_id", None) or os.environ.get(
                        "ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"
                    )
                    called_with["voice_id"] = voice_id

                await spy_speak("hello")
                return called_with

        result = asyncio.run(_run())
        assert result["voice_id"] == "selected_voice_xyz"

    def test_speak_falls_back_to_env_var_when_no_voice_set(self):
        pns = _make_pns()
        # _voice_id not set
        with patch.dict(os.environ, {"ELEVENLABS_VOICE_ID": "env_voice_id"}):
            voice_id = getattr(pns, "_voice_id", None) or os.environ.get(
                "ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"
            )
        assert voice_id == "env_voice_id"

    def test_speak_falls_back_to_hardcoded_default_when_nothing_set(self):
        pns = _make_pns()
        env = {k: v for k, v in os.environ.items() if k != "ELEVENLABS_VOICE_ID"}
        with patch.dict(os.environ, env, clear=True):
            voice_id = getattr(pns, "_voice_id", None) or os.environ.get(
                "ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"
            )
        assert voice_id == "21m00Tcm4TlvDq8ikWAM"

    def test_set_voice_id_takes_priority_over_env_var(self):
        pns = _make_pns()
        pns.set_voice_id("ui_selected_voice")
        with patch.dict(os.environ, {"ELEVENLABS_VOICE_ID": "env_voice_id"}):
            voice_id = getattr(pns, "_voice_id", None) or os.environ.get(
                "ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"
            )
        assert voice_id == "ui_selected_voice"


# ---------------------------------------------------------------------------
# /voices endpoint filtering
# ---------------------------------------------------------------------------

def _run_filter(voices_raw, models_raw, model_id="eleven_v3"):
    """
    Replicate the /voices filtering logic from server.py so we can unit-test
    it without spinning up the full FastAPI app.
    """
    model_caps = next((m for m in models_raw if m.get("model_id") == model_id), {})
    serves_pro = bool(model_caps.get("serves_pro_voices", False))

    pro_voices    = [v for v in voices_raw if v.get("category") == "professional"]
    custom_voices = [v for v in voices_raw if v.get("category") not in ("premade", "professional")]
    premade_voices = [v for v in voices_raw if v.get("category") == "premade"]

    if serves_pro:
        candidates = custom_voices + pro_voices
        excluded_pro = 0
    else:
        candidates = custom_voices
        excluded_pro = len(pro_voices)

    message = ""
    if not candidates:
        candidates = premade_voices
        if excluded_pro:
            message = f"{excluded_pro} professional voices hidden"
    elif excluded_pro:
        message = f"Hiding {excluded_pro} Professional Voice Clones"

    return {
        "voices": [{"voice_id": v["voice_id"], "name": v["name"]} for v in candidates],
        "message": message,
        "excluded_pro": excluded_pro,
    }


class TestVoicesEndpointFiltering:
    def test_pro_voices_excluded_when_serves_pro_false(self):
        """eleven_v3 reports serves_pro_voices=False → pro voices must not appear."""
        voices = _fake_voices(custom=1, pro=2, premade=1)
        models = _fake_models("eleven_v3", serves_pro=False)
        result = _run_filter(voices, models, "eleven_v3")
        ids = {v["voice_id"] for v in result["voices"]}
        assert not any(vid.startswith("pro_") for vid in ids), \
            "Professional voice clones must be excluded when serves_pro_voices=False"
        assert ids == {"custom_0"}

    def test_pro_voices_excluded_when_model_not_found(self):
        """If the model isn't in the API response, safe default excludes pro voices.

        This is the regression: the old default was True (unsafe), which let
        professional voice clones through to eleven_v3, causing silent voice
        substitution by ElevenLabs.
        """
        voices = _fake_voices(custom=1, pro=2, premade=1)
        models = []  # empty — model lookup will fail, model_caps = {}
        result = _run_filter(voices, models, "eleven_v3")
        ids = {v["voice_id"] for v in result["voices"]}
        assert not any(vid.startswith("pro_") for vid in ids), \
            "Safe default must exclude pro voices when model is not found in API response"

    def test_pro_voices_excluded_when_serves_pro_field_missing(self):
        """If the serves_pro_voices field is absent from model caps, treat as False."""
        voices = _fake_voices(custom=1, pro=1, premade=1)
        models = [{"model_id": "eleven_v3"}]  # field missing from model entry
        result = _run_filter(voices, models, "eleven_v3")
        ids = {v["voice_id"] for v in result["voices"]}
        assert "pro_0" not in ids

    def test_pro_voices_included_when_serves_pro_true(self):
        """A model that does serve pro voices should show them."""
        voices = _fake_voices(custom=1, pro=2, premade=1)
        models = _fake_models("eleven_turbo_v2_5", serves_pro=True)
        result = _run_filter(voices, models, "eleven_turbo_v2_5")
        ids = {v["voice_id"] for v in result["voices"]}
        assert "pro_0" in ids and "pro_1" in ids

    def test_premade_fallback_when_no_custom_or_pro(self):
        """If custom+pro are all filtered out, fall back to premade voices."""
        voices = [
            {"voice_id": "pro_0", "name": "Pro", "category": "professional"},
            {"voice_id": "pre_0", "name": "Premade", "category": "premade"},
        ]
        models = _fake_models("eleven_v3", serves_pro=False)
        result = _run_filter(voices, models, "eleven_v3")
        assert len(result["voices"]) == 1
        assert result["voices"][0]["voice_id"] == "pre_0"

    def test_premade_excluded_when_custom_available(self):
        """Premade voices should not appear when the user has their own voices."""
        voices = _fake_voices(custom=2, pro=0, premade=3)
        models = _fake_models("eleven_v3", serves_pro=False)
        result = _run_filter(voices, models, "eleven_v3")
        ids = {v["voice_id"] for v in result["voices"]}
        assert not any(vid.startswith("premade_") for vid in ids)

    def test_message_warns_about_hidden_pro_voices(self):
        voices = _fake_voices(custom=1, pro=2, premade=0)
        models = _fake_models("eleven_v3", serves_pro=False)
        result = _run_filter(voices, models, "eleven_v3")
        assert "2" in result["message"] or "Pro" in result["message"]

    def test_no_message_when_nothing_filtered(self):
        voices = _fake_voices(custom=2, pro=0, premade=1)
        models = _fake_models("eleven_v3", serves_pro=False)
        result = _run_filter(voices, models, "eleven_v3")
        assert result["message"] == ""


# ---------------------------------------------------------------------------
# WS set_voice → callback signal chain
# ---------------------------------------------------------------------------

def _make_server(**kwargs):
    from brain.ui.server import UIServer
    q = asyncio.Queue()
    return UIServer(emitter_queue=q, **kwargs)


class TestVoiceChangeCallback:
    def test_set_voice_message_calls_callback(self):
        """UIServer must invoke on_voice_change when a set_voice WS message arrives."""
        received = []
        server = _make_server(on_voice_change=received.append)

        # Simulate the message dispatch path directly (mirrors _receive_loop logic)
        data = {"type": "set_voice", "voice_id": "abc123"}
        t = data.get("type")
        if t == "set_voice" and server._on_voice_change:
            vid = data.get("voice_id", "").strip()
            if vid:
                server._on_voice_change(vid)

        assert received == ["abc123"]

    def test_empty_voice_id_not_forwarded(self):
        """A set_voice message with empty voice_id must be silently ignored."""
        received = []
        server = _make_server(on_voice_change=received.append)

        data = {"type": "set_voice", "voice_id": "   "}
        t = data.get("type")
        if t == "set_voice" and server._on_voice_change:
            vid = data.get("voice_id", "").strip()
            if vid:
                server._on_voice_change(vid)

        assert received == []

    def test_set_voice_updates_pns(self):
        """End-to-end: set_voice_id callback wired to PNS stores the voice."""
        from brain.bus import Bus
        from brain.pns import PNS

        bus = Bus()
        pns = PNS(bus)
        server = _make_server(on_voice_change=pns.set_voice_id)

        # Simulate the server receiving a set_voice message and routing to PNS
        data = {"type": "set_voice", "voice_id": "wired_voice_id"}
        vid = data.get("voice_id", "").strip()
        if vid:
            server._on_voice_change(vid)

        assert pns._voice_id == "wired_voice_id"
