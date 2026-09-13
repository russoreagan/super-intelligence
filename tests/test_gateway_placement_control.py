"""Placement desired-state loop (brain/gateway/placement_control) against fakes.

Same shape as tests/test_gateway_pod_reconcile.py: no RunPod, no Supabase, the
clock is passed in. Contract under test: a dedicated row gets a process (pinned
when always_on), a missing row stops one, a pod row gets a pod that wakes on
demand, holds on use, sleeps on idle / budget / cap, meters per tick, and is
published so the consumer moves onto it only when it is READY.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

import brain.gpu_usage_store as gus
import brain.org_settings as os_
import brain.persona_placement as pp
import brain.provisioner as pv
from brain.gateway import placement_control as pc
from brain.gateway import pod_reconcile as rc
from brain.pod_pool import PoolConfig

# Wall-clock based: pressure files are staleness-checked against the real clock and
# paid_until dates are compared against the tick's `now`.
NOW = float(int(time.time()))
_REAL_USD_TODAY_FOR = gus.usd_today_for
ORG = "org-1"


# ── fakes ────────────────────────────────────────────────────────────────────


class _Prov:
    def __init__(self, keys=()):
        self.keys = set(keys)
        self.ensured: list[str] = []
        self.stopped: list[str] = []
        self.pinned: dict[str, bool] = {}
        self.refuse: dict[str, str] = {}

    @staticmethod
    def _key(org, persona=None):
        return org if not persona else f"{org}::{persona}"

    def keys_for_all(self):
        return sorted(self.keys)

    def promoted_personas(self, org):
        return sorted(k.split("::", 1)[1] for k in self.keys if k.startswith(f"{org}::"))

    async def ensure(self, org, persona=None):
        key = self._key(org, persona)
        if key in self.refuse:
            raise pv.CapacityError(self.refuse[key])
        self.ensured.append(key)
        self.keys.add(key)
        return 1234

    async def stop_user(self, org, persona=None):
        key = self._key(org, persona)
        self.stopped.append(key)
        self.keys.discard(key)

    def set_pinned(self, org, persona, pinned):
        key = self._key(org, persona)
        if key not in self.keys:
            return False
        self.pinned[key] = bool(pinned)
        return True

    def status(self, org, persona=None):
        key = self._key(org, persona)
        return {"port": 1, "api_port": 2, "booting": False, "pid": 1} if key in self.keys else None

    def is_running(self, org, persona=None):
        return self._key(org, persona) in self.keys


class _Mgr:
    """The RunPodManager surface the controller touches."""

    def __init__(self, kind, key, gpu_type, *, can=True, existing=None):
        self.kind, self.key, self.gpu_type = kind, key, gpu_type
        self.can = can
        self._pod_id = existing
        self._status = "ready" if existing else "off"
        self._cost_per_hr = 0.8 if existing else None
        self.wakes = 0
        self.pauses = 0

    def _pod_host(self, pid):
        return f"https://{pid}-11434.proxy.runpod.net"

    async def discover_and_publish_host(self):
        return self._pod_host(self._pod_id) if self._pod_id else None

    async def ensure_running(self):
        self.wakes += 1
        if not self.can:
            self._status = "failed"
            return False
        self._pod_id = self._pod_id or f"pod-{self.key[-6:]}"
        self._status = "ready"
        self._cost_per_hr = 0.8
        return True

    async def pause(self):
        self.pauses += 1
        self._pod_id, self._status = None, "off"


class _Pool:
    def __init__(self):
        self.cfg = PoolConfig(max_pods=3, grace_s=600.0)
        self.standalone: dict = {}
        self.fallback: dict = {}
        self.extra_pods: list = []


def _row(persona, mode="dedicated", pod="pool", **kw):
    return {
        "persona": persona,
        "mode": mode,
        "pod": pod,
        "gpu_type": kw.get("gpu_type"),
        "always_on": kw.get("always_on", True),
        "paid_until": kw.get("paid_until"),
        "placed": True,
        "expired": False,
    }


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_MULTI_PERSONA", "1")
    monkeypatch.delenv("BRAIN_MAX_STANDALONE_PODS", raising=False)
    monkeypatch.setattr(os_, "refresh_org_caps", lambda org, client=None, force=False: {})
    monkeypatch.setattr(gus, "usd_today_for", lambda org, client=None: 0.0)
    (tmp_path / "pressure").mkdir()


def _budget(monkeypatch, usd):
    monkeypatch.setattr(
        os_,
        "cached_org_caps",
        lambda org: {"max_dedicated_instances": 0, "gpu_daily_usd_budget": usd},
    )


def _rows(monkeypatch, rows_by_org):
    monkeypatch.setattr(pp, "list_all", lambda client=None: rows_by_org)


def _pressure(tmp_path, key, **body):
    import time as _time

    d = tmp_path / "pressure"
    b = {"proc_key": key, "ts": _time.time(), "calls_1m": 0, "busy_s_1m": 0.0}
    b.update(body)
    (d / f"{key}.json").write_text(json.dumps(b))
    return d


def _tick(prov, state, *, pool=None, now=NOW, factory=None, pressure_dir=None, stops=None):
    stops = stops if stops is not None else []

    async def _stop(org, persona):
        stops.append(f"{org}::{persona}")
        await prov.stop_user(org, persona)

    async def _go():
        rep = await pc.placement_tick(
            prov,
            state,
            pool=pool,
            stop_instance=_stop,
            now=now,
            manager_factory=factory or (lambda k, key, g: _Mgr(k, key, g)),
            pressure_dir=pressure_dir,
        )
        await asyncio.sleep(0.01)  # let background spawns / wakes run
        return rep

    return asyncio.run(_go())


# ── desired state ─────────────────────────────────────────────────────────────


def test_desired_instances_are_dedicated_and_unexpired():
    rows = {
        ORG: [
            _row("ahab"),
            _row("ishmael", mode="shared"),
            _row("queequeg", paid_until="2020-01-01T00:00:00+00:00"),
        ]
    }
    want = pc.desired_instances(rows, NOW)
    assert set(want) == {f"{ORG}::ahab"}
    assert want[f"{ORG}::ahab"]["org"] == ORG


def test_registry_failure_keeps_last_known_and_never_stops_on_a_blink(monkeypatch):
    prov = _Prov(keys={ORG, f"{ORG}::ahab"})
    state = pc.PlacementState()
    _rows(monkeypatch, {ORG: [_row("ahab")]})
    _tick(prov, state)
    assert prov.stopped == []
    # The registry blinks: nothing is stopped, the cached rows still apply.
    _rows(monkeypatch, None)
    state.desired_read_at = 0.0  # force a re-read
    rep = _tick(prov, state)
    assert prov.stopped == [] and rep["desired"] == 1


def test_first_read_failure_is_reported_once_and_does_nothing(monkeypatch, caplog):
    prov = _Prov(keys={ORG, f"{ORG}::ahab"})
    state = pc.PlacementState()
    _rows(monkeypatch, None)
    with caplog.at_level(logging.WARNING):
        rep1 = _tick(prov, state)
        rep2 = _tick(prov, state)
    assert rep1.get("unavailable") and rep2.get("unavailable")
    assert prov.stopped == [] and prov.ensured == []
    assert sum("registry unreadable" in r.message for r in caplog.records) == 1


def test_tick_uses_the_gateway_service_role_client_not_the_org_helper(monkeypatch):
    """Regression (first deploy): persona_placement._sb() asks supabase_client for
    the process's own org id, which the gateway has none of. The tick must hand
    every reader the gateway's service-role client explicitly."""
    seen: list = []
    sentinel = object()
    monkeypatch.setattr(pc, "gateway_client", lambda: sentinel)
    monkeypatch.setattr(pp, "list_all", lambda client=None: seen.append(client) or {})
    prov = _Prov(keys={ORG})
    _tick(prov, pc.PlacementState())
    assert seen == [sentinel]


