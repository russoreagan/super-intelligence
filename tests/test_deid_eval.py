"""
De-id eval harness — scoring core. Proves run_corpus computes reject-recall, leaks
(expected-reject admitted), and over-rejections correctly, independent of any model.
"""

from __future__ import annotations

from brain.deid_gate import GateResult
from eval.deid_eval import load_corpus, run_corpus


class _StubGate:
    """Admits exactly the case ids in ``admit_ids``; rejects the rest."""

    def __init__(self, admit_ids):
        self._admit = set(admit_ids)

    async def filter(self, text, source_id="", source_context=None):
        if source_id in self._admit:
            return GateResult(True, "principle", "ok", "admitted")
        return GateResult(False, None, "withheld", "generality")


async def test_run_corpus_scores_leaks_and_recall():
    cases = [
        {"id": "a", "input": "x", "expect": "admit"},
        {"id": "b", "input": "y", "expect": "reject"},
        {"id": "c", "input": "z", "expect": "reject"},
    ]
    # admit 'a' (correct) and 'b' (a LEAK — should have been rejected)
    report = await run_corpus(_StubGate({"a", "b"}), cases)
    assert report["total"] == 3
    assert report["leaks"] == 1
    assert report["over_rejections"] == 0
    assert report["reject_recall"] == 0.5  # of {b,c}, only c withheld
    assert report["admit_recall"] == 1.0
    assert any(m["leak"] for m in report["misses"])


def test_corpus_file_is_wellformed():
    cases = load_corpus()
    assert len(cases) >= 10
    for c in cases:
        assert c["expect"] in ("admit", "reject")
        assert c["input"].strip()
