"""
Provisioner per-(tenant, persona) keying — the Path A enabler that lets one tenant
run several persona processes at once. No real spawns: we inject fake _Proc entries
and assert the lookups resolve per key, and that the no-persona path is unchanged.
"""

from __future__ import annotations

import json
import sys

import brain.provisioner as pv


def test_build_and_launch_repairs_missing_persona_name(tmp_path, monkeypatch):
    """A tenant whose volume settings.json lacks persona_name must be REPAIRED
    (seeded from the bundled default) and still boot — not hard-fail forever, which
    left the user stuck on the 'waking your brain' screen with no brain ever booting
    (the gateway stayed healthy, so there was no crash to see). Regression for the
    second hosted-load failure (the first was the event-loop wedge)."""
    import brain.gateway.org_token as ot
    import brain.vault as vault

    monkeypatch.setattr(pv, "TENANTS_DIR", tmp_path)
    uid = "tenant-no-persona"
    root = tmp_path / uid
    (root / "second_brain").mkdir(parents=True)
    # Pre-existing settings.json WITHOUT persona_name (predates the persona system).
    (root / "settings.json").write_text(json.dumps({"some_other": 1}), encoding="utf-8")

    monkeypatch.setattr(ot, "mint_org_token", lambda _uid: "")  # no network
    monkeypatch.setattr(vault, "fetch_user_keys", lambda _uid: {})

    # Inject a harmless child via cmd_builder (a LIST — avoids shlex-splitting a repo
    # path that may contain spaces, which is a test-env quirk, not a product concern).
    prov = pv.Provisioner(cmd_builder=lambda _port, _env: [sys.executable, "-c", "pass"])
    proc, port, api_port = prov._build_and_launch(uid)
    try:
        data = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        assert data.get("persona_name")  # repaired, non-empty
        assert data["persona_name"] != "default"  # never the cross-bucketing fallback
        assert isinstance(port, int) and isinstance(api_port, int) and port != api_port
    finally:
        proc.terminate()


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
