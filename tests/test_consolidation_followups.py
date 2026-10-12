"""Follow-ups to the cut-short consolidation fix (prod 2026-10-11).

- Turns replayed at boot consolidate soon after boot (pod up, user quiet), not at
  the 2 h idle pass or the next shutdown, where they could be cut again.
- A pass overlaps its independent LLM calls (fact synthesis ‖ personality
  observer, then self-model ‖ REM thoughts) while its writes keep the old order.
- A learning narration a cut-short tail never ran is queued and narrated later.
- The Fleet console sees consolidation state, and warns on a backlog or failure.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain import fleet_alerts
from brain.observability import trace_journal
from brain.observability.timeline import TurnTrace
from brain.settings import settings
from brain.sleep import SleepConsolidation


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "tenant"
    r.mkdir(parents=True)
    monkeypatch.setenv("SECOND_BRAIN_PATH", str(r))
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "the_admin")
    monkeypatch.delenv("BRAIN_TRACE_JOURNAL", raising=False)
    return r


# ── boot replay ────────────────────────────────────────────────────────────────


def _loops(router_lite=False):
    from brain.session_loops import _LoopsMixin

    class _Stub(_LoopsMixin):
        pass

    s = _Stub()
    s.router = MagicMock()
    s.router._local_disabled = router_lite
    s._replay_pending = True
    s._replayed_turns = 24
    return s


@pytest.fixture
def pod_up(monkeypatch):
    monkeypatch.setitem(settings._data, "runpod_pod_ready", 1)
    monkeypatch.setitem(settings._data, "runpod_pod_ready_at", time.time())
    monkeypatch.setitem(settings._data, "sleep_replay_after_boot", 1)
    monkeypatch.setitem(settings._data, "sleep_replay_settle_s", 60.0)


def test_replayed_turns_consolidate_once_the_pod_is_up_and_the_user_quiet(pod_up):
    s = _loops()
    assert s._boot_replay_reason(buffered=24, idle_for=120) == "boot_replay"


def test_replay_waits_for_a_quiet_moment(pod_up):
    assert _loops()._boot_replay_reason(buffered=24, idle_for=10) is None


def test_replay_waits_for_the_pod(pod_up, monkeypatch):
    monkeypatch.setitem(settings._data, "runpod_pod_ready", 0)
    assert _loops()._boot_replay_reason(buffered=24, idle_for=120) is None
    # A stale ready stamp is not up either.
    monkeypatch.setitem(settings._data, "runpod_pod_ready", 1)
    monkeypatch.setitem(settings._data, "runpod_pod_ready_at", time.time() - 3600)
    assert _loops()._boot_replay_reason(buffered=24, idle_for=120) is None


def test_lite_brain_needs_no_pod(pod_up, monkeypatch):
    monkeypatch.setitem(settings._data, "runpod_pod_ready", 0)
    assert _loops(router_lite=True)._boot_replay_reason(buffered=3, idle_for=120) == "boot_replay"


def test_replay_ignores_min_turns_and_has_a_kill_switch(pod_up, monkeypatch):
    s = _loops()
    assert s._boot_replay_reason(buffered=1, idle_for=120) == "boot_replay"
    monkeypatch.setitem(settings._data, "sleep_replay_after_boot", 0)
    assert s._boot_replay_reason(buffered=1, idle_for=120) is None


def test_empty_buffer_clears_the_replay(pod_up):
    s = _loops()
    assert s._boot_replay_reason(buffered=0, idle_for=120) is None
    assert s._replay_pending is False


def test_any_pass_takes_the_replayed_turns(root):
    from brain.session_loops import _LoopsMixin

    class _Stub(_LoopsMixin):
        def __init__(self):
            self._session_traces = [{"user_input": "x"}]
            self._session_traces_full = []
            self._last_consolidation_ts = 0.0
            self._replay_pending = True
            self._last_consolidation_ok = None
            self._sleep = MagicMock()
            self._sleep.consolidate = AsyncMock(return_value=None)
            self._consolidation_lock = asyncio.Lock()
            self.dmn = None
            self.hippocampus = MagicMock()
            self.hippocampus._schema.read = MagicMock(return_value=None)
            self.parietal = MagicMock()
            self.parietal.save_style_to_schema = AsyncMock(return_value=None)
            self.session_id = "sess"

    st = _Stub()
    out = asyncio.run(st.consolidate_now(reason="idle_7200s"))
    assert out["ok"] and st._replay_pending is False and st._last_consolidation_ok is True


# ── overlapped LLM calls, ordered writes ───────────────────────────────────────


def _timed_sleep(delay: float, events: list[str]):
    s = SleepConsolidation.__new__(SleepConsolidation)
    s._router = MagicMock()
    s._hebbian = None
    s._episodic = MagicMock()
    s._org_isolated = lambda: False

    def _cell(name: str, reply: str):
        c = MagicMock()
        c.reset_turn = MagicMock()

        async def _call(messages):
            events.append(f"{name}:start")
            await asyncio.sleep(delay)
            events.append(f"{name}:end")
            return reply

        c.call = AsyncMock(side_effect=_call)
        return c

    s._synthesizer = _cell(
        "synth", json.dumps({"user_facts": ["likes tea"], "topic_clusters": ["tea"]})
    )
    s._personality_observer = _cell("observer", json.dumps({"communication_style": "- terse"}))
    s._self_updater = _cell("self", "{}")
    s._thought_consolidator = _cell("thought", json.dumps({"preoccupations_digest": "tea again"}))

    schema = MagicMock()
    schema.read = MagicMock(return_value="")
    schema.ensure_speaker_schema = MagicMock(side_effect=lambda sp: f"{sp}.md")

    async def _append(fname, fact):
        events.append(f"write:fact:{fname}")

    async def _upsert(fname, section, body):
        events.append(f"write:upsert:{fname}")

    schema.aappend_fact = AsyncMock(side_effect=_append)
    schema.upsert_section = AsyncMock(side_effect=_upsert)
    schema.awrite = AsyncMock()
    s._schema = schema

    async def _cross(traces):
        events.append("cross:start")
        await asyncio.sleep(delay)
        events.append("cross:end")

    s._cross_learning_pass = _cross
    return s


_TURNS = [
    {"user_input": f"turn {i}", "entity_response": "ok", "speaker_name": "russ"} for i in range(4)
]
_THOUGHTS = [{"angle": "tea", "salient": True, "thought": "why tea"}]


@pytest.fixture
def batch_settings(monkeypatch):
    monkeypatch.setitem(settings._data, "cross_learning", 1)
    monkeypatch.setitem(settings._data, "enable_relationship_stage_progression", 0)
    monkeypatch.setitem(settings._data, "self_model_deid", 0)


def _run_batch(s):
    t0 = time.monotonic()
    asyncio.run(s._consolidate_persona_batch("s1", list(_TURNS), list(_THOUGHTS)))
    return time.monotonic() - t0


def test_independent_calls_overlap_and_writes_keep_their_order(batch_settings, monkeypatch):
    monkeypatch.setitem(settings._data, "sleep_parallel_calls", 1)
    events: list[str] = []
    took = _run_batch(_timed_sleep(0.3, events))
    # Serial is five 0.3 s steps (synth, observer, self, cross, thought); overlapped,
    # synth ‖ observer and self ‖ thought (‖ cross) make it about three.
    assert took < 1.25, took
    assert events.index("observer:start") < events.index("synth:end")
    assert events.index("thought:start") < events.index("self:end")
    # Writes: facts, then the observer's upsert; the thought digest lands last,
    # after cross-learning, exactly as in the serial pass.
    writes = [e for e in events if e.startswith("write:") or e == "cross:end"]
    assert writes == [
        "write:fact:russ.md",
        "write:upsert:russ.md",
        "cross:end",
        "write:fact:self.md",
    ]


def test_kill_switch_runs_every_call_in_sequence(batch_settings, monkeypatch):
    monkeypatch.setitem(settings._data, "sleep_parallel_calls", 0)
    events: list[str] = []
    took = _run_batch(_timed_sleep(0.2, events))
    assert took >= 1.0
    starts_ends = [e for e in events if e.endswith((":start", ":end"))]
    for a, b in zip(starts_ends[::2], starts_ends[1::2], strict=True):
        assert a.split(":")[0] == b.split(":")[0], "calls never interleave"
    writes = [e for e in events if e.startswith("write:")]
    assert writes == ["write:fact:russ.md", "write:upsert:russ.md", "write:fact:self.md"]


def test_a_failed_self_update_cancels_the_overlapped_thought_call(batch_settings, monkeypatch):
    monkeypatch.setitem(settings._data, "sleep_parallel_calls", 1)
    events: list[str] = []
    s = _timed_sleep(0.3, events)
    s._self_updater.call = AsyncMock(side_effect=RuntimeError("pod gone"))
    with pytest.raises(RuntimeError):
        asyncio.run(s._consolidate_persona_batch("s1", list(_TURNS), list(_THOUGHTS)))
    assert "thought:end" not in events and "write:fact:self.md" not in events
    assert getattr(s, "_thought_write_gate", None) is None


# ── deferred narration ─────────────────────────────────────────────────────────


def _tail_sleep():
    s = SleepConsolidation.__new__(SleepConsolidation)
    s._batch_personas = lambda traces: {"the_admin"}
    s.angle_synonym_pass = AsyncMock()
    s.chunk_mining_pass = AsyncMock()
    s.learning_story_pass = AsyncMock()
    s.authoring_pass = AsyncMock()
    s.prune_idle_episodes = MagicMock(return_value=0)
    return s


def test_a_cut_tail_leaves_its_narration_for_the_next_pass(root, monkeypatch):
    monkeypatch.setitem(settings._data, "learning_narrator", 1)
    cut = _tail_sleep()
    cut.chunk_mining_pass = AsyncMock(side_effect=asyncio.CancelledError)  # killed before 6
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cut._consolidate_tail("old_sess", []))
    cut.learning_story_pass.assert_not_awaited()

    nxt = _tail_sleep()
    asyncio.run(nxt._consolidate_tail("new_sess", []))
    narrated = [c.args[0] for c in nxt.learning_story_pass.await_args_list]
    assert narrated == ["new_sess", "old_sess"]
    assert nxt.learning_story_pass.await_args_list[1].kwargs["personas"] == {"the_admin"}

    # Narrated once: a third pass has nothing deferred.
    third = _tail_sleep()
    asyncio.run(third._consolidate_tail("third", []))
    assert [c.args[0] for c in third.learning_story_pass.await_args_list] == ["third"]


def test_stale_deferred_narrations_expire(root, monkeypatch):
    from brain import sleep as sleep_mod

    monkeypatch.setitem(settings._data, "learning_narrator", 1)
    s = _tail_sleep()
    s._save_narrations(
        [{"session_id": "ancient", "personas": None, "ts": time.time() - 30 * 86400}]
    )
    assert s._pending_narrations() == []
    assert sleep_mod._PENDING_NARRATION_TTL_S < 30 * 86400


def test_narrator_off_queues_nothing(root, monkeypatch):
    monkeypatch.setitem(settings._data, "learning_narrator", 0)
    s = _tail_sleep()
    asyncio.run(s._consolidate_tail("sess", []))
    s.learning_story_pass.assert_not_awaited()
    assert s._pending_narrations() == []


# ── Fleet visibility ───────────────────────────────────────────────────────────


def test_consolidation_signals_report_the_backlog(root):
    from brain.session_loops import _LoopsMixin

    class _Stub(_LoopsMixin):
        pass

    s = _Stub()
    old = TurnTrace(turn_id="t1", session_id="s", user_input="hi", ts=time.time() - 7200)
    s._session_traces = [{"turn_id": "t1"}]
    s._session_traces_full = [old]
    s._replayed_turns = 1
    s._replay_pending = True
    s._last_consolidation_ok = False
    s._last_consolidation_ts = time.time() - 60
    trace_journal.append(old, {"turn_id": "t1"})
    trace_journal.rotate_inflight()  # a pass that never committed

    sig = s.consolidation_signals()
    assert sig["buffered_turns"] == 1 and sig["replay_pending"] is True
    assert 7100 < sig["oldest_unconsolidated_s"] < 7300
    assert sig["uncommitted_batches"] == 1 and sig["last_pass_ok"] is False
    assert 50 < sig["last_pass_age_s"] < 120


def test_fleet_warns_on_a_day_old_backlog_and_a_failed_pass():
    codes = {
        a["code"]
        for a in fleet_alerts.evaluate(
            {
                "consolidation": {
                    "oldest_unconsolidated_s": 30 * 3600,
                    "buffered_turns": 24,
                    "last_pass_ok": False,
                }
            }
        )
    }
    assert {"consolidation_backlog", "consolidation_failed"} <= codes


def test_fleet_quiet_when_consolidation_is_healthy():
    alerts = fleet_alerts.evaluate(
        {"consolidation": {"oldest_unconsolidated_s": 600, "last_pass_ok": True}}
    )
    assert not [a for a in alerts if a["code"].startswith("consolidation")]
