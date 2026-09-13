"""Gateway pool reconciler (brain/gateway/pod_reconcile) against a fake pool.

Same shape as tests/test_gateway_api_routing.py: every collaborator is a small fake,
nothing touches RunPod, and the clock is passed in. The contract under test:
pod 0 keeps the demand-wake / use-hold / budget / churn-cooldown behaviour of the old
single-pod loop; pods above 0 scale on pressure; a dead pod's consumers move; every
tick publishes.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import brain.pod_budget as pb
import brain.provisioner as pv
from brain.gateway import pod_reconcile as rc
from brain.pod_pool import PodSample, PoolConfig, ScaleHistory, pool_file_body

NOW = 2_000_000.0


class _FakeProv:
    def __init__(self, keys=(), full=1):
        self.keys = list(keys)
        self.full = full

    def keys_for_all(self):
        return list(self.keys)

    def full_count(self):
        return self.full


class _FakePool:
    def __init__(self, cfg=None, pods=None, assignments=None):
        self.cfg = cfg or PoolConfig(max_pods=3, grace_s=600.0, drain_s=60.0, cooldown_s=600.0)
        self._pods: list[PodSample] = list(pods or [])
        self.assignments: dict[str, str] = dict(assignments or {})
        self.history = ScaleHistory()
        self.cooldown_until = 0.0
        self.dead: set[str] = set()
        self.calls: list[str] = []
        self._drain: dict[str, float] = {}
        self._cost_per_hr = 0.5
        self.published: dict | None = None
        self._now = NOW

    @property
    def _pod_id(self):
        return next((p.pod_id for p in self._pods if p.index == 0), None)

    def pods(self, now=None):
        return self._pods

    def index_of(self, pid):
        return next((p.index for p in self._pods if p.pod_id == pid), None)

    def is_draining(self, pid):
        return pid in self._drain

    def draining_due(self, now=None):
        ts = self._now if now is None else now
        return [pid for pid, since in self._drain.items() if ts - since >= self.cfg.drain_s]

    async def probe_all(self):
        out = {p.pod_id: p.pod_id not in self.dead for p in self._pods}
        for pid in list(self.dead):
            self._pods = [p for p in self._pods if p.pod_id != pid]
            self.assignments = {k: v for k, v in self.assignments.items() if v != pid}
        return out

    async def ensure_min(self):
        self.calls.append("ensure_min")
        if self._pod_id is None:
            self._pods.append(_pod("p0", 0))
        return True

    async def scale_up(self):
        self.calls.append("scale_up")
        self.cooldown_until = self._now + self.cfg.cooldown_s
        idx = max([p.index for p in self._pods], default=-1) + 1
        pid = f"p{idx}"
        self._pods.append(_pod(pid, idx))
        return pid

    def drain(self, pid):
        self.calls.append(f"drain:{pid}")
        self._drain[pid] = self._now
        for p in self._pods:
            if p.pod_id == pid:
                p.state = "draining"

    async def terminate(self, pid):
        self.calls.append(f"terminate:{pid}")
        self._drain.pop(pid, None)
        self._pods = [p for p in self._pods if p.pod_id != pid]
        self.assignments = {k: v for k, v in self.assignments.items() if v != pid}

    async def pause(self):
        self.calls.append("pause")
        self._pods = []
        self.assignments = {}

    def publish(self, now=None):
        self.calls.append("publish")
        self.published = pool_file_body(self._pods, self.assignments, {}, now=now or self._now)
        return self.published


def _pod(pid, index, state="ready"):
    return PodSample(
        pod_id=pid, index=index, state=state, host=f"https://{pid}-11434.proxy.runpod.net"
    )


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "_LEDGER", tmp_path / ".pod_budget.json")
    monkeypatch.setattr(pb, "_rate_per_hr", None)
    monkeypatch.setattr(pb, "budget_usd", lambda: 10.0)
    monkeypatch.setattr(pb, "rate_per_hr", lambda: 0.5)
    monkeypatch.setattr(pv, "POD_DEMAND_FILE", tmp_path / ".pod_demand")
    monkeypatch.setattr(pv, "POD_USE_FILE", tmp_path / ".pod_used")
    (tmp_path / "pressure").mkdir()


def _pressure(tmp_path, key, **body):
    import time as _time

    d = tmp_path / "pressure"
    # `ts` must be fresh in WALL-CLOCK terms for read_all's staleness check; the tick's
    # `now` is only used for ages, so stamp demand/use relative to it.
    b = {
        "proc_key": key,
        "ts": _time.time(),
        "calls_1m": 0,
        "busy_s_1m": 0.0,
        "wait_p95_s": 0.0,
        "fail_1m": 0,
    }
    b.update(body)
    (d / f"{key}.json").write_text(json.dumps(b))
    return d


def _tick(pool, prov, state=None, now=NOW, pressure_dir=None):
    state = state or rc.ReconcileState(last_tick=now - 60)
    pool._now = now
    return asyncio.run(
        rc.reconcile_tick(pool, prov, state, now=now, pressure_dir=pressure_dir)
    ), state


# ── pod 0 keeps the single-pod contract ─────────────────────────────────────


def test_fresh_demand_wakes_pod_zero_and_publishes(tmp_path):
    pdir = _pressure(tmp_path, "org-a", demand_ts=NOW - 10)
    pool = _FakePool()
    report, _ = _tick(pool, _FakeProv(["org-a"]), pressure_dir=pdir)
    assert "ensure_min" in pool.calls
    assert pool.calls[-1] == "publish"
    assert report["demand_age_s"] == pytest.approx(10.0)
    assert pool.published is not None and pool.published["pods"][0]["pod_id"] == "p0"


def test_no_demand_leaves_a_cold_pool_cold(tmp_path):
    pool = _FakePool()
    report, _ = _tick(pool, _FakeProv(["org-a"]), pressure_dir=tmp_path / "pressure")
    assert "ensure_min" not in pool.calls and "pause" not in pool.calls
    assert report["decision"] == "none"


def test_legacy_demand_file_still_wakes_the_pool(tmp_path):
    """A brain that predates the pressure file only touches .pod_demand — it must
    still be able to wake pod 0."""
    pv.note_pod_demand()
    pool = _FakePool()
    _tick(pool, _FakeProv(["org-a"]), pressure_dir=tmp_path / "pressure")
    assert "ensure_min" in pool.calls


def test_lite_only_host_never_wakes_the_pool(tmp_path):
    pdir = _pressure(tmp_path, "org-a", demand_ts=NOW - 10)
    pool = _FakePool()
    _tick(pool, _FakeProv(["org-a"], full=0), pressure_dir=pdir)
    assert "ensure_min" not in pool.calls


def test_pod_zero_producing_nothing_is_slept_after_grace_and_arms_cooldown(tmp_path):
    pdir = _pressure(tmp_path, "org-a", demand_ts=NOW - 5)  # still asking, never producing
    pool = _FakePool(pods=[_pod("p0", 0)])
    state = rc.ReconcileState(last_tick=NOW - 60, pod0_up_since=NOW - 2000, idle_since=NOW - 700)
    report, _ = _tick(pool, _FakeProv(["org-a"]), state, pressure_dir=pdir)
    assert "pause" in pool.calls and "ensure_min" not in pool.calls
    assert pb.cooldown_remaining_s() > 0, "an unproductive session arms the churn guard"
    assert report["decision"] is None, "no scaling on the tick that slept everything"


def test_productive_pool_is_held(tmp_path):
    pdir = _pressure(tmp_path, "org-a", demand_ts=NOW - 5, use_ts=NOW - 30)
    pool = _FakePool(pods=[_pod("p0", 0)])
    state = rc.ReconcileState(last_tick=NOW - 60, pod0_up_since=NOW - 2000, idle_since=NOW - 700)
    _tick(pool, _FakeProv(["org-a"]), state, pressure_dir=pdir)
    assert "pause" not in pool.calls and "ensure_min" in pool.calls
    assert state.idle_since is None


def test_over_budget_sleeps_the_whole_pool_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "exhausted", lambda: True)
    pdir = _pressure(tmp_path, "org-a", demand_ts=NOW - 5, use_ts=NOW - 5, busy_s_1m=120)
    pool = _FakePool(pods=[_pod("p0", 0), _pod("p1", 1)], assignments={"org-a": "p0"})
    pool.history.hot_since = NOW - 1000
    report, _ = _tick(pool, _FakeProv(["org-a"]), pressure_dir=pdir)
    assert "pause" in pool.calls
    assert "scale_up" not in pool.calls
    assert report["over_budget"] is True


# ── billing ──────────────────────────────────────────────────────────────────


def test_uptime_is_billed_once_per_held_pod(tmp_path):
    pdir = _pressure(tmp_path, "org-a", demand_ts=NOW - 5, use_ts=NOW - 5)
    pool = _FakePool(pods=[_pod("p0", 0), _pod("p1", 1)])
    _tick(pool, _FakeProv(["org-a"]), rc.ReconcileState(last_tick=NOW - 60), pressure_dir=pdir)
    assert pb.spent_seconds() == pytest.approx(120.0), "two pods × 60 s"


def test_nothing_held_bills_nothing(tmp_path):
    _tick(_FakePool(), _FakeProv([]), pressure_dir=tmp_path / "pressure")
    assert pb.spent_seconds() == 0


# ── assignment + failover ────────────────────────────────────────────────────


def test_new_consumers_are_assigned_least_loaded_and_existing_ones_stick(tmp_path):
    pdir = _pressure(tmp_path, "a", use_ts=NOW - 5)
    pool = _FakePool(pods=[_pod("p0", 0), _pod("p1", 1)], assignments={"a": "p1"})
    _tick(pool, _FakeProv(["a", "b", "c"]), pressure_dir=pdir)
    assert pool.assignments["a"] == "p1", "sticky"
    assert pool.assignments["b"] == "p0", "least loaded"
    assert pool.assignments["c"] == "p0", "1 vs 1 → tie → the lowest slot"
    assert pool.published["assignments"] == pool.assignments


def test_consumers_of_a_dead_pod_are_reassigned_and_republished(tmp_path):
    pdir = _pressure(tmp_path, "a", use_ts=NOW - 5)
    pool = _FakePool(pods=[_pod("p0", 0), _pod("p1", 1)], assignments={"a": "p1", "b": "p0"})
    pool.dead.add("p1")
    report, _ = _tick(pool, _FakeProv(["a", "b"]), pressure_dir=pdir)
    assert "released_dead:p1" in report["actions"]
    assert pool.assignments == {"a": "p0", "b": "p0"}
    assert [p["pod_id"] for p in pool.published["pods"]] == ["p0"]


def test_reaped_processes_drop_out_of_the_assignment_table(tmp_path):
    pool = _FakePool(pods=[_pod("p0", 0)], assignments={"gone": "p0", "a": "p0"})
    _pressure(tmp_path, "a", use_ts=NOW - 5)
    _tick(pool, _FakeProv(["a"]), pressure_dir=tmp_path / "pressure")
    assert "gone" not in pool.assignments


# ── scaling above pod 0 ──────────────────────────────────────────────────────


def test_sustained_pressure_scales_up(tmp_path):
    # both consumers on p0, each burning 60 busy-seconds a minute → util 1.0 vs parallel 2
    _pressure(tmp_path, "a", use_ts=NOW - 5, busy_s_1m=60.0)
    pdir = _pressure(tmp_path, "b", use_ts=NOW - 5, busy_s_1m=60.0)
    pool = _FakePool(pods=[_pod("p0", 0)], assignments={"a": "p0", "b": "p0"})
    pool.history.hot_since = NOW - 1000  # already hot for longer than up_after_s
    report, _ = _tick(pool, _FakeProv(["a", "b"]), pressure_dir=pdir)
    assert report["decision"] == "up"
    assert "scale_up" in pool.calls
    assert pool.cooldown_until > NOW


def test_hot_but_not_yet_sustained_holds(tmp_path):
    _pressure(tmp_path, "a", use_ts=NOW - 5, busy_s_1m=60.0)
    pdir = _pressure(tmp_path, "b", use_ts=NOW - 5, busy_s_1m=60.0)
    pool = _FakePool(pods=[_pod("p0", 0)], assignments={"a": "p0", "b": "p0"})
    report, _ = _tick(pool, _FakeProv(["a", "b"]), pressure_dir=pdir)
    assert report["decision"] == "hold" and "scale_up" not in pool.calls
    assert pool.history.hot_since == NOW


def test_idle_pod_above_zero_is_drained_then_terminated(tmp_path):
    pdir = _pressure(tmp_path, "a", use_ts=NOW - 5, busy_s_1m=6.0)
    pool = _FakePool(pods=[_pod("p0", 0), _pod("p1", 1)], assignments={"a": "p1"})
    pool.history.cold_since["p1"] = NOW - 5000
    report, state = _tick(pool, _FakeProv(["a"]), pressure_dir=pdir)
    assert report["decision"] == "down:p1"
    assert "drain:p1" in pool.calls
    assert pool.assignments == {"a": "p0"}, "its consumer moved before the drain"
    assert pool.published["pods"][1]["state"] == "draining"
    # next tick, before drain_s: still up; after drain_s: released
    _tick(pool, _FakeProv(["a"]), state, now=NOW + 30, pressure_dir=pdir)
    assert not any(c.startswith("terminate") for c in pool.calls)
    _tick(pool, _FakeProv(["a"]), state, now=NOW + 60, pressure_dir=pdir)
    assert "terminate:p1" in pool.calls
    assert [p["pod_id"] for p in pool.published["pods"]] == ["p0"]


def test_draining_pod_is_not_drained_twice(tmp_path):
    pdir = _pressure(tmp_path, "a", use_ts=NOW - 5)
    pool = _FakePool(pods=[_pod("p0", 0), _pod("p1", 1)])
    pool.history.cold_since["p1"] = NOW - 5000
    _tick(pool, _FakeProv(["a"]), pressure_dir=pdir)
    assert pool.calls.count("drain:p1") == 1
    _tick(pool, _FakeProv(["a"]), now=NOW + 10, pressure_dir=pdir)
    assert pool.calls.count("drain:p1") == 1


def test_no_full_tier_brains_drains_pods_above_zero_then_sleeps_pod_zero(tmp_path):
    pdir = _pressure(tmp_path, "a", use_ts=NOW - 5)
    pool = _FakePool(pods=[_pod("p0", 0), _pod("p1", 1)])
    report, state = _tick(pool, _FakeProv(["a"], full=0), pressure_dir=pdir)
    assert report["decision"] == "down:p1" and "drain:p1" in pool.calls
    assert "pause" not in pool.calls, "pod 0 waits out its idle grace"
    _tick(pool, _FakeProv(["a"], full=0), state, now=NOW + 700, pressure_dir=pdir)
    assert "pause" in pool.calls


def test_every_tick_publishes(tmp_path):
    pool = _FakePool()
    _tick(pool, _FakeProv([]), pressure_dir=tmp_path / "pressure")
    assert pool.calls == ["publish"]


# ── the gateway holds a RunPodPool where it held a RunPodManager ────────────────


class _SlotMgr:
    """The manager surface RunPodPool touches, with no RunPod behind it."""

    def __init__(self, index, name, pod_id=None, status="off"):
        self._pod_name = name
        self._pod_id = pod_id
        self._status = status if pod_id else "off"
        self._cost_per_hr = 0.45 if pod_id else None
        self.paused: list[str] = []

    def _pod_host(self, pid):
        return f"https://{pid}-11434.proxy.runpod.net"

    def published_host(self):
        return self._pod_host(self._pod_id) if self._pod_id else None

    def status(self):
        return {
            "state": self._status,
            "detail": "",
            "elapsed_s": 0.0,
            "running": bool(self._pod_id),
        }

    async def ensure_running(self):
        return True

    async def pause(self):
        if self._pod_id:
            self.paused.append(self._pod_id)
        self._pod_id, self._status = None, "off"

    async def _probe_alive(self, pid):
        return True

    def _cancel_watcher(self):
        pass

    def _set_status(self, state, detail=""):
        self._status = state

    async def discover_and_publish_host(self):
        return self.published_host()


def _real_pool(tmp_path, monkeypatch):
    from brain.runpod_pool import RunPodPool

    monkeypatch.setattr(pv, "HOST_SYNC_FILE", tmp_path / ".runpod_host")
    slots = {0: ("p0", "ready"), 1: ("p1", "warming")}
    pool = RunPodPool(
        "k",
        PoolConfig(max_pods=3),
        tenants_dir=tmp_path,
        manager_factory=lambda i, name: _SlotMgr(i, name, *slots.get(i, (None, "off"))),
    )
    pool.assignments = {"org-1": "p0", "org-2": "p1"}
    return pool


def _gateway(monkeypatch, pool, role="owner"):
    import brain.api.auth as api_auth
    from brain.gateway import server as gw

    monkeypatch.setattr(
        api_auth,
        "resolve_key_context",
        lambda _auth: {"org_id": "org-1", "partner_id": "p", "role": role},
    )

    class _Prov:
        def status(self, t, persona=None):
            # booting=True makes _do_sleep skip the brain HTTP call (as the routing
            # tests do); /v1/status only asserts the pod field here.
            return {"port": 0, "api_port": 1, "booting": True, "pid": 1}

        def is_running(self, t, persona=None):
            return False

        def keys_for(self, t):
            return [t]

        async def stop_user(self, t, persona=None):
            pass

        def full_count(self):
            return 0

        def live_count(self):
            return 0

        def touch(self, t, persona=None):
            pass

    return gw.build_gateway_app(_Prov(), [pool])


def test_v1_status_owner_pod_field_is_the_pool_summary(tmp_path, monkeypatch):
    import httpx

    pool = _real_pool(tmp_path, monkeypatch)
    app = _gateway(monkeypatch, pool)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get("/v1/status", headers={"authorization": "Bearer good"})

    d = asyncio.run(run()).json()
    pod = d["pod"]
    assert pod["state"] == "ready", "pod 0's boot phase still leads (the banner reads it)"
    assert pod["ready"] == 1 and pod["assignments"] == 2 and pod["max_pods"] == 3
    assert [(p["pod_id"], p["index"], p["state"]) for p in pod["pods"]] == [
        ("p0", 0, "ready"),
        ("p1", 1, "warming"),
    ]
    assert "pod_budget" in d


def test_v1_status_partner_never_sees_the_pool(tmp_path, monkeypatch):
    import httpx

    app = _gateway(monkeypatch, _real_pool(tmp_path, monkeypatch), role="partner")

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get("/v1/status", headers={"authorization": "Bearer partner"})

    d = asyncio.run(run()).json()
    assert "pod" not in d and "pod_budget" not in d


def test_v1_sleep_pauses_every_pool_pod(tmp_path, monkeypatch):
    """The sleep path calls runpod.pause() when no full brain remains — with the pool
    that must sleep EVERY held pod, largest slot first, and clear assignments."""
    import httpx

    pool = _real_pool(tmp_path, monkeypatch)
    app = _gateway(monkeypatch, pool)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/v1/sleep", headers={"authorization": "Bearer good"})
        for _ in range(10):
            await asyncio.sleep(0)
        return r

    r = asyncio.run(run())
    assert r.status_code == 200
    assert pool.managers[0].paused == ["p0"] and pool.managers[1].paused == ["p1"]
    assert pool.assignments == {}
    assert (tmp_path / ".runpod_host").read_text() == ""


def test_pod_pool_kill_switch(monkeypatch):
    from brain.gateway import server as gw

    monkeypatch.delenv("BRAIN_POD_POOL", raising=False)
    assert gw.pod_pool_enabled() is True, "on by default"
    for off in ("0", "false", "off", "no"):
        monkeypatch.setenv("BRAIN_POD_POOL", off)
        assert gw.pod_pool_enabled() is False
    monkeypatch.setenv("BRAIN_POD_POOL", "1")
    assert gw.pod_pool_enabled() is True


def test_gateway_startup_wires_the_pool_by_default():
    from pathlib import Path

    src = Path("brain/gateway/server.py").read_text(encoding="utf-8")
    assert "if pod_pool_enabled():" in src
    assert "RunPodPool()" in src and "_pool_reconciler(runpod)" in src
    assert "_pod_reconciler(runpod)" in src, "the single-pod loop stays as the kill-switch path"


# ── the ceiling is read each tick: a runtime edit lands without a restart ───


def test_reconciler_picks_up_a_runtime_budget_change_without_restart(tmp_path, monkeypatch):
    """The superadmin route writes `<tenants>/.pod_budget_config.json`; the next tick
    must enforce the new ceiling — no redeploy, no process restart, no cached value.
    The autouse fixture stubs budget_usd() to a constant, so restore the real resolver
    (runtime file > bundled settings) for this test."""
    monkeypatch.setattr(pb, "budget_usd", lambda: pb._settings_budget_usd())
    monkeypatch.setattr(pb, "_runtime_cache", None)
    pb.record_uptime(2 * 3600)  # $1.00 spent today at $0.50/hr
    pdir = _pressure(tmp_path, "org-a", demand_ts=NOW - 5, use_ts=NOW - 5)
    pool = _FakePool(pods=[_pod("p0", 0)])
    state = rc.ReconcileState(last_tick=NOW - 60, pod0_up_since=NOW - 2000)

    # No runtime file: the bundled default ($10) applies and the pod is held.
    report, _ = _tick(pool, _FakeProv(["org-a"]), state, pressure_dir=pdir)
    assert report["over_budget"] is False and "pause" not in pool.calls

    # A superadmin lowers the ceiling below today's spend: the next tick sleeps the pool.
    pb.set_runtime_budget_usd(0.5, {"email": "admin@x"})
    pool.calls.clear()
    report, _ = _tick(pool, _FakeProv(["org-a"]), state, pressure_dir=pdir)
    assert report["over_budget"] is True and "pause" in pool.calls

    # Raising it again (still without a restart) lets demand wake pod 0 once more.
    pb.set_runtime_budget_usd(20.0, {"email": "admin@x"})
    pool.calls.clear()
    report, _ = _tick(pool, _FakeProv(["org-a"]), state, pressure_dir=pdir)
    assert report["over_budget"] is False and "ensure_min" in pool.calls
