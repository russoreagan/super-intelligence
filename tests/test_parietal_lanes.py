"""The recent-conversation ring is rendered verbatim into the next turn's prompt,
and one brain process serves the owner and every engine-API end user of an org.
With a single shared ring, end user A's reply prompt carried end user B's last
exchanges. Parietal state is now per conversation lane (brain.clusters.parietal
lane_key); these tests pin the isolation."""

from __future__ import annotations

import asyncio

import pytest

import brain.clusters.parietal as parietal_mod
from brain.clusters.parietal import OWNER_LANE, ParietalCluster, lane_key
from brain.turn_ctx import bind_turn


def _as(end_user: str = "", partner: str = "p1", agent: str = "nova.support", session: str = "s1"):
    if not end_user and not session:
        return bind_turn("owner")
    return bind_turn(
        "agent", session_id=session, agent_id=agent, end_user_id=end_user, partner_id=partner
    )


def _turn(p: ParietalCluster, said: str, reply: str, **lane) -> None:
    with _as(**lane):
        p.update({"entities": [said.split()[0]]}, said, reply)


def test_end_users_never_see_each_other():
    p = ParietalCluster(bus=None)
    _turn(p, "Alice here, my card number ends 4242", "Noted, Alice.", end_user="alice")
    _turn(p, "Bob asking about refunds", "Refunds take 5 days.", end_user="bob")
    _turn(p, "Alice again, is it charged?", "Yes, Alice.", end_user="alice")

    with _as(end_user="alice"):
        a = p.recent_turns_text()
        assert "4242" in a and "Bob" not in a and "Refunds" not in a
        assert p.turn_count == 2
        assert "Bob" not in p.entity_last_seen()
    with _as(end_user="bob"):
        b = p.recent_turns_text()
        assert "Refunds" in b and "Alice" not in b and "4242" not in b


def test_owner_and_engine_lanes_are_separate():
    p = ParietalCluster(bus=None)
    p.update({}, "owner: plan my week", "Sure.")  # unbound = owner lane
    _turn(p, "customer question", "customer answer", end_user="c1")
    assert "customer" not in p.recent_turns_text()
    with _as(end_user="c1"):
        assert "plan my week" not in p.recent_turns_text()


def test_same_end_user_id_under_two_partners_is_two_people():
    p = ParietalCluster(bus=None)
    _turn(p, "partner one's user1", "hi", end_user="user1", partner="p1")
    with _as(end_user="user1", partner="p2"):
        assert p.recent_turns_text() == ""


def test_same_user_with_two_agents_is_two_conversations():
    p = ParietalCluster(bus=None)
    _turn(p, "talking to support", "hi", end_user="u", agent="nova.support")
    with _as(end_user="u", agent="nova.sales"):
        assert p.recent_turns_text() == ""


def test_no_end_user_falls_back_to_the_session():
    p = ParietalCluster(bus=None)
    _turn(p, "anon in session A", "hi", end_user="", session="A")
    with _as(end_user="", session="B"):
        assert p.recent_turns_text() == ""
    with _as(end_user="", session="A"):
        assert "anon in session A" in p.recent_turns_text()


def test_sticky_skill_and_style_are_per_lane():
    p = ParietalCluster(bus=None)
    with _as(end_user="alice"):
        p.set_active_skill_context("skill-ctx-alice")  # any object; stored as-is
        p.update_register("formal")
    with _as(end_user="bob"):
        assert p.active_skill_context is None
        assert p.dominant_register() != "formal" or not p._register_profile
    assert p.active_skill_context is None  # owner untouched


def test_amend_only_touches_the_current_lane():
    p = ParietalCluster(bus=None)
    _turn(p, "q", "Same words in both lanes.", end_user="alice")
    _turn(p, "q", "Same words in both lanes.", end_user="bob")
    with _as(end_user="alice"):
        assert p.amend_response("Same words in both lanes.", "cut") is True
    with _as(end_user="bob"):
        assert "Same words in both lanes." in p.recent_turns_text()


