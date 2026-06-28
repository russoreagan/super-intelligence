"""The shared JSON-blob guard (brain.text_guards) — one definition used by the UI
emitter, the partner webhook, and the drafter follow-through. Spoken/forwarded text
is natural language; a raw JSON object/array is a degenerate model non-answer and
must be caught everywhere identically."""

from __future__ import annotations

import pytest

from brain.text_guards import looks_like_json_blob


@pytest.mark.parametrize(
    "text",
    [
        '{"has_signal": true}',
        '  {"speech": "hi"}  ',
        "[1, 2, 3]",
        '```json\n{"x": 1}\n```',
        "```\n[1, 2]\n```",
        '```JSON\n{"y": 2}\n```',
    ],
)
def test_flags_raw_json(text):
    assert looks_like_json_blob(text) is True


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "Let me grab those for you.",
        "Here are the results: 3 fills today.",
        "I think {x} is a placeholder, not JSON.",
        "```python\nprint('hi')\n```",  # fenced code, but prose-leading after fence
    ],
)
def test_passes_spoken_prose(text):
    assert looks_like_json_blob(text) is False


def test_reexport_from_follow_through_is_same_object():
    """Existing callers import it from follow_through — keep that path live."""
    from brain.clusters.follow_through import looks_like_json_blob as ft_guard

    assert ft_guard is looks_like_json_blob
