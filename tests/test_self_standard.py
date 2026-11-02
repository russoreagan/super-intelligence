"""Self-graded reward measured against the persona's own running standard.

Prod 2026-11-01: every reply farrah wrote cleared the fixed bars, so draft
quality and craft paid DA nearly every turn (about +0.09 a turn in small talk)
and intrinsic pride labelled 12 of 31 turns "proud". brain/self_standard.py
makes those payouts a prediction error against the persona's expectation.
"""

from __future__ import annotations

import inspect
from collections import deque
from unittest.mock import MagicMock

import pytest

from brain import self_standard
from brain.settings import settings


def test_expectation_moves_once_per_turn():
    first = self_standard.appraise("overall", 0.95, "t1", persona="p")
    again = self_standard.appraise("overall", 0.10, "t1", persona="p")  # same turn
    assert first == again == pytest.approx(float(settings.get("self_standard_prior")))
    nxt = self_standard.appraise("overall", 0.95, "t2", persona="p")
    alpha = float(settings.get("self_standard_alpha"))
    assert nxt == pytest.approx(first + alpha * (0.95 - first))


def test_expectations_are_per_persona_and_metric():
    for i in range(30):
        self_standard.appraise("overall", 0.95, f"t{i}", persona="a")
    prior = float(settings.get("self_standard_prior"))
    assert self_standard.appraise("overall", 0.5, "x", persona="b") == pytest.approx(prior)
    assert self_standard.appraise("craft", 0.5, "x", persona="a") == pytest.approx(prior)


def test_surprise_is_the_share_of_room_above_expectation():
    assert self_standard.surprise(0.9, 0.8) == pytest.approx(0.5)
    assert self_standard.surprise(0.7, 0.8) == 0.0
    assert self_standard.surprise(1.0, 1.0) == 0.0


def test_routine_quality_stops_paying():
    """Twenty replies at the persona's usual 0.88: the share paid for the last one
    is a sliver of the first's, while a standout reply still pays well."""
    shares = []
    for i in range(20):
        exp = self_standard.appraise("overall", 0.88, f"t{i}", persona="farrah")
        shares.append(self_standard.surprise(0.88, exp))
    assert shares[-1] < 0.25 * shares[0]
    exp = self_standard.appraise("overall", 0.97, "standout", persona="farrah")
    assert self_standard.surprise(0.97, exp) > 0.6


# ── Intrinsic pride ───────────────────────────────────────────────────────────


def _meta():
    from brain.metacognition import MetacognitionCell

    meta = MetacognitionCell.__new__(MetacognitionCell)
    meta._bus = MagicMock()
    meta._schema = None
    meta._turn_stats = deque(maxlen=20)
    meta._neuromod_history = deque(maxlen=50)
    meta._override_cooldowns = {}
    meta._cooldown_turns = 3
    return meta


def _scores(overall: float) -> list[dict]:
    return [{"selected": True, "vetoed": False, "overall": overall}]


def _pride_turns(n: int, overall: float, tone: str = "neutral") -> list[str | None]:
    meta = _meta()
    out = []
    for i in range(n):
        emotion, _ = meta._appraise({"user_tone_toward_ai": tone}, {}, _scores(overall), f"t{i}")
        out.append(emotion)
    return out


def test_pride_habituates_to_the_personas_usual_quality(monkeypatch):
    monkeypatch.setitem(settings._data, "self_reward_relative", 1)
    labels = _pride_turns(30, 0.90)
    assert labels[0] == "proud"
    assert labels[-10:].count("proud") == 0


def test_a_standout_reply_is_still_proud(monkeypatch):
    monkeypatch.setitem(settings._data, "self_reward_relative", 1)
    meta = _meta()
    for i in range(30):
        meta._appraise({}, {}, _scores(0.88), f"t{i}")
    emotion, _ = meta._appraise({}, {}, _scores(0.98), "standout")
    assert emotion == "proud"


def test_pride_received_warmly_is_not_gated_by_the_standard(monkeypatch):
    monkeypatch.setitem(settings._data, "self_reward_relative", 1)
    assert _pride_turns(30, 0.90, tone="warm").count("proud") == 30


def test_pride_kill_switch_restores_the_absolute_bar(monkeypatch):
    monkeypatch.setitem(settings._data, "self_reward_relative", 0)
    assert _pride_turns(30, 0.90).count("proud") == 30


# ── Craft reward ──────────────────────────────────────────────────────────────


class _Rec:
    def __init__(self):
        self.da = []

    def add(self, ch, delta, **kw):
        if ch == "DA":
            self.da.append(delta)


def _craft_rewards(n: int, craft: float, monkeypatch) -> list[float]:
    monkeypatch.setitem(settings._data, "persona_name", "The Poet")
    meta = _meta()
    nm = _Rec()
    meta._bus.neuromod = nm
    paid = []
    for i in range(n):
        before = len(nm.da)
        meta._reward_aesthetic(
            [{"selected": True, "vetoed": False, "critic_ran": True, "craft": craft}], f"t{i}"
        )
        paid.append(nm.da[-1] if len(nm.da) > before else 0.0)
    return paid


def test_craft_reward_habituates(monkeypatch):
    monkeypatch.setitem(settings._data, "self_reward_relative", 1)
    paid = _craft_rewards(30, 0.92, monkeypatch)
    assert paid[0] > 0.0
    assert paid[-1] < 0.3 * paid[0]


def test_craft_reward_kill_switch(monkeypatch):
    monkeypatch.setitem(settings._data, "self_reward_relative", 0)
    paid = _craft_rewards(10, 0.92, monkeypatch)
    assert paid[-1] == pytest.approx(paid[0])


# ── Draft-quality reward (session_turn) ───────────────────────────────────────


def test_draft_quality_reward_reads_the_self_standard():
    """The draft-quality DA lives inline in the turn body; pin that it scales by
    the self-standard surprise and moves the expectation on every scored turn."""
    from brain.session_turn import _TurnMixin

    src = inspect.getsource(_TurnMixin._process_turn_body)
    assert 'self_standard.appraise("overall", overall, turn_id)' in src
    assert "self_standard.surprise(" in src
    assert "if overall <= 0.7 and self_standard.enabled():" in src
