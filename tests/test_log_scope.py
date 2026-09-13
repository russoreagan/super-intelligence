"""brain/log_scope.lane_text — user text never reaches the process log from a private lane."""

from __future__ import annotations

from brain import org_settings
from brain.log_scope import digest, lane_text
from brain.second_brain.store import bind_persona
from brain.turn_ctx import bind_turn


def test_owner_lane_keeps_the_slice(monkeypatch):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    assert lane_text("hello world", 5) == "hello"


def test_engine_lane_digests(monkeypatch):
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    with bind_turn("agent", session_id="s", end_user_id="u"):
        out = lane_text("hello world", 5)
    assert out == digest("hello world") and out.startswith("sha256:") and out.endswith("/11")


def test_isolated_non_home_binding_digests_even_unbound_turn(monkeypatch):
    monkeypatch.setenv("BRAIN_PERSONA_NAME", "home_p")
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "isolated")
    with bind_persona("ahab"):
        assert lane_text("private", 80).startswith("sha256:")
    with bind_persona("home_p"):
        assert lane_text("mine", 80) == "mine"
    monkeypatch.setattr(org_settings, "learning_mode", lambda: "consolidated")
    with bind_persona("ahab"):
        assert lane_text("shared", 80) == "shared"


def test_empty_text_and_failures_are_safe(monkeypatch):
    assert lane_text("", 10) == ""
    assert lane_text(None, 10) == ""
