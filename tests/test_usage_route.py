"""GET /v1/usage — the org's bill per UTC day per persona (plan §10.4).

Two ledgers join per (day, persona): agent_usage (calls, cloud_usd, POOL inference
seconds → pod_hours_shared, priced at rate_per_hr) and gpu_usage (standalone / org
pod wall-clock → pod_hours_dedicated with real gpu_usd). Budgets ride along. The
readers are migration-safe (RPC missing → empty), the route is owner-only and runs
the blocking reads off the loop, and the window defaults to the last 7 UTC days.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain import agent_usage_store, gpu_usage_store
from brain import org_settings as os_
from brain import usage_report as ur
from brain.api.server import build_api_router
from brain.api.sessions import ApiSessionRegistry


class _Res:
    def __init__(self, data):
        self.data = data


class _FakeSb:
    def __init__(self):
        self.rpcs: list[tuple[str, dict]] = []
        self.agent_rows: list[dict] = []
        self.gpu_rows: list[dict] = []
        self.gpu_missing = False
        self.daily_missing = False  # agent_usage_by_day_daily (040) not applied
        self.inserted: list[tuple[str, list]] = []
        self._table = ""
        self._payload = None
        self._name = ""

    def rpc(self, name, params):
        self._name = name
        self.rpcs.append((name, dict(params)))
        return self

    def table(self, name):
        self._table, self._name = name, ""
        return self

    def insert(self, payload):
        self._payload = payload
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        if self._name == "agent_usage_by_day_daily":
            if self.daily_missing:
                raise RuntimeError("function agent_usage_by_day_daily does not exist")
            return _Res(list(self.agent_rows))
        if self._name == "agent_usage_by_day":
            return _Res(list(self.agent_rows))
        if self._name == "gpu_usage_by_day":
            if self.gpu_missing:
                raise RuntimeError("function gpu_usage_by_day does not exist")
            return _Res(list(self.gpu_rows))
        if self._table == "gpu_usage" and self._payload is not None:
            self.inserted.append((self._table, list(self._payload)))
            return _Res(list(self._payload))
        if self._table == "organizations":
            return _Res([{"id": "org-1", "gpu_daily_usd_budget": 12.0}])
        return _Res([])


@pytest.fixture
def sb(monkeypatch, tmp_path):
    from brain.second_brain import supabase_client
    from brain.settings import settings

    fake = _FakeSb()
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: fake)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "the_visionary")
    monkeypatch.delenv("BRAIN_RUNPOD_HOST_FILE", raising=False)
    monkeypatch.delenv("BRAIN_RUNPOD_POOL_FILE", raising=False)
    monkeypatch.delenv("RUNPOD_COST_PER_HR", raising=False)
    monkeypatch.setitem(settings._data, "cloud_daily_usd_budget", 20.0)
    monkeypatch.setitem(settings._data, "partner_cloud_daily_usd_budget", 5.0)
    monkeypatch.setitem(settings._data, "agent_usage_read_daily", 1)
    monkeypatch.setattr(gpu_usage_store, "rate_per_hr", lambda: 0.44)
    os_.invalidate()
    yield fake
    os_.invalidate()


# ── readers ──────────────────────────────────────────────────────────────────


def test_readers_pass_org_and_window_and_survive_missing_rpc(sb):
    sb.agent_rows = [{"day": "2026-09-12", "persona": "ahab", "agent_id": "ahab.r", "pod_s": 36}]
    rows = agent_usage_store.by_day("2026-09-06T00:00:00+00:00", "2026-09-13T00:00:00+00:00")
    assert rows[0]["pod_s"] == 36.0 and rows[0]["calls"] == 0
    # The daily rollup (040) is preferred: the raw ledger is pruned to 7 days
    # while the route advertises 92. DATE-grained params, one RPC.
    name, params = sb.rpcs[-1]
    assert name == "agent_usage_by_day_daily" and params["p_org_id"] == "org-1"
    assert params["p_since"] == "2026-09-06" and params["p_until"] == "2026-09-13"
    assert [n for n, _ in sb.rpcs] == ["agent_usage_by_day_daily"]
    sb.gpu_missing = True
    assert gpu_usage_store.by_day(None, None) == []
    assert gpu_usage_store.usd_today() == 0.0
    sb.gpu_missing = False
    sb.gpu_rows = [
        {
            "day": "2026-09-12",
            "persona": "ahab",
            "pod_kind": "standalone",
            "seconds": 7200,
            "usd": 1.5,
        }
    ]
    assert gpu_usage_store.usd_today() == 1.5
    assert sb.rpcs[-1][1]["p_org_id"] == "org-1"


def test_by_day_falls_back_to_raw_when_the_daily_rpc_is_missing(sb):
    sb.daily_missing = True
    sb.agent_rows = [{"day": "2026-09-12", "persona": "ahab", "agent_id": "ahab.r", "calls": 3}]
    rows = agent_usage_store.by_day("2026-09-06T00:00:00+00:00", "2026-09-13T00:00:00+00:00")
    assert rows[0]["calls"] == 3
    names = [n for n, _ in sb.rpcs]
    assert names == ["agent_usage_by_day_daily", "agent_usage_by_day"]
    _, raw_params = sb.rpcs[-1]
    assert raw_params["p_since"].startswith("2026-09-06T") and raw_params["p_org_id"] == "org-1"


def test_by_day_reads_raw_when_the_daily_read_is_switched_off(sb, monkeypatch):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "agent_usage_read_daily", 0)
    sb.agent_rows = [{"day": "2026-09-12", "persona": "ahab", "agent_id": "ahab.r", "calls": 1}]
    assert agent_usage_store.by_day(None, None)[0]["calls"] == 1
    assert [n for n, _ in sb.rpcs] == ["agent_usage_by_day"]


def test_record_stamps_org_and_skips_zero_rows(sb):
    assert gpu_usage_store.record("org-1", [{"persona": "ahab", "seconds": 0}]) is False
    ok = gpu_usage_store.record(
        "org-1",
        [
            {
                "persona": "ahab",
                "pod_kind": "standalone",
                "pod_id": "s1",
                "seconds": 60,
                "usd": 0.01,
            },
            {"persona": "ishmael", "pod_kind": "weird", "seconds": 30},
        ],
        client=sb,
    )
    assert ok is True
    table, rows = sb.inserted[-1]
    assert table == "gpu_usage" and all(r["org_id"] == "org-1" for r in rows)
    assert rows[1]["pod_kind"] == "standalone"  # unknown kinds normalise


# ── the pure report ──────────────────────────────────────────────────────────


def test_window_defaults_and_validation():
    now = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
    s, u = ur.window(None, None, now=now)
    assert u == datetime(2026, 9, 13, tzinfo=UTC) and s == datetime(2026, 9, 6, tzinfo=UTC)
    s, u = ur.window("2026-09-01", "2026-09-03T12:00:00Z", now=now)
    assert s == datetime(2026, 9, 1, tzinfo=UTC) and u == datetime(2026, 9, 3, 12, tzinfo=UTC)
    with pytest.raises(ur.UsageWindowError):
        ur.window("2026-09-03", "2026-09-01", now=now)
    with pytest.raises(ur.UsageWindowError):
        ur.window("2026-01-01", "2026-09-01", now=now)
    with pytest.raises(ur.UsageWindowError):
        ur.window("yesterday", None, now=now)


def test_build_joins_shared_and_dedicated_hours():
    agent = [
        {
            "day": "2026-09-12",
            "persona": "ahab",
            "agent_id": "ahab.r",
            "calls": 10,
            "cloud_calls": 2,
            "cloud_usd": 0.5,
            "pod_s": 1800,
        },
        {
            "day": "2026-09-12",
            "persona": "ahab",
            "agent_id": "owner",
            "calls": 90,
            "cloud_calls": 0,
            "cloud_usd": 0.0,
            "pod_s": 1800,
        },
        {
            "day": "2026-09-12",
            "persona": "",
            "agent_id": "owner",
            "calls": 5,
            "cloud_calls": 1,
            "cloud_usd": 0.1,
            "pod_s": 360,
        },
        {
            "day": "2026-09-11",
            "persona": "ahab",
            "agent_id": "ahab.r",
            "calls": 1,
            "cloud_calls": 0,
            "cloud_usd": 0.0,
            "pod_s": 0,
        },
    ]
    gpu = [
        {
            "day": "2026-09-12",
            "persona": "ahab",
            "pod_kind": "standalone",
            "seconds": 7200,
            "usd": 1.0,
        },
        {"day": "2026-09-12", "persona": "ahab", "pod_kind": "org", "seconds": 3600, "usd": 0.4},
    ]
    out = ur.build(
        agent,
        gpu,
        since=datetime(2026, 9, 6, tzinfo=UTC),
        until=datetime(2026, 9, 13, tzinfo=UTC),
        rate_per_hr=0.5,
        budgets={"cloud_daily_usd_budget": 20, "gpu_daily_usd_budget": 12, "gpu_usd_today": 1.4},
        home_persona="the_visionary",
    )
    assert [d["day"] for d in out["days"]] == ["2026-09-11", "2026-09-12"]
    day = out["days"][1]
    ahab = day["personas"]["ahab"]
    assert ahab["calls"] == 100 and ahab["cloud_calls"] == 2 and ahab["cloud_usd"] == 0.5
    assert ahab["pod_hours_shared"] == 1.0 and ahab["pod_usd_shared"] == 0.5
    assert ahab["pod_hours_dedicated"] == 3.0 and ahab["gpu_usd"] == 1.4
    home = day["personas"]["the_visionary"]  # empty persona = the home process
    assert home["calls"] == 5 and home["pod_hours_shared"] == 0.1
    assert day["totals"]["calls"] == 105 and day["totals"]["pod_hours_dedicated"] == 3.0
    assert out["totals"]["calls"] == 106 and out["personas"]["ahab"]["calls"] == 101
    assert out["rate_per_hr"] == 0.5
    assert out["budgets"] == {
        "cloud_daily_usd_budget": 20.0,
        "partner_cloud_daily_usd_budget": 0.0,
        "gpu_daily_usd_budget": 12.0,
        "gpu_usd_today": 1.4,
    }


# ── the route ────────────────────────────────────────────────────────────────


def _resolver(authorization):
    return {
        "Bearer kp": {"partner_id": "A", "owner": False, "key_id": "kp"},
        "Bearer ko": {"partner_id": None, "owner": True, "key_id": "ko"},
    }.get(authorization)


P = {"Authorization": "Bearer kp"}
OWN = {"Authorization": "Bearer ko"}


@pytest.fixture
def client(sb):
    app = FastAPI()
    app.include_router(
        build_api_router(
            lambda *a, **k: None,
            ApiSessionRegistry(id_fn=lambda: "sx"),
            auth=lambda h: _resolver(h) is not None,
            resolver=_resolver,
        )
    )
    return TestClient(app)


def test_usage_is_owner_only_and_registered(client):
    assert client.get("/v1/usage", headers=P).status_code == 403
    from brain.api.reference import is_owner_route, section_for

    assert is_owner_route("GET", "/v1/usage")
    assert section_for("/v1/usage") == "Usage"


def test_usage_route_reports_hours_and_budgets(client, sb):
    sb.agent_rows = [
        {
            "day": "2026-09-12",
            "persona": "ahab",
            "agent_id": "ahab.r",
            "calls": 4,
            "cloud_calls": 1,
            "cloud_usd": 0.2,
            "pod_s": 3600,
        },
    ]
    sb.gpu_rows = [
        {
            "day": "2026-09-12",
            "persona": "ahab",
            "pod_kind": "standalone",
            "seconds": 1800,
            "usd": 0.25,
        },
    ]
    r = client.get("/v1/usage", headers=OWN, params={"since": "2026-09-06", "until": "2026-09-13"})
    assert r.status_code == 200, r.text
    body = r.json()
    cell = body["days"][0]["personas"]["ahab"]
    assert cell["pod_hours_shared"] == 1.0 and cell["pod_usd_shared"] == 0.44
    assert cell["pod_hours_dedicated"] == 0.5 and cell["gpu_usd"] == 0.25
    assert body["budgets"]["cloud_daily_usd_budget"] == 20.0
    assert body["budgets"]["partner_cloud_daily_usd_budget"] == 5.0
    assert body["budgets"]["gpu_daily_usd_budget"] == 12.0  # from the org row
    # usd_today read today's window; both ledgers were asked with the org id.
    assert all(params["p_org_id"] == "org-1" for _n, params in sb.rpcs)
    assert body["since"].startswith("2026-09-06") and body["until"].startswith("2026-09-13")


def test_usage_route_defaults_and_400(client, sb):
    r = client.get("/v1/usage", headers=OWN)
    assert r.status_code == 200
    since = datetime.fromisoformat(r.json()["since"])
    until = datetime.fromisoformat(r.json()["until"])
    assert (until - since).days == 7 and r.json()["days"] == []
    assert client.get("/v1/usage", headers=OWN, params={"since": "nope"}).status_code == 400
    r = client.get("/v1/usage", headers=OWN, params={"since": "2026-09-13", "until": "2026-09-06"})
    assert r.status_code == 400
