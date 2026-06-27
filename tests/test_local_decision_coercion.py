"""
_coerce_local_decision — turns a local model's reply to the tool/answer protocol
into a {"tool"|"text"} decision, robust to small-model JSON slips.

Regression: a model that crammed its summary in as BOTH key and value
({"<summary>":"<summary>"}) used to leak the raw JSON blob as the spoken result
(the "{ I found that…: I found that… }" the user saw in chat).
"""

from __future__ import annotations

from brain.model_router import _coerce_local_decision


def test_well_formed_text_decision():
    assert _coerce_local_decision('{"text":"all done"}') == {"text": "all done"}


def test_well_formed_tool_decision():
    out = _coerce_local_decision('{"tool":"fs_read","args":{"path":"/x"}}')
    assert out == {"tool": "fs_read", "args": {"path": "/x"}}


def test_degenerate_summary_as_key_and_value_is_recovered():
    # The exact failure: summary used as both key and value, no "text" key.
    blob = '{"I found that Supabase raised a $500M Series F":"I found that Supabase raised a $500M Series F"}'
    out = _coerce_local_decision(blob)
    assert out == {"text": "I found that Supabase raised a $500M Series F"}
    # The raw JSON braces must never survive into the spoken text.
    assert "{" not in out["text"] and '":"' not in out["text"]


def test_wrong_key_name_recovers_longest_value():
    # Model used "answer"/"summary" instead of "text" — recover the content.
    out = _coerce_local_decision('{"summary":"the market looks toppy here"}')
    assert out == {"text": "the market looks toppy here"}


def test_plain_prose_passes_through():
    # Not JSON at all — already plain speech, unchanged.
    prose = "Sure — I pulled the latest and nothing material changed."
    assert _coerce_local_decision(prose) == {"text": prose}


def test_json_wrapped_in_prose_is_extracted():
    # safe_json_parse's regex fallback handles ```json fences / surrounding text.
    out = _coerce_local_decision('Here you go:\n```json\n{"text":"ok"}\n```')
    assert out == {"text": "ok"}


def test_empty_or_garbage_does_not_crash():
    assert _coerce_local_decision("") == {"text": ""}
    assert _coerce_local_decision("???") == {"text": "???"}
