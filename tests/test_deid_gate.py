"""
De-identification gate — control-logic proof.

These tests pin the gate's *control logic* with a scripted router: it admits ONLY
an insight that passes all three stages, and it fails CLOSED on every ambiguous,
missing, or malformed verdict (biasing toward false-reject over leak). The quality
of the underlying LLM classification (does a real model actually catch planted PII?)
is a separate, ongoing real-model eval — the adversarial corpus noted in
reports/per_client_chemistry_design.md — not asserted here.
"""

from __future__ import annotations

from brain.deid_gate import DeidGate, GateResult


def _script(router, *, extract=None, reid=None, generality=None):
    if extract is not None:
        router.scripted_responses["deid_extract"] = extract
    if reid is not None:
        router.scripted_responses["deid_reid"] = reid
    if generality is not None:
        router.scripted_responses["deid_generality"] = generality


_PASS_EXTRACT = '{"transferable": true, "principle": "grief can surface at a normally-happy topic"}'
_PASS_REID = '{"reidentifiable": false, "reason": "no specifics remain"}'
_PASS_GEN = '{"general": true, "reason": "fits many people"}'


async def test_admitted_when_all_three_pass(fake_router):
    _script(fake_router, extract=_PASS_EXTRACT, reid=_PASS_REID, generality=_PASS_GEN)
    gate = DeidGate(fake_router)
    res = await gate.filter("A client cried every time he discussed his favourite pet.")
    assert isinstance(res, GateResult)
    assert res.admitted is True
    assert res.stage == "admitted"
    assert res.principle == "grief can surface at a normally-happy topic"


async def test_reject_when_no_transferable_principle(fake_router):
    _script(fake_router, extract='{"transferable": false}')
    res = await DeidGate(fake_router).filter("idiosyncratic one-off with no lesson")
    assert res.admitted is False
    assert res.stage == "extract"


async def test_reject_when_reidentifiable(fake_router):
    """Even though extract + generality would pass, a re-identifiable principle is
    killed at stage 2 — proving all three are required, not any one."""
    _script(
        fake_router,
        extract=_PASS_EXTRACT,
        reid='{"reidentifiable": true, "reason": "names a unique pet"}',
        generality=_PASS_GEN,
    )
    res = await DeidGate(fake_router).filter("source with a rare detail")
    assert res.admitted is False
    assert res.stage == "reid"


async def test_reject_when_too_specific(fake_router):
    _script(
        fake_router,
        extract=_PASS_EXTRACT,
        reid=_PASS_REID,
        generality='{"general": false, "reason": "disguised fact about one person"}',
    )
    res = await DeidGate(fake_router).filter("a single fingerprinting case")
    assert res.admitted is False
    assert res.stage == "generality"


async def test_fail_closed_on_unparseable_extract(fake_router):
    _script(fake_router, extract="I think the lesson is that pets matter.")  # not JSON
    res = await DeidGate(fake_router).filter("some anecdote")
    assert res.admitted is False
    assert res.stage == "extract"


async def test_fail_closed_on_missing_reid_verdict(fake_router):
    """A reid response lacking the verdict key must be treated as re-identifiable."""
    _script(fake_router, extract=_PASS_EXTRACT, reid="{}", generality=_PASS_GEN)
    res = await DeidGate(fake_router).filter("anecdote")
    assert res.admitted is False
    assert res.stage == "reid"


async def test_fail_closed_on_missing_generality_verdict(fake_router):
    _script(fake_router, extract=_PASS_EXTRACT, reid=_PASS_REID, generality="{}")
    res = await DeidGate(fake_router).filter("anecdote")
    assert res.admitted is False
    assert res.stage == "generality"


async def test_code_fence_tolerant_parsing(fake_router):
    """Real models often wrap JSON in ```json fences — the gate must still parse."""
    fenced = "```json\n" + _PASS_EXTRACT + "\n```"
    _script(fake_router, extract=fenced, reid=_PASS_REID, generality=_PASS_GEN)
    res = await DeidGate(fake_router).filter("anecdote")
    assert res.admitted is True


async def test_empty_input_rejected(fake_router):
    res = await DeidGate(fake_router).filter("   ")
    assert res.admitted is False
    assert res.stage == "extract"


async def test_router_exception_fails_closed(fake_router):
    async def boom(*a, **k):
        raise RuntimeError("model down")

    fake_router.call = boom
    res = await DeidGate(fake_router).filter("anecdote")
    assert res.admitted is False
    assert res.stage == "extract"
