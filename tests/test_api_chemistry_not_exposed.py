"""
The engine API must never expose the raw chemical signal.

Only the mood OUTPUT (emotion) crosses the partner boundary — neuromod/hormonal
levels are internal mechanism and would let the affect model be reverse-engineered.
This locks that contract at both the streamed-event vocabulary and the curated mood
view (both WS and SSE copies). The owner's own UI still sees the chemistry; these
sets only govern the partner taps.
"""

from __future__ import annotations

from brain.api import server as api_server
from brain.api import ws as api_ws


def test_chemistry_not_in_partner_stream_sets():
    assert "neuromod" not in api_ws._FORWARD_TYPES
    assert "hormonal" not in api_ws._FORWARD_TYPES
    assert "neuromod" not in api_server._STREAMED_TYPES
    assert "hormonal" not in api_server._STREAMED_TYPES
    # The mood OUTPUT itself still goes through.
    assert "emotion" in api_ws._FORWARD_TYPES
    assert "emotion" in api_server._STREAMED_TYPES


def test_mood_view_drops_hormonal_keeps_emotion():
    affect = {
        "emotion": "lively",
        "user_emotion": "curious",
        "hormonal": {"CORT": 0.4, "OXT": 0.7},
        "neuromod": {"DA": 0.6, "NE": 0.3},
    }
    for mood_fn in (api_ws._mood_from_affect, api_server._mood_from_affect):
        mood = mood_fn(affect)
        assert mood["emotion"] == "lively"
        assert mood.get("user_emotion") == "curious"
        assert "hormonal" not in mood
        assert "neuromod" not in mood
