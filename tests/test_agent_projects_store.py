"""brain/agent_projects_store — local backend round trip, lifecycle transitions,
the compare-and-set claim, and the fail-CLOSED rule on the hosted backend."""

from __future__ import annotations

import pytest

from brain import agent_projects_store as store


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("BRAIN_STORAGE_BACKEND", "local")
    store.invalidate_cache()
    store._spend_cache = None
    yield
    store.invalidate_cache()
    store._spend_cache = None


# ── Local backend ───────────────────────────────────────────────────────────


def test_add_is_insert_if_missing_and_deterministic():
    pid = store.add("the_admin", "app_admin", "Agent health sweep", "Read the jobs.")
    assert pid == store.project_id("the_admin", "app_admin", "Agent health sweep")
    assert store.get(pid)["state"] == store.READY
    # A second add with different content leaves the existing row alone.
    again = store.add("the_admin", "app_admin", "Agent health sweep", "Something else", priority=0)
    assert again == pid
    assert store.get(pid)["task"] == "Read the jobs."


def test_list_for_personas_spans_mandates_and_list_for_agent_does_not():
    store.add("the_analyst", "day_trading_analyst", "Journal", "Read the journal.")
    store.add("the_analyst", "trading_mispricing", "Screen", "Screen the tape.")
    store.add("the_admin", "app_admin", "Sweep", "Sweep the jobs.")
    all_analyst = store.list_for_personas(["the_analyst"])
    assert {r["mandate_id"] for r in all_analyst} == {"day_trading_analyst", "trading_mispricing"}
    only_full = store.list_for_agent("the_analyst", "day_trading_analyst")
    assert [r["title"] for r in only_full] == ["Journal"]
    assert len(store.list_for_personas(["the_analyst", "the_admin"])) == 3


def test_local_file_lives_under_second_brain_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "sb"))
    store.invalidate_cache()
    store.add("p", "m", "t", "task")
    assert (tmp_path / "sb" / store.LOCAL_FILENAME).exists()


# ── Lifecycle ───────────────────────────────────────────────────────────────


def test_claim_is_compare_and_set():
    pid = store.add("p", "m", "t", "task")
    assert store.claim(pid, "task-1") is True
    assert store.get(pid)["state"] == store.RUNNING
    # The race: a second claimant loses.
    assert store.claim(pid, "task-2") is False
    assert store.get(pid)["in_flight_task_id"] == "task-1"


def test_release_undoes_a_claim_without_counting_a_run():
    pid = store.add("p", "m", "t", "task")
    store.claim(pid, "t1")
    assert store.release(pid) is True
    row = store.get(pid)
    assert row["state"] == store.READY and row["runs"] == 0 and row["in_flight_task_id"] == ""


def test_one_shot_success_is_done_and_clears_user_waiting():
    pid = store.add("p", "m", "t", "task", user_waiting=True)
    store.claim(pid, "t1")
    assert store.finish(pid, success=True, note="did it", job_id="job_task_t1") == store.DONE
    row = store.get(pid)
    assert row["runs"] == 1 and row["user_waiting"] is False and row["last_job_id"] == "job_task_t1"
    assert row["in_flight_task_id"] == "" and row["status_note"] == "did it"


def test_recurring_success_goes_to_the_back_of_the_queue():
    pid = store.add("p", "m", "t", "task", max_runs=0)
    before = store.get(pid)["ready_at"]
    store.claim(pid, "t1")
    assert store.finish(pid, success=True) == store.READY
    assert store.get(pid)["ready_at"] >= before


def test_failure_backs_off_then_quarantines():
    pid = store.add("p", "m", "t", "task")
    for n in range(1, store.MAX_FAILURES):
        store.claim(pid, f"t{n}")
        assert store.finish(pid, success=False, note="boom") == store.PENDING
        row = store.get(pid)
        assert row["consecutive_failures"] == n and row["deferred_until"] > row["updated_at"]
        # Backoff doubles.
        assert (
            row["deferred_until"] - row["updated_at"] >= store.DEFAULT_BACKOFF_S * 2 ** (n - 1) - 1
        )
    store.claim(pid, "last")
    assert store.finish(pid, success=False) == store.FAILED


def test_pending_rows_are_claimable():
    pid = store.add("p", "m", "t", "task", state=store.PENDING)
    assert store.claim(pid, "t1") is True


def test_block_and_unblock():
    pid = store.add("p", "m", "t", "task")
    store.claim(pid, "t1")
    assert store.block(pid, "which dir?")
    row = store.get(pid)
    assert row["state"] == store.BLOCKED and row["blocked_reason"] == "which dir?"
    assert store.unblock(pid)
    row = store.get(pid)
    assert (
        row["state"] == store.READY and row["user_waiting"] is True and row["blocked_reason"] == ""
    )
    # unblock is a no-op on a row that isn't blocked.
    assert store.unblock(pid) is False


def test_upsert_content_never_touches_lifecycle():
    rec = store.new_record("p", "m", "Title", "old task", state=store.READY)
    assert store.upsert_content(rec)
    pid = rec["id"]
    store.claim(pid, "t1")
    store.finish(pid, success=True)  # → done
    rec2 = store.new_record("p", "m", "Title", "new task", priority=0, state=store.READY)
    assert store.upsert_content(rec2)
    row = store.get(pid)
    assert row["task"] == "new task" and row["priority"] == 0
    assert row["state"] == store.DONE and row["runs"] == 1