def test_kill_switch_disables_the_loop(monkeypatch):
    monkeypatch.setenv("BRAIN_MULTI_PERSONA", "0")
    prov = _Prov(keys={ORG})
    _rows(monkeypatch, {ORG: [_row("ahab")]})
    rep = _tick(prov, pc.PlacementState())
    assert rep.get("disabled") and prov.ensured == []


# ── processes ─────────────────────────────────────────────────────────────────


def test_dedicated_row_spawns_and_pins_its_process(monkeypatch):
    prov = _Prov(keys={ORG})
    state = pc.PlacementState()
    _rows(monkeypatch, {ORG: [_row("ahab", always_on=True)]})
    rep = _tick(prov, state)
    assert "spawn:ahab" in rep["actions"]
    assert prov.ensured == [f"{ORG}::ahab"]
    # Second tick: live, so it is pinned (always_on) and not re-spawned.
    rep = _tick(prov, state)
    assert prov.ensured == [f"{ORG}::ahab"]
    assert prov.pinned[f"{ORG}::ahab"] is True


def test_always_on_false_leaves_the_process_reapable(monkeypatch):
    prov = _Prov(keys={ORG, f"{ORG}::ahab"})
    _rows(monkeypatch, {ORG: [_row("ahab", always_on=False)]})
    _tick(prov, pc.PlacementState())
    assert prov.pinned[f"{ORG}::ahab"] is False


