"""Personas keep their own voice, and a voice that's gone from the ElevenLabs
account falls back to the first voice on the app's list instead of going
silent (brain/settings.py declared_type, persona_chem.voice_id_for,
brain/voices.py)."""

from __future__ import annotations

import asyncio
import json
import time

import brain.voices as voices
from brain.settings import Settings, declared_type

# ── personas keep their voice ────────────────────────────────────────────────


def test_any_persona_can_hold_a_voice_key():
    assert declared_type("persona_voice_antar") is str
    assert declared_type("persona_voice_the_analyst") is str
    assert declared_type("persona_voice_Bad-Key") is None
    assert declared_type("persona_voice_") is None
    assert declared_type("not_a_setting") is None


def test_custom_persona_voice_survives_save_and_reload(tmp_path, monkeypatch):
    import brain.settings as bs

    path = tmp_path / "settings.json"
    monkeypatch.setattr(bs, "SETTINGS_PATH", path)
    s = Settings()
    s.save({"persona_voice_antar": "voice-antar", "persona_voice_yssarin": "voice-yss"})
    assert json.loads(path.read_text())["persona_voice_antar"] == "voice-antar"
    again = Settings()
    assert again.get("persona_voice_antar") == "voice-antar"
    assert again.get("persona_voice_yssarin") == "voice-yss"


def test_voice_id_for_finds_a_custom_personas_voice(monkeypatch):
    from brain import persona_chem
    from brain.settings import settings

    data = {"persona_voice_antar": "voice-antar", "persona_voice_id": "generic"}
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda k, d=None: data.get(k, real(k, d)))
    assert persona_chem.voice_id_for("antar") == "voice-antar"
    assert persona_chem.voice_id_for("someone_else") == "generic"


def test_voice_keys_accept_display_names_and_ui_slugs():
    from brain.persona_chem import voice_keys_for

    keys = voice_keys_for("Antar Eketh")
    assert "persona_voice_antar_eketh" in keys
    assert voice_keys_for("O'Brien")[:2] == ["persona_voice_o_brien", "persona_voice_obrien"]


def test_config_save_of_a_non_running_persona_keeps_its_voice(tmp_path, monkeypatch):
    """The persona workspace saves a persona you're not running through the
    config_persona path, which used to drop the voice entirely."""
    import brain.settings as bs

    monkeypatch.setattr(bs, "SETTINGS_PATH", tmp_path / "settings.json")
    src = (bs.Path(__file__).parent.parent / "brain" / "ui" / "server.py").read_text()
    assert 'body.get("config_voice_id")' in src
    js = (bs.Path(__file__).parent.parent / "brain" / "ui" / "settings-ui.js").read_text()
    assert "body.config_voice_id" in js


def test_the_admin_resolves_its_own_voice_from_the_boot_slug(monkeypatch):
    """Tenants boot with persona_name "the_admin" (the slug); the console saves
    "The Admin"'s voice under persona_voice_the_admin. Both must meet."""
    from brain import persona_chem
    from brain.settings import settings

    data = {"persona_name": "the_admin", "persona_voice_the_admin": "admin-voice",
            "persona_voice_id": "generic"}
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda k, d=None: data.get(k, real(k, d)))
    assert persona_chem.voice_id_for() == "admin-voice"
    assert persona_chem.voice_id_for("The Admin") == "admin-voice"


def test_console_never_rewrites_the_inherited_voice_on_a_persona_pick():
    """Picking a voice for one persona used to also set the generic
    persona_voice_id in the page (never saved for a non-running persona), so
    a persona with no voice of its own showed that pick while the server spoke
    the real generic voice. The socket also sent the placeholder voice on open."""
    from pathlib import Path

    ui = Path(__file__).parent.parent / "brain" / "ui"
    js = (ui / "settings-ui.js").read_text()
    assert "values.persona_voice_id = vid" not in js
    html = (ui / "index.html").read_text()
    assert "if (vid && _voiceResolved) ws.send" in html
    assert "const patch = { persona_voice_id: vid };" not in html
    srv = (ui / "server.py").read_text()
    assert "vid = voice_id_for()" in srv


# ── fallback when a voice is gone ────────────────────────────────────────────


def _known(available, first, monkeypatch):
    """Install an account view as refresh would."""

    async def fake_refresh(api_key=None, model_id=None):
        _k, acct = voices._account(api_key)
        acct.available, acct.first, acct.at = set(available), first, time.monotonic()
        acct.missing -= acct.available
        return acct

    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(voices, "refresh", fake_refresh)
    asyncio.run(fake_refresh())


