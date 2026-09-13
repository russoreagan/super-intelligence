"""Event-driven reconciler (brain/gateway/reconciler) and its edges.

Contract: a tick runs on a wake (coalesced), at the state machine's own deadline,
or at the resync — never on a fixed 60 s timer. Edges arrive from the tenant
nudge route, the API's placement/budget writes, child exits and the sleep sweep.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import brain.gateway_nudge as gn
import brain.pod_budget as pb
import brain.provisioner as pv
from brain.gateway import placement_control as pc
from brain.gateway import pod_reconcile as rc
from brain.gateway import reconciler as rl
from brain.pod_pool import PoolConfig, ScaleHistory
from tests.test_persona_placement import OWN, client, sb  # noqa: F401 - fixtures

ORG = "org-1"


# ── the loop ──────────────────────────────────────────────────────────────────


def _run(coro, timeout=5.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_wakes_coalesce_into_one_tick_with_their_reasons():
    ticks: list[list[str]] = []

    async def tick(reasons):
        ticks.append(reasons)

    async def go():
        rec = rl.Reconciler(tick, resync_s=60.0, debounce_s=0.05, name="t")
        task = asyncio.create_task(rec.run(initial=False))
        await asyncio.sleep(0.01)
        rec.wake("placement:a")
        rec.wake("demand:b")
        rec.wake("use:b")
        await asyncio.sleep(0.2)
        task.cancel()
        return rec

    rec = _run(go())
    assert ticks == [["placement:a", "demand:b", "use:b"]]
    assert rec.ticks == 1 and rec.status()["last_reasons"] == ["placement:a", "demand:b", "use:b"]


def test_startup_tick_then_deadline_then_resync():
    ticks: list[list[str]] = []
    deadline = {"at": None}

    async def tick(reasons):
        ticks.append(reasons)

    async def go():
        rec = rl.Reconciler(
            tick, deadline_fn=lambda now: deadline["at"], resync_s=0.3, debounce_s=0.01, name="t"
        )
        deadline["at"] = time.time() + 0.1
        task = asyncio.create_task(rec.run())
        await asyncio.sleep(0.2)  # startup + the 0.1 s deadline
        deadline["at"] = None
        await asyncio.sleep(0.4)  # the 0.3 s resync
        task.cancel()
        return rec

    rec = _run(go())
    assert ticks[0] == ["startup"]
    assert ["deadline"] in ticks and ["resync"] in ticks
    assert rec.status()["resync_s"] == 0.3


def test_a_failing_tick_does_not_kill_the_loop():
    calls = {"n": 0}

    async def tick(reasons):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")

    async def go():
        rec = rl.Reconciler(tick, resync_s=60.0, debounce_s=0.01, name="t")
        task = asyncio.create_task(rec.run(initial=False))
        await asyncio.sleep(0.01)
        rec.wake("a")
        await asyncio.sleep(0.05)
        rec.wake("b")
        await asyncio.sleep(0.05)
        task.cancel()
        return rec

    rec = _run(go())
    assert calls["n"] == 2
    assert rec.last_error is None, "cleared by the successful second tick"


def test_wake_threadsafe_reaches_the_loop():
    import threading

    ticks: list[list[str]] = []

    async def tick(reasons):
        ticks.append(reasons)

    async def go():
        rec = rl.Reconciler(tick, resync_s=60.0, debounce_s=0.01, name="t")
        task = asyncio.create_task(rec.run(initial=False))
        await asyncio.sleep(0.01)
        threading.Thread(target=rec.wake_threadsafe, args=("child_exit:x",)).start()
        await asyncio.sleep(0.1)
        task.cancel()

    _run(go())
    assert ticks == [["child_exit:x"]]


def test_env_knobs(monkeypatch):
    monkeypatch.setenv("BRAIN_RECONCILE_RESYNC_S", "120")
    monkeypatch.setenv("BRAIN_POD_RECONCILE_S", "0")
    assert rl.resync_interval_s() == 120.0 and rl.legacy_period_s() == 0.0
    monkeypatch.setenv("BRAIN_POD_RECONCILE_S", "60")
    rec = rl.Reconciler(lambda r: None, name="t")
    assert rec.period_s == 60.0 and rec._wait_s(time.time()) == 60.0, "opt-in fixed period"
    monkeypatch.delenv("BRAIN_POD_RECONCILE_S")
    rec = rl.Reconciler(lambda r: None, name="t")
    assert rec.period_s == 0.0 and rec._wait_s(time.time()) == rec.resync_s
    assert rl.next_utc_midnight(0.0) == 86400.0


# ── the nudge route ───────────────────────────────────────────────────────────


class _Rec:
    def __init__(self):
        self.reasons: list[str] = []

    def wake(self, reason):
        self.reasons.append(reason)

    def status(self):
        return {"ticks": 0}


def _gw_app(monkeypatch, rec):
    from brain.gateway import server as gw

    class _Prov:
        def keys_for_all(self):
            return []

        def full_count(self):
            return 0

    monkeypatch.setattr(gw, "reconciler_holder", [rec])
    monkeypatch.setattr(gw, "_kick_pod", lambda: None, raising=False)
    return gw.build_gateway_app(_Prov(), [None]), gw


def test_nudge_route_wakes_with_token_from_loopback(monkeypatch):
    from fastapi.testclient import TestClient

    rec = _Rec()
    app, gw = _gw_app(monkeypatch, rec)
    c = TestClient(app)
    r = c.post(
        "/__nudge",
        json={"reason": "placement", "key": f"{ORG}::ahab"},
        headers={"x-brain-nudge-token": gw.NUDGE_TOKEN},
    )
    assert r.status_code == 200 and r.json()["woke"] is True
    assert rec.reasons == [f"placement:{ORG}::ahab"]
    assert (
        c.post(
            "/__nudge", json={"reason": "placement"}, headers={"x-brain-nudge-token": "nope"}
        ).status_code
        == 403
    )
    assert (
        c.post(
            "/__nudge", json={"reason": "reboot"}, headers={"x-brain-nudge-token": gw.NUDGE_TOKEN}
        ).status_code
        == 400
    )
    assert rec.reasons == [f"placement:{ORG}::ahab"], "refused nudges never wake"


def test_nudge_route_refuses_non_loopback(monkeypatch):
    from fastapi.testclient import TestClient

    rec = _Rec()
    app, gw = _gw_app(monkeypatch, rec)
    c = TestClient(app, client=("203.0.113.9", 5000))
    r = c.post(
        "/__nudge", json={"reason": "demand"}, headers={"x-brain-nudge-token": gw.NUDGE_TOKEN}
    )
    assert r.status_code == 403 and rec.reasons == []


def test_token_is_exported_for_tenant_spawns():
    import os

    from brain.gateway import server as gw

    assert os.environ.get("BRAIN_GATEWAY_NUDGE_TOKEN") == gw.NUDGE_TOKEN
    assert len(gw.NUDGE_TOKEN) >= 24


# ── the tenant-side client ────────────────────────────────────────────────────


def test_nudge_client_is_a_noop_without_a_url(monkeypatch):
    monkeypatch.delenv(gn.URL_ENV, raising=False)
    gn.reset()
    assert gn.nudge("placement") is False


def test_nudge_client_sends_and_throttles(monkeypatch):
    sent: list[tuple[str, str, dict]] = []
    monkeypatch.setenv(gn.URL_ENV, "http://127.0.0.1:1/__nudge")
    monkeypatch.setenv(gn.TOKEN_ENV, "tok")
    monkeypatch.setenv("BRAIN_PROC_KEY", f"{ORG}::ahab")
    monkeypatch.setattr(gn, "_send", lambda url, token, body: sent.append((url, token, body)))
    gn.reset()
    now = 1_000.0
    assert gn.nudge("demand", now=now) is True
    assert gn.nudge("demand", now=now + 1) is False, "throttled"
    assert gn.nudge("demand", now=now + 6) is True
    assert gn.nudge("placement", now=now + 6, persona="ahab") is True
    assert gn.nudge("placement", now=now + 6) is True, "structural nudges are never throttled"
    for _ in range(50):  # let the daemon threads land
        if len(sent) == 4:
            break
        time.sleep(0.01)
    assert len(sent) == 4
    url, token, body = sent[0]
    assert url.endswith("/__nudge") and token == "tok"
    assert body["reason"] == "demand" and body["key"] == f"{ORG}::ahab"
    assert any(b.get("persona") == "ahab" for _, _, b in sent)


def test_demand_nudges_only_while_the_pod_is_off_and_use_never(monkeypatch, tmp_path):
    seen: list[str] = []
    monkeypatch.setattr(gn, "nudge", lambda reason, **k: seen.append(reason) or True)
    monkeypatch.setattr(pv, "POD_DEMAND_FILE", tmp_path / ".pod_demand")
    monkeypatch.setattr(pv, "POD_USE_FILE", tmp_path / ".pod_used")
    monkeypatch.setattr(pv, "_last_pod_demand_write", 0.0)
    monkeypatch.setattr(pv, "_last_pod_use_write", 0.0)
    monkeypatch.setattr(pv, "_pod_is_off", lambda: True)
    pv.note_pod_demand()
    pv.note_pod_use()
    assert seen == ["demand"], "asking while the pod is off is the wake edge; output is not an edge"
    monkeypatch.setattr(pv, "_last_pod_demand_write", 0.0)
    monkeypatch.setattr(pv, "_pod_is_off", lambda: False)
    pv.note_pod_demand()
    assert seen == ["demand"], "a pod already serving needs no nudge"


def test_pressure_nudges_only_when_queueing():
    from brain import pod_pressure as pp_

    assert not pp_._queueing(
        {"calls_1m": 5, "busy_s_1m": 20.0, "wait_p95_s": 0.1, "permits": 2, "inflight": 1}
    )
    assert pp_._queueing({"sat_frac_1m": 0.2})
    assert pp_._queueing({"wait_p95_s": 2.5})
    assert pp_._queueing({"permits": 2, "inflight": 2})
    assert not pp_._queueing({"permits": 0, "inflight": 0})


def test_api_placement_and_budget_writes_nudge(client, sb, monkeypatch):  # noqa: F811
    import brain.org_settings as os_

    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(gn, "nudge", lambda reason, **k: seen.append((reason, k)) or True)
    sb.org_row["gpu_daily_usd_budget"] = 10.0
    os_.invalidate()
    assert (
        client.post(
            "/v1/personas/ahab/placement", headers=OWN, json={"mode": "dedicated"}
        ).status_code
        == 200
    )
    assert client.delete("/v1/personas/ahab/placement", headers=OWN).status_code == 200
    r = client.put("/v1/org/permissions", headers=OWN, json={"gpu_daily_usd_budget": 3.0})
    assert r.status_code == 200, r.text
    reasons = [s for s, _ in seen]
    assert reasons == ["placement", "placement", "budget"]
    assert seen[0][1]["persona"] == "ahab"


# ── child exit ────────────────────────────────────────────────────────────────


class _WaitProc:
    def __init__(self):
        self.pid = 4242
        self._done = __import__("threading").Event()
        self.code = None

    def poll(self):
        return self.code

    def wait(self):
        self._done.wait(5.0)
        return self.code

    def exit(self, code=1):
        self.code = code
        self._done.set()

    def terminate(self):
        self.exit(-15)

    def kill(self):
        self.exit(-9)


def test_child_exit_reports_the_key_unless_replaced_or_stopped():
    async def go():
        prov = pv.Provisioner()
        seen: list[str] = []
        prov.on_child_exit = seen.append
        p1 = _WaitProc()
        e1 = pv._Proc(p1, 9001)
        prov._procs[f"{ORG}::a"] = e1
        prov._watch_exit(f"{ORG}::a", e1)
        p2 = _WaitProc()
        e2 = pv._Proc(p2, 9002)
        prov._procs["org-2"] = e2
        prov._watch_exit("org-2", e2)
        # org-2 is deliberately stopped (popped) before it exits: no report.
        prov._procs.pop("org-2")
        p2.exit(0)
        p1.exit(137)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if seen:
                break
        return seen

    assert _run(go()) == [f"{ORG}::a"]


def test_spawn_registers_the_exit_watcher(monkeypatch):
    """_spawn_once wires the watcher for every real spawn (the fake proc here has
    no wait(), so the watcher is a no-op — the wiring is what is asserted)."""
    calls: list[str] = []
    orig = pv.Provisioner._watch_exit
    monkeypatch.setattr(pv.Provisioner, "_watch_exit", lambda self, key, entry: calls.append(key))

    class _P:
        pid = 1

        def poll(self):
            return None

    async def go():
        prov = pv.Provisioner()
        monkeypatch.setattr(prov, "_build_and_launch", lambda uid, persona=None: (_P(), 9000, 9001))

        async def _health(port, proc):
            return "full"

        monkeypatch.setattr(prov, "_wait_health", _health)
        await prov._spawn_once(ORG)

    _run(go())
    assert calls == [ORG]
    monkeypatch.setattr(pv.Provisioner, "_watch_exit", orig)


# ── deadlines ─────────────────────────────────────────────────────────────────


def test_placement_deadlines(monkeypatch):
    from datetime import UTC, datetime

    now = time.time()
    state = pc.PlacementState()
    exp = datetime.fromtimestamp(now + 500, tz=UTC).isoformat()
    state.desired = {
        ORG: [
            {"persona": "a", "mode": "dedicated", "paid_until": exp},
            {"persona": "b", "mode": "dedicated", "paid_until": None},
            {
                "persona": "c",
                "mode": "shared",
                "paid_until": datetime.fromtimestamp(now + 5, tz=UTC).isoformat(),
            },
        ]
    }
    assert pc.next_deadline(state, now, 600.0) == pytest.approx(now + 500, abs=1)

    class _M:
        _pod_id = "p"
        _status = "ready"
        _cost_per_hr = 0.8

        def _pod_host(self, pid):
            return "h"

    pod = pc.DedicatedPod(key=f"{ORG}::b", kind="standalone", org=ORG, manager=_M())
    pod.up_since = now - 100
    pod.last_use_at = now - 50
    state.pods[pod.key] = pod
    # Pause-after-grace anchored on the last use: now - 50 + 600.
    assert pc.next_deadline(state, now, 600.0) == pytest.approx(now + 500, abs=1)
    pod.last_use_at = now - 590
    pod.up_since = now - 1000  # the anchor is the LATER of wake and last use
    assert pc.next_deadline(state, now, 600.0) == pytest.approx(now + 10, abs=1)
    # Budget exhaustion ETA at the observed rate beats it when sooner.
    pod.rate_seen = 0.8
    monkeypatch.setattr(
        "brain.org_settings.cached_org_caps", lambda org: {"gpu_daily_usd_budget": 1.0}
    )
    state.spent[ORG] = (pc._today(), 0.999)
    assert pc.next_deadline(state, now, 600.0) < now + 10
    # Orphan grace (budget ETA cleared so it does not win).
    state.pods.clear()
    pod.rate_seen = 0.0
    pod.last_use_at = None
    pod.up_since = None
    state.spent.clear()
    pod.no_consumer_since = now - 100
    state.pods[pod.key] = pod
    assert pc.next_deadline(state, now, 600.0) == pytest.approx(now + 20, abs=1)
    # A spent budget waits for the UTC rollover.
    state.pods.clear()
    state.desired = {}
    state.budget_paused.add(ORG)
    assert pc.next_deadline(state, now, 600.0) == rl.next_utc_midnight(now)
    # Invalidate forces the next tick to re-read everything.
    state.desired_read_at = now
    state.caps_read_at[ORG] = now
    pc.invalidate(state)
    assert state.desired_read_at == 0.0 and state.caps_read_at == {}


def test_pool_deadlines(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "_LEDGER", tmp_path / ".pod_budget.json")
    monkeypatch.setattr(pb, "cooldown_remaining_s", lambda: 0.0)
    now = time.time()

    class _Pool:
        cfg = PoolConfig(
            max_pods=3, grace_s=600.0, drain_s=60.0, up_after_s=300.0, down_after_s=900.0
        )
        history = ScaleHistory()
        _drain_since: dict = {}

    pool = _Pool()
    st = rc.ReconcileState()
    assert rc.next_deadline(pool, st, now) is None
    st.pod0_held = True
    st.pod0_up_since = now - 100
    st.use_seen_at = now - 200
    assert rc.next_deadline(pool, st, now) == pytest.approx(now + 500, abs=1), "grace after wake"
    st.idle_since = now - 550
    assert rc.next_deadline(pool, st, now) == pytest.approx(now + 50, abs=1)
    pool.history.hot_since = now - 290
    assert rc.next_deadline(pool, st, now) == pytest.approx(now + 10, abs=1), "scale dwell"
    pool.history.hot_since = None
    pool._drain_since = {"p1": now - 55}
    assert rc.next_deadline(pool, st, now) == pytest.approx(now + 5, abs=1), "drain due"
    pool._drain_since = {}
    st.over_budget = True
    st.idle_since = None
    st.use_seen_at = None
    st.pod0_up_since = None
    st.pod0_held = False
    assert rc.next_deadline(pool, st, now) == rl.next_utc_midnight(now)
    st.over_budget = False
    st.demand_seen_at = now - 5
    monkeypatch.setattr(pb, "cooldown_remaining_s", lambda: 42.0)
    assert rc.next_deadline(pool, st, now) == pytest.approx(now + 42, abs=1), "cooldown end"


def test_reconcile_tick_records_what_it_saw(tmp_path, monkeypatch):
    from tests.test_gateway_pod_reconcile import _FakePool, _FakeProv, _pod, _pressure

    monkeypatch.setattr(pb, "_LEDGER", tmp_path / ".pod_budget.json")
    monkeypatch.setattr(pb, "budget_usd", lambda: 10.0)
    monkeypatch.setattr(pb, "rate_per_hr", lambda: 0.5)
    monkeypatch.setattr(pv, "POD_DEMAND_FILE", tmp_path / ".pod_demand")
    monkeypatch.setattr(pv, "POD_USE_FILE", tmp_path / ".pod_used")
    (tmp_path / "pressure").mkdir()
    now = 2_000_000.0
    d = _pressure(tmp_path, ORG, demand_ts=now - 5, use_ts=now - 7)
    pool = _FakePool(pods=[_pod("p0", 0)])
    pool._now = now
    st = rc.ReconcileState(last_tick=now - 60)
    asyncio.run(rc.reconcile_tick(pool, _FakeProv(keys=[ORG]), st, now=now, pressure_dir=d))
    assert st.pod0_held is True
    assert st.use_seen_at == pytest.approx(now - 7) and st.demand_seen_at == pytest.approx(now - 5)
    assert rc.next_deadline(pool, st, now) is not None


def test_gateway_wires_the_reconciler_not_a_sleep_loop():
    from pathlib import Path

    src = Path("brain/gateway/server.py").read_text(encoding="utf-8")
    assert "await asyncio.sleep(reconcile_interval_s)" not in src
    assert src.count("Reconciler(_tick, deadline_fn=_deadline") == 2, "pool and legacy paths"
    assert "provisioner.on_child_exit = lambda key: wake_reconciler" in src
    assert 'os.environ["BRAIN_GATEWAY_NUDGE_URL"]' in src
