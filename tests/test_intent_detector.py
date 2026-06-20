"""Embedding-based IntentDetector: literal seed fast-path, semantic bank matching,
the LLM-taught growth loop, dedup/cap, and graceful fallback with no embedder.

Pure tests — a deterministic fake embedder stands in for the local model, so cosine
similarities are controlled exactly.
"""

from __future__ import annotations

import asyncio

from brain.intent_detector import IntentDetector
from brain.settings import settings

# Controlled embedding space. Phrases map to 3-D vectors; cosine is exact.
_VECS = {
    "remind me": [1.0, 0.0, 0.0],
    "can you jog my memory": [0.95, 0.05, 0.0],  # paraphrase — near "remind me"
    "the sky is blue today": [0.0, 0.0, 1.0],  # unrelated — orthogonal
    "we went over this earlier": [0.9, 0.1, 0.0],  # paraphrase the LLM will confirm
}


async def _embed(text: str):
    return _VECS.get(text.lower())


def _run(coro):
    return asyncio.run(coro)


def _det(tmp, seeds=None):
    return IntentDetector(tmp / "bank.json", seeds or {"epistemic_action": ["remind me"]})


def test_literal_seed_fires_without_embedding(tmp_path):
    d = _det(tmp_path)
    # "remind me" is a seed substring → fires on the literal fast path.
    fired = _run(d.detect_all("ok remind me what we said", _embed))
    assert fired["epistemic_action"] is True


def test_paraphrase_fires_via_semantic_bank(tmp_path):
    d = _det(tmp_path)
    # No seed substring, but embedding is near the seeded exemplar → fires.
    fired = _run(d.detect_all("can you jog my memory", _embed))
    assert fired["epistemic_action"] is True


def test_unrelated_does_not_fire(tmp_path):
    d = _det(tmp_path)
    fired = _run(d.detect_all("the sky is blue today", _embed))
    assert fired["epistemic_action"] is False


def test_llm_teaches_the_bank(tmp_path):
    d = _det(tmp_path, seeds={"epistemic_action": []})  # empty seeds → nothing matches yet
    # First pass misses (empty bank, no literal seed).
    fired = _run(d.detect_all("we went over this earlier", _embed))
    assert fired["epistemic_action"] is False
    # Integrator confirms the intent → detector learns this phrasing.
    d.learn_from_llm({"epistemic_action": True})
    # Same phrasing now fires cheaply via the bank.
    fired2 = _run(d.detect_all("we went over this earlier", _embed))
    assert fired2["epistemic_action"] is True


def test_dedup_skips_near_duplicate(tmp_path):
    d = _det(tmp_path, seeds={"epistemic_action": []})
    _run(d.detect_all("we went over this earlier", _embed))
    d.learn_from_llm({"epistemic_action": True})
    n_after_first = len(d._bank["epistemic_action"])
    # Learn the identical phrasing again → deduped, no growth.
    _run(d.detect_all("we went over this earlier", _embed))
    d.learn_from_llm({"epistemic_action": True})
    assert len(d._bank["epistemic_action"]) == n_after_first


def test_no_learning_when_detector_already_fired(tmp_path):
    d = _det(tmp_path)  # seed "remind me"
    _run(d.detect_all("remind me about that", _embed))  # literal hit → fired, no vec
    d.learn_from_llm({"epistemic_action": True})  # nothing to learn (already fired / no vec)
    # Bank holds only the seeded exemplar, not the literal-hit phrasing.
    assert all(e["t"] == "remind me" for e in d._bank["epistemic_action"])


def test_falls_back_to_literal_without_embedder(tmp_path):
    d = _det(tmp_path)
    assert _run(d.detect_all("remind me please", None))["epistemic_action"] is True  # literal
    assert (
        _run(d.detect_all("can you jog my memory", None))["epistemic_action"] is False
    )  # no embed → miss


def test_disabled_uses_literal_only(tmp_path):
    d = _det(tmp_path)
    settings.update({"intent_detector_enabled": 0})
    try:
        assert _run(d.detect_all("can you jog my memory", _embed))["epistemic_action"] is False
        assert _run(d.detect_all("remind me now", _embed))["epistemic_action"] is True
    finally:
        settings.update({"intent_detector_enabled": 1})


def test_bank_persists_across_instances(tmp_path):
    d = _det(tmp_path, seeds={"epistemic_action": []})
    _run(d.detect_all("we went over this earlier", _embed))
    d.learn_from_llm({"epistemic_action": True})
    # New instance loads the saved bank and recognizes the phrasing.
    d2 = _det(tmp_path, seeds={"epistemic_action": []})
    assert _run(d2.detect_all("we went over this earlier", _embed))["epistemic_action"] is True
