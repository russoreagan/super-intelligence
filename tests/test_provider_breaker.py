"""Provider circuit breaker.

A key that is out of credits or revoked fails every call identically. Without a
breaker the DMN's self-tasks re-planned against a dead Anthropic key ~30 times a
day and nothing told the org admin. The router now classifies the terminal
errors, holds that provider for a cooldown, lets one probe through when the hold
expires, and re-arms with a longer hold if the probe fails again.
"""

from __future__ import annotations

import asyncio

import pytest

import brain.model_router as mr
from brain.autonomy.reasons import DeferReason


class _Err(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        if status is not None:
            self.status_code = status


def _mk_router():
    r = mr.ModelRouter.__new__(mr.ModelRouter)
    r._provider_outage = {}
    r._bg_mode = False
    r._bg_defer_reason = None
    return r


CREDIT_MSG = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'Your credit balance is too low to access the Anthropic API.'}}"
)


def test_classification_is_billing_auth_or_none():
    cls = mr.ModelRouter.classify_provider_error
    assert cls(_Err(CREDIT_MSG, 400)) == "billing"
    assert cls(_Err("payment required", 402)) == "billing"
    assert cls(_Err("invalid x-api-key", 401)) == "auth"
    assert cls(_Err("authentication_error: invalid_api_key")) == "auth"
    # Retryable conditions never trip the breaker.
    assert cls(_Err("rate limit exceeded", 429)) is None
    assert cls(_Err("overloaded", 529)) is None
    assert cls(TimeoutError("timed out")) is None
    assert cls(_Err("max_tokens must be positive", 400)) is None


def test_terminal_error_arms_breaker_and_success_clears(monkeypatch):
    r = _mk_router()
    assert r.provider_blocked("anthropic") is None
    assert r.note_provider_error("anthropic", _Err(CREDIT_MSG, 400)) == "billing"
    o = r.provider_blocked("anthropic")
    assert o and o["kind"] == "billing" and o["strikes"] == 1
    # A model id resolves to its provider.
    assert r.provider_blocked("claude-sonnet-4-6") is o
    # Other providers untouched.
    assert r.provider_blocked("google") is None
    assert "anthropic" in r.provider_outages()
    r.note_provider_success("anthropic")
    assert r.provider_blocked("anthropic") is None
    assert r.provider_outages() == {}


def test_hold_expires_into_a_probe_and_refailure_doubles(monkeypatch):
    from brain.settings import settings

    monkeypatch.setitem(settings._data, "provider_outage_retry_s", 100.0)
    r = _mk_router()
    t = [1_000_000.0]
    monkeypatch.setattr(mr.time, "time", lambda: t[0])
    r.note_provider_error("anthropic", _Err(CREDIT_MSG, 400))
    assert r.provider_blocked("anthropic") is not None
    t[0] += 101.0  # hold expired → next call is the probe
    assert r.provider_blocked("anthropic") is None
    r.note_provider_error("anthropic", _Err(CREDIT_MSG, 400))  # probe failed
    o = r.provider_blocked("anthropic")
    assert o and o["strikes"] == 2
    assert o["until"] - t[0] == pytest.approx(200.0)  # doubled
    # The hold caps at 6h however many strikes.
    for _ in range(10):
        t[0] = o["until"] + 1
        r.note_provider_error("anthropic", _Err(CREDIT_MSG, 400))
        o = r.provider_blocked("anthropic")
    assert o["until"] - t[0] <= 6 * 3600.0


def test_non_terminal_error_leaves_breaker_closed():
    r = _mk_router()
    assert r.note_provider_error("anthropic", _Err("rate limit", 429)) is None
    assert r.provider_blocked("anthropic") is None


def test_call_defers_in_background_and_raises_interactively(monkeypatch):
    """The gate sits before dispatch: nothing is billed while the breaker is open."""
    r = _mk_router()
    r.note_provider_error("anthropic", _Err(CREDIT_MSG, 400))
    dispatched = []

    async def _boom(*a, **k):  # pragma: no cover - must never run
        dispatched.append(1)
        return "", 0, 0, 0, 0

    monkeypatch.setattr(r, "_call_anthropic", _boom)
    monkeypatch.setattr(
        r, "_resolve_model_id", lambda key, cluster: ("sonnet", "claude-sonnet-4-6")
    )
    r._local_disabled = False

    r._bg_mode = True
    out = asyncio.run(
        r.call("sonnet", "sys", [{"role": "user", "content": "hi"}], cluster="motor_cortex")
    )
    assert out == ""
    assert r._bg_defer_reason == DeferReason.PROVIDER_BLOCKED
    assert "credit" in DeferReason.PROVIDER_BLOCKED.human().lower()

    r._bg_mode = False
    with pytest.raises(mr.ProviderBlocked):
        asyncio.run(r.call("sonnet", "sys", [{"role": "user", "content": "hi"}], cluster="frontal"))
    assert dispatched == []


