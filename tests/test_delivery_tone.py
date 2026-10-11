"""Delivery-tone resolver prototype (brain/delivery_tone.py) — exploration, 2026-10-11."""

from __future__ import annotations

import pytest

from brain.delivery_tone import (
    LABEL_TAG_FILL,
    TAG_VA,
    TONE_FAMILIES,
    resolve_delivery,
    supported_families,
    tag_for_label,
)
from brain.emotion_presets import EMOTION_PRESETS, EMOTION_TAG_MAP
from brain.emotion_vocabulary import EMOTION_TABLE

# Labels the hypothalamus can emit beyond the bucket table (NE / hormonal colour,
# text-affect fallback) — see emotion_vocabulary.apply_* and hypothalamus.process.
_COLOURED = {
    "connected",
    "withdrawn",
    "guarded",
    "cautious-warm",
    "eased",
    "dysphoric",
    "vigilant",
    "alert-curious",
    "scattered",
    "wary",
    "down",
    "irritated",
    "concerned",
}


def _all_tags() -> set[str]:
    tags = {v for v in EMOTION_TAG_MAP.values() if v}
    tags |= {p["tag"] for p in EMOTION_PRESETS.values() if p["tag"]}
    tags |= {v for v in LABEL_TAG_FILL.values() if v}
    return tags


def test_every_emittable_label_resolves_explicitly():
    """No chemistry label may fall through to the PNS neuromod threshold fallback."""
    labels = {v[0] for v in EMOTION_TABLE.values()} | _COLOURED
    unresolved = [
        lbl
        for lbl in sorted(labels)
        if lbl not in EMOTION_TAG_MAP and lbl not in LABEL_TAG_FILL and tag_for_label(lbl) is None
    ]
    assert unresolved == []


def test_full_tag_range_is_reachable():
    """The gate must not clamp the range: every tag lives in at least one family."""
    family_tags = {t for f in TONE_FAMILIES.values() for t in f["tags"]}
    assert _all_tags() - family_tags == set()
    assert all(t in TAG_VA for t in family_tags)


def test_felt_tag_kept_when_reply_supports_it():
    tag, _ = resolve_delivery("somber", ["comforting", "inquiring"], (0.4, 0.3))
    assert tag == "[softly]"


def test_pride_voiced_when_reply_celebrates():
    tag, _ = resolve_delivery("proud", ["celebrating"], (0.8, 0.6))
    assert tag == "[proudly]"


def test_pride_not_voiced_over_a_comforting_reply():
    tag, _ = resolve_delivery("proud", ["comforting", "inquiring"], (0.65, 0.35))
    assert tag in TONE_FAMILIES["comforting"]["tags"] + TONE_FAMILIES["inquiring"]["tags"]
    assert tag != "[proudly]"


@pytest.mark.parametrize(
    ("felt_va", "expected"),
    [((0.82, 0.38), "[warmly]"), ((0.48, 0.12), "[softly]"), ((0.66, 0.2), "[gently]")],
)
def test_chemistry_picks_flavour_within_supported_family(felt_va, expected):
    tag, _ = resolve_delivery("excitement", ["comforting"], felt_va)
    assert tag == expected


def test_no_tone_read_falls_back_to_felt_tag():
    assert resolve_delivery("excitement", [])[0] == "[excited]"


def test_supported_families_keeps_close_mixtures():
    probs = {"comforting": 0.5, "inquiring": 0.3, "firm": 0.05}
    assert supported_families(probs) == ["comforting", "inquiring"]
