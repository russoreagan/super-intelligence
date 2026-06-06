"""Smoke + behaviour tests for text paralinguistic feature extraction.

This module is imported LAZILY on the hot text-turn path (session_turn.py), with
the feature enabled by default — so a plain import-time error (e.g. a bad string
escape) silently escapes the rest of the suite and crashes every text turn. These
tests import and exercise the module so that whole class of regression is caught.
"""

from __future__ import annotations

from brain.clusters.text_paralinguistics import (
    TextParalinguisticFeatures,
    extract_text_paralinguistics,
)


def test_module_imports_and_runs_on_plain_text():
    f = extract_text_paralinguistics("Tell me what you think about this project.")
    assert isinstance(f, TextParalinguisticFeatures)
    d = f.to_dict()
    assert set(d) >= {"laughter", "warmth", "negativity", "excitement", "informality"}
    assert all(isinstance(v, (int, float)) for v in d.values())


def test_empty_input_is_neutral():
    assert extract_text_paralinguistics("").to_dict() == TextParalinguisticFeatures().to_dict()


def test_excitement_and_laughter_register():
    f = extract_text_paralinguistics("omg this is amazing lol 🔥🔥")
    assert f.excitement > 0.0
    assert f.laughter > 0.0
    assert f.emoji_count >= 2


def test_ascii_emoticons_register_warmth_and_negativity():
    assert extract_text_paralinguistics("thanks :) really appreciate it").warmth > 0.0
    assert extract_text_paralinguistics("ugh this is broken :(").negativity > 0.0


def test_exclamation_density_scales():
    calm = extract_text_paralinguistics("that is fine")
    loud = extract_text_paralinguistics("that is amazing!!!")
    assert loud.exclamation_density > calm.exclamation_density
