"""The time-based half of agent_jobs repair.

reconcile() can only fix a row the local JSON JobStore still remembers, and that
store is trimmed to its most recent entries. Production carried four rows stuck at
state='running' since 2026-07-03 and nineteen at 'awaiting_approval' since
2026-07-02 — none of which any code path could close, because their local records
were long gone. reap_stale() works off updated_at alone.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from brain import agent_jobs_store as store


class _Update:
    def __init__(self, rows, sink):
        self.rows, self.sink, self.f = rows, sink, {}

    def eq(self, col, val):
        self.f[col] = val
        return self

    def lt(self, col, val):
        self.f["__lt_" + col] = val
        return self

    def execute(self):
        hit = [
            r
            for r in self.rows
            if r["org_id"] == self.f.get("org_id")
            and r["state"] == self.f.get("state")
            and r["updated_at"] < self.f["__lt_updated_at"]
        ]
        for r in hit:
            r.update(self.sink["patch"])
        self.sink["calls"].append((self.f.get("state"), len(hit)))
        return type("R", (), {"data": hit})()


@pytest.fixture
def rows_and_store(monkeypatch):
    now = _dt.datetime(2026, 9, 20, 12, 0, tzinfo=_dt.UTC)

    def iso(**kw):
        return (now - _dt.timedelta(**kw)).isoformat()

    rows = [
        {"job_id": "old_running", "org_id": "o", "state": "running", "updated_at": iso(days=60)},
        {"job_id": "fresh_running", "org_id": "o", "state": "running", "updated_at": iso(minutes=5)},
        {"job_id": "old_appr", "org_id": "o", "state": "awaiting_approval", "updated_at": iso(days=30)},
        {"job_id": "fresh_appr", "org_id": "o", "state": "awaiting_approval", "updated_at": iso(hours=2)},
        {"job_id": "deferred", "org_id": "o", "state": "deferred", "updated_at": iso(days=60)},
        {"job_id": "budget", "org_id": "o", "state": "stopped_budget", "updated_at": iso(days=60)},
        {"job_id": "done", "org_id": "o", "state": "completed", "updated_at": iso(days=60)},
        {"job_id": "other_org", "org_id": "ZZ", "state": "running", "updated_at": iso(days=60)},
    ]
    sink = {"calls": [], "patch": {}}

    class _T:
        def update(self, patch):
            sink["patch"] = patch
            return _Update(rows, sink)

    client = type("C", (), {"table": lambda self, n: _T()})()
    monkeypatch.setattr(store, "_sb", lambda: (client, "o"))
    return rows, sink, now


def test_reaps_only_rows_that_can_no_longer_progress(rows_and_store):
    rows, sink, now = rows_and_store
    n = store.reap_stale(now=now.timestamp())
    by_id = {r["job_id"]: r for r in rows}

    assert n == 2
    assert by_id["old_running"]["state"] == "failed"
    assert by_id["old_running"]["reason_code"] == "stale_running"
    assert by_id["old_appr"]["state"] == "failed"
    assert by_id["old_appr"]["reason_code"] == "approval_expired"

    # Still live, or already settled — untouched.
    for jid in ("fresh_running", "fresh_appr", "deferred", "budget", "done"):
        assert by_id[jid]["state"] != "failed", jid


def test_never_touches_another_org(rows_and_store):
    rows, _, now = rows_and_store
    store.reap_stale(now=now.timestamp())
    assert {r["job_id"]: r for r in rows}["other_org"]["state"] == "running"


def test_deferred_and_budget_stops_are_left_for_their_own_paths(rows_and_store):
    """A deferral has its own backoff and will retry; a budget stop is waiting for
    tomorrow, not for a worker. Reaping either would destroy live work."""
    _, sink, now = rows_and_store
    store.reap_stale(now=now.timestamp())
    assert {state for state, _ in sink["calls"]} == {"running", "awaiting_approval"}


def test_every_reaped_row_carries_an_owner_facing_reason(rows_and_store):
    rows, _, now = rows_and_store
    store.reap_stale(now=now.timestamp())
    for r in rows:
        if r.get("reason_code") in ("stale_running", "approval_expired"):
            assert r["reason_human"] and not r["reason_human"].isupper()
            assert r["completed_at"], "a settled row needs a completed_at"


def test_no_supabase_is_a_silent_no_op(monkeypatch):
    monkeypatch.setattr(store, "_sb", lambda: None)
    assert store.reap_stale() == 0


def test_thresholds_match_the_console_alert():
    """fleet_alerts already flagged exactly these two shapes; the reaper must agree
    with what the console told the operator, or they disagree on screen."""
    from brain import fleet_alerts

    assert store.STALE_RUNNING_S == fleet_alerts.STUCK_RUNNING_S
    assert store.STALE_APPROVAL_S == fleet_alerts.STUCK_APPROVAL_S


def test_boot_reaps_even_without_a_local_job_store():
    """The stuck rows in production had no local record left, which is exactly when
    the reaper matters — so it must not be nested under `if _job_store is not None`."""
    from pathlib import Path

    import brain.session_setup as ss

    src = Path(ss.__file__).read_text()
    i = src.index("_reconcile_jobs")
    body = src[i : i + 1400]
    assert "reap_stale" in body
    guard = body.index("if _job_store is not None")
    assert body.index("reap_stale") > guard
    assert body[guard:].index("reap_stale") > body[guard:].index("reconcile")
