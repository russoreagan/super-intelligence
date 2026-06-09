"""
Cross-learning pipeline — private rumination → de-id gate → hypothesis store.

End-to-end with a scripted router: an admitted insight lands as a provisional
hypothesis; a rejected one leaves the shared store empty; distinct corroborating
customers promote it to established.
"""

from __future__ import annotations

from brain.cross_learning import learn_from_private
from brain.deid_gate import DeidGate
from brain.hypothesis_store import HypothesisStore
from brain.private_rumination import PrivateRuminator

_REFLECT = '{"insight": true, "conclusion": "he grieves at a normally-happy topic"}'
_EXTRACT = '{"transferable": true, "principle": "grief can surface at a normally-happy topic"}'
_REID = '{"reidentifiable": false}'
_GEN = '{"general": true}'


def _script_pass(router):
    router.scripted_responses.update(
        private_reflect=_REFLECT, deid_extract=_EXTRACT, deid_reid=_REID, deid_generality=_GEN
    )


def _rum(router):
    return PrivateRuminator(router, DeidGate(router))


async def test_admitted_insight_lands_as_provisional(fake_router):
    _script_pass(fake_router)
    store = HypothesisStore(promote_k=3, now_fn=lambda: 1000.0)
    out = await learn_from_private(_rum(fake_router), store, "private material", "alice")
    assert out.admitted is True
    assert out.status == "provisional"
    assert store.get(out.hypothesis_id) is not None


async def test_rejected_insight_adds_nothing(fake_router):
    fake_router.scripted_responses["private_reflect"] = '{"insight": false}'
    store = HypothesisStore(promote_k=3, now_fn=lambda: 1000.0)
    out = await learn_from_private(_rum(fake_router), store, "private material", "alice")
    assert out.admitted is False
    assert store.all() == []


async def test_distinct_customers_promote_to_established(fake_router):
    _script_pass(fake_router)
    store = HypothesisStore(promote_k=2, now_fn=lambda: 1000.0)
    out1 = await learn_from_private(_rum(fake_router), store, "ctx from alice", "alice")
    assert out1.status == "provisional"
    out2 = await learn_from_private(_rum(fake_router), store, "ctx from bob", "bob")
    assert out2.hypothesis_id == out1.hypothesis_id  # same principle, corroborated
    assert out2.status == "established"
