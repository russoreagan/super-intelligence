"""
Webhook signatures — Stripe-scheme HMAC over the exact delivered bytes.

The header a receiver verifies:

    Elyceum-Signature: t=<unix-seconds>,v1=<hex hmac_sha256(secret, "<t>.<body>")>

Signing the timestamp together with the body is what makes a captured delivery
non-replayable: the receiver rejects a signature whose `t` is outside a tolerance
window, so an attacker cannot re-send yesterday's valid payload. `v1=` is versioned so
a future scheme can be added without breaking existing verifiers.

This is the scheme every integrator has already written a verifier for, which is the
point — the old env path sent the secret as a bearer token, which is replayable by
anyone who ever sees one delivery.
"""

from __future__ import annotations

import hashlib
import hmac

HEADER = "Elyceum-Signature"
DEFAULT_TOLERANCE_S = 300


def _mac(secret: str, timestamp: int, body: bytes) -> str:
    signed = str(timestamp).encode() + b"." + body
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def sign(secret: str, body: bytes, timestamp: int) -> str:
    """The full header value for `body` at `timestamp` (unix seconds)."""
    return f"t={timestamp},v1={_mac(secret, timestamp, body)}"


def verify(
    secret: str, body: bytes, header: str, *, now: int, tolerance_s: int = DEFAULT_TOLERANCE_S
) -> bool:
    """True iff `header` is a valid signature for `body` under `secret` and within the
    freshness window. Constant-time on the digest; tolerant of unknown extra fields so
    the scheme can grow."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    except ValueError:
        return False
    ts_raw, v1 = parts.get("t"), parts.get("v1")
    if not ts_raw or not v1:
        return False
    try:
        ts = int(ts_raw)
    except ValueError:
        return False
    if abs(now - ts) > tolerance_s:
        return False
    return hmac.compare_digest(v1, _mac(secret, ts, body))
