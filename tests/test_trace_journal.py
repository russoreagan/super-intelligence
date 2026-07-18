"""Crash-safe trace journal: durability + boot replay of un-consolidated turns.

The Hebbian fired-path traces that drive turn learning live only in RAM until a
consolidation runs; a graceful exit commits them, an ungraceful one (OOM/SIGKILL)
would drop them. The journal makes them durable and replays leftovers on boot,
preserving per-persona attribution (each TurnTrace carries its own persona_name),
so a lite agent's conversation learning survives a crash exactly like a full one's.
"""

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


def test_successful_consolidation_clears_the_journal(root):
    trace_journal.append(_trace("t1"), {"user_input": "hi"})
    trace_journal.rotate_inflight()  # pass starts: pending -> inflight
    assert (root / "pending_traces.inflight.jsonl").exists()
    assert not (root / "pending_traces.jsonl").exists()
    trace_journal.clear_inflight()  # pass succeeded
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
    assert (root / "pending_traces.inflight.jsonl").exists()
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


def test_rotate_merges_a_leftover_inflight_instead_of_clobbering(root):
    # Defensive: an inflight from an earlier crash coexisting with a new pending
    # batch must not silently drop either when a fresh pass rotates.
    trace_journal.append(_trace("old"), {})
    trace_journal.rotate_inflight()  # -> inflight has "old"
    trace_journal.append(_trace("new"), {})  # a fresh turn lands in a new pending
    trace_journal.rotate_inflight()  # inflight already exists → merge
    fulls, _ = trace_journal.load_orphans()
    assert sorted(t.turn_id for t in fulls) == ["new", "old"]


def test_disabled_is_a_noop(root, monkeypatch):
    monkeypatch.setenv("BRAIN_TRACE_JOURNAL", "false")
    trace_journal.append(_trace("t1"), {"user_input": "hi"})
    assert not (root / "pending_traces.jsonl").exists()
    assert trace_journal.load_orphans() == ([], [])
