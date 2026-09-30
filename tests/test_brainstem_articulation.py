"""The articulation gate's quiescence window catches late drafts. Once the turn
has marked drafting done, none can arrive, so waiting out the window is dead air
before every reply; selection must be identical either way."""

from __future__ import annotations

import asyncio
import time

from brain.brainstem import QUIESCENCE_WINDOW, Brainstem


class _Router:
    def reset_turn_log(self):
        pass


def _turn_with_drafts(bs: Brainstem):
    turn = bs.begin_turn()
    bs.add_draft("a", "first", 0.6)
    bs.add_draft("b", "best", 0.9)
    bs.add_draft("c", "vetoed", 1.0)
    for d in ("a", "b", "c"):
        bs.endorse(d)
    bs.veto("c")
    return turn


def test_drafting_done_articulates_without_waiting():
    bs = Brainstem(bus=None, model_router=_Router())
    turn = _turn_with_drafts(bs)
    turn.drafting_done = True
    t0 = time.monotonic()
    out = asyncio.run(bs.articulation_gate(turn))
    assert out == "best"
    assert time.monotonic() - t0 < QUIESCENCE_WINDOW / 4


def test_without_the_flag_the_window_still_applies():
    bs = Brainstem(bus=None, model_router=_Router())
    turn = _turn_with_drafts(bs)
    t0 = time.monotonic()
    out = asyncio.run(bs.articulation_gate(turn))
    assert out == "best"  # same selection
    assert time.monotonic() - t0 >= QUIESCENCE_WINDOW


def test_drafting_done_with_nothing_endorsed_keeps_polling_to_fallback():
    bs = Brainstem(bus=None, model_router=_Router())
    turn = bs.begin_turn()
    bs.add_draft("a", "only", 0.5)  # never endorsed
    turn.drafting_done = True
    turn.started_at -= 10_000  # timed out: the gate falls through to best-scored
    assert asyncio.run(bs.articulation_gate(turn)) == "only"
