"""
Agent-lane isolation — events a turn emits are tagged with their routing lane so
the owner's main feed and each partner's stream stay separate.

These cover the load-bearing mechanism (brain/turn_ctx.py + ActivationEmitter._stamp_lane)
and the predicates the subscribers use to filter: the owner UI keeps only
channel=="owner"; an engine-API stream keeps only its own route_sid.
"""

from __future__ import annotations

import asyncio

from brain.turn_ctx import bind_turn, current_turn
from brain.ui.emitter import ActivationEmitter


def test_default_lane_is_owner():
    # Nothing bound → interactive UI + idle inner life all live in the owner lane.
    assert current_turn()["channel"] == "owner"


def test_stamp_owner_by_default():
    em = ActivationEmitter()
    ev = {"type": "stream_thought", "thought": "hi"}
    em._stamp_lane(ev)
    assert ev["channel"] == "owner"
    assert "route_sid" not in ev  # owner events carry no per-session route


def test_stamp_agent_under_bind():
    em = ActivationEmitter()
    with bind_turn(
        "agent", session_id="sess_A", agent_id="the_visionary.trading_bull", end_user_id="cust-1"
    ):
        ev = {"type": "stream_thought", "thought": "buy AAPL"}
        em._stamp_lane(ev)
    assert ev["channel"] == "agent"
    assert ev["route_sid"] == "sess_A"
    assert ev["agent_id"] == "the_visionary.trading_bull"
    assert ev["end_user_id"] == "cust-1"


def test_routing_id_never_clobbers_cosmetic_session_id():
    # turn_start already carries a cosmetic *process* session_id (shown in a status
    # pill). The routing id lives on a distinct key so the two never collide.
    async def run():
        em = ActivationEmitter()
        with bind_turn("agent", session_id="sess_A"):
            await em.emit_turn_start("t1", "buy AAPL", session_id="proc-xyz")
        return em.get_queue().get_nowait()

    ev = asyncio.run(run())
    assert ev["channel"] == "agent"
    assert ev["route_sid"] == "sess_A"
    assert ev["session_id"] == "proc-xyz"  # untouched


def test_two_streams_do_not_bleed_and_owner_idle_reaches_neither():
    """Two concurrent agent turns + a background owner thought, all through the one
    process-global emitter. Each engine-API stream applies the real _gen predicate
    (route_sid == its session) and must see ONLY its own turn's events."""

    async def run():
        em = ActivationEmitter()
        tap = asyncio.Queue(maxsize=512)
        em.add_tap(tap)

        with bind_turn("agent", session_id="A"):
            await em.emit_turn_start("tA", "A asks", session_id="proc")
            await em.emit_stream_thought("A thinks")
            await em.emit_turn_end("tA", "A answer", 0.1, 1)
        with bind_turn("agent", session_id="B"):
            await em.emit_turn_start("tB", "B asks", session_id="proc")
            await em.emit_stream_thought("B thinks")
            await em.emit_turn_end("tB", "B answer", 0.1, 1)
        # Owner/idle DMN thought: bound to nothing → owner lane.
        await em.emit_stream_thought("owner idle")

        seen = []
        while not tap.empty():
            seen.append(tap.get_nowait())
        return seen

    seen = asyncio.run(run())

    a_stream = [e for e in seen if e.get("route_sid") == "A"]  # _gen filter for A
    b_stream = [e for e in seen if e.get("route_sid") == "B"]  # _gen filter for B
    owner_feed = [e for e in seen if e.get("channel") == "owner"]  # main-feed gate

    assert [e["type"] for e in a_stream] == ["turn_start", "stream_thought", "turn_end"]
    assert [e["type"] for e in b_stream] == ["turn_start", "stream_thought", "turn_end"]
    # No bleed: A's stream never carries B's prompt, and vice versa.
    assert all("B" not in (e.get("user_input", "") + e.get("response", "")) for e in a_stream)
    assert all("A" not in (e.get("user_input", "") + e.get("response", "")) for e in b_stream)
    # The owner's idle thought is in the owner feed and in NEITHER agent stream.
    assert [e["thought"] for e in owner_feed] == ["owner idle"]
    assert not any(e.get("route_sid") in ("A", "B") for e in owner_feed)


def test_deferred_job_captures_agent_origin(tmp_path, monkeypatch):
    """A job deferred DURING an agent-lane turn carries that agent on the task, so
    its later execution can be re-bound to the same lane (two-bucket attribution).
    The brain's OWN idle enqueue (nothing bound) stays owner-lane with no agent."""
    import brain.clusters.task_queue as tq

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "task_queue.json")
    q = tq.PersistentTaskQueue()

    # Deferred while serving an agent (engine-API) turn → tagged with that agent.
    with bind_turn(
        "agent", session_id="sess_A", agent_id="the_analyst.day_trader", end_user_id="cust-1"
    ):
        agent_task = q.enqueue("pull the latest AAPL fills", source="user")
    assert agent_task is not None
    assert agent_task.origin_channel == "agent"
    assert agent_task.origin_session_id == "sess_A"
    assert agent_task.origin_agent_id == "the_analyst.day_trader"
    assert agent_task.origin_end_user_id == "cust-1"

    # The brain's own idle reasoning (nothing bound) → owner lane, no agent identity.
    own_task = q.enqueue("reflect on today's mood", source="self")
    assert own_task is not None
    assert own_task.origin_channel == "owner"
    assert own_task.origin_session_id == ""
    assert own_task.origin_agent_id == ""


def test_task_origin_survives_persist_reload(tmp_path, monkeypatch):
    """Origin attribution is durable: a tagged task reloaded from disk (boot
    recovery / page refresh) still knows which agent it descends from."""
    import brain.clusters.task_queue as tq

    monkeypatch.setattr(tq, "TASK_QUEUE_PATH", tmp_path / "task_queue.json")
    q = tq.PersistentTaskQueue()
    with bind_turn("agent", session_id="sess_A", agent_id="the_analyst.day_trader"):
        q.enqueue("deferred work", source="user")

    reloaded = tq.PersistentTaskQueue()._tasks[0]  # fresh load from the same file
    assert reloaded.origin_channel == "agent"
    assert reloaded.origin_agent_id == "the_analyst.day_trader"


def test_ui_diverts_agent_turn_out_of_main_chat():
    """The UI broadcast loop routes a channel=="agent" turn to the Agents history
    (and re-wraps it as ``agent_event``), never the main ``_chat_history``."""
    from brain.ui.server import UIServer

    async def run():
        srv = UIServer(asyncio.Queue())
        start = {
            "type": "turn_start",
            "channel": "agent",
            "route_sid": "A",
            "agent_id": "the_visionary.trading_bull",
            "end_user_id": "cust-1",
            "user_input": "buy AAPL",
            "turn_id": "t1",
            "ts": 1,
        }
        end = {
            "type": "turn_end",
            "channel": "agent",
            "route_sid": "A",
            "agent_id": "the_visionary.trading_bull",
            "end_user_id": "cust-1",
            "response": "bought 10 shares",
            "turn_id": "t1",
            "elapsed_s": 0.2,
        }
        await srv._handle_agent_event(start)
        await srv._handle_agent_event(end)
        return srv

    srv = asyncio.run(run())
    assert srv._chat_history == []  # the trading prompt NEVER lands in the main feed
    assert len(srv._agent_history) == 1  # it is surfaced in the Agents view instead
    h = srv._agent_history[0]
    assert h["user_input"] == "buy AAPL"
    assert h["response"] == "bought 10 shares"
    assert h["agent_id"] == "the_visionary.trading_bull"
    assert h["end_user_id"] == "cust-1"