def test_boot_seed_never_puts_an_end_users_episode_in_the_owner_lane():
    p = ParietalCluster(bus=None)
    p.seed(
        [
            {"user_input": "customer secret", "entity_response": "x", "end_user_id": "c9"},
            {"user_input": "owner chat", "entity_response": "y", "end_user_id": ""},
        ]
    )
    owner = p.recent_turns_text()
    assert "owner chat" in owner and "customer secret" not in owner


def test_engine_lane_warms_from_its_own_episodes_once():
    p = ParietalCluster(bus=None)
    asked: list[str] = []

    def recall(eu: str) -> list[dict]:
        asked.append(eu)
        return [{"user_input": f"{eu} earlier", "entity_response": "ok", "end_user_id": eu}]

    async def run():
        with _as(end_user="alice"):
            await p.ensure_lane_seeded(recall)
            await p.ensure_lane_seeded(recall)
            return p.recent_turns_text()

    assert "alice earlier" in asyncio.run(run())
    assert asked == ["alice"]
    asyncio.run(p.ensure_lane_seeded(recall))  # owner lane: never re-seeded here
    assert asked == ["alice"]


def test_kill_switch_restores_the_shared_ring(monkeypatch):
    from brain.settings import settings

    real = settings.get
    monkeypatch.setattr(
        settings, "get", lambda k, d=None: 0 if k == "engine_lane_scoping" else real(k, d)
    )
    p = ParietalCluster(bus=None)
    _turn(p, "from alice", "a", end_user="alice")
    with _as(end_user="bob"):
        assert lane_key() == OWNER_LANE
        assert "from alice" in p.recent_turns_text()


def test_lane_cap_evicts_oldest_but_never_the_owner(monkeypatch):
    monkeypatch.setattr(parietal_mod, "MAX_LANES", 3)
    p = ParietalCluster(bus=None)
    p.update({}, "owner turn", "ok")
    for u in ("a", "b", "c", "d"):
        _turn(p, f"{u} says", "ok", end_user=u)
    assert OWNER_LANE in p._lanes and len(p._lanes) == 3
    assert "owner turn" in p.recent_turns_text()


@pytest.mark.parametrize("channel", ["owner"])
def test_unbound_and_owner_bound_share_the_owner_lane(channel):
    p = ParietalCluster(bus=None)
    p.update({}, "typed in the console", "ok")
    with bind_turn(channel):
        assert "typed in the console" in p.recent_turns_text()


def test_ws_interruption_amends_the_sessions_own_lane():
    """The WS transcript callback runs outside any turn binding; the history
    amend must still land in the session's lane, not the owner's."""
    import time
    from types import SimpleNamespace

    from brain.api.ws import WsSession
    from brain.session_turn import _TurnMixin
    from brain.spoken_cursor import SpokenCursor

    p = ParietalCluster(bus=None)
    reply = "Your order ships Tuesday."
    _turn(p, "when does it ship", reply, end_user="alice", partner="p1", agent="nova.support")
    brain = SimpleNamespace(parietal=p)

    class _Ws:
        async def send_json(self, payload):
            pass

    sess = WsSession(
        _Ws(),
        SimpleNamespace(
            session_id="s1", agent_id="nova.support", end_user_id="alice", partner_id="p1"
        ),
        {"owner": True},
        turn_runner=None,
        registry=None,
        on_speech_interrupted=lambda full, heard: _TurnMixin.note_speech_interrupted(
            brain, full, heard
        ),
    )
    c = SpokenCursor()
    c.set_text(reply)
    c.add_audio(len(reply) * 60)
    c.started_at = time.monotonic() - 0.5
    sess._cursor, sess._cursor_text, sess._cursor_turn_id = c, reply, "t"
    asyncio.run(sess._barge_in())
    with _as(end_user="alice", partner="p1", agent="nova.support"):
        assert "may not have heard" in p.recent_turns_text()