def test_missing_row_consolidates_and_stops_the_instance(monkeypatch):
    prov = _Prov(keys={ORG, f"{ORG}::ahab", f"{ORG}::starbuck"})
    _rows(monkeypatch, {ORG: [_row("ahab")]})
    stops: list[str] = []
    rep = _tick(prov, pc.PlacementState(), stops=stops)
    assert stops == [f"{ORG}::starbuck"]
    assert "stop:starbuck" in rep["actions"]
    assert prov.keys == {ORG, f"{ORG}::ahab"}


def test_expired_row_is_demoted(monkeypatch):
    prov = _Prov(keys={ORG, f"{ORG}::ahab"})
    _rows(monkeypatch, {ORG: [_row("ahab", paid_until="2020-01-01T00:00:00+00:00")]})
    stops: list[str] = []
    _tick(prov, pc.PlacementState(), stops=stops)
    assert stops == [f"{ORG}::ahab"]


def test_capacity_refusal_is_reported_once_not_retried_noisily(monkeypatch, caplog):
    prov = _Prov(keys={ORG})
    prov.refuse[f"{ORG}::ahab"] = "dedicated-persona cap reached"
    state = pc.PlacementState()
    _rows(monkeypatch, {ORG: [_row("ahab")]})
    with caplog.at_level(logging.WARNING):
        _tick(prov, state)
        _tick(prov, state)
    assert f"{ORG}::ahab" in state.refused
    assert sum("not started" in r.message for r in caplog.records) == 1
    assert pc.summary(state)["refused"][f"{ORG}::ahab"].startswith("dedicated-persona cap")


# ── pods ──────────────────────────────────────────────────────────────────────


def test_wanted_pods_group_standalone_per_instance_and_org_per_org():
    desired = pc.desired_instances(
        {
            ORG: [
                _row("a", pod="standalone", gpu_type="NVIDIA L40"),
                _row("b", pod="org"),
                _row("c", pod="org"),
                _row("d"),
            ]
        },
        NOW,
    )
    wanted = pc.wanted_pods(desired)
    assert set(wanted) == {f"{ORG}::a", ORG}
    assert wanted[f"{ORG}::a"]["kind"] == "standalone"
    assert wanted[f"{ORG}::a"]["gpu_type"] == "NVIDIA L40"
    assert wanted[ORG]["kind"] == "org" and wanted[ORG]["consumers"] == {f"{ORG}::b", f"{ORG}::c"}


def test_no_budget_means_no_pod_and_the_instance_rides_the_pool(monkeypatch, tmp_path):
    prov = _Prov(keys={ORG, f"{ORG}::ahab"})
    _rows(monkeypatch, {ORG: [_row("ahab", pod="standalone")]})
    _budget(monkeypatch, 0.0)
    pool = _Pool()
    d = _pressure(tmp_path, f"{ORG}::ahab", demand_ts=NOW - 5)
    rep = _tick(prov, pc.PlacementState(), pool=pool, pressure_dir=d)
    assert not any(a.startswith("wake") for a in rep["actions"])
    assert pool.standalone == {}
    assert pool.fallback[f"{ORG}::ahab"]["reason"] == "no_budget"


def test_fresh_demand_wakes_the_pod_and_publishes_it_when_ready(monkeypatch, tmp_path):
    prov = _Prov(keys={ORG, f"{ORG}::ahab"})
    state = pc.PlacementState()
    _rows(monkeypatch, {ORG: [_row("ahab", pod="standalone", gpu_type="NVIDIA L40")]})
    _budget(monkeypatch, 12.0)
    pool = _Pool()
    d = _pressure(tmp_path, f"{ORG}::ahab", demand_ts=NOW - 5)
    rep = _tick(prov, state, pool=pool, pressure_dir=d)
    assert any(a.startswith("wake:standalone") for a in rep["actions"])
    pod = state.pods[f"{ORG}::ahab"]
    assert pod.manager.gpu_type == "NVIDIA L40" and pod.manager.wakes == 1
    # The wake ran in the background; the NEXT tick publishes the ready pod.
    rep = _tick(prov, state, pool=pool, pressure_dir=d)
    entry = pool.standalone[f"{ORG}::ahab"]
    assert entry["state"] == "ready" and entry["kind"] == "standalone" and entry["host"]
    assert f"{ORG}::ahab" not in pool.fallback
    assert [p.kind for p in pool.extra_pods] == ["standalone"]
    assert rep["serving"] == 1


