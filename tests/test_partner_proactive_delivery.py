"""
Owner-lane delivery of self-directed job results.

Self-directed motor-cortex jobs (_run_task → execute_internal_job) run on the OWNER
lane with no end-user on the turn context, so emit_proactive_speech's partner webhook
would drop the result. A job-result caller passes ``partner_target`` (the owning tenant)
so the result reaches the partner anyway — without re-laning the local UI event.

Covers brain/ui/emitter.py: emit_proactive_speech(partner_target=...) →
_dispatch_partner_proactive target resolution.
"""

from __future__ import annotations

import asyncio

from brain.turn_ctx import bind_turn
from brain.ui.emitter import ActivationEmitter


def _emitter_with_capture(monkeypatch) -> tuple[ActivationEmitter, list[dict]]:
    monkeypatch.setenv("AGENT_WEBHOOK_URL", "https://scheduler.example/api/agent/inbound")
    monkeypatch.setenv("AGENT_WEBHOOK_SECRET", "shh")
    em = ActivationEmitter()
    captured: list[dict] = []

    async def _capture(url, secret, payload):  # shadows the staticmethod on the instance
        captured.append(payload)

    em._post_partner_webhook = _capture  # type: ignore[assignment]
    return em, captured


def test_owner_lane_job_result_delivers_to_partner_target(monkeypatch):
    em, captured = _emitter_with_capture(monkeypatch)

    async def run():
        # No bind_turn → owner lane, exactly how _run_task runs. The result still
        # reaches the partner because the caller supplies the owning tenant.
        await em.emit_proactive_speech("Found 3 fresh signals on NVDA.", partner_target="cust-1")
        await asyncio.sleep(0)  # let the fire-and-forget POST task run

    asyncio.run(run())
    assert len(captured) == 1
    assert captured[0]["end_user_id"] == "cust-1"
    assert captured[0]["kind"] == "proactive"
    assert captured[0]["text"] == "Found 3 fresh signals on NVDA."


def test_owner_lane_without_target_stays_private(monkeypatch):
    em, captured = _emitter_with_capture(monkeypatch)

    async def run():
        # An idle musing (no partner_target) on the owner lane must NOT leak to the
        # partner — only deliberate job-result delivery does.
        await em.emit_proactive_speech("I wonder if the user likes jazz.")
        await asyncio.sleep(0)

    asyncio.run(run())
    assert captured == []


def test_agent_lane_ignores_target_and_uses_context_end_user(monkeypatch):
    em, captured = _emitter_with_capture(monkeypatch)

    async def run():
        # An agent-lane turn already knows its end-user — the context wins over any
        # passed target so a tenant turn can never be misdelivered.
        with bind_turn("agent", session_id="A", agent_id="x.y", end_user_id="cust-real"):
            await em.emit_proactive_speech("done", partner_target="cust-WRONG")
            await asyncio.sleep(0)

    asyncio.run(run())
    assert len(captured) == 1
    assert captured[0]["end_user_id"] == "cust-real"


def test_local_ui_event_is_not_relaned_by_delivery(monkeypatch):
    em, _ = _emitter_with_capture(monkeypatch)

    async def run():
        await em.emit_proactive_speech("summary", partner_target="cust-1")
        return em.get_queue().get_nowait()

    ev = asyncio.run(run())
    # The brain's own feed still sees the proactive_speech on the OWNER lane —
    # delivering to the partner must not strip it from the owner UI.
    assert ev["type"] == "proactive_speech"
    assert ev["channel"] == "owner"
    assert "route_sid" not in ev