def test_anthropic_call_site_arms_the_breaker(monkeypatch):
    """The classification hooks the real client call, so any caller trips it."""
    r = _mk_router()

    class _Messages:
        async def create(self, **kw):
            raise _Err(CREDIT_MSG, 400)

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(r, "_get_anthropic", lambda: _Client())
    with pytest.raises(_Err):
        asyncio.run(
            r._call_anthropic("claude-sonnet-4-6", "sys", [{"role": "user", "content": "x"}])
        )
    assert r.provider_blocked("anthropic")["kind"] == "billing"


# ── Only KEY-level failures trip the org-wide breaker ──────────────────────────
# A 403 permission_error (model / beta / Managed Agents feature not permitted) used
# to match the "permission_error" text marker and hold every Anthropic call in the
# org for 30 min+, so interactive chat drafts came back empty.


def _anthropic_err(status: int, etype: str, message: str):
    import anthropic
    import httpx

    body = {"type": "error", "error": {"type": etype, "message": message}}
    resp = httpx.Response(
        status,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        json=body,
    )
    cls = {
        400: anthropic.BadRequestError,
        401: anthropic.AuthenticationError,
        403: anthropic.PermissionDeniedError,
    }.get(status, anthropic.APIStatusError)
    return cls(f"Error code: {status} - {body}", response=resp, body=body)


def test_403_permission_error_does_not_block_the_provider():
    r = _mk_router()
    err = _anthropic_err(
        403,
        "permission_error",
        "Your API key does not have permission to use the specified resource.",
    )
    assert mr.ModelRouter.classify_provider_error(err) is None
    assert r.note_provider_error("anthropic", err) is None
    assert r.provider_blocked("anthropic") is None
    # Bare-status 403s and text-only "permission_error"/"unauthorized" never trip it.
    cls = mr.ModelRouter.classify_provider_error
    assert cls(_Err("permission_error: beta not enabled for this org", 403)) is None
    assert cls(_Err("permission_error: model not available")) is None
    assert cls(_Err("upstream said unauthorized")) is None
    assert cls(_Err("see billing docs for model access", 403)) is None


def test_401_authentication_error_blocks_the_provider():
    r = _mk_router()
    err = _anthropic_err(401, "authentication_error", "invalid x-api-key")
    assert r.note_provider_error("anthropic", err) == "auth"
    assert r.provider_blocked("anthropic")["kind"] == "auth"


def test_credit_balance_400_blocks_the_provider():
    r = _mk_router()
    err = _anthropic_err(
        400,
        "invalid_request_error",
        "Your credit balance is too low to access the Anthropic API.",
    )
    assert r.note_provider_error("anthropic", err) == "billing"
    assert r.provider_blocked("anthropic")["kind"] == "billing"


def test_billing_error_type_and_quota_code_are_billing():
    cls = mr.ModelRouter.classify_provider_error
    assert cls(_anthropic_err(402, "billing_error", "payment required")) == "billing"
    # OpenAI reports an exhausted quota as a 429 with code insufficient_quota.
    quota = _Err("You exceeded your current quota", 429)
    quota.code = "insufficient_quota"
    assert cls(quota) == "billing"
    # A plain 400 that merely mentions billing is a malformed request, not a dead key.
    assert cls(_Err("invalid 'metadata.billing' field", 400)) is None


def test_wrapped_sdk_error_classifies_through_its_cause():
    cls = mr.ModelRouter.classify_provider_error
    try:
        try:
            raise _anthropic_err(401, "authentication_error", "invalid x-api-key")
        except Exception as inner:
            raise RuntimeError("drafter failed") from inner
    except RuntimeError as wrapped:
        assert cls(wrapped) == "auth"
    try:
        try:
            raise _anthropic_err(403, "permission_error", "not permitted")
        except Exception as inner:
            raise RuntimeError("drafter failed") from inner
    except RuntimeError as wrapped:
        assert cls(wrapped) is None
