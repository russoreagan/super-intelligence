"""Synchronous capacity refusal + boot-time telemetry on the provisioner.

`ensure()` raised CapacityError inside a fire-and-forget spawn task, so the gateway
could only report `at_capacity` on the caller's SECOND request. `capacity_refusal()`
answers the same two checks synchronously so the first request already says so;
`ensure()` still re-checks under its lock as the backstop. `boot_times`/`boot_stats()`
record how long each cold start took, so a partner's "what is boot p50/p95" can be
answered from data rather than guessed.
"""

from __future__ import annotations

import asyncio

import brain.provisioner as pv


class _FakeProc:
    def __init__(self, alive: bool = True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


def _live(prov, key: str, *, port: int = 1000) -> None:
    prov._procs[key] = pv._Proc(_FakeProc(), port, api_port=port + 1)
    prov._procs[key].booting = False


def test_capacity_refusal_reports_the_tenant_cap(monkeypatch):
    monkeypatch.setattr(pv, "MAX_TENANTS", 2)
    monkeypatch.setattr(pv, "MAX_DEDICATED", 0)
    prov = pv.Provisioner()
    assert prov.capacity_refusal("new-org") is None
    _live(prov, "a")
    _live(prov, "b")
    msg = prov.capacity_refusal("new-org")
    assert msg and "tenant cap reached (2/2" in msg
    # A tenant that is already running never needs a spawn → never refused.
    assert prov.capacity_refusal("a") is None


def test_capacity_refusal_reports_the_dedicated_cap(monkeypatch):
    monkeypatch.setattr(pv, "MAX_TENANTS", 0)
    monkeypatch.setattr(pv, "MAX_DEDICATED", 1)
    prov = pv.Provisioner()
    _live(prov, "org::the_visionary")
    assert prov.capacity_refusal("org") is None  # shared instance: no dedicated cap
    msg = prov.capacity_refusal("org", "the_analyst")
    assert msg and "dedicated-persona cap reached" in msg


def test_ensure_raises_the_same_refusal(monkeypatch):
    monkeypatch.setattr(pv, "MAX_TENANTS", 1)
    prov = pv.Provisioner()
    _live(prov, "a")

    async def _spawn(uid, persona=None):  # must never be reached
        raise AssertionError("spawned past the cap")

    prov._spawn = _spawn
    try:
        asyncio.run(prov.ensure("b"))
    except pv.CapacityError as e:
        assert str(e) == prov.capacity_refusal("b")
    else:
        raise AssertionError("ensure() did not refuse")


def test_boot_stats_percentiles():
    prov = pv.Provisioner()
    assert prov.boot_stats() == {"count": 0, "p50_s": 0.0, "p95_s": 0.0}
    for s in (2.0, 4.0, 6.0, 8.0, 40.0):
        prov.boot_times.append(s)
    st = prov.boot_stats()
    assert st["count"] == 5 and st["p50_s"] == 6.0 and st["p95_s"] == 40.0
