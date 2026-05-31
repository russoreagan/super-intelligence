"""Phase 3 (colony features): releaser + primer in one message."""

from __future__ import annotations

import pytest

from brain.bus import Bus, Message
from brain.settings import settings


def test_message_primer_defaults_none():
    m = Message(topic="t", payload={}, source="s")
    assert m.primer is None


def test_hop_preserves_primer():
    m = Message(topic="t", payload={}, source="s", primer={"OXT": 0.1})
    assert m.hop().primer == {"OXT": 0.1}


@pytest.mark.asyncio
async def test_primer_collected_and_drained_when_on(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 1)
    bus = Bus()
    await bus.publish(Message(topic="warmth", payload={}, source="s", primer={"OXT": 0.2}))
    await bus.publish(Message(topic="warmth", payload={}, source="s", primer={"OXT": 0.1, "5HT": 0.05}))
    drained = bus.drain_primers()
    assert drained["OXT"] == pytest.approx(0.3)
    assert drained["5HT"] == pytest.approx(0.05)
    # draining clears
    assert bus.drain_primers() == {}


@pytest.mark.asyncio
async def test_primer_ignored_when_off(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 0)
    bus = Bus()
    await bus.publish(Message(topic="warmth", payload={}, source="s", primer={"OXT": 0.2}))
    assert bus.drain_primers() == {}


@pytest.mark.asyncio
async def test_releaser_only_message_no_primer(monkeypatch):
    monkeypatch.setitem(settings._data, "colony_features", 1)
    bus = Bus()
    await bus.publish(Message(topic="warmth", payload={}, source="s"))  # no primer
    assert bus.drain_primers() == {}