def test_no_demand_leaves_a_cold_pod_cold(monkeypatch, tmp_path):
    prov = _Prov(keys={ORG, f"{ORG}::ahab"})
    _rows(monkeypatch, {ORG: [_row("ahab", pod="standalone")]})
    _budget(monkeypatch, 12.0)
    pool = _Pool()
    d = _pressure(tmp_path, f"{ORG}::ahab")  # no demand stamp at all
    state = pc.PlacementState()
    _tick(prov, state, pool=pool, pressure_dir=d)
    assert state.pods[f"{ORG}::ahab"].manager.wakes == 0
    assert pool.fallback[f"{ORG}::ahab"]["reason"] == "idle"


def test_pod_that_cannot_be_created_reads_fallback_pool_and_retries(monkeypatch, tmp_path, caplog):
    prov = _Prov(keys={ORG, f"{ORG}::ahab"})
    _rows(monkeypatch, {ORG: [_row("ahab", pod="standalone")]})
    _budget(monkeypatch, 12.0)
    pool = _Pool()
    d = _pressure(tmp_path, f"{ORG}::ahab", demand_ts=NOW - 5)
    state = pc.PlacementState()
    factory = lambda k, key, g: _Mgr(k, key, g, can=False)  # noqa: E731
    with caplog.at_level(logging.WARNING):
        _tick(prov, state, pool=pool, pressure_dir=d, factory=factory)
        _tick(prov, state, pool=pool, pressure_dir=d, factory=factory)
    pod = state.pods[f"{ORG}::ahab"]
    assert pod.manager.wakes == 2, "retried each tick"
    assert pool.fallback[f"{ORG}::ahab"]["reason"] == "fallback_pool"
    assert sum("could not be created" in r.message for r in caplog.records) == 1


def test_idle_pod_is_paused_after_grace_and_use_holds_it(monkeypatch, tmp_path):
    prov = _Prov(keys={ORG, f"{ORG}::ahab"})
    _rows(monkeypatch, {ORG: [_row("ahab", pod="standalone")]})
    _budget(monkeypatch, 12.0)
    pool = _Pool()
    state = pc.PlacementState()
    factory = lambda k, key, g: _Mgr(k, key, g, existing="pod-x")  # noqa: E731
    # Fresh use → held.
    d = _pressure(tmp_path, f"{ORG}::ahab", demand_ts=NOW - 5, use_ts=NOW - 5)
    rep = _tick(prov, state, pool=pool, pressure_dir=d, factory=factory)
    assert not any(a.startswith("pause") for a in rep["actions"])
    assert pool.standalone[f"{ORG}::ahab"]["pod_id"] == "pod-x"
    # Use goes stale past the grace period, up for longer than grace → paused.
    pod = state.pods[f"{ORG}::ahab"]
    pod.up_since = NOW - 5000
    d = _pressure(tmp_path, f"{ORG}::ahab", demand_ts=NOW - 5, use_ts=NOW - 5000)
    rep = _tick(prov, state, pool=pool, pressure_dir=d, factory=factory, now=NOW + 1)
    assert any(a.startswith("pause:standalone") for a in rep["actions"])
    assert pod.manager.pauses == 1 and pool.standalone == {}


def test_metering_writes_one_row_per_tick_and_splits_an_org_pod(monkeypatch, tmp_path):
    prov = _Prov(keys={ORG, f"{ORG}::a", f"{ORG}::b"})
    _rows(monkeypatch, {ORG: [_row("a", pod="org"), _row("b", pod="org")]})
    _budget(monkeypatch, 12.0)
    written: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(
        gus, "record", lambda org, rows, client=None: written.append((org, rows)) or True
    )
    pool = _Pool()
    state = pc.PlacementState()
    factory = lambda k, key, g: _Mgr(k, key, g, existing="pod-org")  # noqa: E731
    d = _pressure(tmp_path, f"{ORG}::a", demand_ts=NOW - 5, use_ts=NOW - 5)
    _tick(prov, state, pool=pool, pressure_dir=d, factory=factory, now=NOW)
    assert written == [], "first tick only stamps last_bill"
    _tick(prov, state, pool=pool, pressure_dir=d, factory=factory, now=NOW + 60)
    org, rows = written[0]
    assert org == ORG and {r["persona"] for r in rows} == {"a", "b"}
    assert all(r["pod_kind"] == "org" and r["pod_id"] == "pod-org" for r in rows)
    assert pytest.approx(sum(r["seconds"] for r in rows)) == 60.0
    assert pytest.approx(sum(r["usd"] for r in rows), rel=1e-6) == 60 / 3600 * 0.8
    assert pc.summary(state)["spent_today"][ORG] == pytest.approx(60 / 3600 * 0.8, abs=1e-4)


