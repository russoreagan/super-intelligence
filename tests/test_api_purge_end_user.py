"""
Right-to-erasure has to actually erase.

The API documents this as covering "every per-user table" and being irreversible. It
covered six of roughly a dozen stores, and reported `ok: True` unconditionally — so
nothing ever surfaced the gap. Specifically these survived a "successful" purge:

  • end_user_mcp_tokens — the customer's live third-party OAuth credentials;
  • agent_turns — verbatim prompt and response text;
  • the per-speaker profile in brain_schemas, because the writer stores
    end_user_id='' while the purge filtered on the real id;
  • the durable chemistry snapshot, whose only cleanup path was a docstring saying
    "the caller" would do it (no caller did);
  • pending approvals, which carry the tool_input of the parked action.

The coverage test asserts on the exact set of stores touched, so adding a new
per-end-user table without a purge decision fails here rather than silently.
"""

from __future__ import annotations

import asyncio

import pytest

from brain.session_turn import _TurnMixin


class _Recorder:
    def __init__(self):
        self.deleted: list[str] = []
        self.rpcs: list[str] = []

    def table(self, name):
        self.deleted.append(name)
        return self

    def rpc(self, name, params):
        self.rpcs.append(name)
        return self

    def delete(self):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": []})()


class _Approvals:
    def __init__(self):
        self.forgotten: list[str] = []

    def forget_end_user(self, euid):
        self.forgotten.append(euid)
        return 1


class _ChemReg:
    def __init__(self):
        self.calls: list[tuple[str, bool]] = []

    def forget(self, euid, *, durable=False):
        self.calls.append((euid, durable))


class _JobStore:
    def __init__(self):
        self.purged: list[str] = []

    def purge_end_user(self, euid):
        self.purged.append(euid)
        return 2


class _Trace:
    def __init__(self, euid):
        self.end_user_id = euid


class _Brain(_TurnMixin):
    """Minimal host object — the mixin only touches these attributes."""

    def __init__(self):
        self.persona_name = "the_visionary"
        self.motor = type("M", (), {"job_store": _JobStore()})()
        # Un-consolidated turns for two customers in both trace buffers.
        self._session_traces = [
            {"user_input": "secret", "end_user_id": "u_1"},
            {"user_input": "other", "end_user_id": "u_2"},
        ]
        self._session_traces_full = [_Trace("u_1"), _Trace("u_2"), _Trace("u_1")]
        self._client_chem = _ChemReg()
        # Keyed (persona, end_user_id): one customer, two personas' profile texts.
        self._engine_um_cache = {
            ("the_visionary", "u_1"): object(),
            ("the_analyst", "u_1"): object(),
            ("the_analyst", "u_2"): object(),
        }
        self._approvals = _Approvals()


@pytest.fixture
def brain(monkeypatch):
    from brain.second_brain import supabase_client

    rec = _Recorder()
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)
    monkeypatch.setattr(supabase_client, "get_client", lambda: rec)
    monkeypatch.setattr(supabase_client, "get_org_id", lambda: "org-1")
    b = _Brain()
    return b, rec


def _purge(b, euid="u_1"):
    return asyncio.run(b.api_purge_end_user(euid))


def test_reports_ok_and_a_per_step_summary(brain):
    b, _ = brain
    out = _purge(b)
    assert out["ok"] is True
    assert out["end_user_id"] == "u_1"
    assert isinstance(out["deleted"], dict)


def test_covers_every_known_per_end_user_table(brain):
    """The regression guard. If you add a table keyed by end_user_id, add it to
    _PURGE_TABLES — or consciously decide not to and update this test."""
    b, rec = brain
    _purge(b)
    assert set(_TurnMixin._PURGE_TABLES) <= set(rec.deleted)
    # The two that were missing entirely:
    assert "agent_turns" in rec.deleted
    assert "purge_end_user_mcp_tokens" in rec.rpcs


def test_connector_tokens_go_through_the_vault_rpc(brain):
    """A row delete would orphan the Vault ciphertext rather than remove it."""
    b, rec = brain
    _purge(b)
    assert "purge_end_user_mcp_tokens" in rec.rpcs
    assert "end_user_mcp_tokens" not in rec.deleted


