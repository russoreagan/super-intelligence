"""
Provisioner per-(tenant, persona) keying — the Path A enabler that lets one tenant
run several persona processes at once. No real spawns: we inject fake _Proc entries
and assert the lookups resolve per key, and that the no-persona path is unchanged.
"""

from __future__ import annotations

import brain.provisioner as pv


class _FakeProc:
    def __init__(self, alive: bool, pid: int = 123):
        self._alive = alive
        self.pid = pid

    def poll(self):
        return None if self._alive else 0


def test_key_is_tenant_only_without_persona_and_composite_with():
    assert pv.Provisioner._key("user-1") == "user-1"
    assert pv.Provisioner._key("user-1", None) == "user-1"
    assert pv.Provisioner._key("user-1", "") == "user-1"
    assert pv.Provisioner._key("user-1", "the_visionary") == "user-1::the_visionary"


def test_status_and_is_running_resolve_per_persona():
    prov = pv.Provisioner()
    prov._procs = {
        prov._key("u", "the_visionary"): pv._Proc(_FakeProc(True), 9101, api_port=9201),
        prov._key("u", "the_adversary"): pv._Proc(_FakeProc(False), 9102, api_port=9202),
    }
    # Each persona is looked up independently.
    assert prov.status("u", "the_visionary")["port"] == 9101
    assert prov.is_running("u", "the_visionary") is True
    assert prov.is_running("u", "the_adversary") is False
    # A persona that was never spawned, and the bare tenant key, are both absent.
    assert prov.status("u", "the_sage") is None
    assert prov.status("u") is None


def test_touch_updates_only_the_addressed_persona():
    prov = pv.Provisioner()
    v = pv._Proc(_FakeProc(True), 9101)
    a = pv._Proc(_FakeProc(True), 9102)
    v.last_active = a.last_active = 0.0
    prov._procs = {prov._key("u", "the_visionary"): v, prov._key("u", "the_adversary"): a}
    prov.touch("u", "the_visionary")
    assert v.last_active > 0.0
    assert a.last_active == 0.0


def test_no_persona_path_is_unchanged():
    # Backward-compat: string tenant keys + bare lookups behave exactly as before.
    prov = pv.Provisioner()
    prov._procs = {"a": pv._Proc(_FakeProc(True), 9001)}
    assert prov.is_running("a") is True
    assert prov.status("a")["port"] == 9001
    assert prov.live_count() == 1


def test_live_count_spans_all_personas():
    prov = pv.Provisioner()
    prov._procs = {
        prov._key("u", "the_visionary"): pv._Proc(_FakeProc(True), 9101),
        prov._key("u", "the_adversary"): pv._Proc(_FakeProc(True), 9102),
        prov._key("u", "the_sage"): pv._Proc(_FakeProc(False), 9103),  # died, unreaped
    }
    assert prov.live_count() == 2
