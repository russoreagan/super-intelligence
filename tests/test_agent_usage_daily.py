"""agent_usage_daily dual write (migration 039): the router's usage flush bumps the
daily rollup in one RPC beside the raw delta rows, meters per end user when
enabled, advances its high-water mark only when a store took the rows, and the
raw table is pruned org-scoped."""

from __future__ import annotations

import pytest

import brain.agent_usage_store as store
import brain.model_router as mr
from brain import turn_ctx
from brain.settings import settings


class _Res:
    def __init__(self, data=None):
        self.data = data if data is not None else []


class _FakeSb:
    def __init__(self):
        self.inserts: list[tuple[str, list]] = []
        self.rpcs: list[tuple[str, dict]] = []
        self.deletes: list[tuple[str, list]] = []
        self.fail_insert: str | None = None
        self.fail_rpc: str | None = None
        self._table = ""
        self._filters: list = []

    def table(self, name):
        self._table, self._filters = name, []
        return self

    def insert(self, payload):
        self._payload = payload
        self._op = "insert"
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, k, v):
        self._filters.append(("eq", k, v))
        return self

    def lt(self, k, v):
        self._filters.append(("lt", k, v))
        return self

    def execute(self):
        if self._op == "insert":
            if self.fail_insert:
                msg, self.fail_insert = self.fail_insert, None
                raise RuntimeError(msg)
            self.inserts.append((self._table, list(self._payload)))
            return _Res()
        self.deletes.append((self._table, list(self._filters)))
        return _Res([{"id": 1}, {"id": 2}])

    def rpc(self, name, params):
        sb = self

        class _R:
            def execute(self_inner):
                sb.rpcs.append((name, params))
                if sb.fail_rpc:
                    raise RuntimeError(sb.fail_rpc)
                return _Res(len(params.get("p_rows") or []))

        return _R()


@pytest.fixture
def sb(monkeypatch):
    from brain.second_brain import supabase_client

    fake = _FakeSb()
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: fake)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    monkeypatch.setattr(store, "_raw_end_user_retry_ts", 0.0)
    monkeypatch.setitem(settings._data, "agent_usage_daily_enabled", 1)
    monkeypatch.setitem(settings._data, "agent_usage_raw_enabled", 1)
    monkeypatch.setitem(settings._data, "agent_usage_meter_end_users", 1)
    return fake


def _router(usage=None):
    r = mr.ModelRouter.__new__(mr.ModelRouter)
    r._agent_usage = usage if usage is not None else {}
    r._usage_flushed = {}
    return r


def _u(**kw):
    base = {"calls": 0, "cloud_calls": 0, "in_tok": 0, "out_tok": 0, "cloud_usd": 0.0, "pod_s": 0.0}
    return {**base, **kw}


def test_flush_bumps_daily_in_one_rpc_with_n_rows(sb):
    r = _router(
        {
            "owner": _u(calls=2, pod_s=10.0),
            ("ahab.companion", "buyer1"): _u(calls=3, cloud_calls=1, cloud_usd=0.02),
            ("ahab.companion", "buyer2"): _u(calls=1, in_tok=50),
        }
    )
    assert mr.ModelRouter.flush_usage(r) == 3
    assert len(sb.rpcs) == 1
    name, params = sb.rpcs[0]
    assert name == "bump_agent_usage_daily" and params["p_org_id"] == "org-1"
    rows = {(x["agent_id"], x["end_user_id"]): x for x in params["p_rows"]}
    assert set(rows) == {("owner", ""), ("ahab.companion", "buyer1"), ("ahab.companion", "buyer2")}
    assert rows[("ahab.companion", "buyer1")]["persona"] == "ahab"
    assert rows[("ahab.companion", "buyer1")]["cloud_usd"] == pytest.approx(0.02)
    assert "org_id" not in rows[("owner", "")], "the RPC scopes by p_org_id"
    # Raw rows land too, carrying end_user_id.
    assert len(sb.inserts) == 1 and sb.inserts[0][0] == "agent_usage"
    raw = {(x["agent_id"], x["end_user_id"]) for x in sb.inserts[0][1]}
    assert raw == set(rows) and all(x["org_id"] == "org-1" for x in sb.inserts[0][1])
    # Second flush with nothing new writes nothing.
    assert mr.ModelRouter.flush_usage(r) == 0 and len(sb.rpcs) == 1


