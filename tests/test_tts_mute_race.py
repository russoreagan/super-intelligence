"""The TTS mute button must skip synthesis even when the click lands *after* a
caller has passed the fast-path mute check but is still queued behind an in-flight
utterance (the _speak_lock). Without the under-lock re-check, that queued call would
synthesize — and bill ElevenLabs — the instant the lock frees."""

from __future__ import annotations

import asyncio

import brain.pns as pns_mod
from brain.pns import PNS


class _FakeBus:
    def subscribe(self, _topic):
        return None


def test_mute_while_queued_skips_synthesis(monkeypatch):
    monkeypatch.setattr(pns_mod, "VOICE_MODE", True)
    pns = PNS(_FakeBus())

    spoken: list[str] = []

    async def _fake_speak(text, affect=None):
        spoken.append(text)

    monkeypatch.setattr(pns, "_speak", _fake_speak)

    async def scenario():
        # Simulate an in-flight utterance holding the serialisation lock.
        await pns._speak_lock.acquire()
        # A second response arrives and passes the fast-path mute check (not muted
        # yet), then blocks waiting for the lock.
        queued = asyncio.create_task(pns.emit("queued response"))
        await asyncio.sleep(0.01)  # let emit() reach the lock and park
        # User hits mute while the second response is still queued.
        pns.set_tts_muted(True)
        pns._speak_lock.release()
        await queued

    asyncio.run(scenario())
    assert spoken == []  # the under-lock re-check suppressed synthesis


def test_unmuted_queued_call_still_speaks(monkeypatch):
    """Control: with no mute, the queued call synthesizes normally once the lock frees."""
    monkeypatch.setattr(pns_mod, "VOICE_MODE", True)
    pns = PNS(_FakeBus())

    spoken: list[str] = []

    async def _fake_speak(text, affect=None):
        spoken.append(text)

    monkeypatch.setattr(pns, "_speak", _fake_speak)

    async def scenario():
        await pns._speak_lock.acquire()
        queued = asyncio.create_task(pns.emit("queued response"))
        await asyncio.sleep(0.01)
        pns._speak_lock.release()
        await queued

    asyncio.run(scenario())
    assert spoken == ["queued response"]
