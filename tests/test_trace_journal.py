"""Crash-safe trace journal: durability + boot replay of un-consolidated turns.

The Hebbian fired-path traces that drive turn learning live only in RAM until a
consolidation runs; a graceful exit commits them, an ungraceful one (OOM/SIGKILL)
would drop them. The journal makes them durable and replays leftovers on boot,
preserving per-persona attribution (each TurnTrace carries its own persona_name),
so a lite agent's conversation learning survives a crash exactly like a full one's.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.observability import trace_journal
from brain.observability.timeline import TurnTrace


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "tenant"
    r.mkdir(parents=True)
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(r))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "the_visionary")
    monkeypatch.delenv("BRAIN_TRACE_JOURNAL", raising=False)
    return r


def _trace(turn_id="t1", persona="the_analyst"):
    return TurnTrace(
        turn_id=turn_id,
        session_id="s1",
        user_input="hi",
        persona_name=persona,
        fired_path=[{"name": "temporal.curiosity", "cluster": "temporal", "kind": "switch"}],
        neuromod={"DA": 0.6},
        prior_neuromod={"DA": 0.5},
    )


def test_append_then_load_roundtrips_trace_and_summary(root):
    trace_journal.append(_trace("t1"), {"user_input": "hi", "intent": "chat"})
    assert (root / "pending_traces.jsonl").exists()

    fulls, sums = trace_journal.load_orphans()
    assert len(fulls) == 1 and len(sums) == 1
    # Attribution + Hebbian-relevant fields survive the JSON round trip.
    assert isinstance(fulls[0], TurnTrace)
    assert fulls[0].persona_name == "the_analyst"
    assert fulls[0].turn_id == "t1"
    assert fulls[0].fired_path[0]["name"] == "temporal.curiosity"
    assert fulls[0].prior_neuromod == {"DA": 0.5}
    assert sums[0]["intent"] == "chat"


def _inflight(root):
    return sorted(root.glob("pending_traces.inflight*.jsonl"))


def test_successful_consolidation_clears_the_journal(root):
    trace_journal.append(_trace("t1"), {"user_input": "hi"})
    token = trace_journal.rotate_inflight()  # pass starts: pending -> its own inflight
    assert token and (root / token).exists()
    assert not (root / "pending_traces.jsonl").exists()
    trace_journal.clear_inflight(token)  # pass succeeded
    assert _inflight(root) == []
    # Nothing left → next boot replays nothing.
    assert trace_journal.load_orphans() == ([], [])


def test_crash_before_consolidation_replays_pending(root):
    # Turns buffered, then the process dies before any consolidation: the pending
    # file is all that's left, and boot replay must recover it.
    trace_journal.append(_trace("t1"), {})
    trace_journal.append(_trace("t2"), {})
    fulls, _ = trace_journal.load_orphans()
    assert [t.turn_id for t in fulls] == ["t1", "t2"]


def test_crash_during_consolidation_replays_inflight(root):
    # rotate ran (pass started) but the process died before clear: the batch lives
    # in the inflight file and must be recovered, not lost.
    trace_journal.append(_trace("t1"), {})
    trace_journal.rotate_inflight()
    assert len(_inflight(root)) == 1
    fulls, _ = trace_journal.load_orphans()
    assert [t.turn_id for t in fulls] == ["t1"]


def test_load_orphans_restages_for_a_second_crash(root):
    # After boot replay puts traces back in the buffers, they must remain durable:
    # if the brain crashes AGAIN before consolidating, a second boot still finds them.
    trace_journal.append(_trace("t1"), {})
    first, _ = trace_journal.load_orphans()
    assert [t.turn_id for t in first] == ["t1"]
    second, _ = trace_journal.load_orphans()  # simulate a second crash+boot
    assert [t.turn_id for t in second] == ["t1"]


def test_rotate_keeps_a_leftover_inflight_alongside_a_new_one(root):
    # An inflight from an earlier pass coexisting with a new pending batch: a fresh
    # rotate must not drop either.
    trace_journal.append(_trace("old"), {})
    trace_journal.rotate_inflight()
    trace_journal.append(_trace("new"), {})
    trace_journal.rotate_inflight()
    assert len(_inflight(root)) == 2
    fulls, _ = trace_journal.load_orphans()
    assert sorted(t.turn_id for t in fulls) == ["new", "old"]


def test_a_later_success_never_clears_an_earlier_failed_batch(root):
    # Pass 1 raised (its batch was never committed); pass 2 succeeds. Pass 2's
    # clear must drop only its own batch — the failed one still replays on boot.
    # (The old shared inflight file merged both and pass 2 deleted pass 1's turns.)
    trace_journal.append(_trace("failed"), {})
    trace_journal.rotate_inflight()  # pass 1 — no clear (it raised)
    trace_journal.append(_trace("ok"), {})
    tok2 = trace_journal.rotate_inflight()
    trace_journal.clear_inflight(tok2)
    fulls, _ = trace_journal.load_orphans()
    assert [t.turn_id for t in fulls] == ["failed"]


def test_clear_none_or_foreign_token_is_a_noop(root):
    trace_journal.append(_trace("t1"), {})
    trace_journal.rotate_inflight()
    trace_journal.clear_inflight(None)
    trace_journal.clear_inflight("../pending_traces.jsonl")
    trace_journal.clear_inflight("settings.json")
    assert len(_inflight(root)) == 1


def test_legacy_single_inflight_file_still_replays(root):
    # A batch an older build left in the fixed-name inflight file must survive the
    # upgrade: boot replay reads it like any per-pass file.
    trace_journal.append(_trace("legacy"), {})
    (root / "pending_traces.jsonl").replace(root / "pending_traces.inflight.jsonl")
    fulls, _ = trace_journal.load_orphans()
    assert [t.turn_id for t in fulls] == ["legacy"]


def test_scrub_end_user_reaches_every_inflight_file(root):
    tr = _trace("a")
    tr.end_user_id = "eu-1"
    trace_journal.append(tr, {"end_user_id": "eu-1"})
    trace_journal.rotate_inflight()
    trace_journal.append(tr, {"end_user_id": "eu-1"})
    assert trace_journal.scrub_end_user("eu-1") == 2
    assert trace_journal.load_orphans() == ([], [])


# ── regression: end-of-session consolidation must journal its batch out ───────
# 2026-10-10 (tenant 0f3c064f): every graceful restart logged "[trace_journal]
# Replayed 8 orphaned turn trace(s)" and then re-consolidated the same 8 turns at
# shutdown with identical Hebbian numbers — the shutdown pass never rotated/cleared
# the journal, and boot replay had re-staged it as `pending`, so it never died.


def _shutdown_session(full_traces, summaries):
    from brain.brain_session import BrainSession

    s = BrainSession.__new__(BrainSession)
    s._session_traces = list(summaries)
    s._session_traces_full = list(full_traces)
    s._sleep = MagicMock()
    s._sleep.consolidate = AsyncMock(return_value=None)
    s.dmn = None
    s.session_id = "sess"
    s.persona_name = "the_empath"
    return s


def test_shutdown_consolidation_journals_the_batch_out_exactly_once(root):
    trace_journal.append(_trace("t1"), {"user_input": "hi"})
    trace_journal.append(_trace("t2"), {"user_input": "there"})

    # Boot 1: replay (as session_setup does), then a graceful shutdown.
    fulls, sums = trace_journal.load_orphans()
    assert [t.turn_id for t in fulls] == ["t1", "t2"]
    s1 = _shutdown_session(fulls, sums)
    asyncio.run(s1._shutdown_consolidate())
    s1._sleep.consolidate.assert_awaited_once()

    # Boot 2: nothing left to replay — the batch was consolidated exactly once.
    assert trace_journal.load_orphans() == ([], [])
    assert _inflight(root) == [] and not (root / "pending_traces.jsonl").exists()


def test_shutdown_consolidation_failure_keeps_the_batch_for_next_boot(root):
    trace_journal.append(_trace("t1"), {})
    fulls, sums = trace_journal.load_orphans()
    s = _shutdown_session(fulls, sums)
    s._sleep.consolidate = AsyncMock(side_effect=RuntimeError("llm down"))
    asyncio.run(s._shutdown_consolidate())  # logged, never raised
    fulls2, _ = trace_journal.load_orphans()
    assert [t.turn_id for t in fulls2] == ["t1"]


def test_in_process_consolidation_clears_only_its_own_batch(root):
    from brain.session_loops import _LoopsMixin

    trace_journal.append(_trace("stranded"), {})
    trace_journal.rotate_inflight()  # an earlier pass that never committed

    class _Stub(_LoopsMixin):
        def __init__(self):
            self._session_traces = [{"user_input": "x"}]
            self._session_traces_full = [_trace("fresh")]
            self._last_consolidation_ts = 0.0
            self._sleep = MagicMock()
            self._sleep.consolidate = AsyncMock(return_value=None)
            self._consolidation_lock = asyncio.Lock()
            self.dmn = None
            self.hippocampus = MagicMock()
            self.hippocampus._schema.read = MagicMock(return_value=None)
            self.parietal = MagicMock()
            self.parietal.save_style_to_schema = AsyncMock(return_value=None)
            self.session_id = "sess"

    trace_journal.append(_trace("fresh"), {})
    out = asyncio.run(_Stub().consolidate_now(reason="test"))
    assert out["ran"] and out["ok"]
    fulls, _ = trace_journal.load_orphans()
    assert [t.turn_id for t in fulls] == ["stranded"]


def test_disabled_is_a_noop(root, monkeypatch):
    monkeypatch.setenv("BRAIN_TRACE_JOURNAL", "false")
    trace_journal.append(_trace("t1"), {"user_input": "hi"})
    assert not (root / "pending_traces.jsonl").exists()
    assert trace_journal.load_orphans() == ([], [])
