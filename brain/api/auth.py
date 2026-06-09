"""
Runtime API-key auth for the engine surface.

A partner's backend authenticates with a bearer key — the *runtime* credential
(distinct from the admin console login). It can open sessions and run turns, but
nothing else. Fail-closed: if no keys are configured, every request is denied, so
an accidentally-exposed server is not open by default.

Keys come from BRAIN_API_KEYS (comma-separated) or BRAIN_API_KEY (single), or the
``api_keys`` setting. Plaintext-compare for v1; hashing/rotation/per-partner scoping
is a follow-on for the full engine layer.
"""

from __future__ import annotations

import os


def configured_keys() -> set[str]:
    raw = os.environ.get("BRAIN_API_KEYS") or os.environ.get("BRAIN_API_KEY") or ""
    if not raw:
        try:
            from brain.settings import settings

            raw = str(settings.get("api_keys", "") or "")
        except Exception:
            raw = ""
    return {k.strip() for k in raw.split(",") if k.strip()}


def _extract_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    auth = authorization.strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return auth or None


def check_bearer(authorization: str | None) -> bool:
    """True iff the Authorization header carries a configured key. Fail-closed:
    no configured keys → always False."""
    keys = configured_keys()
    if not keys:
        return False
    token = _extract_token(authorization)
    return token is not None and token in keys