def test_present_voice_is_kept(monkeypatch):
    _known({"a", "b"}, "a", monkeypatch)
    assert voices.effective_voice("b") == "b"


def test_missing_voice_speaks_with_the_first_on_the_list(monkeypatch):
    _known({"a", "b"}, "a", monkeypatch)
    assert voices.effective_voice("gone") == "a"


def test_unknown_view_never_swaps(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    assert voices.effective_voice("anything") == "anything"


def test_reported_missing_voice_falls_back_and_is_remembered(monkeypatch):
    _known({"a"}, "a", monkeypatch)
    assert asyncio.run(voices.fallback_for("gone")) == "a"
    assert voices.effective_voice("gone") == "a"


def test_readded_voice_is_used_again(monkeypatch):
    _known({"a"}, "a", monkeypatch)
    asyncio.run(voices.fallback_for("back"))
    _known({"a", "back"}, "a", monkeypatch)
    assert voices.effective_voice("back") == "back"


def test_no_fallback_when_the_list_is_only_that_voice(monkeypatch):
    _known(set(), "only", monkeypatch)
    assert asyncio.run(voices.fallback_for("only")) is None


def test_missing_voice_error_shapes():
    from brain.tts_dialogue import DialogueError

    assert voices.is_voice_missing(DialogueError("x", code="voice_not_found", config=True))
    assert voices.is_voice_missing(Exception("A voice with voice_id 'x' was not found."))
    assert not voices.is_voice_missing(Exception("quota exceeded"))

    class _Api(Exception):
        status_code = 404
        body = {"detail": {"status": "voice_not_found"}}

    assert voices.is_voice_missing(_Api())


# ── the speaking paths use it ────────────────────────────────────────────────


def test_console_voice_not_found_retries_with_the_fallback(monkeypatch):
    import brain.pns as pns_mod
    from tests.test_tts_dialogue import PCM, _audio_frame, _FakeBus, _FakeServer, _StubHTTP

    _known({"good"}, "good", monkeypatch)
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "gone")
    monkeypatch.delenv("ELEVENLABS_MODEL_ID", raising=False)
    monkeypatch.setattr(pns_mod, "BROWSER_AUDIO_MODE", True)
    import elevenlabs

    monkeypatch.setattr(elevenlabs, "AsyncElevenLabs", _StubHTTP)
    server = _FakeServer(frames=[_audio_frame(), json.dumps({"is_final": True})]).install(
        monkeypatch
    )
    p = pns_mod.PNS(_FakeBus())
    p._tts_ws_queue = asyncio.Queue(maxsize=10_000)
    asyncio.run(p._speak("Hello there."))
    # The known-missing voice was swapped before the socket opened.
    assert server.sent[0] == {"voices": ["good"]}
    played = b"".join(
        x
        for x in iter(
            lambda: p._tts_ws_queue.get_nowait() if not p._tts_ws_queue.empty() else None, None
        )
        if isinstance(x, bytes)
    )
    assert PCM in played


def test_engine_voice_not_found_retries_with_the_fallback(monkeypatch):
    import brain.api.audio as audio
    from tests.test_tts_dialogue import _FakeServer

    _known({"good"}, "good", monkeypatch)
    monkeypatch.delenv("ELEVENLABS_MODEL_ID", raising=False)
    # Cold path: the voice looks fine until ElevenLabs rejects it.
    _k, acct = voices._account()
    acct.available.add("stale")
    err = json.dumps({"error": "voice_not_found", "message": "not found", "code": 1008})
    calls = {"n": 0}

    class _Server(_FakeServer):
        def install(self, mp):
            super().install(mp)
            import sys

            real = sys.modules["websockets"].connect

            def connect(url, **kw):
                calls["n"] += 1
                self.frames = (
                    [err]
                    if calls["n"] == 1
                    else [__import__("tests.test_tts_dialogue", fromlist=["x"])._audio_frame()]
                    + [json.dumps({"is_final": True})]
                )
                return real(url, **kw)

            sys.modules["websockets"].connect = connect
            return self

    server = _Server().install(monkeypatch)
    out = asyncio.run(audio.synthesize("Hello there.", voice_id="stale", fmt="pcm_22050"))
    assert out["voice_id"] == "good" and out["model"] == "eleven_v4_turbo"
    assert {"voices": ["good"]} in server.sent
