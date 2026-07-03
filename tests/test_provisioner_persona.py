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


# ── Capacity guardrail + RSS telemetry (Railway Pro density work, 2026-07-03) ──


def _inject_live(prov: pv.Provisioner, key: str, tier: str = "full") -> None:
    entry = pv._Proc(_FakeProc(alive=True), port=1000 + len(prov._procs), api_port=None)
    entry.tier = tier
    entry.booting = False
    prov._procs[key] = entry


def test_ensure_refuses_past_max_tenants(monkeypatch):
    """At the BRAIN_MAX_TENANTS ceiling a NEW spawn raises CapacityError; an
    EXISTING live brain keeps resolving (reuse must never be capped)."""
    import asyncio

    monkeypatch.setattr(pv, "MAX_TENANTS", 2)
    prov = pv.Provisioner(cmd_builder=lambda _p, _e: ["true"])
    _inject_live(prov, "org-a")
    _inject_live(prov, "org-b")

    async def _run():
        # Existing tenant: allowed (reuse path, no spawn).
        port = await prov.ensure("org-a")
        assert isinstance(port, int)
        # New tenant: refused at cap.
        try:
            await prov.ensure("org-c")
        except pv.CapacityError as e:
            assert "BRAIN_MAX_TENANTS" in str(e)
            return True
        return False

    assert asyncio.run(_run()) is True
    assert "org-c" not in prov._procs


def test_ensure_uncapped_when_zero(monkeypatch):
    """MAX_TENANTS=0 disables the gate (spawn proceeds to _spawn — stubbed)."""
    import asyncio

    monkeypatch.setattr(pv, "MAX_TENANTS", 0)
    prov = pv.Provisioner(cmd_builder=lambda _p, _e: ["true"])
    _inject_live(prov, "org-a")

    async def _fake_spawn(user_id, persona=None):
        return 4242

    prov._spawn = _fake_spawn

    async def _run():
        return await prov.ensure("org-new")

    assert asyncio.run(_run()) == 4242


def test_tenant_stats_reports_live_only():
    prov = pv.Provisioner(cmd_builder=lambda _p, _e: ["true"])
    _inject_live(prov, "org-a", tier="full")
    _inject_live(prov, "org-b", tier="lite")
    dead = pv._Proc(_FakeProc(alive=False), port=1, api_port=None)
    prov._procs["org-dead"] = dead

    stats = prov.tenant_stats()
    keys = {s["key"] for s in stats}
    assert keys == {"org-a", "org-b"}
    for s in stats:
        assert s["uptime_s"] >= 0
        assert s["tier"] in ("full", "lite")
        # rss may be None for fake pids — the field must exist either way
        assert "rss_mb" in s


# ── Elastic placement primitives (Phase 1, 2026-07-03) ──────────────────────


def test_keys_for_and_dedicated_count():
    prov = pv.Provisioner(cmd_builder=lambda _p, _e: ["true"])
    _inject_live(prov, "org-a")
    _inject_live(prov, "org-a::the_analyst")
    _inject_live(prov, "org-a::the_poet")
    _inject_live(prov, "org-b")
    dead = pv._Proc(_FakeProc(alive=False), port=1, api_port=None)
    prov._procs["org-a::the_sage"] = dead  # dead → excluded

    keys = prov.keys_for("org-a")
    assert keys == ["org-a::the_analyst", "org-a::the_poet", "org-a"]  # default LAST
    assert prov.dedicated_count("org-a") == 2
    assert prov.promoted_personas("org-a") == ["the_analyst", "the_poet"]
    assert prov.keys_for("org-b") == ["org-b"]
    # org-b must not match a prefix of another org id
    _inject_live(prov, "org-bb")
    assert "org-bb" not in prov.keys_for("org-b")


def test_max_dedicated_gates_persona_spawns_only(monkeypatch):
    import asyncio

    monkeypatch.setattr(pv, "MAX_TENANTS", 0)
    monkeypatch.setattr(pv, "MAX_DEDICATED", 1)
    prov = pv.Provisioner(cmd_builder=lambda _p, _e: ["true"])
    _inject_live(prov, "org-a")
    _inject_live(prov, "org-a::the_analyst")

    async def _fake_spawn(user_id, persona=None):
        return 4242

    prov._spawn = _fake_spawn

    async def _run():
        # Second dedicated spawn → refused.
        try:
            await prov.ensure("org-a", "the_poet")
            raised = False
        except pv.CapacityError as e:
            raised = "BRAIN_MAX_DEDICATED" in str(e)
        # Default-instance spawn for another org: NOT gated by MAX_DEDICATED.
        port = await prov.ensure("org-new")
        return raised, port

    raised, port = asyncio.run(_run())
    assert raised is True
    assert port == 4242


def test_write_placement_files_derives_and_removes(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "TENANTS_DIR", tmp_path)
    pv._placement_last.clear()
    prov = pv.Provisioner(cmd_builder=lambda _p, _e: ["true"])
    _inject_live(prov, "org-a")
    _inject_live(prov, "org-a::the_analyst")

    pv.write_placement_files(prov)
    pfile = pv.placement_file("org-a")
    assert pfile.exists()
    import json as _json

    data = _json.loads(pfile.read_text())
    assert data["promoted"] == ["the_analyst"]

    # Dedicated instance dies → next derivation removes the file (self-healing).
    prov._procs["org-a::the_analyst"]._alive = False
    prov._procs["org-a::the_analyst"].proc._alive = False
    pv.write_placement_files(prov)
    assert not pfile.exists()
