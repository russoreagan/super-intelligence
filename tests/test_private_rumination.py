"""
PrivateRuminator — the two-tier (reason-privately → gate) flow.

Proves: the reasoning pass runs first and only a gate-admitted result leaves; the
gate's re-id stage is fed the ORIGINAL private material (not just the abstracted
conclusion); and every no-insight / error path leaves nothing behind.
"""

from __future__ import annotations

from brain.deid_gate import DeidGate
from brain.private_rumination import PrivateRuminator

_PASS_REFLECT = '{"insight": true, "conclusion": "he reacts with grief to a normally-happy topic"}'
_PASS_EXTRACT = '{"transferable": true, "principle": "grief can surface at a normally-happy topic"}'
_PASS_REID = '{"reidentifiable": false, "reason": "no specifics"}'
_PASS_GEN = '{"general": true, "reason": "many referents"}'

_PRIVATE = "Customer C-4471 (Jacob) cried whenever his dog Rex came up across three sessions."


def _script(router, **by_cell):
    router.scripted_responses.update(by_cell)


async def test_full_pipeline_admits(fake_router):
    _script(
        fake_router,
        private_reflect=_PASS_REFLECT,
        deid_extract=_PASS_EXTRACT,
        deid_reid=_PASS_REID,
        deid_generality=_PASS_GEN,
    )
    rum = PrivateRuminator(fake_router, DeidGate(fake_router))
    res = await rum.ruminate(_PRIVATE, source_id="C-4471")
    assert res.admitted is True
    assert res.principle == "grief can surface at a normally-happy topic"


async def test_reid_stage_sees_private_material_not_just_conclusion(fake_router):
    """The re-id check must be able to catch identifiers from the underlying
    source, so it receives the full private material as context."""
    _script(
        fake_router,
        private_reflect=_PASS_REFLECT,
        deid_extract=_PASS_EXTRACT,
        deid_reid=_PASS_REID,
        deid_generality=_PASS_GEN,
    )
    rum = PrivateRuminator(fake_router, DeidGate(fake_router))
    await rum.ruminate(_PRIVATE, source_id="C-4471")

    reid_calls = [c for c in fake_router.calls if c["cell"] == "deid_reid"]
    assert reid_calls, "reid stage should have run"
    reid_user_text = reid_calls[0]["messages"][0]["content"]
    assert "Rex" in reid_user_text and "Jacob" in reid_user_text  # the private source, verbatim


async def test_no_insight_short_circuits_before_gate(fake_router):
    _script(fake_router, private_reflect='{"insight": false}')
    rum = PrivateRuminator(fake_router, DeidGate(fake_router))
    res = await rum.ruminate(_PRIVATE, source_id="C-4471")
    assert res.admitted is False
    assert res.stage == "reflect"
    # the gate never ran
    assert not any(c["cell"].startswith("deid_") for c in fake_router.calls)


async def test_gate_can_still_reject_a_reasoned_conclusion(fake_router):
    """Reasoning produced a candidate, but the gate kills it (re-identifiable)."""
    _script(
        fake_router,
        private_reflect=_PASS_REFLECT,
        deid_extract=_PASS_EXTRACT,
        deid_reid='{"reidentifiable": true, "reason": "still pins one person"}',
        deid_generality=_PASS_GEN,
    )
    rum = PrivateRuminator(fake_router, DeidGate(fake_router))
    res = await rum.ruminate(_PRIVATE, source_id="C-4471")
    assert res.admitted is False
    assert res.stage == "reid"


async def test_empty_context_rejected(fake_router):
    rum = PrivateRuminator(fake_router, DeidGate(fake_router))
    res = await rum.ruminate("   ", source_id="C-4471")
    assert res.admitted is False
    assert res.stage == "reflect"


async def test_reflect_router_error_fails_closed(fake_router):
    async def boom(*a, **k):
        raise RuntimeError("model down")

    fake_router.call = boom
    rum = PrivateRuminator(fake_router, DeidGate(fake_router))
    res = await rum.ruminate(_PRIVATE, source_id="C-4471")
    assert res.admitted is False
    assert res.stage == "reflect"