def test_spent_budget_sleeps_the_org_pods_until_rollover(monkeypatch, tmp_path, caplog):
    prov = _Prov(keys={ORG, f"{ORG}::ahab"})
    _rows(monkeypatch, {ORG: [_row("ahab", pod="standalone")]})
    _budget(monkeypatch, 1.0)
    monkeypatch.setattr(gus, "usd_today_for", lambda org, client=None: 1.5)
    pool = _Pool()
    state = pc.PlacementState()
    factory = lambda k, key, g: _Mgr(k, key, g, existing="pod-x")  # noqa: E731
    d = _pressure(tmp_path, f"{ORG}::ahab", demand_ts=NOW - 5, use_ts=NOW - 5)
    with caplog.at_level(logging.WARNING):
        rep = _tick(prov, state, pool=pool, pressure_dir=d, factory=factory)
    assert any(a.startswith("pause:standalone") for a in rep["actions"])
    assert pool.fallback[f"{ORG}::ahab"]["reason"] == "budget_spent"
    assert ORG in pc.summary(state)["budget_paused"]
    assert any("budget spent" in r.message for r in caplog.records)
    # Budget raised: the org is released and demand can wake the pod again.
    _budget(monkeypatch, 5.0)
    rep = _tick(prov, state, pool=pool, pressure_dir=d, factory=factory)
    assert ORG not in state.budget_paused
    assert any(a.startswith("wake") for a in rep["actions"])