def test_clear_in_flight_frees_only_stale_running_rows():
    a = store.add("p", "m", "a", "task a")
    b = store.add("p", "m", "b", "task b")
    store.claim(a, "t1")
    # lease_s=0 → every claim counts as orphaned (the old, unconditional behaviour).
    assert store.clear_in_flight(["p"], lease_s=0) == 1
    assert store.get(a)["state"] == store.READY and store.get(b)["state"] == store.READY
    assert store.clear_in_flight(["p"], lease_s=0) == 0


def test_clear_in_flight_does_not_steal_a_live_claim():
    """next_project()'s compare-and-set promises two processes serving one org cannot
    both start the same project. An unconditional reset broke that: on a redeploy the
    old instance is still mid-step when the new one flips the row and re-claims it,
    and the same paid job runs twice."""
    pid = store.add("p", "m", "a", "task a")
    store.claim(pid, "t1")
    assert store.clear_in_flight(["p"]) == 0, "a claim made seconds ago was stolen"
    assert store.get(pid)["state"] == store.RUNNING
    assert store.get(pid)["in_flight_task_id"] == "t1"


def test_clear_in_flight_still_repairs_a_claim_past_its_lease():
    """The repair this exists for: a pod that died mid-step, whose claim nobody is
    working on any more."""
    pid = store.add("p", "m", "a", "task a")
    store.claim(pid, "t1")
    assert store.clear_in_flight(["p"], lease_s=store.CLAIM_LEASE_S) == 0
    # Age the claim past the lease.
    assert store.clear_in_flight(["p"], lease_s=-1) == 1
    assert store.get(pid)["state"] == store.READY
    assert store.get(pid)["in_flight_task_id"] == ""


def test_cache_is_invalidated_by_writes():
    pid = store.add("p", "m", "t", "task")
    assert store.list_for_personas(["p"])[0]["state"] == store.READY
    store.claim(pid, "t1")
    assert store.list_for_personas(["p"])[0]["state"] == store.RUNNING


# ── Hosted backend: org scoping + fail closed ──────────────────────────────


class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
    """A tiny fluent fake that records the chain and answers from `rows`."""

    def __init__(self, rows, log):
        self._rows, self._log = rows, log
        self._filters, self._in = [], []
        self._op, self._payload = None, None

    def select(self, *_):
        self._op = "select"
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None):
        self._op, self._payload = "upsert", payload
        self._log.append(("upsert", on_conflict))
        return self

    def eq(self, k, v):
        self._filters.append((k, v))
        return self

    def in_(self, k, vs):
        self._in.append((k, list(vs)))
        return self

    def limit(self, *_):
        return self

    def _match(self, r):
        return all(r.get(k) == v for k, v in self._filters) and all(
            r.get(k) in vs for k, vs in self._in
        )

    def execute(self):
        self._log.append((self._op, list(self._filters)))
        if self._op == "select":
            return _Res([dict(r) for r in self._rows if self._match(r)])
        if self._op == "update":
            hit = [r for r in self._rows if self._match(r)]
            for r in hit:
                r.update(self._payload)
            return _Res([dict(r) for r in hit])
        if self._op == "upsert":
            self._rows.append(dict(self._payload))
            return _Res([dict(self._payload)])
        raise AssertionError(self._op)


class _Client:
    def __init__(self, rows, log):
        self.rows, self.log = rows, log

    def table(self, name):
        assert name == store.TABLE
        return _Q(self.rows, self.log)


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setenv("BRAIN_STORAGE_BACKEND", "supabase")
    rows, log = [], []
    monkeypatch.setattr(store, "_sb", lambda: (_Client(rows, log), "org-1"))
    store.invalidate_cache()
    return rows, log


def test_hosted_chains_are_org_scoped(hosted):
    rows, log = hosted
    pid = store.add("p", "m", "t", "task")
    assert pid
    assert ("upsert", "id,org_id") in log
    assert rows[0]["org_id"] == "org-1"
    store.list_for_personas(["p"])
    store.claim(pid, "t1")
    for op, filters in log:
        if op in ("select", "update"):
            assert ("org_id", "org-1") in filters, (op, filters)


def test_hosted_claim_race_exactly_one_winner(hosted):
    rows, _ = hosted
    pid = store.add("p", "m", "t", "task")
    assert store.claim(pid, "a") is True
    assert store.claim(pid, "b") is False
    assert rows[0]["in_flight_task_id"] == "a"


def test_hosted_times_round_trip_as_iso(hosted):
    rows, _ = hosted
    pid = store.add("p", "m", "t", "task")
    assert isinstance(rows[0]["ready_at"], str) and rows[0]["ready_at"].endswith("+00:00")
    assert isinstance(store.get(pid)["ready_at"], float)


def test_hosted_failure_is_closed_never_local(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path / "sb"))
    monkeypatch.setattr(store, "_sb", lambda: None)
    store.invalidate_cache()
    assert store.list_for_personas(["p"]) == []
    assert store.add("p", "m", "t", "task") == ""
    assert store.claim("p-whatever", "t") is False
    assert store.clear_in_flight(["p"]) == 0
    assert not (tmp_path / "sb" / store.LOCAL_FILENAME).exists()
