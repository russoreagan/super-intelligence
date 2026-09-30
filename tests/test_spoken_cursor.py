"""Barge-in must know what the listener actually heard. v4 Turbo generates audio
~7x faster than it plays, so "synthesis finished" is not "playback finished":
the echo guard and barge-in have to follow playback, and a cut-off reply must be
annotated in the next turn's history with where it was cut (brain.spoken_cursor,
ParietalCluster.amend_response, BrainSession.note_speech_interrupted)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from brain.clusters.parietal import ParietalCluster
from brain.spoken_cursor import SpokenCursor, interruption_note

TEXT = "The pod sleeps at night. It wakes when you speak."


def _aligned(text: str, ms_per_char: int = 50, tag: str = "") -> dict:
    chars = list(tag + text)
    return {
        "chars": chars,
        "char_start_times_ms": [i * ms_per_char for i in range(len(chars))],
        "char_durations_ms": [ms_per_char] * len(chars),
    }


def _cursor_at(elapsed_ms: float, cursor: SpokenCursor) -> float:
    """A `now` that puts playback `elapsed_ms` in."""
    return cursor.started_at + elapsed_ms / 1000


# ── cursor ───────────────────────────────────────────────────────────────────


def test_heard_text_follows_playback_and_drops_a_half_word():
    c = SpokenCursor()
    c.add_chunk("", 3000, _aligned(TEXT, tag="[warmly] "))
    # 9 tag chars + "The pod sl" = 19 chars → 950 ms is mid-"sleeps".
    assert c.heard_text(_cursor_at(950, c)) == "The pod"
    assert c.heard_text(_cursor_at(10_000, c)) == TEXT


def test_window_is_recent_speech_not_the_whole_reply():
    long = "alpha " * 200 + "omega words at the end"
    c = SpokenCursor()
    c.add_chunk("", 60_000, _aligned(long, ms_per_char=50))
    early = c.window_text(_cursor_at(1000, c))
    assert "alpha" in early and "omega" not in early
    assert "omega" in c.window_text(_cursor_at(len(long) * 50, c))


def test_relative_alignment_blocks_tile_the_clip():
    """The socket restarts start times at 0 in each block."""
    c = SpokenCursor()
    c.add_audio(1)
    c.add_alignment_block(_aligned("Hello "))
    c.add_alignment_block(_aligned("there friend"))
    assert c.heard_text(_cursor_at(6 * 50 + 5 * 50 + 1, c)) == "Hello there"


def test_without_alignment_the_text_is_spread_over_the_audio():
    c = SpokenCursor()
    c.set_text("one two three four")
    c.add_audio(1800)  # 18 chars → 100 ms each
    assert c.heard_text(_cursor_at(800, c)) == "one two"
    assert c.finished(_cursor_at(1801, c))


def test_interruption_note_keeps_what_was_not_heard():
    note = interruption_note("The pod", TEXT)
    assert note.startswith("The pod…") and "may not have heard" in note
    assert "sleeps at night" in note  # the rest is kept, flagged as unheard
    assert "before hearing" in interruption_note("", TEXT)


# ── history ──────────────────────────────────────────────────────────────────


def test_parietal_amends_the_matching_reply_only():
    p = ParietalCluster(bus=None)
    p.update({}, "hi", "Hello there.")
    p.update({}, "when does it sleep", TEXT)
    assert p.amend_response("  the POD sleeps at night.  It wakes when you speak.", "X") is True
    assert p.recent_turns(2)[-1]["response"] == "X"
    assert p.recent_turns(2)[0]["response"] == "Hello there."
    assert p.amend_response("never said this", "Y") is False


def test_brain_hook_annotates_history():
    from brain.session_turn import _TurnMixin

    p = ParietalCluster(bus=None)
    p.update({}, "when does it sleep", TEXT)
    brain = SimpleNamespace(parietal=p)
    assert _TurnMixin.note_speech_interrupted(brain, TEXT, "The pod sleeps") is True
    assert "may not have heard" in p.recent_turns_text()


# ── hosted realtime socket ───────────────────────────────────────────────────


def _session(hook_calls: list):
    from brain.api.ws import WsSession

    sent: list[dict] = []

    class _Ws:
        async def send_json(self, payload):
            sent.append(payload)

    sess = WsSession(
        _Ws(),
        SimpleNamespace(session_id="s", agent_id="p.a", end_user_id=None, mandate_id=None),
        {"owner": True},
        turn_runner=None,
        registry=None,
        on_speech_interrupted=lambda full, heard: hook_calls.append((full, heard)),
    )

    async def _no_turn(message, *, transcript=None):
        pass

    sess._run_turn = _no_turn
    return sess, sent


def _playing(sess, text: str, elapsed_ms: float):
    c = SpokenCursor()
    c.add_chunk("", len(text) * 50, _aligned(text))
    c.started_at = time.monotonic() - elapsed_ms / 1000
    sess._cursor, sess._cursor_text, sess._cursor_turn_id = c, text, "t1"
    sess._speaking_text = ""  # synthesis already finished; the client still plays


async def test_barge_after_synthesis_stops_playback_and_records_heard():
    calls: list = []
    sess, sent = _session(calls)
    _playing(sess, TEXT, elapsed_ms=24 * 50)  # "The pod sleeps at night."
    await sess._on_transcript("hang on a second", False, 0.0)
    interrupted = [m for m in sent if m.get("type") == "audio_interrupted"]
    assert interrupted and interrupted[0]["turn_id"] == "t1"
    assert interrupted[0]["heard"].startswith("The pod sleeps at")
    assert calls and calls[0][0] == TEXT
    assert sess._cursor is None  # the reply is closed out once


async def test_echo_of_current_playback_is_still_recognised_after_synthesis():
    """The old guard cleared its reference when synthesis ended — seconds before
    a v4 reply finished playing — so the tail of the reply became a user turn."""
    calls: list = []
    sess, sent = _session(calls)
    long = "We moved the scheduler off the hourly cron last week. " * 6
    _playing(sess, long, elapsed_ms=len(long) * 50 * 0.8)
    assert sess._is_tts_echo("off the hourly cron last week")
    await sess._on_transcript("off the hourly cron last week", False, 0.0)
    assert calls == []  # echo never counts as a barge-in


async def test_finished_playback_is_not_interrupted():
    calls: list = []
    sess, sent = _session(calls)
    _playing(sess, "Done.", elapsed_ms=60_000)
    await sess._on_transcript("next question please", True, 1.0)
    assert calls == [] and not any(m.get("type") == "audio_interrupted" for m in sent)


def test_hook_failure_never_breaks_barge_in():
    def boom(*_a):
        raise RuntimeError("history unavailable")

    sess, _sent = _session([])
    sess._on_speech_interrupted = boom
    _playing(sess, TEXT, elapsed_ms=500)
    asyncio.run(sess._barge_in())
    assert sess._tts_cancel.is_set()


# ── the console's own voice (PNS) ────────────────────────────────────────────


def test_console_barge_mid_synthesis_reports_heard(monkeypatch):
    import brain.pns as pns_mod
    from tests.test_tts_dialogue import PCM, _audio_frame, _FakeBus, _FakeServer, _StubHTTP

    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v")
    monkeypatch.delenv("ELEVENLABS_MODEL_ID", raising=False)
    monkeypatch.setattr(pns_mod, "BROWSER_AUDIO_MODE", True)
    import elevenlabs

    monkeypatch.setattr(elevenlabs, "AsyncElevenLabs", _StubHTTP)
    frames = [_audio_frame(_aligned(TEXT[i : i + 10])) for i in range(0, len(TEXT), 10)]
    _FakeServer(frames=frames * 20).install(monkeypatch)
    p = pns_mod.PNS(_FakeBus())
    p._tts_ws_queue = asyncio.Queue(maxsize=100_000)
    p.BARGE_IN_GRACE_SECONDS = 0
    calls: list = []
    p.on_speech_interrupted = lambda full, heard: calls.append((full, heard))

    async def run():
        task = asyncio.create_task(p._speak(TEXT))
        while p._cursor is None or p._cursor.started_at is None:
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.05)
        p.interrupt()
        await task

    asyncio.run(run())
    assert len(PCM) and calls, "barge-in must reach the brain"
    full, heard = calls[0]
    assert full == TEXT and len(heard) < len(full)
