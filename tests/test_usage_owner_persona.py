"""Owner-lane usage is metered under the BOUND persona, not a persona named "owner".

Every console turn and every DMN idle tick runs on the owner lane (no agent_id),
and the flush stamped persona = agent_id.split(".")[0] = "owner" on those rows —
so /v1/usage, persona_usage_totals and the Fleet console attributed all idle spend
to a persona that does not exist. The meter now stamps the persona the lane is
bound to (the DMN binds each roster persona per tick), falling back to the home
persona exactly as usage_report.build does. agent_id stays "owner" — both readers
key their owner filter on it.
"""

from __future__ import annotations

import brain.agent_usage_store as store
import brain.model_router as mr
from brain import turn_ctx
from brain.second_brain.store import bind_persona
from brain.settings import settings


def _mk_router(monkeypatch):
    r = mr.ModelRouter.__new__(mr.ModelRouter)
    r._agent_usage = {}
    r._usage_flushed = {}
    monkeypatch.setattr(r, "_price_usd", lambda *a, **k: 0.01)
    monkeypatch.setitem(settings._data, "agent_usage_raw_enabled", 1)
    monkeypatch.setitem(settings._data, "agent_usage_daily_enabled", 1)
    monkeypatch.setitem(settings._data, "agent_usage_meter_end_users", 1)
    return r


def _capture_flush(monkeypatch):
    rows: list[list[dict]] = []
    monkeypatch.setattr(store, "record_deltas", lambda r: rows.append(list(r)) or True)
    monkeypatch.setattr(store, "bump_daily", lambda r: True)
    return rows


def test_dmn_tick_bound_to_persona_x_meters_persona_x(monkeypatch):
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    r = _mk_router(monkeypatch)
    rows = _capture_flush(monkeypatch)
    with bind_persona("the_visionary"):
        r._meter_agent("claude-sonnet-4-5", 100, 20, is_cloud=True)
    assert list(r._agent_usage) == [("owner", "", "the_visionary")]
    assert r.flush_usage() == 1
    (row,) = rows[0]
    assert row["agent_id"] == "owner"
    assert row["persona"] == "the_visionary"
    assert row["end_user_id"] == ""
    assert row["cloud_calls"] == 1 and row["in_tok"] == 100
    # The in-memory dashboard reader still hides the owner lane.
    assert r.agent_usage() == {}


def test_unbound_owner_lane_falls_back_to_the_home_persona(monkeypatch):
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "The Analyst")
    r = _mk_router(monkeypatch)
    rows = _capture_flush(monkeypatch)
    r._meter_agent("qwen2.5:32b", 50, 10, is_cloud=False, latency=1.5)
    assert list(r._agent_usage) == [("owner", "", "the_analyst")]
    r.flush_usage()
    (row,) = rows[0]
    assert row["persona"] == "the_analyst" and row["agent_id"] == "owner"
    assert row["pod_s"] == 1.5


def test_two_personas_on_the_owner_lane_do_not_merge(monkeypatch):
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    r = _mk_router(monkeypatch)
    rows = _capture_flush(monkeypatch)
    with bind_persona("alpha"):
        r._meter_agent("m", 1, 1, is_cloud=True)
    with bind_persona("beta"):
        r._meter_agent("m", 2, 2, is_cloud=True)
        r._meter_agent("m", 2, 2, is_cloud=True)
    r.flush_usage()
    by_persona = {row["persona"]: row for row in rows[0]}
    assert by_persona["alpha"]["calls"] == 1 and by_persona["beta"]["calls"] == 2


def test_agent_lane_is_unchanged(monkeypatch):
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    r = _mk_router(monkeypatch)
    rows = _capture_flush(monkeypatch)
    with (
        bind_persona("the_visionary"),
        turn_ctx.bind_turn("agent", agent_id="ahab.m", end_user_id="cust-1"),
    ):
        r._meter_agent("m", 5, 5, is_cloud=True)
    assert list(r._agent_usage) == [("ahab.m", "cust-1")]
    r.flush_usage()
    (row,) = rows[0]
    assert row["agent_id"] == "ahab.m" and row["persona"] == "ahab"
    assert row["end_user_id"] == "cust-1"
    assert "ahab.m" in r.agent_usage()