def test_platform_cap_bounds_dedicated_pods(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_MAX_STANDALONE_PODS", "1")
    prov = _Prov(keys={ORG, f"{ORG}::a", f"{ORG}::b"})
    _rows(monkeypatch, {ORG: [_row("a", pod="standalone"), _row("b", pod="standalone")]})
    _budget(monkeypatch, 12.0)
    pool = _Pool()
    state = pc.PlacementState()
    _pressure(tmp_path, f"{ORG}::a", demand_ts=NOW - 5)
    d = _pressure(tmp_path, f"{ORG}::b", demand_ts=NOW - 5)
    rep = _tick(prov, state, pool=pool, pressure_dir=d)
    assert sum(a.startswith("wake") for a in rep["actions"]) == 1
    reasons = {k: v["reason"] for k, v in pool.fallback.items()}
    assert "platform_cap" in reasons.values()


def test_unplaced_pod_is_paused_and_forgotten(monkeypatch, tmp_path):
    prov = _Prov(keys={ORG, f"{ORG}::ahab"})
    _rows(monkeypatch, {ORG: [_row("ahab", pod="standalone")]})
    _budget(monkeypatch, 12.0)
    pool = _Pool()
    state = pc.PlacementState()
    factory = lambda k, key, g: _Mgr(k, key, g, existing="pod-x")  # noqa: E731
    d = _pressure(tmp_path, f"{ORG}::ahab", demand_ts=NOW - 5, use_ts=NOW - 5)
    _tick(prov, state, pool=pool, pressure_dir=d, factory=factory)
    mgr = state.pods[f"{ORG}::ahab"].manager
    # Row switches to the pool: the pod loses its consumer; after the orphan grace
    # it is paused and dropped.
    _rows(monkeypatch, {ORG: [_row("ahab", pod="pool")]})
    state.desired_read_at = 0.0
    _tick(prov, state, pool=pool, pressure_dir=d, factory=factory, now=NOW + 1)
    assert f"{ORG}::ahab" in state.pods, "orphan grace"
    _tick(prov, state, pool=pool, pressure_dir=d, factory=factory, now=NOW + pc.ORPHAN_GRACE_S + 2)
    assert f"{ORG}::ahab" not in state.pods and mgr.pauses == 1


def test_pause_org_sleeps_only_that_orgs_pods(monkeypatch, tmp_path):
    state = pc.PlacementState()
    m1 = _Mgr("standalone", f"{ORG}::a", None, existing="p1")
    m2 = _Mgr("standalone", "org-2::b", None, existing="p2")
    state.pods[f"{ORG}::a"] = pc.DedicatedPod(
        key=f"{ORG}::a", kind="standalone", org=ORG, manager=m1
    )
    state.pods["org-2::b"] = pc.DedicatedPod(
        key="org-2::b", kind="standalone", org="org-2", manager=m2
    )
    assert asyncio.run(pc.pause_org(state, ORG)) == 1
    assert m1.pauses == 1 and m2.pauses == 0
    assert state.pods[f"{ORG}::a"].fallback_reason == "slept"


def test_without_a_pool_pods_are_requested_but_processes_still_place(monkeypatch, caplog):
    prov = _Prov(keys={ORG})
    _rows(monkeypatch, {ORG: [_row("ahab", pod="standalone")]})
    with caplog.at_level(logging.WARNING):
        rep = _tick(prov, pc.PlacementState(), pool=None)
    assert prov.ensured == [f"{ORG}::ahab"] and rep["pods"] == 0
    assert any("need the pod pool" in r.message for r in caplog.records)


# ── publication and the pool reconciler ──────────────────────────────────────


def test_pool_reconciler_leaves_dedicated_consumers_off_the_pool(tmp_path, monkeypatch):
    import brain.pod_budget as pb
    from tests.test_gateway_pod_reconcile import _FakePool, _FakeProv, _pod

    monkeypatch.setattr(pb, "_LEDGER", tmp_path / ".pod_budget.json")
    monkeypatch.setattr(pb, "budget_usd", lambda: 10.0)
    monkeypatch.setattr(pb, "rate_per_hr", lambda: 0.5)
    monkeypatch.setattr(pv, "POD_DEMAND_FILE", tmp_path / ".pod_demand")
    monkeypatch.setattr(pv, "POD_USE_FILE", tmp_path / ".pod_used")
    pool = _FakePool(pods=[_pod("p0", 0)])
    pool.standalone = {f"{ORG}::ahab": {"pod_id": "s1", "host": "h", "state": "ready"}}
    prov = _FakeProv(keys=[ORG, f"{ORG}::ahab"], full=2)
    d = _pressure(tmp_path, f"{ORG}::ahab", demand_ts=NOW - 5, use_ts=NOW - 5)
    state = rc.ReconcileState(last_tick=NOW - 60)
    pool._now = NOW
    report = asyncio.run(rc.reconcile_tick(pool, prov, state, now=NOW, pressure_dir=d))
    assert report["consumers"] == 1
    assert set(pool.assignments) == {ORG}
    assert report["demand_age_s"] is None, "a dedicated consumer's demand never reaches the pool"


def test_pool_file_carries_standalone_fallback_and_dedicated_pods(tmp_path, monkeypatch):
    from brain.pod_pool import PodSample
    from brain.runpod_pool import RunPodPool
    from tests.test_gateway_pod_reconcile import _SlotMgr

    monkeypatch.setattr(pv, "HOST_SYNC_FILE", tmp_path / ".runpod_host")
    pool = RunPodPool(
        "k",
        PoolConfig(max_pods=2),
        tenants_dir=tmp_path,
        manager_factory=lambda i, name: _SlotMgr(
            i, name, *(("p0", "ready") if i == 0 else (None, "off"))
        ),
    )
    pool.standalone = {
        f"{ORG}::ahab": {
            "pod_id": "s1",
            "host": "https://s1",
            "state": "ready",
            "kind": "standalone",
        }
    }
    pool.fallback = {
        f"{ORG}::b": {"kind": "org", "reason": "budget_spent", "state": "off", "pod_id": None}
    }
    pool.extra_pods = [
        PodSample(pod_id="s1", index=1000, state="ready", host="https://s1", kind="standalone")
    ]
    body = pool.publish(NOW)
    data = json.loads((tmp_path / ".runpod_pool.json").read_text())
    assert data["standalone"] == body["standalone"] == pool.standalone
    assert data["fallback"][f"{ORG}::b"]["reason"] == "budget_spent"
    assert [p["kind"] for p in data["pods"]] == ["pool", "standalone"]


def test_live_view_reports_fallback_pool(tmp_path, monkeypatch):
    from brain import placement_client as plc
    from brain.pod_pool import PodSample, pool_file_body

    pool_file = tmp_path / ".runpod_pool.json"
    body = pool_file_body(
        [PodSample(pod_id="p0", index=0, state="ready", host="https://p0", kind="pool")],
        {f"{ORG}::ahab": "p0"},
        {},
        now=NOW,
        fallback={f"{ORG}::ahab": {"kind": "standalone", "reason": "budget_spent"}},
    )
    pool_file.write_text(json.dumps(body))
    monkeypatch.setenv("BRAIN_RUNPOD_POOL_FILE", str(pool_file))
    monkeypatch.setenv("BRAIN_PROC_KEY", f"{ORG}::ahab")
    monkeypatch.setenv("BRAIN_PLACEMENT_FILE", str(tmp_path / "placement.json"))
    (tmp_path / "placement.json").write_text(json.dumps({"promoted": ["ahab"], "ts": NOW}))
    plc._cached_at = 0.0
    view = plc.live_view("ahab")
    assert view["instance"] == "dedicated"
    assert view["pod_state"] == "fallback_pool" and view["host_kind"] == "standalone"
    assert view["reason"] == "budget_spent"


# ── provisioner pinning and the demand touch ──────────────────────────────────


def test_set_pinned_and_reaper_skips_pinned_instances(monkeypatch):
    from tests.test_provisioner_persona import _inject_live

    prov = pv.Provisioner()
    _inject_live(prov, f"{ORG}::ahab")
    _inject_live(prov, "org-2")
    assert prov.set_pinned(ORG, "ahab", True)
    assert prov.status(ORG, "ahab")["pinned"] is True
    assert not prov.set_pinned("org-9", "nobody", True)
    for p in prov._procs.values():
        p.booting = False
        p.last_active = 0.0
    monkeypatch.setattr(pv, "IDLE_TIMEOUT_S", 10)
    stopped: list[str] = []

    async def _stop(key):
        stopped.append(key)
        prov._procs.pop(key, None)

    prov._stop_key = _stop
    calls = {"n": 0}

    async def _sleep(_s):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(pv.asyncio, "sleep", _sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(prov._reaper_loop())
    assert stopped == ["org-2"], "the pinned dedicated instance survives the reaper"


def test_dedicated_consumer_never_touches_the_pool_demand_files(tmp_path, monkeypatch):
    from brain import runpod_manager as rm

    monkeypatch.setattr(pv, "POD_DEMAND_FILE", tmp_path / ".pod_demand")
    monkeypatch.setattr(pv, "POD_USE_FILE", tmp_path / ".pod_used")
    monkeypatch.setattr(pv, "_last_pod_demand_write", 0.0)
    monkeypatch.setattr(pv, "_last_pod_use_write", 0.0)
    monkeypatch.setattr(rm, "_HOST_SOURCE", "standalone")
    pv.note_pod_demand()
    pv.note_pod_use()
    assert not (tmp_path / ".pod_demand").exists() and not (tmp_path / ".pod_used").exists()
    monkeypatch.setattr(rm, "_HOST_SOURCE", "pool")
    monkeypatch.setattr(pv, "_last_pod_demand_write", 0.0)
    pv.note_pod_demand()
    assert (tmp_path / ".pod_demand").exists()


def test_consumer_host_records_where_the_host_came_from(tmp_path, monkeypatch):
    from brain import runpod_manager as rm
    from brain.pod_pool import PodSample, pool_file_body

    pool_file = tmp_path / ".runpod_pool.json"
    body = pool_file_body(
        [PodSample(pod_id="p0", index=0, state="ready", host="https://p0", kind="pool")],
        {"org-2": "p0"},
        {f"{ORG}::ahab": {"pod_id": "s1", "host": "https://s1", "state": "ready"}},
        now=NOW,
    )
    pool_file.write_text(json.dumps(body))
    monkeypatch.setenv("BRAIN_PROC_KEY", f"{ORG}::ahab")
    assert rm._consumer_host(None, pool_file) == "https://s1"
    assert rm.host_source() == "standalone"
    monkeypatch.setenv("BRAIN_PROC_KEY", "org-2")
    assert rm._consumer_host(None, pool_file) == "https://p0"
    assert rm.host_source() == "pool"
    monkeypatch.setenv("BRAIN_PROC_KEY", "org-3")
    rm._consumer_host(None, pool_file)
    assert rm.host_source() == "legacy"


def test_manager_volume_override_and_pod_names(monkeypatch):
    from brain.runpod_manager import RunPodManager

    monkeypatch.setenv("RUNPOD_NETWORK_VOLUME_ID", "vol-pool")
    m = RunPodManager("k", pod_name="x", publish_host=False, network_volume_id="vol-sa")
    assert m._network_volume_id() == "vol-sa"
    e = RunPodManager("k", pod_name="y", publish_host=False, ephemeral_disk=True)
    assert e._network_volume_id() == ""
    assert RunPodManager("k", pod_name="z")._network_volume_id() == "vol-pool"
    assert pc.pod_name_for("standalone", f"{ORG}::ahab").startswith("ollama-sa-")
    assert pc.pod_name_for("org", ORG).startswith("ollama-org-")
    assert pc.pod_name_for("org", ORG) == pc.pod_name_for("org", ORG), "deterministic"


def test_default_manager_factory_uses_the_standalone_volume_or_ephemeral(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "k")
    monkeypatch.delenv("RUNPOD_STANDALONE_VOLUME_ID", raising=False)
    m = pc.default_manager_factory("standalone", f"{ORG}::ahab", "NVIDIA L40")
    assert m._ephemeral_disk and m._gpu_type_id == "NVIDIA L40" and not m._publish_host
    monkeypatch.setenv("RUNPOD_STANDALONE_VOLUME_ID", "vol-sa")
    m = pc.default_manager_factory("org", ORG, None)
    assert not m._ephemeral_disk and m._network_volume_id() == "vol-sa"


# ── readers ───────────────────────────────────────────────────────────────────


class _Res:
    def __init__(self, data):
        self.data = data


class _AllClient:
    def __init__(self, rows, fail=False):
        self.rows, self.fail = rows, fail
        self.rpcs: list[tuple[str, dict]] = []

    def table(self, name):
        assert name == "persona_placement"
        return self

    def select(self, *a):
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        if self.fail:
            raise RuntimeError("relation persona_placement does not exist")
        return _Res([dict(r) for r in self.rows])

    def rpc(self, name, args):
        self.rpcs.append((name, args))
        return self


def test_list_all_groups_rows_by_org_and_none_on_failure(monkeypatch):
    monkeypatch.setattr(pp, "_warned_missing", False)
    rows = [
        {"org_id": "org-2", "persona": "b", "mode": "dedicated", "pod": "org"},
        {"org_id": ORG, "persona": "a", "mode": "dedicated", "pod": "standalone"},
    ]
    out = pp.list_all(client=_AllClient(rows))
    assert set(out) == {ORG, "org-2"}
    assert out[ORG][0]["persona"] == "a" and out[ORG][0]["placed"] is True
    assert pp.list_all(client=_AllClient(rows, fail=True)) is None


def test_usd_today_for_sums_the_org_rpc(monkeypatch):
    monkeypatch.setattr(gus, "usd_today_for", _REAL_USD_TODAY_FOR)
    c = _AllClient([])
    c.execute = lambda: _Res([{"usd": 0.25}, {"usd": 0.5}])  # type: ignore[assignment]
    assert gus.usd_today_for(ORG, client=c) == 0.75
    name, args = c.rpcs[0]
    assert name == "gpu_usage_by_day" and args["p_org_id"] == ORG
    assert gus.usd_today_for("", client=c) == 0.0


# ── gateway surface ───────────────────────────────────────────────────────────


def test_fleet_placement_route_is_superadmin_only(monkeypatch):
    from brain.gateway import server as gw
    from tests.test_fleet_superadmin import ADMIN, MEMBER, _auth_patched, _get

    monkeypatch.delenv("BRAIN_ADMIN_EMAILS", raising=False)
    prov = _Prov(keys={ORG, f"{ORG}::ahab"})
    state = pc.PlacementState()
    state.pods[f"{ORG}::ahab"] = pc.DedicatedPod(
        key=f"{ORG}::ahab",
        kind="standalone",
        org=ORG,
        manager=_Mgr("standalone", f"{ORG}::ahab", None, existing="p1"),
    )
    monkeypatch.setattr(gw, "placement_holder", [state])
    with _auth_patched(MEMBER):
        assert asyncio.run(_get(prov, "/__fleet/placement")).status_code == 403
    with _auth_patched(ADMIN):
        r = asyncio.run(_get(prov, "/__fleet/placement"))
    assert r.status_code == 200
    body = r.json()
    assert body["instances"] == [f"{ORG}::ahab"]
    assert body["pods"][0]["kind"] == "standalone" and body["pods"][0]["serving"] is True
