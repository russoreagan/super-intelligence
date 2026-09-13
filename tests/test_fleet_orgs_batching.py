"""Fleet view query batching (brain/gateway/fleet_orgs.py).

Before: every 30 s refresh issued three `count=exact, head=true` selects per org
(3N queries) and one unbounded `select * from organizations`. Now: one grouped
RPC (migration 041, `fleet_persona_counts`) for the whole fleet, the per-org
head counts only while that RPC is missing (parked for RPC_MISSING_TTL_S), and
organizations read in pages of ORG_PAGE_SIZE. The response shape is unchanged.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from brain.gateway import fleet_orgs as fo

NOW = 1_800_000_000.0


class _Res:
    def __init__(self, data=None, count=None):
        self.data = data if data is not None else []
        self.count = count


class _Query:
    def __init__(self, sb, table):
        self.sb, self.table, self.filters = sb, table, []

    def select(self, *a, **k):
        return self

    def __getattr__(self, name):
        if name in ("eq", "is_", "neq", "gte", "order", "range"):

            def _f(*a):
                self.filters.append((name, *a))
                return self

            return _f
        raise AttributeError(name)

    def execute(self):
        self.sb.calls.append((self.table, list(self.filters)))
        if self.table == "organizations":
            rng = next(f for f in self.filters if f[0] == "range")
            page_no = rng[1] // fo.ORG_PAGE_SIZE
            if page_no in self.sb.fail_pages:
                raise RuntimeError("organizations page failed")
            return _Res(self.sb.orgs[rng[1] : rng[2] + 1])
        if self.table == "personas":
            if self.sb.personas_error:
                raise RuntimeError(self.sb.personas_error)
            org = next(f[2] for f in self.filters if f[0] == "eq" and f[1] == "org_id")
            kinds = {f[0] for f in self.filters}
            c = self.sb.counts.get(org, {"customs": 0, "clones": 0, "active": 0})
            if "neq" in kinds:
                return _Res(count=c["clones"])
            if "gte" in kinds:
                return _Res(count=c["active"])
            return _Res(count=c["customs"])
        return _Res()


class _FakeSb:
    def __init__(self):
        self.calls: list = []
        self.rpcs: list = []
        self.orgs: list = []
        self.counts: dict = {}
        self.rpc_rows: dict = {}
        self.rpc_error: dict = {}
        self.fail_pages: set = set()
        self.personas_error = ""

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        sb = self

        class _Rpc:
            def execute(self_inner):
                sb.rpcs.append((name, dict(params)))
                if name in sb.rpc_error:
                    raise RuntimeError(sb.rpc_error[name])
                return _Res(sb.rpc_rows.get(name, []))

        return _Rpc()


def _orgs(n: int) -> list[dict]:
    return [
        {"id": f"org-{i:04d}", "name": f"Org {i}", "learning_mode": None, "instance_seed": None}
        for i in range(n)
    ]


def _org_calls(sb) -> list[tuple[int, int]]:
    return [
        (f[1], f[2])
        for table, filters in sb.calls
        if table == "organizations"
        for f in filters
        if f[0] == "range"
    ]


def _persona_calls(sb) -> int:
    return sum(1 for table, _f in sb.calls if table == "personas")


@pytest.fixture
def sb(monkeypatch):
    from brain import agent_usage_store
    from brain.second_brain import supabase_client

    fake = _FakeSb()
    fake.orgs = _orgs(3)
    fake.counts = {
        "org-0000": {"customs": 120, "clones": 117, "active": 40},
        "org-0001": {"customs": 2, "clones": 0, "active": 0},
    }
    fake.rpc_rows[fo.COUNTS_RPC] = [
        {"org_id": "org-0000", "persona_count": 120, "clone_count": 117, "active_roster_size": 40},
        {"org_id": "org-0001", "persona_count": 2, "clone_count": 0, "active_roster_size": 0},
        {"org_id": "org-zzzz", "persona_count": 9, "clone_count": 9, "active_roster_size": 9},
    ]
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: fake)
    monkeypatch.setattr(agent_usage_store, "totals_all_by_org", lambda *a: ({}, "none"))
    fo._reset_for_tests()
    yield fake
    fo._reset_for_tests()


EXPECTED = {
    "org-0000": {
        "persona_count": 120,
        "clone_count": 117,
        "templates": 3,
        "active_roster_size": 40,
    },
    "org-0001": {"persona_count": 2, "clone_count": 0, "templates": 2, "active_roster_size": 0},
    "org-0002": {"persona_count": 0, "clone_count": 0, "templates": 0, "active_roster_size": 0},
}


# ── grouped RPC ─────────────────────────────────────────────────────────────


def test_counts_come_from_one_grouped_rpc(sb):
    snap = fo.db_snapshot(NOW)
    assert snap["counts"] == EXPECTED  # org-zzzz (not an org row) is not surfaced
    names = [n for n, _p in sb.rpcs if n == fo.COUNTS_RPC]
    assert names == [fo.COUNTS_RPC]
    assert _persona_calls(sb) == 0
    cutoff = _dt.datetime.fromtimestamp(NOW - fo.ACTIVE_DAYS * 86400, _dt.UTC).isoformat()
    assert dict(sb.rpcs[0][1]) == {"p_active_since": cutoff}
    # One organizations page for a small fleet.
    assert _org_calls(sb) == [(0, fo.ORG_PAGE_SIZE - 1)]


def test_missing_rpc_falls_back_to_per_org_head_counts_and_is_parked(sb):
    sb.rpc_error[fo.COUNTS_RPC] = "Could not find the function public.fleet_persona_counts"
    snap = fo.db_snapshot(NOW)
    assert snap["counts"] == EXPECTED  # identical numbers, identical shape
    assert _persona_calls(sb) == 3 * len(sb.orgs)
    assert sum(1 for n, _p in sb.rpcs if n == fo.COUNTS_RPC) == 1
    # Next refresh inside the TTL: no RPC retry, still per-org.
    fo._db_cache = None
    fo.db_snapshot(NOW + fo.DB_CACHE_S + 1)
    assert sum(1 for n, _p in sb.rpcs if n == fo.COUNTS_RPC) == 1
    assert _persona_calls(sb) == 2 * 3 * len(sb.orgs)
    # After the TTL the RPC is tried again — and when the migration has landed
    # in the meantime, the grouped path takes over.
    del sb.rpc_error[fo.COUNTS_RPC]
    fo._db_cache = None
    snap = fo.db_snapshot(NOW + fo.RPC_MISSING_TTL_S + 1)
    assert sum(1 for n, _p in sb.rpcs if n == fo.COUNTS_RPC) == 2
    assert _persona_calls(sb) == 2 * 3 * len(sb.orgs)
    assert snap["counts"] == EXPECTED


def test_transient_rpc_error_falls_back_this_refresh_only(sb):
    sb.rpc_error[fo.COUNTS_RPC] = "connection reset"
    assert fo.db_snapshot(NOW)["counts"] == EXPECTED
    assert _persona_calls(sb) == 3 * len(sb.orgs)
    del sb.rpc_error[fo.COUNTS_RPC]
    fo._db_cache = None
    fo.db_snapshot(NOW + fo.DB_CACHE_S + 1)
    assert sum(1 for n, _p in sb.rpcs if n == fo.COUNTS_RPC) == 2  # retried at once
    assert _persona_calls(sb) == 3 * len(sb.orgs)  # no more head counts


def test_missing_table_yields_nulls_on_both_paths(sb):
    sb.rpc_error[fo.COUNTS_RPC] = 'relation "personas" does not exist'
    sb.personas_error = 'relation "personas" does not exist'
    snap = fo.db_snapshot(NOW)
    nulls = {"persona_count": None, "clone_count": None, "templates": None}
    nulls["active_roster_size"] = None
    assert snap["counts"] == {o["id"]: nulls for o in sb.orgs}


def test_rpc_rows_with_no_org_id_are_ignored(sb):
    sb.rpc_rows[fo.COUNTS_RPC].append({"org_id": "", "persona_count": 5})
    assert fo.db_snapshot(NOW)["counts"] == EXPECTED


# ── organizations pagination ────────────────────────────────────────────────


def test_org_rows_iterate_pages_of_200(sb):
    sb.orgs = _orgs(450)
    rows = fo._org_rows(sb)
    assert [r["org_id"] for r in rows] == [o["id"] for o in sb.orgs]
    assert _org_calls(sb) == [(0, 199), (200, 399), (400, 599)]
    assert all(("order", "id") in f for t, f in sb.calls if t == "organizations")


def test_org_rows_exact_multiple_reads_one_empty_page(sb):
    sb.orgs = _orgs(200)
    assert len(fo._org_rows(sb)) == 200
    assert _org_calls(sb) == [(0, 199), (200, 399)]


def test_org_rows_failed_page_returns_none_not_a_short_fleet(sb):
    sb.orgs = _orgs(250)
    sb.fail_pages = {1}
    assert fo._org_rows(sb) is None
    snap = fo.db_snapshot(NOW)
    assert snap["orgs"] is None and snap["counts"] == {}
    assert sum(1 for n, _p in sb.rpcs if n == fo.COUNTS_RPC) == 0  # nothing to count


def test_snapshot_counts_every_org_across_pages_with_one_rpc(sb):
    sb.orgs = _orgs(401)
    snap = fo.db_snapshot(NOW)
    assert len(snap["orgs"]) == 401 and len(snap["counts"]) == 401
    assert snap["counts"]["org-0000"]["templates"] == 3
    assert snap["counts"]["org-0400"]["persona_count"] == 0
    assert sum(1 for n, _p in sb.rpcs if n == fo.COUNTS_RPC) == 1
    assert _persona_calls(sb) == 0
    assert len(_org_calls(sb)) == 3
