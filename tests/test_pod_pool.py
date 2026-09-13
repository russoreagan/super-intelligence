"""Pod pool — the pure scaling/assignment layer (brain/pod_pool) and the pool object
that owns one RunPodManager per slot (brain/runpod_pool).

Every decision here runs without a RunPod key, a gateway or a clock: the RunPod API is
never called (managers are fakes), and `now` is passed in.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import brain.pod_pool as pool
import brain.provisioner as pv
import brain.runpod_manager as rm
from brain.pod_pool import PodSample, PoolConfig, ScaleHistory
from brain.runpod_pool import RunPodPool

NOW = 1_000_000.0


def _pod(pid, index, state="ready", busy=0.0, wait=0.0, host=None):
    return PodSample(
        pod_id=pid,
        index=index,
        state=state,
        host=host or f"https://{pid}-11434.proxy.runpod.net",
        busy_frac=busy,
        wait_p95_s=wait,
    )


def _sample(ts=NOW, **kw):
    body = {"ts": ts, "calls_1m": 0, "busy_s_1m": 0.0, "wait_p95_s": 0.0, "fail_1m": 0}
    body.update(kw)
    return body


# ── aggregate_pressure ───────────────────────────────────────────────────────


def test_aggregate_folds_consumers_onto_their_pods():
    samples = {
        "org-a": _sample(busy_s_1m=30.0, wait_p95_s=2.0, calls_1m=4, demand_ts=NOW - 5),
        "org-b": _sample(busy_s_1m=30.0, wait_p95_s=9.0, calls_1m=6, use_ts=NOW - 3),
        "org-c": _sample(busy_s_1m=12.0, fail_1m=2),
        "org-x": _sample(busy_s_1m=60.0),  # no assignment → legacy host / pod 0
        "org-dead": _sample(ts=NOW - 3600, busy_s_1m=60.0),  # stale → ignored
    }
    assignments = {"org-a": "podA", "org-b": "podA", "org-c": "podB"}
    agg = pool.aggregate_pressure(samples, assignments, NOW, parallel=2)
    a, b = agg.pods["podA"], agg.pods["podB"]
    assert a.consumers == 2 and a.calls_1m == 10
    assert a.busy_frac == pytest.approx(60.0 / 120.0)  # Σ busy / (60 × parallel)
    assert a.wait_p95_s == 9.0, "the pod's wait is its worst consumer's"
    assert b.busy_frac == pytest.approx(0.1) and b.fail_1m == 2
    assert agg.unassigned == ["org-x"]
    assert agg.samples == 4, "the stale sample is not counted"
    assert agg.demand_age_s == pytest.approx(5.0)
    assert agg.use_age_s == pytest.approx(3.0)


def test_aggregate_caps_utilisation_at_one():
    agg = pool.aggregate_pressure({"o": _sample(busy_s_1m=500.0)}, {"o": "p"}, NOW, parallel=2)
    assert agg.pods["p"].busy_frac == 1.0


def test_apply_pressure_stamps_samples():
    pods = [_pod("podA", 0), _pod("podB", 1)]
    agg = pool.aggregate_pressure(
        {"o1": _sample(busy_s_1m=90.0, wait_p95_s=3.0)}, {"o1": "podB"}, NOW, parallel=2
    )
    pool.apply_pressure(pods, agg)
    assert pods[1].consumers == 1 and pods[1].busy_frac == pytest.approx(0.75)
    assert pods[0].consumers == 0 and pods[0].busy_frac == 0.0


# ── decide_scale ─────────────────────────────────────────────────────────────


def _decide(pods, history=None, cfg=None, now=NOW, **kw):
    kw.setdefault("over_budget", False)
    kw.setdefault("cooldown_until", 0.0)
    kw.setdefault("full_tier_brains", 1)
    return pool.decide_scale(pods, history or ScaleHistory(), cfg or PoolConfig(), now, **kw)


def test_no_pods_is_none_unless_min_pods_pins_one():
    assert _decide([]) == "none"  # pod 0's wake is should_hold_pod's business
    assert _decide([], cfg=PoolConfig(min_pods=1)) == "up"
    assert _decide([], cfg=PoolConfig(min_pods=1), full_tier_brains=0) == "none"
    assert _decide([], cfg=PoolConfig(min_pods=1), over_budget=True) == "none"


def test_hot_pod_holds_until_sustained_then_scales_up():
    h = ScaleHistory()
    pods = [_pod("p0", 0, busy=0.9)]
    assert _decide(pods, h, now=NOW) == "hold"
    assert h.hot_since == NOW
    assert _decide(pods, h, now=NOW + 100) == "hold", "a hot minute is not a trend"
    assert _decide(pods, h, now=NOW + 300) == "up"
    assert h.hot_since is None, "the dwell resets after a scale event"


def test_slot_wait_alone_is_a_scale_signal():
    h = ScaleHistory()
    pods = [_pod("p0", 0, busy=0.3, wait=9.0)]
    _decide(pods, h, now=NOW)
    assert _decide(pods, h, now=NOW + 300) == "up"


def test_cooling_pod_resets_the_hot_dwell():
    h = ScaleHistory()
    _decide([_pod("p0", 0, busy=0.9)], h, now=NOW)
    assert _decide([_pod("p0", 0, busy=0.3)], h, now=NOW + 100) == "hold"
    assert h.hot_since is None


def test_no_scale_up_at_max_in_cooldown_or_while_booting():
    h = ScaleHistory(hot_since=NOW - 1000)
    hot = [_pod("p0", 0, busy=0.9), _pod("p1", 1, busy=0.9)]
    assert _decide(hot, h, cfg=PoolConfig(max_pods=2)) == "hold", "at max_pods"
    assert _decide(hot, ScaleHistory(hot_since=NOW - 1000), cooldown_until=NOW + 1) == "hold"
    booting = [_pod("p0", 0, busy=0.9), _pod("p1", 1, state="resuming")]
    assert _decide(booting, ScaleHistory(hot_since=NOW - 1000)) == "hold", (
        "capacity still booting is capacity, not a reason for more"
    )
    assert _decide(hot, ScaleHistory(hot_since=NOW - 1000)) == "up"


def test_over_budget_sleeps_the_largest_index_first_and_never_scales_up():
    pods = [_pod("p0", 0, busy=0.9), _pod("p1", 1, busy=0.9), _pod("p2", 2, busy=0.9)]
    assert _decide(pods, ScaleHistory(hot_since=NOW - 1000), over_budget=True) == "down:p2"
    assert _decide([_pod("p0", 0)], over_budget=True) == "down:p0"


def test_no_full_tier_brain_brings_down_pods_above_zero_only():
    assert _decide([_pod("p0", 0), _pod("p1", 1)], full_tier_brains=0) == "down:p1"
    assert _decide([_pod("p0", 0)], full_tier_brains=0) == "none"  # pod 0: should_hold_pod


def test_idle_pod_above_zero_drains_after_the_dwell():
    h = ScaleHistory()
    pods = [_pod("p0", 0, busy=0.3), _pod("p1", 1, busy=0.1)]
    assert _decide(pods, h, now=NOW) == "hold"
    assert "p1" in h.cold_since
    assert _decide(pods, h, now=NOW + 500) == "hold"
    assert _decide(pods, h, now=NOW + 900) == "down:p1"
    assert "p1" not in h.cold_since


def test_pod_zero_is_never_removed_by_decide_scale():
    h = ScaleHistory()
    pods = [_pod("p0", 0, busy=0.0)]
    _decide(pods, h, now=NOW)
    assert _decide(pods, h, now=NOW + 5000) == "hold"


def test_scale_down_respects_min_pods():
    h = ScaleHistory()
    pods = [_pod("p0", 0, busy=0.0), _pod("p1", 1, busy=0.0)]
    cfg = PoolConfig(min_pods=2)
    _decide(pods, h, cfg=cfg, now=NOW)
    assert _decide(pods, h, cfg=cfg, now=NOW + 5000) == "hold"


def test_scale_down_is_refused_when_the_rest_would_go_hot():
    """Draining an idle pod onto a pod that is already near the up threshold would just
    trigger the next scale-up: oscillation. Keep it."""
    h = ScaleHistory()
    pods = [_pod("p0", 0, busy=0.7), _pod("p1", 1, busy=0.1)]
    _decide(pods, h, now=NOW)
    assert _decide(pods, h, now=NOW + 5000) == "hold"


def test_cold_dwell_is_forgotten_when_the_pod_warms_up():
    h = ScaleHistory()
    _decide([_pod("p0", 0), _pod("p1", 1, busy=0.1)], h, now=NOW)
    assert "p1" in h.cold_since
    _decide([_pod("p0", 0), _pod("p1", 1, busy=0.5)], h, now=NOW + 10)
    assert "p1" not in h.cold_since


def test_below_min_pods_scales_up_unless_cooling():
    pods = [_pod("p0", 0)]
    cfg = PoolConfig(min_pods=2)
    assert _decide(pods, cfg=cfg) == "up"
    assert _decide(pods, cfg=cfg, cooldown_until=NOW + 60) == "hold"


# ── assign / drain_plan ──────────────────────────────────────────────────────


def test_assign_is_least_loaded_with_ties_to_the_lowest_index():
    pods = [_pod("p0", 0), _pod("p1", 1)]
    out = pool.assign(["a", "b", "c"], pods, {})
    assert out["a"] == "p0" and out["b"] == "p1" and out["c"] == "p0"


def test_assign_is_sticky_while_the_pod_is_ready():
    pods = [_pod("p0", 0), _pod("p1", 1)]
    current = {"a": "p1", "b": "p1", "c": "p1"}
    out = pool.assign(["a", "b", "c", "d"], pods, current)
    assert out["a"] == out["b"] == out["c"] == "p1", "no mass reshuffle"
    assert out["d"] == "p0", "the newcomer goes to the emptier pod"


def test_assign_moves_consumers_off_a_pod_that_is_not_ready():
    pods = [_pod("p0", 0), _pod("p1", 1, state="draining")]
    out = pool.assign(["a", "b"], pods, {"a": "p1", "b": "p0"})
    assert out == {"a": "p0", "b": "p0"}


def test_assign_is_empty_when_nothing_is_ready():
    assert pool.assign(["a"], [_pod("p0", 0, state="resuming")], {"a": "p0"}) == {}
    assert pool.assign([], [_pod("p0", 0)], {}) == {}


def test_assign_drops_consumers_that_are_gone():
    out = pool.assign(["a"], [_pod("p0", 0)], {"a": "p0", "zombie": "p0"})
    assert "zombie" not in out


def test_drain_plan_spreads_movers_over_the_least_loaded_pods():
    pods = [_pod("p0", 0), _pod("p1", 1), _pod("p2", 2)]
    assignments = {"a": "p2", "b": "p2", "c": "p0", "d": "p0", "e": "p1"}
    plan = pool.drain_plan("p2", assignments, pods)
    assert set(plan) == {"a", "b"}
    assert plan["a"] == "p1", "p1 has one consumer, p0 has two"
    assert plan["b"] in ("p0", "p1")
    assert pool.drain_plan("p1", {"x": "p0"}, pods) == {}


def test_drain_plan_with_no_other_pod_sends_movers_to_legacy():
    plan = pool.drain_plan("p0", {"a": "p0"}, [_pod("p0", 0)])
    assert plan == {"a": None}


# ── pool file ↔ consumer round trip ──────────────────────────────────────────


def test_pool_file_body_has_the_documented_shape():
    body = pool.pool_file_body([_pod("p1", 1), _pod("p0", 0)], {"a": "p0"}, now=NOW)
    assert body["ts"] == NOW
    assert [p["index"] for p in body["pods"]] == [0, 1], "sorted by slot"
    assert body["pods"][1]["name"] == "ollama-brain-p2"
    assert body["assignments"] == {"a": "p0"}
    assert body["standalone"] == {}
    for k in ("pod_id", "host", "kind", "state", "gpu", "cost_per_hr", "parallel", "consumers"):
        assert k in body["pods"][0]


def test_resolve_pool_host_precedence():
    data = pool.pool_file_body(
        [_pod("p0", 0), _pod("p1", 1, state="warming"), _pod("p2", 2)],
        {"a": "p0", "b": "p1", "c": "gone"},
        {
            "s1": {"pod_id": "s", "host": "https://s-11434.proxy.runpod.net", "state": "ready"},
            "s2": {"pod_id": "t", "host": "https://t-11434.proxy.runpod.net", "state": "resuming"},
        },
        now=NOW,
    )
    legacy = "https://legacy-11434.proxy.runpod.net"
    assert pool.resolve_pool_host(data, "a", legacy).startswith("https://p0-")
    assert pool.resolve_pool_host(data, "b", legacy) == "", "assigned but pod not ready → off"
    assert pool.resolve_pool_host(data, "c", legacy) == "", "assigned but pod absent → off"
    assert pool.resolve_pool_host(data, "unknown", legacy) == legacy, "unmentioned → legacy"
    assert pool.resolve_pool_host(data, "unknown", None) is None
    assert pool.resolve_pool_host(None, "a", legacy) == legacy, "no pool file → legacy"
    assert pool.resolve_pool_host(data, "s1", legacy).startswith("https://s-")
    assert pool.resolve_pool_host(data, "s2", legacy) == "", "standalone not ready → off"


def test_consumer_host_reads_pool_file_then_legacy(tmp_path, monkeypatch):
    host_file = tmp_path / ".runpod_host"
    pool_file = tmp_path / ".runpod_pool.json"
    host_file.write_text("https://legacy-11434.proxy.runpod.net")
    pool_file.write_text(json.dumps(pool.pool_file_body([_pod("p1", 1)], {"org-1": "p1"}, now=NOW)))
    monkeypatch.setenv("BRAIN_PROC_KEY", "org-1")
    assert rm._consumer_host(host_file, pool_file).startswith("https://p1-")
    monkeypatch.setenv("BRAIN_PROC_KEY", "org-2")
    assert rm._consumer_host(host_file, pool_file) == "https://legacy-11434.proxy.runpod.net"
    assert rm._consumer_host(host_file, None) == "https://legacy-11434.proxy.runpod.net"
    pool_file.write_text("garbage")
    assert rm._consumer_host(host_file, pool_file) == "https://legacy-11434.proxy.runpod.net"
    host_file.unlink()
    assert rm._consumer_host(host_file, None) is None
    assert rm._consumer_host(None, None) is None


def test_consumer_refresh_loop_uses_the_pool_file(tmp_path, monkeypatch):
    """The running consumer's poll adopts its pool assignment, and flips to 'off' when
    its pod stops being ready — without a respawn."""
    import asyncio as _asyncio
    import contextlib

    import brain.settings as settings_mod

    captured: dict = {}
    monkeypatch.setattr(
        settings_mod.settings, "get", lambda k, d=None: captured.get(k, "" if d is None else d)
    )
    monkeypatch.setattr(settings_mod.settings, "update", captured.update)
    host_file = tmp_path / ".runpod_host"
    pool_file = tmp_path / ".runpod_pool.json"
    host_file.write_text("https://pod0-11434.proxy.runpod.net")
    pool_file.write_text(json.dumps(pool.pool_file_body([_pod("p1", 1)], {"o": "p1"}, now=NOW)))
    monkeypatch.setenv("BRAIN_RUNPOD_HOST_FILE", str(host_file))
    monkeypatch.setenv("BRAIN_RUNPOD_POOL_FILE", str(pool_file))
    monkeypatch.setenv("BRAIN_RUNPOD_HOST_POLL_S", "0.02")
    monkeypatch.setenv("BRAIN_PROC_KEY", "o")
    m = rm.RunPodManager(api_key="k")
    m._consumer = True

    async def _run():
        task = _asyncio.create_task(m._consumer_host_refresh())
        await _asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(_asyncio.CancelledError):
            await task

    _asyncio.run(_run())
    assert captured["runpod_host"].startswith("https://p1-"), "assigned pod, not the legacy host"
    assert captured["runpod_pod_ready"] == 1

    pool_file.write_text(
        json.dumps(pool.pool_file_body([_pod("p1", 1, state="draining")], {"o": "p1"}, now=NOW))
    )
    _asyncio.run(_run())
    assert captured["runpod_host"] == "off"


def test_pool_config_from_env(monkeypatch):
    monkeypatch.setenv("BRAIN_POOL_MIN_PODS", "1")
    monkeypatch.setenv("BRAIN_POOL_MAX_PODS", "5")
    monkeypatch.setenv("BRAIN_POOL_UP_UTIL", "0.6")
    monkeypatch.setenv("BRAIN_POOL_DOWN_UTIL", "0.1")
    monkeypatch.setenv("BRAIN_POOL_UP_AFTER_S", "120")
    monkeypatch.setenv("BRAIN_POOL_DOWN_AFTER_S", "600")
    monkeypatch.setenv("RUNPOD_NUM_PARALLEL", "4")
    monkeypatch.setenv("BRAIN_POOL_MAX_PODS", "5")
    cfg = PoolConfig.from_env()
    assert (cfg.min_pods, cfg.max_pods, cfg.parallel) == (1, 5, 4)
    assert (cfg.up_util, cfg.down_util) == (0.6, 0.1)
    assert (cfg.up_after_s, cfg.down_after_s) == (120.0, 600.0)
    monkeypatch.setenv("BRAIN_POOL_MAX_PODS", "junk")
    assert PoolConfig.from_env().max_pods == 3, "unparseable → default"


# ── RunPodPool with fake managers ────────────────────────────────────────────


class _FakeMgr:
    """The slice of RunPodManager the pool touches. Never calls RunPod."""

    def __init__(self, index, name, *, pod_id=None, status="off", alive=True, ensure_ok=True):
        self.index = index
        self._pod_name = name
        self._pod_id = pod_id
        self._status = status if pod_id else "off"
        self._cost_per_hr = 0.4 + 0.1 * index if pod_id else None
        self.alive = alive
        self.ensure_ok = ensure_ok
        self.paused: list[str] = []
        self.ensured = 0
        self.discovered = False

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
        self.ensured += 1
        if not self.ensure_ok:
            self._status = "failed"
            return False
        if not self._pod_id:
            self._pod_id = f"{self._pod_name}-id"
            self._cost_per_hr = 0.4 + 0.1 * self.index
        self._status = "ready"
        return True

    async def pause(self):
        if self._pod_id:
            self.paused.append(self._pod_id)
        self._pod_id = None
        self._status = "off"

    async def _probe_alive(self, pid):
        return self.alive

    def _cancel_watcher(self):
        pass

    def _set_status(self, state, detail=""):
        self._status = state

    async def discover_and_publish_host(self):
        self.discovered = True
        return self.published_host()


def _pool(tmp_path, monkeypatch, cfg=None, slots=None):
    """slots: {index: kwargs for _FakeMgr}"""
    slots = slots or {}
    monkeypatch.setattr(pv, "HOST_SYNC_FILE", tmp_path / ".runpod_host")

    def factory(i, name):
        return _FakeMgr(i, name, **slots.get(i, {}))

    return RunPodPool(
        "k", cfg or PoolConfig(max_pods=3), tenants_dir=tmp_path, manager_factory=factory
    )


def test_pool_builds_one_manager_per_slot_with_distinct_names(tmp_path, monkeypatch):
    p = _pool(tmp_path, monkeypatch)
    assert [m._pod_name for m in p.managers] == [
        "ollama-brain",
        "ollama-brain-p2",
        "ollama-brain-p3",
    ]


def test_default_factory_publishes_only_from_slot_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "HOST_SYNC_FILE", tmp_path / ".runpod_host")
    p = RunPodPool("k", PoolConfig(max_pods=2), tenants_dir=tmp_path)
    assert p.managers[0]._publish_host is True
    assert p.managers[1]._publish_host is False
    assert p.managers[1]._pod_name == "ollama-brain-p2"


def test_publish_writes_pool_file_and_legacy_host(tmp_path, monkeypatch):
    p = _pool(
        tmp_path,
        monkeypatch,
        slots={0: {"pod_id": "p0", "status": "ready"}, 1: {"pod_id": "p1", "status": "ready"}},
    )
    p.assignments = {"org-a": "p1", "org-b": "p0"}
    body = p.publish(now=NOW)
    on_disk = json.loads(p.pool_file.read_text())
    assert on_disk == body
    assert {x["pod_id"] for x in on_disk["pods"]} == {"p0", "p1"}
    assert on_disk["assignments"] == {"org-a": "p1", "org-b": "p0"}
    assert (tmp_path / ".runpod_host").read_text() == "https://p0-11434.proxy.runpod.net"
    # a consumer with an assignment follows it; one without follows legacy (pod 0)
    monkeypatch.setenv("BRAIN_PROC_KEY", "org-a")
    assert rm._consumer_host(tmp_path / ".runpod_host", p.pool_file).startswith("https://p1-")
    monkeypatch.setenv("BRAIN_PROC_KEY", "org-new")
    assert rm._consumer_host(tmp_path / ".runpod_host", p.pool_file).startswith("https://p0-")


def test_publish_blanks_legacy_host_when_pod_zero_is_not_ready(tmp_path, monkeypatch):
    p = _pool(tmp_path, monkeypatch, slots={0: {"pod_id": "p0", "status": "resuming"}})
    p.publish(now=NOW)
    assert (tmp_path / ".runpod_host").read_text() == "", "booting pod 0 → consumers see off"
    p2 = _pool(tmp_path, monkeypatch)  # nothing held at all
    p2.publish(now=NOW)
    assert (tmp_path / ".runpod_host").read_text() == ""
    assert json.loads(p2.pool_file.read_text())["pods"] == []


def test_scale_up_takes_the_lowest_empty_slot_and_arms_cooldown(tmp_path, monkeypatch):
    p = _pool(tmp_path, monkeypatch, slots={0: {"pod_id": "p0", "status": "ready"}})
    pid = asyncio.run(p.scale_up())
    assert pid == "ollama-brain-p2-id"
    assert p.managers[1]._pod_id == pid and p.managers[2]._pod_id is None
    assert p.cooldown_until > 0
    assert len(p.pods()) == 2 and p.pods()[1].index == 1


def test_failed_scale_up_still_arms_cooldown(tmp_path, monkeypatch):
    p = _pool(
        tmp_path,
        monkeypatch,
        slots={0: {"pod_id": "p0", "status": "ready"}, 1: {"ensure_ok": False}},
    )
    assert asyncio.run(p.scale_up()) is None
    assert p.cooldown_until > 0, "a failed create must not be retried every tick"


def test_ensure_min_wakes_pod_zero_and_pinned_slots(tmp_path, monkeypatch):
    p = _pool(tmp_path, monkeypatch, cfg=PoolConfig(max_pods=3, min_pods=2))
    assert asyncio.run(p.ensure_min()) is True
    assert p.managers[0]._pod_id and p.managers[1]._pod_id and not p.managers[2]._pod_id
    assert p._pod_id == p.managers[0]._pod_id, "duck-typed _pod_id is pod 0's"


def test_drain_then_terminate_after_drain_s(tmp_path, monkeypatch):
    p = _pool(
        tmp_path,
        monkeypatch,
        cfg=PoolConfig(max_pods=2, drain_s=60.0),
        slots={0: {"pod_id": "p0", "status": "ready"}, 1: {"pod_id": "p1", "status": "ready"}},
    )
    p.assignments = {"a": "p1", "b": "p0"}
    p.drain("p1")
    assert p.is_draining("p1")
    assert [x.state for x in p.pods() if x.pod_id == "p1"] == ["draining"]
    assert p.draining_due(now=p._drain_since["p1"] + 30) == []
    assert p.draining_due(now=p._drain_since["p1"] + 60) == ["p1"]
    asyncio.run(p.terminate("p1"))
    assert p.managers[1].paused == ["p1"] and p.managers[1]._pod_id is None
    assert p.assignments == {"b": "p0"}, "its consumers are unassigned"
    assert not p.is_draining("p1")


def test_probe_all_releases_a_dead_pod_and_its_assignments(tmp_path, monkeypatch):
    p = _pool(
        tmp_path,
        monkeypatch,
        slots={
            0: {"pod_id": "p0", "status": "ready"},
            1: {"pod_id": "p1", "status": "ready", "alive": False},
        },
    )
    p.assignments = {"a": "p1", "b": "p0"}
    out = asyncio.run(p.probe_all())
    assert out == {"p0": True, "p1": False}
    assert p.managers[1]._pod_id is None
    assert p.assignments == {"b": "p0"}


def test_pause_sleeps_every_pod_and_clears_assignments(tmp_path, monkeypatch):
    p = _pool(
        tmp_path,
        monkeypatch,
        slots={0: {"pod_id": "p0", "status": "ready"}, 1: {"pod_id": "p1", "status": "ready"}},
    )
    p.assignments = {"a": "p1"}
    asyncio.run(p.pause())
    assert p.managers[0].paused == ["p0"] and p.managers[1].paused == ["p1"]
    assert p.assignments == {} and p.held_pod_ids() == []
    assert (tmp_path / ".runpod_host").read_text() == ""


def test_discover_adopts_pods_and_rebuilds_assignments_for_held_pods(tmp_path, monkeypatch):
    p0 = _pool(
        tmp_path,
        monkeypatch,
        slots={0: {"pod_id": "p0", "status": "ready"}, 1: {"pod_id": "p1", "status": "ready"}},
    )
    p0.assignments = {"a": "p1", "b": "p0", "c": "p-gone"}
    p0.publish(now=NOW)
    # a "redeployed" gateway: only p0 is still there
    p1 = _pool(tmp_path, monkeypatch, slots={0: {"pod_id": "p0", "status": "ready"}})
    host = asyncio.run(p1.discover())
    assert host == "https://p0-11434.proxy.runpod.net"
    assert all(m.discovered for m in p1.managers)
    assert p1.assignments == {"b": "p0"}


def test_status_carries_pod_zero_state_and_the_pool_summary(tmp_path, monkeypatch):
    p = _pool(
        tmp_path,
        monkeypatch,
        slots={0: {"pod_id": "p0", "status": "ready"}, 1: {"pod_id": "p1", "status": "warming"}},
    )
    p.assignments = {"a": "p0"}
    st = p.status()
    assert st["state"] == "ready" and "elapsed_s" in st
    assert st["ready"] == 1 and st["assignments"] == 1 and st["max_pods"] == 3
    assert [x["pod_id"] for x in st["pods"]] == ["p0", "p1"]
    assert p._cost_per_hr == pytest.approx(0.5), "the budget converts at the HIGHEST rate"
    assert p.published_host() == "https://p0-11434.proxy.runpod.net"