def test_per_speaker_profile_is_purged_by_filename(brain):
    """It is stored with end_user_id='', so the end_user_id filter never reached it."""
    b, rec = brain
    out = _purge(b)
    assert "speaker_schema" in out["deleted"]
    # brain_schemas is hit twice: once by id, once by derived filename.
    assert rec.deleted.count("brain_schemas") >= 2


def test_durable_chemistry_is_removed_not_just_the_live_mood(brain):
    b, _ = brain
    out = _purge(b)
    assert b._client_chem.calls == [("u_1", True)], "forget() must be asked for the durable half"
    assert "chem_snapshots" in out["deleted"]


def test_pending_approvals_are_dropped(brain):
    b, _ = brain
    out = _purge(b)
    assert b._approvals.forgotten == ["u_1"]
    assert out["deleted"]["approvals"] == 1


def test_unconsolidated_turns_and_journal_are_dropped(brain, tmp_path, monkeypatch):
    """The next sleep pass must not relearn an erased customer: both in-memory trace
    buffers and the crash-safe journal lose that customer's lines, nobody else's."""
    from brain.observability import trace_journal
    from brain.observability.timeline import TurnTrace

    monkeypatch.setenv("SECOND_BRAIN_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "the_visionary")
    monkeypatch.delenv("BRAIN_TRACE_JOURNAL", raising=False)
    for tid, euid, text in (("t1", "u_1", "secret"), ("t2", "u_2", "other")):
        tr = TurnTrace(turn_id=tid, session_id="s", user_input=text, end_user_id=euid)
        trace_journal.append(tr, {"user_input": text, "end_user_id": euid})
    b, _ = brain
    out = _purge(b)
    assert out["deleted"]["session_traces"] == 3  # one summary + two full traces
    assert [t["end_user_id"] for t in b._session_traces] == ["u_2"]
    assert [t.end_user_id for t in b._session_traces_full] == ["u_2"]
    assert out["deleted"]["trace_journal"] == 1
    _, sums = trace_journal.load_orphans()
    assert [s["end_user_id"] for s in sums] == ["u_2"]


def test_local_job_files_are_purged_and_skipped_without_motor(brain):
    b, _ = brain
    out = _purge(b)
    assert out["deleted"]["local_jobs"] == 2
    assert b.motor.job_store.purged == ["u_1"]
    b.motor = None
    assert _purge(b)["deleted"]["local_jobs"] == "skipped"


def test_agent_jobs_rows_are_covered(brain):
    b, rec = brain
    _purge(b)
    assert "agent_jobs" in rec.deleted


def test_eval_log_policy_is_reported(brain, monkeypatch):
    b, _ = brain
    monkeypatch.delenv("BRAIN_EVAL_LOG_AGENT_TEXT", raising=False)
    assert _purge(b)["deleted"]["eval_log"] == "redacted_at_write"
    monkeypatch.setenv("BRAIN_EVAL_LOG_AGENT_TEXT", "verbatim")
    assert _purge(b)["deleted"]["eval_log"] == "verbatim"


def test_in_memory_caches_are_cleared_first(brain):
    b, _ = brain
    _purge(b)
    # Every persona's cached profile for u_1 is gone; another customer's survives.
    assert set(b._engine_um_cache) == {("the_analyst", "u_2")}


def test_a_failing_store_makes_ok_false(brain, monkeypatch):
    """Previously every partial failure still reported success."""
    b, rec = brain

    def _boom():
        raise RuntimeError("table gone")

    original = rec.execute
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            _boom()
        return original()

    rec.execute = _flaky
    out = _purge(b)
    assert out["ok"] is False
    assert out["failed"]


def test_empty_id_is_refused(brain):
    b, _ = brain
    assert _purge(b, "   ")["ok"] is False


def test_local_backend_still_erases(monkeypatch):
    """With no Supabase the table loop is a no-op; reporting ok:True without doing
    anything else was a silent non-erasure."""
    from brain.second_brain import supabase_client

    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    b = _Brain()
    out = _purge(b)
    assert "local_schema" in out["deleted"]
    assert b._client_chem.calls == [("u_1", True)]