def test_meter_keys_end_user_when_enabled_and_blank_when_off(sb, monkeypatch):
    r = _router()
    monkeypatch.setattr(r, "_price_usd", lambda *a, **k: 0.001, raising=False)
    with turn_ctx.bind_turn("agent", "s1", "ahab.companion", "buyer1", partner_id="acme"):
        r._meter_agent("m", 10, 5, is_cloud=True)
        r._meter_agent("m", 10, 5, is_cloud=False, latency=2.0)
    with turn_ctx.bind_turn("agent", "s2", "ahab.companion", "buyer2"):
        r._meter_agent("m", 1, 1, is_cloud=False, latency=1.0)
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    r._meter_agent("m", 1, 1, is_cloud=False, latency=0.5)  # owner lane, unbound
    # The owner/idle lane keys on the persona it is bound to (here the home
    # persona fallback) so per-persona cost includes idle spend; agent_id stays
    # "owner" so both readers still filter it out of the per-agent dashboard.
    assert set(r._agent_usage) == {
        ("ahab.companion", "buyer1"),
        ("ahab.companion", "buyer2"),
        ("owner", "", "home_p"),
    }
    assert r._agent_usage[("ahab.companion", "buyer1")]["calls"] == 2
    # The live dashboard view still sums per agent and hides the owner lane.
    view = r.agent_usage()
    assert set(view) == {"ahab.companion"}
    assert view["ahab.companion"]["calls"] == 3 and view["ahab.companion"]["pod_s"] == 3.0

    monkeypatch.setitem(settings._data, "agent_usage_meter_end_users", 0)
    r2 = _router()
    with turn_ctx.bind_turn("agent", "s1", "ahab.companion", "buyer1"):
        r2._meter_agent("m", 1, 1, is_cloud=False, latency=1.0)
    assert set(r2._agent_usage) == {"ahab.companion"}
    assert mr.ModelRouter.flush_usage(r2) == 1
    assert sb.rpcs[-1][1]["p_rows"][0]["end_user_id"] == ""


def test_high_water_advances_per_store_only_when_that_store_took_the_rows(sb):
    r = _router({("ahab.companion", "b1"): _u(calls=5)})
    sb.fail_insert = "boom"
    sb.fail_rpc = "boom"
    assert mr.ModelRouter.flush_usage(r) == 0
    assert r._usage_flushed == {} and r._usage_flushed_daily == {}, "nothing landed"
    # Raw still fails, daily lands → ONLY the daily mark advances.
    sb.fail_rpc = None
    sb.fail_insert = "still down"
    assert mr.ModelRouter.flush_usage(r) == 1
    assert r._usage_flushed_daily[("ahab.companion", "b1")]["calls"] == 5
    assert r._usage_flushed == {}, "the raw ledger has not taken these rows yet"
    # Raw recovers → the SAME delta reaches it; daily is not re-sent.
    sb.fail_insert = None
    n_rpc = len(sb.rpcs)
    assert mr.ModelRouter.flush_usage(r) == 1
    assert sb.inserts[-1][1][0]["calls"] == 5
    assert len(sb.rpcs) == n_rpc, "daily already has it"
    assert mr.ModelRouter.flush_usage(r) == 0
    # Daily off + raw on: raw success alone advances the raw mark.
    settings._data["agent_usage_daily_enabled"] = 0
    r._agent_usage[("ahab.companion", "b1")]["calls"] = 9
    assert mr.ModelRouter.flush_usage(r) == 1
    assert len(sb.rpcs) == n_rpc and r._usage_flushed[("ahab.companion", "b1")]["calls"] == 9
    settings._data["agent_usage_daily_enabled"] = 1
    # Back on: the rollup catches up on exactly what it missed.
    assert mr.ModelRouter.flush_usage(r) == 1
    assert sb.rpcs[-1][1]["p_rows"][0]["calls"] == 4


def test_daily_failure_is_retried_not_lost(sb):
    r = _router({("ahab.companion", "b1"): _u(calls=3, cloud_usd=0.5)})
    sb.fail_rpc = "timeout"
    assert mr.ModelRouter.flush_usage(r) == 1  # raw took it
    sb.fail_rpc = None
    r._agent_usage[("ahab.companion", "b1")]["calls"] = 4
    assert mr.ModelRouter.flush_usage(r) == 1
    row = sb.rpcs[-1][1]["p_rows"][0]
    assert row["calls"] == 4 and row["cloud_usd"] == pytest.approx(0.5)
    assert sb.inserts[-1][1][0]["calls"] == 1  # raw only gets the new call


