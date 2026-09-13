"""brain/persona_residency — bounded per-persona memory in one shared process."""

from __future__ import annotations

import asyncio

import pytest

from brain import persona_residency as pr
from brain.settings import settings

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pr._reset_for_tests()
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.setitem(settings._data, "persona_residency_enabled", 1)
    monkeypatch.setitem(settings._data, "persona_resident_cap", 2)
    monkeypatch.setitem(settings._data, "persona_resident_idle_s", 3600)
    monkeypatch.setitem(settings._data, "persona_residency_sweep_s", 60)
    yield
    pr._reset_for_tests()


class _Holder:
    def __init__(self, *personas):
        self.live = set(personas)
        self.evicted: list[str] = []

    def evict(self, slug):
        self.evicted.append(slug)
        self.live.discard(slug)

    def resident(self):
        return sorted(self.live)


def test_sweep_evicts_lru_beyond_cap_and_protects(monkeypatch):
    h = _Holder("a", "b", "c", "d", "home_p")
    pr.register("h", h.evict, h.resident)
    for i, p in enumerate(("a", "b", "c", "d")):
        pr.touch(p, now=NOW + i)
    pr.register_protector("sessions", lambda: ["b"])
    monkeypatch.setitem(settings._data, "persona_resident_cap", 1)
    out = asyncio.run(pr.sweep(protected=["d"], now=NOW + 10, force=True))
    # cap 1 over {a, c} (b, d and home are protected) → oldest first: a out, c kept.
    assert out == ["a"] and h.live == {"b", "c", "d", "home_p"}


def test_sweep_evicts_idle_and_is_throttled(monkeypatch):
    monkeypatch.setitem(settings._data, "persona_resident_cap", 0)
    h = _Holder("a", "b")
    pr.register("h", h.evict, h.resident)
    pr.touch("a", now=NOW - 7200)
    pr.touch("b", now=NOW - 10)
    assert asyncio.run(pr.sweep(now=NOW, force=True)) == ["a"]
    pr.touch("b", now=NOW - 7200)
    assert asyncio.run(pr.sweep(now=NOW + 1)) == []  # throttled
    assert asyncio.run(pr.sweep(now=NOW + 61)) == ["b"]


def test_disabled_never_evicts_and_async_evictors_awaited(monkeypatch):
    calls = []

    async def aev(slug):
        calls.append(slug)

    pr.register("a", aev, lambda: ["x", "y", "z"])
    monkeypatch.setitem(settings._data, "persona_residency_enabled", 0)
    assert asyncio.run(pr.sweep(now=NOW, force=True)) == []
    monkeypatch.setitem(settings._data, "persona_residency_enabled", 1)
    assert asyncio.run(pr.sweep(now=NOW, force=True)) == ["x"]  # cap 2 → one out
    assert calls == ["x"]


def test_bind_persona_touches():
    from brain.second_brain.store import bind_persona

    with bind_persona("Ahab"):
        pass
    assert pr.last_bound("ahab") is not None


def test_dmn_evict_persists_then_drops_bundle():
    from unittest.mock import AsyncMock

    from brain.dmn import DefaultModeNetwork
    from brain.second_brain.store import active_persona

    d = DefaultModeNetwork.__new__(DefaultModeNetwork)
    d._home, d._pstate, d._hydrated_personas = (
        "home_p",
        {"ahab": {"x": 1}, "home_p": {}},
        {"ahab", "home_p"},
    )
    d._roster_cache, d._roster_ts, d._rr_idx = [], 0.0, 0
    bound = []

    async def _persist():
        bound.append(active_persona())

    d._persist_active = AsyncMock(side_effect=_persist)
    assert asyncio.run(d.evict_persona("ahab")) is True
    assert bound == ["ahab"] and "ahab" not in d._pstate and "ahab" not in d._hydrated_personas
    assert asyncio.run(d.evict_persona("home_p")) is False and "home_p" in d._pstate
    assert asyncio.run(d.evict_persona("nobody")) is False
    assert d.resident_personas() == ["home_p"]


def test_wiring_and_mandates_evict(monkeypatch):
    from brain import mandates

    monkeypatch.setattr(mandates, "_catalog", {"ahab": {"m": {}}})
    assert mandates.resident_personas() == ["ahab"]
    assert mandates.evict_persona("Ahab") is True and mandates.evict_persona("ahab") is False


def test_client_chem_evict_idle_persists_lru(monkeypatch):
    from brain.client_chem import ClientChemRegistry, InMemoryChemStore

    class _Pair:
        def restore(self, s):
            pass

        def snapshot(self):
            return {"DA": 0.5}

    class _Bus:
        def new_chem(self):
            return _Pair()

    clock = {"t": 100.0}
    store = InMemoryChemStore()
    reg = ClientChemRegistry(_Bus(), store, persona="p", now_fn=lambda: clock["t"])
    for i, eu in enumerate(("u1", "u2", "u3")):
        clock["t"] = 100.0 + i
        reg.get_or_create(eu)
    clock["t"] = 200.0
    reg.get_or_create("u1")  # u1 is now the most recent
    assert reg.evict_idle(2) == 1
    assert set(reg._live) == {"u1", "u3"}
    assert store.load("p:u2")[0] is not None  # persisted before the drop
    assert reg.evict_idle(0) == 0


def test_session_evict_persona_chem_idle_and_api_protection():
    from collections import OrderedDict
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from brain.session_turn import _TurnMixin

    s = _TurnMixin.__new__(_TurnMixin)
    reg = MagicMock()
    s._persona_chem = OrderedDict({"ahab": reg})
    assert s.resident_persona_chem() == ["ahab"]
    assert s.evict_persona_chem_idle("ahab") is True and reg.flush.called and not s._persona_chem
    assert s.evict_persona_chem_idle("ahab") is False
    s._api_registry = SimpleNamespace(
        _sessions={"s1": SimpleNamespace(agent_id="luna.m"), "s2": SimpleNamespace(agent_id="")}
    )
    assert s.api_session_personas() == ["luna"]