def test_owner_lane_personas_coalesce_into_one_daily_row(sb):
    """Two idle personas share the rollup key (owner, '') — sent as two rows the
    RPC fails with 'ON CONFLICT DO UPDATE command cannot affect row a second time'."""
    r = _router(
        {
            ("owner", "", "home"): _u(calls=2, pod_s=10.0),
            ("owner", "", "the_analyst"): _u(calls=3, cloud_usd=0.25),
            ("ahab.companion", "b1"): _u(calls=1),
        }
    )
    assert mr.ModelRouter.flush_usage(r) == 3
    rows = sb.rpcs[-1][1]["p_rows"]
    keys = [(x["agent_id"], x["end_user_id"]) for x in rows]
    assert len(keys) == len(set(keys)) == 2
    owner = next(x for x in rows if x["agent_id"] == "owner")
    assert owner["calls"] == 5 and owner["pod_s"] == pytest.approx(10.0)
    assert owner["cloud_usd"] == pytest.approx(0.25)
    # The raw ledger keeps the per-persona split.
    assert len(sb.inserts[-1][1]) == 3


def test_usage_metered_during_the_flush_is_not_marked_flushed(sb, monkeypatch):
    r = _router({("ahab.companion", "b1"): _u(calls=1)})
    real = store.record_deltas

    def _meter_mid_flush(rows):
        # The event loop keeps metering while this thread waits on Supabase.
        r._agent_usage[("ahab.companion", "b1")]["calls"] += 7
        return real(rows)

    monkeypatch.setattr(store, "record_deltas", _meter_mid_flush)
    mr.ModelRouter.flush_usage(r)
    monkeypatch.setattr(store, "record_deltas", real)
    assert mr.ModelRouter.flush_usage(r) == 1
    assert sb.inserts[-1][1][0]["calls"] == 7, "the mid-flush calls reach the next flush"


def test_raw_insert_retries_without_end_user_pre_migration(sb):
    sb.fail_insert = 'column "end_user_id" of relation "agent_usage" does not exist'
    ok = store.record_deltas([{"agent_id": "a.b", "end_user_id": "u1", "calls": 1}])
    assert ok is True and len(sb.inserts) == 1
    assert "end_user_id" not in sb.inserts[0][1][0]
    # Remembered: the next insert strips up front.
    store.record_deltas([{"agent_id": "a.b", "end_user_id": "u1", "calls": 1}])
    assert "end_user_id" not in sb.inserts[1][1][0]


def test_prune_raw_is_org_scoped_by_ts(sb):
    assert store.prune_raw(7) == 2
    table, filters = sb.deletes[0]
    assert table == "agent_usage"
    assert ("eq", "org_id", "org-1") in filters
    lt = [f for f in filters if f[0] == "lt"]
    assert len(lt) == 1 and lt[0][1] == "ts" and lt[0][2].startswith("20")
    assert store.prune_raw(0) is None and store.prune_raw("x") is None
    assert len(sb.deletes) == 1


def test_flush_loop_extras_prune_once_per_day_and_flush_touches(sb, monkeypatch):
    import asyncio

    from brain import persona_index
    from brain.session_loops import _LoopsMixin

    pruned, flushed = [], []
    monkeypatch.setattr(store, "prune_raw", lambda d: pruned.append(d))
    monkeypatch.setattr(persona_index, "flush_touches", lambda: flushed.append(1))
    monkeypatch.setitem(settings._data, "agent_usage_raw_retention_days", 7)

    class _B(_LoopsMixin):
        pass

    b = _B()
    asyncio.run(b._usage_flush_extras())
    asyncio.run(b._usage_flush_extras())
    assert pruned == [7] and flushed == [1, 1]
    monkeypatch.setitem(settings._data, "agent_usage_raw_retention_days", 0)
    b._usage_prune_day = ""
    asyncio.run(b._usage_flush_extras())
    assert pruned == [7]


def test_no_backend_is_a_silent_no_op(monkeypatch):
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    assert store.bump_daily([{"agent_id": "a.b", "calls": 1}]) is False
    assert store.prune_raw(7) is None
    r = _router({"owner": _u(calls=1)})
    assert mr.ModelRouter.flush_usage(r) == 0 and r._usage_flushed == {}
